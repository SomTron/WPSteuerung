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

from energy_source import batterie_ladung_watt
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
    battery_power: Optional[float] = None,
    lauf_start_regel: Optional[str] = None,
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

    # --- Anlass bestimmen: Einspeisung ODER Batterieladung -----------------
    # Nutzerwunsch 10.11.2026: Laedt die Hausbatterie mit Ueberschuss, soll die
    # Regel ebenfalls starten - dann aber nur bis zum Minimum-Soll unten, damit
    # die Batterie nicht leergesaugt wird.
    min_w = float(ueb_cfg.einspeisung_min_watt)
    bat_min_w = float(getattr(ueb_cfg, "batterie_ladung_min_w", 0.0) or 0.0)
    ladung_w = batterie_ladung_watt(battery_power) or 0.0
    einspeisung_ok = feedin_watt >= min_w
    ladung_ok = bat_min_w > 0.0 and ladung_w >= bat_min_w
    # Ein LAUFENDER Zyklus bleibt nur dann an, wenn DIESE Regel ihn gestartet
    # hat - sonst uebernimmt sie fremde Laeufe (Netz-Notfallstart von
    # CalcStart, Mindestlaufzeit) und meldet "Modus: PV_Ueberschuss", obwohl
    # gar kein PV-Strom fliesst (Befund 10.11.2026, 17:59).
    eigener_lauf = (lauf_start_regel or "") == "PV_Ueberschuss"
    weiterlauf_ok = kompressor_ein and eigener_lauf and (
        feedin_watt >= float(ueb_cfg.weiterlauf_ab_watt) or ladung_ok
    )

    # Anlass hat Vorrang: echter Einspeise-Ueberschuss > Batterieladung >
    # Weiterlauf. So bleibt ein wegen Batterieladung gestarteter Lauf auch
    # beim Weiterlauf bei seinem niedrigeren Ziel (42 C) und laeuft nicht
    # bis 48 C hoch.
    if einspeisung_ok:
        anlass = f"Einspeisung {feedin_watt:.0f}W >= {min_w:.0f}W"
    elif ladung_ok:
        anlass = (
            f"Batterie laedt {ladung_w:.0f}W >= {bat_min_w:.0f}W "
            f"(kein Einspeise-Ueberschuss)"
        )
    elif weiterlauf_ok:
        anlass = (
            f"Weiterlauf (eigener Lauf, Einspeisung {feedin_watt:.0f}W >= "
            f"{float(ueb_cfg.weiterlauf_ab_watt):.0f}W)"
        )
    else:
        result.set_reason_code("pv_unterbrechung")
        fremd = ""
        if kompressor_ein and not eigener_lauf:
            fremd = (
                f" (laufender Zyklus von "
                f"'{lauf_start_regel or 'unbekannt'}' - kein Uebernehmen)"
            )
        result.grund = (
            f"PV-Ueberschuss: Einspeisung {feedin_watt:.0f}W < {min_w:.0f}W"
            + (
                f" und Batterieladung {ladung_w:.0f}W < {bat_min_w:.0f}W"
                if bat_min_w > 0.0 else ""
            )
            + " -> keine Aktion"
            + fremd
        )
        return result

    # Zielgrenze: Bei reiner Batterieladung nur bis zum Minimum-Soll heizen.
    grenze_c = float(ueb_cfg.ausschalten_bei_c)
    bat_anlass = ladung_ok and not einspeisung_ok
    if bat_anlass:
        bat_grenze = float(
            getattr(ueb_cfg, "batterie_ausschalten_bei_c", grenze_c)
        )
        grenze_c = min(grenze_c, bat_grenze)
    legionellen_grenze = False
    if legionellen_aktiv and legionellen_ziel_c is not None:
        # Waehrend einer Legionellenfahrt gilt deren Ziel als Abschaltgrenze -
        # sonst bricht der Puffer die Prophylaxe vorzeitig ab.
        grenze_c = max(grenze_c, float(legionellen_ziel_c))
        legionellen_grenze = True

    if temp >= grenze_c:
        result.einschalten = False
        suffix = " (Legionellenfahrt: Ziel erreicht)" if legionellen_grenze else ""
        result.grund = (
            f"PV-Ueberschuss: {ueb_cfg.temperaturfuehler} {temp:.1f}C >= "
            f"{grenze_c:.1f}C -> AUS ({anlass}){suffix}"
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

    if kompressor_ein and (
        feedin_watt >= float(ueb_cfg.weiterlauf_ab_watt) or ladung_ok
    ):
        result.einschalten = True
        result.grund = (
            f"PV-Ueberschuss Weiterlauf: {anlass} -> EIN "
            f"({ueb_cfg.temperaturfuehler} {temp:.1f}C bis {grenze_c:.1f}C)"
        )
        return result

    ein_bis = float(getattr(ueb_cfg, "einschalten_bei_c", 43.0))
    if bat_anlass:
        # Batterie-Anlass: mindestens 1 K Hysterese unter dem (niedrigeren)
        # Ziel, damit ein Start ueberhaupt Sinn ergibt (Ziel 42 -> Start < 41).
        ein_bis = min(ein_bis, max(grenze_c - 1.0, 20.0))
    # Waehrend einer Legionellenfahrt darf die Einschaltgrenze den Lauf
    # nicht blockieren - sonst bricht der Puffer die Prophylaxe vor
    #zeitigem Erreichen des 60-C-Ziels ab (Befund wie bei `Einspeisung`).
    if temp >= ein_bis and not legionellen_aktiv:
        result.einschalten = None
        result.grund = (
            f"PV-Ueberschuss: {anlass}, aber "
            f"{ueb_cfg.temperaturfuehler} {temp:.1f}C >= {ein_bis:.1f}C "
            f"(Einschaltgrenze) -> warte"
        )
        return result
    result.einschalten = True
    result.grund = (
        f"PV-Ueberschuss: {anlass} -> EIN "
        f"({ueb_cfg.temperaturfuehler} {temp:.1f}C, bis {grenze_c:.1f}C)"
    )
    return result
