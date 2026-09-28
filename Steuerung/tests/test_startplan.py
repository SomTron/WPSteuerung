# -*- coding: utf-8 -*-
"""Tests: 15-min-Startplan-Log und zeitlich begrenzter Zapfungs-Abzug."""
import logging
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

import priority_control as pc  # noqa: E402
from json_config import CalculatedStartConfig, WPSteuerungConfig  # noqa: E402
from learning_engine import LearningEngine  # noqa: E402


@pytest.fixture
def sammler():
    records = []

    class Handler(logging.Handler):
        def emit(self, record):
            records.append((record.levelno, record.getMessage()))

    handler = Handler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    alter_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    pc._last_startplan_log = None
    pc._debug_memo.clear()
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(alter_level)
        pc._last_startplan_log = None
        pc._debug_memo.clear()


# ─────────────────────── Startplan-Log (alle 15 min) ───────────────────────

def _startplan_zeilen(records):
    return [m for _level, m in records if m.startswith("Startplan")]


def test_fmt_stunde_rundet_auf_minuten():
    assert pc._fmt_stunde(17.0) == "17:00"
    assert pc._fmt_stunde(16.5) == "16:30"
    assert pc._fmt_stunde(16.333) == "16:20"
    assert pc._fmt_stunde(0.0) == "00:00"


def test_fmt_stunde_klemmt_ungueltige_werte():
    assert pc._fmt_stunde(-5.0) == "00:00"
    assert pc._fmt_stunde(99.0) == "24:00"
    assert pc._fmt_stunde("keine zahl") == "?"
    assert pc._fmt_stunde(None) == "?"


class _Erg:
    def __init__(self, regel_dict, grund="", name="CalcStart"):
        self.regel_dict = regel_dict
        self.grund = grund
        self.name = name


def test_startplan_loggt_geplanten_start(sammler):
    calc = _Erg({
        "planned_start_hour": 16.33,
        "spaetester_start_hour": 15.83,
        "target_hour": 17.0,
        "hours_needed": 1.2,
        "time_left_hours": 2.7,
        "effective_buffer_hours": 0.4,
    })
    pc._logge_startplan(datetime(2026, 9, 28, 14, 0), calc, False)
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 1
    z = zeilen[0]
    assert "16:20" in z            # naechster Start
    assert "15:50" in z            # spaetestens
    assert "Soll 17:00" in z
    assert "Heizzeit 1.2h" in z
    assert "WP aus" in z


def test_startplan_zeigt_laufenden_kompressor(sammler):
    calc = _Erg({"planned_start_hour": 16.0, "target_hour": 17.0})
    pc._logge_startplan(datetime(2026, 9, 28, 14, 0), calc, True)
    assert "WP laeuft" in _startplan_zeilen(sammler)[0]


def test_startplan_meldet_fehlende_planung(sammler):
    """Auch 'kein Start geplant' wird geloggt - sonst ist bei stiller WP
    nicht erkennbar, ob das geplant ist oder ein Fehler vorliegt."""
    calc = _Erg(None, grund="Nachtsperre aktiv")
    pc._logge_startplan(datetime(2026, 9, 28, 14, 0), calc, False)
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 1
    assert "kein Start geplant" in zeilen[0]
    assert "Nachtsperre" in zeilen[0]


def test_startplan_wird_alle_15_minuten_wiederholt(sammler):
    calc = _Erg({"planned_start_hour": 16.0, "target_hour": 17.0})
    start = datetime(2026, 9, 28, 14, 0)
    for minute in range(0, 30, 5):
        pc._logge_startplan(start + timedelta(minutes=minute), calc, False)
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 2, f"erwartet 2 Zeilen, erhalten {len(zeilen)}"


def test_startplan_wird_nicht_bei_jedem_lauf_geschrieben(sammler):
    """Der 10-s-Takt darf keine Zeile erzeugen - nur alle 15 min."""
    calc = _Erg({"planned_start_hour": 16.0, "target_hour": 17.0})
    start = datetime(2026, 9, 28, 14, 0)
    for sek in range(0, 120, 10):        # 12 Laeufe in 2 Minuten
        pc._logge_startplan(start + timedelta(seconds=sek), calc, False)
    assert len(_startplan_zeilen(sammler)) == 1


def test_startplan_kein_ergebnis_ist_kein_absturz(sammler):
    pc._logge_startplan(datetime(2026, 9, 28, 14, 0), None, False)
    assert len(_startplan_zeilen(sammler)) == 1


# ───────────────── Zapfungs-Abzug nur solange unkompensiert ─────────────────

@pytest.fixture
def engine(tmp_path):
    return LearningEngine(data_path=str(tmp_path / "lern.json"))


def _event(engine, ts):
    engine.data.usage_events.append(
        {"timestamp": ts.isoformat(), "drop_gesamt_k": 0.5}
    )


def _zyklus(engine, ende):
    engine.data.cycles.append(
        {"end_time": ende.isoformat(), "duration_min": 30.0}
    )


def test_zapfung_ohne_heizzyklus_wirkt(engine):
    jetzt = datetime(2026, 9, 28, 14, 48)
    _event(engine, datetime(2026, 9, 28, 13, 57))
    treffer = engine.get_recent_usage_events(hours=2, now=jetzt)
    assert len(treffer) == 1, "Frischer Abfall muss weiterhin wirken"


def test_zapfung_nach_heizzyklus_wirkt_nicht_mehr(engine):
    """Der Kern: nach einem Heizzyklus ist der Verlust ausgeglichen.
    Vorher zog derselbe Abzug ueber Stunden weiter den Start vor."""
    jetzt = datetime(2026, 9, 28, 14, 48)
    _event(engine, datetime(2026, 9, 28, 13, 57))
    _zyklus(engine, datetime(2026, 9, 28, 14, 30))   # 33 min nach der Zapfung
    treffer = engine.get_recent_usage_events(hours=2, now=jetzt)
    assert treffer == [], "kompensierter Abfall darf den Start nicht verfruehen"


def test_heizzyklus_ohne_effekt_behaelt_wirkung(engine):
    """Ein Zyklus VOR der Zapfung darf die Wirkung nicht aufheben."""
    jetzt = datetime(2026, 9, 28, 14, 48)
    _zyklus(engine, datetime(2026, 9, 28, 13, 0))
    _event(engine, datetime(2026, 9, 28, 13, 57))
    treffer = engine.get_recent_usage_events(hours=2, now=jetzt)
    assert len(treffer) == 1


def test_juengere_zapfung_nach_heizzyklus_wirkt(engine):
    jetzt = datetime(2026, 9, 28, 18, 0)
    _zyklus(engine, datetime(2026, 9, 28, 14, 30))
    _event(engine, datetime(2026, 9, 28, 17, 30))   # Dusche nach dem Heizen
    treffer = engine.get_recent_usage_events(hours=2, now=jetzt)
    assert len(treffer) == 1, "eine spaetere Zapfung ist wieder unkompensiert"


def test_filter_laesst_sich_abschalten(engine):
    """Rueckwaertskompatibilitaet: mit nur_unkompensierte=False zaehlt
    wieder das reine Zeitfenster."""
    jetzt = datetime(2026, 9, 28, 14, 48)
    _event(engine, datetime(2026, 9, 28, 13, 57))
    _zyklus(engine, datetime(2026, 9, 28, 14, 30))
    assert engine.get_recent_usage_events(hours=2, now=jetzt) == []
    assert len(
        engine.get_recent_usage_events(hours=2, now=jetzt, nur_unkompensierte=False)
    ) == 1


def test_zeitfenster_bleibt_wirksam(engine):
    """Ausserhalb des Zeitfensters faellt der Abzug auch ohne Zyklus weg."""
    jetzt = datetime(2026, 9, 28, 20, 0)
    _event(engine, datetime(2026, 9, 28, 13, 57))
    assert engine.get_recent_usage_events(hours=2, now=jetzt) == []


def test_kaputter_zyklus_eintrag_bricht_nicht_ab(engine):
    """Ein Zyklus ohne parsebares end_time darf die Zapfung nicht
    unerklaerbar verschwinden lassen."""
    jetzt = datetime(2026, 9, 28, 14, 48)
    _event(engine, datetime(2026, 9, 28, 13, 57))
    engine.data.cycles.append({"end_time": "kein datum"})
    treffer = engine.get_recent_usage_events(hours=2, now=jetzt)
    assert len(treffer) == 1


def test_config_hat_nutzungsfenster():
    cfg = WPSteuerungConfig()
    assert cfg.calculated_start.nutzung_fenster_h == 2.0


def test_validator_prueft_nutzungsfenster():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CalculatedStartConfig(nutzung_fenster_h=-1.0)
    with pytest.raises(ValidationError):
        CalculatedStartConfig(nutzung_fenster_h=99.0)
