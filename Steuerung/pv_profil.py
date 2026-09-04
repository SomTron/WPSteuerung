# -*- coding: utf-8 -*-
"""Stundenscharfes PV-Profil (Punkt E).

Liest die letzten 14 Tage aus der CSV (heizungsdaten.csv) und berechnet
ein durchschnittliches Tagesprofil der Einspeiseleistung pro Stunde.

Das Profil wird auf den maximalen Wert der letzten 7 Tage normalisiert
und kann mit der heutigen Solax-Prognose skaliert werden.
"""
import asyncio
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
    forecast_scaling: Optional[float] = None,
) -> Dict[int, float]:
    """Berechnet das durchschnittliche PV-Profil (feedinpower) pro Stunde.

    Args:
        csv_path: Pfad zur CSV-Datei
        tage: Anzahl der Tage zurueck fuer die Berechnung
        force_refresh: Fuerz Neuberechnung (ignoriert Cache)
        forecast_scaling: Optionaler Skalierungsfaktor basierend auf Forecast
                         (z.B. 1.2 fuer 20% mehr PV als historisch, 0.8 fuer 20% weniger)

    Returns:
        Dict[stunde=0..23, durchschnittliche_einspeisung_in_watt]
        Leeres/Null-Dict wenn keine Daten.
    """
    global _cache
    jetzt = datetime.now()
    cache_key = f"{csv_path}_{forecast_scaling}"
    if not force_refresh and cache_key in _cache:
        cache_time, cache_profil = _cache[cache_key]
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

    # Forecast-Skalierung anwenden (wenn Forecast verfügbar)
    if forecast_scaling is not None and forecast_scaling > 0:
        for h in profil:
            profil[h] = round(profil[h] * forecast_scaling, 1)

    _cache[cache_key] = (jetzt, profil)
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


async def berechne_profil_async(
    csv_path: str = HEIZUNGSDATEN_CSV,
    tage: int = 14,
    force_refresh: bool = False,
    forecast_scaling: Optional[float] = None,
) -> Dict[int, float]:
    """Async-Wrapper fuer berechne_profil.

    Fuhrt das CSV-Lesen und die Profil-Berechnung im Thread-Pool aus,
    damit der Event-Loop auf langsamen SD-Karten nicht blockiert wird.
    Hat den gleichen 30-Minuten-Cache wie die synchrone Variante.
    """
    return await asyncio.to_thread(berechne_profil, csv_path, tage, force_refresh, forecast_scaling)


def berechne_forecast_scaling(
    forecast_today_wh_qm: Optional[float],
    historisches_wh_qm: Optional[float] = None,
) -> Optional[float]:
    """Berechnet den Skalierungsfaktor basierend auf der heutigen PV-Prognose.

    Args:
        forecast_today_wh_qm: Prognostizierter PV-Ertrag (Wh/m²)
        historisches_wh_qm: Historischer durchschnittlicher PV-Ertrag (Wh/m²)

    Returns:
        Skalierungsfaktor (z.B. 1.2 = 20% mehr als historisch)
        None wenn keine Prognose verfügbar ist
    """
    if forecast_today_wh_qm is None or forecast_today_wh_qm <= 0:
        return None
    
    if historisches_wh_qm is not None and historisches_wh_qm > 0:
        return round(forecast_today_wh_qm / historisches_wh_qm, 3)
    
    # Wenn kein historischer Wert, nutze Standardannahme (z.B. 1800 Wh/m² für guten Tag)
    standard_wh_qm = 1800.0
    return round(forecast_today_wh_qm / standard_wh_qm, 3)
