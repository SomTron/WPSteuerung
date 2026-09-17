# -*- coding: utf-8 -*-
"""Schichtungsschutz der Abweichungs-Regel + Deployment-Werte.

Hintergrund (Auswertung 15.09. aus dem Entscheidungslog):
410 von 435 Abweichungs-Entscheidungen fielen in 8-9 Uhr (direkt nach der
Nachtsperre), 237 davon bei "oben >= 42 C" - und alle ohne PV/Batterie-Quelle
(385 von 393 Lauf-Eintraegen netzgespeist). Der Schichtungs-Zweig lag VOR dem
Quellen-Gate und heizte daher mit Netzstrom, obwohl der obere Boilerteil warm
war. Deshalb steht im Deployment jetzt `schichtung_erlaube_start: false`.
"""
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import AbweichungConfig  # noqa: E402
from priority_control import evaluate_abweichung  # noqa: E402


def _cfg(**overrides):
    basis = dict(
        solltemperatur_c=44.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=4.9,
        ausschalten_bei_abweichung_k=0.7,
        schichtung_min_oben_c=42.0,
        schichtung_erlaube_start=False,
        schichtung_max_steig_k=1.0,
        netz_notfall_offset_k=12.0,
        pv_warten_aktiv=False,      # Overlay hier aus -> reine Schichtungspruefung
    )
    basis.update(overrides)
    return AbweichungConfig(**basis)


def _eval(cfg, oben, unten):
    return evaluate_abweichung(
        cfg, {"unten": unten, "mitte": oben - 0.2, "oben": oben},
        False, 9, 19, 8, feedin_watt=0.0, soc=66.0, forecast_today_wh_qm=None,
    )


def test_oben_warm_und_start_verboten_wartet():
    """Fall 05.09./08.09./13.09.: oben heiss, unten kalt, keine Quelle -> warten."""
    r = _eval(_cfg(schichtung_erlaube_start=False), oben=48.5, unten=24.6)
    assert r.einschalten is None
    assert "kein Start" in r.grund
    assert "schichtung_erlaube_start=false" in r.grund


def test_oben_warm_und_start_erlaubt_deckelt_oben():
    """Mit erlaubtem Warmstart muss der Deckel im regel_dict stehen."""
    r = _eval(_cfg(schichtung_erlaube_start=True), oben=48.5, unten=24.6)
    assert r.einschalten is True
    assert r.regel_dict["schichtung_oben_max"] == 49.5      # 48.5 + 1.0 K
    assert "Obergrenze oben" in r.grund


def test_oben_kalt_geht_den_normalen_pfad():
    """Gruppe A (28.08./03.09./14.09.): auch oben kalt -> Heizen bleibt richtig."""
    r = _eval(_cfg(schichtung_erlaube_start=False), oben=36.4, unten=31.3)
    assert r.einschalten is True
    assert "Schichtung" not in r.grund


def test_schichtungsgrenze_wird_respektiert():
    """Knapp unter 42 C greift der Schichtungszweig noch nicht."""
    r = _eval(_cfg(schichtung_min_oben_c=42.0), oben=41.9, unten=30.0)
    assert r.einschalten is True
    assert "Schichtung" not in r.grund


def test_deployment_config_hat_die_gepruefte_einstellung():
    """Guard: die optimierten Werte in wp_steuerung_parameter.json nicht verlieren."""
    pfad = os.path.join(os.path.dirname(__file__), "..", "wp_steuerung_parameter.json")
    with open(pfad, encoding="utf-8") as f:
        abw = json.load(f)["abweichung"]
    assert abw["schichtung_erlaube_start"] is False
    assert abw["netz_notfall_offset_k"] == 12
    assert abw["pv_warten_forecast_schwelle_wh_qm"] == 1200
    # unveraendert bleiben:
    assert abw["solltemperatur_c"] == 44
    assert abw["temperaturfuehler"] == "unten"