# -*- coding: utf-8 -*-
"""END-TO-END-Test des Schichtungs-Warmstarts ueber echte Hardware-Zyklen.

Regression 28.09.2026: Meine Annahme "der Deckel begrenzt die obere Schicht
auf 1 K" war falsch. Die Regel liefert pro Lauf "aktueller Oberwert +
schichtung_max_steig_k", und handle_compressor_off loeschte beim Erreichen
des Deckels auch die BASIS. Jeder neue Lauf baste damit auf dem hoeheren
Oberwert: 55->56, 56->57, ... bis der Ueberhitzungsschutz bei 58 C
ausloest. Erlaubt war also "1 K pro Lauf" statt "1 K insgesamt".

Wichtig: Dieser Test ruft die ECHTEN Funktionen handle_compressor_on und
handle_compressor_off auf. Ein Test, der nur Regel + Spiegelung prueft,
haette den Fehler durchgelassen - genau das war die Luecke.
"""
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import AbweichungConfig, WPSteuerungConfig  # noqa: E402
from priority_control import evaluate_abweichung  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")

ABW = AbweichungConfig(
    solltemperatur_c=42.0,
    temperaturfuehler="unten",
    einschalten_bei_abweichung_k=4.9,
    ausschalten_bei_abweichung_k=0.7,
    schichtung_min_oben_c=42.0,
    schichtung_erlaube_start=True,
    schichtung_netz_fallback_erlaubt=True,
    schichtung_max_steig_k=1.0,
    netz_notfall_offset_k=12.0,
    pv_warten_aktiv=False,
)


def _state(t_oben, t_unten=22.8, t_mittig=41.0):
    now = datetime.now(TZ)
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(
            t_unten=t_unten, t_oben=t_oben, t_mittig=t_mittig, t_verd=20.0
        ),
        priority_config=WPSteuerungConfig(),
        control=SimpleNamespace(
            kompressor_ein=False,
            blocking_reason=None,
            _soll_einschalten=True,
            restart_lockout_until=None,
            requested_rule_name=None,
            schichtung_oben_max=None,
            schichtung_oben_start=None,
            boiler_max_blockiert=None,
            _lauf_start_regel=None,
            zyklus_id=1,
        ),
        legionellen_aktiv=False,
        legionellen_temp_override=None,
        # Frische Heizraten-Messung, damit die Start-Antizipation den Start
        # nicht wegen des grossen Hubs blockiert (unten 22.8 -> 48 C waeren
        # bei der Fallback-Rate 12 C/h ueber zwei Stunden). Sie ist hier nur
        # Stornoise - getestet wird der Schichtungs-Deckel.
        stats=SimpleNamespace(
            last_compressor_on_time=now,
            # Kompressor stand laenger als die Mindestpause still.
            last_compressor_off_time=now - timedelta(hours=2),
        ),
    )
    state.control._rate_messung = {
        "ts": now - timedelta(minutes=5), "unten": t_unten - 5.0
    }
    state.control._rate_messungen = deque(
        [
            (now - timedelta(minutes=15), 30.0),
            (now - timedelta(minutes=10), 30.0),
            (now - timedelta(minutes=5), 30.0),
        ],
        maxlen=5,
    )
    state.control._rate_confidence = 0.9


@pytest.mark.asyncio
async def test_vollstaendiger_zyklus_schraubt_nicht_weg():
    """Vier echte Zyklen: Start -> heizen bis Deckel -> Stopp -> naechster Start.

    Der Oberfuehler darf nach allen Zyklen hoechstens einmal 1 K ueber die
    Basis (55.0) gewandert sein - also bei 56.0 stehen bleiben.
    """
    state = _state(t_oben=55.0)
    dekel_je_zyklus = []
    starts = []

    async def set_status(st, ein, **kw):
        st.control.kompressor_ein = ein
        return True

    for _zyklus in range(4):
        t_oben = state.sensors.t_oben
        regel = evaluate_abweichung(
            ABW,
            {"unten": state.sensors.t_unten, "mittig": 41.0, "oben": t_oben},
            False, 14, 19, 8,
            feedin_watt=0.0, soc=99.0, forecast_today_wh_qm=None,
        )
        assert regel.einschalten is True, f"Start bei oben {t_oben} faellt aus"
        pcl._spiegele_schichtungsdeckel(
            state.control, regel.regel_dict, t_oben
        )
        deckel = state.control.schichtung_oben_max
        assert deckel is not None, "kein Schichtungsdeckel gesetzt"
        dekel_je_zyklus.append(deckel)

        # Echter Start
        state.control.kompressor_ein = False
        await pcl.handle_compressor_on(
            state, None,
            regelfuehler=state.sensors.t_unten, einschaltpunkt=45.0,
            ausschaltpunkt=48.0, min_laufzeit=timedelta(minutes=30),
            min_pause=timedelta(minutes=0), t_oben=t_oben, t_mittig=41.0,
            set_kompressor_status_func=set_status,
        )
        starts.append(state.control.kompressor_ein)

        # Der Lauf heizt den Oberfuehler bis zum Deckel
        state.control.kompressor_ein = True
        state.sensors.t_oben = deckel
        await pcl.handle_compressor_off(
            state, None,
            regelfuehler=state.sensors.t_unten, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(seconds=0), t_oben=deckel,
            set_kompressor_status_func=set_status, regel_name="Abweichung",
        )

    assert all(starts), f"nicht jeder Start war moeglich: {starts}"
    assert dekel_je_zyklus == [56.0] * 4, (
        f"Deckel wanderte ueber die Zyklen: {dekel_je_zyklus} - "
        "er darf nur einmalig 56.0 sein"
    )
    assert state.sensors.t_oben == 56.0, (
        f"Oberfuehler endete bei {state.sensors.t_oben}, erwartet 56.0"
    )


@pytest.mark.asyncio
async def test_basis_ueberlebt_den_stopp_am_deckel():
    """Kontrolle: Der Start endet am Schichtungsdeckel (nicht erst bei 58 C),
    und die BASIS ueberlebt diesen Stopp - genau das verhindert das
    Hochschrauben in den naechsten Lauf hinein."""
    state = _state(t_oben=55.0)
    regel = evaluate_abweichung(
        ABW, {"unten": 22.8, "mittig": 41.0, "oben": 55.0},
        False, 14, 19, 8,
        feedin_watt=0.0, soc=99.0, forecast_today_wh_qm=None,
    )
    pcl._spiegele_schichtungsdeckel(state.control, regel.regel_dict, 55.0)
    assert state.control.schichtung_oben_max == 56.0
    assert state.control.schichtung_oben_start == 55.0

    async def set_status(st, ein, **kw):
        st.control.kompressor_ein = ein
        return True

    state.control.kompressor_ein = True
    state.sensors.t_oben = 56.0          # Deckel erreicht
    await pcl.handle_compressor_off(
        state, None, regelfuehler=22.8, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(seconds=0), t_oben=56.0,
        set_kompressor_status_func=set_status, regel_name="Abweichung",
    )
    assert state.control.kompressor_ein is False
    assert "Schichtungs-Obergrenze" in (state.control.blocking_reason or "")
    # Basis bleibt, nur der Deckel wird freigegeben.
    assert state.control.schichtung_oben_start == 55.0
    assert state.control.schichtung_oben_max is None
