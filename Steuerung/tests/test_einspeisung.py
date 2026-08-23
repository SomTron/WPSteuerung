"""Tests: Einspeise-Begrenzungs-Regel (PV-Shaping am Netzlimit 7500W).

Betriebsvorgabe: "es darf nicht mehr als 7500W eingespeist werden, somit
waere bei viel PV-Strom ideal diesen Zeitraum zu nutzen."
"""
import os
import sys
from datetime import datetime

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402
import priority_control as pc  # noqa: E402


def baue_config():
    config = WPSteuerungConfig()
    config.calculated_start.aktiv = False
    return config


def bewerte(temp_dict, feedin, kompressor_ein=False, now_hour=12, config=None):
    config = config or baue_config()
    return pc.evaluate_einspeisung(
        config.einspeisung, temp_dict, feedin, kompressor_ein,
        now_hour, config.sicherheit.nachtsperre_start,
        config.sicherheit.nachtsperre_ende,
    )


# ── Einschalten am Netzlimit ──

def test_einspeisung_an_grenze_einschalten():
    erg = bewerte({"unten": 43.0}, feedin=7600.0)
    assert erg.einschalten is True
    assert "PV-Shaping" in erg.grund


def test_unter_grenze_keine_aktion():
    erg = bewerte({"unten": 43.0}, feedin=7000.0)
    assert erg.einschalten is None
    assert "keine Aktion" in erg.grund or "<" in erg.grund


# ── Weiterlauf mit Abschlag ──

def test_weiterlauf_solange_ueberschuss():
    """WP laeuft (zieht ~600W) -> Einspeisung faellt auf ~6900W; weiterlaufen."""
    erg = bewerte({"unten": 44.0}, feedin=6900.0, kompressor_ein=True)
    assert erg.einschalten is True
    assert "Weiterlauf" in erg.grund


def test_aus_wenn_ueberschuss_wegfaellt():
    erg = bewerte({"unten": 44.0}, feedin=5000.0, kompressor_ein=True)
    assert erg.einschalten is None


def test_aus_bei_zielt_temperatur():
    erg = bewerte({"unten": 48.0}, feedin=8000.0)
    assert erg.einschalten is False


# ── Nachtsperre / Sensor ──

def test_nachts_inaktiv():
    erg = bewerte({"unten": 40.0}, feedin=9000.0, now_hour=22)
    assert not erg.aktiv


def test_sensor_fehlt_inaktiv():
    erg = bewerte({"unten": None}, feedin=9000.0)
    assert not erg.aktiv


# ── Integration: Prioritaet ──

def test_integration_gewinnt_gegen_calcstart_und_batterie():
    """Einspeisung (83) schlaegt CalcStart (82) und Batterie (75)."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 41.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=7800.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=3000.0,
        soc=95.0, battery_power=1500.0,
    )
    namen = {e.name for e in alle}
    assert "Einspeisung" in namen
    assert gewinner.name == "Einspeisung"
    assert gewinner.einschalten is True


def test_integration_mindestgarantie_bleibt_vorrangig_beim_aus():
    """MindestTemp-EIN (65) verliert gegen Einspeisung-EIN (83) - aber wenn die
    Einspeisungs-Regel AUS sagt (Boiler warm), gewinnt sie nicht ueber eine
    Garantieverletzung hinaus... Pruefung: Bei warmem Boiler unten=48 ist
    MinTemp eh stumm; hier nur dokumentiertes Verhalten pruefen."""
    config = baue_config()
    config.zeitfenster.aktiv = False  # wuerde sonst bei PV>=410W EIN sagen
    temp = {"oben": 48.5, "mittig": 47.0, "unten": 48.2}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=7800.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=None, soc=None,
    )
    # Boiler komplett warm: Alles sagt AUS oder nichts -> kein EIN irgendwo
    eins_ent = [e for e in alle if e.einschalten is True]
    assert eins_ent == []
    assert gewinner is None or gewinner.einschalten is False
