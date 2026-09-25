# -*- coding: utf-8 -*-
"""Gemeinsame, schema-bewusste Hilfen fuer die WP-Steuerungsanalyse."""
from __future__ import annotations

import csv
import json
import math
import os
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Berlin")
_EXCEL_EPOCH = datetime(1899, 12, 30)
CURRENT_CYCLE_FIELDS = (
    "start", "ende", "dauer_min", "quelle", "source_at_start", "start_regel", "end_grund",
    "reason_code", "start_unten", "start_mittig", "start_oben", "max_unten", "max_mittig", "max_oben",
    "ueberschreitung_k", "start_verd",
)
LEGACY_CYCLE_FIELDS = (
    "start", "ende", "dauer_min", "quelle", "start_regel", "end_grund", "start_unten",
    "max_unten", "ueberschreitung_k", "start_verd",
)
PRE_REASON_CYCLE_FIELDS = tuple(
    field for field in CURRENT_CYCLE_FIELDS if field != "reason_code"
)
KNOWN_END_CODES = {
    "boiler_max", "regel_aus", "keine_regel", "mindestlaufzeit", "mindestpause",
    "uebertemperatur", "sensorfehler", "druckfehler", "hardwarefehler",
    "kompressor_verifizierung", "verifizierung_fehler", "legionellen_timeout",
    "dienst_neustart", "api_manuell", "boiler_bereits_warm",
    "regelungsfehler", "nachtsperre", "warte_quelle", "verdampfer",
    "daten_stale", "sonstige_sperre",
    "unknown", "unbekannt",
}


def get_git_revision() -> str:
    """Ermittelt die Code-Revision robust für den Qualitätsbericht.

    Eine explizite Umgebungsvariable hat Vorrang. Ohne sie wird die Revision
    ohne Shell über ``git rev-parse`` aus dem Repository ermittelt. Fehlende
    Git-Installation, kein Repository oder ein Timeout liefern ``unknown``.
    """
    configured = str(os.environ.get("WPS_GIT_REVISION", "")).strip()
    if configured:
        return configured[:40]
    try:
        repository = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repository),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        revision = str(result.stdout or "").strip().splitlines()[0].strip()
        if result.returncode == 0 and revision:
            return revision[:40]
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return "unknown"


def parse_timestamp(value: Any) -> datetime | None:
    """Parst ISO/Log/Excel-Zeitstempel und normalisiert auf lokale naive Zeit.

    UTC/Offset-Zeiten werden nach Europe/Berlin umgerechnet; naive Werte
    werden als bereits lokale Betriebszeit interpretiert. Excel-Serienwerte
    werden nur bei plausiblen Tageswerten erkannt.
    """
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 20000 <= number <= 80000:
            return (_EXCEL_EPOCH + timedelta(days=number)).replace(tzinfo=None)
        return None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z")
        return parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(LOCAL_TZ).replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        number = float(text.replace(",", "."))
        if 20000 <= number <= 80000:
            return (_EXCEL_EPOCH + timedelta(days=number)).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def finite_number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def classify_source(row: dict[str, Any]) -> tuple[str, str]:
    """(source, quality): direkte Startquelle vor Feed-in-Heuristik."""
    diagnostics = row.get("diagnostics")
    candidates = [
        row.get("source_at_start"),
        diagnostics.get("source_at_start") if isinstance(diagnostics, dict) else None,
        row.get("source_current"),
        diagnostics.get("source_current") if isinstance(diagnostics, dict) else None,
        row.get("effective_source"),
        diagnostics.get("effective_source") if isinstance(diagnostics, dict) else None,
    ]
    for value in candidates:
        value = str(value or "").strip().lower()
        if value in {"pv", "solar", "batterie", "battery", "netz", "grid", "unknown", "unbekannt"}:
            return ("unbekannt" if value in {"unknown", "unbekannt"} else value, "direct")
    for key in ("quelle", "PowerSource", "power_source"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            aliases = {"solar": "pv", "battery": "batterie", "grid": "netz"}
            return aliases.get(value, value), "recorded"
    start_rule = str(row.get("start_regel") or "").strip().lower()
    if start_rule:
        if start_rule in {"adaptivepv", "einspeisung", "pv_mitte", "pv_unten"}:
            return "pv", "inferred_rule"
        if start_rule == "batterie":
            return "batterie", "inferred_rule"
        if start_rule in {"abweichung", "calcstart", "komfort", "notfallschutz", "legionellen", "wochenende", "mindesttemp"}:
            return "netz", "inferred_rule"
    feedin = finite_number(row.get("feedin_w") or row.get("feedin_watt"))
    if feedin is not None:
        return ("pv" if feedin >= -50 else "netz"), "inferred_feedin"
    return "unbekannt", "missing"


def classify_end_reason(row: dict[str, Any]) -> tuple[str, str]:
    """(end_grund, quality); legacy-text wird nur als Fallback klassifiziert."""
    raw = str(row.get("reason_code") or row.get("end_grund_code") or "").strip().lower()
    if raw:
        return raw, ("direct" if raw in KNOWN_END_CODES else "direct_unmapped")
    text = str(row.get("end_grund") or "").strip().lower()
    if not text or text in {"unbekannt", "unknown", "?"}:
        return "unbekannt", "missing"
    if text in KNOWN_END_CODES:
        return text, "direct"
    if text.startswith("regel:"):
        return "regel_aus", "legacy_text"
    return text, "legacy_text"

def read_cycles(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Liest eine oder mehrere Zyklus-CSVs und kennzeichnet ihr Schema."""
    requested = Path(path)
    if not requested.exists():
        return [], {"present": False, "schema": "missing", "rows": 0, "invalid_rows": 0}
    files = [requested]
    if requested.is_dir():
        files = [p for p in iter_chronological_files(requested) if p.stem.startswith("zyklen")]
    elif requested.suffix.lower() == ".csv":
        files = iter_chronological_files(requested)
    all_rows = []
    schemas = []
    all_fields = []
    for file_path in files:
        try:
            with file_path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                fields = tuple(reader.fieldnames or ())
                rows = list(reader)
        except OSError:
            continue
        if fields == CURRENT_CYCLE_FIELDS:
            schema = "current_v2"
        elif fields == PRE_REASON_CYCLE_FIELDS:
            schema = "current_v1_legacy_reason"
        elif fields == LEGACY_CYCLE_FIELDS:
            schema = "legacy_v1"
        elif {"start", "ende"}.issubset(fields):
            schema = "legacy_unknown"
        else:
            schema = "invalid"
        schemas.append(schema)
        all_fields = list(fields)
        for row in rows:
            row["_source_file"] = file_path.name
        all_rows.extend(rows)
    schema = schemas[0] if len(set(schemas)) == 1 else ("mixed" if schemas else "invalid")
    quality = {
        "present": bool(all_rows or files), "schema": schema,
        "fields": all_fields, "rows": len(all_rows), "files": [p.name for p in files],
        "invalid_rows": sum(1 for row in all_rows if parse_timestamp(row.get("start")) is None),
        "invalid_timestamps": sum(
            1 for row in all_rows
            if parse_timestamp(row.get("start")) is None or parse_timestamp(row.get("ende")) is None
        ),
        "missing_source_at_start": sum(1 for row in all_rows if not row.get("source_at_start")),
        "unknown_end_grund": 0,
        "unknown_reason_codes": 0,
    }
    _add_timestamp_quality(quality, all_rows, ("start",))
    for row in all_rows:
        reason, reason_quality = classify_end_reason(row)
        row["_end_grund_normalisiert"] = reason
        row["_end_grund_quality"] = reason_quality
        if reason_quality == "missing":
            quality["unknown_end_grund"] += 1
        if reason_quality == "direct_unmapped":
            quality["unknown_reason_codes"] += 1
        source, source_quality = classify_source(row)
        row["_source_normalisiert"] = source
        row["_source_quality"] = source_quality
    return all_rows, quality

def _add_timestamp_quality(quality: dict[str, Any], rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    """Ergänzt Zeitqualität für Reihenfolge, Duplikate und Rückwärtszeiten."""
    parsed_rows = []
    for row in rows:
        values = [parse_timestamp(row.get(field)) for field in fields]
        if all(value is not None for value in values):
            parsed_rows.append(values[0])
    backwards = duplicates = 0
    previous = None
    for current in parsed_rows:
        if previous is not None:
            if current < previous:
                backwards += 1
            elif current == previous:
                duplicates += 1
        previous = current
    quality.update({
        "valid_timestamps": len(parsed_rows),
        "duplicate_timestamps": duplicates,
        "backwards_timestamps": backwards,
    })


def heating_intervals(times: list[datetime], short_gap_s: float = 120, gap_s: float = 900) -> dict[str, Any]:
    """Klassifiziert Zeitabstaende, ohne grosse Lücken als Messintervall zu zählen."""
    deltas = []
    for previous, current in zip(times, times[1:]):
        seconds = (current - previous).total_seconds()
        if seconds >= 0:
            deltas.append(seconds)
    if not deltas:
        return {"count": 0, "median_s": 0, "gaps_120_900": 0, "gaps_gt_900": 0, "max_s": 0}
    ordered = sorted(deltas)
    return {
        "count": len(deltas), "median_s": ordered[len(ordered) // 2],
        "gaps_120_900": sum(short_gap_s < x < gap_s for x in deltas),
        "gaps_gt_900": sum(x >= gap_s for x in deltas), "max_s": max(deltas),
    }


def energy_wh(samples: list[tuple[datetime, Any]], max_interval_s: float = 120.0) -> float:
    """Zeitbasiert integriert; Lücken werden nicht als Leistung gewertet."""
    total = 0.0
    for (previous, value), (current, _next_value) in zip(samples, samples[1:]):
        seconds = (current - previous).total_seconds()
        if 0 < seconds <= max_interval_s:
            watts = finite_number(value)
            if watts is not None:
                total += watts * seconds / 3600.0
    return total


def build_cycle_quality(cycles: list[dict[str, Any]], quality: dict[str, Any]) -> dict[str, Any]:
    durations = [finite_number(row.get("dauer_min")) for row in cycles]
    durations = [value for value in durations if value is not None]
    result = dict(quality)
    result.update({
        "duration_count": len(durations),
        "duration_under_5_min": sum(value < 5 for value in durations),
        "duration_under_10_min": sum(value < 10 for value in durations),
        "duration_under_20_min": sum(value < 20 for value in durations),
        "end_grund_quality": dict(Counter(
            row.get("_end_grund_quality", row.get("end_grund_quality", "unknown"))
            for row in cycles
        )),
        "source_quality": dict(Counter(
            row.get("_source_quality", row.get("source_quality", "missing"))
            for row in cycles
        )),
        "unknown_reason_codes": sum(
            row.get("_end_grund_quality") == "direct_unmapped" for row in cycles
        ),
    })
    return result


def iter_chronological_files(path: str | Path) -> list[Path]:
    """Liefert eine CSV und relevante Monatsarchive in zeitlicher Reihenfolge."""
    basis = Path(path)
    files = []
    if basis.is_dir():
        candidates = list(basis.glob("*.csv"))
    else:
        candidates = [basis]
        if basis.parent.exists():
            candidates.extend(basis.parent.glob(f"{basis.stem}_*.csv"))
    for candidate in sorted(set(candidates)):
        if not candidate.is_file():
            continue
        if candidate == basis:
            files.append(candidate)
            continue
        try:
            datetime.strptime(candidate.stem.rsplit("_", 1)[-1], "%Y-%m")
        except ValueError:
            continue
        files.append(candidate)
    return sorted(
        set(files),
        key=lambda item: (
            1 if item == basis else 0,
            item.stem.rsplit("_", 1)[-1] if item != basis else "",
        ),
    )


def read_jsonl(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Liest JSONL robust und zählt syntaktisch sowie zeitlich ungültige Einträge."""
    path = Path(path)
    if not path.exists():
        return [], {"present": False, "rows": 0, "invalid_rows": 0}
    rows = []
    invalid = 0
    invalid_times = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                invalid += 1
                continue
            if not isinstance(row, dict):
                invalid += 1
                continue
            if parse_timestamp(row.get("ts")) is None:
                invalid_times += 1
            rows.append(row)
    quality = {
        "present": True, "rows": len(rows), "invalid_rows": invalid,
        "invalid_timestamps": invalid_times,
    }
    _add_timestamp_quality(quality, rows, ("ts",))
    return rows, quality


def build_quality_report(
    cycle_rows: list[dict[str, Any]],
    cycle_quality: dict[str, Any],
    decision_rows: Iterable[dict[str, Any]],
    decision_quality: dict[str, Any],
    *,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Baut einen stabilen, JSON-serialisierbaren Qualitätsvertrag."""
    scenarios = Counter()
    end_codes = Counter()
    sources = Counter()
    for cycle in cycle_rows:
        source = str(cycle.get("_source_normalisiert") or cycle.get("source_at_start") or "unbekannt")
        sources[source] += 1
        reason = str(cycle.get("_end_grund_normalisiert") or "unbekannt")
        end_codes[reason] += 1
        rule = str(cycle.get("start_regel") or "unbekannt").strip()
        if source == "pv":
            scenarios["pv"] += 1
        elif source == "batterie":
            scenarios["batterie"] += 1
        elif source == "netz":
            scenarios["netz"] += 1
        if "legionellen" in rule.lower():
            scenarios["legionellen"] += 1
        if reason in {"boiler_max", "uebertemperatur"}:
            scenarios["boiler_schutz"] += 1
        if reason in {"sensorfehler", "druckfehler", "hardwarefehler", "verifizierung_fehler"}:
            scenarios["sensor_druck_hardware"] += 1
        if reason == "dienst_neustart":
            scenarios["dienst_neustart"] += 1
    decision_list = list(decision_rows)
    decision_source_quality = Counter()
    decision_reason_quality = Counter()
    decision_unknown_codes = 0
    decision_missing_codes = 0
    for decision in decision_list:
        diagnostics = decision.get("diagnostics") if isinstance(decision.get("diagnostics"), dict) else {}
        reason = str(
            decision.get("reason_code")
            or diagnostics.get("reason_code")
            or decision.get("blocking_code")
            or diagnostics.get("blocking_code")
            or "unbekannt"
        ).strip().lower()
        if reason in {"unbekannt", "unknown"}:
            decision_missing_codes += 1
        if reason in KNOWN_END_CODES or reason in {
            "warte_quelle", "waiting_source", "pv_unterbrechung", "data_stale",
        }:
            decision_reason_quality["direct"] += 1
        elif reason == "unbekannt":
            decision_reason_quality["missing"] += 1
        else:
            decision_reason_quality["direct_unmapped"] += 1
            decision_unknown_codes += 1
        source, source_quality = classify_source(decision)
        end_codes[f"decision:{reason}"] += 1
        sources[f"decision:{source}"] += 1
        decision_source_quality[source_quality] += 1
    decision_quality = dict(decision_quality)
    decision_quality["missing_reason_codes"] = decision_missing_codes
    decision_quality["unknown_reason_codes"] = decision_unknown_codes
    decision_quality["reason_quality"] = dict(decision_reason_quality)
    decision_quality["source_quality"] = dict(decision_source_quality)
    now = generated_at or datetime.now(LOCAL_TZ)
    unknown_codes = (
        cycle_quality.get("unknown_reason_codes", 0)
        + decision_quality.get("unknown_reason_codes", 0)
    )
    time_issues = sum(
        quality.get(key, 0)
        for quality in (cycle_quality, decision_quality)
        for key in ("duplicate_timestamps", "backwards_timestamps")
    )
    invalid_total = (
        cycle_quality.get("invalid_rows", 0)
        + cycle_quality.get("invalid_timestamps", 0)
        + decision_quality.get("invalid_rows", 0)
        + decision_quality.get("invalid_timestamps", 0)
    )
    status = "degraded" if (
        invalid_total or unknown_codes or time_issues
        or cycle_quality.get("missing_source_at_start", 0)
        or decision_missing_codes
    ) else "ok"
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "git_revision": get_git_revision(),
        "status": status,
        "sources": dict(sources),
        "end_reasons": dict(end_codes),
        "scenarios": dict(scenarios),
        "cycle_quality": dict(cycle_quality),
        "decision_quality": dict(decision_quality),
        "summary": {
            "cycles": len(cycle_rows),
            "decisions": len(decision_list),
            "invalid_rows": invalid_total,
            "unknown_reason_codes": unknown_codes,
            "unknown_decision_reason_codes": decision_unknown_codes,
            "missing_source_at_start": cycle_quality.get("missing_source_at_start", 0),
            "missing_decision_reason_codes": decision_missing_codes,
        },
    }
