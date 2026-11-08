"""Komfort-Regel (PV-abhaengige Zusatzwaerme).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import KomfortConfig
from regel_helpers import _parse_sensor, _is_nachtsperre


def evaluate_komfort(
    komfort: KomfortConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    now_hour: int,
) -> RegelErgebnis:
    temp = _parse_sensor(temp_dict, "unten")
    nachtsperre = _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende)

    result = RegelErgebnis(name="Komfort", prioritaet=komfort.prioritaet, aktiv=True)

    if not getattr(komfort, "aktiv", True):
        result.aktiv = False
        result.grund = "Komfort-Regel abgeschaltet (aktiv=false)"
        return result

    if temp is None:
        result.aktiv = False
        result.grund = "Sensor 'unten' nicht verfuegbar"
        return result

    if nachtsperre:
        result.aktiv = False
        result.grund = "Nachtsperre (kein Komfort-Heizen)"
        return result

    if temp >= komfort.ausschalten_bei_c:
        result.einschalten = False
        result.grund = f"Komfort AUS: unten {temp:.1f}C >= {komfort.ausschalten_bei_c}C"
        return result

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