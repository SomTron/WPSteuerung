# -*- coding: utf-8 -*-
"""Gemeinsame, schema-bewusste Hilfen fuer die WP-Steuerungsanalyse."""
from __future__ import annotations

import csv
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

CURRENT_CYCLE_FIELDS = (
    "start", "ende", "dauer_min", "quelle", "source_at_start", "start_regel", "end_grund",
    "start_unten", "start_mittig", "start_oben", "max_unten", "max_mittig", "max_oben",
    "ueberschreitung_k", "start_verd",
)
LEGACY_CYCLE_FIELDS = (
    "start", "ende", "dauer_min", "quelle", "start_regel", "end_grund", "start_unten",
    "max_unten", "ueberschreitung_k", "start_verd",
)
KNOWN_END_CODES = {
    "boiler_max", "regel_aus", "keine_regel", "mindestlaufzeit", "mindestpause",
    "uebertemperatur", "sensorfehler", "druckfehler", "hardwarefehler",
    "kompressor_verifizierung", "verifizierung_fehler", "legionellen_timeout",
    "dienst_neustart", "api_manuell", "unknown", "unbekannt",
}


def parse_timestamp(value: Any) -> datetime | None:
    """Parst ISO/Log-Zeitstempel und liefert lokale naive Zeit fuer Vergleiche."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=None)
    except ValueError:
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
    """Liest zyklen.csv und kennzeichnet aktuelles/legacy Schema."""
    path = Path(path)
    if not path.exists():
        return [], {"present": False, "schema": "missing", "rows": 0, "invalid_rows": 0}
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        fields = tuple(reader.fieldnames or ())
        rows = list(reader)
    if fields == CURRENT_CYCLE_FIELDS:
        schema = "current"
    elif fields == LEGACY_CYCLE_FIELDS:
        schema = "legacy_v1"
    elif {"start", "ende"}.issubset(fields):
        schema = "legacy_unknown"
    else:
        schema = "invalid"
    quality = {
        "present": True, "schema": schema, "fields": list(fields), "rows": len(rows),
        "invalid_rows": sum(1 for row in rows if parse_timestamp(row.get("start")) is None),
        "missing_source_at_start": sum(1 for row in rows if not row.get("source_at_start")),
        "unknown_end_grund": 0,
    }
    for row in rows:
        reason, reason_quality = classify_end_reason(row)
        row["_end_grund_normalisiert"] = reason
        row["_end_grund_quality"] = reason_quality
        if reason_quality == "missing":
            quality["unknown_end_grund"] += 1
        source, source_quality = classify_source(row)
        row["_source_normalisiert"] = source
        row["_source_quality"] = source_quality
    return rows, quality

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
    })
    return result
