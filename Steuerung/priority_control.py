"""
Prioritätenbasierte WP-Steuerung (Pareto-optimiert).

Statt modusbasierter Logik bewerten mehrere unabhängige Regeln
parallel die aktuelle Situation. Die Regel mit der höchsten Priorität,
deren Bedingungen erfüllt sind, gewinnt.
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Tuple, List
from dataclasses import dataclass

from json_config import (
    WPSteuerungConfig, PVRegel, KomfortConfig,
    ZeitfensterConfig, AbweichungConfig, WochenendeConfig,
    ForecastConfig, AdaptivePVConfig, CalculatedStartConfig
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
    """Liest einen Sensorwert aus dem Dictionary (mit Alias-Unterstützung)."""
    # Alias-Mapping: "mitte" -> "mittig" (abweichende Benennung im JSON vs. Code)
    alias_map = {"mitte": "mittig"}
    resolved = alias_map.get(sensor_name, sensor_name)
    return temp_dict.get(resolved)


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



def calcstart_nachtsperre_konflikt(
    ziel_uhr: float,
    nachtsperre_start: int,
    nachtsperre_ende: int,
) -> Tuple[bool, Optional[int], str]:
    """Prueft den Konflikt zwischen CalcStart-Zielzeit und Nachtsperre.

    Die Regel kann nur feuern, wenn eine volle Stunde ausserhalb der Sperre
    UND vor der Zielzeit liegt. Eine Zielzeit innerhalb/hinter der Sperre
    macht die Regel sonst STUMM (sie feuert nie, ohne Warnung).

    Returns:
        (tot, letzte_feuerbare_stunde, hinweis)
        tot=True: Regel kann NIE aktiv werden.
        tot=False mit hinweis != "": Vorheizen wird von der Sperre abgeschnitten.
    """
    feuerbare_stunden = [
        h for h in range(24)
        if not _is_nachtsperre(h, nachtsperre_start, nachtsperre_ende)
        and h < ziel_uhr
    ]

    if not feuerbare_stunden:
        return True, None, (
            f"CalcStart-Zielzeit {ziel_uhr:g}:00 liegt innerhalb/hinter der "
            f"Nachtsperre ({nachtsperre_start}-{nachtsperre_ende} Uhr) - "
            f"die Regel kann nie aktiv werden!"
        )

    letzte_stunde = max(feuerbare_stunden)
    folge_stunde_gesperrt = (
        letzte_stunde + 1 < 24
        and _is_nachtsperre(letzte_stunde + 1, nachtsperre_start, nachtsperre_ende)
    )
    if folge_stunde_gesperrt and (letzte_stunde + 1) < ziel_uhr:
        return False, letzte_stunde, (
            f"CalcStart-Vorheizen wird um {letzte_stunde + 1}:00 Uhr von der "
            f"Nachtsperre abgeschnitten (Zielzeit {ziel_uhr:g}:00 wird nicht "
            f"voll erreicht)"
        )

    return False, letzte_stunde, ""

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
    Bewertet eine PV-Regel mit Hysterese (einschalten_bei_c / ausschalten_bei_c).
    
    Logik:
    - Wenn Nachtsperre aktiv -> Regel inaktiv
    - Wenn Temp >= Ausschalt-Schwelle -> AUS (Schritt 1)
    - Wenn Kompressor schon laeuft UND PV >= Weiterlauf-Schwelle -> EIN (Schritt 2, PV-Shaping bis Ausschaltpunkt)
    - Wenn PV >= Schwelle UND Temp <= Einschalt-Schwelle -> EIN (Schritt 3, Neustart)
    - In Hysterese (zwischen Ein- und Ausschaltpunkt) -> Keine Aktion (Schritt 4)
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
    
    # 1. AUSSCHALTEN: Temp zu hoch (immer zuerst pruefen!)
    if temp >= regel.ausschalten_bei_c:
        result.einschalten = False
        result.grund = f"{sensor_name} {temp:.1f}C >= {regel.ausschalten_bei_c}C -> AUS"
        return result
    
    # 2. WEITERLAUFEN (PV-Shaping): Kompressor laeuft schon und PV reicht fuer Weiterbetrieb.
    #    Dies ermoeglicht das PV-Shaping von der Einschaltschwelle bis zum Ausschaltpunkt
    #    (z.B. 42->48°C). Der Kompressor bleibt an, solange PV >= Weiterlauf-Schwelle,
    #    auch wenn die Hysterese-Zone (42-48°C) durchschritten wird.
    if kompressor_ein and pv_leistung >= regel.weiterlaufen_ab_pv_watt:
        result.einschalten = True
        result.grund = (
            f"PV-Shaping: {sensor_name} {temp:.1f}C, PV {pv_leistung:.0f}W >= "
            f"{regel.weiterlaufen_ab_pv_watt}W (Weiterlauf bis {regel.ausschalten_bei_c}C)"
        )
        return result
    
    # 3. EINSCHALTEN (Neustart): PV genug UND Temp unter Einschaltschwelle
    if pv_leistung >= regel.pv_schwelle_watt and temp <= regel.einschalten_bei_c:
        result.einschalten = True
        result.grund = (
            f"PV-Shaping: {sensor_name} {temp:.1f}C <= {regel.einschalten_bei_c}C, "
            f"PV {pv_leistung:.0f}W >= {regel.pv_schwelle_watt}W -> EIN"
        )
        return result
    
    # 4. HYSTERESE: Temp zwischen Ein- und Ausschaltpunkt -> Keine Aktion
    if regel.einschalten_bei_c < temp < regel.ausschalten_bei_c:
        result.einschalten = None
        result.grund = (
            f"In Hysterese: {sensor_name} {temp:.1f}C zwischen "
            f"{regel.einschalten_bei_c}C und {regel.ausschalten_bei_c}C (PV={pv_leistung:.0f}W)"
        )
        return result
    
    # 5. Keine Bedingung erfuellt
    result.einschalten = None
    result.grund = f"Keine Bedingung erfuellt (PV={pv_leistung:.0f}W, {sensor_name}={temp:.1f}C)"
    return result


def evaluate_wochenende(
    wochenende: WochenendeConfig,
    now: datetime,
) -> RegelErgebnis:
    """
    Wochenende-Regel: Blockiert Einschalten am Wochenende vor fruehestens_uhr.
    
    - Wenn Wochenende UND aktuelle Stunde < fruehestens_uhr -> AUS
    - Sonst -> inaktiv (andere Regeln entscheiden)
    """
    if not wochenende.aktiv:
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,  # blockierend
            aktiv=False,
            grund="Wochenende-Regel inaktiv"
        )
    
    if not _is_weekend(now):
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,
            aktiv=False,
            grund="Kein Wochenende"
        )
    
    # Wochenende aktiv: Pruefe ob vor fruehestens_uhr
    if now.hour < wochenende.fruehestens_uhr:
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,  # blockiert alles andere
            aktiv=True,
            einschalten=False,
            grund=f"Wochenende: Vor {wochenende.fruehestens_uhr} Uhr ({now.hour}:xx) -> AUS"
        )
    
    return RegelErgebnis(
        name="Wochenende",
        prioritaet=wochenende.prioritaet,
        aktiv=True,
        einschalten=None,  # Keine Aktion, andere Regeln dufen entscheiden
        grund=f"Wochenende: Ab {wochenende.fruehestens_uhr} Uhr erlaubt ({now.hour}:xx)"
    )


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
    
    # Wenn oben-Sensor nicht verfuegbar: Fallback auf mittig
    temp_mittig = _parse_sensor(temp_dict, "mittig")
    if temp_oben is None and temp_mittig is not None and temp_mittig <= komfort.notfall_einschalten_bei_c:
        result.einschalten = True
        result.grund = f"NOTFALL (Fallback mittig): mittig {temp_mittig:.1f}C <= {komfort.notfall_einschalten_bei_c}C -> EIN"
        return result
    
    # Wenn oben UND mittig nicht verfuegbar: Fallback auf unten
    if temp_oben is None and temp_mittig is None and temp <= komfort.notfall_einschalten_bei_c:
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
    
    if not zf.aktiv:
        result.grund = "Zeitfenster-Regel inaktiv"
        return result
    
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
    
    Bei Nachtsperre: Nur Ausschalten erlaubt, kein Einschalten
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
    
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    abweichung = abw.solltemperatur_c - temp  # Positiv = zu kalt, Negativ = zu warm
    
    # Ausschalten: Ziel erreicht oder ueberschritten (immer erlaubt, auch Nachts)
    if abweichung <= abw.ausschalten_bei_abweichung_k:
        result.einschalten = False
        result.grund = (
            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
            f"{abweichung:.1f}K <= +{abw.ausschalten_bei_abweichung_k}K -> AUS"
        )
        return result
    
    # Bei Nachtsperre: Kein Einschalten (Blockierungsgrund anzeigen)
    if nachtsperre:
        result.aktiv = False
        result.grund = f"Nachtsperre (kein Einschalten, {abw.temperaturfuehler}={temp:.1f}C)"
        return result
    
    # Einschalten: Zu kalt
    if abweichung >= abw.einschalten_bei_abweichung_k:
        # 2-Zonen-Schichtungs-Check: Wenn der konfigurierte Fuehler nicht "oben" ist
        # (z.B. unten/mittig), prüfe ob oben noch warm genug ist.
        # Verhindert unnötige Netzstrom-Starts nach Zapfen, wenn oben noch warmes
        # Wasser vorhanden ist (vermiedene Schichtungs-Falle).
        if abw.temperaturfuehler != "oben" and abw.schichtung_min_oben_c > 0:
            temp_oben = _parse_sensor(temp_dict, "oben")
            if temp_oben is not None and temp_oben >= abw.schichtung_min_oben_c:
                result.einschalten = None
                result.grund = (
                    f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                    f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                    f"aber oben {temp_oben:.1f}C >= {abw.schichtung_min_oben_c}C "
                    f"(Schichtung) -> kein Einschalten"
                )
                return result
        
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



def evaluate_forecast(
    forecast_cfg: ForecastConfig,
    temp_dict: Dict[str, Optional[float]],
    forecast_wh_qm: Optional[float],
    now_hour: int,
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
) -> RegelErgebnis:
    """
    Prognose-Regel: Vorheizen bei schlechter Solar-Prognose, sparen bei guter.
    
    - Morgen bewölkt (Prognose < Niedrig-Schwelle): Heute vorheizen
    - Morgen sonnig (Prognose > Hoch-Schwelle): Heute sparen (nicht unnötig heizen)
    """
    result = RegelErgebnis(
        name="Forecast",
        prioritaet=forecast_cfg.prioritaet,
        aktiv=forecast_cfg.aktiv
    )
    
    if not forecast_cfg.aktiv:
        result.grund = "Forecast-Regel inaktiv"
        return result
    
    if forecast_wh_qm is None:
        result.aktiv = False
        result.grund = "Keine Prognose verfuegbar"
        return result
    
    # Nachtsperre: Kein Vorheizen/Sparen waehrend der Sperrzeit
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre aktiv"
        return result
    
    temp_oben = _parse_sensor(temp_dict, "oben")
    sensor_name = forecast_cfg.temperaturfuehler
    temp_sensor = _parse_sensor(temp_dict, sensor_name)
    temp = temp_sensor if temp_sensor is not None else temp_oben
    if temp is None:
        result.aktiv = False
        result.grund = "Kein Sensorwert verfuegbar"
        return result
    
    # VORHEIZEN: Prognose morgen schlecht -> heute vorheizen
    if forecast_wh_qm <= forecast_cfg.fc_schwelle_niedrig_wh:
        if forecast_cfg.vorheiz_start_uhr <= now_hour < forecast_cfg.vorheiz_ende_uhr:
            if temp <= forecast_cfg.t_vorheiz_ab_c:
                result.einschalten = True
                result.grund = (
                    f"Forecast-Vorheiz: Morgen {forecast_wh_qm:.0f} Wh/qm <= "
                    f"{forecast_cfg.fc_schwelle_niedrig_wh:.0f} (schlecht), "
                    f"Temp {temp:.1f}C <= {forecast_cfg.t_vorheiz_ab_c}C -> EIN"
                )
                return result
            result.einschalten = None
            result.grund = (
                f"Forecast: Morgen schlecht ({forecast_wh_qm:.0f} Wh/qm), "
                f"aber Temp {temp:.1f}C > {forecast_cfg.t_vorheiz_ab_c}C"
            )
            return result
    
    # SPAREN: Prognose morgen gut -> heute sparen (nicht heizen)
    if forecast_wh_qm >= forecast_cfg.fc_schwelle_hoch_wh:
        if forecast_cfg.sparen_start_uhr <= now_hour < forecast_cfg.sparen_ende_uhr:
            if temp >= forecast_cfg.t_vorheiz_ab_c:
                result.einschalten = False
                result.grund = (
                    f"Forecast-Sparen: Morgen {forecast_wh_qm:.0f} Wh/qm >= "
                    f"{forecast_cfg.fc_schwelle_hoch_wh:.0f} (gut), "
                    f"Temp {temp:.1f}C >= {forecast_cfg.t_vorheiz_ab_c}C -> Sparen"
                )
                return result
    
    result.grund = f"Forecast {forecast_wh_qm:.0f} Wh/qm -> keine Aktion"
    return result


def evaluate_adaptive_pv(
    adaptive_cfg: AdaptivePVConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    forecast_wh_qm: Optional[float],
    kompressor_ein: bool,
    now_hour: int = 12,
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
) -> RegelErgebnis:
    """
    Adaptive-PV-Regel: PV-Schwelle passt sich dynamisch an.
    
    - Temperaturabhängig: Bei kaltem Boiler wird die Schwelle gesenkt
    - Prognoseabhängig: Bei schlechter Prognose wird die Schwelle gesenkt
    """
    result = RegelErgebnis(
        name="AdaptivePV",
        prioritaet=adaptive_cfg.prioritaet,
        aktiv=adaptive_cfg.aktiv
    )
    
    if not adaptive_cfg.aktiv:
        result.grund = "AdaptivePV-Regel inaktiv"
        return result
    
    # Nachtsperre: Kein Einschalten waehrend der Sperrzeit
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre aktiv"
        return result
    
    sensor_name = adaptive_cfg.temperaturfuehler
    temp = _parse_sensor(temp_dict, sensor_name)
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{sensor_name}' nicht verfuegbar"
        return result
    
    if temp >= adaptive_cfg.tmax_c:
        result.einschalten = False
        result.grund = f"AdaptivePV: {sensor_name} {temp:.1f}C >= {adaptive_cfg.tmax_c}C -> AUS"
        return result
    
    # Hysterese: Nur einschalten wenn Temp unter Einschaltgrenze (tmax_c - 3K Default)
    # Sonst soll die PV-Regel mit ihrer Hysterese (42/48°C) entscheiden
    einschalten_bis_c = getattr(adaptive_cfg, 'einschalten_bis_c', None)
    if einschalten_bis_c is None:
        einschalten_bis_c = adaptive_cfg.tmax_c - 3.0
    
    if temp >= einschalten_bis_c and not kompressor_ein:
        result.einschalten = None
        result.grund = (
            f"AdaptivePV: {sensor_name} {temp:.1f}C >= {einschalten_bis_c:.1f}C "
            f"(Einschaltgrenze) -> Hysterese, kein Einschalten"
        )
        return result
    
    # Dynamische Schwelle berechnen
    schwelle = adaptive_cfg.base_threshold_watt
    
    # Temperatur-Anpassung
    if temp < adaptive_cfg.t_aggressiv_kalt_c:
        schwelle *= 0.5  # Sehr kalt: aggressiver heizen
    elif temp < adaptive_cfg.t_normal_kalt_c:
        schwelle *= 0.7  # Kalt: etwas niedrigere Schwelle
    
    # Prognose-Anpassung
    if forecast_wh_qm is not None:
        if forecast_wh_qm >= adaptive_cfg.fc_schwelle_gut_wh:
            schwelle *= 1.5  # Sehr sonnig: höhere Schwelle = konservativer
        elif forecast_wh_qm <= adaptive_cfg.fc_schwelle_schlecht_wh:
            schwelle *= 0.5  # Bewölkt: niedrige Schwelle = PV jetzt nutzen
    
    if pv_leistung >= schwelle:
        result.einschalten = True
        result.grund = (
            f"AdaptivePV: PV {pv_leistung:.0f}W >= {schwelle:.0f}W "
            f"(Basis {adaptive_cfg.base_threshold_watt:.0f}W, {sensor_name}={temp:.1f}C) -> EIN"
        )
        return result
    
    result.grund = f"AdaptivePV: PV {pv_leistung:.0f}W < {schwelle:.0f}W"
    return result


def evaluate_calculated_start(
    calc_cfg: CalculatedStartConfig,
    temp_dict: Dict[str, Optional[float]],
    now_hour: int,
    now_minute: int,
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
    forecast_wh_qm: Optional[float] = None,
    learned_heating_rate_unten: Optional[float] = None,
    learned_heating_rate_gesamt: Optional[float] = None,
    learned_target_hour: Optional[float] = None,
) -> RegelErgebnis:
    """
    Berechnete-Startzeit-Regel: Schaltet rechtzeitig vor der Zielzeit ein.
    
    Berechnet aus Temperaturdifferenz und Heizrate die benötigte Zeit.
    Wenn die verbleibende Zeit knapp wird -> einschalten.
    """
    result = RegelErgebnis(
        name="CalcStart",
        prioritaet=calc_cfg.prioritaet,
        aktiv=calc_cfg.aktiv
    )
    
    if not calc_cfg.aktiv:
        result.grund = "CalcStart-Regel inaktiv"
        return result

    # Nachtsperre: Kein Einschalten waehrend der Sperrzeit
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        # Konflikt-Hinweis: Liegt die Zielzeit hinter/innerhalb der Sperre,
        # kann die Regel NIE feuern - das soll im Grund lesbar sein.
        ziel = learned_target_hour if learned_target_hour is not None else float(calc_cfg.target_uhr)
        tot, _, hinweis = calcstart_nachtsperre_konflikt(
            ziel, nachtsperre_start, nachtsperre_ende
        )
        result.grund = "Nachtsperre aktiv" + (f" | {hinweis}" if tot else "")
        return result
    
    temp_unten = _parse_sensor(temp_dict, "unten")
    temp_mitte = _parse_sensor(temp_dict, "mitte")
    temp_oben = _parse_sensor(temp_dict, "oben")
    
    if temp_unten is None or temp_mitte is None:
        result.aktiv = False
        result.grund = "Sensoren nicht verfuegbar"
        return result
    
    # Bereits erreicht? Nur unteren Fuehler pruefen (kaltester Punkt im Boiler)
    # Der obere/mittige Fuehler kann durch Schichtung heiss sein, obwohl
    # die WP noch viel Energie unten reinstecken kann (vor allem bei PV!)
    if temp_unten is not None and temp_unten >= calc_cfg.tmax_c:
        result.einschalten = False
        result.grund = f"CalcStart: Zieltemp {calc_cfg.tmax_c}C unten bereits erreicht -> AUS"
        return result
    
    # Aktuelle Zeit
    current_time = now_hour + now_minute / 60.0
    
    # Noch vor Zielzeit?
    if current_time >= calc_cfg.target_uhr:
        # Nach Zielzeit: nichts tun
        result.grund = f"CalcStart: Nach Zielzeit ({now_hour}:{now_minute:02d} > {calc_cfg.target_uhr}:00)"
        return result
    
    # Temperaturdifferenz berechnen
    diff_unten = max(0, calc_cfg.solltemperatur_c - temp_unten)
    diff_mitte = max(0, calc_cfg.solltemperatur_c - temp_mitte)
    diff_oben = max(0, calc_cfg.solltemperatur_c - temp_oben) if temp_oben is not None else 0
    diff_gesamt = diff_unten + diff_mitte + diff_oben
    
    if diff_gesamt <= 0:
        result.grund = f"CalcStart: Soll {calc_cfg.solltemperatur_c}C bereits erreicht"
        return result
    
    # Gelernte Heizraten verwenden (falls vorhanden), sonst Config-Defaults
    heizrate_unten = learned_heating_rate_unten if learned_heating_rate_unten is not None else calc_cfg.heizrate_unten_c_h
    heizrate_gesamt = learned_heating_rate_gesamt if learned_heating_rate_gesamt is not None else calc_cfg.heizrate_gesamt_c_h
    ziel_uhr = learned_target_hour if learned_target_hour is not None else float(calc_cfg.target_uhr)

    # Benoetigte Heizzeit
    hours_needed = diff_unten / max(heizrate_unten, 0.1)
    hours_needed_mitte = diff_mitte / max(heizrate_gesamt, 0.1)
    hours_needed = max(hours_needed, hours_needed_mitte)
    
    time_left = ziel_uhr - current_time
    
    # === Saisonale + Prognose-basierte Puffer-Berechnung ===
    # Saison erkennen (0=Winter, 1=Sommer)
    # Wir nutzen now_hour/min + wissen nicht direkt den Monat, aber wir
    # kriegen ihn ueber das now datetime objekt... da wir nur now_hour haben,
    # nutzen wir die Config-Puffer direkt mit Prognose-Anpassung.
    
    # Basis-Puffer aus Config
    buffer_hours = time_left - hours_needed
    
    # Prognose-Anpassung: Heute viel PV erwartet? -> laenger warten
    pv_faktor = 1.0
    if forecast_wh_qm is not None:
        if forecast_wh_qm >= 3000:  # Sehr sonnig
            pv_faktor = 2.0
            pv_label = "sehr sonnig"
        elif forecast_wh_qm >= 1500:  # Sonnig
            pv_faktor = 1.5
            pv_label = "sonnig"
        elif forecast_wh_qm <= 500:  # Bewoelkt
            pv_faktor = 0.5
            pv_label = "bewoelkt"
        else:
            pv_faktor = 1.0
            pv_label = f"{forecast_wh_qm:.0f} Wh/qm"
    else:
        pv_label = "keine Prognose"
    
    effektiver_puffer = buffer_hours * pv_faktor
    
    if buffer_hours < 0:
        # Bereits ueber Zielzeit oder zu spaet -> sofort heizen!
        result.einschalten = True
        result.grund = (
            f"CalcStart: ZU SPAET! Zeitablauf ({time_left:.1f}h < {hours_needed:.1f}h) "
            f"-> EIN (Notfall)"
        )
        return result
    
    if effektiver_puffer < 0.5:
        # Weniger als 30min effektiven Puffer -> heizen
        result.einschalten = True
        result.grund = (
            f"CalcStart: Nur {effektiver_puffer:.1f}h Puffer (PV={pv_label}, "
            f"brauche {hours_needed:.1f}h bis {ziel_uhr:.0f}:00) -> EIN"
        )
        return result
    
    # Genug Puffer + gute PV-Prognose -> warten
    result.einschalten = None
    result.grund = (
        f"CalcStart: {effektiver_puffer:.1f}h Puffer reicht (PV={pv_label}, "
        f"brauche {hours_needed:.1f}h bis {ziel_uhr:.0f}:00) -> warte auf PV"
    )
    return result


def bewerte_alle_regeln(
    config: WPSteuerungConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    kompressor_ein: bool,
    now: Optional[datetime] = None,
    forecast_wh_qm: Optional[float] = None,
    forecast_today_wh_qm: Optional[float] = None,
    soc: Optional[float] = None,
    battery_power: Optional[float] = None,
    learned_heating_rate_unten: Optional[float] = None,
    learned_heating_rate_gesamt: Optional[float] = None,
    learned_target_hour: Optional[float] = None,
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
    
    # 0. Wochenende-Regel (hoechste Prioritaet: blockiert Einschalten am Wochenende vor fruehestens_uhr)
    ergebnis = evaluate_wochenende(config.wochenende, now)
    ergebnisse.append(ergebnis)
    
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
    
    # 5. Forecast-Regel (Prognose-basiert vorheizen/sparen)
    ergebnis = evaluate_forecast(
        config.forecast, temp_dict, forecast_wh_qm, now_hour,
        nachtsperre_start, nachtsperre_ende
    )
    ergebnisse.append(ergebnis)
    
    # 6. Adaptive-PV-Regel (dynamische PV-Schwelle)
    ergebnis = evaluate_adaptive_pv(
        config.adaptive_pv, temp_dict, pv_leistung, forecast_wh_qm, kompressor_ein,
        now_hour, nachtsperre_start, nachtsperre_ende
    )
    ergebnisse.append(ergebnis)
    
    # 7. Calculated-Start-Regel (optimierter Startzeitpunkt)
    ergebnis = evaluate_calculated_start(
        config.calculated_start, temp_dict, now_hour, now.minute,
        nachtsperre_start, nachtsperre_ende,
        forecast_wh_qm=forecast_today_wh_qm,
        learned_heating_rate_unten=learned_heating_rate_unten,
        learned_heating_rate_gesamt=learned_heating_rate_gesamt,
        learned_target_hour=learned_target_hour,
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
    
    # Notfall-Pruefung: Wenn Wochenende blockiert, aber Komfort-Notfall aktiv ist,
    # ueberschreibt der Notfall die Wochenende-Sperre.
    if gewinner.name == "Wochenende" and gewinner.einschalten is False:
        for e in ergebnisse:
            if e.name == "Komfort" and e.aktiv and e.einschalten is True and "NOTFALL" in e.grund:
                logging.info(f"Notfall ueberschreibt Wochenende-Sperre: {e.grund}")
                gewinner = e
                break
    
    # Bewertung wird nur alle 5 Minuten in priority_control_logic.py geloggt (INFO, throttelt)
    # Hier nur minimales DEBUG, nicht bei jedem Durchlauf
    
    
    return gewinner, ergebnisse


def formatiere_ergebnisse(ergebnisse: List[RegelErgebnis]) -> str:
    """Formatiert alle Ergebnisse fuer Logging/Anzeige."""
    lines = []
    for e in sorted(ergebnisse, key=lambda x: x.prioritaet, reverse=True):
        status = "EIN" if e.einschalten is True else ("AUS" if e.einschalten is False else "---")
        aktive = "" if e.aktiv else " [INAKTIV]"
        lines.append(f"  [{e.prioritaet:3d}] {status} {e.name}{aktive}: {e.grund}")
    return "\n".join(lines)
