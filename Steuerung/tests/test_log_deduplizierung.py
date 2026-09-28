# -*- coding: utf-8 -*-
"""Regressionstest: identisch wiederholte Debug-Zeilen werden entprellt.

Beobachtung 28.09.2026, 14:46-14:48: Die Zeile

    CalcStart: Nutzungsevent 2026-09-28T13:57:51.443870 drop 0.5K ->
    Stundenbedarf -0.25h (neu 1.15h)

erschien im 10-s-Takt immer wieder. Ursache: Die Zapfungs-Korrektur wird
bei jedem Lauf neu berechnet, das Ergebnis aber bei jedem Lauf erneut
geloggt - solange das Ereignis das "letzte" blieb.

Dieselbe Klasse von Spam traf auch die Modus-Meldungen (Sommer-/Bade-/
Urlaubsmodus), die tagelang mit konstantem Inhalt liefen.
"""
import logging
import os
import sys

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import priority_control as pc  # noqa: E402
from json_config import CalculatedStartConfig  # noqa: E402


@pytest.fixture
def sammler():
    """Sammelt alle DEBUG-Meldungen waehrend des Tests."""
    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Handler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    alter_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    pc._debug_memo.clear()
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(alter_level)
        pc._debug_memo.clear()


EREIGNIS = {"timestamp": "2026-09-28T13:57:51.443870", "drop_gesamt_k": 0.5}


def _calcstart(cfg, unten, ereignisse):
    return pc.evaluate_calculated_start(
        cfg,
        {"oben": 56.1, "mittig": 43.0, "unten": unten, "verd": 20.6},
        14, 46,
        forecast_wh_qm=None,
        recent_usage_events=ereignisse,
    )


def test_zapfungs_debug_wird_entprellt(sammler):
    """12 Laeufe mit demselben Ereignis -> genau EINE Zeile.

    Die Temperaturen schwanken bewusst minimal: der errechnete
    Stundenbedarf kippt dadurch zwischen den Laeufen. Genau das darf die
    Entprellung nicht ausloesen - sonst waere der Spam nur kleiner, nicht
    weg.
    """
    cfg = CalculatedStartConfig(solltemperatur_c=42.0, target_uhr=17)
    for i in range(12):
        _calcstart(cfg, 23.0 + (i % 3) * 0.05, [EREIGNIS])
    zeilen = [m for m in sammler if "Nutzungsevent" in m]
    assert len(zeilen) == 1, f"12 Laeufe ergaben {len(zeilen)} Zeilen statt 1"


def test_neues_zapfungsereignis_loggt_wieder(sammler):
    """Ein echtes neues Ereignis muss wieder in der Logdatei stehen."""
    cfg = CalculatedStartConfig(solltemperatur_c=42.0, target_uhr=17)
    _calcstart(cfg, 23.0, [EREIGNIS])
    _calcstart(cfg, 23.0, [
        EREIGNIS,
        {"timestamp": "2026-09-28T15:10:00.0", "drop_gesamt_k": 1.2},
    ])
    zeilen = [m for m in sammler if "Nutzungsevent" in m]
    assert len(zeilen) == 2, f"erwartet 2 Zeilen, erhalten {len(zeilen)}"
    assert "15:10:00" in zeilen[1]


def test_debug_once_entprellt_nur_bei_echter_aenderung(sammler):
    """Grundverhalten des Helfers."""
    pc._debug_once("k", "erste Zeile")
    pc._debug_once("k", "erste Zeile")
    pc._debug_once("k", "erste Zeile")
    assert len(sammler) == 1
    pc._debug_once("k", "zweite Zeile")
    assert len(sammler) == 2
    assert sammler[-1] == "zweite Zeile"


def test_debug_once_identity_ueberschreibt_den_vergleich(sammler):
    """Mit `identity` zaehlt nur die Identitaet, nicht der Meldungstext."""
    pc._debug_once("k", "Text A (Wert 1.1)", identity="ereignis-1")
    pc._debug_once("k", "Text B (Wert 2.2)", identity="ereignis-1")
    assert len(sammler) == 1
    pc._debug_once("k", "Text C (Wert 3.3)", identity="ereignis-2")
    assert len(sammler) == 2
    assert sammler[-1] == "Text C (Wert 3.3)"


def test_debug_once_unterschiedliche_keys_teilen_keinen_merkspeicher(sammler):
    """Zwei Quellen duerfen sich nicht gegenseitig unterdruecken."""
    pc._debug_once("a", "gleicher Text")
    pc._debug_once("b", "gleicher Text")
    assert len(sammler) == 2


def test_die_regelungslogik_bleibt_unveraendert(sammler):
    """Die Korrektur der Heizzeit wird weiterhin JEDEM Lauf angewandt.

    Der Fix darf nur das Schreiben beeinflussen, nicht die Berechnung:
    die 0.25h muessen bei jedem Lauf abgezogen werden.
    """
    cfg = CalculatedStartConfig(solltemperatur_c=42.0, target_uhr=17)

    ohne = pc.evaluate_calculated_start(
        cfg, {"oben": 56.1, "mittig": 43.0, "unten": 23.0, "verd": 20.6},
        14, 46, forecast_wh_qm=None, recent_usage_events=None,
    )
    mit = pc.evaluate_calculated_start(
        cfg, {"oben": 56.1, "mittig": 43.0, "unten": 23.0, "verd": 20.6},
        14, 46, forecast_wh_qm=None, recent_usage_events=[EREIGNIS],
    )
    # 0.5K Drop -> 0.25h Korrektur; der Bedarf muss also kleiner sein.
    a = ohne.regel_dict["hours_needed"]
    b = mit.regel_dict["hours_needed"]
    assert b < a, f"Zapfungskorrektur wirkte nicht: {b} !< {a}"
    assert abs((a - b) - 0.25 / 3.0) < 0.01  # drop/2 / heizrate_unten
