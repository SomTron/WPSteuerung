"""
PV-Regel (PV-Shaping mit Hysterese).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import PVRegel
from regel_helpers import _parse_sensor, _is_nachtsperre

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
    # Bewusst gesetzt: der Text enthaelt weder ' < ' noch 'keine Aktion'
    # und wuerde sonst als 'no_action' abgeleitet. Genau an diesem Code
    # erkennt die PV-Weiterlauf-Logik, dass der Lauf weiterlaeuft.
    result.set_reason_code("pv_unterbrechung")
    result.grund = (
        f"Keine Bedingung erfuellt (PV={pv_leistung:.0f}W, {sensor_name}={temp:.1f}C)"
    )
    return result


