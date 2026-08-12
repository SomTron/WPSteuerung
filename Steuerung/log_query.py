"""
Log-Abfrage: Durchsucht das heizungssteuerung.log nach Datum/Zeit.

Format der Logs (aus logging_config.py):
  %(asctime)s %(levelname)s - %(message)s
  z.B. "2026-08-08 16:24:33 +0200 INFO - Regel-Bewertung (9 Regeln):"
"""

import os
import re
from datetime import datetime
from typing import Optional, List, Tuple

# Regex zum Erkennen des Zeitstempels am Zeilenanfang
# Format: "2026-08-08 16:24:33 +0200"
TS_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")

# Log-Level-Pattern
LEVEL_PATTERN = re.compile(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


def _parse_ts(line: str) -> Optional[datetime]:
    """Extrahiert den Zeitstempel aus einer Log-Zeile (ohne timezone)."""
    m = TS_PATTERN.match(line)
    if m:
        try:
            # Nur Datum+Zeit parsen, Zeitzone ignorieren
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return None


def _find_default_log_path() -> str:
    """Ermittelt den Pfad zur Log-Datei (relativ zu diesem Modul)."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "heizungssteuerung.log")


def query_logs(
    after: Optional[datetime] = None,
    lines: int = 50,
    level: Optional[str] = None,
    log_path: Optional[str] = None,
    before: Optional[datetime] = None,
) -> Tuple[List[str], dict]:
    """
    Durchsucht das Log nach einem Zeitbereich und gibt die Zeilen zurueck.

    Args:
        after:     Nur Zeilen NACH diesem Zeitpunkt (ausschliesslich).
                   None = von Anfang an.
        lines:     Maximale Anzahl Zeilen zurueck (default 50, max 500).
        level:     Optional filter: "INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL".
        log_path:  Pfad zur Log-Datei. None = automatisch suchen.
        before:    Nur Zeilen VOR diesem Zeitpunkt (optional).

    Returns:
        (zeilen, meta) mit:
          - zeilen: Liste der Log-Zeilen (mit Zeilenumbruch)
          - meta: Dict mit Statistik (total_found, matched_lines, start_ts, end_ts, ...)
    """
    if log_path is None:
        log_path = _find_default_log_path()

    meta = {
        "log_path": log_path,
        "log_exists": os.path.exists(log_path),
        "total_found": 0,
        "matched_lines": 0,
        "start_ts": None,
        "end_ts": None,
        "filter_level": level,
        "filter_after": after.isoformat() if after else None,
        "filter_before": before.isoformat() if before else None,
        "requested_lines": min(lines, 500),
    }

    if not meta["log_exists"]:
        return [], meta

    # Datei von hinten lesen (tail-Ansatz) fuer Effizienz
    # Ziel: aus einem Zeitbereich die letzten X Zeilen finden
    max_lines = min(lines, 500)

    # Wenn after gesetzt: binäre Suche nach der Startposition
    # Sonst: tail von hinten
    start_pos = None
    if after is not None:
        start_pos = _binary_search_ts(log_path, after)

    # Jetzt die Datei ab start_pos lesen (oder ab 0)
    result = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        if start_pos is not None:
            f.seek(start_pos)
            # Sicherstellen, dass wir nicht mitten in einer Zeile sind
            if start_pos > 0:
                f.readline()  # Rest der angefangenen Zeile ueberspringen

        for line in f:
            line_ts = _parse_ts(line)

            # Vor Filter: wenn before gesetzt, nur Zeilen VOR before
            if before is not None and line_ts is not None and line_ts >= before:
                break

            # Nach Filter: wenn after gesetzt, nur Zeilen NACH after
            if after is not None and line_ts is not None and line_ts <= after:
                continue

            # Level-Filter
            if level is not None:
                level_match = LEVEL_PATTERN.search(line)
                if not level_match or level_match.group(1) != level.upper():
                    continue

            result.append(line)
            if len(result) >= max_lines:
                break

    # Metadaten sammeln
    meta["total_found"] = len(result)

    # Start/End-Timestamp aus den gefundenen Zeilen
    if result:
        first_ts = _parse_ts(result[0])
        last_ts = _parse_ts(result[-1])
        meta["start_ts"] = first_ts.isoformat() if first_ts else None
        meta["end_ts"] = last_ts.isoformat() if last_ts else None

    return result, meta


def _binary_search_ts(log_path: str, target: datetime) -> int:
    """
    Binäre Suche nach der ersten Log-Zeile NACH `target`.
    Gibt die Byte-Position zurueck.

    Vorgehen:
    - Datei in der Mitte oeffnen, Zeile lesen, Timestamp parsen
    - Solange bis die richtige Position gefunden ist
    """
    import os

    file_size = os.path.getsize(log_path)
    with open(log_path, "rb") as f:  # binary mode fuer Praezision
        low, high = 0, file_size
        best_pos = 0

        while low < high:
            mid = (low + high) // 2
            f.seek(mid)

            # Zum Zeilenanfang zurueck
            if mid > 0:
                f.readline()

            pos = f.tell()
            line = f.readline().decode("utf-8", errors="replace")

            if not line:
                high = mid
                continue

            line_ts = _parse_ts(line)
            if line_ts is None:
                # Kein Timestamp -> naechste Zeile versuchen
                low = pos + len(line.encode("utf-8"))
                continue

            if line_ts <= target:
                low = pos + len(line.encode("utf-8"))
                best_pos = low
            else:
                high = mid
                best_pos = pos

        return best_pos


def tail_log(
    lines: int = 50,
    log_path: Optional[str] = None,
) -> Tuple[List[str], dict]:
    """Gibt die letzten X Zeilen des Logs zurueck (effizient)."""
    if log_path is None:
        log_path = _find_default_log_path()

    meta = {
        "log_path": log_path,
        "log_exists": os.path.exists(log_path),
        "total_found": 0,
    }

    if not meta["log_exists"]:
        return [], meta

    result = []
    with open(log_path, "rb") as f:
        try:
            f.seek(0, 2)  # ans Ende
            file_size = f.tell()
            block_size = 4096
            buffer = b""

            while len(result) < lines and file_size > 0:
                read_size = min(block_size, file_size)
                file_size -= read_size
                f.seek(file_size)
                chunk = f.read(read_size)
                buffer = chunk + buffer

                # Zeilen extrahieren (letzte lines Stueck)
                lines_in_buf = buffer.split(b"\n")
                if len(lines_in_buf) > lines:
                    result = lines_in_buf[-lines:]
                    break
                result = lines_in_buf

            # Letzte leere Zeile entfernen
            if result and result[-1] == b"":
                result = result[:-1]

            result = [l.decode("utf-8", errors="replace") + "\n" for l in result]

        except Exception:
            pass

    meta["total_found"] = len(result)
    return result, meta