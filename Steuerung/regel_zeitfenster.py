"""Zeitfenster-Regel (Heizen zu festen Uhrzeiten).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import ZeitfensterConfig
from regel_helpers import _parse_sensor, _is_zeitfenster_active


def evaluate_zeitfenster(
    zf: ZeitfensterConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    now_hour: int,
) -> RegelErgebnis:
    result = RegelErgebnis(
        name="Zeitfenster",
        prioritaet=zf.prioritaet,
        aktiv=False,
        grund="Ausserhalb Zeitfenster",
    )

    if not zf.aktiv:
        result.grund = "Zeitfenster-Regel inaktiv"
        return result

    if not _is_zeitfenster_active(now_hour, zf.start_uhr, zf.ende_uhr):
        return result

    result.aktiv = True

    if zf.min_pv_watt > 0 and pv_leistung < zf.min_pv_watt:
        result.grund = (
            f"Zeitfenster aktiv, aber PV {pv_leistung:.0f}W < {zf.min_pv_watt}W"
        )
        result.einschalten = None
        return result

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