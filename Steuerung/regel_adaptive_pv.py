"""AdaptivePV-Regel (dynamische Einspeise-Schwelle).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from typing import Optional, Dict

from rule_types import RegelErgebnis
from json_config import AdaptivePVConfig
from energy_source import Energiequelle
from regel_helpers import _nachtsperre_block, _forecast_effektiv_wh_qm, _is_nachtsperre, _parse_sensor, _sensor_oder_none

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
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    soc: Optional[float] = None,
    solar_stale: bool = False,
    # BUGFIX (22.10.2026): Das PV-Fenster-Gate. Ohne diesen Parameter
    # zaehlte die PV-Messung auch ausserhalb des Fensters als Quelle -
    # die Regel startete dann nachts oder nach Sonnenuntergang aus PV.
    pv_quelle_erlaubt: bool = True,
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
    if _is_nachtsperre(now_hour, nachtsperre_start, nachtsperre_ende):
        return _nachtsperre_block(result)

    sensor_name = adaptive_cfg.temperaturfuehler
    temp = _sensor_oder_none(temp_dict, sensor_name, result)
    if temp is None:
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

    # Schutz des oberen Speicherbereichs (Befund Oktober 2026): Der
    # Boiler-Max-Bezugsfuehler ist 'unten'. Am 12.10.2026 startete AdaptivePV
    # bei unten 43,9 C, waehrend oben bereits 57,1 C stand (0,9 K vor der
    # Ueberhitzung bei 58 C). Ist der obere Bereich voll, darf kein PV-Start
    # mehr ausgeloest werden - der Speicher oben ist bereits gesaettigt.
    temp_oben = _parse_sensor(temp_dict, "oben")
    oben_cap = getattr(adaptive_cfg, "oben_max_start_c", None)
    if (
        temp_oben is not None
        and isinstance(oben_cap, (int, float))
        and not isinstance(oben_cap, bool)
        and temp_oben >= float(oben_cap)
    ):
        # BEFUND 18.10.2026 (Betriebslog 18:29:54): Der Text sagt "kein
        # Start", die Regel lieferte aber `False` ("kein Heizen"). Weil der
        # Gewinner nach Prioritaet bestimmt wird, verdeckte diese Sonderregel
        # die rangniedrigere Abweichung (47): oben 61,6 C (Legionellen-
        # Nachlauf), unten 37,0 C nach Zapfung, SOC 96 %, Batterie 263 W - die
        # Abweichung wollte mit Batterieleistung heizen (Schichtungs-Start,
        # Obergrenze oben 62,6 C), AdaptivePV verhinderte es 35 min lang, bis
        # unten auf 22,2 C gefallen war. Ohne diesen Zweig haette das
        # Quellen-Gate weiter unten ("AdaptivePV wartet auf PV") ohnehin
        # `None` gemeldet - nur die Prioritaet des Deckels hat daraus ein Veto
        # gemacht. Im Aus-Zustand daher `None` (keine Meinung), wie im
        # Hysterese-Zweig darunter. Laeuft der Kompressor, bleibt es beim AUS:
        # dann ist es die Ueberhitzungs-Bremse des laufenden PV-Zyklus.
        if kompressor_ein:
            result.einschalten = False
            result.grund = (
                f"AdaptivePV: oben {temp_oben:.1f}C >= {float(oben_cap):.1f}C "
                f"(Speicher oben voll) -> AUS"
            )
        else:
            result.einschalten = None
            result.grund = (
                f"AdaptivePV: oben {temp_oben:.1f}C >= {float(oben_cap):.1f}C "
                f"(Speicher oben voll) -> kein Start trotz {sensor_name} {temp:.1f}C"
            )
        return result

    if temp >= einschalten_bis_c and not kompressor_ein:
        result.einschalten = None
        result.grund = (
            f"AdaptivePV: {sensor_name} {temp:.1f}C >= {einschalten_bis_c:.1f}C "
            f"(Einschaltgrenze) -> Hysterese, kein Einschalten"
        )
        return result

    # Dynamische Schwelle berechnen
    schwelle = adaptive_cfg.base_threshold_watt

    # Nur eine zentral bestätigte PV-Quelle darf AdaptivePV auslösen.
    #pv_acpower ist die Erzeugung; Netzeinspeisung allein beweist keine PV-Leistung.
    # Enum statt String-Match auf die Begruendung: Der Text kann sich aendern,
    # die Quelle als Wert nicht (Befund 07.10.2026 - Robustheit).
    _pv_status = None
    from priority_control import classify_energy_source as _ces
    _pv_status = _ces(
        pv_acpower=pv_acpower,
        feedin_watt=pv_leistung,
        batpower_raw=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=1.0,
        battery_min_watt=1.0,
        soc_min_prozent=0.0,
        max_netzkauf_watt=-50.0,
        # BUGFIX (22.10.2026): Ohne `pv_erlaubt` zaehlte die PV-Messung
        # auch ausserhalb des PV-Fensters als Quelle. AdaptivePV startete
        # dann nachts oder nach Sonnenuntergang aus PV - dieselbe
        # Fehlerklasse wie bei der Legionellen-Regel (BUGFIX 21.10.2026).
        pv_erlaubt=pv_quelle_erlaubt,
    )
    if _pv_status.quelle == Energiequelle.PV:
        quelle_ok, quelle_text = True, _pv_status.begruendung
    else:
        quelle_ok = False
        quelle_text = (
            "Batterie verfuegbar; AdaptivePV steuert ausschliesslich PV"
            if _pv_status.quelle == Energiequelle.BATTERIE
            else _pv_status.begruendung
        )
    if not quelle_ok:
        result.set_reason_code("waiting_source")
        result.grund = f"AdaptivePV wartet auf PV: {quelle_text}"
        return result

    # Temperatur- UND Prognose-Anpassung.
    #
    # VORHER stapelten sich zwei unabhaengige if/elif-Paare (bis zu x0.175):
    #   * nach unten ueberlagerte sich die Senkung - die eigentliche
    #     Temperaturinformation ("der Boiler ist kalt") ging verloren,
    #   * nach oben war die Schwelle unbegrenzt: x0.7 (leicht kalt) mal
    #     x1.5 (guter Tag) ergab 315 W, bei 7 kW PV also eine Ausloesung
    #     durch jede Wolke.
    #
    # JETZT: beide Faktoren getrennt bestimmen, Senkung begrenzt
    # kombinieren (`max_kombinationsfaktor`), Hoeherschwelle auf
    # `max_start_watt` deckeln. Der Staerkere Einfluss gewinnt, statt dass
    # sich zwei Sichtweisen gegenseitig aufheben.
    temperatur_faktor = 1.0
    if temp < adaptive_cfg.t_aggressiv_kalt_c:
        temperatur_faktor = 0.5  # Sehr kalt: aggressiver heizen
    elif temp < adaptive_cfg.t_normal_kalt_c:
        temperatur_faktor = 0.7  # Kalt: etwas niedrigere Schwelle

    # Prognose (fc_ratio = gelernte Haus-Kalibrierung)
    prognose_eff = (
        forecast_wh_qm * fc_ratio
        if forecast_wh_qm is not None and fc_ratio != 1.0
        else forecast_wh_qm
    )
    prognose_faktor = 1.0
    if prognose_eff is not None:
        if prognose_eff >= adaptive_cfg.fc_schwelle_gut_wh:
            prognose_faktor = 1.5  # Sehr sonnig: hoehere Schwelle = konservativer
        elif prognose_eff <= adaptive_cfg.fc_schwelle_schlecht_wh:
            prognose_faktor = 0.5  # Bewoelkt: niedrige Schwelle = PV jetzt nutzen

    # Senkende Faktoren begrenzt kombinieren.
    senkend = min(temperatur_faktor, prognose_faktor)
    grenze_senkung = float(getattr(adaptive_cfg, "max_kombinationsfaktor", 1.0) or 1.0)
    if senkend < grenze_senkung:
        senkend = grenze_senkung
    schwelle *= senkend
    if prognose_faktor > 1.0:
        schwelle *= prognose_faktor

    # Grenzen erst NACH der Kombination anwenden. min_start_watt war schon
    # vorhanden; max_start_watt schliesst die offene Luecke nach oben.
    min_start = float(getattr(adaptive_cfg, "min_start_watt", 0.0) or 0.0)
    max_start = float(getattr(adaptive_cfg, "max_start_watt", 0.0) or 0.0)
    geklammert = ""
    if min_start > 0 and schwelle < min_start:
        schwelle = min_start
        geklammert = f", auf Mindestwert {min_start:.0f}W angehoben"
    elif max_start > 0 and schwelle > max_start:
        schwelle = max_start
        geklammert = f", auf Hoechstwert {max_start:.0f}W begrenzt"

    if pv_leistung >= schwelle:
        result.einschalten = True
        result.grund = (
            f"AdaptivePV: PV {pv_leistung:.0f}W >= {schwelle:.0f}W "
            f"(Basis {adaptive_cfg.base_threshold_watt:.0f}W, "
            f"Temp x{temperatur_faktor:g}, Prognose x{prognose_faktor:g}, "
            f"{sensor_name}={temp:.1f}C{geklammert}) -> EIN"
        )
        return result

    result.set_reason_code("pv_unterbrechung")
    result.grund = (
        f"AdaptivePV: PV {pv_leistung:.0f}W < {schwelle:.0f}W{geklammert}"
    )
    return result


