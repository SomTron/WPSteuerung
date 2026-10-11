# -*- coding: utf-8 -*-
"""Regression: AUS-Zweige der EIN-Regeln bei tmax_c (Befund V2).

Anlass: Im Entscheidungslog zeigte `AdaptivePV` und `CalcStart` 0x AUS. Das
liess vermuten, sie haetten keinen AUS-Zweig. Tatsaechlich haben BEIDE einen
eigenen AUS bei `tmax_c` (48 C) - im Log erscheint dort nur `Einspeisung`
(Prio 85), weil sie den 48-C-Deckel hoeher priorisiert mittraegt. Diese Tests
halten beide Fakten fest, damit der Deckel nicht versehentlich verwaist.
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from priority_control import (  # noqa: E402
    bewerte_alle_regeln,
    evaluate_adaptive_pv,
    evaluate_calculated_start,
)
from json_config import (  # noqa: E402
    AdaptivePVConfig,
    CalculatedStartConfig,
    WPSteuerungConfig,
)

TEMPS_48 = {"unten": 48.0, "mittig": 48.0, "oben": 50.0, "verd": 20.0}


def test_adaptivepv_schaltet_bei_tmax_ab():
    cfg = AdaptivePVConfig()
    erg = evaluate_adaptive_pv(
        cfg, TEMPS_48, pv_leistung=5000.0, forecast_wh_qm=None,
        kompressor_ein=False, now_hour=12,
    )
    assert erg.einschalten is False
    assert "-> AUS" in erg.grund


def test_calcstart_schaltet_bei_tmax_ab():
    cfg = CalculatedStartConfig()
    erg = evaluate_calculated_start(cfg, TEMPS_48, now_hour=12, now_minute=0)
    assert erg.einschalten is False
    assert "-> AUS" in erg.grund


def test_einspeisung_traegt_das_48c_aus():
    """Bei 48 C gewinnt `Einspeisung` (85) den Deckel gegen AdaptivePV (78)."""
    cfg = WPSteuerungConfig()
    gewinner, alle = bewerte_alle_regeln(
        config=cfg,
        temp_dict=TEMPS_48,
        pv_leistung=3000.0,
        kompressor_ein=True,
        now=datetime(2026, 6, 10, 13, 0),  # Mittwoch, keine Nachtsperre
    )
    assert gewinner is not None
    assert gewinner.name == "Einspeisung"
    assert gewinner.einschalten is False
    # AdaptivePV hat den AUS-Zweig trotzdem - er wird nur ueberstimmt.
    adaptive = next(e for e in alle if e.name == "AdaptivePV")
    assert adaptive.einschalten is False
