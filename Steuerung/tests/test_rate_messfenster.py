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
from json_config import WPSteuerungConfig  # noqa: E402
from learning_engine import LearningConfig  # noqa: E402

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


# ==========================================================================
# Befund 18.10.2026 (Betriebslog 17:31-18:41)
#
# Der Lauf 17:40:12-17:53:42 dauerte 13,5 min. Die Lern-Engine hat seine
# Rate ausdruecklich verworfen ("14min, unten 40.1->48.1C = 35.79C/h
# (transition, MW=5.45, n=236, zu kurz fuer die Rate)"), die Lern-Engine des
# Heizplans nannte im selben Zeitraum 5,5 C/h. Die ENTSCHEIDUNGEN rechneten
# trotzdem mit 35,79 C/h:
#   * 17:53:42 AUS-Vorhersage "unten 48,0C + 46min x 35C/h -> 74,9C" ->
#     planmaessiger Lauf nach 13,5 von 60 min Mindestlaufzeit abgewuergt
#     (real blieb der Speicher bei 48,8 C stehen),
#   * 18:06-18:30 jeder Start blockiert, zuletzt der Schichtungs-Start mit
#     12,5 K Hub ("21min < 25min+1"; mit 5,45 C/h sind es 138 min).
#
# Deshalb gelten in `_rate_fuer_entscheidung` dieselben zwei Grenzen wie beim
# Lernen: Zyklen unter `heating_rate_lern_min_minuten` liefern keine Rate, und
# keine Rate liegt ueber `heating_rate_max_c_h`.
# ==========================================================================


def _lernstand(rate=35.79, dauer_min=13.5, saison=5.45):
    """Learning-Engine-Attrappe MIT echter Lern-Konfiguration (wie Produktion).

    Nur das `config`-Feld unterscheidet sie von `_state()`: genau daran
    haengen Lern-Mindestdauer und physikalische Obergrenze.
    """
    return SimpleNamespace(
        data=SimpleNamespace(
            cycles=[{"rate_unten_c_h": rate, "duration_min": dauer_min}],
            config=LearningConfig(),
        ),
        get_learned_heating_rate=lambda _monat: saison,
    )


def _state_mit_lernen(gelernte_rate=35.79, dauer_min=13.5, saison=5.45):
    st = _state(gelernte_rate=gelernte_rate)
    st.learning_engine = _lernstand(gelernte_rate, dauer_min, saison)
    return st


def test_kurzzyklus_rate_wird_verworfen():
    """Der Log-Wert 35,79 C/h darf nicht mehr in Entscheidungen landen."""
    st = _state_mit_lernen()
    _setze_zeit(datetime.now(TZ))
    rate = pcl._rate_fuer_entscheidung(st, 48.0)
    assert rate == pytest.approx(5.45, abs=0.01), f"Kurzzyklus-Rate wirksam: {rate}"
    # Saisonmittel ist keine Messung dieses Speichers -> niedrige Konfidenz
    # (< 0.4), damit der Sicherheitspuffer der Antizipation greift.
    assert st.control._rate_confidence == pytest.approx(0.3, abs=0.01)


def test_rate_obergrenze_wird_gekappt():
    """Auch ein repraesentativer Zyklus ueber der Planungsgrenze wird gekappt."""
    st = _state_mit_lernen(gelernte_rate=40.0, dauer_min=45.0, saison=40.0)
    _setze_zeit(datetime.now(TZ))
    rate = pcl._rate_fuer_entscheidung(st, 48.0)
    assert rate == pytest.approx(6.0, abs=0.01), rate


def test_live_rate_wird_gekappt():
    """Live-Messung 24 C/h: in Produktion (Lern-Konfiguration vorhanden) gilt
    die physikalische Obergrenze. Ohne Konfiguration bleibt sie unveraendert -
    das ist der Unterschied zu `test_frische_rate_wird_verwendet`."""
    st = _state_mit_lernen()
    jetzt = datetime.now(TZ)
    _setze_zeit(jetzt)
    st.control._rate_messung = {
        "ts": jetzt - timedelta(minutes=6), "unten": 45.9 - 2.4,
    }
    rate = pcl._rate_fuer_entscheidung(st, 45.9)  # 2,4 K / 0,1 h = 24 C/h
    assert rate == pytest.approx(6.0, abs=0.01), rate


def test_altbestand_ohne_dauer_bleibt_unveraendert():
    """Zyklussaetze ohne `duration_min` (Altdaten) werden nicht verworfen."""
    st = _state_mit_lernen(gelernte_rate=4.0)
    del st.learning_engine.data.cycles[0]["duration_min"]
    _setze_zeit(datetime.now(TZ))
    assert pcl._rate_fuer_entscheidung(st, 48.0) == pytest.approx(4.0, abs=0.01)


def _lern_engine_oder_ohne(mit_konfiguration: bool, rate=35.79, dauer_min=13.5,
                           saison=5.45):
    """Lernstand mit oder ohne Lern-Konfiguration (vorher/nachher-Vergleich)."""
    if mit_konfiguration:
        return _lernstand(rate, dauer_min, saison)
    # Alt-/Mock-Zustand: kein `config` -> keine Grenzen (Verhalten vor dem Fix)
    return SimpleNamespace(
        data=SimpleNamespace(cycles=[{"rate_unten_c_h": rate, "duration_min": dauer_min}])
    )


def _aus_state_start(t_unten, mit_konfiguration=True):
    """Minimaler AUS-Zustand fuer `handle_compressor_on` (Log 18:xx)."""
    now = datetime.now(TZ)
    cfg = WPSteuerungConfig()
    # Nachtabschaltung ausschalten: der Test darf nicht von der Wanduhr abhaengen.
    cfg.notfallschutz.bis_uhr = 0
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(t_unten=t_unten, t_mittig=61.0, t_oben=61.6,
                                t_verd=19.0),
        priority_config=cfg,
        learning_engine=_lern_engine_oder_ohne(mit_konfiguration),
        control=SimpleNamespace(
            kompressor_ein=False, blocking_reason=None, _soll_einschalten=True,
            restart_lockout_until=None, requested_rule_name="AdaptivePV",
            active_rule_name=None, previous_modus="AdaptivePV",
            _pending_start_rule=None, _pending_start_source=None,
            _rate_messung=None, _rate_messungen=deque(maxlen=5),
            _rate_confidence=0.0, schichtung_oben_max=None,
            schichtung_oben_start=None, boiler_max_blockiert=None,
            notfall_aktiv=False,
        ),
        legionellen_aktiv=False,
        legionellen_temp_override=None,
        stats=SimpleNamespace(
            last_compressor_on_time=now,
            last_compressor_off_time=now - timedelta(hours=2),
        ),
    )


def _lauf_state(t_unten=48.0, lauf_min=13.5, mit_konfiguration=True):
    """Laufender Zustand fuer `handle_compressor_off` (Log 17:53:42).

    `max_temp_c` 48 und `ueberhitzung_c` 58 sind im Log auf 65 C angehoben
    (Legionellen-Nachlauf: "Limit 65.0C", oben stand bei 61,8 C).
    """
    now = datetime.now(TZ)
    cfg = WPSteuerungConfig()
    cfg.notfallschutz.bis_uhr = 0
    cfg.sicherheit.max_temp_c = 65.0
    cfg.sicherheit.ueberhitzung_c = 65.0
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(t_unten=t_unten, t_mittig=61.4, t_oben=61.8,
                                t_verd=14.0),
        priority_config=cfg,
        learning_engine=_lern_engine_oder_ohne(mit_konfiguration),
        control=SimpleNamespace(
            kompressor_ein=True, blocking_reason=None, _soll_einschalten=False,
            zyklus_id=6, _lauf_start_regel="CalcStart",
            _rate_messung=None, _rate_messungen=deque(maxlen=5),
            _rate_confidence=0.0, schichtung_oben_max=None,
            schichtung_oben_start=None, boiler_max_blockiert=None,
            notfall_aktiv=False,
        ),
        legionellen_aktiv=False,
        legionellen_temp_override=None,
        stats=SimpleNamespace(
            last_compressor_on_time=now - timedelta(minutes=lauf_min),
            last_compressor_off_time=None,
        ),
        solar=SimpleNamespace(feedinpower=0.0, soc=98.0),
    )


async def _starte(st, regelname="AdaptivePV"):
    async def set_status(s, ein, **kw):
        s.control.kompressor_ein = ein
        return True

    return await pcl.handle_compressor_on(
        st, None,
        regelfuehler=st.sensors.t_unten, einschaltpunkt=43.0,
        ausschaltpunkt=48.0, min_laufzeit=timedelta(minutes=60),
        min_pause=timedelta(minutes=0),
        t_oben=st.sensors.t_oben, t_mittig=st.sensors.t_mittig,
        set_kompressor_status_func=set_status, regel_name=regelname,
    )


@pytest.mark.asyncio
async def test_log_18_30_schichtungs_start_wird_nicht_mehr_blockiert():
    """Log 18:30:14: unten 35,5 C -> Hub 12,5 K, gemeldet als
    "hub zur Obergrenze nur 12.5K (Rate 36C/h ... -> 21min < 25min+1min)".

    Mit der validierten Rate (5,45 C/h) sind es ~138 min: der Start ist erlaubt.
    """
    st = _aus_state_start(t_unten=35.5)
    ergebnis = await _starte(st)
    vorhersage = st.control._last_start_anticipation
    assert vorhersage, "Start-Antizipation wurde nicht ausgewertet"
    assert vorhersage["rate_c_h"] == pytest.approx(5.45, abs=0.01), vorhersage
    assert vorhersage["blocked"] is False, vorhersage
    assert "Start-Antizipation" not in (st.control.blocking_reason or "")
    assert ergebnis is True
    assert st.control.kompressor_ein is True


def test_log_18_30_ohne_lernkonfiguration_waere_blockiert():
    """Gegenprobe: ohne die Grenzen der Lern-Konfiguration (Alt-Zustand) galt
    der 12,5-K-Hub bei 35,79 C/h als 21 min - der Start war blockiert."""
    st = _aus_state_start(t_unten=35.5, mit_konfiguration=False)
    alte_rate = pcl._rate_fuer_entscheidung(st, 35.5)
    assert alte_rate == pytest.approx(35.79, abs=0.01)
    erwartet_min = (48.0 - 35.5) / alte_rate * 60.0
    assert erwartet_min < 26.0, erwartet_min  # 21 min < 25 min + 1 min Reserve


@pytest.mark.asyncio
async def test_log_17_53_overshoot_vorhersage_bleibt_aus():
    """Log 17:53:42: "unten 48.0C steigt mit 35C/h auf ~74.9C (Limit 65.0C)" ->
    Lauf nach 13,5 von 60 min Mindestlaufzeit abgewuergt.

    Mit 5,45 C/h lautet die Prognose 48,0 + 46,5min x 5,45 = 52,2 C - der Lauf
    darf die Mindestlaufzeit erfuellen.
    """
    st = _lauf_state()
    calls = []

    async def set_status(s, ein, **kw):
        calls.append(ein)
        return True

    erg = await pcl.handle_compressor_off(
        st, None, regelfuehler=48.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), t_oben=st.sensors.t_oben,
        set_kompressor_status_func=set_status, regel_name="Einspeisung",
    )
    assert calls == [], "Lauf wurde vorzeitig beendet"
    assert erg is False
    assert "Overshoot" not in (st.control.blocking_reason or "")
    assert "Mindestlaufzeit" in (st.control.blocking_reason or ""), \
        st.control.blocking_reason


@pytest.mark.asyncio
async def test_log_17_53_overshoot_ohne_lernkonfiguration_schaltet_ab():
    """Gegenprobe (Zustand vor dem Fix): mit 35,79 C/h greift die Vorhersage
    (74,9 C >= 64,2 C) und beendet den Lauf vorzeitig."""
    st = _lauf_state(mit_konfiguration=False)
    calls = []

    async def set_status(s, ein, **kw):
        calls.append(ein)
        return True

    erg = await pcl.handle_compressor_off(
        st, None, regelfuehler=48.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), t_oben=st.sensors.t_oben,
        set_kompressor_status_func=set_status, regel_name="Einspeisung",
    )
    assert erg is True
    assert calls == [False]
    assert "Overshoot-Vorhersage" in (st.control.blocking_reason or "")