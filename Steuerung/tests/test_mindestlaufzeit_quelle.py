# -*- coding: utf-8 -*-
"""Regression 01.11.2026: Abweichungs-Start mit PV bekommt die kurze Schutzzeit."""
from datetime import timedelta
from types import SimpleNamespace

import priority_control_logic as pcl


def _state(quelle, pv_min=10.0, netz_min=60.0):
    return SimpleNamespace(
        priority_config=SimpleNamespace(
            zyklus=SimpleNamespace(
                pv_min_laufzeit_minuten=pv_min,
                mindestlaufzeit_minuten=netz_min,
            )
        ),
        control=SimpleNamespace(
            source_at_start=quelle,
            source_current=None,
            _lauf_start_source=None,
        ),
    )


def test_abweichung_mit_pv_haelt_die_kurze_mindestlaufzeit():
    """BEFUND 01.11.2026, 09:00: Der Schichtungs-Warmstart lief ueber die
    PV-Quelle an (unten 21 C), bekam aber die vollen 60 min Mindestlaufzeit.
    AdaptivePV meldete nach 5 min "Speicher oben voll" - der Lauf heizte
    also 55 min weiter, waehrend zwei Stunden spaeter 9 kW PV kamen.
    """
    for quelle in ("PV", "Batterie", "pv", "batterie"):
        assert pcl._effektive_mindestlaufzeit(
            _state(quelle), timedelta(minutes=60), "Abweichung"
        ) == timedelta(minutes=10), quelle


def test_abweichung_mit_netz_behaelt_die_volle_mindestlaufzeit():
    """Ein Netz-Start muss weiterhin die volle Schutzzeit halten."""
    for quelle in ("Netz", "gemischt", None, "unbekannt"):
        assert pcl._effektive_mindestlaufzeit(
            _state(quelle), timedelta(minutes=60), "Abweichung"
        ) == timedelta(minutes=60), quelle


def test_andere_regeln_bleiben_unberuehrt():
    """Nur CalcStart und Abweichung sind quellenabhaengig."""
    assert pcl._ist_pv_gesteuerter_lauf("Legionellen", "PV") is False
    assert pcl._ist_pv_gesteuerter_lauf("Einspeisung", None) is True
    assert pcl._ist_pv_gesteuerter_lauf("AdaptivePV", None) is True
    assert pcl._ist_pv_gesteuerter_lauf("CalcStart", "PV") is True
    assert pcl._ist_pv_gesteuerter_lauf("CalcStart", "Netz") is False
    assert pcl._ist_pv_gesteuerter_lauf("Abweichung", "PV") is True
    assert pcl._ist_pv_gesteuerter_lauf("Abweichung", "Netz") is False
