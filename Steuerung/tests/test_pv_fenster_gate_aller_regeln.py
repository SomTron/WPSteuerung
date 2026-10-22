"""Das PV-Fenster-Gate muss fuer ALLE quellenrelevanten Regeln gelten.

BUGFIX (22.10.2026): Das PV-Fenster-Gate wurde in `a00f085` eingefuehrt
("ausserhalb des PV-Fensters zaehlt PV nicht als Quelle"). Damals
bekamen aber nur drei Regeln den Parameter `pv_quelle_erlaubt`:
Abweichung, Forecast und CalculatedStart.

AdaptivePV rief `classify_energy_source` OHNE `pv_erlaubt` auf. Damit
zaehlte die PV-Messung auch ausserhalb des Fensters als Quelle - die
Regel startete nachts oder nach Sonnenuntergang aus PV. Dieselbe
Fehlerklasse wie bei der Legionellen-Regel (BUGFIX 21.10.2026).

Diese Tests nageln das Gate fuer jede quellenrelevante Regel fest.
"""
from datetime import datetime

from json_config import WPSteuerungConfig
from priority_control import bewerte_alle_regeln


_TEMP = {"unten": 30.0, "mittig": 35.0, "oben": 40.0}


def _bewerten(pv_quelle_erlaubt: bool):
    cfg = WPSteuerungConfig()
    gewinner, alle = bewerte_alle_regeln(
        config=cfg,
        temp_dict=_TEMP,
        pv_leistung=1800.0,          # deutliche Einspeisung
        kompressor_ein=False,
        now=datetime(2026, 10, 22, 15, 0),
        forecast_wh_qm=2500.0,
        forecast_today_wh_qm=2500.0,
        soc=50.0,
        pv_acpower=2000.0,            # die PV erzeugt tatsaechlich
        battery_power=0.0,
        pv_quelle_erlaubt=pv_quelle_erlaubt,
    )
    return gewinner, alle


def _regel(alle, name):
    treffer = [e for e in alle if e.name == name]
    assert treffer, f"Regel {name} fehlt in der Ergebnisliste"
    return treffer[0]


def test_adaptive_pv_startet_im_fenster():
    """Im PV-Fenster darf AdaptivePV aus PV starten."""
    _gew, alle = _bewerten(True)
    ap = _regel(alle, "AdaptivePV")
    assert ap.einschalten is True, (
        "Im PV-Fenster muss AdaptivePV starten duerfen. Grund: " + str(ap.grund))


def test_adaptive_pv_startet_nicht_ausserhalb_des_fensters():
    """Kernfall: Ausserhalb des Fensters zaehlt PV nicht als Quelle."""
    _gew, alle = _bewerten(False)
    ap = _regel(alle, "AdaptivePV")
    assert ap.einschalten is not True, (
        "Ausserhalb des PV-Fensters darf AdaptivePV nicht aus PV "
        "starten. Grund: " + str(ap.grund))


def test_der_grund_nennt_das_fenster():
    """Der Grund muss das Fenster nennen - sonst ist die Blockade fuer
    den Betreiber nicht nachvollziehbar."""
    _gew, alle = _bewerten(False)
    ap = _regel(alle, "AdaptivePV")
    assert "Fenster" in str(ap.grund), (
        "Der Grund muss das PV-Fenster nennen: " + str(ap.grund))


def test_die_regel_hat_den_parameter_ueberhaupt():
    """Verdrahtung: evaluate_adaptive_pv muss den Parameter annehmen."""
    import inspect
    from priority_control import evaluate_adaptive_pv
    params = inspect.signature(evaluate_adaptive_pv).parameters
    assert "pv_quelle_erlaubt" in params, (
        "evaluate_adaptive_pv muss das PV-Fenster-Gate annehmen koennen")


def test_der_aufrufer_reicht_es_weiter():
    """Verdrahtung: Der Aufrufer muss das Gate tatsaechlich uebergeben."""
    import pathlib
    quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control.py"
    text = quelle.read_text(encoding="utf-8")
    zeilen = [z.strip() for z in text.splitlines()
              if "pv_quelle_erlaubt=pv_quelle_erlaubt" in z]
    assert len(zeilen) >= 4, (
        "Das Gate muss an alle quellenrelevanten Regeln gereicht werden - "
        f"gefunden: {len(zeilen)}")
    # Und ganz konkret: AdaptivePV braucht es ebenfalls. Der Test muss
    # die STELLE im AdaptivePV-Block pruefen, nicht nur die Gesamtzahl -
    # sonst wuerde eine Mutation, die eine ANDER Stelle trifft,
    # unbemerkt bleiben.
    import re
    block = re.search(
        r"def evaluate_adaptive_pv\(.*?(?=\ndef )",
        text, re.S)
    assert block, "evaluate_adaptive_pv nicht gefunden"
    assert "pv_erlaubt=pv_quelle_erlaubt" in block.group(0), (
        "AdaptivePV muss das PV-Fenster-Gate an classify_energy_source "
        "uebergeben")
