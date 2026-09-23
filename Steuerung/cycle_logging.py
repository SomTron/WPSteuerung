"""Persistente Historie abgeschlossener Kompressorzyklen.

Die Temperaturhistorie bleibt in ``heizungsdaten.csv`` (ein Sample pro
Main-Loop). Diese ergaenzende Datei schreibt genau einen Datensatz je
abgeschlossenem Kompressorlauf. Das Schema entspricht der ``zyklen.csv``
aus ``Analyse/log_analyse.py``, sodass bestehende Auswertungen und der
WP-Manager dieselben Spalten verwenden koennen.
"""
import csv
import logging
import math
import os
from datetime import datetime

from utils import rotiere_csv_monatlich


CYCLE_CSV = os.path.join("csv log", "zyklen.csv")
CYCLE_CSV_HEADER = [
    "start", "ende", "dauer_min", "quelle", "start_regel", "end_grund",
    "start_unten", "start_mittig", "start_oben",
    "max_unten", "max_mittig", "max_oben",
    "ueberschreitung_k", "start_verd",
]
OVERSHOOT_LIMIT_C = 48.5


def _zahl(value):
    """Nur endliche numerische Sensor-/Messwerte als CSV-Zahl akzeptieren."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        wert = float(value)
        return wert if math.isfinite(wert) else None
    return None


def _quelle(start_regel):
    """Kompatibel zur bisherigen Log-Auswertung Stromquelle ableiten."""
    if start_regel in ("AdaptivePV", "Einspeisung", "PV_mitte", "PV_unten"):
        return "pv"
    if start_regel == "Batterie":
        return "batterie"
    return "netz"


def _zeitpunkt(value):
    """Zeitstempel im bestehenden sekundengenauen CSV-Format schreiben."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return value or ""


def ensure_cycle_csv(csv_path=CYCLE_CSV, jetzt=None):
    """Sichert Header und rotiert einen eventuell alten Monat."""
    if jetzt is None:
        jetzt = datetime.now()
    try:
        os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
        rotiere_csv_monatlich(csv_path, heute=jetzt, delimiter=";")
        if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                csv.writer(f, delimiter=";").writerow(CYCLE_CSV_HEADER)
        return True
    except Exception as exc:
        logging.error(f"Zyklus-CSV kann nicht vorbereitet werden ({csv_path}): {exc}")
        return False


def begin_cycle(state, now, cycle_id):
    """Start-Snapshot fuer einen realen Kompressorlauf in memory ablegen."""
    sensors = getattr(state, "sensors", None)
    start_regel = getattr(getattr(state, "control", None), "active_rule_name", None)
    state._cycle_log = {
        "start_ts": now,
        "cycle_id": cycle_id,
        "start_regel": start_regel,
        "start_unten": _zahl(getattr(sensors, "t_unten", None)),
        "start_mittig": _zahl(getattr(sensors, "t_mittig", None)),
        "start_oben": _zahl(getattr(sensors, "t_oben", None)),
        "start_verd": _zahl(getattr(sensors, "t_verd", None)),
        "max_unten": _zahl(getattr(sensors, "t_unten", None)),
        "max_mittig": _zahl(getattr(sensors, "t_mittig", None)),
        "max_oben": _zahl(getattr(sensors, "t_oben", None)),
    }


def update_cycle_maxima(state):
    """Aktuelle Sensoren als Maxima des laufenden Zyklus nachfuehren."""
    cycle = getattr(state, "_cycle_log", None)
    if not cycle or not getattr(getattr(state, "control", None), "kompressor_ein", False):
        return False
    sensors = getattr(state, "sensors", None)
    for sensor, key in (("t_unten", "max_unten"), ("t_mittig", "max_mittig"), ("t_oben", "max_oben")):
        wert = _zahl(getattr(sensors, sensor, None))
        if wert is not None and (cycle.get(key) is None or wert > cycle[key]):
            cycle[key] = wert
    return True


def finish_cycle(state, now, end_grund=None, csv_path=None):
    """Laufzeit abschliessen und genau eine Zeile dauerhaft anhaengen."""
    cycle = getattr(state, "_cycle_log", None)
    if not cycle:
        return False
    state._cycle_log = None

    sensors = getattr(state, "sensors", None)
    for sensor, key in (("t_unten", "max_unten"), ("t_mittig", "max_mittig"), ("t_oben", "max_oben")):
        wert = _zahl(getattr(sensors, sensor, None))
        if wert is not None and (cycle.get(key) is None or wert > cycle[key]):
            cycle[key] = wert

    start_ts = cycle.get("start_ts")
    dauer_min = None
    if isinstance(start_ts, datetime):
        dauer_min = round(max((now - start_ts).total_seconds(), 0.0) / 60.0, 1)
    max_unten = cycle.get("max_unten")
    ueberschreitung = None
    if max_unten is not None:
        ueberschreitung = round(max(max_unten - OVERSHOOT_LIMIT_C, 0.0), 1)

    row = {
        "start": _zeitpunkt(start_ts),
        "ende": _zeitpunkt(now),
        "dauer_min": dauer_min,
        "quelle": _quelle(cycle.get("start_regel")),
        "start_regel": cycle.get("start_regel") or "",
        "end_grund": (end_grund or getattr(getattr(state, "control", None), "blocking_reason", None) or "unbekannt"),
        "start_unten": cycle.get("start_unten"),
        "start_mittig": cycle.get("start_mittig"),
        "start_oben": cycle.get("start_oben"),
        "max_unten": max_unten,
        "max_mittig": cycle.get("max_mittig"),
        "max_oben": cycle.get("max_oben"),
        "ueberschreitung_k": ueberschreitung,
        "start_verd": cycle.get("start_verd"),
    }

    if csv_path is None:
        csv_path = CYCLE_CSV
    try:
        if not ensure_cycle_csv(csv_path, jetzt=now):
            return False
        with open(csv_path, "a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=CYCLE_CSV_HEADER, delimiter=";").writerow(row)
        logging.info(
            f"Zyklus gespeichert (cycle={cycle.get('cycle_id', '?')}, "
            f"Dauer={dauer_min} min, Ende={row['end_grund']})."
        )
        return True
    except Exception as exc:
        # Persistenz darf niemals den sicheren Hardware-Schaltvorgang brechen.
        logging.error(f"Zyklus konnte nicht gespeichert werden ({csv_path}): {exc}")
        return False
