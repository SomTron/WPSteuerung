"""Sensor-Plausibilitaet: Defekte Fuehler werden als DEFEKT behandelt.

Professioneller Standard: Ein abgerissener DS18B20 meldet -127.0 C, ein
GPIO-Ausreisser +300 C. Vorher wanderten solche Werte direkt in die
Regelung (Notfall-EIN bei -127, permanenter AUS bei 300). Jetzt verwirft
_parse_sensor sie: Die Regel behandelt den Fuehler wie 'nicht verfuegbar'
(fail-safe), und bei der 'auto'-Strategie waehlt der Notfallschutz einen
gesunden Fuehler.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control as pc  # noqa: E402


def test_offener_fuehler_wird_verworfen():
    """DS18B20 open-wire: -127.0 -> None statt Notfall-EIN."""
    assert pc._parse_sensor({"unten": -127.0}, "unten") is None


def test_gpio_ausreisser_wird_verworfen():
    assert pc._parse_sensor({"oben": 300.0}, "oben") is None


def test_normalwerte_und_grenzfaelle_durchlassen():
    assert pc._parse_sensor({"unten": 22.8}, "unten") == 22.8
    assert pc._parse_sensor({"unten": 85.0}, "unten") == 85.0   # echtes 85C ok
    assert pc._parse_sensor({"unten": -5.0}, "unten") == -5.0   # kalter Verd
    assert pc._parse_sensor({"mittig": 48.5}, "mitte") == 48.5  # Alias mitte->mittig


def test_kein_wert_bleibt_none():
    assert pc._parse_sensor({"unten": None}, "unten") is None
    assert pc._parse_sensor({}, "unten") is None


def test_notfallschutz_faellt_auf_gesunden_fuehler_zurueck():
    """Defekter Fuehler darf den Schutzleiter nicht laehmen."""
    cfg = pc.NotfallschutzConfig()
    erg = pc.evaluate_notfallschutz(
        cfg,
        {"oben": 40.0, "mittig": 38.5, "unten": -127.0},
        kompressor_ein=False,
    )
    # unten ist verworfen; 'auto' waehlt oben (40.0 > 36 -> stumm), kein Crash
    assert erg.einschalten is None
    assert "oben" in erg.grund


def test_defekter_einzelfuehler_laehmt_nicht_dauerhaft():
    """Ohne Ersatz fuehrt der Defekt zum fail-safe, nicht zu Unsinn."""
    cfg = pc.NotfallschutzConfig()
    erg = pc.evaluate_notfallschutz(cfg, {"unten": -127.0}, kompressor_ein=False)
    assert erg.aktiv is False
    assert "nicht verfuegbar" in erg.grund