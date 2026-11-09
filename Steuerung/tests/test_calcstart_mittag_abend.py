# -*- coding: utf-8 -*-
"""CalcStart-Mittag und CalcStart-Abend sind getrennte Regeln (09.11.2026).

Betriebslog 09.11.2026, 09:57:

    [ 82] EIN CalcStart-Mittag: CalcStart: ZU SPAET! Zeitablauf
          (2.1h bis 12:00 < 3.8h) -> EIN (Notfall)
    [ 82] EIN CalcStart-Abend:  CalcStart: ZU SPAET! Zeitablauf
          (2.1h bis 12:00 < 3.8h) -> EIN (Notfall)

Beide Regeln trugen denselben Text - und damit dasselbe Ziel. Zwei Ursachen:

  * ``ziel_uhr`` kam aus ``learned_target_hour`` (der gelernten
    ABEND-Zapfzeit, Default 17:00); der uebergebene ``target_mittag_uhr``
    kam nie an, weil im else-Zweig ``calc_cfg.target_uhr`` stand.
  * Der Etappenplan lief fuer BEIDE Regeln ueber alle Etappen und waehlte
    die dringendste (Mittag, 12:00).

Jetzt bewertet jede Regel nur ihre eigene Etappe und ihre eigene Zielzeit.
"""
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

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

# Log-Situation 09.11.2026, 09:57 (Puffer 2 h 3 min bis 12:00)
TEMPS = {"unten": 23.1, "mittig": 26.4, "oben": 34.6}
MIT_PV = {
    "feedin_watt": 4000.0,
    "pv_acpower": 5000.0,
    "forecast_wh_qm": 2042.0,
    "learned_heating_rate_unten": 3.0,
    "learned_heating_rate_gesamt": 2.0,
}


def _calc(name, ziel, gelernt=17.0, stunde=9, minute=57):
    return pc.evaluate_calculated_start(
        CALC, TEMPS, stunde, minute,
        learned_target_hour=gelernt,
        name_override=name,
        target_uhr_override=ziel,
        etappen_filter=[name.split("-", 1)[1].lower()],
        **MIT_PV,
    )


def test_mittag_rechnet_gegen_die_mittagszeit():
    """``target_mittag_uhr`` muss ankommen (12:00, nicht die Abendzeit)."""
    assert CALC.target_mittag_uhr == 12, "Ausgangslage der Projektkonfiguration"
    e = _calc("CalcStart-Mittag", CALC.target_mittag_uhr)
    assert e.regel_dict["target_hour"] == 12.0, e.grund
    assert "12:00" in e.grund, e.grund


def test_abend_rechnet_gegen_die_abendzeit():
    e = _calc("CalcStart-Abend", CALC.target_uhr)
    assert e.regel_dict["target_hour"] == 17.0, e.grund
    assert "17:00" in e.grund, e.grund


def test_die_beiden_regeln_tragen_nicht_mehr_denselben_text():
    mittag = _calc("CalcStart-Mittag", CALC.target_mittag_uhr)
    abend = _calc("CalcStart-Abend", CALC.target_uhr)
    assert mittag.grund != abend.grund, (
        f"beide Regeln melden denselben Text: {mittag.grund!r}")
    # Der Mittag ist um 09:57 knapp (2,1 h bis 12:00), der Abend nicht.
    assert "12:00" in mittag.grund
    assert "12:00" not in abend.grund


def test_jede_regel_bewertet_nur_ihre_eigene_etappe():
    """Sonst waehlt der Plan in beiden Regeln die dringendste Etappe."""
    mittag = _calc("CalcStart-Mittag", CALC.target_mittag_uhr)
    abend = _calc("CalcStart-Abend", CALC.target_uhr)
    assert [e["name"] for e in mittag.regel_dict["etappen"]] == ["Mittag"]
    assert [e["name"] for e in abend.regel_dict["etappen"]] == ["Abend"]


def test_gelernte_abendzeit_gilt_nicht_fuer_den_mittag():
    """Die gelernte Zapfzeit ist die ABEND-Zeit - der Mittag darf sie nicht erben."""
    e = _calc("CalcStart-Mittag", CALC.target_mittag_uhr, gelernt=18.5)
    assert e.regel_dict["target_hour"] == 12.0, e.grund
    assert "18:30" not in e.grund, e.grund


def test_gelernte_abendzeit_korrigiert_den_abend():
    """Befund 04.10.2026 bleibt gueltig: der Abend folgt der gelernten Zeit."""
    e = _calc("CalcStart-Abend", CALC.target_uhr, gelernt=18.5)
    assert e.regel_dict["target_hour"] == 18.5, e.grund
    assert "18:30" in e.grund, e.grund


def test_ohne_etappenfilter_bleibt_der_plan_vollstaendig():
    """Aufrufer ohne Etappen-Zuordnung sehen weiterhin alle Etappen."""
    e = pc.evaluate_calculated_start(
        CALC, TEMPS, 9, 57, learned_target_hour=17.0, **MIT_PV)
    assert [x["name"] for x in e.regel_dict["etappen"]] == ["Mittag", "Abend"]
