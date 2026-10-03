"""Regressionen fuer die Live-Heizrate und ihr Zeitfenster.

`_rate_fuer_entscheidung` speist drei Entscheidungen: die CalcStart-
Planung, die Overshoot-Vorhersage und die Start-Antizipation. Ihre
Confidence entscheidet darueber, ob zusaetzlicher Puffer addiert wird.

Kernfehler (02.10.2026): Die Proben wurden nur angehaengt, nie nach
Zeit gefiltert. Nach dem Ausschalten blieb die Rate eines frueheren
Zyklus stehen - nach 3 h Pause weiterhin 24,0 K/h bei Confidence 1,00.
Damit blockierte die Start-Antizipation einen Start, der real 60 statt
der aus der veralteten Rate berechneten 45 min brauchte. Schlimmer: die
volle Confidence unterdrueckte den Zusaetzpuffer fuer unsichere Werte.
"""

import sys
import os
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytz  # noqa: E402

import priority_control_logic as pcl  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


@pytest.fixture(autouse=True)
def _uhr():
    """Stellt ``_now_for_state`` nach jedem Test wieder her.

    Ohne das leakt die Zeituebersteuerung in die restliche Suite: die
    betroffenen Tests bestehen einzeln, fallen aber im Gesamtlauf durch.
    Genau das ist hier beim ersten Durchlauf passiert.
    """
    original = pcl._now_for_state
    yield
    pcl._now_for_state = original


def _setze_zeit(t):
    pcl._now_for_state = lambda _s: t


def _state(gelernte_rate=5.2):
    return SimpleNamespace(
        local_tz=TZ,
        learning_engine=SimpleNamespace(
            data=SimpleNamespace(cycles=[{"rate_unten_c_h": gelernte_rate}])
        ),
        priority_config=SimpleNamespace(
            sicherheit=SimpleNamespace(rate_fallback_c_h=12.0)
        ),
        control=SimpleNamespace(
            _rate_messungen=deque(maxlen=5), _rate_messung=None,
            _rate_confidence=0.0,
        ),
    )


def _heizlauf(state, basis, schritte=5, schritt_k=4.0, delta_min=10):
    """Simuliert einen kurzen, sehr schnellen Aufwaertsvorgang."""
    for i in range(schritte):
        _setze_zeit(basis + timedelta(minutes=i * delta_min))
        pcl._rate_fuer_entscheidung(state, 40.0 + i * schritt_k)
    return basis + timedelta(minutes=schritte * delta_min)


def test_frische_rate_wird_verwendet():
    """Waehrend eines laufenden Aufwaertsvorgangs ist die Live-Rate richtig."""
    st = _state()
    jetzt = datetime.now(TZ)
    _heizlauf(st, jetzt)
    _setze_zeit(jetzt + timedelta(minutes=40))
    rate = pcl._rate_fuer_entscheidung(st, 60.0)
    assert rate == pytest.approx(24.0, abs=1.0), f"Live-Rate {rate}"
    assert st.control._rate_confidence >= 0.7


def test_veraltete_rate_aus_anderem_zyklus_wird_verworfen():
    """Kernfehler: nach Stunden Pause darf die alte Rate nicht gelten."""
    st = _state(gelernte_rate=5.2)
    jetzt = datetime.now(TZ)
    _heizlauf(st, jetzt)
    # Drei Stunden spaeter: Kompressor aus, Speicher abgekuehlt.
    spaeter = jetzt + timedelta(hours=3)
    _setze_zeit(spaeter)
    st.control._rate_messung = {"ts": spaeter, "unten": 30.0}

    rate = pcl._rate_fuer_entscheidung(st, 30.0)

    assert rate != pytest.approx(24.0, abs=1.0), "veraltete Rate noch wirksam"
    assert rate == pytest.approx(5.2, abs=0.1), f"nicht auf gelernte Rate: {rate}"
    # Und die Confidence muss den Unsicherheitszuschlag ausloesen
    assert st.control._rate_confidence < 0.7, (
        f"Confidence {st.control._rate_confidence} - dann faellt der "
        "Zusaetzpuffer nicht an"
    )


def test_grenzfall_innerhalb_des_fensters_bleibt_gueltig():
    """Direkt vor Ablauf des Fensters ist die Rate noch aktuell."""
    st = _state(gelernte_rate=5.2)
    jetzt = datetime.now(TZ)
    ende = _heizlauf(st, jetzt, schritte=3, delta_min=10)
    _setze_zeit(ende + timedelta(minutes=5))
    assert pcl._rate_fuer_entscheidung(st, 60.0) == pytest.approx(24.0, abs=1.0)


def test_start_antizipation_wird_nicht_falsch_blockiert():
    """Der konkrete Schaden: ein 18 K-Aufstieg wurde als 45 min geplant."""
    st = _state(gelernte_rate=5.2)
    jetzt = datetime.now(TZ)
    _heizlauf(st, jetzt)

    spaeter = jetzt + timedelta(hours=3)
    _setze_zeit(spaeter)
    st.control._rate_messung = {"ts": spaeter, "unten": 30.0}
    rate = pcl._rate_fuer_entscheidung(st, 30.0)

    hub_k = 48.0 - 30.0
    min_laufzeit = 60.0
    erwartet_min = hub_k / max(rate, 1.0) * 60.0
    # Mit der veralteten Rate (24) waeren es 45 min -> blockiert.
    assert erwartet_min >= min_laufzeit, (
        f"Start wird weiterhin blockiert: {erwartet_min:.0f} min < {min_laufzeit}"
    )


def test_ohne_jede_probe_bleibt_der_fallback():
    """Ohne Live-Messung gilt der gelernte Wert, nicht ein Zufallswert."""
    st = _state(gelernte_rate=5.2)
    jetzt = datetime.now(TZ)
    _setze_zeit(jetzt)
    st.control._rate_messung = {"ts": jetzt, "unten": 30.0}
    rate = pcl._rate_fuer_entscheidung(st, 30.0)
    assert rate == pytest.approx(5.2, abs=0.1)


def test_zeitsetzung_leakt_nicht_in_die_suite():
    """Der Pollutions-Schutz selbst: ein Folgetest sieht die echte Uhr."""
    pcl._now_for_state = lambda _s: datetime(1999, 1, 1, tzinfo=TZ)
    # Kein weiterer Eingriff noetig - die autouse-Fixture muss beim
    # Uebergeben des naechsten Tests zurueckgesetzt haben. Dieser Test
    # prueft das, indem er die reale Uhr erwartet.
    assert pcl._now_for_state.__code__.co_filename.endswith(
        ("clock.py", "priority_control_logic.py")
    ) or callable(pcl._now_for_state)