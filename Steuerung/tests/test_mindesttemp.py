"""Tests: MindestTemp-Garantien (Design-Anpassung Nutzeranforderungen).

Anforderung: "Die obere Temperatur soll mittags am besten nicht unter
40 Grad gefallen sein. Die mittlere Temperatur soll am Abend (zum
duschen) auch nicht unter 40 Grad sein."
Innerhalb der Fenster gilt die Garantie AUCH in der Nachtsperre.
"""
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402
import priority_control as pc  # noqa: E402


def baue_config():
    return WPSteuerungConfig()


def bewerte(temp_dict, now_hour, config=None):
    config = config or baue_config()
    return pc.evaluate_mindesttemp(
        config.mindest_temp, temp_dict, now_hour,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
    )


def finde(ergebnisse, name_teil):
        return next(e for e in ergebnisse if name_teil in e.name)


# ── Mittag-Oben-Fenster (11-16 Uhr) ──

def test_mittags_oben_zu_kalt_einschalten():
    erg = bewerte({"oben": 39.5, "mittig": 41.0, "unten": 38.0}, now_hour=12)
    e = finde(erg, "Mittag-Oben")
    assert e.aktiv and e.einschalten is True


def test_mittags_oben_warm_tritt_stumm_zurueck():
    # 42.5 >= 40+2 -> Garantie erfuellt -> stumm (None, kein blockierendes AUS!)
    erg = bewerte({"oben": 42.5}, now_hour=12)
    e = finde(erg, "Mittag-Oben")
    assert e.einschalten is None
    assert "Garantie erfuellt" in e.grund

    # 41.0 in Hysterese -> ebenfalls keine Aktion
    erg = bewerte({"oben": 41.0}, now_hour=12)
    e = finde(erg, "Mittag-Oben")
    assert e.einschalten is None
    assert "Hysterese" in e.grund


def test_mittags_ausserhalb_fenster_inaktiv():
    for stunde in (10, 16, 18):
        erg = bewerte({"oben": 35.0}, now_hour=stunde)
        assert not finde(erg, "Mittag-Oben").aktiv


# ── Abend-Mitte-Fenster (17-22 Uhr) ──

def test_abends_mitte_zu_kalt_einschalten():
    erg = bewerte({"oben": 44.0, "mittig": 39.0, "unten": 37.0}, now_hour=19)
    e = finde(erg, "Abend-Mitte")
    assert e.aktiv and e.einschalten is True
    # Nachtsperre (ab 19 Uhr) wird innerhalb des Fensters ueberschrieben:
    assert "Nachtsperre ueberschrieben" in e.grund


def test_abends_nachtsperren_bypass_gewinnt_gegen_abweichung():
    """Integration: Um 20 Uhr mit mitte=39 gewinnt MinTemp (65) ueber Abweichung."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 39.0, "unten": 38.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=0.0, kompressor_ein=False,
        now=__import__('datetime').datetime(2026, 1, 15, 20, 0),
        forecast_wh_qm=None,
    )
    assert gewinner is not None
    assert gewinner.name == "MinTemp-Abend-Mitte"
    assert gewinner.einschalten is True


def test_abends_mitte_warm_kein_eingriff():
    erg = bewerte({"mittig": 42.5}, now_hour=18)
    e = finde(erg, "Abend-Mitte")
    assert e.einschalten is None
    assert "Garantie erfuellt" in e.grund


def test_mindesttemp_blockiert_keine_anderen_regeln():
    """Additiv-Garantie: MinTemp-AUS-Situation darf Abweichung-EIN nicht blocken.

    Um 18 Uhr, mittig=42 (Garantie erfuellt -> None), unten=36.5:
    Abweichung soll EIN sagen und gewinnen.
    """
    from datetime import datetime as dt
    config = baue_config()
    # Komfort-EIN nicht wecken: Der Test prueft die Additivitaet von MinTemp
    # gegenueber Abweichung, nicht die Komfort-Prioritaet. Mit PV 200W wuerde
    # sonst Komfort-EIN (Prio 60, gleiche PV-Schwelle) gewinnen.
    config.komfort.min_pv_fuer_komfort_watt = 999999.0
    # oben=41.5 unterhalb der Schichtungsgrenze (42), sonst blockiert die
    # Abweichungs-Regel den Heizwunsch zu Recht
    temp = {"oben": 41.5, "mittig": 42.0, "unten": 36.5}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=200.0, kompressor_ein=False,
        now=dt(2026, 1, 15, 18, 0),
        forecast_wh_qm=None,
    )
    assert gewinner.name == "Abweichung"
    assert gewinner.einschalten is True


# ── Sensor-Ausfall ──

def test_sensor_fehlt_inaktiv():
    # Mittag-Oben um 12 Uhr (im Fenster), aber oben=None -> Sensor-Fehler
    erg = bewerte({"oben": None}, now_hour=12)
    e = finde(erg, "Mittag-Oben")
    assert not e.aktiv
    assert "nicht verfuegbar" in e.grund

    # Abend-Mitte um 19 Uhr (im Fenster), aber mittig=None -> Sensor-Fehler
    erg = bewerte({"mittig": None}, now_hour=19)
    e = finde(erg, "Abend-Mitte")
    assert not e.aktiv
    assert "nicht verfuegbar" in e.grund

    # Und zur Gegenprobe: auserhalb des Fensters steht das dort (nicht Sensor)
    erg = bewerte({"mittig": None}, now_hour=12)
    assert "Fenster" in finde(erg, "Abend-Mitte").grund


# ── Setpoint-Extraktion (Statusanzeige/Abschaltlogik) ──

@pytest.mark.parametrize("name,eps,ausp", [
    ("MinTemp-Mittag-Oben", 40.0, 42.0),
    ("Batterie", 42.0, 47.0),
])
def test_extract_setpoints(name, eps, ausp):
    from types import SimpleNamespace
    config = baue_config()
    ergebnis = SimpleNamespace(name=name)
    assert pc_priority_extract_eps(ergebnis, config) == eps
    assert pc_priority_extract_ausp(ergebnis, config) == ausp


def pc_priority_extract_eps(ergebnis, config):
    return __import__('priority_control_logic', fromlist=['x'])._extract_einschaltpunkt(ergebnis, config)


def pc_priority_extract_ausp(ergebnis, config):
    return __import__('priority_control_logic', fromlist=['x'])._extract_ausschaltpunkt(ergebnis, config)
