"""PV-Ueberschuss-Regel: heizt bei Einspeisung, unabhaengig vom Zeitplan.

Nutzerwunsch 10.11.2026: Der Speicher soll den PV-Ueberschuss verheizen, auch
wenn keine Zapf-Etappe (CalcStart) ansteht - ab einer einstellbaren
Einspeise-Grenze (Auslegung 500 W). Damit steigt der Eigenverbrauch, statt
spaeter am Abend mit Netzstrom auf Soll zu gehen.

Abgrenzung zu den bestehenden Regeln:

  * ``Einspeisung`` (Prio 85) begrenzt die Einspeisung am Netzlimit (7500 W).
  * ``PV_mitte``/``PV_unten`` (Prio 78) messen die PV-ERZEUGUNG und sind bei
    vorhandenem Forecast als Backup inaktiv.
  * Diese Regel misst die EINSPEISUNG (feedinpower) - den echten Ueberschuss
    nach Hausverbrauch und Batterieladung. Sie ist damit unabhaengig von
    Etappen, Zielzeiten und der gelernten Zapfzeit.

Der Name beginnt bewusst mit ``PV_``: ``_ist_pv_gesteuerter_lauf`` erkennt
PV-Laeufe daran und gewaehrt die kurze PV-Mindestlaufzeit
(``zyklus.pv_min_laufzeit_minuten``). Bei ``solar_stale`` pausiert die Regel
wie alle PV-Regeln.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import PVUeberschussConfig
from regel_helpers import (
    _is_nachtsperre,
    _nachtsperre_block,
    _parse_sensor,
    _sensor_oder_none,
)


def evaluate_pv_ueberschuss(
    ueb_cfg: PVUeberschussConfig,
    temp_dict: Dict[str, Optional[float]],
    feedin_watt: float,
    kompressor_ein: bool,
    now_hour: int,
    nachtsperre_start: int,
    nachtsperre_ende: int,
    legionellen_aktiv: bool = False,
    legionellen_ziel_c: Optional[float] = None,
) -> RegelErgebnis:
    """EIN ab ``einspeisung_min_watt`` Einspeisung, AUS bei ``ausschalten_bei_c``.

    Reihenfolge (wie bei den anderen PV-Regeln):
      1. inaktiv / Nachtsperre / Sensor fehlt
      2. AUS: Regelfuehler >= Ausschaltgrenze (Legionellenfahrt: deren Ziel)
      3. AUS/kein Start: oberer Fuehler >= ``oben_max_start_c`` (Speicher oben voll)
      4. WEITERLAUF: Kompressor laeuft und Einspeisung >= ``weiterlauf_ab_watt``
      5. EIN: Einspeisung >= ``einspeisung_min_watt`` und Fuehler < Einschaltgrenze
      6. sonst: keine Aktion (Ueberschuss zu klein)
    """
    result = RegelErgebnis(
        name="PV_Ueberschuss",
        prioritaet=ueb_cfg.prioritaet,
        aktiv=ueb_cfg.aktiv,
    )

    if not ueb_cfg.aktiv:
        result.grund = "PV-Ueberschuss-Regel inaktiv"
        return result

    # Nachtsperre: Standardmaessig respektiert - nachts gibt es keinen
    # PV-Ueberschuss. Nur wenn ausdruecklich erlaubt (z.B. Batterie), startet
    # die Regel auch in der Sperrzeit.
    if not getattr(ueb_cfg, "nachtsperre_ueberschreiben", False):
        if _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende):
            return _nachtsperre_block(result)

    temp = _sensor_oder_none(temp_dict, ueb_cfg.temperaturfuehler, result)
    if temp is None:
        return result

    grenze_c = float(ueb_cfg.ausschalten_bei_c)
    legionellen_grenze = False
    if legionellen_aktiv and legionellen_ziel_c is not None:
        # Waehrend einer Legionellenfahrt gilt deren Ziel als Abschaltgrenze -
        # sonst bricht der 48-C-Puffer die Prophylaxe vorzeitig ab.
        grenze_c = max(grenze_c, float(legionellen_ziel_c))
        legionellen_grenze = True

    if temp >= grenze_c:
        result.einschalten = False
        suffix = " (Legionellenfahrt: Ziel erreicht)" if legionellen_grenze else ""
        result.grund = (
            f"PV-Ueberschuss: {ueb_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{grenze_c:.1f}C -> AUS (Einspeisung {feedin_watt:.0f}W){suffix}"
        )
        return result

    # Schutz des oberen Speicherbereichs: Der Regelfuehler ist meist 'unten',
    # deshalb saehe die Regel den Speicher als kalt, obwohl oben bereits kurz
    # vor der Ueberhitzung steht.
    oben = _parse_sensor(temp_dict, "oben")
    oben_cap = float(getattr(ueb_cfg, "oben_max_start_c", 0.0) or 0.0)
    if (
        not legionellen_aktiv
        and oben is not None
        and oben_cap > 0.0
        and oben >= oben_cap
    ):
        if kompressor_ein:
            result.einschalten = False
            result.grund = (
                f"PV-Ueberschuss: oben {oben:.1f}C >= {oben_cap:.1f}C "
                f"(Speicher oben voll) -> AUS"
            )
        else:
            result.einschalten = None
            result.grund = (
                f"PV-Ueberschuss: oben {oben:.1f}C >= {oben_cap:.1f}C "
                f"(Speicher oben voll) -> kein Start"
            )
        return result

    if kompressor_ein and feedin_watt >= float(ueb_cfg.weiterlauf_ab_watt):
        result.einschalten = True
        result.grund = (
            f"PV-Ueberschuss Weiterlauf: Einspeisung {feedin_watt:.0f}W >= "
            f"{ueb_cfg.weiterlauf_ab_watt:.0f}W -> EIN "
            f"({ueb_cfg.temperaturfuehler} {temp:.1f}C bis {grenze_c:.1f}C)"
        )
        return result

    if feedin_watt >= float(ueb_cfg.einspeisung_min_watt):
        ein_bis = float(getattr(ueb_cfg, "einschalten_bei_c", 43.0))
        # Waehrend einer Legionellenfahrt darf die Einschaltgrenze den Lauf
        # nicht blockieren - sonst bricht der 48-C-Puffer die Prophylaxe vor
        #zeitigem Erreichen des 60-C-Ziels ab (Befund wie bei `Einspeisung`).
        if temp >= ein_bis and not legionellen_aktiv:
            result.einschalten = None
            result.grund = (
                f"PV-Ueberschuss: Einspeisung {feedin_watt:.0f}W >= "
                f"{ueb_cfg.einspeisung_min_watt:.0f}W, aber "
                f"{ueb_cfg.temperaturfuehler} {temp:.1f}C >= {ein_bis:.1f}C "
                f"(Einschaltgrenze) -> warte"
            )
            return result
        result.einschalten = True
        result.grund = (
            f"PV-Ueberschuss: Einspeisung {feedin_watt:.0f}W >= "
            f"{ueb_cfg.einspeisung_min_watt:.0f}W -> EIN "
            f"({ueb_cfg.temperaturfuehler} {temp:.1f}C, bis {grenze_c:.1f}C)"
        )
        return result

    result.set_reason_code("pv_unterbrechung")
    result.grund = (
        f"PV-Ueberschuss: Einspeisung {feedin_watt:.0f}W < "
        f"{ueb_cfg.einspeisung_min_watt:.0f}W -> keine Aktion"
    )
    return result
