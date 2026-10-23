# -*- coding: utf-8 -*-
"""Stundenscharfes PV-Profil (Punkt E).

Liest die letzten 14 Tage aus der CSV (heizungsdaten.csv) und berechnet
ein durchschnittliches Tagesprofil der PV-ERZEUGUNG pro Stunde.

Wichtig: Grundlage ist ``ACPower`` (Erzeugung), NICHT ``FeedinPower``
(Einspeisung ins Netz). Bei wechselrichtergekoppeltem Betrieb geht der
Ueberschuss zuerst in die Batterie; die Einspeisung ist deshalb ueber weite
Tagesabschnitte null und als Profilgrundlage systematisch zu niedrig
(gemessen: 22-75 % unter der tatsaechlichen Erzeugung).

Das Profil wird auf den maximalen Wert der letzten 7 Tage normalisiert
und kann mit der heutigen Solax-Prognose skaliert werden.
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import Dict, Optional

from csv_tail import streame_tail
from utils import HEIZUNGSDATEN_CSV, to_naive


_cache: Dict[str, tuple] = {}
CACHE_TTL_SEKUNDEN = 1800  # 30 Minuten
#: Der Cache-Schluessel enthaelt den Forecast-Skalierungsfaktor, der sich mit
#: jeder Prognose aendert. Ohne Deckel waechst das Dict ueber Wochen mit.
CACHE_MAX_EINTRAEGE = 4

#: Spalten in der Reihenfolge, in der ``_stundenwerte`` sie liest.
_PROFIL_SPALTEN = (
    "Zeitstempel", "PowerDC1", "PowerDC2", "ACPower", "FeedinPower",
)


def _cache_key(csv_path: str, forecast_scaling: Optional[float]) -> str:
    """Schluessel des Profil-Caches (Pfad + Forecast-Faktor)."""
    return f"{csv_path}_{forecast_scaling}"


def hole_gecachtes_profil(
    forecast_scaling: Optional[float] = None,
    csv_path: str = HEIZUNGSDATEN_CSV,
    max_alter_sek: Optional[float] = None,
) -> Optional[Dict[int, float]]:
    """Liefert das gecachte Profil OHNE zu rechnen (None, wenn kalt).

    BUGFIX (23.10.2026): `/status` darf nie auf die CSV-Auswertung warten -
    sonst haengt die GESAMTE Anzeige (Temperaturen, Kompressor) mit. Im
    Betriebslog gemessen: "/status brauchte 8.4 s", die WebApp bricht nach
    8 s ab und zeigt dann gar nichts. Ist der Cache kalt, antwortet die
    Route ohne Profil; die Neuberechnung laeuft im Hintergrund und ist
    beim naechsten Poll (5 s spaeter) da.
    """
    eintrag = _cache.get(_cache_key(csv_path, forecast_scaling))
    if eintrag is None:
        return None
    zeit, profil = eintrag
    grenze = CACHE_TTL_SEKUNDEN if max_alter_sek is None else max_alter_sek
    if (datetime.now() - zeit).total_seconds() > grenze:
        return None
    return profil


def _parse_zeitstempel(text: Optional[str]) -> Optional[datetime]:
    """CSV-Zeitstempel als naive lokale Zeit (None bei unbrauchbarem Wert)."""
    if not text:
        return None
    try:
        return to_naive(datetime.fromisoformat(text))
    except (ValueError, TypeError):
        return None


def _stundenwerte(
    csv_path: str,
    grenze: datetime,
    tage: int,
) -> tuple:
    """Summiert die PV-Erzeugung stundenweise, ohne Zeilen zu halten.

    BUGFIX (23.10.2026): Vorher wurde der CSV-Tail als Liste von Dicts mit
    ALLEN Spalten gehalten (bis 21.160 Zeilen, gemessen 30 MB Spitze) - und
    zwar bei jedem Cache-Miss von ``/status``, also potenziell alle 30
    Minuten. Auf dem Pi (416 MB) hat das am 23.10.2026 mit zum OOM-Kill
    beigetragen. Jetzt laufen nur Summe und Zaehler je Stunde mit.

    Rueckgabe: (summen, counts) - je 24 Eintraege, Index = Stunde.
    """
    summen = [0.0] * 24
    counts = [0] * 24

    def _verarbeite(row):
        ts = _parse_zeitstempel(row[0])
        if ts is None or ts < grenze:
            return
        # Die Erzeugung steht auf der Gleichstromseite: powerdc1 + powerdc2.
        erzeugung = _parse_float(row[1])
        dc2 = _parse_float(row[2])
        if dc2 is not None:
            erzeugung = (erzeugung or 0.0) + dc2
        if erzeugung is None:
            # Aeltere CSV-Dateien ohne DC-Spalten: ACPower als Notnagel.
            erzeugung = _parse_float(row[3])
        if erzeugung is None:
            erzeugung = _parse_float(row[4])
        if erzeugung is None or erzeugung < 0:
            return
        stunde = ts.hour
        summen[stunde] += erzeugung
        counts[stunde] += 1

    # Ca. 1 Zeile pro Minute fuer `tage` Tage (z.B. 14*24*60 = 20.160 Zeilen)
    max_rows = max(1000, tage * 24 * 60 + 1000)
    try:
        streame_tail(csv_path, max_rows, _PROFIL_SPALTEN, _verarbeite)
    except OSError as exc:
        # Wie bisher: ein Lesefehler ergibt ein leeres Profil, keine Ausnahme
        # bis in die API (die Anzeige darf am Profil nicht scheitern).
        logging.warning(f"PV-Profil: CSV-Lesen fehlgeschlagen: {exc}")
    return summen, counts


def _parse_float(val) -> Optional[float]:
    """Wandelt CSV-Wert sicher in float um."""
    if val is None:
        return None
    try:
        wert = float(val.replace(",", ".").replace(" ", ""))
        return wert if math.isfinite(wert) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def berechne_profil(
    csv_path: str = HEIZUNGSDATEN_CSV,
    tage: int = 14,
    force_refresh: bool = False,
    forecast_scaling: Optional[float] = None,
) -> Dict[int, float]:
    """Berechnet das durchschnittliche PV-Profil (ACPower) pro Stunde.

    Args:
        csv_path: Pfad zur CSV-Datei
        tage: Anzahl der Tage zurueck fuer die Berechnung
        force_refresh: Fuerz Neuberechnung (ignoriert Cache)
        forecast_scaling: Optionaler Skalierungsfaktor basierend auf Forecast
                         (z.B. 1.2 fuer 20% mehr PV als historisch, 0.8 fuer 20% weniger)

    Returns:
        Dict[stunde=0..23, durchschnittliche_pv_erzeugung_in_watt]
        Leeres/Null-Dict wenn keine Daten.
    """
    # BUGFIX (22.10.2026): "global _cache" entfernt. Der Cache wird nur
    # per Subscript-Zugriff veraendert (_cache[key] = ...); das bindet
    # den Namen NICHT lokal - ohne "global" wuerde Python die Variable
    # als lokal behandeln und beim Lesen (cache_key in _cache) einen
    # UnboundLocalError werfen. Der Eintrag war also noetig UND
    # unsinnig zugleich: korrekt ist, ihn wegzulassen, weil das
    # Modul-Level genuegt.
    jetzt = datetime.now()
    cache_key = _cache_key(csv_path, forecast_scaling)
    if not force_refresh and cache_key in _cache:
        cache_time, cache_profil = _cache[cache_key]
        if (jetzt - cache_time).total_seconds() < CACHE_TTL_SEKUNDEN:
            return cache_profil

    # BUGFIX (23.10.2026): Die Berechnung der Erzeugung ist fachlich
    # unveraendert, laeuft aber jetzt WAEHREND des Lesens (siehe
    # _stundenwerte). Vorher wurde der Tail als Dict-Liste gehalten:
    # gemessen 30 MB Spitze - bei jedem Cache-Miss von /status.
    #
    # Grundlage bleibt die Gleichstromseite (powerdc1 + powerdc2), NICHT
    # ACPower: ACPower ist der Wechselstromausgang (Haus + Netz) und
    # meldet nachts 224-250 W, obwohl die Sonne nichts liefert. Historisch
    # fuehrte das zu rund 250-950 W phantomhafter Nachtproduktion und
    # mittags zu deutlich zu niedrigen Werten (ein grosser Teil lief in
    # die Batterie). FeedinPower taugt ebenfalls nicht: bei
    # wechselrichtergekoppeltem Betrieb ist die Einspeisung ueber weite
    # Tagesabschnitte null.
    grenze = to_naive(jetzt - timedelta(days=max(1, int(tage))))
    summen, counts = _stundenwerte(csv_path, grenze, tage)

    # Durchschnitt pro Stunde
    profil: Dict[int, float] = {}
    for h in range(24):
        if counts[h] > 0:
            profil[h] = round(summen[h] / counts[h], 1)
        else:
            profil[h] = 0.0

    # Forecast-Skalierung anwenden (wenn Forecast verfügbar)
    if forecast_scaling is not None and forecast_scaling > 0:
        for h in profil:
            profil[h] = round(profil[h] * forecast_scaling, 1)

    _cache[cache_key] = (jetzt, profil)
    # Deckel gegen unbegrenztes Wachstum (aeltester Eintrag faellt raus).
    while len(_cache) > CACHE_MAX_EINTRAEGE:
        _cache.pop(next(iter(_cache)))
    return profil


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
