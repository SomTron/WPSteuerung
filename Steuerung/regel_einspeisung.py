"""Einspeise-Begrenzungs-Regel (PV-Shaping am Netzlimit).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py
erhaelt die Abwaertskompatibilitaet.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import EinspeisungConfig
from regel_helpers import _nachtsperre_block, _parse_sensor, _is_nachtsperre, _sensor_oder_none


def evaluate_einspeisung(
    einsp_cfg: EinspeisungConfig,
    temp_dict: Dict[str, Optional[float]],
    feedin_watt: float,
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    legionellen_aktiv: bool = False,
    legionellen_ziel_c: Optional[float] = None,
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

    LEGIONELLEN-AUSNAHME (Befund aus dem Laufzeitlog 18.09.-01.10.2026):
    `ausschalten_bei_c` (48 C) ist das Zielt des PV-Puffers, NICHT das Ziel
    einer Legionellenfahrt (60 C). Ohne Ausnahme hat diese Regel die
    Prophylaxe am 25.09. nach 199 min bei 53,0 C abgebrochen - das
    Hygieneziel war noch 7 K entfernt. Waehrend einer aktiven Fahrt gilt
    daher das Legionellen-Ziel als Abschaltgrenze (nie unter dem Pufferziel).
    """
    result = RegelErgebnis(
        name="Einspeisung",
        prioritaet=einsp_cfg.prioritaet,
        aktiv=einsp_cfg.aktiv,
    )

    if not einsp_cfg.aktiv:
        result.grund = "Einspeisung-Regel inaktiv"
        return result

    if _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende):
        return _nachtsperre_block(result)

    temp = _sensor_oder_none(temp_dict, einsp_cfg.temperaturfuehler, result)
    if temp is None:
        return result

    grenze_c = float(einsp_cfg.ausschalten_bei_c)
    legionellen_grenze = False
    if legionellen_aktiv and legionellen_ziel_c is not None:
        grenze_c = max(grenze_c, float(legionellen_ziel_c))
        legionellen_grenze = True

    if temp >= grenze_c:
        result.einschalten = False
        suffix = " (Legionellenfahrt: Ziel erreicht)" if legionellen_grenze else ""
        result.grund = (
            f"Einspeisung: {einsp_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{grenze_c:.1f}C -> AUS (Einspeisung {feedin_watt:.0f}W){suffix}"
        )
        return result

    if kompressor_ein and feedin_watt >= einsp_cfg.weiterlauf_ab_watt and temp > 0:
        result.einschalten = True
        result.grund = (
            f"PV-Shaping Weiterlauf: Einspeisung {feedin_watt:.0f}W >= "
            f"{einsp_cfg.weiterlauf_ab_watt:.0f}W (Grenze {einsp_cfg.einspeisegrenze_watt:.0f}W), "
            f"{einsp_cfg.temperaturfuehler} {temp:.1f}C bis "
            f"{grenze_c:.1f}C"
        )
        return result

    if feedin_watt >= einsp_cfg.einspeisegrenze_watt:
        temp_oben = _parse_sensor(temp_dict, "oben")
        oben_cap = getattr(einsp_cfg, "oben_max_start_c", None)
        if (
            not legionellen_aktiv
            and temp_oben is not None
            and isinstance(oben_cap, (int, float))
            and not isinstance(oben_cap, bool)
            and temp_oben >= float(oben_cap)
        ):
            if kompressor_ein:
                result.einschalten = False
                result.grund = (
                    f"Einspeisung: oben {temp_oben:.1f}C >= {float(oben_cap):.1f}C "
                    f"(Speicher oben voll) -> AUS"
                )
            else:
                result.einschalten = None
                result.grund = (
                    f"Einspeisung: oben {temp_oben:.1f}C >= {float(oben_cap):.1f}C "
                    f"(Speicher oben voll) -> kein Start"
                )
            return result
        ein_bis = getattr(einsp_cfg, "einschalten_bis_c", None)
        if (
            not legionellen_aktiv
            and isinstance(ein_bis, (int, float))
            and not isinstance(ein_bis, bool)
            and temp >= float(ein_bis)
        ):
            result.einschalten = None
            result.grund = (
                f"Einspeisung: {einsp_cfg.temperaturfuehler} {temp:.1f}C >= "
                f"{float(ein_bis):.1f}C (Einschaltgrenze) -> warte"
            )
            return result
        result.einschalten = True
        result.grund = (
            f"PV-Shaping: Einspeisung {feedin_watt:.0f}W >= Grenze "
            f"{einsp_cfg.einspeisegrenze_watt:.0f}W -> EIN "
            f"({einsp_cfg.temperaturfuehler} {temp:.1f}C)"
        )
        return result

    result.set_reason_code("pv_unterbrechung")
    result.grund = f"Einspeisung {feedin_watt:.0f}W < {einsp_cfg.einspeisegrenze_watt:.0f}W -> keine Aktion"
    return result