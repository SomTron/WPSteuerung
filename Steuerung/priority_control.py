"""
Prioritätenbasierte WP-Steuerung (Pareto-optimiert).

Statt modusbasierter Logik bewerten mehrere unabhängige Regeln
parallel die aktuelle Situation. Die Regel mit der höchsten Priorität,
deren Bedingungen erfüllt sind, gewinnt.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
from rule_types import RegelErgebnis

from energy_source import (
    batterie_entladung_watt,
    classify_energy_source,
)
from utils import to_naive
from json_config import (
    WPSteuerungConfig,
    PVRegel,
    KomfortConfig,
    ZeitfensterConfig,
    AbweichungConfig,
    WochenendeConfig,
    ForecastConfig,
    AdaptivePVConfig,
    CalculatedStartConfig,
    MindestTempConfig,
    BatterieConfig,
    EinspeisungConfig,
    NotfallschutzConfig,
)

# Module-level throttles: Letzte Zeitstempel fuer wiederholte Warnungen/Logs.
_last_calcstart_log: Optional[datetime] = None
_last_stale_warning: Optional[datetime] = None
STALE_LOG_INTERVAL_MIN = 5

def _parse_sensor(
    temp_dict: Dict[str, Optional[float]], sensor_name: str
) -> Optional[float]:
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
        h
        for h in range(24)
        if not _is_nachtsperre(h, nachtsperre_start, nachtsperre_ende) and h < ziel_uhr
    ]

    if not feuerbare_stunden:
        return (
            True,
            None,
            (
                f"CalcStart-Zielzeit {ziel_uhr:g}:00 liegt innerhalb/hinter der "
                f"Nachtsperre ({nachtsperre_start}-{nachtsperre_ende} Uhr) - "
                f"die Regel kann nie aktiv werden!"
            ),
        )

    letzte_stunde = max(feuerbare_stunden)
    folge_stunde_gesperrt = letzte_stunde + 1 < 24 and _is_nachtsperre(
        letzte_stunde + 1, nachtsperre_start, nachtsperre_ende
    )
    if folge_stunde_gesperrt and (letzte_stunde + 1) < ziel_uhr:
        return (
            False,
            letzte_stunde,
            (
                f"CalcStart-Vorheizen wird um {letzte_stunde + 1}:00 Uhr von der "
                f"Nachtsperre abgeschnitten (Zielzeit {ziel_uhr:g}:00 wird nicht "
                f"voll erreicht)"
            ),
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
            grund="Nachtsperre aktiv",
        )

    if temp is None:
        return RegelErgebnis(
            name=regel.name,
            prioritaet=regel.prioritaet,
            aktiv=False,
            grund=f"Sensor '{sensor_name}' nicht verfuegbar",
        )

    # Grundlage fuer Entscheidung
    result = RegelErgebnis(name=regel.name, prioritaet=regel.prioritaet, aktiv=True)

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
    result.reason_code = "pv_unterbrechung"
    result.grund = (
        f"Keine Bedingung erfuellt (PV={pv_leistung:.0f}W, {sensor_name}={temp:.1f}C)"
    )
    return result


def evaluate_mindesttemp(
    mindest_cfg: MindestTempConfig,
    temp_dict: Dict[str, Optional[float]],
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    learned_evening_window: Optional[Tuple[float, float]] = None,
    learned_morning_window: Optional[Tuple[float, float]] = None,
) -> List[RegelErgebnis]:
    """Mindest-Temperatur-Garantien pro Fuehler und Zeitfenster.

    Zweck: Der Boiler darf zu definierten Zeiten nicht zu kalt sein
    (z.B. oben mittags >= 40C, mitte am Abend >= 40C zum Duschen).
    Innerhalb des Fensters gilt die Garantie AUCH waehrend der Nachtsperre -
    genau dafuer ist sie da. Ausnahme: Eintraege mit
    nachtsperre_ueberschreiben=False feuern NICHT in der Nachtsperre; ihre
    Garantie endet mit dem Sperren-Beginn (kein Nacht-Heizen ohne PV).
    Ausserhalb der Fenster entscheidet wie ueblich das Spar-Prioritaetensystem.
    """
    ergebnisse = []
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)

    for eintrag in mindest_cfg.eintraege:
        name = f"MinTemp-{eintrag.name}"
        result = RegelErgebnis(
            name=name,
            prioritaet=mindest_cfg.prioritaet,
            aktiv=False,
        )

        if not mindest_cfg.aktiv:
            result.grund = "MindestTemp-Regel inaktiv"
            ergebnisse.append(result)
            continue

        # Zeitfenster: statisch aus Config oder dynamisch gelernt?
        start_uhr = eintrag.start_uhr
        ende_uhr = eintrag.ende_uhr
        fenster_hinweis = ""
        lern_fenster = (
            learned_morning_window
            if (eintrag.start_uhr < 12 and learned_morning_window is not None)
            else learned_evening_window
        )
        if eintrag.fenster_aus_lernen and lern_fenster is not None:
            l_start, l_ende = lern_fenster
            # Gelerntes Fenster mit Sicherheitsgrenzen klemmen:
            # max. 2h frueher Start, max. 1h spaeteres Ende als konfiguriert.
            import math

            start_uhr = max(eintrag.start_uhr - 2, int(math.floor(l_start)))
            ende_uhr = min(eintrag.ende_uhr + 1, int(math.ceil(l_ende)))
            ende_uhr = max(ende_uhr, start_uhr + 1)
            fenster_hinweis = (
                f" [gelernt {l_start:.1f}-{l_ende:.1f}h -> {start_uhr}-{ende_uhr} Uhr]"
            )

        if not _is_zeitfenster_active(now_hour, start_uhr, ende_uhr):
            result.grund = (
                f"{eintrag.name}: auserhalb Fenster {start_uhr}-{ende_uhr} Uhr"
                f"{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        # Nachtsperre respektieren? Garantien duerfen die Sperre normalerweise
        # ueberschreiben - genau dafuer sind sie da. Eintraege mit
        # nachtsperre_ueberschreiben=False bleiben innerhalb der Sperre aber
        # stumm: Die Garantie gilt nur BIS zum Sperren-Beginn (z.B. abends
        # warm genug zum Duschen), danach wird nachts nicht mehr nachgeheizt
        # (nachts gibt es kein PV -> sonst Netzstrom).
        if nachtsperre and not eintrag.nachtsperre_ueberschreiben:
            result.grund = (
                f"{eintrag.name}: Nachtsperre aktiv ({nachtsperre_start}-{nachtsperre_ende} Uhr) "
                f"- Garantie ueberschreibt nicht{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        temp = _parse_sensor(temp_dict, eintrag.temperaturfuehler)
        if temp is None:
            result.grund = (
                f"{eintrag.name}: Sensor '{eintrag.temperaturfuehler}' nicht verfuegbar"
            )
            ergebnisse.append(result)
            continue

        result.aktiv = True
        aus_schwelle = eintrag.min_temp_c + eintrag.hysterese_k

        # WICHTIG: Diese Regel ist rein ADDITIV - sie kann nur EINSCHALTEN
        # (Garantieverletzung), aber niemals blockieren. Ein explizites AUS
        # wuerde mit ihrer hohen Prioritaet Heizwuensche anderer Regeln
        # (PV/Abweichung/CalcStart) ungewollt abschneiden. Ist die Garantie
        # erfuellt, tritt die Regel therefore stumm zurueck (einschalten=None).
        # Die Ausschaltung nach einem Garantie-Einschalten uebernimmt der
        # normale Setpoint (min_temp_c + hysterese_k via _extract_ausschaltpunkt).

        # EINSCHALTEN: unter der Mindesttemperatur (garantiert auch in Nachtsperre)
        if temp < eintrag.min_temp_c:
            result.einschalten = True
            hinweis = " [Nachtsperre ueberschrieben]" if nachtsperre else ""
            result.grund = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C < "
                f"{eintrag.min_temp_c}C -> EIN{hinweis}{fenster_hinweis}"
            )
            ergebnisse.append(result)
            continue

        # Hysterese-Zone bzw. Garantie erfuellt -> keine Aktion
        if temp >= aus_schwelle:
            grund_text = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C >= "
                f"{aus_schwelle:.1f}C ({eintrag.min_temp_c}C+{eintrag.hysterese_k}K) "
                f"-> Garantie erfuellt"
            )
        else:
            grund_text = (
                f"MindestTemp: {eintrag.temperaturfuehler} {temp:.1f}C in Hysterese "
                f"({eintrag.min_temp_c}-{aus_schwelle:.1f}C)"
            )
        result.einschalten = None
        result.grund = grund_text
        ergebnisse.append(result)

    return ergebnisse


def evaluate_batterie(
    batt_cfg: BatterieConfig,
    temp_dict: Dict[str, Optional[float]],
    feedin_watt: float,
    soc: Optional[float],
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    forecast_wh_qm: Optional[float] = None,
    battery_power: Optional[float] = None,
) -> RegelErgebnis:
    "Batterie-Regel: Heizen mit Hausbatterie statt Netzstrom."
    result = RegelErgebnis(
        name="Batterie",
        prioritaet=batt_cfg.prioritaet,
        aktiv=batt_cfg.aktiv,
    )
    if not batt_cfg.aktiv:
        result.grund = "Batterie-Regel inaktiv"
        return result
    if soc is None:
        result.aktiv = False
        result.grund = "SOC nicht verfuegbar"
        return result
    min_batpower = max(float(getattr(batt_cfg, "min_batterieleistung_watt", 0.0) or 0.0), 0.0)
    discharge = batterie_entladung_watt(battery_power)
    if discharge is None or discharge < min_batpower:
        result.grund = (
            f"Batterie: Entladung {discharge if discharge is not None else 'n/a'}W "
            f"< {min_batpower:.0f}W (keine Entladung nachgewiesen)"
        )
        return result
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre aktiv (kein Batterie-Heizen)"
        return result
    temp = _parse_sensor(temp_dict, batt_cfg.temperaturfuehler)
    if temp is None:
        result.aktiv = False
        result.grund = "Sensor nicht verfuegbar"
        return result
    if temp >= batt_cfg.ausschalten_bei_c:
        result.einschalten = False
        result.grund = (
            f"Batterie: {batt_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{batt_cfg.ausschalten_bei_c}C -> AUS"
        )
        return result
    # Dynamische Batteriereserve (Punkt C)
    eff_min_soc = batt_cfg.min_soc_prozent
    if forecast_wh_qm is not None and forecast_wh_qm >= 2000.0:
        entlastung = min(
            getattr(batt_cfg, "entlastung_max_prozent", 15.0),
            eff_min_soc - getattr(batt_cfg, "min_soc_absolut", 10.0),
        )
        eff_min_soc -= max(entlastung, 0.0)
    # SOC-Hysterese gegen Grenzkanten-Flattern: Im laufenden Zyklus genuegt
    # (eff_min_soc - Hysterese), damit ein 1%-SOC-Ticken den Lauf nicht abbricht.
    soc_schwelle = eff_min_soc
    if kompressor_ein:
        soc_schwelle -= max(getattr(batt_cfg, "soc_hysterese_prozent", 2.0), 0.0)
    strom_ok = (
        soc >= soc_schwelle
        and discharge is not None
        and discharge >= min_batpower
        and feedin_watt >= batt_cfg.max_netzbezug_watt
    )
    if kompressor_ein and strom_ok and temp > batt_cfg.einschalten_bei_c:
        result.einschalten = True
        result.grund = (
            f"Batterie-Weiterlauf: SOC {soc:.0f}% >= {eff_min_soc:.0f}%, "
            f"Einspeisung {feedin_watt:.0f}W >= {batt_cfg.max_netzbezug_watt:.0f}W, "
            f"Entladung {discharge:.0f}W, {batt_cfg.temperaturfuehler} {temp:.1f}C"
        )
        return result
    if strom_ok and temp <= batt_cfg.einschalten_bei_c:
        result.einschalten = True
        result.grund = (
            f"Batterie: SOC {soc:.0f}% >= {eff_min_soc:.0f}%, "
            f"Einspeisung {feedin_watt:.0f}W >= {batt_cfg.max_netzbezug_watt:.0f}W, "
            f"Entladung {discharge:.0f}W, {batt_cfg.temperaturfuehler} {temp:.1f}C -> EIN"
        )
        return result
    if soc < soc_schwelle:
        result.grund = f"Batterie: SOC {soc:.0f}% < {soc_schwelle:.0f}% (Schonung)"
    elif feedin_watt < batt_cfg.max_netzbezug_watt:
        result.grund = f"Batterie: Netzbezug {feedin_watt:.0f}W < {batt_cfg.max_netzbezug_watt:.0f}W (kein Netzstrom!)"
    elif discharge < min_batpower:
        result.grund = f"Batterie: Entladung {discharge:.0f}W < {min_batpower:.0f}W"
    else:
        result.grund = f"Batterie: {temp:.1f}C in Hysterese ({batt_cfg.einschalten_bei_c}-{batt_cfg.ausschalten_bei_c}C)"
    return result


def evaluate_einspeisung(
    einsp_cfg: EinspeisungConfig,
    temp_dict: Dict[str, Optional[float]],
    feedin_watt: float,
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
) -> RegelErgebnis:
    """Einspeise-Begrenzungs-Regel (PV-Shaping am Netzlimit).

    Zweck (Betriebsvorgabe): Es darf nicht mehr als ~7500W eingespeist
    werden. Liegt die Einspeisung an dieser Grenze, ist der Strom fuer die
    WP praktisch gratis bzw. wuerde sonst gedrosselt - idealer Heizzeitraum.

    Logik:
    - EIN sobald feedinpower >= einspeisegrenze_watt und Fuehler < ausschalten_bei_c
    - WEITERLAUFEN solange feedinpower >= weiterlauf_ab_watt (Abschlag, da die
      WP selbst ~600W zieht und die Einspeisung beim Start einbricht)
    - AUS wenn Fuehler >= ausschalten_bei_c
    """
    result = RegelErgebnis(
        name="Einspeisung",
        prioritaet=einsp_cfg.prioritaet,
        aktiv=einsp_cfg.aktiv,
    )

    if not einsp_cfg.aktiv:
        result.grund = "Einspeisung-Regel inaktiv"
        return result

    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)
    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre aktiv"
        return result

    temp = _parse_sensor(temp_dict, einsp_cfg.temperaturfuehler)
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{einsp_cfg.temperaturfuehler}' nicht verfuegbar"
        return result

    # 1. AUSSCHALTEN: Ziel erreicht (immer zuerst)
    if temp >= einsp_cfg.ausschalten_bei_c:
        result.einschalten = False
        result.grund = (
            f"Einspeisung: {einsp_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{einsp_cfg.ausschalten_bei_c}C -> AUS (Einspeisung {feedin_watt:.0f}W)"
        )
        return result

    # 2. WEITERLAUFEN: laeuft schon, Ueberschuss traegt weiter
    if kompressor_ein and feedin_watt >= einsp_cfg.weiterlauf_ab_watt and temp > 0:
        result.einschalten = True
        result.grund = (
            f"PV-Shaping Weiterlauf: Einspeisung {feedin_watt:.0f}W >= "
            f"{einsp_cfg.weiterlauf_ab_watt:.0f}W (Grenze {einsp_cfg.einspeisegrenze_watt:.0f}W), "
            f"{einsp_cfg.temperaturfuehler} {temp:.1f}C bis "
            f"{einsp_cfg.ausschalten_bei_c}C"
        )
        return result

    # 3. EINSCHALTEN: Einspeisung am/am ueber dem Netzlimit
    if feedin_watt >= einsp_cfg.einspeisegrenze_watt:
        result.einschalten = True
        result.grund = (
            f"PV-Shaping: Einspeisung {feedin_watt:.0f}W >= Grenze "
            f"{einsp_cfg.einspeisegrenze_watt:.0f}W -> EIN "
            f"({einsp_cfg.temperaturfuehler} {temp:.1f}C)"
        )
        return result

    result.reason_code = "pv_unterbrechung"
    result.grund = f"Einspeisung {feedin_watt:.0f}W < {einsp_cfg.einspeisegrenze_watt:.0f}W -> keine Aktion"
    return result


def evaluate_wochenende(
    wochenende: WochenendeConfig,
    now: datetime,
    kompressor_ein: bool = False,
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
            grund="Wochenende-Regel inaktiv",
        )

    if not _is_weekend(now):
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,
            aktiv=False,
            grund="Kein Wochenende",
        )

    # Wochenende aktiv: Pruefe ob vor fruehestens_uhr
    if now.hour < wochenende.fruehestens_uhr:
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,  # blockiert alles andere
            aktiv=True,
            einschalten=None if kompressor_ein else False,
            grund=(
                f"Wochenende: Vor {wochenende.fruehestens_uhr} Uhr ({now.hour}:xx); "
                + (
                    "laufender Zyklus bleibt bis Mindestlaufzeit aktiv"
                    if kompressor_ein
                    else "Start gesperrt -> AUS"
                )
            ),
        )

    return RegelErgebnis(
        name="Wochenende",
        prioritaet=wochenende.prioritaet,
        aktiv=True,
        einschalten=None,  # Keine Aktion, andere Regeln dufen entscheiden
        grund=f"Wochenende: Ab {wochenende.fruehestens_uhr} Uhr erlaubt ({now.hour}:xx)",
    )


def evaluate_notfallschutz(
    nf_cfg: NotfallschutzConfig,
    temp_dict: Dict[str, Optional[float]],
) -> RegelErgebnis:
    """Notfallschutz (Prio 110): Reiner Schutzleiter fuer die
    Brauchwasser-Mindesttemperatur.

    Aus dem alten Komfort-Notfall ausgekoppelt. Greift ohne weitere Bedin-
    gungen vor ALLEN Sperren (Wochenende, Nachtsperre): Sinkt die Nutz-
    Wassertemperatur unter einschalten_bei_c, wird eingeschaltet. Im
    Normalbetrieb ist die Regel STUMM (einschalten=None) - sie blockt
    dadurch niemals andere Heizwuensche.
    """
    result = RegelErgebnis(
        name="Notfallschutz",
        prioritaet=nf_cfg.prioritaet,
        aktiv=nf_cfg.aktiv,
    )
    if not nf_cfg.aktiv:
        result.grund = "Notfallschutz inaktiv"
        return result

    # Konfigurierbare Strategie. "auto" bewahrt die bisherige sichere Reihenfolge;
    # "alle" verwendet den kältesten verfügbaren Boiler-Fühler.
    strategie = getattr(nf_cfg, "temperaturfuehler", "auto")
    if strategie == "alle":
        kandidaten = []
        for name in ("oben", "mittig", "unten"):
            wert = _parse_sensor(temp_dict, name)
            if wert is not None:
                kandidaten.append((wert, name))
        if not kandidaten:
            result.aktiv = False
            result.grund = "Keine Sensordaten fuer Notfallschutz verfuegbar"
            return result
        temp, sensor = min(kandidaten, key=lambda item: item[0])
    elif strategie == "auto":
        temp = None
        sensor = ""
        for name in ("oben", "mittig", "unten"):
            temp = _parse_sensor(temp_dict, name)
            if temp is not None:
                sensor = name
                break
    else:
        temp = _parse_sensor(temp_dict, strategie)
        sensor = strategie
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{sensor or strategie}' fuer Notfallschutz nicht verfuegbar"
        return result

    if temp <= nf_cfg.einschalten_bei_c:
        result.einschalten = True
        result.grund = (
            f"NOTFALLSCHUTZ: {sensor} {temp:.1f}C <= "
            f"{nf_cfg.einschalten_bei_c}C -> EIN"
        )
        return result

    result.einschalten = None
    result.grund = (
        f"Notfallschutz: {sensor} {temp:.1f}C > {nf_cfg.einschalten_bei_c}C (stumm)"
    )
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
    Komfort-Regel: Haelt zusaetzliche Waerme, sofern genug PV da ist.

    - Komfort: Einschalten wenn Temp <= Komfort-Schwelle UND genug PV
    - Ausschalten: Temp >= Ausschalt-Schwelle
    - Der reine Notfall (<=36C, nachts) steckt in der eigenstaendigen
      Regel "Notfallschutz" (Prio 110).
    """
    temp = _parse_sensor(temp_dict, "unten")
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)

    result = RegelErgebnis(name="Komfort", prioritaet=komfort.prioritaet, aktiv=True)

    if temp is None:
        result.aktiv = False
        result.grund = "Sensor 'unten' nicht verfuegbar"
        return result

    # Notfall (<=36C, auch nachts) ist in die eigenstaendige Regel
    # 'Notfallschutz' (Prio 110) ausgekoppelt - hier bleibt nur noch der
    # PV-abhaengige Komfort sowie die Ziel-Abschaltung.

    # Bei Nachtsperre: Kein Komfort-Heizen
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
    if (
        pv_leistung >= komfort.min_pv_fuer_komfort_watt
        and temp <= komfort.komfort_einschalten_bei_c
    ):
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
        grund="Auserhalb Zeitfenster",
    )

    if not zf.aktiv:
        result.grund = "Zeitfenster-Regel inaktiv"
        return result

    if not _is_zeitfenster_active(now_hour, zf.start_uhr, zf.ende_uhr):
        return result

    result.aktiv = True

    # PV-Check
    if zf.min_pv_watt > 0 and pv_leistung < zf.min_pv_watt:
        result.grund = (
            f"Zeitfenster aktiv, aber PV {pv_leistung:.0f}W < {zf.min_pv_watt}W"
        )
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
    feedin_watt: float = 0.0,
    soc: Optional[float] = None,
    bademodus_aktiv: bool = False,
    forecast_today_wh_qm: Optional[float] = None,
    fc_ratio: float = 1.0,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
) -> RegelErgebnis:
    """
    Abweichungs-Regel: haelt den Boiler auf Solltemperatur (Komfort-Boden).
    
    Regelfuehler ist `abw.temperaturfuehler` (Deployment: "unten"): Der untere
    Fuehler ist der kalteste Punkt und der Fruehindikator fuer "Boiler
    entladen" (Zapfung bringt Kaltwasser unten ein). "oben" bleibt durch die
    Schichtung lange warm und waere als Regelfuehler blind dafuer.

    Schutz vor der Schichtungs-Falle ("oben heiss, unten kalt"):
    Ist `oben >= schichtung_min_oben_c` (Default 42 C), entscheidet
    `schichtung_erlaube_start`:
      * false (Deployment) -> es wird NICHT geheizt, die Regel wartet
      * true  -> Warmstart mit Deckel: oben darf max. `schichtung_max_steig_k`
                 (Default 1 K) steigen
    ACHTUNG: Dieser Zweig liegt VOR dem Quellen-Gate - bei `true` kann also
    auch ohne PV/Batterie geheizt werden.

    Danach Quellen-Gate (nur wenn der Schichtungs-Zweig nicht gegriffen hat):
      * `quelle_warten` (Default true): EIN nur mit PV-Einspeisung oder voller
        Batterie, sonst wartet die Regel (stumm, kein AUS)
      * Tiefschutz: unter `solltemperatur_c - netz_notfall_offset_k` ist
        Netzstrom erlaubt, damit der Boiler nicht durchkuehlt
      * `pv_warten_*`: bei guter Tagesprognose vor `pv_warten_bis_uhr` auf die
        Sonne warten; unter `pv_warten_unten_min_c` gilt "echt kalt" -> sofort

    AUS erfolgt ueber den Regelfuehler: `soll - temp <= ausschalten_bei_abweichung_k`
    """
    result = RegelErgebnis(name="Abweichung", prioritaet=abw.prioritaet, aktiv=True)

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
    # AUSNAHME Bademodus: Der Nutzer moechte warmes Wasser, Nachtsperre
    # darf dann nicht blockieren.
    if nachtsperre and not bademodus_aktiv:
        result.aktiv = False
        result.grund = (
            f"Nachtsperre (kein Einschalten, {abw.temperaturfuehler}={temp:.1f}C)"
        )
        return result

    # Einschalten: Zu kalt
    if abweichung >= abw.einschalten_bei_abweichung_k:
        # PV-Warten-Overlay (Empfehlung 3.3): Bei sehr guter Tagesprognose
        # morgens NICHT mit Netzstrom vorheizen, sondern auf die erwartete
        # PV-Sonne warten. Steht vor dem Schichtungs-Check, damit auch der
        # "oben warm, unten kalt"-Warmstart nicht aus dem Netz bedient wird.
        # Schutz: Unter pv_warten_unten_min_c (echt kalt) wird sofort geheizt,
        # ab pv_warten_bis_uhr spielt die Regel wieder normal; ist die Sonne
        # zum Wartezeitpunkt schon aktiv, wird normal entschieden.
        if (
            getattr(abw, "pv_warten_aktiv", True)
            and now_hour < getattr(abw, "pv_warten_bis_uhr", 12)
            and temp >= getattr(abw, "pv_warten_unten_min_c", 20.0)
            and forecast_today_wh_qm is not None
            and _forecast_effektiv_wh_qm(forecast_today_wh_qm, fc_ratio)
            >= getattr(abw, "pv_warten_forecast_schwelle_wh_qm", 2500.0)
            and not _energiequelle_ok(
                feedin_watt,
                soc,
                getattr(abw, "pv_einspeisung_min_watt", 50.0),
                getattr(abw, "soc_min_prozent", 90.0),
                getattr(abw, "max_netzbezug_watt", -50.0),
                pv_acpower=pv_acpower,
                battery_power=battery_power,
                battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                solar_stale=solar_stale,
            )
        ):
            eff = _forecast_effektiv_wh_qm(forecast_today_wh_qm, fc_ratio)
            result.einschalten = None
            result.grund = (
                f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                f"heute gute PV-Prognose ({eff:.0f} Wh/m2, PV {feedin_watt:.0f}W) "
                f"-> warte auf PV (Netz ab {getattr(abw, 'pv_warten_bis_uhr', 12)}:00 "
                f"oder Notfall unter {getattr(abw, 'pv_warten_unten_min_c', 20.0):.0f}C)"
            )
            return result
        # 2-Zonen-Schichtungs-Check: Wenn der konfigurierte Fuehler nicht "oben" ist
        # (z.B. unten/mittig), pruefe ob oben noch warm genug ist.
        if abw.temperaturfuehler != "oben" and abw.schichtung_min_oben_c > 0:
            temp_oben = _parse_sensor(temp_dict, "oben")
            if temp_oben is not None and temp_oben >= abw.schichtung_min_oben_c:
                erlaube = getattr(abw, "schichtung_erlaube_start", False)
                steig = max(getattr(abw, "schichtung_max_steig_k", 1.0), 0.1)
                if erlaube:
                    quelle_ok, quelle_grund = _energiequelle_mit_grund(
                        feedin_watt,
                        soc,
                        getattr(abw, "pv_einspeisung_min_watt", 50.0),
                        getattr(abw, "soc_min_prozent", 90.0),
                        getattr(abw, "max_netzbezug_watt", -50.0),
                        pv_acpower=pv_acpower,
                        battery_power=battery_power,
                        battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                        solar_stale=solar_stale,
                    )
                    netz_erlaubt = getattr(
                        abw, "schichtung_netz_fallback_erlaubt", False
                    )
                    if not quelle_ok and not netz_erlaubt:
                        tief_grenze = abw.solltemperatur_c - max(
                            getattr(abw, "netz_notfall_offset_k", 8.0), 0.0
                        )
                        if temp > tief_grenze:
                            result.einschalten = None
                            result.grund = (
                                f"oben {temp_oben:.1f}C warm, Schichtungs-Start verlangt "
                                f"PV/Batterie: {quelle_grund}"
                            )
                            return result
                        # Tiefenschutz bleibt auch im Schichtungs-Warmstart
                        # fail-safe erhalten; nur unterhalb der Notfallgrenze.
                        result.einschalten = True
                        result.regel_dict = {
                            "schichtung_oben_max": temp_oben + steig,
                            "schichtung_oben_start": temp_oben,
                            "schichtung_max_steig_k": steig,
                            "schichtung_netz_fallback": True,
                        }
                        result.grund = (
                            f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                            f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                            f"oben {temp_oben:.1f}C warm, Tiefenschutz <= {tief_grenze:.1f}C "
                            f"-> EIN mit Netz; Obergrenze oben {temp_oben + steig:.1f}C"
                        )
                        return result
                    # Netzfallback ist nur durch die explizite Option aktiv.
                    result.einschalten = True
                    result.regel_dict = {
                        "schichtung_oben_max": temp_oben + steig,
                        "schichtung_oben_start": temp_oben,
                        "schichtung_max_steig_k": steig,
                        "schichtung_netz_fallback": not quelle_ok,
                    }
                    result.grund = (
                        f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                        f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                        f"oben {temp_oben:.1f}C warm, Schichtungs-Start "
                        f"({quelle_grund if quelle_ok else 'Netzfallback explizit erlaubt'}) "
                        f"-> EIN; Obergrenze oben {temp_oben + steig:.1f}C"
                    )
                    return result
                else:
                    result.einschalten = None
                    result.grund = (
                        f"Soll {abw.solltemperatur_c}C - {abw.temperaturfuehler} {temp:.1f}C = "
                        f"+{abweichung:.1f}K >= +{abw.einschalten_bei_abweichung_k}K, "
                        f"oben {temp_oben:.1f}C >= {abw.schichtung_min_oben_c:.1f}C -> "
                        f"kein Start (Schichtungsschutz, schichtung_erlaube_start=false)"
                    )
                    return result
        # Quellen-Gate mit Tiefenschutz: Im Normalfall auf PV/Batterie warten
        # statt mit Netzstrom zu heizen. Erst wenn der Fuehler unter
        # (Soll - netz_notfall_offset_k) faellt, erlaubt der Tiefschutz Netz.
        if getattr(abw, "quelle_warten", True):
            tief_grenze = abw.solltemperatur_c - max(
                getattr(abw, "netz_notfall_offset_k", 8.0), 0.0
            )
            if temp > tief_grenze and not _energiequelle_ok(
                feedin_watt,
                soc,
                getattr(abw, "pv_einspeisung_min_watt", 50.0),
                getattr(abw, "soc_min_prozent", 90.0),
                getattr(abw, "max_netzbezug_watt", -50.0),
                pv_acpower=pv_acpower,
                battery_power=battery_power,
                battery_min_watt=getattr(abw, "batterie_entladung_min_watt", 50.0),
                solar_stale=solar_stale,
            ):
                result.einschalten = None
                result.grund = (
                    f"Soll {abw.solltemperatur_c}C - oben "
                    f"{temp:.1f}C = +{abweichung:.1f}K >= "
                    f"+{abw.einschalten_bei_abweichung_k}K, aber keine Quelle "
                    f"(PV {feedin_watt:.0f}W) -> wartet auf PV/Batterie "
                    f"(Netz erst unter {tief_grenze:.1f}C)"
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


def _forecast_effektiv_wh_qm(forecast_today, fc_ratio: float = 1.0) -> float:
    """Normiert die Tages-Prognose auf Wh/m2 und wendet die Kalibrierung an.

    `state.solar.forecast_today` wird an der Integrationsgrenze bereits
    explizit in Wh/m² normalisiert. Der Parameter ist daher intern Wh/m².
    Keine Größenheuristik mehr: die Quelle bestimmt die Einheit.
    """
    if forecast_today is None:
        return 0.0
    try:
        wert = float(forecast_today)
    except (TypeError, ValueError):
        return 0.0
    # Die Integrationsgrenze liefert Wh/m². Keine heuristische Umrechnung.
    return wert * fc_ratio


def _energiequelle_ok(
    feedin_watt: float,
    soc: Optional[float],
    pv_min_watt: float,
    soc_min_prozent: float,
    max_netzkauf_watt: float,
    *,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    battery_min_watt: float = 50.0,
    solar_stale: bool = False,
) -> bool:
    """Gemeinsames Gate: PV oder echte Batterieentladung ohne Netzkauf."""
    return classify_energy_source(
        pv_acpower=pv_acpower,
        feedin_watt=feedin_watt,
        battery_discharge_watt=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=pv_min_watt,
        battery_min_watt=battery_min_watt,
        soc_min_prozent=soc_min_prozent,
        max_netzkauf_watt=max_netzkauf_watt,
    ).ist_erneuerbar


def _energiequelle_mit_grund(
    feedin_watt: float,
    soc: Optional[float],
    pv_min_watt: float,
    soc_min_prozent: float,
    max_netzkauf_watt: float,
    *,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    battery_min_watt: float = 50.0,
    solar_stale: bool = False,
) -> tuple:
    """Wie _energiequelle_ok, aber mit stabiler Quelle und lesbarem Grund."""
    status = classify_energy_source(
        pv_acpower=pv_acpower,
        feedin_watt=feedin_watt,
        battery_discharge_watt=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=pv_min_watt,
        battery_min_watt=battery_min_watt,
        soc_min_prozent=soc_min_prozent,
        max_netzkauf_watt=max_netzkauf_watt,
    )
    return status.ist_erneuerbar, f"{status.quelle.value}: {status.begruendung}"


def _mittagstief_stunden(
    current_time: float,
    ziel_uhr: float,
    surplus_profile: Dict[str, float],
    schwelle_w: float = 250.0,
) -> tuple:
    """Gelernte Surplus-schwache Stunden zwischen jetzt und Zielzeit.

    Das Profil kommt aus der LearningEngine (Netzeinspeisung je Stunde,
    nur bei ausgeschaltetem WP gesampelt = Haushaltsmuster pur). Stunden
    mit typisch < schwelle_w (Kochen etc.) zaehlen nur teilweise als
    Heizzeit -> CalcStart beginnt frueher und kann im Tief pausieren.
    Returns: (anzahl_tiefstunden, lesbares_label)
    """
    stunden = []
    h = int(current_time) + 1
    while h < ziel_uhr and (h - current_time) <= 10.0:
        wert = surplus_profile.get(str(h % 24))
        if wert is not None and wert < schwelle_w:
            stunden.append(h % 24)
        h += 1
    if not stunden:
        return 0.0, ""
    if stunden[-1] - stunden[0] == len(stunden) - 1:
        label = f"{stunden[0]}-{stunden[-1] + 1} Uhr"
    else:
        label = f"{len(stunden)} Std."
    return float(len(stunden)), label


def _forecast_quelle_ok(
    forecast_cfg: ForecastConfig,
    feedin_watt: float,
    soc: Optional[float],
    *,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
) -> tuple:
    """Energie-Quelle fuer das Vorheizen: PV-Direkt vor Batterie.

    Netz nur, wenn vorheiz_netz_erlaubt=True explizit freigegeben ist.
    Returns: (ok, beschreibung_der_quelle_bzw._des_grunds)
    """
    if getattr(forecast_cfg, "vorheiz_netz_erlaubt", False):
        return True, "Netz erlaubt (Konfig)"
    return _energiequelle_mit_grund(
        feedin_watt,
        soc,
        getattr(forecast_cfg, "pv_einspeisung_min_watt", 50.0),
        getattr(forecast_cfg, "soc_min_prozent", 90.0),
        getattr(forecast_cfg, "vorheiz_max_netzbezug_watt", -50.0),
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        battery_min_watt=getattr(
            forecast_cfg, "batterie_entladung_min_watt", 50.0
        ),
        solar_stale=solar_stale,
    )


def evaluate_forecast(
    forecast_cfg: ForecastConfig,
    temp_dict: Dict[str, Optional[float]],
    forecast_wh_qm: Optional[float],
    now_hour: int,
    nachtsperre_start: int = 19,
    nachtsperre_ende: int = 8,
    feedin_watt: float = 0.0,
    soc: Optional[float] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
) -> RegelErgebnis:
    """
    Prognose-Regel: Vorheizen bei schlechter Solar-Prognose, sparen bei guter.

    - Morgen bewölkt (Prognose < Niedrig-Schwelle): Heute vorheizen
    - Morgen sonnig (Prognose > Hoch-Schwelle): Heute sparen (nicht unnötig heizen)
    """
    result = RegelErgebnis(
        name="Forecast", prioritaet=forecast_cfg.prioritaet, aktiv=forecast_cfg.aktiv
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
                quelle_ok, quelle_grund = _forecast_quelle_ok(
                    forecast_cfg, feedin_watt, soc,
                    pv_acpower=pv_acpower,
                    battery_power=battery_power,
                    solar_stale=solar_stale,
                )
                if not quelle_ok:
                    # Keine akzeptierte Quelle (weder PV noch volle Batterie):
                    # Warten statt Netzstrom verbrauchen. Bewusst STUMM
                    # (kein AUS), damit niedriger priorisierte Regeln wie
                    # Abweichung weiterhin entscheiden koennen.
                    result.einschalten = None
                    result.grund = (
                        f"Forecast-Vorheiz wartet auf Quelle: PV "
                        f"{feedin_watt:.0f}W < "
                        f"{getattr(forecast_cfg, 'pv_einspeisung_min_watt', 50.0):.0f}W, "
                        f"{quelle_grund}"
                    )
                    return result
                result.einschalten = True
                result.grund = (
                    f"Forecast-Vorheiz: Morgen {forecast_wh_qm:.0f} Wh/qm <= "
                    f"{forecast_cfg.fc_schwelle_niedrig_wh:.0f} (schlecht), "
                    f"Temp {temp:.1f}C <= {forecast_cfg.t_vorheiz_ab_c}C -> EIN "
                    f"[{quelle_grund}]"
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
    fc_ratio: float = 1.0,
    forecast_today_wh_qm: Optional[float] = None,
) -> RegelErgebnis:
    """
    Adaptive-PV-Regel: PV-Schwelle passt sich dynamisch an.

    - Temperaturabhängig: Bei kaltem Boiler wird die Schwelle gesenkt
    - Prognoseabhängig: Bei schlechter Prognose wird die Schwelle gesenkt
    """
    result = RegelErgebnis(
        name="AdaptivePV", prioritaet=adaptive_cfg.prioritaet, aktiv=adaptive_cfg.aktiv
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
        result.grund = (
            f"AdaptivePV: {sensor_name} {temp:.1f}C >= {adaptive_cfg.tmax_c}C -> AUS"
        )
        return result

    # Hysterese: Nur einschalten wenn Temp unter Einschaltgrenze (tmax_c - 3K Default)
    # Sonst soll die PV-Regel mit ihrer Hysterese (42/48°C) entscheiden
    einschalten_bis_c = getattr(adaptive_cfg, "einschalten_bis_c", None)
    if einschalten_bis_c is None:
        einschalten_bis_c = adaptive_cfg.tmax_c - 3.0

    # Hysterese-Sparen (Empfehlung 3.2): Bei sehr guter HEUTE-Prognose wird die
    # Einschaltgrenze gesenkt -> die WP startet tiefer und laeuft laenger.
    sparen_k = float(getattr(adaptive_cfg, "sparen_hysterese_k", 0.0) or 0.0)
    if sparen_k > 0 and forecast_today_wh_qm is not None:
        fc_schwelle = float(
            getattr(adaptive_cfg, "hysterese_forecast_schwelle_wh_qm", 2500.0))
        if _forecast_effektiv_wh_qm(forecast_today_wh_qm, 1.0) >= fc_schwelle:
            einschalten_bis_c = einschalten_bis_c - sparen_k

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

    # Prognose-Anpassung (fc_ratio = gelernte Haus-Kalibrierung)
    prognose_eff = (
        forecast_wh_qm * fc_ratio
        if forecast_wh_qm is not None and fc_ratio != 1.0
        else forecast_wh_qm
    )
    if prognose_eff is not None:
        if prognose_eff >= adaptive_cfg.fc_schwelle_gut_wh:
            schwelle *= 1.5  # Sehr sonnig: höhere Schwelle = konservativer
        elif prognose_eff <= adaptive_cfg.fc_schwelle_schlecht_wh:
            schwelle *= 0.5  # Bewölkt: niedrige Schwelle = PV jetzt nutzen

    if pv_leistung >= schwelle:
        result.einschalten = True
        result.grund = (
            f"AdaptivePV: PV {pv_leistung:.0f}W >= {schwelle:.0f}W "
            f"(Basis {adaptive_cfg.base_threshold_watt:.0f}W, {sensor_name}={temp:.1f}C) -> EIN"
        )
        return result

    result.reason_code = "pv_unterbrechung"
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
    feedin_watt: float = 0.0,
    soc: Optional[float] = None,
    fc_ratio: float = 1.0,
    surplus_profile: Optional[Dict[str, float]] = None,
    recent_usage_events: Optional[List[Dict]] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    solar_stale: bool = False,
) -> RegelErgebnis:
    """
    Berechnete-Startzeit-Regel: Schaltet rechtzeitig vor der Zielzeit ein.

    Berechnet aus Temperaturdifferenz und Heizrate die benötigte Zeit.
    Wenn die verbleibende Zeit knapp wird -> einschalten.
    """
    result = RegelErgebnis(
        name="CalcStart", prioritaet=calc_cfg.prioritaet, aktiv=calc_cfg.aktiv
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
        ziel = (
            learned_target_hour
            if learned_target_hour is not None
            else float(calc_cfg.target_uhr)
        )
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
        result.grund = (
            f"CalcStart: Zieltemp {calc_cfg.tmax_c}C unten bereits erreicht -> AUS"
        )
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
    diff_oben = (
        max(0, calc_cfg.solltemperatur_c - temp_oben) if temp_oben is not None else 0
    )
    diff_gesamt = diff_unten + diff_mitte + diff_oben

    if diff_gesamt <= 0:
        result.grund = f"CalcStart: Soll {calc_cfg.solltemperatur_c}C bereits erreicht"
        return result

    # Gelernte Heizraten verwenden (falls vorhanden), sonst Config-Defaults
    heizrate_unten = (
        learned_heating_rate_unten
        if learned_heating_rate_unten is not None
        else calc_cfg.heizrate_unten_c_h
    )
    heizrate_gesamt = (
        learned_heating_rate_gesamt
        if learned_heating_rate_gesamt is not None
        else calc_cfg.heizrate_gesamt_c_h
    )
    ziel_uhr = (
        learned_target_hour
        if learned_target_hour is not None
        else float(calc_cfg.target_uhr)
    )

    # Benoetigte Heizzeit
    hours_needed = diff_unten / max(heizrate_unten, 0.1)
    hours_needed_mitte = diff_mitte / max(heizrate_gesamt, 0.1)
    hours_needed = max(hours_needed, hours_needed_mitte)

    # === Multisensor-Zapfungsdaten einbeziehen (Punkt 4) ===
    # Temperaturverlust aus erkannter Zapfung abziehen, damit der
    # CalcStart die tatsaechliche Startzeit korrigeriert und die
    # Zapf-Garantie waerert.
    usage_drop_gesamt_k = 0.0
    if recent_usage_events:
        # Letzte Zapfung nehmen und Temperaturverlust als "bereits verlorene
        # Energie" interpretieren -> reduziert effektive Heizzeit
        # (die WP muss den Verlust nicht mehr kompensieren, er ist
        #  already passiert; der Schicht-Temperaturabfall muss aber
        #  innerhalb des Zeitfensters ausgeglichen werden, daher
        #  begrenzen wir den Effekt auf max. 50% des Abfalls)
        neueste = recent_usage_events[-1]
        drop = neueste.get("drop_gesamt_k", 0.0)
        usage_drop_gesamt_k = round(drop * 0.5, 2)
        if usage_drop_gesamt_k > 0:
            hours_needed = max(
                0.0, hours_needed - usage_drop_gesamt_k / max(heizrate_unten, 0.1)
            )
            logging.debug(
                f"CalcStart: Nutzungsevent {neueste.get('timestamp','')} "
                f"drop {drop:.1f}K -> Stundenbedarf -{usage_drop_gesamt_k:.2f}h "
                f"(neu {hours_needed:.2f}h)"
            )

    time_left = ziel_uhr - current_time

    # === Saisonale + Prognose-basierte Puffer-Berechnung ===
    # Saison erkennen (0=Winter, 1=Sommer)
    # Wir nutzen now_hour/min + wissen nicht direkt den Monat, aber wir
    # kriegen ihn ueber das now datetime objekt... da wir nur now_hour haben,
    # nutzen wir die Config-Puffer direkt mit Prognose-Anpassung.

    # Basis-Puffer aus Config
    # Prognose-Anpassung: Heute viel PV erwartet? -> laenger warten.
    # fc_ratio kalibriert die Prognose am gemessenen Netzeinschuss der
    # Vergangenheit (Haus-spezifischer Langfehler des Forecast-Dienstes).
    prognose_eff = forecast_wh_qm
    kalibrier_label = ""
    if forecast_wh_qm is not None and fc_ratio != 1.0:
        prognose_eff = forecast_wh_qm * fc_ratio
        kalibrier_label = f", Kalibrierung x{fc_ratio:.2f}"
    pv_faktor = 1.0
    if prognose_eff is not None:
        if prognose_eff >= 3000:  # Sehr sonnig
            pv_faktor = 2.0
            pv_label = f"sehr sonnig{kalibrier_label}"
        elif prognose_eff >= 1500:  # Sonnig
            pv_faktor = 1.5
            pv_label = f"sonnig{kalibrier_label}"
        elif prognose_eff <= 500:  # Bewoelkt
            pv_faktor = 0.5
            pv_label = f"bewoelkt{kalibrier_label}"
        else:
            pv_faktor = 1.0
            pv_label = f"{prognose_eff:.0f} Wh/qm{kalibrier_label}"
    else:
        pv_label = "keine Prognose"

    # Verbrauchsbewusstsein: Gelernte Stunden mit wenig Netzeinschuss
    # (Mittags Kochen etc.) zaehlen nur zu 75% als Heizzeit -> frueherer
    # Start, damit waehrend des Tiefs pausiert werden kann.
    dip_h, dip_label = (
        _mittagstief_stunden(current_time, ziel_uhr, surplus_profile)
        if surplus_profile
        else (0.0, "")
    )
    if dip_h:
        hours_needed = hours_needed + dip_h * 0.75

    buffer_hours = time_left - hours_needed
    effektiver_puffer = buffer_hours * pv_faktor

    quelle_ok, quelle_grund = _energiequelle_mit_grund(
        feedin_watt,
        soc,
        getattr(calc_cfg, "pv_einspeisung_min_watt", 50.0),
        getattr(calc_cfg, "soc_min_prozent", 90.0),
        getattr(calc_cfg, "max_netzbezug_watt", -50.0),
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        battery_min_watt=getattr(calc_cfg, "batterie_entladung_min_watt", 50.0),
        solar_stale=solar_stale,
    )

    if buffer_hours < 0:
        # Bereits ueber Zielzeit oder zu spaet -> sofort heizen.
        # Netzfallback ist nur erlaubt, wenn explizit aktiviert und ab der
        # konfigurierten Uhrzeit freigegeben; sonst bleibt die Zapf-Garantie
        # sichtbar, aber die Regel wartet auf eine freigegebene Quelle.
        notfall_grenze = float(getattr(calc_cfg, "notfall_unten_c", 32.0))
        netzfallback_ok = (
            bool(getattr(calc_cfg, "netz_fallback_erlaubt", False))
            and now_hour >= int(getattr(calc_cfg, "netz_fallback_ab_uhr", 12))
        )
        if not quelle_ok and not netzfallback_ok and temp_unten > notfall_grenze:
            result.einschalten = None
            result.grund = (
                f"CalcStart: ZU SPAET ({time_left:.1f}h < {hours_needed:.1f}h), "
                f"aber Netzfallback ist deaktiviert; warte auf PV/Batterie "
                f"({quelle_grund})"
            )
            return result
        result.einschalten = True
        result.grund = (
            f"CalcStart: ZU SPAET! Zeitablauf ({time_left:.1f}h < {hours_needed:.1f}h) "
            f"-> EIN (Notfall)"
        )
        return result

    if quelle_ok and effektiver_puffer < 0.5:
        # Weniger als 30min effektiven Puffer + guenstige Quelle -> heizen
        result.einschalten = True
        result.grund = (
            f"CalcStart: Nur {effektiver_puffer:.1f}h Puffer (PV={pv_label}, "
            f"brauche {hours_needed:.1f}h bis {ziel_uhr:.0f}:00) -> EIN "
            f"[{quelle_grund}]"
        )
        return result

    if buffer_hours <= getattr(calc_cfg, "spaetstart_puffer_h", 0.5):
        # Errechneter SPAETEST-START: Netzfallback nur explizit und zeitlich
        # freigegeben; sonst wartet die Regel auf PV/Batterie.
        netzfallback_ok = (
            bool(getattr(calc_cfg, "netz_fallback_erlaubt", False))
            and now_hour >= int(getattr(calc_cfg, "netz_fallback_ab_uhr", 12))
        )
        if not quelle_ok and not netzfallback_ok:
            result.einschalten = None
            result.grund = (
                f"CalcStart: SPAETEST-START ({buffer_hours:.1f}h Restpuffer), "
                f"aber Netzfallback ist deaktiviert; warte auf PV/Batterie "
                f"({quelle_grund})"
            )
            return result
        result.einschalten = True
        dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
        result.grund = (
            f"CalcStart: SPAETEST-START ({buffer_hours:.1f}h Restpuffer, "
            f"brauche {hours_needed:.1f}h bis {ziel_uhr:.0f}:00{dip_txt}) -> EIN "
            f"(Zapf-Garantie; Quelle: {quelle_grund})"
        )
        return result

    # Genug Puffer -> warten; ohne Quelle explizit benennen, worauf gewartet wird
    if not quelle_ok:
        dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
        result.einschalten = None
        result.grund = (
            f"CalcStart: {effektiver_puffer:.1f}h Puffer reicht, warte auf "
            f"PV/Batterie ({quelle_grund}; brauche {hours_needed:.1f}h bis "
            f"{ziel_uhr:.0f}:00{dip_txt})"
        )
        return result
    result.einschalten = None
    dip_txt = f" | Mittagstief {dip_label}" if dip_h else ""
    result.grund = (
        f"CalcStart: {effektiver_puffer:.1f}h Puffer reicht (PV={pv_label}, "
        f"brauche {hours_needed:.1f}h bis {ziel_uhr:.0f}:00{dip_txt}) -> warte auf PV"
    )
    return result


def _fmt_quelle(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError, OverflowError):
        return "n/a"


def _legionellen_quelle_status(cfg, pv_leistung, battery_power, soc, solar_stale, pv_acpower=None):
    """Prüft PV/Batterie anhand derselben zentralen Quellenlogik."""
    status = classify_energy_source(
        pv_acpower=pv_acpower,
        feedin_watt=pv_leistung,
        battery_discharge_watt=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=float(getattr(cfg, "pv_start_min_watt", 500.0)),
        battery_min_watt=float(getattr(cfg, "batterie_start_min_watt", 50.0)),
        soc_min_prozent=float(getattr(cfg, "batterie_start_min_soc_prozent", 90.0)),
        max_netzkauf_watt=float(getattr(cfg, "max_netzbezug_watt", -50.0)),
    )
    if status.ist_erneuerbar:
        return True, status.quelle.value
    return False, f"{status.quelle.value}: {status.begruendung}"


def evaluate_legionellen(
    legionellen_cfg,
    temp_dict,
    now,
    forecast_today_wh_qm=None,
    forecast_wh_qm=None,
    forecast_day2_wh_qm=None,
    legionellen_aktiv=False,
    legionellen_last_done=None,
    legionellen_started_at=None,
    kompressor_ein=False,
    legionellen_target_reached_at=None,
    wochenende_cfg=None,
    legionellen_planned_tag=None,
    legionellen_planned_date=None,
    pv_leistung: Optional[float] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    soc: Optional[float] = None,
    solar_stale: bool = False,
):
    """Bewertet, ob die Legionellenprophylaxe durchgefuehrt werden soll."""
    result = RegelErgebnis(
            name="Legionellen",
            prioritaet=legionellen_cfg.prioritaet,
            aktiv=True,
        )
    if not legionellen_cfg.aktiv:
        result.aktiv = False
        result.grund = "Legionellenprophylaxe deaktiviert"
        return result
    t_unten = temp_dict.get("unten")
    if t_unten is None:
        result.aktiv = False
        result.grund = "Sensordaten nicht verfuegbar"
        return result
    if legionellen_aktiv:
        if t_unten >= legionellen_cfg.target_temp_c:
            if legionellen_started_at is None:
                result.einschalten = None
                result.grund = "Legionellen wartet: Startzeit fehlt fuer die Probezeit"
                return result
            try:
                gehalten_seit = now - (legionellen_target_reached_at or legionellen_started_at)
            except (TypeError, ValueError):
                gehalten_seit = timedelta(0)
            if legionellen_target_reached_at is None:
                result.einschalten = True
                result.grund = "Legionellen: Zieltemperatur erreicht; Probezeit startet"
                return result
            # Ziel erreicht, aber die konfigurierte Probezeit muss noch ablaufen.
            if gehalten_seit < timedelta(minutes=int(legionellen_cfg.probezeit_minuten)):
                result.einschalten = True
                verbleib = max(0, int((timedelta(minutes=int(legionellen_cfg.probezeit_minuten)) - gehalten_seit).total_seconds() // 60) + 1)
                result.grund = (
                    f"Legionellen: Zieltemperatur erreicht; Probezeit laeuft noch "
                    f"(ca. {verbleib} min)"
                )
            else:
                result.einschalten = False
                result.grund = (
                    f"Legionellen: unten {t_unten:.1f}C >= {legionellen_cfg.target_temp_c:.0f}C "
                    f"und Probezeit {legionellen_cfg.probezeit_minuten} min abgeschlossen -> AUS"
                )
            return result
        else:
            result.einschalten = True
            result.grund = (
                f"Legionellen aktiv: {t_unten:.1f}C < {legionellen_cfg.target_temp_c:.0f}C, "
                f"heize weiter"
            )
            return result
    if legionellen_last_done is not None:
        letzte_kw = legionellen_last_done.isocalendar()[1]
        aktuelle_kw = now.isocalendar()[1]
        if letzte_kw == aktuelle_kw:
            result.aktiv = False
            result.grund = f"Bereits in KW {aktuelle_kw} durchgefuehrt"
            return result
    start_h = int(legionellen_cfg.start_uhr)
    start_m = int((legionellen_cfg.start_uhr - start_h) * 60 + 0.5)
    is_weekend = _is_weekend(now)
    fruehestens = (
        getattr(wochenende_cfg, "fruehestens_uhr", None)
        if wochenende_cfg is not None
        else None
    )
    heute_wochentag = now.weekday()  # 0=Mo .. 6=So

    # --- Wochentags-Gate (Nutzeranforderung) ---
    # Start nur an einem der erlaubten Tage (bevorzugter_tag..letzter_tag,
    # z.B. Freitag..Sonntag). Liegt bereits ein geplanter bester Forecast-Tag
    # vor, wird nur noch an EXAKT diesem Tag gestartet. An allen anderen Tagen
    # bleibt die Regel stumm/inaktiv (kein falscher Start am Mittwoch etc.).
    erlaubt_tag = range(
        int(legionellen_cfg.bevorzugter_tag),
        int(legionellen_cfg.letzter_tag) + 1,
    )
    if legionellen_planned_date is not None and now.date() < legionellen_planned_date:
        result.aktiv = True
        result.einschalten = None
        result.grund = (
            f"Legionellen: geplanter Termin {legionellen_planned_date} liegt in der Zukunft; "
            "warte auf den Plan"
        )
        return result
    if legionellen_planned_date is not None and now.date() > legionellen_planned_date:
        result.aktiv = True
        result.einschalten = None
        result.grund = (
            f"Legionellen: geplanter Termin {legionellen_planned_date} ist verstrichen; "
            "Forecast-Plan verwerfen"
        )
        return result
    if legionellen_planned_tag is not None:
        if heute_wochentag != int(legionellen_planned_tag):
            result.aktiv = True
            result.einschalten = None
            result.grund = (
                f"Legionellen: Start erst am geplanten Tag "
                f"({legionellen_planned_tag}) - heute ist "
                f"{_wochentag_name(heute_wochentag)}"
            )
            return result
    elif heute_wochentag not in erlaubt_tag:
        result.aktiv = True
        result.einschalten = None
        result.grund = (
            f"Legionellen: Nur Start an {_wochentag_name(int(legionellen_cfg.bevorzugter_tag))}"
            f"-{_wochentag_name(int(legionellen_cfg.letzter_tag))} "
            f"(heute: {_wochentag_name(heute_wochentag)})"
        )
        return result

    # Prioritaeten-Kaskade (Legionellen=90 < Wochenende=100): Am Wochenende
    # blockt die Prio-100-Wochenende-Sperre Starts vor fruehestens_uhr
    # (z.B. 09:00). Das Startfenster wird flexibel gehalten, damit die Fahrt
    # direkt nach der Sperre nachgeholt wird.
    if is_weekend and fruehestens is not None:
        fruehster_start = int(fruehestens)
        if now.hour < fruehster_start:
            if now.hour >= start_h:
                # Fahrt ist eigentlich faellig, wird aber bis zur Freigabe
                # von der Wochenende-Sperre gehalten. Fenster offen lassen,
                # damit um fruehster_start sofort gestartet wird.
                result.einschalten = None
                result.grund = (
                    f"Legionellen: Wochenende-Sperre blockt bis {fruehster_start:g}:00; "
                    f"PV-/Batterie-Quelle und Start werden danach erneut geprueft "
                    f"(unten {t_unten:.1f}C)"
                )
            else:
                result.aktiv = False
                result.grund = (
                    f"Nicht zur geplanten Startzeit {legionellen_cfg.start_uhr:g}:00"
                )
            return result
        # Nach der Wochenende-Sperre: Nachhol-Fenster bis spaeteste_start_uhr
        spatest_hr = int(getattr(legionellen_cfg, "spaeteste_start_uhr", 16))
        if now.hour > spatest_hr:
            result.aktiv = False
            result.grund = f"Startfenster abgelaufen (spaeteste {spatest_hr:g}:00)"
            return result
        if kompressor_ein:
            result.einschalten = None
            result.grund = "Kompressor laeuft bereits, warte auf Abschluss"
            return result
        quelle_ok, quelle_text = _legionellen_quelle_status(
            legionellen_cfg, pv_leistung, battery_power, soc, solar_stale, pv_acpower
        )
        if not quelle_ok:
            result.einschalten = None
            result.reason_code = "waiting_source"
            result.grund = f"Legionellen wartet auf PV/Batterie: {quelle_text}"
            return result
        result.einschalten = True
        result.grund = (
            f"Legionellenprophylaxe faellig (Wochenende-Nachholung): Starte "
            f"mit {quelle_text} auf {legionellen_cfg.target_temp_c:.0f}C "
            f"(unten {t_unten:.1f}C)"
        )
        return result

    # Werktag: exakte Startstunde
    if now.hour != start_h or now.minute < start_m:
        result.aktiv = False
        result.grund = f"Nicht zur geplanten Startzeit {legionellen_cfg.start_uhr:g}:00"
        return result
    if kompressor_ein:
        result.einschalten = None
        result.grund = "Kompressor laeuft bereits, warte auf Abschluss"
        return result
    quelle_ok, quelle_text = _legionellen_quelle_status(
        legionellen_cfg, pv_leistung, battery_power, soc, solar_stale, pv_acpower
    )
    if not quelle_ok:
        result.einschalten = None
        result.reason_code = "waiting_source"
        result.grund = f"Legionellen wartet auf PV/Batterie: {quelle_text}"
        return result
    quelle = quelle_text
    result.einschalten = True
    result.grund = (
        f"Legionellenprophylaxe faellig: Starte mit {quelle} auf "
        f"{legionellen_cfg.target_temp_c:.0f}C (unten {t_unten:.1f}C)"
    )
    return result


def _wochentag_name(tag: int) -> str:
    """Gibt den deutschen Wochentagsnamen zurueck (0=Montag..6=Sonntag)."""
    namen = [
        "Montag",
        "Dienstag",
        "Mittwoch",
        "Donnerstag",
        "Freitag",
        "Samstag",
        "Sonntag",
    ]
    if 0 <= tag < 7:
        return namen[tag]
    return f"Unbekannt ({tag})"


def _marke_regel_ursache(result, code: str) -> RegelErgebnis:
    """Setzt einen stabilen Grundcode ohne Änderung des Lesetexts."""
    result.reason_code = code
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
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    learned_evening_window: Optional[Tuple[float, float]] = None,
    learned_morning_window: Optional[Tuple[float, float]] = None,
    solar_stale: bool = False,
    learned_heating_rate_unten: Optional[float] = None,
    learned_heating_rate_gesamt: Optional[float] = None,
    learned_target_hour: Optional[float] = None,
    forecast_hourly_wh: Optional[Dict[int, float]] = None,
    fc_ratio: float = 1.0,
    surplus_profile: Optional[Dict[str, float]] = None,
    recent_usage_events: Optional[List[Dict]] = None,
    bademodus_aktiv: bool = False,
    legionellen_aktiv: bool = False,
    legionellen_last_done=None,
    legionellen_started_at=None,
    legionellen_target_reached_at=None,
    forecast_day2_wh_qm: Optional[float] = None,
    wochenende_cfg=None,
    legionellen_planned_tag=None,
    legionellen_planned_date=None,
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

    # -1. Notfallschutz (Prio 110): hoechste Regel, reiner Schutzleiter.
    #     Greift ohne weitere Bedingungen vor allen Sperren (Wochenende,
    #     Nachtsperre) - im Normalbetrieb stumm.
    ergebnis = evaluate_notfallschutz(config.notfallschutz, temp_dict)
    ergebnisse.append(ergebnis)

    # 0. Wochenende-Regel (blockiert Einschalten am Wochenende vor fruehestens_uhr)
    ergebnis = evaluate_wochenende(config.wochenende, now, kompressor_ein)
    ergebnisse.append(ergebnis)

    # 1. Einspeise-Begrenzung (PV-Shaping am Netzlimit, hoechste Heizen-Prioritaet)
    ergebnis = evaluate_einspeisung(
        config.einspeisung,
        temp_dict,
        pv_leistung,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
    )
    ergebnisse.append(ergebnis)

    # 1a. PV-Regeln bewerten (PV-Exklusivitaet: PV_unten/PV_mitte sind reines
    #     Backup bei fehlendem Forecast. Sobald Forecast-Daten da sind,
    #     steuert ausschliesslich AdaptivePV.)
    pv_backup_aktiv = not (
        config.adaptive_pv.aktiv
        and getattr(config.adaptive_pv, "exklusiv_mit_forecast", False)
        and forecast_wh_qm is not None
    )
    for pv_regel in config.pv_regeln:
        if not pv_backup_aktiv:
            ergebnisse.append(
                RegelErgebnis(
                    name=pv_regel.name,
                    prioritaet=pv_regel.prioritaet,
                    aktiv=False,
                    einschalten=None,
                    grund="PV-Exklusivitaet: Forecast vorhanden, AdaptivePV steuert",
                )
            )
            continue
        ergebnis = evaluate_pv_regel(
            pv_regel,
            temp_dict,
            pv_leistung,
            kompressor_ein,
            now_hour,
            nachtsperre_start,
            nachtsperre_ende,
        )
        ergebnisse.append(ergebnis)

    # 2. Komfort-Regel
    ergebnis = evaluate_komfort(
        config.komfort,
        temp_dict,
        pv_leistung,
        nachtsperre_start,
        nachtsperre_ende,
        now_hour,
    )
    ergebnisse.append(ergebnis)

    # 2b. Mindest-Temperatur-Garantien (jeder Eintrag ein Ergebnis)
    for min_ergebnis in evaluate_mindesttemp(
        config.mindest_temp,
        temp_dict,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        learned_evening_window=learned_evening_window,
        learned_morning_window=learned_morning_window,
    ):
        ergebnisse.append(min_ergebnis)

    # 2c. Batterie-Regel (PV-Direkt > Batterie > Netz, mit dynamischer Reserve)
    ergebnis = evaluate_batterie(
        config.batterie,
        temp_dict,
        pv_leistung,
        soc,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        forecast_wh_qm=forecast_wh_qm,
        battery_power=battery_power,
    )
    ergebnisse.append(ergebnis)
    # 3. Zeitfenster-Regel
    ergebnis = evaluate_zeitfenster(
        config.zeitfenster, temp_dict, pv_leistung, now_hour
    )
    ergebnisse.append(ergebnis)

    # 4. Abweichungs-Regel
    ergebnis = evaluate_abweichung(
        config.abweichung,
        temp_dict,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        feedin_watt=pv_leistung,
        soc=soc,
        bademodus_aktiv=bademodus_aktiv,
        forecast_today_wh_qm=forecast_today_wh_qm,
        fc_ratio=fc_ratio,
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        solar_stale=solar_stale,
    )
    ergebnisse.append(ergebnis)

    # 5. Forecast-Regel (Prognose-basiert vorheizen/sparen)
    ergebnis = evaluate_forecast(
        config.forecast,
        temp_dict,
        forecast_wh_qm,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        feedin_watt=pv_leistung,
        soc=soc,
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        solar_stale=solar_stale,
    )
    ergebnisse.append(ergebnis)

    # 6. Adaptive-PV-Regel (dynamische PV-Schwelle)
    ergebnis = evaluate_adaptive_pv(
        config.adaptive_pv,
        temp_dict,
        pv_leistung,
        forecast_wh_qm,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        fc_ratio=fc_ratio,
        forecast_today_wh_qm=forecast_today_wh_qm,
    )
    ergebnisse.append(ergebnis)

    # 7. Calculated-Start-Regel (optimierter Startzeitpunkt)
    ergebnis = evaluate_calculated_start(
        config.calculated_start,
        temp_dict,
        now_hour,
        now.minute,
        nachtsperre_start,
        nachtsperre_ende,
        forecast_wh_qm=forecast_today_wh_qm,
        learned_heating_rate_unten=learned_heating_rate_unten,
        learned_heating_rate_gesamt=learned_heating_rate_gesamt,
        learned_target_hour=learned_target_hour,
        feedin_watt=pv_leistung,
        soc=soc,
        fc_ratio=fc_ratio,
        surplus_profile=surplus_profile,
        recent_usage_events=recent_usage_events,
        pv_acpower=pv_acpower,
        battery_power=battery_power,
        solar_stale=solar_stale,
    )
    ergebnisse.append(ergebnis)

    # 8. Legionellenprophylaxe-Regel
    ergebnis = evaluate_legionellen(
        config.legionellen,
        temp_dict,
        now,
        forecast_today_wh_qm=forecast_today_wh_qm,
        forecast_wh_qm=forecast_wh_qm,
        forecast_day2_wh_qm=forecast_day2_wh_qm,
        legionellen_aktiv=legionellen_aktiv,
        legionellen_last_done=legionellen_last_done,
        legionellen_started_at=legionellen_started_at,
        kompressor_ein=kompressor_ein,
        legionellen_target_reached_at=legionellen_target_reached_at,
        pv_acpower=pv_acpower,
        wochenende_cfg=config.wochenende,
        legionellen_planned_tag=legionellen_planned_tag,
        legionellen_planned_date=legionellen_planned_date,
        pv_leistung=pv_leistung,
        battery_power=battery_power,
        soc=soc,
        solar_stale=solar_stale,
    )
    ergebnisse.append(ergebnis)

    # Gewinner bestimmen: Höchste priorisierte Regel mit klarer Entscheidung.
    # Legionellen-Wartefälle (None) bleiben sichtbar und können einen anderen
    # aktiven Regler nicht fälschlich als effektive Hardwarequelle überschreiben.
    aktive_regeln = [e for e in ergebnisse if e.aktiv and e.einschalten is not None]
    gewinner = max(
        aktive_regeln,
        key=lambda e: e.prioritaet,
        default=None,
    )

    if not aktive_regeln:
        # Keine aktive Regel mit klarer EIN/AUS-Entscheidung.
        return None, ergebnisse

    # Solar-Daten veraltet: PV-/Batterie-/Prognose-Regeln duerfen nicht
    # auf eingefrorenen Werten entscheiden. Garantien (MinTemp, Komfort)
    # und Abweichung (Netzstrom) bleiben bewusst aktiv.
    global _last_stale_warning
    if solar_stale:
        _stale_namen = {
            "Einspeisung",
            "CalcStart",
            "Batterie",
            "AdaptivePV",
            "Zeitfenster",
            "Forecast",
        }
        for e in ergebnisse:
            if (e.name in _stale_namen or e.name.startswith("PV_")) and (
                e.aktiv or e.einschalten is not None
            ):
                e.aktiv = False
                e.einschalten = None
                e.reason_code = "data_stale"
                e.grund = "Solar-Daten veraltet -> Regel pausiert"
        jetzt = now
        if (
            _last_stale_warning is None
            or not isinstance(_last_stale_warning, datetime)
            or (to_naive(jetzt) - to_naive(_last_stale_warning))
            >= timedelta(minutes=STALE_LOG_INTERVAL_MIN)
        ):
            logging.warning(
                "Solar-Daten veraltet: PV/Batterie/Einspeisung/"
                "CalcStart/Forecast/Zeitfenster pausiert"
            )
            _last_stale_warning = jetzt
    else:
        # Nach Rueckkehr frischer Daten darf ein neues Stale-Ereignis sofort loggen.
        _last_stale_warning = None

    # Nach der Stale-Pausierung die Kandidatenliste neu aufbauen. Die zuvor
    # gebaute Liste enthaelt dieselben Objekte, wurde aber vor der Mutation
    # gefiltert und wuerde sonst eine pausierte Solarregel weiter gewaehlen.
    aktive_regeln = [
        e for e in aktive_regeln if e.aktiv and e.einschalten is not None
    ]
    if not aktive_regeln:
        return None, ergebnisse

    # Nach Prioritaet sortieren (hoeher zuerst)
    aktive_regeln.sort(key=lambda e: e.prioritaet, reverse=True)
    gewinner = aktive_regeln[0]

    # CalcStart/AdaptivePV Koordinations-Logging (gedrosselt auf 5 Min)
    if (
        gewinner.name == "AdaptivePV"
        and any(r.name == "CalcStart" and r.aktiv for r in ergebnisse)
        and not any(r.name == "CalcStart" and r.einschalten is True for r in ergebnisse)
    ):
        global _last_calcstart_log
        if _last_calcstart_log is None or (now - _last_calcstart_log) > timedelta(minutes=5):
            _last_calcstart_log = now
            logging.debug(
                "CalcStart wartet auf besseres PV-Angebot -> AdaptivePV übernimmt."
            )

    # Der Notfallschutz (Prio 110) greift ohne Workaround vor allen Sperren -
    # ein manueller Override ist nicht mehr noetig (frueher: Komfort-NOTFALL
    # ueberschrieb die Wochenende-Prio-100-Sperre per Spezialfall).

    # Bewertung wird nur alle 5 Minuten in priority_control_logic.py geloggt (INFO, throttelt)
    # Hier nur minimales DEBUG, nicht bei jedem Durchlauf

    return gewinner, ergebnisse


def formatiere_ergebnisse(ergebnisse: List[RegelErgebnis]) -> str:
    """Formatiert alle Ergebnisse fuer Logging/Anzeige."""
    lines = []
    for e in sorted(ergebnisse, key=lambda x: x.prioritaet, reverse=True):
        status = (
            "EIN"
            if e.einschalten is True
            else ("AUS" if e.einschalten is False else "---")
        )
        aktive = "" if e.aktiv else " [INAKTIV]"
        lines.append(f"  [{e.prioritaet:3d}] {status} {e.name}{aktive}: {e.grund}")
    return "\n".join(lines)






