"""
Prioritaetenbasierte Steuerungslogik (Pareto-optimiert).

Ersetzt die modusbasierte Logik durch eine Regelengine mit Prioritaeten.
Jede Regel bewertet unabhaengig die aktuelle Situation.
Die Regel hoechster Prioritaet bestimmt das Schaltverhalten.
"""

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Callable, Optional, List

from utils import safe_timedelta
from constants import (
    CONFIG_CHECK_INTERVAL_SEC,
    COMPRESSOR_VERIFICATION_ERROR_THRESHOLD,
    TEMP_VERD_MIN_VALID, TEMP_VERD_MAX_VALID,
)
from logic_utils import is_valid_temperature, check_log_throttle
from safety_logic import (
    check_sensors_and_safety,
    handle_critical_compressor_error,
    verify_compressor_running,
)
from json_config import WPSteuerungConfig
from priority_control import (
    RegelErgebnis,
    bewerte_alle_regeln,
    formatiere_ergebnisse,
)

try:
    from telegram_api import send_telegram_message
except ImportError:
    pass


def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.stats.last_compressor_off_time = time_val


async def check_pressure_and_config(
    session, state, handle_pressure_check_func: Callable,
    set_kompressor_status_func: Callable, only_pressure: bool = False
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
            await set_kompressor_status_func(state, False, force=True)
        return False

    if not only_pressure:
        if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=CONFIG_CHECK_INTERVAL_SEC):
            state.update_config()
            state._last_config_check = datetime.now(state.local_tz)
    return True


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
    if pv_leistung < 0:
        pv_leistung = 0.0
    
    # --- Bademodus/Urlaubsmodus-Kopplung ---
    # Wir arbeiten mit einer Kopie der Config, um die original-Config nicht zu aendern.
    # Falls Bademodus/Urlaub aktiv, passen wir die Solltemperatur der Abweichungs-Regel an.
    import copy
    effektive_config = copy.deepcopy(state.priority_config)
    
    if state.bademodus_aktiv:
        # Bademodus: Solltemperatur +3°C (fuer warmes Wasser)
        erhoehung = 3.0
        effektive_config.abweichung.solltemperatur_c += erhoehung
        logging.debug(f"Bademodus aktiv: Solltemperatur +{erhoehung}C auf {effektive_config.abweichung.solltemperatur_c}C")
    
    if state.urlaubsmodus_aktiv:
        # Urlaubsmodus: Solltemperatur senken (Sparmodus)
        absenkung = float(state.config.Urlaubsmodus.URLAUBSABSENKUNG) if hasattr(state.config, 'Urlaubsmodus') else 5.0
        effektive_config.abweichung.solltemperatur_c -= absenkung
        logging.debug(f"Urlaubsmodus aktiv: Solltemperatur -{absenkung}C auf {effektive_config.abweichung.solltemperatur_c}C")
    

    # Forecast + AdaptivePV brauchen die MORGEN-Prognose (Vorheizen/Sparen)
    forecast_wh_qm = getattr(state.solar, 'forecast_tomorrow', None)
    if forecast_wh_qm is not None:
        try:
            forecast_wh_qm = float(forecast_wh_qm)
        except (TypeError, ValueError):
            forecast_wh_qm = None
    # CalcStart braucht die HEUTE-Prognose (PV-Erwartung zum Warten/Heizen)
    forecast_today_wh = getattr(state.solar, 'forecast_today', None)
    if forecast_today_wh is not None:
        try:
            forecast_today_wh = float(forecast_today_wh)
        except (TypeError, ValueError):
            forecast_today_wh = None

    
    # --- Sommer-Modus: Temperatur senken bei mehrtagiger guter PV-Prognose ---
    forecast_day2_wh_orig = getattr(state.solar, "forecast_day2", None)
    forecast_day2_wh = None
    if forecast_day2_wh_orig is not None:
        try:
            forecast_day2_wh = float(forecast_day2_wh_orig)
        except (TypeError, ValueError):
            pass
    _sommer_aktiv = False
    if (effektive_config.sommer_modus.aktiv
            and forecast_today_wh is not None
            and forecast_wh_qm is not None
            and forecast_day2_wh is not None):
        ft = float(forecast_today_wh)
        fm = float(forecast_wh_qm)
        fd = float(forecast_day2_wh)
        schwelle = effektive_config.sommer_modus.mindest_prognose_wh
        benoetigte = effektive_config.sommer_modus.benoetigte_tage
        tage_ueber = sum(1 for v in [ft, fm, fd] if v >= schwelle)
        if tage_ueber >= benoetigte:
            offset = effektive_config.sommer_modus.temperatur_offset_c
            effektive_config.abweichung.solltemperatur_c += offset
            for pv in effektive_config.pv_regeln:
                pv.einschalten_bei_c += offset
                pv.ausschalten_bei_c += offset
            effektive_config.komfort.ausschalten_bei_c += offset
            effektive_config.forecast.tmax_c += offset
            effektive_config.forecast.t_vorheiz_ab_c += offset
            effektive_config.adaptive_pv.tmax_c += offset
            effektive_config.calculated_start.tmax_c += offset
            effektive_config.calculated_start.solltemperatur_c += offset
            _sommer_aktiv = True
            # Sommer-Modus Status im State speichern (fuer API/Frontend)
            state.control.sommer_modus_aktiv = True
            state.control.sommer_modus_offset_c = offset
            state.control.sommer_modus_tage_ueber = tage_ueber
            state.control.sommer_modus_benoetigte = benoetigte
    else:
        # Sommer-Modus inaktiv: State zuruecksetzen
        state.control.sommer_modus_aktiv = False
        state.control.sommer_modus_offset_c = 0.0
        state.control.sommer_modus_tage_ueber = 0
    if _sommer_aktiv:
        logging.info(
            f"Sommer-Modus AKTIV: {tage_ueber}/{benoetigte} Tage >= {schwelle:.0f} Wh/qm "
            f"(heute={ft:.0f}, morgen={fm:.0f}, uebermorgen={fd:.0f}). "
            f"Temp-Offset: {offset:+.1f}C"
        )

    # Forecast-Daten aus State holen
    
    # Alle Regeln bewerten (mit effektiver Config)
    # Learning Engine aktualisieren (Heizzyklen + Zapfprofil)
    if learning_engine is not None:
        learning_engine.update(
            now=datetime.now(state.local_tz),
            temp_dict=temp_dict,
            compressor_is_on=state.control.kompressor_ein,
        )
        gelernte_rate_unten = learning_engine.get_learned_heating_rate(
            datetime.now(state.local_tz).month, 'unten'
        )
        gelernte_rate_gesamt = learning_engine.get_learned_heating_rate(
            datetime.now(state.local_tz).month, 'gesamt'
        )
        gelernte_zielzeit = learning_engine.get_learned_target_hour()
    else:
        gelernte_rate_unten = None
        gelernte_rate_gesamt = None
        gelernte_zielzeit = None

    gewinner, alle_ergebnisse = bewerte_alle_regeln(
        config=effektive_config,
        temp_dict=temp_dict,
        pv_leistung=pv_leistung,
        kompressor_ein=state.control.kompressor_ein,
        now=datetime.now(state.local_tz),
        forecast_wh_qm=forecast_wh_qm,
        forecast_today_wh_qm=forecast_today_wh,
        learned_heating_rate_unten=gelernte_rate_unten,
        learned_heating_rate_gesamt=gelernte_rate_gesamt,
        learned_target_hour=gelernte_zielzeit,
    )
    
    # Ergebnisse loggen (gethrottelt)
    if check_log_throttle(state, "_last_priority_log", interval_minutes=5.0):
        logging.info(f"Regel-Bewertung ({len(alle_ergebnisse)} Regeln):\n{formatiere_ergebnisse(alle_ergebnisse)}")
    
    # Gewinner-Regel in State speichern fuer Anzeige
    state.control.active_rule_sensor = None
    state.control.active_rule_name = None
    
    if gewinner is not None:
        state.control.active_rule_name = gewinner.name
        
        # Sensor-Name aus Regel ermitteln
        if "mitte" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Mittig"
        elif "unten" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Unten"
        elif "oben" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Oben"
        
        # Ein/Ausschaltpunkte aus Regel ermitteln
        eps = _extract_einschaltpunkt(gewinner, state.priority_config)
        ausp = _extract_ausschaltpunkt(gewinner, state.priority_config)
        state.control.aktueller_einschaltpunkt = eps
        state.control.aktueller_ausschaltpunkt = ausp
    else:
        # Keine Regel will einschalten: Standard = ausschalten
        state.control.aktueller_einschaltpunkt = state.priority_config.sicherheit.max_temp_c
        state.control.aktueller_ausschaltpunkt = state.priority_config.sicherheit.max_temp_c
    
    # Komfort-Einschaltstatus (fuer Komfort-Regel)
    state.control.komfort_aktiv = any(
        e.name == "Komfort" and e.aktiv and e.einschalten is True
        for e in alle_ergebnisse
    )
    
    # Alle Ergebnisse im State speichern fuer API/HTML-Anzeige
    state.control.alle_ergebnisse = alle_ergebnisse
    
    # Ergebnis aufbereiten
    should_on = gewinner is not None and gewinner.einschalten is True
    
    # Regelfuehler dynamisch aus der aktiven Regel ermitteln, nicht hartcodiert t_unten
    regelfuehler = t_unten  # Fallback
    if gewinner is not None:
        if "unten" in gewinner.grund.lower() or "unten" in (gewinner.name or "").lower():
            regelfuehler = t_unten
        elif "mitte" in gewinner.grund.lower() or "mitte" in (gewinner.name or "").lower():
            regelfuehler = t_mittig
        elif "oben" in gewinner.grund.lower() or "oben" in (gewinner.name or "").lower():
            regelfuehler = state.sensors.t_oben
    
    # Solarueberschuss aktiv wenn PV-Leistung >= niedrigste Schwelle aller PV-Regeln
    if state.priority_config.pv_regeln:
        min_pv_schwelle = min(r.pv_schwelle_watt for r in state.priority_config.pv_regeln)
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
    
    # Moduswechsel-Logging
    if state.control.previous_modus != res["modus"]:
        logging.info(f"Wechsel zu Regel: {res['modus']} ({'EIN' if should_on else 'AUS'})")
        state.control.previous_modus = res["modus"]
    
    return res


def _extract_einschaltpunkt(ergebnis: RegelErgebnis, config: WPSteuerungConfig) -> float:
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
        return config.abweichung.solltemperatur_c - config.abweichung.einschalten_bei_abweichung_k
    elif name == "Forecast":
        return config.forecast.t_vorheiz_ab_c
    elif name == "AdaptivePV":
        return config.adaptive_pv.base_threshold_watt
    elif name == "CalcStart":
        return config.calculated_start.solltemperatur_c
    
    return config.sicherheit.max_temp_c


def _extract_ausschaltpunkt(ergebnis: RegelErgebnis, config: WPSteuerungConfig) -> float:
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
        return config.abweichung.solltemperatur_c - config.abweichung.ausschalten_bei_abweichung_k
    elif name == "Forecast":
        return config.forecast.tmax_c
    elif name == "AdaptivePV":
        return config.adaptive_pv.tmax_c
    elif name == "CalcStart":
        return config.calculated_start.tmax_c
    
    return config.sicherheit.max_temp_c


async def handle_compressor_off(
    state, session, regelfuehler, ausschaltpunkt, min_laufzeit,
    t_oben, set_kompressor_status_func: Callable
):
    """Prueft Abschaltbedingungen und schaltet aus."""
    if not state.control.kompressor_ein:
        return False

    # Absolute Sicherheitsgrenze
    if t_oben is not None and t_oben >= state.priority_config.sicherheit.ueberhitzung_c:
        if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
            state.control.blocking_reason = f"Ueberhitzungsschutz ({t_oben:.1f}C >= {state.priority_config.sicherheit.ueberhitzung_c}C)"
            logging.warning(f"SICHERHEIT AUS: Ueberhitzung ({t_oben:.1f}C)")
            return True
        await handle_critical_compressor_error(session, state, "bei Ueberhitzung")
        return False

    # --- NEU: Keine Regel aktiv -> Kompressor ausschalten ---
    # Wenn keine Regel den Kompressor einschalten will (z.B. wegen Nachtsperre),
    # muss der Kompressor ausgeschaltet werden, auch wenn der regelfuehler
    # noch unter dem ausschaltpunkt liegt.
    should_on = getattr(state.control, '_soll_einschalten', False)
    if not should_on:
        # Pruefe ob wir schon laenger als die Mindestlaufzeit laufen
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(
                    f"Keine Regel aktiv: Kompressor AUS. "
                    f"Laufzeit: {elapsed}"
                )
                return True
        else:
            remaining_min = int((min_laufzeit - elapsed).total_seconds() // 60)
            state.control.blocking_reason = f"Keine Regel aktiv, warte auf Mindestlaufzeit (noch {remaining_min}m)"
            if check_log_throttle(state, "log_min_laufzeit_keine_regel", interval_minutes=5):
                logging.info(
                    f"Keine Regel aktiv, aber Mindestlaufzeit noch nicht erreicht. "
                    f"Laufzeit: {elapsed}"
                )
        return False

    # Regel-basiertes Ausschalten
    if regelfuehler is not None and regelfuehler >= ausschaltpunkt:
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(
                    f"Regel AUS: Regelfuehler ({regelfuehler:.1f}) >= Ziel ({ausschaltpunkt:.1f}). "
                    f"Laufzeit: {elapsed}"
                )
                return True
            await handle_critical_compressor_error(session, state, "")
        else:
            remaining_min = int((min_laufzeit - elapsed).total_seconds() // 60)
            state.control.blocking_reason = f"Warte auf Mindestlaufzeit (noch {remaining_min}m)"
            if check_log_throttle(state, "log_min_laufzeit_off", interval_minutes=5):
                logging.info(
                    f"Abschaltwunsch unterdrueckt: Mindestlaufzeit noch nicht erreicht. "
                    f"Laufzeit: {elapsed}"
                )
    return False


async def handle_compressor_on(
    state, session, regelfuehler, einschaltpunkt, ausschaltpunkt,
    min_laufzeit, min_pause, t_oben,
    set_kompressor_status_func: Callable
):
    """Prueft Einschaltbedingungen und schaltet ein."""
    now = datetime.now(state.local_tz)
    
    # Die Prioritaeten-Engine hat bereits entschieden
    # Wir muessen nur noch Mindestlaufzeit/-pause und Basis-Sicherheit pruefen
    
    pause_ok = True
    pause_remaining = None
    if state.stats.last_compressor_off_time:
        elapsed_pause = safe_timedelta(now, state.stats.last_compressor_off_time, state.local_tz)
        if elapsed_pause < min_pause:
            pause_ok = False
            pause_remaining = min_pause - elapsed_pause

    # Nur pruefen ob der Regelfuehler der aktiven Regel ueber Ausschaltpunkt liegt.
    # t_oben wird hier NICHT geprueft, da:
    #   1. check_safety_limits() bereits t_oben >= max_temp_c / ueberhitzung_c abfaengt
    #   2. Der Boiler stratifiziert ist - t_oben kann hoch sein waehrend unten noch kalt ist.
    stop_condition = (
        regelfuehler is not None and regelfuehler >= ausschaltpunkt
    )
    
    if not state.control.kompressor_ein:
        # Pruefe ob die Regel einschalten will (ueber state oder Aufruf-Parameter)
        should_on = getattr(state.control, '_soll_einschalten', False)
        
        if should_on and pause_ok:
            if stop_condition:
                logging.info(
                    f"Einschalten unterdrueckt: Regelfuehler ({regelfuehler:.1f}) >= "
                    f"Ausschaltpunkt ({ausschaltpunkt:.1f})"
                )
                state.control.blocking_reason = "Zieltemp erreicht"
                return False
            
            if await set_kompressor_status_func(state, True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(
                    f"Eingeschaltet um {now}. "
                    f"Grund: Regel-Einschalt (Regelfuehler={regelfuehler}, Ziel={einschaltpunkt})"
                )
                return True
    
    # Blocking-Reason setzen wenn Bedingungen nicht erfuellt
        # Nur setzen, wenn ueberhaupt eine Regel einschalten will (sonst sinnlose Warnung)
        if not state.control.kompressor_ein:
            should_on = getattr(state.control, '_soll_einschalten', False)
            if should_on and not pause_ok and pause_remaining:
                minutes = int(pause_remaining.total_seconds() // 60)
                seconds = int(pause_remaining.total_seconds() % 60)
                state.control.blocking_reason = f"Min. Pause (noch {minutes}m {seconds}s)"
            elif not should_on:
                state.control.blocking_reason = None
    
    return False


async def handle_mode_switch(
    state, session, t_oben, t_mittig,
    set_kompressor_status_func: Callable
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


async def check_safety_limits(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """
    Erweiterte Sicherheitspruefungen basierend auf der JSON-Config.
    Prueft nur Ueberhitzung.
    
    Hinweis: max_temp_c (48C) ist kein Abschaltpunkt mehr, da der Boiler
    kurzzeitig darueber gehen kann (z.B. 49C) ohne Probleme.
    Nur ueberhitzung_c (58C) fuehrt zur sofortigen Abschaltung.
    """
    cfg = state.priority_config.sicherheit
    
    # 1. Ueberhitzungsschutz (einzige harte Abschaltung)
    if t_oben is not None and t_oben >= cfg.ueberhitzung_c:
        state.control.blocking_reason = f"UEBERHITZUNG ({t_oben:.1f}C >= {cfg.ueberhitzung_c}C)"
        if state.control.kompressor_ein:
            logging.critical(f"UEBERHITZUNG: {t_oben:.1f}C - Sofort-Abschaltung!")
            await set_kompressor_status_func(state, False, force=True)
        return False
    
    # 2. Max-Temperatur nur als Warnung (kein Abschalten, Schwellwert: max_temp_c + 2)
    max_temp_warn = cfg.max_temp_c + 2.0
    if t_oben is not None and t_oben >= max_temp_warn:
        if check_log_throttle(state, "log_max_temp_warn", interval_minutes=5):
            logging.warning(f"Temperatur ueber Normalbereich: {t_oben:.1f}C >= {max_temp_warn}C (kein Abschalten)")
    
    return True


def get_priority_control_status(state) -> dict:
    """Gibt einen Status-Report der Prioritaeten-Steuerung zurueck."""
    cfg = state.priority_config
    
    pv_leistung = state.solar.feedinpower if state.solar.feedinpower else 0.0
    
    return {
        "wp_leistung_watt": cfg.wp.leistung_watt,
        "wp_typ": cfg.wp.typ,
        "pv_leistung_watt": pv_leistung,
        "aktive_regel": getattr(state.control, 'active_rule_name', None),
        "sensoren": state.control.active_rule_sensor,
        "nachtsperre_aktiv": _is_nachtsperre_aktiv(cfg, datetime.now(state.local_tz)),
        "komfort_aktiv": getattr(state.control, 'komfort_aktiv', False),
        "anzahl_regeln": len(cfg.pv_regeln) + 4 + 3,  # +Wochenende+PV+Komfort+Zeitfenster+Abweichung+Forecast+AdaptivePV+CalcStart
    }


def _is_nachtsperre_aktiv(cfg: WPSteuerungConfig, now: datetime) -> bool:
    """Prueft ob die Nachtsperre aktiv ist."""
    start = cfg.sicherheit.nachtsperre_start
    ende = cfg.sicherheit.nachtsperre_ende
    h = now.hour
    if start <= ende:
        return start <= h < ende
    return h >= start or h < ende
