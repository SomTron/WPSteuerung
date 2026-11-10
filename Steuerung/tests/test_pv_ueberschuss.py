# -*- coding: utf-8 -*-
"""PV-Ueberschuss-Regel: Eigenverbrauch ab einstellbarer Einspeisung.

Nutzerwunsch 10.11.2026: Der Speicher soll PV-Ueberschuss verheizen, auch wenn
keine Zapf-Etappe (CalcStart) ansteht. Anders als ``Einspeisung`` (Netzlimit
7500 W) und ``PV_mitte``/``PV_unten`` (PV-Erzeugung) misst diese Regel die
EINSPEISUNG (feedinpower) - den echten Ueberschuss nach Hausverbrauch und
Batterieladung.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_config import PVUeberschussConfig  # noqa: E402
from regel_pv_ueberschuss import evaluate_pv_ueberschuss  # noqa: E402

TEMPS = {"unten": 35.0, "mittig": 40.0, "oben": 45.0}


def _cfg(**kw):
    return PVUeberschussConfig(**kw)


def _eval(cfg=None, feedin=0.0, temps=None, kompressor=False, stunde=13,
          battery=None):
    return evaluate_pv_ueberschuss(
        cfg or _cfg(), temps or dict(TEMPS), feedin, kompressor, stunde, 19, 8,
        battery_power=battery,
    )


def test_start_ab_500_w_einspeisung():
    """Die Auslegung: ab 500 W Ueberschuss heizen."""
    e = _eval(feedin=500.0)
    assert e.name == "PV_Ueberschuss"
    assert e.einschalten is True, e.grund
    assert "500W" in e.grund or "500 W" in e.grund, e.grund


def test_kein_start_unter_der_schwelle():
    e = _eval(feedin=499.0)
    assert e.einschalten is None, e.grund
    assert e.reason_code == "pv_unterbrechung"


def test_nachtsperre_blockiert_standardmaessig():
    e = _eval(feedin=2000.0, stunde=22)
    assert e.aktiv is False
    assert e.einschalten is None
    assert "Nachtsperre" in e.grund


def test_nachtsperre_kann_ueberschrieben_werden():
    e = _eval(_cfg(nachtsperre_ueberschreiben=True), feedin=2000.0, stunde=22)
    assert e.aktiv is True
    assert e.einschalten is True, e.grund


def test_aus_bei_ausschaltgrenze():
    e = _eval(feedin=3000.0, temps={"unten": 48.0, "mittig": 50.0, "oben": 52.0})
    assert e.einschalten is False, e.grund
    assert "48.0C" in e.grund


def test_kein_start_wenn_speicher_oben_voll():
    """Der Regelfuehler ist 'unten' - oben >= 55 C darf nicht gestartet werden."""
    e = _eval(feedin=3000.0, temps={"unten": 35.0, "mittig": 50.0, "oben": 56.0})
    assert e.einschalten is None, e.grund
    assert "oben 56.0C" in e.grund


def test_oben_voll_beendet_einen_laufenden_zyklus():
    e = _eval(feedin=3000.0, temps={"unten": 40.0, "mittig": 50.0, "oben": 56.0},
              kompressor=True)
    assert e.einschalten is False, e.grund


def test_weiterlauf_solange_kein_netzbezug():
    """weiterlauf_ab_watt=0: Ein laufender Zyklus bleibt an (WP zieht ~600 W)."""
    e = _eval(feedin=0.0, kompressor=True)
    assert e.einschalten is True, e.grund
    assert "Weiterlauf" in e.grund


def test_einschaltgrenze_verhindert_kurzlauf():
    """Bei unten 44 C lohnt kein Start mehr (Ziel 48, Boiler-Max)."""
    e = _eval(feedin=3000.0, temps={"unten": 44.0, "mittig": 45.0, "oben": 46.0})
    assert e.einschalten is None, e.grund
    assert "Einschaltgrenze" in e.grund


def test_legionellenfahrt_hebt_die_ausschaltgrenze():
    e = evaluate_pv_ueberschuss(
        _cfg(), {"unten": 50.0, "mittig": 52.0, "oben": 55.0}, 3000.0, False,
        13, 19, 8, legionellen_aktiv=True, legionellen_ziel_c=60.0,
    )
    assert e.einschalten is True, e.grund


def test_sensor_fehlt_macht_die_regel_inaktiv():
    e = evaluate_pv_ueberschuss(
        _cfg(), {"mittig": 40.0}, 3000.0, False, 13, 19, 8,
    )
    assert e.aktiv is False
    assert "nicht verfuegbar" in e.grund


def test_inaktive_regel_heisst_inaktiv():
    e = _eval(_cfg(aktiv=False), feedin=5000.0)
    assert e.aktiv is False
    assert "inaktiv" in e.grund


def test_name_aktiviert_die_kurze_pv_mindestlaufzeit():
    """PV_-Praefix -> _ist_pv_gesteuerter_lauf -> pv_min_laufzeit_minuten."""
    import priority_control_logic as pcl

    assert pcl._ist_pv_gesteuerter_lauf("PV_Ueberschuss", None) is True


def test_projektkonfiguration_ist_aktiv_und_bei_500_w():
    import json

    pfad = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "wp_steuerung_parameter.json",
    )
    with open(pfad, encoding="utf-8") as f:
        daten = json.load(f)
    sektion = daten.get("pv_ueberschuss", {})
    assert sektion.get("aktiv") is True, "PV-Ueberschuss-Regel muss aktiv sein"
    assert float(sektion.get("einspeisung_min_watt", 0)) == 500.0
    assert float(sektion.get("einspeisung_min_watt_sommer", 0)) == 5000.0
    assert sektion.get("nachtsperre_ueberschreiben") is False


# --- Saison: Winter 500 W, Sommer 5000 W (Nutzerwunsch 10.11.2026) ---------

def test_sommer_modus_hebt_die_einschaltschwelle():
    """Im Sommer-Modus startet PV_Ueberschuss erst bei 5000 W Einspeisung.

    Hintergrund: Im Winter ist jeder Ueberschuss wertvoll. Im Sommer waere der
    Speicher bei 500 W schon am Vormittag voll, kuehlte bis zum Abend aus und
    die Einspeisung muesste gar nicht begrenzt werden (Netzlimit 7500 W).
    """
    import priority_control_logic as pcl
    from json_config import WPSteuerungConfig

    cfg = WPSteuerungConfig()
    cfg.pv_ueberschuss = PVUeberschussConfig(
        einspeisung_min_watt=500.0,
        einspeisung_min_watt_sommer=5000.0,
        einschalten_bei_c=43.0,
        ausschalten_bei_c=48.0,
    )
    pcl.wende_sommer_offset_an(cfg)
    assert cfg.pv_ueberschuss.einspeisung_min_watt == 5000.0
    # Temperaturgrenzen wie bei den PV-Regeln abgesenkt (Offset -2 K).
    assert cfg.pv_ueberschuss.ausschalten_bei_c == 46.0
    assert cfg.pv_ueberschuss.einschalten_bei_c == 41.0


def test_ohne_sommer_schwelle_bleibt_der_winterwert():
    """0 = im Sommer gilt dieselbe Schwelle wie im Winter (altes Verhalten)."""
    import priority_control_logic as pcl
    from json_config import WPSteuerungConfig

    cfg = WPSteuerungConfig()
    cfg.pv_ueberschuss = PVUeberschussConfig(
        einspeisung_min_watt=500.0,
        einspeisung_min_watt_sommer=0.0,
    )
    pcl.wende_sommer_offset_an(cfg)
    assert cfg.pv_ueberschuss.einspeisung_min_watt == 500.0


def test_sommer_schwelle_kleiner_als_winter_wird_abgelehnt():
    with pytest.raises(Exception):
        PVUeberschussConfig(einspeisung_min_watt=1000.0,
                            einspeisung_min_watt_sommer=500.0)


# --- Batterieladung zaehlt als Ueberschuss (Nutzerwunsch 10.11.2026) ------
#
# Fall 10.11.2026, 14:47: PV 5285 W, aber Einspeisung 0 W (die Batterie lud).
# Die Regel sah "keinen Ueberschuss" und heizte nicht. Jetzt darf sie auch bei
# Batterieladung starten - aber nur bis zum Minimum-Soll unten, damit die
# Batterie nicht leergesaugt wird.

def test_batterieladung_startet_ohne_einspeisung():
    e = _eval(feedin=0.0, battery=800.0)      # batPower > 0 = Ladung
    assert e.einschalten is True, e.grund
    assert "Batterie laedt 800W" in e.grund
    assert "bis 42.0C" in e.grund


def test_batterieladung_unter_der_schwelle_startet_nicht():
    e = _eval(feedin=0.0, battery=300.0)
    assert e.einschalten is None, e.grund
    assert "keine Aktion" in e.grund


def test_batterie_anlass_heizt_nur_bis_minimum_soll():
    """Bei 42.5 C ist das Minimum-Soll erreicht -> AUS (nicht erst bei 48 C)."""
    e = _eval(feedin=0.0, battery=800.0,
              temps={"unten": 42.5, "mittig": 44.0, "oben": 46.0})
    assert e.einschalten is False, e.grund
    assert "42.0C" in e.grund


def test_einspeisung_hat_vorrang_und_heizt_bis_48():
    """Echter Einspeise-Ueberschuss -> volles Ziel 48 C, nicht 42 C."""
    e = _eval(feedin=900.0, battery=800.0,
              temps={"unten": 42.0, "mittig": 43.0, "oben": 44.0})
    assert e.einschalten is True, e.grund
    assert "bis 48.0C" in e.grund


def test_batterie_abschaltgrenze_wird_validiert():
    with pytest.raises(Exception):
        # Ueber der Ausschaltgrenze (48 C) -> unzulaessig
        PVUeberschussConfig(batterie_ausschalten_bei_c=50.0)


def test_batterie_ladung_null_bedeutet_nur_einspeisung():
    """0 = alte Logik: Batterieladung allein startet nicht."""
    e = _eval(_cfg(batterie_ladung_min_w=0.0), feedin=0.0, battery=5000.0)
    assert e.einschalten is None, e.grund


def test_projektkonfiguration_nutzt_batterieladung_bis_42():
    import json

    pfad = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "wp_steuerung_parameter.json",
    )
    with open(pfad, encoding="utf-8") as f:
        daten = json.load(f)
    sektion = daten["pv_ueberschuss"]
    assert float(sektion["batterie_ladung_min_w"]) == 500.0
    assert float(sektion["batterie_ausschalten_bei_c"]) == 42.0
