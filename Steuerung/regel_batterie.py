"""Batterie-Regel (Heizen mit Hausbatterie statt Netzstrom).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import BatterieConfig
from energy_source import batterie_entladung_watt
from regel_helpers import _is_nachtsperre, _sensor_oder_none


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
    temp = _sensor_oder_none(temp_dict, batt_cfg.temperaturfuehler, result)
    if temp is None:
        return result
    if temp >= batt_cfg.ausschalten_bei_c:
        result.einschalten = False
        result.grund = (
            f"Batterie: {batt_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{batt_cfg.ausschalten_bei_c}C -> AUS"
        )
        return result
    entlastung_ab_wh = float(getattr(batt_cfg, "entlastung_ab_wh", 2000.0) or 0.0)
    eff_min_soc = batt_cfg.min_soc_prozent
    if forecast_wh_qm is not None and entlastung_ab_wh > 0 and forecast_wh_qm >= entlastung_ab_wh:
        entlastung = min(
            getattr(batt_cfg, "entlastung_max_prozent", 15.0),
            eff_min_soc - getattr(batt_cfg, "min_soc_absolut", 10.0),
        )
        eff_min_soc -= max(entlastung, 0.0)
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