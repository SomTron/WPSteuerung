"""Hilfsfunktionen fuer die prioritaetenbasierte Regel-Engine.

Ausgelagert aus priority_control.py, um die Modulgroesse zu reduzieren und
die Hilfsfunktionen testbar zu machen. Re-Export in priority_control.py
erhaelt die Abwaertskompatibilitaet.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List

from json_config import ForecastConfig
from rule_types import RegelErgebnis

def _fmt_stunde(dezimalstunde: float) -> str:
    """Formatiert eine Dezimalstunde als HH:MM (z.B. 16.333 -> '16:20')."""
    try:
        wert = float(dezimalstunde)
    except (TypeError, ValueError):
        return "?"
    gesamt_min = int(round(wert * 60.0))
    gesamt_min = max(0, min(24 * 60, gesamt_min))
    return f"{gesamt_min // 60:02d}:{gesamt_min % 60:02d}"


_debug_memo: Dict[str, str] = {}


def _debug_once(key: str, message: str, identity: Optional[str] = None) -> None:
    """Loggt `message` nur, wenn sich die Identität geändert hat."""
    vergleich = message if identity is None else identity
    if _debug_memo.get(key) == vergleich:
        return
    _debug_memo[key] = vergleich
    logging.debug(message)


def _fmt_pv(value: object) -> str:
    """Formatiert die PV-Erzeugung fuer Begruendungstexte."""
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.0f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "n/a"


SENSOR_PLAUSIBEL_MIN_C = -30.0
SENSOR_PLAUSIBEL_MAX_C = 99.0

_sensor_warn_memo: Dict[str, datetime] = {}
SENSOR_WARN_INTERVAL_MIN = 10


def _parse_sensor(
    temp_dict: Dict[str, Optional[float]], sensor_name: str
) -> Optional[float]:
    """Liest einen Sensorwert aus dem Dictionary (mit Alias-Unterstützung)."""
    alias_map = {"mitte": "mittig"}
    resolved = alias_map.get(sensor_name, sensor_name)
    wert = temp_dict.get(resolved)
    if wert is None:
        return None
    try:
        wert = float(wert)
    except (TypeError, ValueError):
        return None
    if not (SENSOR_PLAUSIBEL_MIN_C <= wert <= SENSOR_PLAUSIBEL_MAX_C):
        jetzt = datetime.now()
        letzte = _sensor_warn_memo.get(sensor_name)
        if letzte is None or (jetzt - letzte) >= timedelta(
            minutes=SENSOR_WARN_INTERVAL_MIN
        ):
            _sensor_warn_memo[sensor_name] = jetzt
            logging.warning(
                "Sensor '%s' liefert unplausiblen Wert %.1f C "
                "(Bereich %.0f..%.0f) -> wird als DEFEKT verworfen, "
                "Regeln behandeln ihn als nicht verfuegbar",
                sensor_name, wert, SENSOR_PLAUSIBEL_MIN_C, SENSOR_PLAUSIBEL_MAX_C,
            )
        return None
    return wert


def _sensor_oder_none(
    temp_dict: Dict[str, Optional[float]], name: str, result: RegelErgebnis
) -> Optional[float]:
    """Parst den Sensor; bei None setzt result auf 'inaktiv' und gibt None zurueck.

    Konsolidiert das wiederkehrende Muster:
        temp = _parse_sensor(temp_dict, name)
        if temp is None:
            result.aktiv = False
            result.grund = f"Sensor '{name}' nicht verfuegbar"
            return None
    """
    temp = _parse_sensor(temp_dict, name)
    if temp is None:
        result.aktiv = False
        result.grund = f"Sensor '{name}' nicht verfuegbar"
    return temp


def _nachtsperre_block(result: RegelErgebnis, grund: str = "Nachtsperre aktiv") -> RegelErgebnis:
    """Setzt das Ergebnis auf 'inaktiv wegen Nachtsperre' und gibt es zurueck.

    Konsolidiert das wiederkehrende Muster:
        nachtsperre = _is_nachtsperre(...)
        if nachtsperre:
            result.aktiv = False
            result.grund = "Nachtsperre aktiv"
            return result
    """
    result.aktiv = False
    result.grund = grund
    return result


def _is_nachtsperre(now_hour: int, start: int, ende: int) -> bool:
    if start <= ende:
        return start <= now_hour < ende
    else:
        return now_hour >= start or now_hour < ende


def _is_weekend(now: datetime) -> bool:
    return now.weekday() >= 5


def _is_zeitfenster_active(now_hour: int, start: int, ende: int) -> bool:
    if start <= ende:
        return start <= now_hour < ende
    else:
        return now_hour >= start or now_hour < ende


def calcstart_nachtsperre_konflikt(
    ziel_uhr: float,
    nachtsperre_start: int,
    nachtsperre_ende: int,
) -> Tuple[bool, Optional[int], str]:
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


def _forecast_effektiv_wh_qm(forecast_today: Optional[float], fc_ratio: float = 1.0) -> float:
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
    pv_erlaubt: bool = True,
) -> bool:
    """Gemeinsames Gate: PV oder echte Batterieentladung ohne Netzkauf.

    ``pv_erlaubt=False`` = ausserhalb des PV-Fensters (Befund 18.10.2026):
    dann zaehlt die PV-Momentanmessung nicht mehr als Quelle, nur die Batterie.

    BUGFIX: ``pv_erlaubt`` wurde hier zwar angenommen, aber NICHT an
    ``classify_energy_source`` weitergereicht. Damit war das Gate
    wirkungslos - der PV-Zweig meldete immer "Quelle vorhanden", auch
    ausserhalb des PV-Fensters. Die Zwillingsfunktion
    ``_energiequelle_mit_grund`` reichte den Wert korrekt weiter.
    """
    from priority_control import classify_energy_source as _ces
    return _ces(
        pv_acpower=pv_acpower,
        feedin_watt=feedin_watt,
        batpower_raw=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=pv_min_watt,
        battery_min_watt=battery_min_watt,
        soc_min_prozent=soc_min_prozent,
        max_netzkauf_watt=max_netzkauf_watt,
        pv_erlaubt=pv_erlaubt,
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
    pv_erlaubt: bool = True,
) -> tuple:
    """Wie _energiequelle_ok, aber mit stabiler Quelle und lesbarem Grund."""
    from priority_control import classify_energy_source as _ces
    status = _ces(
        pv_acpower=pv_acpower,
        feedin_watt=feedin_watt,
        batpower_raw=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=pv_min_watt,
        battery_min_watt=battery_min_watt,
        soc_min_prozent=soc_min_prozent,
        max_netzkauf_watt=max_netzkauf_watt,
        pv_erlaubt=pv_erlaubt,
    )
    return status.ist_erneuerbar, f"{status.quelle.value}: {status.begruendung}"


def _mittagstief_stunden(
    current_time: float,
    ziel_uhr: float,
    surplus_profile: Dict[str, float],
    schwelle_w: float = 800.0,
    relativ_faktor: float = 1.4,
) -> tuple:
    """Gelernte Surplus-schwache Stunden zwischen jetzt und Zielzeit.

    Das Profil kommt aus der LearningEngine (Netzeinspeisung je Stunde,
    nur bei ausgeschaltetem WP gesampelt = Haushaltsmuster pur). Stunden
    mit typisch < schwelle_w (Kochen etc.) zaehlen nur teilweise als
    Heizzeit -> CalcStart beginnt frueher und kann im Tief pausieren.
    Returns: (anzahl_tiefstunden, lesbares_label)
    """
    # BEFUND 11.11.2026 (Log 08:00, Start aus dem Netz): Das Profil ist der
    # HAUSVERBRAUCH. Eine konstante Grundlast von 1095 W (7-17 Uhr) lag ueber
    # der absoluten Schwelle 800 W - damit galten ALLE Tagesstunden als
    # "Tief". CalcStart rechnete 3 x 0,75 h = 2,25 h extra (4,5 h statt 2,25 h)
    # und startete um 08:00 fuer das Mittagsziel 12:00.
    # Ein Tief ist eine Spitze UEBER der Tages-Grundlast (Kochen), nicht die
    # Grundlast selbst. Deshalb gilt: Wert > max(schwelle_w, Median * Faktor).
    # `relativ_faktor <= 0` schaltet die relative Pruefung ab (alte Logik).
    basis_werte = []
    if relativ_faktor > 0.0:
        for stunde, wert in surplus_profile.items():
            try:
                h_tag = int(stunde) % 24
                w = float(wert)
            except (TypeError, ValueError):
                continue
            if 6 <= h_tag <= 22 and w > 0.0:
                basis_werte.append(w)
    # Mindestens 4 bekannte Tagesstunden, sonst ist der Median nicht
    # aussagekraeftig (analog `get_surplus_profile`: Profil ab 4 Stunden).
    if len(basis_werte) >= 4:
        basis_werte.sort()
        mitte = len(basis_werte) // 2
        if len(basis_werte) % 2 == 1:
            median = basis_werte[mitte]
        else:
            median = (basis_werte[mitte - 1] + basis_werte[mitte]) / 2.0
        schwelle_w = max(schwelle_w, median * relativ_faktor)

    stunden = []
    h = int(current_time) + 1
    while h < ziel_uhr and (h - current_time) <= 10.0:
        # BUGFIX (21.10.2026): Der Lookup muss beide Key-Typen
        # akzeptieren. Die Lernengine liefert int-Keys (get_surplus_
        # profile), die API serialisiert sie zu str. Mit str-Lookup
        # gegen int-Keys wurde NIE eine Stunde gefunden - die
        # Mittagstief-Erkennung war thus wirkungslos.
        wert = surplus_profile.get(h % 24)
        if wert is None:
            wert = surplus_profile.get(str(h % 24))
        # BUGFIX (21.10.2026): Das Profil misst jetzt den HAUSVERBRAUCH
        # (nicht mehr die Einspeisung). Ein "Tief" ist damit eine Stunde
        # mit HOHEM Verbrauch (z.B. Kochen) - vorher galt umgekehrt eine
        # Stunde mit WENIGER Einspeisung als Tief.
        if wert is not None and wert > schwelle_w:
            stunden.append(h % 24)
        h += 1
    if not stunden:
        return 0.0, ""
    # Mitternachtssprung (z.B. 22,23,0,1) erzeugt keinen fortlaufenden
    # Bereich - dann nur die Dauer ausgeben, nicht eine falsche Spanne.
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
    pv_erlaubt: bool = True,
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
        pv_erlaubt=pv_erlaubt,
    )




def _uhr_text(stunde: float) -> str:
    """Formatiert eine Zielzeit lesbar.

    Die gelernte Zielzeit ist ein EWMA und damit praktisch nie ganzzahlig
    (z.B. 18.5). `f"{18.5:.0f}"` liefert "18" - daraus las sich im Log
    "bis 18:00", obwohl 18:30 gemeint war. Deshalb: volle Stunden ohne
    Nachkommastellen, halbe als "18:30".
    """
    gesamt = int(round(float(stunde) * 60.0))
    return f"{gesamt // 60}:{gesamt % 60:02d}"


def _etappen_heizzeit_h(
    temp_dict: Dict[str, Optional[float]],
    etappe: object,
    parameter: object = None,
    max_h: float = 8.0,
    schritt_min: int = 15,
    boiler_max_c: float = 48.0,
) -> Optional[Tuple[float, bool]]:
    """Heizzeit (h) bis eine Etappe ihre Zieltemperatur erreicht.

    Vorwaertssimulation des kalibrierten Speichermodells
    (``Steuerung/thermal_modell.py``) mit laufendem Kompressor.
    Rueckgabe ``(stunden, erreicht)`` oder ``None``, wenn Modell oder
    Sensoren fehlen - dann rechnet der Aufrufer mit der Raten-Schaetzung.

    WARUM SIMULATION STATT RATE (Messung 01.10.-05.11.2026): Die Rate des
    mittleren Fuehlers haengt vom Schichtungsabstand ab - bei delta <= 4 K
    steigt er mit 3,75 K/h, bei delta > 8 K FAELLT er (-1,25 K/h), waehrend
    ``unten`` mit 17-21 K/h hochgeht. Eine Konstante liegt damit je nach
    Situation um Faktor 2-3 daneben und verschiebt den Start um Stunden.
    """
    try:
        from thermal_modell import Speicher, SpeicherParameter
    except ImportError:      # pragma: no cover - Modell nicht vorhanden
        return None

    unten = _parse_sensor(temp_dict, "unten")
    mitte = _parse_sensor(temp_dict, "mitte")
    oben = _parse_sensor(temp_dict, "oben")
    if unten is None or mitte is None or oben is None:
        return None

    if parameter is None:
        parameter = SpeicherParameter.gemessen_oktober_2026()
    feld = getattr(etappe, "temperaturfuehler", "unten")
    ziel = float(getattr(etappe, "solltemperatur_c", 42.0))

    speicher = Speicher(parameter, start_unten=unten, start_mitte=mitte, start_oben=oben)  # type: ignore[arg-type]
    if feld not in speicher.temperaturen:
        return None
    if speicher.temperaturen[feld] >= ziel:
        return (0.0, True)

    minuten = 0.0
    while minuten < max_h * 60.0:
        speicher.schritt(schritt_min * 60.0, kompressor_ein=True)
        minuten += schritt_min
        if speicher.temperaturen[feld] >= ziel:
            return (minuten / 60.0, True)
        if speicher.temperaturen["oben"] >= boiler_max_c:
            # Boiler-Max erreicht: die reale WP schaltet ab, mehr geht nicht.
            return (minuten / 60.0, False)
    return (max_h, False)


def _etappen_plan(
    calc_cfg: object,
    temp_dict: Dict[str, Optional[float]],
    current_time: float,
    ziel_uhr_default: float,
    boiler_max_c: float = 48.0,
    parameter: object = None,
    etappen_filter: Optional[List[str]] = None,
) -> Tuple[Optional[float], Optional[float], Optional[float], List[Dict[str, object]]]:
    """Dringendste Etappe bestimmen.

    Rueckgabe ``(ziel_uhr, solltemperatur_c, hours_needed, plan)``.
    ``plan`` ist die vollstaendige Liste fuer die Diagnose/WebApp:
    je Etappe Zielzeit, Soll, Heizzeit und Restpuffer. Ist keine Etappe
    offen (alle Ziele erreicht), kommen ``hours_needed = 0.0`` und die
    zuletzt faellige Etappe zurueck - der Aufrufer meldet dann "Ziel
    bereits erreicht".

    ``etappen_filter`` (BEFUND 09.11.2026): Liste von Etappennamen
    (kleingeschrieben). CalcStart-Mittag und CalcStart-Abend sind eigene
    Regeln und duerfen NICHT dieselbe Etappe bewerten - sonst trugen beide
    den gleichen Text und starteten mit demselben Ziel (Betriebslog
    09.11.2026: beide "bis 12:00"). ``None`` = alle Etappen (altes
    Verhalten fuer Aufrufer ohne Etappen-Zuordnung).
    """
    plan: List[Dict] = []
    spaetstart_puffer = float(getattr(calc_cfg, "spaetstart_puffer_h", 0.5))
    for etappe in getattr(calc_cfg, "etappen", None) or []:
        name = str(getattr(etappe, "name", "Etappe") or "Etappe").strip().lower()
        if etappen_filter is not None and name not in etappen_filter:
            continue
        ziel = (float(etappe.ziel_uhr) if getattr(etappe, "ziel_uhr", None) is not None
                else float(ziel_uhr_default))
        erg = _etappen_heizzeit_h(temp_dict, etappe, parameter=parameter,
                                  boiler_max_c=boiler_max_c)
        if erg is None:
            continue
        stunden, erreichbar = erg
        plan.append({
            "name": getattr(etappe, "name", "Etappe"),
            "ziel_uhr": round(ziel, 2),
            # Berechnete Startzeit: Zielzeit minus benoetigte Heizzeit. Das
            # ist die Zahl, die der Betreiber in der WebApp sehen will
            # ("wann muss ich einschalten, um um 13:00 40 C zu haben?").
            "start_uhr": round(ziel - stunden, 2),
            # Letzter moeglicher Start fuer DIESE Etappe (Ziel - Heizzeit -
            # Spaetstart-Puffer). Bewusst je Etappe: die globale Regel-Rechnung
            # mischt den Mittagstief-Aufschlag hinein und liefert dadurch eine
            # andere (fruehere) Zeit als der Etappenplan - das war im Betrieb
            # 09.11.2026 die Ursache fuer "geplant 08:46 / Start 10:30".
            "spaetester_start_uhr": round(ziel - stunden - spaetstart_puffer, 2),
            "solltemperatur_c": float(getattr(etappe, "solltemperatur_c", 42.0)),
            "temperaturfuehler": getattr(etappe, "temperaturfuehler", "unten"),
            "hours_needed": round(stunden, 2),
            "puffer_h": round((ziel - current_time) - stunden
                              - float(getattr(etappe, "puffer_h", 0.5)), 2),
            "erreichbar": erreichbar,
        })
    if not plan:
        return (None, None, None, [])

    offen = [e for e in plan if e["hours_needed"] > 0.0]
    if not offen:
        letzte = max(plan, key=lambda e: e["ziel_uhr"])
        return (letzte["ziel_uhr"], letzte["solltemperatur_c"], 0.0, plan)
    # Dringendste = kleinster Restpuffer; bei Gleichstand die fruehere Zielzeit.
    dringend = min(offen, key=lambda e: (e["puffer_h"], e["ziel_uhr"]))
    dringend["dringendste"] = True
    return (dringend["ziel_uhr"], dringend["solltemperatur_c"],
            dringend["hours_needed"], plan)




def _legionellen_ueberfaellig(cfg: object, last_done: Optional[datetime], now: datetime) -> bool:
    """True, wenn der letzte Prophylaxe-Lauf zu lange zurueckliegt.

    Ohne PV (Dezember, Nebulaeltage) wartet die Regel sonst unbegrenzt auf
    eine Quelle, die nicht kommt. ``max_tage_ohne_lauf`` (Default 14 Tage)
    begrenzt das Warten: danach ist Hygiene wichtiger als der PV-Optimismus
    und der Lauf wird mit Netzstrom durchgefuehrt. 0 schaltet die Frist ab.
    """
    frist = getattr(cfg, "max_tage_ohne_lauf", 14)
    try:
        frist = int(frist)
    except (TypeError, ValueError):
        return False
    if frist <= 0:
        return False
    if last_done is None:
        # Noch nie gelaufen: Ohne bekannten Referenzzeitpunkt gibt es keine
        # Frist, die man ueberschreiten koennte. Dann gilt das alte Verhalten
        # (auf PV warten) - ein Frischbetrieb soll nicht ungefragt mit
        # Netzstrom auf 60 C fahren.
        return False
    try:
        tage = (now.date() - last_done.date()).days if hasattr(last_done, "date") else (now.date() - last_done).days
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(tage >= frist)


def _legionellen_quelle_status(cfg: object, pv_leistung: float, battery_power: Optional[float],
                               soc: Optional[float], solar_stale: bool,
                               pv_acpower: Optional[float] = None,
                               pv_erlaubt: bool = True) -> tuple:
    """Prüft PV/Batterie anhand derselben zentralen Quellenlogik."""
    from priority_control import classify_energy_source as _ces
    status = _ces(
        pv_acpower=pv_acpower,
        feedin_watt=pv_leistung,
        batpower_raw=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        pv_min_watt=float(getattr(cfg, "pv_start_min_watt", 500.0)),
        battery_min_watt=float(getattr(cfg, "batterie_start_min_watt", 50.0)),
        soc_min_prozent=float(getattr(cfg, "batterie_start_min_soc_prozent", 90.0)),
        max_netzkauf_watt=float(getattr(cfg, "max_netzbezug_watt", -50.0)),
        # BUGFIX (21.10.2026): Ausserhalb des PV-Fensters zaehlt PV
        # nicht als Quelle. Dieser Aufruf war der letzte, der den
        # Parameter nicht durchreichte - die Legionelle haette damit
        # auch nachts oder nach Sonnenuntergang aus PV starten
        # duerfen. Nachweis: PV 600 W ergab "PV erneuerbar=True",
        # obwohl das Fenster geschlossen war.
        pv_erlaubt=pv_erlaubt,
    )
    if status.ist_erneuerbar:
        return True, status.quelle.value
    return False, f"{status.quelle.value}: {status.begruendung}"




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


def _marke_regel_ursache(result: RegelErgebnis, code: str) -> RegelErgebnis:
    """Setzt einen stabilen Grundcode ohne Änderung des Lesetexts."""
    result.set_reason_code(code)
    return result




