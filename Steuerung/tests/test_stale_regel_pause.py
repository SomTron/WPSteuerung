"""Tests: Stale-Daten pausieren Solar-abhaengige Regeln.

Neuer Guard (Punkt ②): Wenn solar_stale=True uebergeben wird, duerfen
Einspeisung, Batterie, AdaptivePV, Zeitfenster und Forecast nicht mehr
entscheiden. MinTemp, Komfort und Abweichung bleiben als Netz-betriebene
Sicherheitsgarantien weiter aktiv.
"""
import os
import sys
from datetime import datetime


sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig, PVRegel
import priority_control as pc


def baue_config():
    config = WPSteuerungConfig()
    config.calculated_start.aktiv = False
    config.pv_regeln = [
        PVRegel(name="PV_unten", prioritaet=81, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0),
    ]
    return config


def test_stale_pausiert_pv_regeln():
    """solar_stale=True -> PV-Regeln (ohne Forecast als Backup aktiv) werden
    deaktiviert, trotz hoher PV-Leistung."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 36.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=None, forecast_today_wh_qm=2800.0,  # kein Forecast -> Backup aktiv
        soc=95.0, battery_power=1500.0,
        solar_stale=True,  # <-- Stale-Guard aktiv
    )
    for e in alle:
        if e.name.startswith("PV_"):
            assert e.aktiv is False, f"{e.name} sollte durch Stale pausiert sein"
            assert e.einschalten is None, f"{e.name} sollte keine Entscheidung treffen"
            assert "Solar-Daten veraltet" in e.grund


def test_forecast_vorhanden_pv_regeln_nur_backup():
    """PV-Exklusivitaet: Bei vorhandenem Forecast sind die statischen PV-Regeln
    stumm (AdaptivePV steuert exklusiv) - auch bei frischen Daten."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 36.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=False,
    )
    pv = next(e for e in alle if e.name.startswith("PV_"))
    assert pv.aktiv is False
    assert pv.einschalten is None
    assert "PV-Exklusivitaet" in pv.grund


def test_ohne_forecast_sind_pv_regeln_wieder_aktiv():
    """Ohne Forecast greift die PV-Regel als Backup normal (Backup-Funktion)."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 36.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=None, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=False,
    )
    pv = next(e for e in alle if e.name.startswith("PV_"))
    assert pv.aktiv is True
    assert pv.einschalten is True


def test_min_temp_komfort_abweichung_bleiben_bei_stale():
    """Bei Stale bleiben Sicherheitsgarantien (MinTemp, Komfort, Abweichung) aktiv."""
    config = baue_config()
    # MinTemp Mittag-Oben deckt 11-16 Uhr, 40C
    temp = {"oben": 38.0, "mittig": 42.0, "unten": 41.0}  # oben kalt -> MinTemp
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=200.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=True,
    )
    mt = next(e for e in alle if "MinTemp" in e.name and "Mittag" in e.name)
    assert mt.aktiv is True
    assert "Solar-Daten veraltet" not in mt.grund


def test_keine_aktiven_regeln_ohne_stale_nach_pause():
    """Wenn alle Solar-Regeln pausiert sind, soll keine Regel einschalten wollen.
    Sicherheitsgarantien (Abweichung) duerfen aber weiter AUS entscheiden."""
    from json_config import KomfortConfig
    config = baue_config()
    # Komfort per Konstruktor deaktivieren (hat kein .aktiv-Feld)
    config.komfort = KomfortConfig(prioritaet=60, notfall_einschalten_bei_c=30.0,
                                   komfort_einschalten_bei_c=30.0,
                                   ausschalten_bei_c=50.0,  # > temp=42 -> keine Entscheidung
                                   min_pv_fuer_komfort_watt=99999)
    config.adaptive_pv.aktiv = False
    config.einspeisung.aktiv = False
    config.batterie.aktiv = False
    config.wochenende.aktiv = False
    config.forecast.aktiv = False
    config.zeitfenster.aktiv = False
    # MinTemp innerhalb der Garantie (oben=44, mittag 40+2=42 -> oben=44>=42)
    # Keine PV-Regel aktiv (config.pv_regeln ist default=[])
    config.pv_regeln = []
    # Nur noch MinTemp aktiv, aber innerhalb der Garantie (oben=44 >= 40)
    temp = {"oben": 44.0, "mittig": 43.0, "unten": 42.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=True,
    )
    assert gewinner is None or gewinner.einschalten is not True, \
            "Keine Regel sollte einschalten wollen (Abweichung/Komfort duerfen AUS geben)"


def test_einspeisung_batterie_forecast_adaptive_pausiert():
    """Stale pausiert auch Einspeisung, Batterie, Forecast, AdaptivePV."""
    config = baue_config()
    config.einspeisung.aktiv = True
    config.batterie.aktiv = True
    config.forecast.aktiv = True
    config.adaptive_pv.aktiv = True
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 41.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=True,
    )
    for name in ("Einspeisung", "Batterie", "Forecast", "AdaptivePV"):
        e = next((x for x in alle if x.name == name), None)
        if e is not None:
            assert e.aktiv is False, f"{name} sollte pausiert sein"
            assert "Solar-Daten veraltet" in e.grund


def test_zeitfenster_pausiert():
    """Zeitfenster wird bei Stale ebenfalls pausiert."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 36.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=8000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
        solar_stale=True,
    )
    zf = next((e for e in alle if e.name == "Zeitfenster"), None)
    if zf is not None:
        assert zf.aktiv is False
        assert "Solar-Daten veraltet" in zf.grund