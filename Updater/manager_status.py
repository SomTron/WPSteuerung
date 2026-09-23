#!/usr/bin/env python3
"""Liest robuste Betriebsdaten fuer die Anzeige von ``wp-manager.sh``.

Der Manager soll grosse CSV-Dateien nicht vollstaendig in den Speicher laden und
sich nicht auf Dateiaenderungszeiten verlassen. Statusfelder werden aus Header
und Dateiende gelesen; die Zyklusanzahl wird blockweise gezaehlt. Die Ausgabe ist
absichtlich als einfache ``schluessel=wert``-Zeilen fuer die POSIX-Shell
aufgebaut.
"""
import csv
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

_STATE_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_STATE_COMP_RE = re.compile(r"\bKomp=(EIN|AUS)\b")


def parse_timestamp(value: str) -> Optional[datetime]:
    """Liest den sekundengenauen Zeitstempel des Steuerungscodes."""
    match = _STATE_TIME_RE.match(value.strip())
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def format_age(seconds: Optional[int]) -> str:
    """Formatiert ein Datenalter kompakt und verständlich."""
    if seconds is None:
        return "n/a"
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _tail_text(path: str, max_bytes: int = 65536) -> str:
    """Liest nur das Dateiende und liefert eine reparierte Textschwanzzeile."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except (OSError, ValueError):
        return ""
    text = data.decode("utf-8-sig", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _last_csv_row(path: str, delimiter: str) -> Tuple[List[str], Dict[str, str]]:
    """Liest Header und letzte nichtleere Datenzeile eines CSV."""
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            header = next(csv.reader(handle, delimiter=delimiter), [])
    except (OSError, StopIteration, csv.Error):
        return [], {}
    row = next(csv.reader([_tail_text(path)], delimiter=delimiter), [])
    if not row or (header and len(row) == 1 and not row[0].strip()):
        return header, {}
    return header, dict(zip(header, row))


def _count_data_rows(path: str) -> Optional[int]:
    """Zaehlt CSV-Datenzeilen in Bloecken, ohne die Datei zu laden."""
    try:
        with open(path, "rb") as handle:
            newlines = 0
            last_byte = b""
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                newlines += block.count(b"\n")
                last_byte = block[-1:]
        if not newlines and not last_byte:
            return 0
        return max(0, newlines - 1 + (1 if last_byte != b"\n" else 0))
    except OSError:
        return None


def _state_snapshot(path: str) -> Tuple[Optional[str], Optional[str]]:
    text = _tail_text(path)
    time_match = _STATE_TIME_RE.match(text)
    comp_match = _STATE_COMP_RE.search(text)
    return (
        time_match.group(1) if time_match else None,
        comp_match.group(1) if comp_match else None,
    )


def collect_status(
    heating_csv: str,
    cycle_csv: str,
    state_file: str,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """Sammelt Kompressor-, Datenalter- und Zyklusstatus als Shell-Werte."""
    now = now or datetime.now()
    heating_header, heating_row = _last_csv_row(heating_csv, ",")
    compressor = None
    compressor_source = "unbekannt"
    compressor_timestamp = None

    state_time, state_comp = _state_snapshot(state_file)
    if state_time and state_comp:
        compressor = state_comp
        compressor_source = "last_state.txt"
        compressor_timestamp = state_time

    if compressor is None and heating_header and heating_row:
        try:
            time_index = heating_header.index("Zeitstempel")
            comp_index = heating_header.index("Kompressor")
            raw_comp = heating_row.get(heating_header[comp_index], "").strip()
            if raw_comp in {"0", "1"}:
                compressor = "EIN" if raw_comp == "1" else "AUS"
                compressor_source = "heizungsdaten.csv"
                compressor_timestamp = heating_row.get(heating_header[time_index])
        except (ValueError, IndexError):
            pass

    heating_dt = None
    if heating_row and heating_header:
        try:
            heating_dt = parse_timestamp(heating_row["Zeitstempel"])
        except KeyError:
            pass

    cycle_header, cycle_row = _last_csv_row(cycle_csv, ";")
    last_cycle_end = cycle_row.get("ende", "") if cycle_row else ""
    last_cycle_reason = cycle_row.get("end_grund", "") if cycle_row else ""
    cycle_dt = parse_timestamp(last_cycle_end)
    cycle_count = _count_data_rows(cycle_csv) if os.path.isfile(cycle_csv) else None

    def age(value: Optional[datetime]) -> Optional[int]:
        return int(max(0, (now - value).total_seconds())) if value else None

    return {
        "compressor": compressor or "UNBEKANNT",
        "compressor_source": compressor_source,
        "compressor_age": format_age(age(parse_timestamp(compressor_timestamp or ""))),
        "heating_age": format_age(age(heating_dt)),
        "cycle_count": str(cycle_count) if cycle_count is not None else "n/a",
        "cycle_last_end": last_cycle_end or "n/a",
        "cycle_age": format_age(age(cycle_dt)),
        "cycle_last_reason": last_cycle_reason or "n/a",
        "cycle_columns": str(len(cycle_header)) if cycle_header else "n/a",
    }


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(
            "usage: manager_status.py HEIZUNGSDATEN_CSV ZYKLEN_CSV LAUFSTATUS",
            file=sys.stderr,
        )
        return 2
    for key, value in collect_status(*args).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
