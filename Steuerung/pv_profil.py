# -*- coding: utf-8 -*-
"""Stundenscharfes PV-Profil (Punkt E).

Liest die letzten 14 Tage aus der CSV (heizungsdaten.csv) und berechnet
ein durchschnittliches Tagesprofil der Einspeiseleistung pro Stunde.

Das Profil wird auf den maximalen Wert der letzten 7 Tage normalisiert
und kann mit der heutigen Solax-Prognose skaliert werden.
"""
import csv
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from utils import HEIZUNGSDATEN_CSV


_cache: Dict[str, tuple] = {}
CACHE_TTL_SEKUNDEN = 1800  # 30 Minuten


def _lies_csv_letzte_tage(
    csv_path: str = HEIZUNGSDATEN_CSV,
    tage: int = 14,
) -> List[dict]:
    """Liest CSV-Zeilen der letzten tage. Toleriert fehlende Datei + leere Zeilen."""
    if not os.path.exists(csv_path):
        return []
    grenze = datetime.now() - timedelta(days=tage)
    zeilen = []
    try:
        from collections import deque
        # Ca. 1 Zeile pro Minute fuer `tage` Tage (z.B. 14*24*60 = 20.160 Zeilen)
        max_rows = max(1000, tage * 24 * 60 + 1000)
        with open(csv_path, "r", encoding="utf-8") as f:
            header_line = f.readline()
            if not header_line.strip():
                return []
            tail_lines = list(deque(f, maxlen=max_rows))
            reader = csv.DictReader([header_line] + tail_lines)
            for row in reader:
                try:
                    ts = datetime.fromisoformat(row.get("Zeitstempel", ""))
                except (ValueError, TypeError):
                    continue
                if ts < grenze:
                    continue
                zeilen.append(row)
    except Exception as e:
        logging.warning(f"PV-Profil: CSV-Lesen fehlgeschlagen: {e}")
        return []
    return zeilen


def _parse_float(val) -> Optional[float]:
    """Wandelt CSV-Wert sicher in float um."""
    if val is None:
        return None
    try:
        return float(val.replace(",", ".").replace(" ", ""))
    except (ValueError, AttributeError):
        return None


def berechne_profil(
    csv_path: str = HEIZUNGSDATEN_CSV,
    tage: int = 14,
    force_refresh: bool = False,
) -> Dict[int, float]:
    """Berechnet das durchschnittliche PV-Profil (feedinpower) pro Stunde.

    Returns:
        Dict[stunde=0..23, durchschnittliche_einspeisung_in_watt]
        Leeres/Null-Dict wenn keine Daten.
    """
    global _cache
    jetzt = datetime.now()
    if not force_refresh and csv_path in _cache:
        cache_time, cache_profil = _cache[csv_path]
        if (jetzt - cache_time).total_seconds() < CACHE_TTL_SEKUNDEN:
            return cache_profil

    zeilen = _lies_csv_letzte_tage(csv_path, tage=tage)

    # Pro Stunde sammeln
    summen: Dict[int, float] = defaultdict(float)
    counts: Dict[int, int] = defaultdict(int)

    for row in zeilen:
        try:
            ts = datetime.fromisoformat(row.get("Zeitstempel", ""))
        except (ValueError, TypeError):
            continue
        feedin = _parse_float(row.get("FeedinPower"))
        if feedin is None or feedin < 0:
            continue
        h = ts.hour
        summen[h] += feedin
        counts[h] += 1

    # Durchschnitt pro Stunde
    profil: Dict[int, float] = {}
    for h in range(24):
        if counts.get(h, 0) > 0:
            profil[h] = round(summen[h] / counts[h], 1)
        else:
            profil[h] = 0.0

    _cache[csv_path] = (jetzt, profil)
    return profil


def get_erwartete_pv_watt(
    stunde: int,
    profil: Optional[Dict[int, float]] = None,
    tage: int = 14,
    csv_path: str = HEIZUNGSDATEN_CSV,
) -> float:
    """Gibt die erwartete PV-Einspeisung fuer eine bestimmte Stunde zurueck.

    Args:
        stunde: 0-23
        profil: Berechnetes Profil (wenn None, wird es aus CSV geladen).
        tage: Tage zurueck fuer das Profil.

    Returns:
        Erwartete Watt (0 wenn keine Daten).
    """
    if profil is None:
        profil = berechne_profil(csv_path=csv_path, tage=tage)
    return profil.get(stunde, 0.0)


def get_peak_leistung(profil: Dict[int, float]) -> float:
    """Hoechster Wert im Profil (fuer Normalisierung)."""
    return max(profil.values()) if profil else 0.0
