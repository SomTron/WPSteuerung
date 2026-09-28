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
import priority_control_logic as pcl  # noqa: E402
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
    pcl._last_heizplan_log = None
    pc._debug_memo.clear()
    try:
        yield records
    finally:
        root.removeHandler(handler)
        root.setLevel(alter_level)
        pcl._last_heizplan_log = None
        pc._debug_memo.clear()


# ────────── Heiz-/Startplan-Log (alle 15 min, zustandsabhaengig) ──────────

def _aufrufen(records, laeuft, ziel, regel_dict, jetzt=None):
    """Ruft die Meldung mit dem passenden Zustand auf."""
    from types import SimpleNamespace

    state = SimpleNamespace(
        control=SimpleNamespace(
            kompressor_ein=laeuft, active_rule_sensor="Unten"
        )
    )
    pcl._last_heizplan_log = None
    pcl._logge_heizplan(
        state,
        _Erg(regel_dict, name="CalcStart") if regel_dict is not None or True else None,
        [_Erg(regel_dict)] if regel_dict is not None else [],
        40.0, "unten", ziel, 3.0,
        jetzt or datetime(2026, 9, 28, 14, 0),
    )


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
    _aufrufen(sammler, False, 42.0, {
        "planned_start_hour": 16.33,
        "spaetester_start_hour": 15.83,
        "target_hour": 17.0,
        "hours_needed": 1.2,
        "time_left_hours": 2.7,
    })
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 1
    z = zeilen[0]
    assert "16:20" in z            # naechster Start
    assert "15:50" in z            # spaetestens
    assert "Soll 17:00" in z
    assert "Heizzeit 1.2h" in z
    assert "WP aus" in z


def test_startplan_meldet_fehlende_planung(sammler):
    """Auch 'kein Start geplant' wird geloggt - sonst ist bei stiller WP
    nicht erkennbar, ob das geplant ist oder ein Fehler vorliegt."""
    _aufrufen(sammler, False, 42.0, None)
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 1
    assert "kein Start geplant" in zeilen[0]


def test_startplan_wird_alle_15_minuten_wiederholt(sammler):
    from types import SimpleNamespace

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=False, active_rule_sensor="Unten")
    )
    calc = _Erg({"planned_start_hour": 16.0, "target_hour": 17.0})
    pcl._last_heizplan_log = None
    start = datetime(2026, 9, 28, 14, 0)
    for minute in range(0, 30, 5):
        pcl._logge_heizplan(
            state, calc, [calc], 40.0, "unten", 42.0, 3.0,
            start + timedelta(minutes=minute),
        )
    zeilen = _startplan_zeilen(sammler)
    assert len(zeilen) == 2, f"erwartet 2 Zeilen, erhalten {len(zeilen)}"


def test_startplan_wird_nicht_bei_jedem_lauf_geschrieben(sammler):
    """Der 10-s-Takt darf keine Zeile erzeugen - nur alle 15 min."""
    from types import SimpleNamespace

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=False, active_rule_sensor="Unten")
    )
    calc = _Erg({"planned_start_hour": 16.0, "target_hour": 17.0})
    pcl._last_heizplan_log = None
    start = datetime(2026, 9, 28, 14, 0)
    for sek in range(0, 120, 10):        # 12 Laeufe in 2 Minuten
        pcl._logge_heizplan(
            state, calc, [calc], 40.0, "unten", 42.0, 3.0,
            start + timedelta(seconds=sek),
        )
    assert len(_startplan_zeilen(sammler)) == 1


def test_startplan_kein_ergebnis_ist_kein_absturz(sammler):
    from types import SimpleNamespace

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=False, active_rule_sensor=None)
    )
    pcl._last_heizplan_log = None
    pcl._logge_heizplan(
        state, None, [], 40.0, "unten", 42.0, 3.0,
        datetime(2026, 9, 28, 14, 0),
    )
    assert len(_startplan_zeilen(sammler)) == 1


def test_kein_startplan_wenn_der_kompressor_laeuft(caplog):
    """Regression Nutzer-Log 28.09.2026, 19:13:

        19:13:10 Kompressor EIN ... Regelfuehler=24.437, Ausschaltgrenze=38.0
        19:13:20 Startplan (WP laeuft): kein Start geplant - Nachtsperre aktiv

    Ein Startplan ist sinnlos, wenn der Start schon geschehen ist.
    Stattdessen: Zeit bis zur Solltemperatur, mit Angabe des Fuehlers.
    """
    from datetime import datetime
    from types import SimpleNamespace

    import priority_control_logic as pcl

    class E:
        name = "Notfallschutz"
        grund = "NOTFALLSCHUTZ: oben 24.4C <= 36.0C -> EIN"
        regel_dict = None

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=True, active_rule_sensor="Oben")
    )
    pcl._last_heizplan_log = None
    with caplog.at_level(logging.INFO, logger="root"):
        pcl._logge_heizplan(
            state, E(), [], 24.437, "oben", 38.0, 3.6,
            datetime(2026, 9, 28, 19, 13, 20),
        )
    zeilen = [r.getMessage() for r in caplog.records]
    assert len(zeilen) == 1, zeilen
    z = zeilen[0]
    assert z.startswith("Heizplan (WP laeuft"), z
    assert "Startplan" not in z, "Startplan gehoert nicht in einen laufenden Lauf"
    assert "oben" in z
    assert "38.0C" in z
    assert "erreicht ca." in z
    assert "in 226 min" in z      # (38.0-24.437)/3.6*60 = 226.4


def test_ohne_heizrate_keine_falsche_zeitprognose(caplog):
    """Lieber keine Zeitangabe als eine erfundene."""
    from datetime import datetime
    from types import SimpleNamespace

    import priority_control_logic as pcl

    class E:
        name = "Notfallschutz"
        grund = "NOTFALLSCHUTZ"
        regel_dict = None

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=True, active_rule_sensor="Oben")
    )
    pcl._last_heizplan_log = None
    with caplog.at_level(logging.INFO, logger="root"):
        pcl._logge_heizplan(
            state, E(), [], 24.4, "oben", 38.0, 0.0,
            datetime(2026, 9, 28, 19, 13, 20),
        )
    z = caplog.records[0].getMessage()
    assert "keine brauchbare Heizrate" in z
    assert "erreich ca." not in z


def test_startplan_nur_wenn_kompressor_aus(caplog):
    """Ohne laufenden Kompressor bleibt der berechnete Start die Meldung."""
    from datetime import datetime
    from types import SimpleNamespace

    import priority_control_logic as pcl

    class E:
        name = "CalcStart"
        grund = "CalcStart: wartet auf PV"
        regel_dict = {
            "planned_start_hour": 16.33,
            "spaetester_start_hour": 15.83,
            "target_hour": 17.0,
            "hours_needed": 1.2,
        }

    state = SimpleNamespace(
        control=SimpleNamespace(kompressor_ein=False, active_rule_sensor=None)
    )
    pcl._last_heizplan_log = None
    with caplog.at_level(logging.INFO, logger="root"):
        pcl._logge_heizplan(
            state, E(), [E()], 40.0, "unten", 42.0, 3.0,
            datetime(2026, 9, 28, 14, 0),
        )
    z = caplog.records[0].getMessage()
    assert z.startswith("Startplan (WP aus)"), z
    assert "16:20" in z


def test_fmt_uhrzeit_uebersteht_unsinnige_werte():
    import priority_control_logic as pcl
    assert pcl._fmt_uhrzeit(None) == "?"
    assert pcl._fmt_uhrzeit("kein datum") == "?"


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
