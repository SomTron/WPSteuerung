"""
Prioritätenbasierte WP-Steuerung (Pareto-optimiert).

Statt modusbasierter Logik bewerten mehrere unabhängige Regeln
parallel die aktuelle Situation. Die Regel mit der höchsten Priorität,
deren Bedingungen erfüllt sind, gewinnt.
"""

import logging
from datetime import datetime, time
from typing import Optional, Dict, Any, Tuple, List
from dataclasses import dataclass

from json_config import (
    WPSteuerungConfig, PVRegel, KomfortConfig,
    ZeitfensterConfig, AbweichungConfig
)


@dataclass
class RegelErgebnis:
    """Ergebnis einer einzelnen Regelbewertung."""
    name: str
    prioritaet: int
    aktiv: bool
    einschalten: Optional[bool] = None  # True = Einschalten, False = Ausschalten, None = Keine Aktion
    grund: str = ""
    regel_dict: Optional[Dict] = None  # Fuer API/Status-Anzeige


def _parse_sensor(temp_dict: Dict[str, Optional[float]], sensor_name: str) -> Optional[float]:
    """Liest einen Sensorwert aus dem Dictionary."""
    return temp_dict.get(sensor_name)


def _is_nachtsperre(now_hour: int, start: int, ende: int) -> bool:
    """
    Prueft ob Nachtsperre aktiv ist.
    Funktionierts auch ueber Mitternacht: start=19, ende=8 -> 19-8 Uhr.
    """
    if start <= ende:
        return start <= now_hour < ende
    else:
        # Ueber Mitternacht: 19-8 -> 19-23 ODER 0-8
        return now_hour >= start or now_hour < ende


def _is_weekend(now: datetime) -> bool:
    """Prueft ob Wochenende (Samstag=5, Sonntag=6)."""
    return now.weekday() >= 5


def _is_zeitfenster_active(now_hour: int, start: int, ende: int) -> bool:
    """Prueft ob die aktuelle Stunde im Zeitfenster liegt."""
    if start <= ende:
        return start <= now_hour < ende
    else:
        return now_hour >= start or now_hour < ende


def evaluate_pv_regel(
    regel: PVRegel,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
) -> RegelErgebnis:
    """
    Bewertet eine PV-Regel.
    
    Logik:
    - Wenn Nachtsperre aktiv -> Regel inaktiv
    - Wenn PV >= Schwelle UND Temp <= Einschalt-Schwelle -> EIN
    - Wenn Kompressor schon laeuft UND PV >= Weiterlauf-Schwelle -> EIN (bleibt)
    - Wenn Temp >= Ausschalt-Schwelle -> AUS
    """
    sensor_name = regel.temperaturfuehler
    temp = _parse_sensor(temp_dict, sensor_name)
    
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    
    if nachtsperre:
        return RegelErgebnis(
            name=regel.name,
            prioritaet=regel.prioritaet,
            aktiv=False,
            grund="Nachtsperre aktiv"
        )
    
    if temp is None:
        return RegelErgebnis(
            name=regel.name,
            prioritaet=regel.prioritaet,
            aktiv=False,
            grund=f"Sensor '{sensor_name}' nicht verfuegbar"
        )
    
    # Grundlage fuer Entscheidung
    result = RegelErgebnis(
        name=regel.name,
        prioritaet=regel.prioritaet,
        aktiv=True
    )
    
    # Ausschalten: Temp zu hoch (ZUERST pruefen!)
    if temp >= regel.ausschalten_bei_c:
        result.einschalten = False
        result.grund = f"{sensor_name} {temp:.1f}C >= {regel.ausschalten_bei_c}C -> AUS"
        return result
    
    # Einschalten: PV hoch genug UND Temp niedrig
    if pv_leistung >= regel.pv_schwelle_watt and temp <= regel.einschalten_bei_c:
        result.einschalten = True
        result.grund = f"PV {pv_leistung:.0f}W >= {regel.pv_schwelle_watt}W, {sensor_name} {temp:.1f}C <= {regel.einschalten_bei_c}C -> EIN"
        return result
    
    # Weiterlaufen: Kompressor laeuft schon und PV reicht fuer Weiterbetrieb
    if kompressor_ein and pv_leistung >= regel.weiterlaufen_ab_pv_watt:
        result.einschalten = True
        result.grund = f"Weiterlauf: PV {pv_leistung:.0f}W >= {regel.weiterlaufen_ab_pv_watt}W (Kompressor laeuft)"
        return result
    
    # Keine Aktion
    result.einschalten = None
    result.grund = f"Keine Bedingung erfuellt (PV={pv_leistung:.0f}W, {sensor_name}={temp:.1f}C)"
    return result


def evaluate_komfort(
    komfort: KomfortConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    now_hour: int,
) -> RegelErgebnis:
    """
    Komfort-Regel: Haelt eine Mindesttemperatur im Boiler.
    
    - Notfall: Immer einschalten wenn Temp <= Notfall-Schwelle (auch Nachts!)
    - Komfort: Einschalten wenn Temp <= Komfort-Schwelle UND genug PV
    - Ausschalten: Temp >= Ausschalt-Schwelle
    """
    temp = _parse_sensor(temp_dict, "unten")
    temp_oben = _parse_sensor(temp_dict, "oben")
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    
    result = RegelErgebnis(
        name="Komfort",
        prioritaet=komfort.prioritaet,
        aktiv=True
    )
    
    if temp is None:
        result.aktiv = False
        result.grund = "Sensor 'unten' nicht verfuegbar"
        return result
    
    # Notfall: Auch bei Nachtsperre! (obener Sensor, da in der Nacht die
    # Schichtung relevant ist und oben die Nutztemperatur repraesentiert)
    if temp_oben is not None and temp_oben <= komfort.notfall_einschalten_bei_c:
        result.einschalten = True
        result.grund = f"NOTFALL: oben {temp_oben:.1f}C <= {komfort.notfall_einschalten_bei_c}C -> EIN"
        return result
    
    # Wenn oben-Sensor nicht verfuegbar: Fallback auf unten
    if temp_oben is None and temp <= komfort.notfall_einschalten_bei_c:
        result.einschalten = True
        result.grund = f"NOTFALL (Fallback unten): unten {temp:.1f}C <= {komfort.notfall_einschalten_bei_c}C -> EIN"
        return result
    
    # Bei Nachtsperre: Nur Notfall, kein Komfort
    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre (kein Komfort-Heizen)"
        return result
    
    # Ausschalten
    if temp >= komfort.ausschalten_bei_c:
        result.einschalten = False
        result.grund = f"Komfort AUS: unten {temp:.1f}C >= {komfort.ausschalten_bei_c}C"
        return result
    
    # Komfort-Einschalten: Genug PV
    if pv_leistung >= komfort.min_pv_fuer_komfort_watt and temp <= komfort.komfort_einschalten_bei_c:
        result.einschalten = True
        result.grund = f"Komfort: unten {temp:.1f}C <= {komfort.komfort_einschalten_bei_c}C, PV {pv_leistung:.0f}W -> EIN"
        return result
    
    result.einschalten = None
    result.grund = f"Komfort aktiv, aber keine Bedingung (unten={temp:.1f}C, PV={pv_leistung:.0f}W)"
    return result


def evaluate_zeitfenster(
    zf: ZeitfensterConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    now_hour: int,
) -> RegelErgebnis:
    """
    Zeitfenster-Regel: Heizt zu festen Uhrzeiten.
    
    Nur aktiv wenn:
    - Aktuelle Stunde im Fenster
    - Temperatur unter Schwellwert
    - PV ausreichend (falls > 0)
    """
    result = RegelErgebnis(
        name="Zeitfenster",
        prioritaet=zf.prioritaet,
        aktiv=False,
        grund="Auserhalb Zeitfenster"
    )
    
    if not _is_zeitfenster_active(now_hour, zf.start_uhr, zf.ende_uhr):
        return result
    
    result.aktiv = True
    
    # PV-Check
    if zf.min_pv_watt > 0 and pv_leistung < zf.min_pv_watt:
        result.grund = f"Zeitfenster aktiv, aber PV {pv_leistung:.0f}W < {zf.min_pv_watt}W"
        result.einschalten = None
        return result
    
    # Temperatur-Check
    temp = _parse_sensor(temp_dict, zf.temperaturfuehler)
    if temp is None:
        result.grund = f"Zeitfenster aktiv, Sensor '{zf.temperaturfuehler}' fehlt"
        result.einschalten = None
        return result
    
    if temp <= zf.max_temp_fuer_einschalten_c:
        result.einschalten = True
        result.grund = f"Zeitfenster {zf.start_uhr}-{zf.ende_uhr} Uhr: {zf.temperaturfuehler} {temp:.1f}C <= {zf.max_temp_fuer_einschalten_c}C -> EIN"
    else:
        result.einschalten = False
        result.grund = f"Zeitfenster: {zf.temperaturfuehler} {temp:.1f}C > {zf.max_temp_fuer_einschalten_c}C (bereits warm) -> AUS"
    
    return result


def evaluate_abweichung(
    abw: AbweichungConfig,
    temp_dict: Dict[str, Optional[float]],
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
) -> RegelErgebnis:
    """
    Abweichungs-Regel: Haelt Temperatur nahe am Sollwert.
    
    Einschalten wenn: Soll - Ist >= Einschalt-Abweichung
    Ausschalten wenn: Soll - Ist <= Ausschalt-Abweichung
    (Negative Abweichung = Ist > Soll)
    """
    result = RegelErgebnis(
        name="Abweichung",
        prioritaet=abw.prioritaet,
        aktiv=True
    )
    
    temp = _parse_sensor(temp_dict, abw.temperaturfuehler)
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{abw.temperaturfuehler}' nicht verfuegbar"
        return result
    
    abweichung = abw.solltemperatur_c - temp  # Positiv = zu kalt, Negativ = zu warm
    
    # Ausschalten: Ziel erreicht oder ueberschritten
    if abweichung <= abw.ausschalten_bei_abweichung_k:
        result.einschalten = False
        result.grund = (
            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
            f"{abweichung:.1f}K <= +{abw.ausschalten_bei_abweichung_k}K -> AUS"
        )
        return result
    
    # Einschalten: Zu kalt
    if abweichung >= abw.einschalten_bei_abweichung_k:
        result.einschalten = True
        result.grund = (
            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
            f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K -> EIN"
        )
        return result
    
    # In der Hysterese: Keine Aktion (Kompressor laeuft weiter/bleibt aus)
    result.einschalten = None
    result.grund = (
        f"In Hysterese: Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
        f"{abweichung:.1f}K (zwischen {abw.ausschalten_bei_abweichung_k}K und {abw.einschalten_bei_abweichung_k}K)"
    )
    return result


def bewerte_alle_regeln(
    config: WPSteuerungConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    kompressor_ein: bool,
    now: Optional[datetime] = None,
) -> Tuple[Optional[RegelErgebnis], List[RegelErgebnis]]:
    """
    Hauptfunktion: Bewertet alle Regeln und gibt die Gewinner-Regel zurueck.
    
    Returns:
        (gewinner, alle_ergebnisse): Die gewinnende Regel (oder None) und alle Ergebnisse.
    """
    if now is None:
        now = datetime.now()
    
    now_hour = now.hour
    nachtsperre_start = config.sicherheit.nachtsperre_start
    nachtsperre_ende = config.sicherheit.nachtsperre_ende
    
    ergebnisse: List[RegelErgebnis] = []
    
    # 1. PV-Regeln bewerten
    for pv_regel in config.pv_regeln:
        ergebnis = evaluate_pv_regel(
            pv_regel, temp_dict, pv_leistung, kompressor_ein,
            now_hour, nachtsperre_start, nachtsperre_ende
        )
        ergebnisse.append(ergebnis)
    
    # 2. Komfort-Regel
    ergebnis = evaluate_komfort(
        config.komfort, temp_dict, pv_leistung,
        nachtsperre_start, nachtsperre_ende, now_hour
    )
    ergebnisse.append(ergebnis)
    
    # 3. Zeitfenster-Regel
    ergebnis = evaluate_zeitfenster(
        config.zeitfenster, temp_dict, pv_leistung, now_hour
    )
    ergebnisse.append(ergebnis)
    
    # 4. Abweichungs-Regel
    ergebnis = evaluate_abweichung(
        config.abweichung, temp_dict, kompressor_ein,
        now_hour, nachtsperre_start, nachtsperre_ende
    )
    ergebnisse.append(ergebnis)
    
    # Gewinner bestimmen: Hoechste priorisierte Regel, die eine klare Entscheidung trifft
    aktive_regeln = [e for e in ergebnisse if e.aktiv and e.einschalten is not None]
    
    if not aktive_regeln:
        # Keine Regel will etwas tun
        return None, ergebnisse
    
    # Nach Prioritaet sortieren (hoeher zuerst)
    aktive_regeln.sort(key=lambda e: e.prioritaet, reverse=True)
    gewinner = aktive_regeln[0]
    
    logging.debug(
        f"Regel-Bewertung: {len(ergebnisse)} Regeln, "
        f"{len(aktive_regeln)} aktiv, Gewinner: {gewinner.name} "
        f"(Prio {gewinner.prioritaet}) -> {'EIN' if gewinner.einschalten else 'AUS'}"
    )
    
    return gewinner, ergebnisse


def formatiere_ergebnisse(ergebnisse: List[RegelErgebnis]) -> str:
    """Formatiert alle Ergebnisse fuer Logging/Anzeige."""
    lines = []
    for e in sorted(ergebnisse, key=lambda x: x.prioritaet, reverse=True):
        status = "EIN" if e.einschalten is True else ("AUS" if e.einschalten is False else "---")
        aktive = "" if e.aktiv else " [INAKTIV]"
        lines.append(f"  [{e.prioritaet:3d}] {status} {e.name}{aktive}: {e.grund}")
    return "\n".join(lines)
