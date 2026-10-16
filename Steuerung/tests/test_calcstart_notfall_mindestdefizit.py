# -*- coding: utf-8 -*-
"""Regression 15.10.2026: CalcStart-Notfall trotz warmem Speicher.

Log 15.10.2026, 18:37:
    CalcStart: ZU SPAET! Zeitablauf (0.0h bis 18:37 < 0.1h) -> EIN (Notfall)
    oben 48,4 C | mitte 48,0 C | unten 41,56 C

Zwei Drittel des Speichers standen auf Soll, unten fehlten 0,44 K. Der
Notfall-Zweig (`buffer_hours < 0`) startete blind - der Lauf waere sofort am
Boiler-Maximum gestorben. Neu: Bei oben UND mitte auf Soll und einem
Restdefizit unten unter `notfall_mindestdefizit_k` (Default 1,0 K) faellt kein
Notfall-Start mehr.
"""
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control as pc  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

_CFG = WPSteuerungConfig(
    **json.load(
        open(
            os.path.join(os.path.dirname(pc.__file__), "wp_steuerung_parameter.json"),
            encoding="utf-8",
        )
    )
)
CALC = _CFG.calculated_start

# Gelernte Abend-Zielzeit 18:37; der Aufruf erfolgt um 18:37.
ZIEL = 18.62
QUELLE = {"feedin_watt": 0.0, "pv_acpower": 0.0, "battery_power": -810.0, "soc": 97.0}


def test_kein_notfall_bei_warmem_speicher():
    temps = {"unten": 41.56, "mittig": 48.0, "oben": 48.44}
    e = pc.evaluate_calculated_start(
        CALC, temps, 18, 37, learned_target_hour=ZIEL, **QUELLE
    )
    assert e.einschalten is not True, e.grund
    assert "kein Notfall-Start" in e.grund


def test_notfall_bleibt_bei_echtem_defizit():
    """Deutlich kaltes Wasser unten -> Zapfgarantie bleibt wirksam."""
    temps = {"unten": 30.0, "mittig": 48.0, "oben": 48.0}
    e = pc.evaluate_calculated_start(
        CALC, temps, 18, 37, learned_target_hour=ZIEL, **QUELLE
    )
    assert e.einschalten is True, e.grund
    assert "Notfall" in e.grund
