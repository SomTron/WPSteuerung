"""Tests fuer die schema-bewusste WP-Analyse."""
import csv
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "Analyse"))

from analysis_core import (  # noqa: E402
    build_cycle_quality,
    classify_end_reason,
    classify_source,
    energy_wh,
    heating_intervals,
    read_cycles,
)


CURRENT = [
    "start", "ende", "dauer_min", "quelle", "source_at_start", "start_regel", "end_grund",
    "start_unten", "start_mittig", "start_oben", "max_unten", "max_mittig", "max_oben",
    "ueberschreitung_k", "start_verd",
]
LEGACY = [
    "start", "ende", "dauer_min", "quelle", "start_regel", "end_grund", "start_unten",
    "max_unten", "ueberschreitung_k", "start_verd",
]


def _write(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def test_cycles_schema_und_qualitaet_werden_unterschieden(tmp_path):
    path = tmp_path / "zyklen.csv"
    _write(path, CURRENT, [{
        "start": "2026-09-24 10:00:00", "ende": "2026-09-24 10:08:00",
        "dauer_min": "8", "quelle": "pv", "source_at_start": "PV",
        "start_regel": "AdaptivePV", "end_grund": "boiler_max",
        "start_unten": "40", "start_mittig": "41", "start_oben": "45",
        "max_unten": "42", "max_mittig": "43", "max_oben": "46",
        "ueberschreitung_k": "0", "start_verd": "15",
    }])
    rows, quality = read_cycles(path)
    assert quality["schema"] == "current"
    assert rows[0]["_source_quality"] == "direct"
    assert rows[0]["_end_grund_quality"] == "direct"
    assert build_cycle_quality(rows, quality)["duration_under_10_min"] == 1


def test_legacy_schema_wird_nicht_als_aktuell_ausgegeben(tmp_path):
    path = tmp_path / "zyklen.csv"
    _write(path, LEGACY, [{
        "start": "2026-09-24 10:00:00", "ende": "2026-09-24 10:08:00",
        "dauer_min": "8", "quelle": "pv", "start_regel": "AdaptivePV",
        "end_grund": "unbekannt", "start_unten": "40", "max_unten": "42",
        "ueberschreitung_k": "0", "start_verd": "15",
    }])
    rows, quality = read_cycles(path)
    assert quality["schema"] == "legacy_v1"
    assert rows[0]["_source_quality"] == "recorded"
    assert rows[0]["_end_grund_quality"] == "missing"


def test_quellen_und_endgruende_haben_eindeutige_prioritaet():
    assert classify_source({"source_at_start": "PV", "feedin_w": -500}) == ("pv", "direct")
    assert classify_source({
        "diagnostics": {"source_at_start": "Batterie"},
        "feedin_w": -500,
    }) == ("batterie", "direct")
    assert classify_source({"start_regel": "Batterie", "feedin_w": 0}) == ("batterie", "inferred_rule")
    assert classify_source({"feedin_w": 100}) == ("pv", "inferred_feedin")
    assert classify_end_reason({"reason_code": "boiler_max", "end_grund": "freitext"}) == (
        "boiler_max", "direct"
    )
    assert classify_end_reason({"end_grund": "unbekannt"}) == ("unbekannt", "missing")


def test_zeiten_und_energie_werden_zeitbasiert_und_luecken_ausgelassen():
    base = datetime(2026, 9, 24, 10, 0)
    samples = [
        (base, 1000),
        (base + timedelta(seconds=14), 2000),
        (base + timedelta(minutes=20), 5000),
    ]
    assert energy_wh(samples) == 1000 * 14 / 3600
    quality = heating_intervals([item[0] for item in samples])
    assert quality["gaps_gt_900"] == 1


def test_logparser_erkennt_aktuellen_abschluss_und_stabilen_code(tmp_path):
    import log_analyse

    log = tmp_path / "heizungssteuerung.log"
    log.write_text(
        "2026-09-24 10:00:00 +0200 INFO - Kompressor EIN (cycle=7) - "
        "Verifizierung gestartet (t_verd=15.0, t_unten=40.0)\n"
        "2026-09-24 10:05:00 +0200 INFO - Kompressor AUS (cycle=7) - "
        "Laufzeit: 0:05:00.000 reason=boiler_max\n",
        encoding="utf-8",
    )
    parsed = log_analyse.parse_log(str(log))
    assert len(parsed["zyklen"]) == 1
    assert parsed["zyklen"][0]["end_grund"] == "boiler_max"
    assert parsed["zyklen"][0]["end_grund_quality"] == "direct"
    assert parsed["quality"]["unknown_end_grund"] == 0
