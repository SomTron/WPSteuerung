"""Tests fuer den gemeinsamen Regelvertrag (`rule_types`).

Warum diese Tests wichtig sind: `reason_code` ist der stabile Ersatz fuer
die frueher used Freitext-/Regex-Auswertung der Regelergebnisse.
`priority_control_logic._ist_pv_unterbrechung()` entscheidet daran, ob ein
PV-Lauf weiterlaeuft oder neu bewertet wird. Bricht die Inferenz, faellt die
Weiterlauf-Logik still auf den Regex-Fallback zurueck - oder schlimmer: ein
PV-Einbruch wuerde als "start" fehlinterpretiert.

Zusaetzlich ist `RegelErgebnis.__setattr__` mehrdeutig: Bei jeder Aenderung
von `grund` oder `einschalten` wird `reason_code` neu abgeleitet, solange er
`None` oder `no_action` ist. Genau diese Re-Inferenz wird hier festgenagelt.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from rule_types import RegelErgebnis, _infer_reason_code  # noqa: E402


def mache(name, aktiv=True, einschalten=None, grund=""):
    return RegelErgebnis(
        name=name, prioritaet=50, aktiv=aktiv, einschalten=einschalten, grund=grund
    )


# ----------------------------------------------------------- Grundabkommen


def test_regelergebnis_vergibt_alle_felder():
    e = mache("Abweichung", einschalten=True, grund="zu kalt")
    assert e.name == "Abweichung"
    assert e.prioritaet == 50
    assert e.aktiv is True
    assert e.einschalten is True
    assert e.grund == "zu kalt"
    assert e.regel_dict is None


def test_reason_code_wird_bei_erzeugung_abgeleitet():
    e = mache("Abweichung", einschalten=False, grund="Ziel erreicht")
    assert e.reason_code == "stop"


def test_reason_code_ist_nicht_die_prioritaet():
    e = mache("Abweichung", einschalten=True, grund="zu kalt")
    assert e.reason_code == "start"
    assert e.prioritaet == 50


# ------------------------------------------------------------- Inferenzlogik


@pytest.mark.parametrize(
    "name,grund,erwartet",
    [
        ("Abweichung", "Nachtsperre (kein Einschalten)", "nachtsperre"),
        ("Abweichung", "Sensor 'unten' nicht verfuegbar", "sensor_unavailable"),
        ("Abweichung", "Sensorfehler: T_Oben invalid", "sensor_unavailable"),
        ("Abweichung", "Solar-Daten veraltet | STALE", "data_stale"),
        ("Legionellen", "Legionellen wartet auf PV/Batterie", "waiting_source"),
        ("Legionellen", "warte auf die Quelle", "waiting_source"),
        ("PV_mitte", "PV 100W < 315W (Basis 300W, unten=42.0C)", "pv_unterbrechung"),
        ("PV_unten", "PV 0W < 500W, keine Aktion", "pv_unterbrechung"),
        ("PV_unten", "keine Bedingung", "pv_unterbrechung"),
    ],
)
def test_sonderfaelle_werden_erkannt(name, grund, erwartet):
    assert _infer_reason_code(name, True, None, grund) == erwartet


@pytest.mark.parametrize(
    "einschalten,erwartet",
    [(True, "start"), (False, "stop"), (None, "no_action")],
)
def test_aktion_bestimmt_den_code_bei_unklaren_grund(einschalten, erwartet):
    assert _infer_reason_code("X", True, einschalten, "irgendwas") == erwartet


def test_leerer_grund_erzeugt_keinen_absturz():
    assert _infer_reason_code("X", True, None, "") == "no_action"
    assert _infer_reason_code("X", True, None, None) == "no_action"


# ------------------------------------------- Re-Inferenz bei __setattr__


def test_grund_aenderung_leitet_code_neu_ab():
    """Kernvertrag: 'no_action' darf nicht stehen bleiben, wenn sich der
    Grund aendert - sonst erkennt die PV-Weiterlauf-Logik nichts mehr."""
    e = mache("PV_mitte", einschalten=None, grund="egal")
    assert e.reason_code == "no_action"

    e.grund = "PV 100W < 315W, keine Aktion"
    assert e.reason_code == "pv_unterbrechung", (
        "PV-Unterbrechung muss nach der Grund-Aenderung erkannt werden"
    )


def test_einschalten_aenderung_leitet_code_neu_ab():
    e = mache("X", einschalten=None, grund="egal")
    assert e.reason_code == "no_action"

    e.einschalten = True
    assert e.reason_code == "start"


def test_deutlich_klassifizierter_code_bleibt_stehen():
    """Ein bewusst gesetzter Code bleibt gegenueber Aenderungen stabil."""
    e = mache("X", einschalten=False, grund="egal")
    e.set_reason_code("nachtsperre")

    e.grund = "voellig anderer Text"
    assert e.reason_code == "nachtsperre"


def test_beim_erzeugen_gesetzter_code_bleibt_stabil():
    e = RegelErgebnis(
        name="X", prioritaet=50, aktiv=True,
        einschalten=None, grund="egal", reason_code="waiting_source",
    )
    e.grund = "anderer Text"
    e.einschalten = False
    assert e.reason_code == "waiting_source"


def test_reihenfolge_der_setattr_ist_egal():
    """Regression: die Reihenfolge der Zuweisungen war Ergebnis-relevant.

    Vorher: `einschalten = True` sperrte den Code auf 'start', danach
    konnte ein 'Nachtsperre'-Grund ihn nicht mehr korrigieren.
    """
    a = mache("X", einschalten=None, grund="egal")
    a.grund = "Nachtsperre (kein Einschalten)"
    a.einschalten = True
    assert a.reason_code == "nachtsperre"

    b = mache("X", einschalten=None, grund="egal")
    b.einschalten = True
    b.grund = "Nachtsperre (kein Einschalten)"
    assert b.reason_code == "nachtsperre"


def test_pv_unterbrechung_ueberlebt_grund_wechsel():
    """Der eigentliche PV-Fall: einmal erkannt, bleibt er erkannt."""
    e = mache("PV_unten", einschalten=None, grund="PV 0W < 500W, keine Aktion")
    assert e.reason_code == "pv_unterbrechung"
    e.grund = "PV 0W < 500W, keine Aktion (unveraendert)"
    assert e.reason_code == "pv_unterbrechung"


# --------------------------------- Verbindung zur PV-Weiterlauf-Logik


def test_pv_weiterlauf_logik_benoetigt_genau_diesen_code():
    """Regression gegen die Ersetzung der Regex-Auswertung.

    Faellt der Code auf 'no_action' zurueck, sieht die Weiterlauf-Logik eine
    normale Nicht-Aktion und bewertet den PV-Lauf neu - mit Risiko eines
    Neustarts mitten im Lauf.
    """
    from priority_control_logic import _ist_pv_unterbrechung

    e = mache("PV_mitte", einschalten=None, grund="PV 100W < 315W, keine Aktion")
    assert e.reason_code == "pv_unterbrechung"
    assert _ist_pv_unterbrechung(e) is True


def test_start_und_stop_sind_keine_pv_unterbrechung():
    from priority_control_logic import _ist_pv_unterbrechung

    assert _ist_pv_unterbrechung(mache("X", einschalten=True, grund="start")) is False
    assert _ist_pv_unterbrechung(mache("X", einschalten=False, grund="stop")) is False
