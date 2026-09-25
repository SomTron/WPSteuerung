# -*- coding: utf-8 -*-
"""
Prioritaetenbasierte Steuerungslogik (Pareto-optimiert).

Ersetzt die modusbasierte Logik durch eine Regelengine mit Prioritaeten.
Jede Regel bewertet unabhaengig die aktuelle Situation.
Die Regel hoechster Prioritaet bestimmt das Schaltverhalten.
"""

import logging
import re
import datetime as _datetime_module
from datetime import date, datetime, timedelta
from typing import Callable

from utils import safe_timedelta
from clock import now_for
from constants import (
    CONFIG_CHECK_INTERVAL_SEC,
    FORECAST_MAX_AGE_HOURS,
    SOLAR_DATA_STALE_THRESHOLD_MIN,
)
from collections import deque
from logic_utils import check_log_throttle, normalize_forecast_wh_qm
from legionellen_plan import clear_plan
from safety_logic import handle_critical_compressor_error
from json_config import WPSteuerungConfig
from priority_control import (
    RegelErgebnis,
    bewerte_alle_regeln,
    calcstart_nachtsperre_konflikt,
    formatiere_ergebnisse,
)

_REAL_DATETIME = datetime


def _now_for_state(state):
    """State-Zeit mit Rückwärtskompatibilität für Test-/Alt-Time-Patches."""
    if datetime is not _REAL_DATETIME:
        return datetime.now(getattr(state, "local_tz", None))
    return now_for(state)

try:
    import entscheidungs_log
except ImportError:
    entscheidungs_log = None

try:
    from telegram_api import send_telegram_message  # noqa: F401 - Verfuegbarkeits-Probe
except ImportError:
    pass

# Sicherheitsabstand zum harten Boiler-Maximum beim Neueinschalten (K).
# Default 2.0: Bei Bezugsfuehler >= 46C (Limit 48C) wird kein EIN mehr
# ausgeloest, damit ein Start das Limit nicht innerhalb der Mindestlaufzeit
# erreicht (Incident 26.08.: EIN bei unten 47.94C -> nach 2 min BOILERMAX-AUS).
BOILER_MAX_EIN_ABSTAND_K = 2.0


def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.stats.last_compressor_off_time = time_val


def setze_neustartsperre(state, minuten: int = 10) -> None:
    """Setzt eine explizite Neustartsperre fuer den Kompressor.

    Ersetzt den alten Trick, last_compressor_off_time in die Zukunft zu
    setzen: Die Sperre ist jetzt ein eigenes Feld mit klar lesbarem
    Blocking-Reason, statt eine Mindestpausen-Rechnung zu verfaelschen."""
    state.control.restart_lockout_until = _now_for_state(state) + timedelta(
        minutes=minuten
    )


async def check_pressure_and_config(
    session,
    state,
    handle_pressure_check_func: Callable,
    set_kompressor_status_func: Callable,
    only_pressure: bool = False,
):
    """Prueft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.control.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.control.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.control.ausschluss_grund = "Druckschalterfehler"
        state.control.blocking_reason = "Druckschalter-Fehler"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(
                state, False, force=True, end_grund="druckfehler"
            )
        return False

    if not only_pressure:
        if safe_timedelta(
            _now_for_state(state), state._last_config_check, state.local_tz
        ) > timedelta(seconds=CONFIG_CHECK_INTERVAL_SEC):
            state.update_config()
            state._last_config_check = _now_for_state(state)
    return True


def _solar_daten_veraltet(state) -> bool:
    """True, wenn die Solax-Daten aelter als der Stale-Schwellwert sind.

    Die PV-abhaengigen Regeln werden dann pausiert (siehe priority_control);
    main.py setzt zusaetzlich die Werte selbst auf 0."""
    last_api_call = getattr(state.solar, "last_api_call", None)
    if not isinstance(last_api_call, _datetime_module.datetime):
        return True  # nicht lieferbar oder Mock/ungültig -> fail-safe
    try:
        alter_min = safe_timedelta(
            _now_for_state(state), last_api_call, state.local_tz
        ).total_seconds() / 60.0
    except (TypeError, ValueError) as exc:
        # Fehlerhafte Zeitdaten sind nicht frisch. Fail-safe: Solarregeln
        # pausieren, statt mit einem unkontrollierbaren Zustand zu arbeiten.
        logging.debug("Alter der Solax-Daten nicht bestimmbar; stale=%s", exc)
        return True
    return alter_min < 0 or alter_min > SOLAR_DATA_STALE_THRESHOLD_MIN


def _forecast_daten_veraltet(state) -> bool:
    """Forecast ist nach der Update-Frist nicht mehr vertrauenswürdig."""
    updated = getattr(state, "last_forecast_update", None)
    if updated is None:
        # Ohne erfolgreichen Abruf darf weder ein gespeicherter noch ein
        # bereits gesetzter Forecast als aktuell verwendet werden.
        hat_forecast = any(
            getattr(state.solar, name, None) is not None
            for name in ("forecast_today", "forecast_tomorrow", "forecast_day2")
        )
        hat_plan = getattr(state, "legionellen_planned_date", None) is not None
        state.forecast_stale = bool(hat_forecast or hat_plan)
        state.forecast_age_s = None
        if state.forecast_stale:
            logging.debug("Forecast-Daten ohne Aktualisierungszeitpunkt -> stale")
        return state.forecast_stale
    if not isinstance(updated, _datetime_module.datetime):
        state.forecast_age_s = None
        return True
    try:
        age = safe_timedelta(
            _now_for_state(state), updated, state.local_tz
        ).total_seconds()
        state.forecast_age_s = max(0, int(age))
        stale = age < 0 or age > FORECAST_MAX_AGE_HOURS * 3600
        state.forecast_stale = stale
        return stale
    except (TypeError, ValueError):
        state.forecast_age_s = None
        state.forecast_stale = True
        return True


def _invalidate_legionellen_plan(state, reason: str = "Forecast veraltet") -> None:
    """Alten Legionellen-Tagesplan verwerfen, statt ihn weiter zu starten."""
    clear_plan(state, reason, persist=True)


def _set_stale_forecast(state) -> None:
    """Alle prognoseabhängigen Felder neutralisieren, nie alte Werte weitergeben."""
    state.forecast_stale = True
    state.forecast_age_s = None
    _invalidate_legionellen_plan(state, "Forecast veraltet")
    if check_log_throttle(state, "_log_forecast_stale", interval_minutes=30):
        logging.warning(
            "Forecast veraltet/ungültig -> Prognose-Regeln werden nicht mehr gesteuert"
        )
    for name in (
        "forecast_today",
        "forecast_tomorrow",
        "forecast_day2",
        "forecast_hourly_wm2",
    ):
        setattr(state.solar, name, None)


def _gelerntes_morgenfenster(learning_engine):
    """Gelerntes Morgen-Zapffenster defensiv abfragen (alte Fakes fehlt es)."""
    if not learning_engine or not hasattr(
        learning_engine, "get_learned_morning_window"
    ):
        return None
    try:
        return learning_engine.get_learned_morning_window()
    except Exception as e:  # pragma: no cover - Lernen darf nie blockieren
        logging.debug(f"Morgenfenster nicht ermittelbar: {e}")
        return None


PV_WEITERLAUF_REGELN = ("AdaptivePV", "PV_mitte", "PV_unten", "Einspeisung")


def _ist_pv_unterbrechung(grund) -> bool:
    """Prüft den stabilen Regelcode; Text-Fallback bleibt für Alt-Tests."""
    reason_code = getattr(grund, "reason_code", None)
    if reason_code:
        return reason_code == "pv_unterbrechung"
    if not isinstance(grund, str) or not grund:
        text = getattr(grund, "grund", "")
        if not text:
            return False
        grund = text
    return bool(
        re.search(r"PV [\d.]+W < [\d.]+W", grund)
        or "kein PV-Ueberschuss" in grund
        or "kein Überschuss" in grund
    )


def _pv_weiterlauf_block(state, gewinner, should_on: bool) -> bool:
    """Unterdrueckt ein PV-bedingtes AUS waehrend kurzer PV-Einbrueche.

    Empfehlung 3.2: Will eine PV-Regel trotz laufendem Kompressor wegen
    'PV zu wenig' abschalten, wird das AUS fuer
    pv_weiterlauf_abschalt_delay_min Minuten zurueckgehalten (Wolke). Erst
    danach greift der AUS-Wunsch. Nicht-PV-bedingte AUS-Gruende bleiben
    unveraendert.
    """

    def _reset():
        try:
            state.control._pv_abschaltwunsch_seit = None
        except Exception:
            pass

    if not (getattr(state.control, "kompressor_ein", False) and not should_on):
        _reset()
        return should_on
    if gewinner is None or getattr(gewinner, "name", "") not in PV_WEITERLAUF_REGELN:
        _reset()
        return should_on
    if not _ist_pv_unterbrechung(gewinner):
        _reset()
        return should_on
    try:
        _zykl = getattr(getattr(state, "priority_config", None), "zyklus", None)
        delay_min = float(getattr(_zykl, "pv_weiterlauf_abschalt_delay_min", 0.0))
    except (TypeError, ValueError):
        delay_min = 0.0
    if delay_min <= 0:
        _reset()
        return should_on
    jetzt = _now_for_state(state)
    seit = getattr(state.control, "_pv_abschaltwunsch_seit", None)
    if seit is None:
        state.control._pv_abschaltwunsch_seit = jetzt
        if check_log_throttle(state, "log_pv_weiterlauf", 5):
            logging.info(
                f"PV-Weiterlauf (cycle={_zyklus_id(state)}): kurzer "
                f"PV-Einbruch, Kompressor laeuft bis zu {delay_min:.0f} min weiter"
            )
        return True
    try:
        vergangen = safe_timedelta(jetzt, seit, state.local_tz)
    except Exception:
        state.control._pv_abschaltwunsch_seit = jetzt
        return True
    if vergangen < timedelta(minutes=delay_min):
        return True
    _reset()
    return should_on


def _aktuelle_quellenbezeichnung(state) -> str:
    """Trennt PV-Erzeugung, Batterieentladung und Netzbezug im Status."""
    source = getattr(getattr(state, "solar", None), "energy_source", None)
    if source in {"PV", "Batterie", "Netz", "Daten stale", "keine Quelle"}:
        return source
    return "Netz"


def _set_effective_cycle_rule(state, rule_name, source_name=None) -> None:
    """Setzt die tatsächlich Hardware-steuernde Regel nach erfolgreichem Start."""
    control = getattr(state, "control", None)
    if control is None:
        return
    control.effective_rule_name = rule_name or None
    control.active_rule_name = rule_name or None
    control._lauf_start_regel = rule_name or None
    if source_name:
        control.source_at_start = source_name
        control.effective_source = source_name
    control.source_current = _aktuelle_quellenbezeichnung(state)


def _soll_priority_loggen(state, alle_ergebnisse) -> bool:
    """Kompakt-Log-Entscheidung (Empfehlung "Logvolumen reduzieren").

    Die volle 15-Zeilen-Bewertung erscheint nur noch, wenn sich eine
    EIN/AUS-Entscheidung einer Regel geaendert hat ODER als staendlicher
    Snapshot alle 60 Minuten. Dazwischen bleibt das Log ruhig - viele
    Einzelheiten stehen strukturiert in entscheidungs_log.jsonl.
    """
    signatur = tuple(
        sorted((r.name, r.einschalten) for r in alle_ergebnisse
               if r.einschalten is not None)
    )
    letzte_signatur = getattr(state, "_last_priority_signatur", None)
    if signatur != letzte_signatur:
        state._last_priority_signatur = signatur
        # Throttle-Zeitpunkt mitziehen, damit der Snapshot-Zweig (60 min)
        # nicht unmittelbar danach ein zweites Mal loggen wuerde.
        state._last_priority_log = _now_for_state(state)
        return True
    return check_log_throttle(state, "_last_priority_log", interval_minutes=60.0)


async def determine_mode_and_setpoints(state, t_unten, t_mittig, learning_engine=None):
    """
    Bestimmt den Betriebsmodus basierend auf den Prioritaeten-Regeln.

    Statt fester Modus-Logik wird die Prioritaeten-Engine befragt.
    """
    temp_dict = {
        "oben": state.sensors.t_oben,
        "unten": t_unten,
        "mittig": t_mittig,
        "verd": state.sensors.t_verd,
    }

    pv_leistung = state.solar.feedinpower if state.solar.feedinpower else 0.0

    # --- Bademodus/Urlaubsmodus-Kopplung ---
    # Wir arbeiten mit einer Kopie der Config, um die original-Config nicht zu aendern.
    # Falls Bademodus/Urlaub aktiv, passen wir die Solltemperatur der Abweichungs-Regel an.
    import copy

    effektive_config = copy.deepcopy(state.priority_config)

    if state.bademodus_aktiv:
        # Bademodus: Zieltemperatur-Erhoehung aus der Config (fuer warmes Wasser)
        erhoehung = effektive_config.bademodus.solltemperatur_erhoehung_c
        effektive_config.abweichung.solltemperatur_c += erhoehung
        logging.debug(
            f"Bademodus aktiv: Solltemperatur +{erhoehung}C auf {effektive_config.abweichung.solltemperatur_c}C"
        )

    if state.urlaubsmodus_aktiv:
        # Urlaubsmodus: Solltemperatur senken (Sparmodus)
        absenkung = (
            float(state.config.Urlaubsmodus.URLAUBSABSENKUNG)
            if hasattr(state.config, "Urlaubsmodus")
            else 5.0
        )
        effektive_config.abweichung.solltemperatur_c -= absenkung
        logging.debug(
            f"Urlaubsmodus aktiv: Solltemperatur -{absenkung}C auf {effektive_config.abweichung.solltemperatur_c}C"
        )

    # Sommer-Modus: Solltemperatur senken bei mehrtÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¤gig guter PV-Prognose.
    # Im Sommer scheint fast jeden Tag die Sonne, daher braucht der Boiler nicht
    # jeden Tag auf 44ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚°C+ hochgeheizt zu werden - morgen kommt ja wieder PV-Strom.
    # Der Offset (default -3ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚°C) reduziert die Zieltemperatur der Abweichungs-Regel.
    if hasattr(state, "sommer_modus_aktiv") and state.sommer_modus_aktiv:
        # AUSNAHME Bademodus (Nutzeranforderung): Der Bademodus setzt den
        # Sommer-Offset temporaer aus, damit die Temp-Anhebung (+3K) nicht
        # neutralisiert wird.
        if not state.bademodus_aktiv:
            wende_sommer_offset_an(effektive_config)
        logging.debug(
            f"Sommer-Modus aktiv: Abweichungs-Soll {effektive_config.abweichung.solltemperatur_c:.1f}C, "
            f"PV-Ausschaltpunkte "
            f"{', '.join(f'{r.name}:{r.ausschalten_bei_c:.0f}C' for r in effektive_config.pv_regeln)}"
        )

    # Forecast-Daten aus State holen
    forecast_stale = _forecast_daten_veraltet(state)
    if forecast_stale:
        _set_stale_forecast(state)
        # Nach der Invalidierung bleiben die PV-/Batterie-Livewerte nutzbar;
        # nur die Prognose ist nicht mehr vertrauenswürdig.
    else:
        state.forecast_stale = False
    # Forecast + AdaptivePV brauchen die MORGEN-Prognose (Vorheizen/Sparen)
    forecast_wh_qm = getattr(state.solar, "forecast_tomorrow", None)
    forecast_wh_qm = normalize_forecast_wh_qm(forecast_wh_qm)
    # CalcStart braucht die HEUTE-Prognose (PV-Erwartung zum Warten/Heizen)
    forecast_today_wh = getattr(state.solar, "forecast_today", None)
    forecast_today_wh = normalize_forecast_wh_qm(forecast_today_wh)
    # Stundenscharfe Forecast-Daten in Watt (W/mÂ² -> W)
    hourly_forecast_watt = None
    hourly_mw2 = getattr(state.solar, "forecast_hourly_wm2", None)
    if hourly_mw2 is not None:
        pv_flaeche = float(getattr(effektive_config.wp, "pv_array_size_qm", 10.0))
        hourly_forecast_watt = {h: w * pv_flaeche for h, w in hourly_mw2.items()}

    planned_date = getattr(state, "legionellen_planned_date", None)
    planned_tag = getattr(state, "legionellen_planned_tag", None)
    if not isinstance(planned_date, date):
        planned_date = None
    now_local = _now_for_state(state)
    if planned_date is not None and planned_date < now_local.date():
        _invalidate_legionellen_plan(state, "Geplanter Legionellen-Termin ist verstrichen")
        planned_date = None

    # Alle Regeln bewerten (mit effektiver Config)
    # Learning Engine aktualisieren (Heizzyklen + Zapfprofil + Solar-Tracking)
    if learning_engine is not None:
        learning_engine.update(
            now=_now_for_state(state),
            temp_dict=temp_dict,
            compressor_is_on=state.control.kompressor_ein,
            feedin_watt=pv_leistung,
            soc=getattr(state.solar, "soc", None),
            forecast_today_wh_qm=forecast_today_wh,
            forecast_hourly_wh=hourly_forecast_watt,
        )
        gelernte_rate_unten = learning_engine.get_learned_heating_rate(
            now_local.month, "unten"
        )
        gelernte_rate_gesamt = learning_engine.get_learned_heating_rate(
            now_local.month, "gesamt"
        )
        gelernte_zielzeit = learning_engine.get_learned_target_hour()
        # Defensiv: aeltere Lern-Engines/Fakes kennen die Methoden evtl.
        # nicht -> dann neutral bleiben (Ratio 1.0 / kein Profil).
        _get_fenster = getattr(learning_engine, "get_learned_evening_window", None)
        gelerntes_abendfenster = _get_fenster() if callable(_get_fenster) else None
        _ratio_getter = getattr(learning_engine, "get_forecast_ratio", None)
        fc_ratio = _ratio_getter() if callable(_ratio_getter) else 1.0
        _profil_getter = getattr(learning_engine, "get_surplus_profile_with_forecast", None)
        surplus_profil = _profil_getter(
            forecast_today_wh_qm=forecast_today_wh,
            forecast_hourly_wh=hourly_forecast_watt,
        ) if callable(_profil_getter) else None
        _usage_getter = getattr(learning_engine, "get_recent_usage_events", None)
        recent_usage_events = _usage_getter(hours=2) if callable(_usage_getter) else []
    else:
        gelernte_rate_unten = None
        gelernte_rate_gesamt = None
        gelernte_zielzeit = None
        gelerntes_abendfenster = None
        fc_ratio = 1.0
        surplus_profil = None
        recent_usage_events = []

    # Konfigurations-Guard: CalcStart-Zielzeit vs. Nachtsperre.
    # Eine Zielzeit innerhalb/hinter der Sperre macht die Regel stumm -
    # einmal pro Konfigurations-/Lernstand-Aenderung warnen, damit der
    # Betreiber den Konflikt sieht statt sich ueber kaltes Wasser zu wundern.
    try:
        ziel_pruefung = (
            gelernte_zielzeit
            if gelernte_zielzeit is not None
            else float(state.priority_config.calculated_start.target_uhr)
        )
        calc_tot, _, calc_hinweis = calcstart_nachtsperre_konflikt(
            float(ziel_pruefung),
            state.priority_config.sicherheit.nachtsperre_start,
            state.priority_config.sicherheit.nachtsperre_ende,
        )
        signatur = (calc_tot, round(float(ziel_pruefung), 2))
        if (calc_tot or calc_hinweis) and getattr(
            state, "_calcstart_warn_signatur", None
        ) != signatur:
            state._calcstart_warn_signatur = signatur
            if calc_tot:
                logging.error(f"CalcStart-Konflikt: {calc_hinweis}")
            else:
                logging.warning(f"CalcStart-Hinweis: {calc_hinweis}")
    except (TypeError, ValueError):
        pass  # ungueltige Lernwerte: Regel-Engine faengt das selbst ab

    gewinner, alle_ergebnisse = bewerte_alle_regeln(
        config=effektive_config,
        temp_dict=temp_dict,
        pv_leistung=pv_leistung,
        kompressor_ein=state.control.kompressor_ein,
        now=_now_for_state(state),
        forecast_wh_qm=forecast_wh_qm,
        forecast_today_wh_qm=forecast_today_wh,
        forecast_hourly_wh=hourly_forecast_watt,
        soc=getattr(state.solar, "soc", None),
        battery_power=getattr(state.solar, "batpower", None),
        pv_acpower=getattr(state.solar, "acpower", None),
        learned_evening_window=gelerntes_abendfenster,
        learned_morning_window=_gelerntes_morgenfenster_mit_bonus(
            state, learning_engine
        ),
        solar_stale=_solar_daten_veraltet(state),
        learned_heating_rate_unten=gelernte_rate_unten,
        learned_heating_rate_gesamt=gelernte_rate_gesamt,
        learned_target_hour=gelernte_zielzeit,
        fc_ratio=fc_ratio,
        surplus_profile=surplus_profil,
        recent_usage_events=recent_usage_events,
        bademodus_aktiv=bool(state.bademodus_aktiv),
        legionellen_aktiv=bool(state.legionellen_aktiv),
        legionellen_last_done=state.legionellen_last_done,
        legionellen_started_at=state.legionellen_started_at,
        legionellen_target_reached_at=getattr(state, "legionellen_target_reached_at", None),
        forecast_day2_wh_qm=normalize_forecast_wh_qm(
            getattr(state.solar, "forecast_day2", None)
        ),
        wochenende_cfg=state.priority_config.wochenende,  # fuer Wochenende-Nachholung
        legionellen_planned_tag=(
            planned_tag if isinstance(planned_tag, int) and 0 <= planned_tag <= 6 else None
        ),
        legionellen_planned_date=planned_date,
    )

    # Ergebnisse loggen - KOMPAKT-MODUS (Empfehlung "Logvolumen reduzieren"):
    # Volle Bewertung nur bei Aenderung einer EIN/AUS-Entscheidung oder als
    # staendlicher Snapshot (alle 60 min). Details: entscheidungs_log.jsonl.
    if _soll_priority_loggen(state, alle_ergebnisse):
        stale_marker = "[STALE] " if _solar_daten_veraltet(state) else ""
        logging.info(
            f"Regel-Bewertung {stale_marker}({len(alle_ergebnisse)} Regeln):\n{formatiere_ergebnisse(alle_ergebnisse)}"
        )

    # Gewinner-Regel in State speichern fuer Anzeige
    if not state.control.kompressor_ein:
        state.control.active_rule_sensor = None
    # `requested_rule_name` darf eine wartende Regel sein. Die beiden
    # `effective_*`-Felder beschreiben dagegen ausschließlich einen laufenden
    # Hardware-Zyklus und werden bei AUS nicht aus dem letzten Modus geerbt.
    if gewinner is None:
        waiting = next(
            (
                e for e in alle_ergebnisse
                if e.aktiv and e.einschalten is None
                and getattr(e, "reason_code", "") == "waiting_source"
            ),
            None,
        )
        state.control.requested_rule_name = waiting.name if waiting else None
    else:
        state.control.requested_rule_name = gewinner.name

    state.control.source_current = _aktuelle_quellenbezeichnung(state)
    if state.control.kompressor_ein:
        state.control.effective_rule_name = (
            getattr(state.control, "effective_rule_name", None)
            or getattr(state.control, "_lauf_start_regel", None)
            or getattr(state.control, "previous_modus", None)
        )
        state.control.active_rule_name = state.control.effective_rule_name
        state.control.effective_source = (
            getattr(state.control, "source_at_start", None)
            or state.control.source_current
        )
    else:
        state.control.effective_rule_name = None
        state.control.active_rule_name = None
        state.control.effective_source = None
        state.control.source_at_start = None

    if gewinner is not None:
        if gewinner.name == state.control.effective_rule_name:
            # Sensor-Name aus der aktuell bestätigten Regel ermitteln.
            if "mitte" in gewinner.grund.lower():
                state.control.active_rule_sensor = "Mittig"
            elif "unten" in gewinner.grund.lower():
                state.control.active_rule_sensor = "Unten"
            elif "oben" in gewinner.grund.lower():
                state.control.active_rule_sensor = "Oben"

        # Ein/Ausschaltpunkte aus Regel ermitteln.
        # WICHTIG: effektive_config (mit Bademodus-/Urlaubs-Offsets) verwenden,
        # nicht state.priority_config - sonst melden die Setpoints einen
        # anderen Sollwert, als die Regeln tatsaechlich ausgewertet haben.
        if gewinner.einschalten is True:
            eps = _extract_einschaltpunkt(gewinner, effektive_config)
            ausp = _extract_ausschaltpunkt(gewinner, effektive_config)
            state.control.aktueller_einschaltpunkt = eps
            state.control.aktueller_ausschaltpunkt = ausp
        else:
            # Regel sagt AUS: korrekte Setpoints aus der Regel extrahieren,
            # damit handle_compressor_off() den Kompressor auch abschalten kann.
            # Wenn wir hier max_temp_c setzen, wuerde der Kompressor nie ausschalten,
            # weil z.B. t_unten=43.2C < max_temp_c=48C.
            eps = _extract_einschaltpunkt(gewinner, effektive_config)
            ausp = _extract_ausschaltpunkt(gewinner, effektive_config)
            state.control.aktueller_einschaltpunkt = max(
                eps, ausp
            )  # hoch, damit kein Neueinschalten
            state.control.aktueller_ausschaltpunkt = (
                ausp  # korrekt, damit Abschaltung funktioniert
            )
    else:
        # Keine Regel will einschalten: Standard = ausschalten
        state.control.aktueller_einschaltpunkt = (
            state.priority_config.sicherheit.max_temp_c
        )
        state.control.aktueller_ausschaltpunkt = (
            state.priority_config.sicherheit.max_temp_c
        )

    # Komfort-Einschaltstatus (fuer Komfort-Regel)
    state.control.komfort_aktiv = any(
        e.name == "Komfort" and e.aktiv and e.einschalten is True
        for e in alle_ergebnisse
    )

    # Alle Ergebnisse im State speichern fuer API/HTML-Anzeige
    state.control.alle_ergebnisse = alle_ergebnisse

    # Ergebnis aufbereiten
    should_on = gewinner is not None and gewinner.einschalten is True

    # PV-Weiterlauf-Band (Empfehlung 3.2): Kurze Wolken/PV-Einbrueche beenden
    # einen laufenden PV-Zyklus nicht sofort (verhindert 10-Minuten-Takte).
    should_on = _pv_weiterlauf_block(state, gewinner, should_on)

    # Regelfuehler dynamisch aus der aktiven Regel ermitteln, nicht hartcodiert t_unten
    regelfuehler = t_unten  # Fallback
    if gewinner is not None:
        if (
            "unten" in gewinner.grund.lower()
            or "unten" in (gewinner.name or "").lower()
        ):
            regelfuehler = t_unten
        elif (
            "mitte" in gewinner.grund.lower()
            or "mitte" in (gewinner.name or "").lower()
        ):
            regelfuehler = t_mittig
        elif (
            "oben" in gewinner.grund.lower() or "oben" in (gewinner.name or "").lower()
        ):
            regelfuehler = state.sensors.t_oben

    # Schichtungs-Warmstart: Der Gewinner (Abweichung) kann eine dynamische
    # Obergrenze fuer die obere Schicht mitgeben (nach Legionellenmodus etc.).
    # Sie wird an state.control gespiegelt, damit handle_compressor_off() den
    # Lauf stoppen kann, sobald oben die Grenze erreicht - ohne dass die WP
    # dauerhaft blockiert wird. Eine bestehende Grenze bleibt erhalten, solange
    # der Kompressor laeuft (auch bei anderen Gewinnern wie PV/Batterie).
    try:
        if gewinner is not None and gewinner.regel_dict:
            _sd = gewinner.regel_dict
            if "schichtung_oben_max" in _sd:
                state.control.schichtung_oben_max = float(_sd["schichtung_oben_max"])
                state.control.schichtung_oben_start = float(
                    _sd.get("schichtung_oben_start", state.sensors.t_oben or 0.0)
                )
    except Exception:
        # Bei MagicMock/Fehlern defensiv zuruecksetzen
        state.control.schichtung_oben_max = None
        state.control.schichtung_oben_start = None

    # Solarueberschuss aktiv wenn PV-Leistung >= niedrigste Schwelle aller PV-Regeln
    if state.priority_config.pv_regeln:
        min_pv_schwelle = min(
            r.pv_schwelle_watt for r in state.priority_config.pv_regeln
        )
        solar_ueberschuss_aktiv = pv_leistung >= min_pv_schwelle
    else:
        solar_ueberschuss_aktiv = False

    res = {
        "modus": gewinner.name if gewinner else "Keine Regel aktiv",
        "einschaltpunkt": state.control.aktueller_einschaltpunkt,
        "ausschaltpunkt": state.control.aktueller_ausschaltpunkt,
        "regelfuehler": regelfuehler,
        "solar_ueberschuss_aktiv": solar_ueberschuss_aktiv,
        "soll_einschalten": should_on,
        "gewinner_ergebnis": gewinner,
        "alle_ergebnisse": alle_ergebnisse,
    }

    # Moduswechsel-Logging + Taktschutz-Tracking - erst nach Bestaetigung
    # durch das Debouncing (siehe _gewinner_debounce): Ein-Sensor-Ticks an
    # Regel-Grenzen erzeugen weder Log-Zeilen noch Taktschutz-Zaehler.
    #
    # Debounce-Schaltverzug (Nutzeranforderung): `soll_einschalten` wird erst
    # an die Hardware uebergeben, wenn der Gewinner-Moduswechsel per Debounce
    # bestaetigt ist. Solange ein neuer Gewinner nur "pending" ist, gilt die
    # letzte bestaetigte Schaltempfehlung (verhindert Hardware-Takten an
    # Regel-Kanten).
    modus = res["modus"]
    prev_modus = getattr(state.control, "previous_modus", None)

    if gewinner is not None and modus != prev_modus:
        ist_bestaetigt = bool(_gewinner_debounce(state, modus))
    else:
        # Kein Wechsel / keine Regel aktiv: Empfehlung sofort uebernehmen und
        # einen ggf. pendelnden Pending-Zustand aufloesen.
        ist_bestaetigt = True
        try:
            state.control._pending_modus = None
        except Exception:
            pass

    if ist_bestaetigt:
        state.control.previous_modus = modus
        # effective_rule_name/active_rule_name bleiben hier unverändert.
        # Erst handle_compressor_on bestätigt sie nach erfolgreichem
        # Hardware-Start; laufende Zyklen behalten ihre Startquelle.
        state.control._soll_einschalten_bestaetigt = bool(should_on)
        if modus != prev_modus:
            logging.info(
                f"Wechsel zu Regel: {res['modus']} ({'EIN' if should_on else 'AUS'})"
            )
            # Taktschutz (Punkt D): nur bestaetigte Wechsel tracken
            _track_wechsel(state, modus)
        soll_an_hardware = bool(should_on)
    else:
        # Wechsel noch nicht bestaetigt: letzte bestaetigte Empfehlung behalten
        soll_an_hardware = bool(
            getattr(state.control, "_soll_einschalten_bestaetigt", False)
        )

    res["soll_einschalten"] = soll_an_hardware

    # Entscheidungs-Historie (JSONL) fuer Webapp/KPIs - darf nie blockieren
    if entscheidungs_log is not None:
        try:
            # Alter der Solax-Daten (STALE-Kennzeichnung fuer Analysen, 3.5)
            stale_s = None
            _last_call = getattr(state.solar, "last_api_call", None)
            if _last_call is not None:
                try:
                    stale_s = int(max(
                        (_now_for_state(state) - _last_call).total_seconds(),
                        0,
                    ))
                except (TypeError, ValueError):
                    stale_s = None
            entscheidungs_log.schreibe_eintrag(
                gewinner_name=gewinner.name if gewinner else None,
                gewinner_grund=gewinner.grund if gewinner else "",
                soll_einschalten=bool(soll_an_hardware),
                kompressor_laeuft=bool(state.control.kompressor_ein),
                feedin_watt=pv_leistung,
                batpower_watt=getattr(state.solar, "batpower", None),
                soc=getattr(state.solar, "soc", None),
                t_unten=t_unten,
                t_oben=getattr(state.sensors, "t_oben", None),
                stale_s=stale_s,
                reason_code=getattr(gewinner, "reason_code", None) if gewinner else None,
                ts=_now_for_state(state),
                diagnostics={
                    "reason_code": getattr(gewinner, "reason_code", None) if gewinner else None,
                    "requested_rule": getattr(state.control, "requested_rule_name", None),
                    "effective_rule": getattr(state.control, "effective_rule_name", None),
                    "source_at_start": getattr(state.control, "source_at_start", None),
                    "forecast_stale": bool(getattr(state, "forecast_stale", False)),
                    "forecast_age_s": getattr(state, "forecast_age_s", None),
                    "rate_confidence": getattr(state.control, "_rate_confidence", None),
                    "start_anticipation": getattr(state.control, "_last_start_anticipation", {}),
                },
            )
        except Exception as e:  # pragma: no cover
            logging.debug(f"Entscheidungslog-Fehler: {e}")

    return res


def wende_sommer_offset_an(config) -> None:
    """Wendet die Sommermodus-Absenkungen auf eine (Kopie-)Config an.

    Hintergrund (Nutzeranforderung): Bei mehrtaetig gutem PV-Wetter soll der
    Boiler-Buffer bewusst NICHT voll aufgebaut werden - die maximale
    Temperatur wird minimiert, da morgen wieder genug PV-Strom kommt.

    - abweichung.solltemperatur_c += temperatur_offset_c (bisheriges Verhalten)
    - NEU: PV-Regeln + AdaptivePV: ausschalten_bei_c += pv_ausschalt_offset_c
      (PV-Shaping laeuft bei Dauer-Sonne z.B. nur bis 46 statt 48C),
      geklemmt, damit die Einschalthysterese erhalten bleibt.
    """
    offset = config.sommer_modus.temperatur_offset_c
    config.abweichung.solltemperatur_c += offset

    pv_offset = config.sommer_modus.pv_ausschalt_offset_c
    einschalt_offset = getattr(
        config.sommer_modus, "pv_einschalt_offset_c", pv_offset
    )
    for pv in config.pv_regeln:
        neuer_aus = pv.ausschalten_bei_c + pv_offset
        # Klemme: Ausschaltpunkt bleibt mind. 2K ueber dem Einschaltpunkt
        pv.ausschalten_bei_c = max(neuer_aus, pv.einschalten_bei_c + 2.0)
        # NEU (Sommer-Hysterese): Einschaltpunkt synchron senken (42 -> 40C),
        # damit die Hysterese im Sommer nicht schrumpft und es nicht taktet.
        pv.einschalten_bei_c = max(pv.einschalten_bei_c + einschalt_offset, 20.0)

    # --- NEU: Auch Einspeisung im Sommermodus kappen (wie PV-Regeln) ---
    # Die Einspeisung-Regel (Prio 83) ist die hoechste Heiz-Prioritaet und
    # heizte bisher ungebremst bis 48C, obwohl der Boiler im Sommer jeden
    # Tag wieder PV bekommt. Mit diesem Offset senkt sie ihr Ziel auf 46C
    # (ausschalten_bei_c=48 + pv_ausschalt_offset_c=-2 = 46C).
    einsp_aus = config.einspeisung.ausschalten_bei_c + pv_offset
    # Klemme: Ausschaltpunkt nicht unter 42C (Regel hat keinen eigenen EP)
    config.einspeisung.ausschalten_bei_c = max(einsp_aus, 42.0)

    apv = config.adaptive_pv
    # Absoluter Boden 42C, damit die Regel nicht wirkungslos/kippelig wird
    apv.tmax_c = max(apv.tmax_c + pv_offset, 42.0)


def _extract_einschaltpunkt(
    ergebnis: RegelErgebnis, config: WPSteuerungConfig
) -> float:
    """Extrahiert den Einschaltpunkt aus dem Regel-Ergebnis fuer Statusanzeige."""
    name = ergebnis.name

    if name.startswith("PV_"):
        # PV-Regel: Finde die passende
        for pv in config.pv_regeln:
            if pv.name == name:
                return pv.einschalten_bei_c
    elif name == "Komfort":
        return config.komfort.komfort_einschalten_bei_c
    elif name == "Zeitfenster":
        return config.zeitfenster.max_temp_fuer_einschalten_c
    elif name == "Abweichung":
        return (
            config.abweichung.solltemperatur_c
            - config.abweichung.einschalten_bei_abweichung_k
        )
    elif name == "Forecast":
        return config.forecast.t_vorheiz_ab_c
    elif name == "AdaptivePV":
        adaptive_cfg = getattr(config, "adaptive_pv", None)
        direct = getattr(adaptive_cfg, "einschalten_bis_c", None)
        if isinstance(direct, (int, float)) and not isinstance(direct, bool):
            return float(direct)
        t_max = getattr(adaptive_cfg, "tmax_c", None)
        if isinstance(t_max, (int, float)) and not isinstance(t_max, bool):
            return float(t_max) - 3.0
        return config.sicherheit.max_temp_c
    elif name == "CalcStart":
        return config.calculated_start.solltemperatur_c
    elif name.startswith("MinTemp-"):
        for eintrag in config.mindest_temp.eintraege:
            if name == f"MinTemp-{eintrag.name}":
                return eintrag.min_temp_c
    elif name == "Batterie":
        return config.batterie.einschalten_bei_c
    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert
    elif name == "Notfallschutz":
        return config.notfallschutz.einschalten_bei_c
    elif name == "Legionellen":
        return config.legionellen.target_temp_c - 5.0  # EIN unter 55C

    return config.sicherheit.max_temp_c


def _extract_ausschaltpunkt(
    ergebnis: RegelErgebnis, config: WPSteuerungConfig
) -> float:
    """Extrahiert den Ausschaltpunkt aus dem Regel-Ergebnis fuer Statusanzeige."""
    name = ergebnis.name

    if name.startswith("PV_"):
        for pv in config.pv_regeln:
            if pv.name == name:
                return pv.ausschalten_bei_c
    elif name == "Komfort":
        return config.komfort.ausschalten_bei_c
    elif name == "Zeitfenster":
        return config.zeitfenster.max_temp_fuer_einschalten_c
    elif name == "Abweichung":
        return (
            config.abweichung.solltemperatur_c
            - config.abweichung.ausschalten_bei_abweichung_k
        )
    elif name == "Forecast":
        return config.forecast.tmax_c
    elif name == "AdaptivePV":
        return config.adaptive_pv.tmax_c
    elif name == "CalcStart":
        return config.calculated_start.tmax_c
    elif name.startswith("MinTemp-"):
        for eintrag in config.mindest_temp.eintraege:
            if name == f"MinTemp-{eintrag.name}":
                return eintrag.min_temp_c + eintrag.hysterese_k
    elif name == "Batterie":
        return config.batterie.ausschalten_bei_c
    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c
    elif name == "Notfallschutz":
        return config.notfallschutz.ausschalten_bei_c
    elif name == "Legionellen":
        return config.legionellen.target_temp_c

    return config.sicherheit.max_temp_c


def _gelerntes_morgenfenster_mit_bonus(state, learning_engine):
    """Morgenfenster + Komfort-Bonus (Punkt B): Bei Verletzungen 0.5h Vorlauf."""
    fenster = _gelerntes_morgenfenster(learning_engine)
    if fenster is None:
        return None
    bonus = 0.0
    if learning_engine is not None:
        bonus = getattr(learning_engine, "get_komfort_bonus_vorlauf", lambda: 0.0)()
    if bonus > 0:
        fruehe, spaete = fenster
        fenster = (max(fruehe - bonus, 4.5), spaete)
    return fenster


def _track_wechsel(state, gewinner_name):
    """Trackt echte Entscheidungswechsel (Punkt D) im letzten 60min-Fenster.

    Nur tatsaechliche Wechsel des Gewinners werden erfasst, nicht jede
    Bewertung (siehe Kommentar unten).
    """
    now = _now_for_state(state)
    hist = getattr(state.control, "_wechsel_historie", None)
    if not isinstance(hist, deque):
        hist = deque()
        try:
            state.control._wechsel_historie = hist
        except Exception:
            pass
    # Alte Eintraege immer entfernen - auch ohne neuen Wechsel
    grenze = now - timedelta(hours=1)
    while hist and hist[0][0] < grenze:
        hist.popleft()
    # Nur echte Wechsel erfassen: erster Eintrag oder anderer Gewinner als
    # zuvor. Der Loop laeuft alle ~13 s - ohne diesen Vergleich waechst der
    # Zaehler mit jedem Durchlauf und der Taktschutz feuert dauerhaft,
    # obwohl der Kompressor gar nicht getaktet hat.
    if hist and hist[-1][1] == gewinner_name:
        return
    hist.append((now, gewinner_name))


def _gewinner_debounce(state, modus):
    """Bestaetigt einen Gewinnerwechsel erst nach 2 identischen Bewertungen.

    Hintergrund: Mehrere Regeln teilen sich scharfe Kanten (z.B. Batterie-EIN
    bei unten <= 42.0C gegen Komfort-AUS bei unten >= 42.0C). Sensor-/SOC-
    Jitter an diesen Kanten liess den Gewinner sonst im Sekundentakt pendeln -
    jeder Hub war ein echter Wechsel fuer Taktschutz und Log.
    """
    control = getattr(state, "control", None)
    if control is None:
        return True  # ohne Control-Objekt kein Debouncing moeglich
    if modus == getattr(control, "previous_modus", None):
        control._pending_modus = None
        return False
    pend = getattr(control, "_pending_modus", None)
    name, cnt = pend if isinstance(pend, tuple) else (None, 0)
    if name == modus:
        cnt += 1
    else:
        name, cnt = modus, 1
    try:
        control._pending_modus = (name, cnt)
    except Exception:
        return True
    if cnt >= 2:
        control._pending_modus = None
        return True
    return False


def _taktschutz_blockiert(state, cfg) -> float:
    """Prueft echte Hardware-Schaltvorgaenge und wendet die Zusatzpause an.
    Returns: zusaetzliche Pause in Sekunden (0 = keine Blockade).

    Meldungen erscheinen nur beim Uebergang (Episode startet/endet), nicht
    bei jedem Loop-Durchlauf.
    """
    ts_cfg = getattr(cfg, "taktschutz", None)
    if ts_cfg is None or not getattr(ts_cfg, "aktiv", False):
        return 0.0
    # Regelwechsel bleiben diagnostisch erhalten, fuehren aber nicht mehr zur
    # Hardware-Pause. Produktiv zaehlt nur die in set_kompressor_status()
    # aufgezeichnete Hardware-Historie. Der Fallback dient alten States/Tests.
    hardware_hist = getattr(state.control, "_hardware_wechsel_historie", None)
    hist = hardware_hist if isinstance(hardware_hist, deque) else getattr(
        state.control, "_wechsel_historie", deque()
    )
    kontrolle = getattr(state, "control", None)

    def _merker(wert):
        if kontrolle is not None:
            try:
                kontrolle._taktschutz_aktiv_merker = wert
            except Exception:
                pass

    def _episode_beenden():
        if getattr(kontrolle, "_taktschutz_aktiv_merker", False):
            logging.info(
                f"Taktschutz beendet: weniger als "
                f"{ts_cfg.max_wechsel_pro_stunde} Wechsel/h"
            )
            _merker(False)

    if len(hist) < ts_cfg.max_wechsel_pro_stunde:
        _episode_beenden()
        return 0.0
    # Sind die Wechsel innerhalb der letzten Stunde?
    now = _now_for_state(state)
    grenze = now - timedelta(hours=1)
    aktuelle = sum(1 for ts, _ in hist if ts >= grenze)
    if aktuelle >= ts_cfg.max_wechsel_pro_stunde:
        if not getattr(kontrolle, "_taktschutz_aktiv_merker", False):
            # Nur beim Uebergang in die Episode melden - nicht jeden Loop
            logging.warning(
                f"Taktschutz aktiv: {aktuelle} Wechsel/h >= "
                f"{ts_cfg.max_wechsel_pro_stunde}, zusaetzliche Pause "
                f"{ts_cfg.zusatz_pause_minuten} min (Meldung 1x pro Episode)"
            )
            _merker(True)
        return ts_cfg.zusatz_pause_minuten * 60.0
    _episode_beenden()
    return 0.0


def _boiler_max_info(state):
    """Infos zum harten Boiler-Maximum: (temp, limit, wiederein, fuehler).

    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
    Bei aktiver Legionellenprophylaxe wird das Limit auf
    legionellen_max_temp_c erhoeht (wenn gesetzt).
    """
    cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if cfg is None:
        return None, None, None, "unten"
    fuehler = getattr(cfg, "boiler_max_fuehler", None) or "unten"
    temp = getattr(getattr(state, "sensors", None), f"t_{fuehler}", None)
    if temp is None or not isinstance(temp, (int, float)):
        return None, None, None, fuehler
    # Standard-Limit aus Config
    limit = float(getattr(cfg, "max_temp_c", 48.0))
    # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der
    # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum
    # zuschlaegt.
    legionellen_limit = getattr(state, "legionellen_temp_override", None)
    if legionellen_limit is not None and legionellen_limit > limit:
        limit = legionellen_limit

    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
    return temp, limit, wiederein, fuehler


def _zyklus_id(state) -> str:
    """Lesbarer Zyklus-ID-String fuer Event-Kodierung (robust gegen Mock-State)."""
    try:
        return str(getattr(state.control, "zyklus_id", "?"))
    except Exception:
        return "?"


def _rate_fuer_entscheidung(state, t_unten):
    """Robuste Heizrate (Median der jüngsten Messfenster) + Confidence.

    Ein einzelner Sensor-Tick kann die Startentscheidung stark verfälschen.
    Deshalb werden bis zu fünf positive Live-Messungen gespeichert und der
    Median der letzten drei Werte verwendet. Die Confidence ist 0.1 bei
    Fallback, 0.4 bei gelernter Rate und bis 1.0 bei mindestens drei
    plausiblen Live-Messungen.
    """
    jetzt = _now_for_state(state)
    messung = getattr(state.control, "_rate_messung", None)
    samples = getattr(state.control, "_rate_messungen", None)
    if not isinstance(samples, deque):
        samples = deque(maxlen=5)
        state.control._rate_messungen = samples

    live_rate = None
    if isinstance(t_unten, (int, float)) and not isinstance(t_unten, bool):
        if isinstance(messung, dict):
            try:
                dt_h = (jetzt - messung["ts"]).total_seconds() / 3600.0
                diff = t_unten - messung["unten"]
                if 0.03 <= dt_h <= 2.0 and diff > 0.05:
                    live_rate = diff / dt_h
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        state.control._rate_messung = {"ts": jetzt, "unten": t_unten}

    if live_rate is not None:
        samples.append((jetzt, float(live_rate)))
        # Zeitfenster begrenzen; die Schleife ist robust gegen Mock-Zeitstempel.
        while len(samples) > 5:
            samples.popleft()

    werte = [float(r) for _, r in samples if isinstance(r, (int, float)) and r > 0]
    if werte:
        # Die Reihenfolge ist zeitlich (deque). Den Median der jüngsten drei
        # Werte bilden, nicht die drei größten historischen Raten.
        werte = werte[-3:]
        n = len(werte)
        middle = n // 2
        rate = werte[middle] if n % 2 else (werte[middle - 1] + werte[middle]) / 2.0
        confidence = min(1.0, 0.4 + 0.2 * n)
    else:
        engine = getattr(state, "learning_engine", None)
        zyklen = getattr(getattr(engine, "data", None), "cycles", None)
        learned = None
        if zyklen and isinstance(zyklen[-1], dict):
            learned = zyklen[-1].get("rate_unten_c_h")
        if isinstance(learned, (int, float)) and learned > 0:
            rate = float(learned)
            confidence = 0.4
        else:
            cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
            fallback = getattr(cfg, "rate_fallback_c_h", 12.0)
            try:
                rate = float(fallback) if float(fallback) > 0 else 12.0
            except (TypeError, ValueError, OverflowError):
                rate = 12.0
            confidence = 0.1

    state.control._rate_confidence = round(float(confidence), 2)
    return float(rate)


def _fmt_float(wert, einheit="", nachkomma=0) -> str:
    """Formatiert einen Optional-float robust (None -> 'n/a')."""
    if wert is None:
        return "n/a"
    try:
        return f"{float(wert):.{nachkomma}f}{einheit}"
    except (TypeError, ValueError):
        return "n/a"


def _boiler_max_kontext(state) -> str:
    """Kontext fuer BOILERMAX-Warnungen (Empfehlung "WARNINGS anreichern").

    Fuegt der Warnung die aktive Regel, die aktuelle PV-Einspeisung, den
    Batterie-SOC sowie die zuletzt gemessene unten-Heizrate (aus der
    Learning-Engine, letzter abgeschlossener Zyklus) hinzu, damit die
    Abschaltung aus dem Log heraus vollstaendig nachvollziehbar ist.
    """
    aktive_regel = (
        getattr(state.control, "_lauf_start_regel", None)
        or getattr(state.control, "active_rule_name", None)
        or "unbekannt"
    )
    solar = getattr(state, "solar", None)
    feedin = getattr(solar, "feedinpower", None) if solar is not None else None
    soc = getattr(solar, "soc", None) if solar is not None else None
    rate = None
    engine = getattr(state, "learning_engine", None)
    zyklen = getattr(getattr(engine, "data", None), "cycles", None)
    if zyklen:
        letzter = zyklen[-1]
        if isinstance(letzter, dict):
            rate = letzter.get("rate_unten_c_h")
    return (
        f"Regel={aktive_regel} | "
        f"PV={_fmt_float(feedin, 'W')} | "
        f"SOC={_fmt_float(soc, '%')} | "
        f"Rate(letzter Lauf)={_fmt_float(rate, 'C/h', 1)}"
    )


def _ist_pv_gesteuerter_lauf(name) -> bool:
    """True, wenn der laufende Kompressor-Lauf von einer PV-/Einspeisung-
    Regel gestartet wurde (fuer die PV-Mindestlaufzeit-Entkopplung)."""
    if not isinstance(name, str) or not name:
        return False
    return name in ("Einspeisung", "AdaptivePV") or name.startswith("PV_")


def _effektive_mindestlaufzeit(state, min_laufzeit, regel_name=None) -> timedelta:
    """Effektive Mindestlaufzeit fuer den aktuellen Lauf.

    Die JSON-Fachkonfiguration ist die fuehrende Quelle: Netz-/Batterie-
    laeufe duerfen nicht auf einen veralteten INI-Wert verkuerzt werden.
    PV-/Einspeisungslaeufe duerfen dagegen nach der konfigurierten
    Hardware-Schutzzeit enden. PV_Mindestlaufzeit wird nur fuer PV_Regeln
    verwendet; Batterie bleibt bewusst im Schutzbereich der vollen
    Mindestlaufzeit.
    """
    basis = min_laufzeit if isinstance(min_laufzeit, timedelta) else timedelta()
    # Ohne benannte aktive Regel (z.B. in alten/vereinfachten Test-States)
    # bleibt der uebergebene Basiswert erhalten. Im Produktivbetrieb wird
    # active_rule_name vor handle_compressor_on gesetzt.
    if regel_name is None:
        return basis
    if _ist_pv_gesteuerter_lauf(regel_name):
        cfg = getattr(getattr(state, "priority_config", None), "zyklus", None)
        wert = getattr(cfg, "pv_min_laufzeit_minuten", None)
        if isinstance(wert, (int, float)) and not isinstance(wert, bool):
            return timedelta(minutes=max(float(wert), 0.0))

    cfg = getattr(getattr(state, "priority_config", None), "zyklus", None)
    json_wert = getattr(cfg, "mindestlaufzeit_minuten", None)
    if isinstance(json_wert, (int, float)) and not isinstance(json_wert, bool):
        return max(basis, timedelta(minutes=max(float(json_wert), 0.0)))
    return basis


def _effektive_ueberhitzung_schwelle(state) -> float:
    """Ueberhitzungsschwelle inkl. Legionellen-Bypass.

    Nutzeranforderung: Waehrend einer aktiven Legionellenfahrt wird
    ueberhitzung_c dynamisch auf legionellen_max_temp_c (z.B. 65C) angehoben,
    damit der Ueberhitzungsschutz die Prophylaxefahrt (Ziel 60/65C) nicht
    vorzeitig abbricht.
    """
    cfg = getattr(state, "priority_config", None)
    sicherheit = getattr(cfg, "sicherheit", None)
    base = getattr(sicherheit, "ueberhitzung_c", 58.0)
    if not isinstance(base, (int, float)):
        base = 58.0
    # Legionellen aktiv ODER temp_override gesetzt (robust gegen MagicMocks /
    # Neustart-Zustaende, in denen legionellen_aktiv evtl. erst im naechsten
    # Lifecycle-Zyklus wieder True ist).
    legionellen_betrieb = (
        getattr(state, "legionellen_aktiv", False) is True
        or getattr(state, "legionellen_temp_override", None) is not None
    )
    if legionellen_betrieb:
        lle_cfg = getattr(cfg, "legionellen", None)
        lle_max = getattr(lle_cfg, "legionellen_max_temp_c", None)
        if isinstance(lle_max, (int, float)) and float(lle_max) > float(base):
            return float(lle_max)
    return float(base)


async def handle_compressor_off(
    state,
    session,
    regelfuehler,
    ausschaltpunkt,
    min_laufzeit,
    t_oben,
    set_kompressor_status_func: Callable,
    regel_name=None,
):
    """Prueft Abschaltbedingungen und schaltet aus.

    regel_name: Name der Gewinner-Regel, die explizit AUS entschieden hat.
    None heisst: gar keine Regel aktiv (z.B. wegen Nachtsperre). Dient der
    sauberen Trennung in Log und Blocking-Reason - frueher lief beides unter
    "Keine Regel aktiv" und verschleierte die eigentliche Entscheidung."""
    if not state.control.kompressor_ein:
        return False

    # Absolute Sicherheitsgrenze (dynamisch: waehrend einer Legionellenfahrt
    # auf legionellen_max_temp_c angehoben)
    ueberhitz = _effektive_ueberhitzung_schwelle(state)
    if t_oben is not None and t_oben >= ueberhitz:
        if await set_kompressor_status_func(
            state, False, force=True, t_boiler_oben=t_oben,
            end_grund="ueberhitzungsschutz",
        ):
            state.control.blocking_reason = f"Ueberhitzungsschutz ({t_oben:.1f}C >= {ueberhitz:.1f}C)"
            logging.warning(f"SICHERHEIT AUS: Ueberhitzung ({t_oben:.1f}C)")
            return True
        await handle_critical_compressor_error(session, state, "bei Ueberhitzung")
        return False

    # Hartes Boiler-Maximum am Bezugsfuehler (Standard: unten).
    # Bricht die Mindestlaufzeit - Schutz geht vor Taktschutz. Ohne diesen
    # Bruch heizt der Kompressor nach Erreichen des Limits weiter und treibt
    # v.a. die obere Schicht unnoetig weiter hoch.
    t_max, limit, wiederein, fuehler = _boiler_max_info(state)
    if t_max is not None and t_max >= limit:
        if await set_kompressor_status_func(
            state, False, force=True, t_boiler_oben=t_oben,
            end_grund="boiler_max",
        ):
            state.control.boiler_max_blockiert = wiederein
            state.control.blocking_reason = (
                f"Boiler-Maximum ({fuehler} {t_max:.1f}C >= {limit:.1f}C)"
            )
            logging.warning(
                f"BOILERMAX AUS (cycle={_zyklus_id(state)}) - "
                f"{fuehler} {t_max:.1f}C >= {limit:.1f}C - "
                f"Mindestlaufzeit gebrochen, Freigabe erst <= {wiederein:.1f}C "
                f"reason=boiler_max [{_boiler_max_kontext(state)}]"
            )
            return True
        await handle_critical_compressor_error(session, state, "bei Boiler-Maximum")
        return False

    # AUS-Vorhersage (Empfehlung 3.1): Verhindert, dass die Mindestlaufzeit
    # den Kompressor ueber die Obergrenze treibt. Falls der Fuehler mit der
    # aktuellen Rate bis zum Laufzeit-Ende die Obergrenze erreicht, wird VOR
    # der 48.0-C-Grenze abgeschaltet - statt den Bruch bei 49C zu erleben.
    _sic_cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if (
        _sic_cfg is not None
        and getattr(_sic_cfg, "overshoot_vorhersage_aktiv", True)
        and t_max is not None
        and state.stats.last_compressor_on_time is not None
        # Nur relevant, wenn die Regel den Abschaltpunkt erreicht hat und die
        # Mindestlaufzeit den Nachlauf erzwungen wuerde (48 -> 49C).
        and regelfuehler is not None
        and regelfuehler >= ausschaltpunkt
    ):
        lauf_regel = getattr(state.control, "_lauf_start_regel", None)
        minz = _effektive_mindestlaufzeit(state, min_laufzeit, lauf_regel)
        rate_var = _rate_fuer_entscheidung(state, float(t_max))
        rate_schwelle = float(getattr(_sic_cfg, "overshoot_rate_schwelle_c_h", 12.0))
        reserve_k = float(getattr(_sic_cfg, "overshoot_reserve_k", 0.8))
        elapsed_var = safe_timedelta(
            _now_for_state(state),
            state.stats.last_compressor_on_time,
            state.local_tz,
        )
        rest_min = max((minz - elapsed_var).total_seconds(), 0) / 60.0
        if rate_var >= rate_schwelle and rest_min > 0:
            t_prognose = t_max + rate_var * (rest_min / 60.0)
            if t_prognose >= limit - reserve_k:
                if await set_kompressor_status_func(
                    state, False, force=True, t_boiler_oben=t_oben,
                    end_grund="overshoot_vorhersage",
                ):
                    state.control.blocking_reason = (
                        f"Overshoot-Vorhersage ({fuehler} {t_max:.1f}C + "
                        f"{rest_min:.0f}min x {rate_var:.0f}C/h "
                        f"-> {t_prognose:.1f}C >= {limit:.1f}C)"
                    )
                    logging.warning(
                        f"OVERSHOOT-VORHERSAGE AUS (cycle={_zyklus_id(state)}) "
                        f"reason=overshoot_vorhersage: {fuehler} {t_max:.1f}C steigt "
                        f"mit {rate_var:.0f}C/h auf ~{t_prognose:.1f}C "
                        f"(Limit {limit:.1f}C, Reserve {reserve_k:.1f}K) "
                        f"[{_boiler_max_kontext(state)}]"
                    )
                    return True

    # Schichtungs-Warmstart-Obergrenze (Task: nach Legionellenmodus oben heiss,
    # unten/mitte kalt -> einschalten erlaubt, aber oben darf nicht weiter
    # stark steigen). Der Wert kommt aus dem Abweichungs-gewinn (regel_dict)
    # und wurde in determine_mode_and_setpoints nach state.control gespiegelt.
    schichtung_max = getattr(state.control, "schichtung_oben_max", None)
    if (
        schichtung_max is not None
        and t_oben is not None
        and isinstance(schichtung_max, (int, float))
        and t_oben >= float(schichtung_max)
    ):
        start = getattr(state.control, "schichtung_oben_start", None)
        steig_txt = (
            f" (Start {float(start):.1f}C)" if isinstance(start, (int, float)) else ""
        )
        if await set_kompressor_status_func(
            state, False, force=True, t_boiler_oben=t_oben,
            end_grund="schichtung",
        ):
            state.control.blocking_reason = (
                f"Schichtungs-Obergrenze ({t_oben:.1f}C >= {float(schichtung_max):.1f}C{steig_txt})"
            )
            logging.info(
                f"SCHICHTUNG AUS (cycle={_zyklus_id(state)}) reason=schichtung: "
                f"oben {t_oben:.1f}C >= {float(schichtung_max):.1f}C{steig_txt}"
            )
            # Obergrenze zuruecksetzen, damit der naechste normale Lauf nicht
            # durch eine alte Grenze begrenzt wird.
            state.control.schichtung_oben_max = None
            state.control.schichtung_oben_start = None
            return True
        return False

    # --- NEU: Keine Regel aktiv -> Kompressor ausschalten ---
    # Wenn keine Regel den Kompressor einschalten will (z.B. wegen Nachtsperre),
    # muss der Kompressor ausgeschaltet werden, auch wenn der regelfuehler
    # noch unter dem ausschaltpunkt liegt.
    should_on = getattr(state.control, "_soll_einschalten", False)
    if not should_on:
        # Zwei Faelle, die hier sauber getrennt werden: Eine Regel hat explizit
        # AUS entschieden (regel_name gesetzt) ODER gar keine Regel ist aktiv
        # (regel_name None, z.B. Nachtsperre). Verhalten identisch, Text ehrlich.
        if regel_name is not None:
            kontext = f"Regel '{regel_name}' sagt AUS"
        else:
            kontext = "Keine Regel aktiv"

        # Pruefe ob wir schon laenger als die Mindestlaufzeit laufen.
        # PV-Mindestlaufzeit entkoppeln (Nutzeranforderung): Wurde der Lauf von
        # einer PV-/Einspeisung-Regel gestartet, reicht nach Ablauf der
        # Hardware-Schutzzeit (zyklus.pv_min_laufzeit_minuten, 10-15 min) ein
        # PV-Einbruch zum Abschalten - die volle Mindestlaufzeit (60 min) darf
        # dann keinen Netzbezug erzwingen.
        # Fuer die Abschaltung zaehlt die Regel, die den Lauf gestartet hat.
        # Ein spaeterer Gewinner (z.B. Komfort) darf die Hardware-Schutzzeit
        # eines PV-Laufs nicht wieder auf den vollen INI-Wert zuruecksetzen.
        lauf_regel = getattr(state.control, "_lauf_start_regel", None) or regel_name
        min_laufzeit_eff = _effektive_mindestlaufzeit(
            state, min_laufzeit, lauf_regel
        )

        elapsed = safe_timedelta(
            _now_for_state(state),
            state.stats.last_compressor_on_time,
            state.local_tz,
        )
        if elapsed >= min_laufzeit_eff:
            if await set_kompressor_status_func(
                state, False, force=True, t_boiler_oben=t_oben,
                end_grund="regel_aus",
            ):
                state.control.blocking_reason = None
                state.control._lauf_start_regel = None  # Fahrt beendet
                logging.info(
                    f"{kontext}: Kompressor AUS (cycle={_zyklus_id(state)}) "
                    f"reason=regel_aus. Laufzeit: {elapsed}"
                )
                return True
        else:
            remaining_min = int((min_laufzeit_eff - elapsed).total_seconds() // 60)
            state.control.blocking_reason = (
                f"{kontext}, warte auf Mindestlaufzeit (noch {remaining_min}m)"
            )
            throttle_key = (
                "log_min_laufzeit_regel_aus"
                if regel_name is not None
                else "log_min_laufzeit_keine_regel"
            )
            if check_log_throttle(state, throttle_key, interval_minutes=5):
                logging.info(
                    f"{kontext}, aber Mindestlaufzeit noch nicht erreicht. "
                    f"Laufzeit: {elapsed}"
                )
        return False

    # Regel-basiertes Ausschalten
    if regelfuehler is not None and regelfuehler >= ausschaltpunkt:
        lauf_regel = getattr(state.control, "_lauf_start_regel", None) or regel_name
        min_laufzeit_eff = _effektive_mindestlaufzeit(
            state, min_laufzeit, lauf_regel
        )
        elapsed = safe_timedelta(
            _now_for_state(state),
            state.stats.last_compressor_on_time,
            state.local_tz,
        )
        if elapsed >= min_laufzeit_eff:
            if await set_kompressor_status_func(
                state, False, force=True, t_boiler_oben=t_oben,
                end_grund="regel_aus",
            ):
                state.control.blocking_reason = None
                logging.info(
                    f"Regel AUS (cycle={_zyklus_id(state)}) reason=regel_aus: "
                    f"Regelfuehler ({regelfuehler:.1f}) >= Ziel ({ausschaltpunkt:.1f}). "
                    f"Laufzeit: {elapsed}"
                )
                return True
            await handle_critical_compressor_error(session, state, "")
        else:
            remaining_min = int((min_laufzeit_eff - elapsed).total_seconds() // 60)
            state.control.blocking_reason = (
                f"Warte auf Mindestlaufzeit (noch {remaining_min}m)"
            )
            if check_log_throttle(state, "log_min_laufzeit_off", interval_minutes=5):
                logging.info(
                    f"Abschaltwunsch unterdrueckt (cycle={_zyklus_id(state)}) "
                    f"reason=mindestlaufzeit: "
                    f"Mindestlaufzeit noch nicht erreicht. Laufzeit: {elapsed}"
                )
    return False


async def handle_compressor_on(
    state,
    session,
    regelfuehler,
    einschaltpunkt,
    ausschaltpunkt,
    min_laufzeit,
    min_pause,
    t_oben,
    t_mittig,
    set_kompressor_status_func: Callable,
):
    """Prueft Einschaltbedingungen und schaltet ein."""
    now = _now_for_state(state)

    # Schichtungs-Warmstart: Wenn ein neuer Lauf beginnt und KEINE gÃƒÆ’Ã‚Â¼ltige
    # Obergrenze vorliegt (z.B. normaler Abweichungslauf ohne warmes Ober),
    # eine evtl. alte Grenze aus einem vorherigen Lauf entfernen - sie darf
    # einen neuen Lauf nicht unnÃƒÆ’Ã‚Â¶tig begrenzen.
    hat_grenze = getattr(state.control, "schichtung_oben_max", None) is not None
    if not state.control.kompressor_ein and not hat_grenze:
        state.control.schichtung_oben_max = None
        state.control.schichtung_oben_start = None

    # Boiler-Maximum-Kuehlphase: Nur nach einem tatsaechlichen Limit-Abschalten
    # aktiv (Flag boiler_max_blockiert). Der normale EIN-Bereich unterhalb des
    # Limits bleibt unangetastet - bewusst KEINE pauschale Hysterese, damit
    # z.B. PV-Heizen bei unten 47 C weiter moeglich bleibt.
    schwelle = getattr(state.control, "boiler_max_blockiert", None)
    if schwelle is not None:
        t_max, _limit, _wiederein, fuehler = _boiler_max_info(state)
        if t_max is not None and t_max > schwelle:
            state.control.blocking_reason = (
                f"Boiler-Maximum-Kuehlphase ({fuehler} {t_max:.1f}C, "
                f"Einschalten erst <= {schwelle:.1f}C)"
            )
            return False
        state.control.boiler_max_blockiert = None  # abgekuehlt -> freigegeben

    # Ein-Sperre in Limitnaehe (Anforderung 2026-08-26, Default 2K):
    # Reicht der Bezugsfuehler schon dicht an das Maximum heran, erreicht ein
    # Start das harte Limit noch innerhalb der Mindestlaufzeit - die Folge
    # waere ein Kurzlauf mit gebrochener Laufzeit und verschenktem Puffer
    # durch die anschliessende Kuehlphase. Diese Sperre greift dauerhaft im
    # Naehbereich (unabhaengig vom Kuehlphase-Flag) und hebt sich automatisch
    # auf, sobald wieder boiler_max_ein_abstand_k Luft zum Limit besteht.
    _sicher_cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    ein_abstand = float(
        getattr(_sicher_cfg, "boiler_max_ein_abstand_k", BOILER_MAX_EIN_ABSTAND_K)
    )
    t_nahe, nahe_limit, _kuehl_schwelle, fuehler_nahe = _boiler_max_info(state)
    t_mittig = getattr(getattr(state, "sensors", None), "t_mittig", None)
    # Nur blockieren wenn SOWOHL unten ALS AUCH mittig nahe am Limit sind
    # Wenn nur unten nahe am Limit aber mittig noch kalt ist -> nicht blockieren
    mittig_close = t_mittig is not None and t_mittig >= nahe_limit - ein_abstand if t_mittig is not None else False
    unten_close = t_nahe is not None and t_nahe >= nahe_limit - ein_abstand
    if unten_close and mittig_close:
        state.control.blocking_reason = (
            f"Boiler-Max-Naehe (unten {t_nahe:.1f}C, mittig {t_mittig:.1f}C, "
            f"Einschalten erst < {nahe_limit - ein_abstand:.1f}C)"
        )
        return False
    elif unten_close and not mittig_close:
        # Unten nahe am Limit aber mittig noch kalt -> nicht blockieren
        pass
    elif mittig_close and not unten_close:
        # Mittig nahe am Limit aber unten noch kalt -> nicht blockieren
        pass

    # Start-Antizipation (Empfehlung 3.1): Nur einschalten, wenn der freie
    # Hub bis zum Ausschaltpunkt reicht, um die Mindestlaufzeit inkl. Reserve
    # zu fuellen. Sonst entsteht ein Kurzlauf, der die Obergrenze erreicht und
    # in den erzwungenen Laufzeit-Bruch/Luehlphase laeuft.
    _sic_cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if (
        getattr(_sic_cfg, "start_vorhersage_aktiv", True)
        and isinstance(regelfuehler, (int, float))
        and min_laufzeit is not None
    ):
        rate_var = _rate_fuer_entscheidung(state, float(regelfuehler))
        _ausschalt = float(ausschaltpunkt or 0.0)
        hub_k = _ausschalt - float(regelfuehler)
        if hub_k > 0:
            erwartet_min = hub_k / max(rate_var, 1.0) * 60.0
            puffer_min = float(getattr(_sic_cfg, "start_vorhersage_puffer_min", 1.0))
            low_conf = float(getattr(_sic_cfg, "start_vorhersage_niedrig_confidence", 0.4))
            high_conf = float(getattr(_sic_cfg, "start_vorhersage_hoch_confidence", 0.7))
            rate_confidence = float(getattr(state.control, "_rate_confidence", 0.1))
            if rate_confidence < low_conf:
                confidence_category = "niedrig"
                extra_puffer = float(
                    getattr(_sic_cfg, "start_vorhersage_niedrig_zusatzpuffer_min", 2.0)
                )
            elif rate_confidence < high_conf:
                confidence_category = "mittel"
                extra_puffer = 0.0
            else:
                confidence_category = "hoch"
                extra_puffer = 0.0
            effective_puffer_min = puffer_min + extra_puffer
            minz = _effektive_mindestlaufzeit(
                state, min_laufzeit, getattr(state.control, "active_rule_name", None)
            )
            minz_min = float(minz.total_seconds() / 60.0)
            rate_confidence = float(getattr(state.control, "_rate_confidence", 0.1))
            state.control._last_start_anticipation = {
                "blocked": erwartet_min + effective_puffer_min < minz_min,
                "hub_k": round(hub_k, 2),
                "rate_c_h": round(rate_var, 2),
                "rate_confidence": round(rate_confidence, 2),
                "confidence_category": confidence_category,
                "expected_min": round(erwartet_min, 1),
                "effective_min": round(minz_min, 1),
                "buffer_min": round(puffer_min, 1),
                "extra_buffer_min": round(extra_puffer, 1),
                "effective_buffer_min": round(effective_puffer_min, 1),
            }
            if erwartet_min + effective_puffer_min < minz_min:
                state.control.blocking_reason = (
                    f"Start-Antizipation: hub zur Obergrenze nur {hub_k:.1f}K "
                    f"(Rate {rate_var:.0f}C/h, Confidence {rate_confidence:.0%} -> "
                    f"{erwartet_min:.0f}min < {minz_min:.0f}min Mindestlaufzeit + "
                    f"{effective_puffer_min:.0f}min Reserve, Konfidenz {confidence_category})"
                )
                if check_log_throttle(state, "log_start_vorhersage_block", 10):
                    logging.info(
                        f"Start blockiert reason=start_vorhersage "
                        f"(cycle={_zyklus_id(state)}): {state.control.blocking_reason}"
                    )
                return False

    # Taktschutz (Punkt D): Bei zu vielen Wechseln zusaetzliche Pause
    _ts_cfg = getattr(state, "priority_config", None)
    takt_pause = _taktschutz_blockiert(state, _ts_cfg)
    if takt_pause > 0 and min_pause.total_seconds() < takt_pause:
        min_pause_orig = min_pause
        min_pause = timedelta(seconds=takt_pause)
        if check_log_throttle(
            state, "log_taktschutz_verlaengerung", interval_minutes=30
        ):
            logging.info(
                f"Taktschutz verlaengert Pause von "
                f"{min_pause_orig.total_seconds()/60:.0f} auf {takt_pause/60:.0f} min"
            )

    # Explizite Neustartsperre (z.B. nach Verifizierungsfehler): blockiert
    # VOR der Mindestpausen-Pruefung, damit der Grund eindeutig im Log steht.
    lockout_until = getattr(state.control, "restart_lockout_until", None)
    if lockout_until is not None and now < lockout_until:
        rest = lockout_until - now
        mins = int(rest.total_seconds() // 60)
        secs = int(rest.total_seconds() % 60)
        state.control.blocking_reason = f"Neustartsperre (noch {mins}m {secs}s)"
        return False

    # Die JSON-Pareto-Konfiguration ist die fachliche Mindestpause. Die alte
    # INI-Sicht MIN_PAUSE kann aelter sein (Beispiel: 3 Min) und darf die
    # dokumentierten 30 Min nicht verkuerzen. Grosse INI-Werte bleiben wirksam.
    _zyklus_cfg = getattr(getattr(state, "priority_config", None), "zyklus", None)
    json_pause_min = getattr(_zyklus_cfg, "mindestpausenzeit_minuten", None)
    if isinstance(json_pause_min, (int, float)) and not isinstance(json_pause_min, bool):
        json_pause = timedelta(minutes=max(int(json_pause_min), 0))
        if json_pause > min_pause:
            min_pause = json_pause

    # Die Prioritaeten-Engine hat bereits entschieden
    # Wir muessen nur noch Mindestlaufzeit/-pause und Basis-Sicherheit pruefen

    pause_ok = True
    pause_remaining = None
    if state.stats.last_compressor_off_time:
        elapsed_pause = safe_timedelta(
            now, state.stats.last_compressor_off_time, state.local_tz
        )
        if elapsed_pause < min_pause:
            pause_ok = False
            pause_remaining = min_pause - elapsed_pause

    # Nur pruefen ob der Regelfuehler der aktiven Regel ueber Ausschaltpunkt liegt.
    # t_oben wird hier NICHT geprueft, da:
    #   1. check_safety_limits() bereits t_oben >= max_temp_c / ueberhitzung_c abfaengt
    #   2. Der Boiler stratifiziert ist - t_oben kann hoch sein waehrend unten noch kalt ist.
    stop_condition = regelfuehler is not None and regelfuehler >= ausschaltpunkt

    if not state.control.kompressor_ein:
        # Pruefe ob die Regel einschalten will (ueber state oder Aufruf-Parameter)
        should_on = getattr(state.control, "_soll_einschalten", False)

        if should_on and pause_ok:
            state.control._pending_start_rule = getattr(
                state.control, "requested_rule_name", None
            ) or getattr(state.control, "previous_modus", None)
            state.control._pending_start_source = getattr(
                state.control, "source_current", None
            )
            if stop_condition:
                state.control._pending_start_rule = None
                state.control._pending_start_source = None
                logging.info(
                    f"Einschalten unterdrueckt: Regelfuehler ({regelfuehler:.1f}) >= "
                    f"Ausschaltpunkt ({ausschaltpunkt:.1f})"
                )
                state.control.blocking_reason = "Zieltemp erreicht"
                return False

            pending_rule = getattr(state.control, "_pending_start_rule", None)
            pending_source = getattr(state.control, "_pending_start_source", None)
            if await set_kompressor_status_func(state, True, t_boiler_oben=t_oben):
                state.control._pending_start_rule = None
                state.control._pending_start_source = None
                state.control.blocking_reason = None
                state.control.restart_lockout_until = None  # Sperre erledigt
                # Erst nach erfolgreichem Hardware-Start wird die gewünschte
                # Regel zur effektiv wirksamen Hardware-Regel.
                start_regel = pending_rule
                if not start_regel:
                    start_regel = getattr(state.control, "requested_rule_name", None)
                if not start_regel:
                    start_regel = getattr(state.control, "previous_modus", None)
                _set_effective_cycle_rule(state, start_regel, pending_source)
                # Regelnamen + tatsaechliche Ausschaltgrenze loggen (nicht den
                # nur fuer die Anzeige abgeleiteten Einschaltpunkt - bei der
                # Einspeisungs-Regel waere das ein Dummy-Wert wie 42.0).
                aktive_regel = getattr(state.control, "active_rule_name", None)
                regel_txt = (
                    f"'{aktive_regel}'" if aktive_regel else "(keine Regel ermittelt)"
                )
                logging.info(
                    f"Eingeschaltet um {now}. Grund: Regel-Einschalt {regel_txt} "
                    f"(Regelfuehler={regelfuehler}, Ausschaltgrenze={ausschaltpunkt})"
                )
                return True

        # Blocking-Reason setzen wenn Bedingungen nicht erfuellt
        # Nur setzen, wenn ueberhaupt eine Regel einschalten will (sonst sinnlose Warnung)
        if not state.control.kompressor_ein:
            should_on = getattr(state.control, "_soll_einschalten", False)
            if should_on and not pause_ok and pause_remaining:
                minutes = int(pause_remaining.total_seconds() // 60)
                seconds = int(pause_remaining.total_seconds() % 60)
                state.control.blocking_reason = (
                    f"Min. Pause (noch {minutes}m {seconds}s)"
                )
            elif not should_on:
                state.control.blocking_reason = None

    return False


async def handle_mode_switch(
    state, session, t_oben, t_mittig, set_kompressor_status_func: Callable
):
    """
    Prueft ob bei Regelwechsel der Kompressor ausgeschaltet werden sollte.
    Wird nur bei Wechsel von EIN->EIN (andere Regel) relevant.
    """
    if not state.control.kompressor_ein:
        return False

    # Bei Prioritaeten-System: Der neue Regel-Zyklus bestimmt automatisch
    # ob weiterlaufen soll. Kein aktives Ausschalten noetig bei Moduswechsel.
    # Das handle_compressor_off uebernimmt die Temp-Logik.
    return False


async def check_safety_limits(
    session,
    state,
    t_oben,
    t_unten,
    t_mittig,
    t_verd,
    set_kompressor_status_func: Callable,
):
    """
    Erweiterte Sicherheitspruefungen basierend auf der JSON-Config.
    Prueft nur Ueberhitzung.

    Das harte Boiler-Maximum (max_temp_c am Bezugsfuehler boiler_max_fuehler,
    Standard unten) wird separat in handle_compressor_off/-on erzwungen und
    bricht dort die Mindestlaufzeit. Hier bleibt es bei der Warnung fuer
    t_oben: Die obere Schichtung darf das Limit naturgemaeÃƒÆ’Ã…Â¸ uebersteigen,
    solange der Bezugsfuehler darunter liegt.
    """
    cfg = state.priority_config.sicherheit

    # 1. Ueberhitzungsschutz (einzige harte Abschaltung). Waehrend einer
    #    Legionellenfahrt dynamisch auf legionellen_max_temp_c angehoben,
    #    damit der Schutz die Prophylaxe nicht abbricht.
    ueberhitz = _effektive_ueberhitzung_schwelle(state)
    if t_oben is not None and t_oben >= ueberhitz:
        state.control.blocking_reason = (
            f"UEBERHITZUNG ({t_oben:.1f}C >= {ueberhitz:.1f}C)"
        )
        if state.control.kompressor_ein:
            logging.critical(f"UEBERHITZUNG: {t_oben:.1f}C - Sofort-Abschaltung!")
            await set_kompressor_status_func(state, False, force=True)
        return False

    # 2. Max-Temperatur nur als Warnung (kein Abschalten, Schwellwert: max_temp_c + 2)
    # Während Legionellenbetrieb auf 65°C erhöhen, dann bis Temp um 1°C unter normal gefallen
    # (49°C wenn normal 50°C) auf 65°C belassen, danach normale Grenzwerte
    normal_max_temp_warn = cfg.max_temp_c + 2.0  # Normal 50°C
    if hasattr(state.control, 'active_rule_name') and state.control.active_rule_name == 'Legionellen':
        max_temp_warn = 65.0  # Während Legionellenbetrieb
    else:
        # Nach Legionellenbetrieb: Warnung erst wieder bei normaler Temp wenn vorher Legionellen
        last_was_legionellen = getattr(state, '_last_was_legionellen', False)
        if last_was_legionellen and t_oben >= normal_max_temp_warn:
            max_temp_warn = 65.0  # Noch erhöht bis Temp unter normal - 1°C fällt
        else:
            max_temp_warn = normal_max_temp_warn
        # Reset wenn unter normal - 1°C gefallen
        if t_oben < normal_max_temp_warn - 1.0:
            state._last_was_legionellen = False

    if t_oben is not None and t_oben >= max_temp_warn:
        # Hysteresis: Warnung erst wieder aktiv wenn Temp um 1°C unter aktuellen Warnwert gefallen
        last_warned_at = getattr(state, '_last_temp_warn_threshold', None)
        is_valid_threshold = isinstance(last_warned_at, (int, float))
        if last_warned_at is None or not is_valid_threshold or t_oben < last_warned_at - 1.0:
            state._last_temp_warn_threshold = max_temp_warn
            if check_log_throttle(state, 'log_max_temp_warn', interval_minutes=5):
                logging.warning(
                    f'Temperatur ueber Normalbereich: {t_oben:.1f}°C >= {max_temp_warn}°C (kein Abschalten)'
                )
    return True



def get_priority_control_status(state) -> dict:
    """Gibt einen Status-Report der Prioritaeten-Steuerung zurueck."""
    cfg = state.priority_config

    pv_leistung = state.solar.feedinpower if state.solar.feedinpower else 0.0

    return {
        "wp_leistung_watt": cfg.wp.leistung_watt,
        "wp_typ": cfg.wp.typ,
        "pv_leistung_watt": pv_leistung,
        "aktive_regel": getattr(state.control, "active_rule_name", None),
        "sensoren": state.control.active_rule_sensor,
        "nachtsperre_aktiv": _is_nachtsperre_aktiv(cfg, _now_for_state(state)),
        "komfort_aktiv": getattr(state.control, "komfort_aktiv", False),
        "anzahl_regeln": len(cfg.pv_regeln)
        + 4
        + 3,  # +Wochenende+PV+Komfort+Zeitfenster+Abweichung+Forecast+AdaptivePV+CalcStart
    }


def _is_nachtsperre_aktiv(cfg: WPSteuerungConfig, now: datetime) -> bool:
    """Prueft ob die Nachtsperre aktiv ist."""
    start = cfg.sicherheit.nachtsperre_start
    ende = cfg.sicherheit.nachtsperre_ende
    h = now.hour
    if start <= ende:
        return start <= h < ende
    return h >= start or h < ende


