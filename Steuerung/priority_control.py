"""
Prioritätenbasierte WP-Steuerung (Pareto-optimiert).

Statt modusbasierter Logik bewerten mehrere unabhängige Regeln
parallel die aktuelle Situation. Die Regel mit der höchsten Priorität,
deren Bedingungen erfüllt sind, gewinnt.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List
from rule_types import RegelErgebnis

from utils import to_naive
from energy_source import classify_energy_source  # noqa: F401 (Re-Export)
from json_config import (
    WPSteuerungConfig,
    NotfallschutzConfig,  # noqa: F401 (Re-Export fuer Tests)
)

# Module-level throttles: Letzte Zeitstempel fuer wiederholte Warnungen/Logs.
_last_calcstart_log: Optional[datetime] = None
_last_stale_warning: Optional[datetime] = None
STALE_LOG_INTERVAL_MIN = 5

# Re-Export der Hilfsfunktionen fuer Abwaertskompatibilitaet
# (Tests und Aufrufer importieren sie aus priority_control).
from regel_helpers import (  # noqa: E402, F401
    _fmt_stunde,
    _debug_once,
    _debug_memo,
    _fmt_pv,
    SENSOR_PLAUSIBEL_MIN_C,
    SENSOR_PLAUSIBEL_MAX_C,
    _sensor_warn_memo,
    SENSOR_WARN_INTERVAL_MIN,
    _parse_sensor,
    _is_nachtsperre,
    _is_weekend,
    _is_zeitfenster_active,
    calcstart_nachtsperre_konflikt,
    _forecast_effektiv_wh_qm,
    _energiequelle_ok,
    _energiequelle_mit_grund,
    _mittagstief_stunden,
    _forecast_quelle_ok,
    _uhr_text,
    _etappen_heizzeit_h,
    _etappen_plan,
    _legionellen_ueberfaellig,
    _legionellen_quelle_status,
    _marke_regel_ursache,
    _wochentag_name,
)


# evaluate_pv_regel ist in regel_pv.py ausgelagert.
from regel_pv import evaluate_pv_regel  # noqa: E402, F401

# evaluate_pv_ueberschuss ist in regel_pv_ueberschuss.py ausgelagert.
from regel_pv_ueberschuss import evaluate_pv_ueberschuss  # noqa: E402, F401

# evaluate_mindesttemp ist in regel_mindesttemp.py ausgelagert.
from regel_mindesttemp import evaluate_mindesttemp  # noqa: E402, F401
from regel_einspeisung import evaluate_einspeisung  # noqa: E402, F401
from regel_batterie import evaluate_batterie  # noqa: E402, F401
from regel_wochenende import evaluate_wochenende  # noqa: E402, F401
from regel_komfort import evaluate_komfort  # noqa: E402, F401
from regel_zeitfenster import evaluate_zeitfenster  # noqa: E402, F401
from regel_notfallschutz import evaluate_notfallschutz  # noqa: E402, F401

def _speichervolumen_l(config) -> Optional[float]:
    """Speichervolumen des Boilers in Litern (oder None).

    BUGFIX (22.10.2026): Wird fuer den Rest-Ertrag-Check im PV-Warten
    gebraucht: der Aufheizbedarf haengt vom Speichervolumen ab. Ohne
    Volumen ist der Bedarf nicht berechenbar - dann bleibt das alte
    Verhalten (fail-open).
    """
    for pfad in ("boiler_modell", "boiler", "Boiler"):
        teil = getattr(config, pfad, None)
        if teil is None:
            continue
        for name in ("volumen_l", "VOLUMEN_L", "volumen"):
            wert = getattr(teil, name, None)
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                if wert > 0:
                    return float(wert)
    return None


# evaluate_abweichung ist in regel_abweichung.py ausgelagert.
from regel_abweichung import evaluate_abweichung  # noqa: E402, F401
from regel_forecast import evaluate_forecast  # noqa: E402, F401
from regel_adaptive_pv import evaluate_adaptive_pv  # noqa: E402, F401

# evaluate_calculated_start ist in regel_calculated_start.py ausgelagert.
from regel_calculated_start import evaluate_calculated_start  # noqa: E402, F401
from regel_legionellen import evaluate_legionellen  # noqa: E402, F401

def _fmt_quelle(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError, OverflowError):
        return "n/a"


def bewerte_alle_regeln(
    config: WPSteuerungConfig,
    temp_dict: Dict[str, Optional[float]],
    pv_leistung: float,
    kompressor_ein: bool,
    now: Optional[datetime] = None,
    forecast_wh_qm: Optional[float] = None,
    forecast_today_wh_qm: Optional[float] = None,
    soc: Optional[float] = None,
    pv_acpower: Optional[float] = None,
    battery_power: Optional[float] = None,
    learned_evening_window: Optional[Tuple[float, float]] = None,
    learned_morning_window: Optional[Tuple[float, float]] = None,
    solar_stale: bool = False,
    learned_heating_rate_unten: Optional[float] = None,
    learned_heating_rate_gesamt: Optional[float] = None,
    learned_target_hour: Optional[float] = None,
    fc_ratio: float = 1.0,
    surplus_profile: Optional[Dict[str, float]] = None,
    recent_usage_events: Optional[List[Dict]] = None,
    bademodus_aktiv: bool = False,
    legionellen_aktiv: bool = False,
    legionellen_last_done=None,
    legionellen_started_at=None,
    legionellen_unvollstaendig: bool = False,
    legionellen_target_reached_at=None,
    # BUGFIX (22.10.2026): "forecast_day2_wh_qm" und "wochenende_cfg"
    # entfernt. Beide wurden nie gelesen:
    # - forecast_day2_wh_qm kam nur in der Signatur vor. Die Forecast-Regel
    #   arbeitet mit forecast_wh_qm (MORGEN) - die Uebermorgen-Prognose
    #   wird nirgends ausgewertet.
    # - wochenende_cfg: evaluate_wochenende holt sich die Daten selbst
    #   aus config.wochenende (siehe Aufruf in dieser Funktion). Der
    #   Parameter war doppelt.
    legionellen_planned_tag=None,
    legionellen_planned_date=None,
    notfall_aktiv: bool = False,
    pv_erzeugung_watt: Optional[float] = None,
    pv_fenster_stunde: Optional[float] = None,
    pv_fenster_aktiv: bool = True,
    pv_quelle_erlaubt: bool = True,
    pv_fenster_reserve_h: float = 0.5,
    # BUGFIX (22.10.2026): Stundenprognose fuer den Rest-Ertrag-Check
    # im PV-Warten (evaluate_abweichung).
    forecast_hourly_wh: Optional[Dict[int, float]] = None,
    # BUGFIX (22.10.2026): Zapfungs-Vorhaltung (K) und die gelernte
    # Zapfungsmenge (l) fuer den CalcStart.
    zapf_vorhaltung_k: float = 0.0,
    zapfmenge_l: float = 0.0,
) -> Tuple[Optional[RegelErgebnis], List[RegelErgebnis]]:
    """
    Hauptfunktion: Bewertet alle Regeln und gibt die Gewinner-Regel zurueck.

    ``pv_acpower`` ist der Wechselstromausgang des Wechselrichter
    (Haus + Netz) und damit NICHT die PV-Erzeugung - nachts betraegt er
    rund 250 W, obwohl die Gleichstromseite 0 meldet. Fuer die
    Quellenpruefung zaehlt die Erzeugung; sie kommt als
    ``pv_erzeugung_watt`` aus powerdc1 + powerdc2.

    Beide Parameter bleiben erhalten, weil die uebrigen Regelfunktionen
    ``pv_acpower`` weiterreichen. Aufgeloest wird hier EINMAL, an der
    einzigen Stelle, an der beide Werte zusammenlaufen: bevorzugt die
    Erzeugung, sonst der AC-Wert wie bisher.

    Returns:
        (gewinner, alle_ergebnisse): Die gewinnende Regel (oder None) und alle Ergebnisse.
    """
    if now is None:
        now = datetime.now()

    # Genau eine Aufloesung fuer alle Regeln - sonst muesste jedes
    # Untersystem selbst entscheiden, und eine vergessene Stelle faellt
    # nicht auf, sondern liefert still die falsche Quelle.
    _pv_signal = pv_acpower if pv_erzeugung_watt is None else pv_erzeugung_watt
    now_hour = now.hour
    nachtsperre_start = config.sicherheit.nachtsperre_start
    nachtsperre_ende = config.sicherheit.nachtsperre_ende

    ergebnisse: List[RegelErgebnis] = []

    # -1. Notfallschutz (Prio 110): hoechste Regel, reiner Schutzleiter.
    #     Greift ohne weitere Bedingungen vor allen Sperren (Wochenende,
    #     Nachtsperre) - im Normalbetrieb stumm.
    # "Angekuendigt": eine Legionellenfahrt ist aktiv, der Plan betrifft
    # heute, oder ein Abbruch macht einen Nachversuch noetig. Dann darf die
    # Hysterese den faelligen Prophylaxe-Start nicht abwuergen (Prio 110
    # liegt sonst ueber der Legionelle mit 90).
    _leg_angekuendigt = bool(legionellen_aktiv or legionellen_unvollstaendig)
    if not _leg_angekuendigt and legionellen_planned_date is not None:
        try:
            _leg_angekuendigt = (now.date() == legionellen_planned_date)
        except (AttributeError, TypeError):
            _leg_angekuendigt = False
    ergebnis = evaluate_notfallschutz(
        config.notfallschutz,
        temp_dict,
        kompressor_ein=kompressor_ein,
        legionellen_aktiv=bool(legionellen_aktiv),
        legionellen_max_c=(
            getattr(config.legionellen, "legionellen_max_temp_c", None)
            if legionellen_aktiv else None
        ),
        legionellen_angekuendigt=_leg_angekuendigt,
        notfall_aktiv=notfall_aktiv,
        now_hour=now_hour,
        nacht_ende=nachtsperre_ende,
    )
    ergebnisse.append(ergebnis)

    # 0. Wochenende-Regel (blockiert Einschalten am Wochenende vor fruehestens_uhr)
    ergebnis = evaluate_wochenende(config.wochenende, now, kompressor_ein)
    ergebnisse.append(ergebnis)

    # 1. Einspeise-Begrenzung (PV-Shaping am Netzlimit, hoechste Heizen-Prioritaet)
    #    Waehrend einer Legionellenfahrt gilt deren Ziel (60 C) als Abschalt-
    #    grenze - sonst bricht der 48-C-Puffer die Prophylaxe vorzeitig ab.
    ergebnis = evaluate_einspeisung(
        config.einspeisung,
        temp_dict,
        pv_leistung,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        legionellen_aktiv=bool(legionellen_aktiv),
        legionellen_ziel_c=(
            getattr(config.legionellen, "target_temp_c", None)
            if legionellen_aktiv else None
        ),
    )
    ergebnisse.append(ergebnis)

    # 1b. PV-Ueberschuss (Eigenverbrauch, Nutzerwunsch 10.11.2026)
    #     Heizt ab einer einstellbaren EINSPEISUNG (Auslegung 500 W), also
    #     unabhaengig von Etappen/Zielzeiten. Misst `pv_leistung` =
    #     feedinpower (Einspeisung), nicht die PV-Erzeugung.
    ergebnis = evaluate_pv_ueberschuss(
        config.pv_ueberschuss,
        temp_dict,
        pv_leistung,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        legionellen_aktiv=bool(legionellen_aktiv),
        legionellen_ziel_c=(
            getattr(config.legionellen, "target_temp_c", None)
            if legionellen_aktiv else None
        ),
    )
    ergebnisse.append(ergebnis)

    # 1a. PV-Regeln bewerten (PV-Exklusivitaet: PV_unten/PV_mitte sind reines
    #     Backup bei fehlendem Forecast. Sobald Forecast-Daten da sind,
    #     steuert ausschliesslich AdaptivePV.)
    pv_backup_aktiv = not (
        config.adaptive_pv.aktiv
        and getattr(config.adaptive_pv, "exklusiv_mit_forecast", False)
        and forecast_wh_qm is not None
    )
    for pv_regel in config.pv_regeln:
        if not pv_backup_aktiv:
            ergebnisse.append(
                RegelErgebnis(
                    name=pv_regel.name,
                    prioritaet=pv_regel.prioritaet,
                    aktiv=False,
                    einschalten=None,
                    grund="PV-Exklusivitaet: Forecast vorhanden, AdaptivePV steuert",
                )
            )
            continue
        ergebnis = evaluate_pv_regel(
            pv_regel,
            temp_dict,
            pv_leistung,
            kompressor_ein,
            now_hour,
            nachtsperre_start,
            nachtsperre_ende,
        )
        ergebnisse.append(ergebnis)

    # 2. Komfort-Regel
    ergebnis = evaluate_komfort(
        config.komfort,
        temp_dict,
        pv_leistung,
        nachtsperre_start,
        nachtsperre_ende,
        now_hour,
    )
    ergebnisse.append(ergebnis)

    # 2b. Mindest-Temperatur-Garantien (jeder Eintrag ein Ergebnis)
    for min_ergebnis in evaluate_mindesttemp(
        config.mindest_temp,
        temp_dict,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        learned_evening_window=learned_evening_window,
        learned_morning_window=learned_morning_window,
    ):
        ergebnisse.append(min_ergebnis)

    # 2c. Batterie-Regel (PV-Direkt > Batterie > Netz, mit dynamischer Reserve)
    ergebnis = evaluate_batterie(
        config.batterie,
        temp_dict,
        pv_leistung,
        soc,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        forecast_wh_qm=forecast_wh_qm,
        battery_power=battery_power,
    )
    ergebnisse.append(ergebnis)
    # 3. Zeitfenster-Regel
    ergebnis = evaluate_zeitfenster(
        config.zeitfenster, temp_dict, pv_leistung, now_hour
    )
    ergebnisse.append(ergebnis)

    # 4. Abweichungs-Regel
    ergebnis = evaluate_abweichung(
        config.abweichung,
        temp_dict,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        feedin_watt=pv_leistung,
        soc=soc,
        bademodus_aktiv=bademodus_aktiv,
        forecast_today_wh_qm=forecast_today_wh_qm,
        fc_ratio=fc_ratio,
        pv_acpower=_pv_signal,
        battery_power=battery_power,
        solar_stale=solar_stale,
        notfall_einschalten_c=float(config.notfallschutz.einschalten_bei_c),
        pv_quelle_erlaubt=pv_quelle_erlaubt,
        # BUGFIX (22.10.2026): Stundenprognose + Speichervolumen fuer den
        # Rest-Ertrag-Check im PV-Warten (siehe evaluate_abweichung).
        forecast_hourly_wh=forecast_hourly_wh,
        speicher_volumen_l=_speichervolumen_l(config),
    )
    ergebnisse.append(ergebnis)

    # 5. Forecast-Regel (Prognose-basiert vorheizen/sparen)
    ergebnis = evaluate_forecast(
        config.forecast,
        temp_dict,
        forecast_wh_qm,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        feedin_watt=pv_leistung,
        soc=soc,
        pv_acpower=_pv_signal,
        battery_power=battery_power,
        solar_stale=solar_stale,
        kompressor_ein=kompressor_ein,
        pv_quelle_erlaubt=pv_quelle_erlaubt,
    )
    ergebnisse.append(ergebnis)

    # 6. Adaptive-PV-Regel (dynamische PV-Schwelle)
    ergebnis = evaluate_adaptive_pv(
        config.adaptive_pv,
        temp_dict,
        pv_leistung,
        forecast_wh_qm,
        kompressor_ein,
        now_hour,
        nachtsperre_start,
        nachtsperre_ende,
        fc_ratio=fc_ratio,
        forecast_today_wh_qm=forecast_today_wh_qm,
        pv_acpower=_pv_signal,
        battery_power=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        # BUGFIX (22.10.2026): Das PV-Fenster-Gate auch fuer AdaptivePV.
        # Ohne es startete die Regel auch ausserhalb des PV-Fensters
        # aus der PV-Messung.
        pv_quelle_erlaubt=pv_quelle_erlaubt,
    )
    ergebnisse.append(ergebnis)

    # 7. Calculated-Start-Regel (optimierter Startzeitpunkt) - Mittag und Abend
    #
    # BEFUND 09.11.2026: Beide Regeln trugen denselben Text und starteten mit
    # demselben Ziel, weil (a) der Etappenplan fuer beide ueber ALLE Etappen
    # lief und die dringendste (Mittag, 12:00) waehlte und (b) die Zielzeit
    # `target_mittag_uhr` nie ankam. Jetzt bekommt jede Regel ihre eigene
    # Zielzeit und nur ihre eigene Etappe (`etappen_filter`).
    for _cs_name, _cs_target in (
        ("CalcStart-Mittag", config.calculated_start.target_mittag_uhr),
        ("CalcStart-Abend", config.calculated_start.target_uhr),
    ):
        # "CalcStart-Mittag" -> Etappe "Mittag", "CalcStart-Abend" -> "Abend"
        _cs_etappen = (
            [_cs_name.split("-", 1)[1].strip().lower()] if "-" in _cs_name else None
        )
        ergebnis = evaluate_calculated_start(
            config.calculated_start,
            temp_dict,
            now_hour,
            now.minute,
            nachtsperre_start,
            nachtsperre_ende,
            forecast_wh_qm=forecast_today_wh_qm,
            learned_heating_rate_unten=learned_heating_rate_unten,
            learned_heating_rate_gesamt=learned_heating_rate_gesamt,
            learned_target_hour=learned_target_hour,
            feedin_watt=pv_leistung,
            soc=soc,
            fc_ratio=fc_ratio,
            surplus_profile=surplus_profile,
            recent_usage_events=recent_usage_events,
            pv_acpower=_pv_signal,
            battery_power=battery_power,
            solar_stale=solar_stale,
            pv_fenster_stunde=pv_fenster_stunde,
            pv_fenster_aktiv=pv_fenster_aktiv,
            pv_quelle_erlaubt=pv_quelle_erlaubt,
            pv_fenster_reserve_h=pv_fenster_reserve_h,
            # BUGFIX (22.10.2026): Zapfungs-Vorhaltung
            zapf_vorhaltung_k=zapf_vorhaltung_k,
            zapfmenge_l=zapfmenge_l,
            speicher_volumen_l=_speichervolumen_l(config),
            name_override=_cs_name,
            target_uhr_override=_cs_target,
            etappen_filter=_cs_etappen,
        )
        ergebnisse.append(ergebnis)

    # 8. Legionellenprophylaxe-Regel
    ergebnis = evaluate_legionellen(
        config.legionellen,
        temp_dict,
        now,
        legionellen_aktiv=legionellen_aktiv,
        legionellen_last_done=legionellen_last_done,
        legionellen_started_at=legionellen_started_at,
        legionellen_unvollstaendig=legionellen_unvollstaendig,
        kompressor_ein=kompressor_ein,
        legionellen_target_reached_at=legionellen_target_reached_at,
        pv_acpower=_pv_signal,
        wochenende_cfg=config.wochenende,
        legionellen_planned_tag=legionellen_planned_tag,
        legionellen_planned_date=legionellen_planned_date,
        pv_leistung=pv_leistung,
        battery_power=battery_power,
        soc=soc,
        solar_stale=solar_stale,
        # Nutzeranforderung 23.10.2026: Die Prophylaxe respektiert die
        # Nachtsperre aus derselben Config wie alle anderen Regeln.
        nachtsperre_start=nachtsperre_start,
        nachtsperre_ende=nachtsperre_ende,
    )
    ergebnisse.append(ergebnis)

    # Gewinner bestimmen: Höchste priorisierte Regel mit klarer Entscheidung.
    # Legionellen-Wartefälle (None) bleiben sichtbar und können einen anderen
    # aktiven Regler nicht fälschlich als effektive Hardwarequelle überschreiben.
    aktive_regeln = [e for e in ergebnisse if e.aktiv and e.einschalten is not None]

    if not aktive_regeln:
        # Keine aktive Regel mit klarer EIN/AUS-Entscheidung.
        return None, ergebnisse

    # Solar-Daten veraltet: PV-/Batterie-/Prognose-Regeln duerfen nicht
    # auf eingefrorenen Werten entscheiden. Garantien (MinTemp, Komfort)
    # und Abweichung (Netzstrom) bleiben bewusst aktiv.
    global _last_stale_warning
    if solar_stale:
        _stale_namen = {
            "Einspeisung",
            "CalcStart-Mittag",
            "CalcStart-Abend",
            "Batterie",
            "AdaptivePV",
            "Zeitfenster",
            "Forecast",
        }
        for e in ergebnisse:
            if (e.name in _stale_namen or e.name.startswith("PV_")) and (
                e.aktiv or e.einschalten is not None
            ):
                e.aktiv = False
                e.einschalten = None
                e.set_reason_code("data_stale")
                e.grund = "Solar-Daten veraltet -> Regel pausiert"
        jetzt = now
        if (
            _last_stale_warning is None
            or not isinstance(_last_stale_warning, datetime)
            or (to_naive(jetzt) - to_naive(_last_stale_warning))
            >= timedelta(minutes=STALE_LOG_INTERVAL_MIN)
        ):
            logging.warning(
                "Solar-Daten veraltet: PV/Batterie/Einspeisung/"
                "CalcStart/Forecast/Zeitfenster pausiert"
            )
            _last_stale_warning = jetzt
    else:
        # Nach Rueckkehr frischer Daten darf ein neues Stale-Ereignis sofort loggen.
        _last_stale_warning = None

    # Nach der Stale-Pausierung die Kandidatenliste neu aufbauen. Die zuvor
    # gebaute Liste enthaelt dieselben Objekte, wurde aber vor der Mutation
    # gefiltert und wuerde sonst eine pausierte Solarregel weiter gewaehlen.
    aktive_regeln = [
        e for e in aktive_regeln if e.aktiv and e.einschalten is not None
    ]
    if not aktive_regeln:
        return None, ergebnisse

    # Nach Prioritaet sortieren (hoeher zuerst)
    aktive_regeln.sort(key=lambda e: e.prioritaet, reverse=True)
    gewinner = aktive_regeln[0]

    # CalcStart/AdaptivePV Koordinations-Logging (gedrosselt auf 5 Min)
    if (
        gewinner.name == "AdaptivePV"
        and any(r.name.startswith("CalcStart") and r.aktiv for r in ergebnisse)
        and not any(r.name.startswith("CalcStart") and r.einschalten is True for r in ergebnisse)
    ):
        global _last_calcstart_log
        if _last_calcstart_log is None or (now - _last_calcstart_log) > timedelta(minutes=5):
            _last_calcstart_log = now
            logging.debug(
                "CalcStart wartet auf besseres PV-Angebot -> AdaptivePV übernimmt."
            )

    # Der Notfallschutz (Prio 110) greift ohne Workaround vor allen Sperren -
    # ein manueller Override ist nicht mehr noetig (frueher: Komfort-NOTFALL
    # ueberschrieb die Wochenende-Prio-100-Sperre per Spezialfall).

    # Bewertung wird nur alle 5 Minuten in priority_control_logic.py geloggt (INFO, throttelt)
    # Hier nur minimales DEBUG, nicht bei jedem Durchlauf

    return gewinner, ergebnisse


def formatiere_ergebnisse(ergebnisse: List[RegelErgebnis]) -> str:
    """Formatiert alle Ergebnisse fuer Logging/Anzeige."""
    lines = []
    for e in sorted(ergebnisse, key=lambda x: x.prioritaet, reverse=True):
        status = (
            "EIN"
            if e.einschalten is True
            else ("AUS" if e.einschalten is False else "---")
        )
        aktive = "" if e.aktiv else " [INAKTIV]"
        lines.append(f"  [{e.prioritaet:3d}] {status} {e.name}{aktive}: {e.grund}")
    return "\n".join(lines)






