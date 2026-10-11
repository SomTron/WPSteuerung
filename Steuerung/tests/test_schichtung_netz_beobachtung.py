# -*- coding: utf-8 -*-
"""V7-Beobachtung: Schichtungs-Warmstart OHNE PV/Batterie-Quelle.

Der Warmstart-Zweig der Abweichungs-Regel liegt VOR dem Quellen-Gate und
koennte theoretisch mit Netzstrom heizen. Im Bestandslog 24.08.-11.10.2026
trat das nie auf (407 Warmstarts, alle mit PV/Batterie). Diese Tests sichern
die reine BEOBACHTUNG ab: Flankenzaehler, gedrosselte Warnung, Log-Feld,
API-Kennzahl - ohne Verhaltensaenderung.
"""
import logging
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def _gewinner(netz_fallback):
    import priority_control as pc
    return pc.RegelErgebnis(
        name="Abweichung", prioritaet=47, aktiv=True, einschalten=True,
        grund="Soll 42.0C - unten 29.0C = +13.0K, oben 43.0C warm, "
              "Tiefenschutz <= 30.0C -> EIN mit Netz",
        regel_dict={"schichtung_netz_fallback": bool(netz_fallback)},
    )


def _state():
    return SimpleNamespace(
        local_tz=TZ,
        control=SimpleNamespace(
            schichtung_netz_warmstarts=0, _schichtung_netz_aktiv=False),
    )


# ─────────────────────────── Flankenzaehler ──────────────────────────────

def test_warmstart_ohne_quelle_zaehlt_als_flanke(caplog):
    state = _state()
    g = _gewinner(True)
    with caplog.at_level(logging.WARNING):
        assert pcl._beobachte_schichtung_netz(state, g) is True
        assert pcl._beobachte_schichtung_netz(state, g) is True  # gleiche Flanke
    assert state.control.schichtung_netz_warmstarts == 1
    assert any("SCHICHTUNGS-WARMSTART MIT NETZ" in r.message for r in caplog.records)


def test_warmstart_mit_quelle_zaehlt_nicht():
    state = _state()
    assert pcl._beobachte_schichtung_netz(state, _gewinner(False)) is False
    assert pcl._beobachte_schichtung_netz(state, None) is False
    assert state.control.schichtung_netz_warmstarts == 0


def test_neue_flanke_nach_ende_zaehlt_erneut():
    state = _state()
    g = _gewinner(True)
    pcl._beobachte_schichtung_netz(state, g)
    pcl._beobachte_schichtung_netz(state, _gewinner(False))  # Ende
    pcl._beobachte_schichtung_netz(state, g)                 # neue Flanke
    assert state.control.schichtung_netz_warmstarts == 2


# ─────────────────────────── Integration ─────────────────────────────────

def _min_state():
    cfg = WPSteuerungConfig()
    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=cfg,
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=43.0, t_unten=29.0, t_mittig=40.0, t_verd=30.0),
        solar=SimpleNamespace(
            feedinpower=0.0, batpower=0.0, soc=50.0,
            forecast_today=None, forecast_tomorrow=None, forecast_day2=None,
            last_api_call=jetzt - timedelta(minutes=5),
        ),
        control=SimpleNamespace(
            kompressor_ein=False, previous_modus="Keine Regel aktiv",
            aktueller_einschaltpunkt=None, aktueller_ausschaltpunkt=None,
            active_rule_name=None, active_rule_sensor=None, komfort_aktiv=False,
            alle_ergebnisse=[], _soll_einschalten=False,
            _soll_einschalten_bestaetigt=False, _lauf_start_regel=None,
            _pending_modus=None, _wechsel_historie=deque(),
            _gewinner_bestaetigt_seit=None, restart_lockout_until=None,
            boiler_max_blockiert=None, blocking_reason=None,
            schichtung_oben_max=None, schichtung_oben_start=None,
            schichtung_netz_warmstarts=0, _schichtung_netz_aktiv=False,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=jetzt - timedelta(minutes=5),
            last_compressor_off_time=jetzt - timedelta(minutes=10),
        ),
    )


@pytest.mark.asyncio
async def test_zaehler_und_logfeld_bei_netz_warmstart():
    state = _min_state()
    g = _gewinner(True)
    with patch("priority_control_logic.bewerte_alle_regeln",
               return_value=(g, [g])), \
         patch("priority_control_logic.entscheidungs_log.schreibe_eintrag") as log:
        await pcl.determine_mode_and_setpoints(state, 29.0, 40.0)

    assert state.control.schichtung_netz_warmstarts == 1
    assert log.call_args.kwargs["diagnostics"]["schichtung_netz_warmstart"] is True
