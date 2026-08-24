"""Tests: Konfig-Plausibilitätsvalidierung (Punkt ⑨).

Die json_config-Validatoren (model_validator mode="after") sollen
inkonsistente Configs mit ValueError ablehnen, Defaults aber passieren.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import (
    WPSteuerungConfig,
    PVRegel,
    MindestTempEintrag,
    BatterieConfig,
    EinspeisungConfig,
    AbweichungConfig,
    ForecastConfig,
    CalculatedStartConfig,
)


# ── Defaults muessen gueltig sein ──

def test_defaults_gueltig():
    """Alle Default-Werte muessen die Validatoren passieren."""
    config = WPSteuerungConfig()
    # Kein Fehler -> Konstruktor hat geklappt
    assert config.mindest_temp.eintraege[0].min_temp_c == 38.0
    # Default ist 90% SOC (nicht 50)
    assert config.batterie.min_soc_prozent == 90.0
    assert config.einspeisung.einspeisegrenze_watt > 0


# ── PVRegel ──

def test_pv_ein_kleiner_aus():
    """einschalten_bei_c muss < ausschalten_bei_c sein."""
    with pytest.raises(ValueError, match="einschalten_bei_c"):
        PVRegel(name="PV_check", prioritaet=80, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=48.0, ausschalten_bei_c=45.0)


def test_pv_schwelle_nicht_negativ():
    """pv_schwelle_watt darf nicht negativ sein."""
    with pytest.raises(ValueError):
        PVRegel(name="PV_check", prioritaet=80, temperaturfuehler="unten",
                pv_schwelle_watt=-100.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0)


# ── MindestTemp ──

def test_mindesttemp_ende_vor_start():
    """ende_uhr muss > start_uhr sein."""
    with pytest.raises(ValueError):
        MindestTempEintrag(name="Test", temperaturfuehler="mitte",
                           min_temp_c=40.0, start_uhr=14, ende_uhr=12,
                           hysterese_k=2.0, fenster_aus_lernen=False)


def test_mindesttemp_temp_zu_niedrig():
    """min_temp_c < 20 wird abgelehnt."""
    with pytest.raises(ValueError):
        MindestTempEintrag(name="Test", temperaturfuehler="mitte",
                           min_temp_c=18.0, start_uhr=6, ende_uhr=8,
                           hysterese_k=2.0, fenster_aus_lernen=False)


def test_mindesttemp_hysterese_zu_klein():
    """hysterese_k < 0.5 wird abgelehnt."""
    with pytest.raises(ValueError):
        MindestTempEintrag(name="Test", temperaturfuehler="mitte",
                           min_temp_c=38.0, start_uhr=6, ende_uhr=8,
                           hysterese_k=0.1, fenster_aus_lernen=False)


# ── Batterie ──

def test_batterie_ein_vor_aus():
    """einschalten_bei_c < ausschalten_bei_c erforderlich."""
    with pytest.raises(ValueError):
        BatterieConfig(aktiv=True, prioritaet=75, temperaturfuehler="unten",
                       einschalten_bei_c=46.0, ausschalten_bei_c=44.0,
                       min_soc_prozent=50, max_netzbezug_watt=0)


def test_batterie_min_soc_darf_nicht_negativ():
    """min_soc_prozent < 0 ist ungueltig."""
    with pytest.raises(ValueError):
        BatterieConfig(aktiv=True, prioritaet=75, temperaturfuehler="unten",
                       einschalten_bei_c=42.0, ausschalten_bei_c=48.0,
                       min_soc_prozent=-5, max_netzbezug_watt=0)


# ── Einspeisung ──

def test_einspeisung_weiterlauf_kleiner_grenze():
    """weiterlauf_ab_watt muss <= einspeisegrenze_watt sein."""
    with pytest.raises(ValueError):
        EinspeisungConfig(aktiv=True, prioritaet=83, temperaturfuehler="unten",
                          einspeisegrenze_watt=500.0, weiterlauf_ab_watt=2000.0,
                          ausschalten_bei_c=48.0)


def test_einspeisung_ausschalten_auszerhalb():
    """ausschalten_bei_c muss zwischen 30 und 50 liegen."""
    with pytest.raises(ValueError):
        EinspeisungConfig(aktiv=True, prioritaet=83, temperaturfuehler="unten",
                          einspeisegrenze_watt=500.0, weiterlauf_ab_watt=50.0,
                          ausschalten_bei_c=55.0)


# ── Abweichung ──

def test_abweichung_ein_groesser_aus():
    """ausschalten_bei_abweichung_k < einschalten_bei_abweichung_k erforderlich."""
    with pytest.raises(ValueError):
        AbweichungConfig(aktiv=True, prioritaet=47, temperaturfuehler="unten",
                         einschalten_bei_abweichung_k=3.0,
                         ausschalten_bei_abweichung_k=5.0,
                         solltemperatur_c=40.0)


def test_abweichung_soll_zu_niedrig():
    """solltemperatur_c < 20 ist ungueltig."""
    with pytest.raises(ValueError):
        AbweichungConfig(aktiv=True, prioritaet=47, temperaturfuehler="unten",
                         einschalten_bei_abweichung_k=5.0,
                         ausschalten_bei_abweichung_k=3.0,
                         solltemperatur_c=15.0)


# ── Forecast ──

def test_forecast_niedrig_vor_hoch():
    """fc_schwelle_niedrig_wh < fc_schwelle_hoch_wh erforderlich."""
    with pytest.raises(ValueError):
        ForecastConfig(aktiv=True, prioritaet=57, t_vorheiz_ab_c=38.0,
                       tmax_c=48.0,
                       fc_schwelle_niedrig_wh=4000.0,
                       fc_schwelle_hoch_wh=1000.0)


# ── CalcStart ──

def test_calcstart_uhr_im_bereich():
    """target_uhr muss zwischen 0 und 23 liegen."""
    with pytest.raises(ValueError):
        CalculatedStartConfig(aktiv=True, prioritaet=82,
                              target_uhr=26, solltemperatur_c=40.0, tmax_c=48.0)


def test_calcstart_soll_im_bereich():
    """solltemperatur_c muss zwischen 20 und 55 liegen."""
    with pytest.raises(ValueError):
        CalculatedStartConfig(aktiv=True, prioritaet=82,
                              target_uhr=22, solltemperatur_c=18.0, tmax_c=48.0)


def test_calcstart_tmax_groesser_soll():
    """tmax_c > solltemperatur_c erforderlich."""
    with pytest.raises(ValueError):
        CalculatedStartConfig(aktiv=True, prioritaet=82,
                              target_uhr=22, solltemperatur_c=45.0, tmax_c=44.0)