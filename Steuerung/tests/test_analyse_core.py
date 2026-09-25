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
    read_jsonl,
    build_quality_report,
    parse_timestamp,
    get_git_revision,
)


CURRENT = [
    "start", "ende", "dauer_min", "quelle", "source_at_start", "start_regel", "end_grund", "reason_code",
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


def test_git_revision_hat_environment_vorrang(monkeypatch):
    monkeypatch.setenv("WPS_GIT_REVISION", "env-revision")
    assert get_git_revision() == "env-revision"


def test_git_revision_fallback_ohne_environment_ist_ermittelbar(monkeypatch):
    monkeypatch.delenv("WPS_GIT_REVISION", raising=False)
    revision = get_git_revision()
    assert revision == "unknown" or (len(revision) >= 4 and " " not in revision)


def test_cycles_schema_und_qualitaet_werden_unterschieden(tmp_path):

    path = tmp_path / "zyklen.csv"
    _write(path, CURRENT, [{
        "start": "2026-09-24 10:00:00", "ende": "2026-09-24 10:08:00",
        "dauer_min": "8", "quelle": "pv", "source_at_start": "PV",
        "start_regel": "AdaptivePV", "end_grund": "boiler_max", "reason_code": "boiler_max",
        "start_unten": "40", "start_mittig": "41", "start_oben": "45",
        "max_unten": "42", "max_mittig": "43", "max_oben": "46",
        "ueberschreitung_k": "0", "start_verd": "15",
    }])
    rows, quality = read_cycles(path)
    assert quality["schema"] == "current_v2"
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
    assert parsed["zyklen"][0]["reason_code"] == "boiler_max"
    assert parsed["quality"]["unknown_end_grund"] == 0


def test_decision_diagnostics_und_qualitaetsbericht_werden_ausgewertet(tmp_path):
    path = tmp_path / "entscheidungs_log.jsonl"
    path.write_text(
        '{"ts":"2026-09-24 10:00:00+00:00","diagnostics":{"source_at_start":"Batterie"},'
        '"reason_code":"warte_quelle","blocking_code":"mindestpause"}\n'
        '{"ts":"2026-09-24 10:01:00","diagnostics":{"source_at_start":"PV"},'
        '"reason_code":"pv_unterbrechung"}\n',
        encoding="utf-8",
    )
    rows, quality = read_jsonl(path)
    report = build_quality_report([], {"unknown_reason_codes": 0}, rows, quality,
                                  generated_at=datetime(2026, 9, 24, 12, 0))
    assert report["decision_quality"]["rows"] == 2
    assert report["summary"]["missing_decision_reason_codes"] == 0
    assert report["sources"]["decision:batterie"] == 1
    assert report["sources"]["decision:pv"] == 1


def test_source_at_start_hat_vorrang_vor_effective_source_und_diag_code():
    report = build_quality_report(
        [], {"unknown_reason_codes": 0},
        [{
            "ts": "2026-09-24 10:00:00",
            "source_at_start": "Batterie",
            "effective_source": "PV",
            "diagnostics": {"reason_code": "warte_quelle"},
        }],
        {"invalid_rows": 0, "invalid_timestamps": 0},
    )
    assert report["sources"]["decision:batterie"] == 1
    assert report["end_reasons"]["decision:warte_quelle"] == 1
    assert report["summary"]["missing_decision_reason_codes"] == 0


def test_duplicate_und_rueckwaertszeiten_werden_als_degraded_gewertet():
    rows = [
        {"ts": "2026-09-24 10:00:00", "reason_code": "regel_aus"},
        {"ts": "2026-09-24 10:00:00", "reason_code": "regel_aus"},
        {"ts": "2026-09-24 09:59:00", "reason_code": "regel_aus"},
    ]
    quality = {
        "present": True, "rows": 3, "invalid_rows": 0, "invalid_timestamps": 0,
        "duplicate_timestamps": 1, "backwards_timestamps": 1,
    }
    report = build_quality_report([], {"unknown_reason_codes": 0}, rows, quality)
    assert report["status"] == "degraded"
    assert report["decision_quality"]["duplicate_timestamps"] == 1
    assert report["decision_quality"]["backwards_timestamps"] == 1


def test_zeiten_und_monatsarchive_werden_zentral_parsed(tmp_path):
    assert parse_timestamp("2026-09-24T08:00:00Z").strftime("%H:%M") == "10:00"
    assert parse_timestamp("2026-09-24 10:00:00 +0200").strftime("%H:%M") == "10:00"
    archive = tmp_path / "zyklen_2026-08.csv"
    current = tmp_path / "zyklen.csv"
    for file, date in ((archive, "2026-08-01 10:00:00"), (current, "2026-09-01 10:00:00")):
        file.write_text(
            f"start;ende;dauer_min;quelle;start_regel;end_grund;start_unten;max_unten;ueberschreitung_k;start_verd\n"
            f"{date};{date};1.0;pv;AdaptivePV;regel_aus;40;40;0;10\n",
            encoding="utf-8",
        )
    rows, quality = read_cycles(current)
    assert len(rows) == 2
    assert quality["files"] == ["zyklen_2026-08.csv", "zyklen.csv"]
    assert quality["schema"] == "legacy_v1"


def test_jsonl_ignoriert_nicht_objekte_und_zaehlt_ungueltige_zeiten(tmp_path):
    path = tmp_path / "entscheidungs_log.jsonl"
    path.write_text(
        '{"ts":"2026-09-24 10:00:00","reason_code":"regel_aus"}\n'
        '[]\n'
        '{broken\n'
        '{"ts":"ungueltig","reason_code":"regel_aus"}\n',
        encoding="utf-8",
    )
    rows, quality = read_jsonl(path)
    assert len(rows) == 2
    assert quality["invalid_rows"] == 2
    assert quality["invalid_timestamps"] == 1


def test_unbekannter_decision_code_wird_als_degraded_gewertet():
    report = build_quality_report(
        [], {"unknown_reason_codes": 0},
        [{"ts": "2026-09-24 10:00:00", "reason_code": "neuer_code"}],
        {"invalid_rows": 0, "invalid_timestamps": 0},
    )
    assert report["status"] == "degraded"
    assert report["decision_quality"]["unknown_reason_codes"] == 1
    assert report["summary"]["unknown_decision_reason_codes"] == 1


def test_fehlender_decision_code_wird_als_missing_gewertet():
    report = build_quality_report(
        [], {"unknown_reason_codes": 0},
        [{"ts": "2026-09-24 10:00:00"}],
        {"invalid_rows": 0, "invalid_timestamps": 0},
    )
    assert report["status"] == "degraded"
    assert report["summary"]["missing_decision_reason_codes"] == 1


def test_legacy_zyklus_migration_ergaenzt_reason_code(tmp_path):
    import cycle_logging

    path = tmp_path / "zyklen.csv"
    path.write_text(
        "start;ende;dauer_min;quelle;source_at_start;start_regel;end_grund;"
        "start_unten;start_mittig;start_oben;max_unten;max_mittig;max_oben;"
        "ueberschreitung_k;start_verd\n"
        "2026-09-24 10:00:00;2026-09-24 10:10:00;10;pv;PV;AdaptivePV;boiler_max;"
        "40;41;45;42;43;46;0;15\n",
        encoding="utf-8",
    )
    assert cycle_logging.ensure_cycle_csv(str(path), jetzt=datetime(2026, 9, 24)) is True
    with path.open(encoding="utf-8", newline="") as handle:
        row = next(csv.DictReader(handle, delimiter=";"))
    assert row["reason_code"] == "boiler_max"
    assert row["source_at_start"] == "PV"
