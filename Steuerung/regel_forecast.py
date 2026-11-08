"""Forecast-Regel (Prognose-basiert vorheizen/sparen).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import ForecastConfig
from regel_helpers import _nachtsperre_block, _forecast_quelle_ok, _is_nachtsperre, _parse_sensor

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
    pv_quelle_erlaubt: bool = True,
    kompressor_ein: bool = False,
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
    if _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende):
        return _nachtsperre_block(result)

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
                    pv_erlaubt=pv_quelle_erlaubt,
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
                if kompressor_ein:
                    result.einschalten = None
                    result.grund = (
                        f"Forecast-Sparen: Morgen {forecast_wh_qm:.0f} Wh/qm gut; "
                        "laufender Zyklus wird allein durch die Tagesprognose nicht beendet"
                    )
                    return result
                result.einschalten = False
                result.grund = (
                    f"Forecast-Sparen: Morgen {forecast_wh_qm:.0f} Wh/qm >= "
                    f"{forecast_cfg.fc_schwelle_hoch_wh:.0f} (gut), "
                    f"Temp {temp:.1f}C >= {forecast_cfg.t_vorheiz_ab_c}C -> kein Start"
                )
                return result

    result.grund = f"Forecast {forecast_wh_qm:.0f} Wh/qm -> keine Aktion"
    return result


