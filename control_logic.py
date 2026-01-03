import logging
import math
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Callable

from utils import safe_timedelta
# removed is_solar_window import to avoid circular dependency
from telegram_handler import send_telegram_message

def is_valid_temperature(temp: Optional[float], min_temp: float = -50.0, max_temp: float = 150.0) -> bool:
    """
    Prüft, ob ein Temperaturwert gültig ist.
    """
    if temp is None:
        return False
    if not isinstance(temp, (int, float)):
        return False
    if math.isnan(temp) or math.isinf(temp):
        return False
    if temp < min_temp or temp > max_temp:
        return False
    return True

def check_log_throttle(state, attribute_name: str, interval_minutes: float = 5.0) -> bool:
    """
    Prüft, ob eine Log-Nachricht gesendet werden soll (Throttling).
    """
    last_time = getattr(state, attribute_name, None)
    now = datetime.now(state.local_tz)
    
    if last_time is None or safe_timedelta(now, last_time, state.local_tz) > timedelta(minutes=interval_minutes):
        setattr(state, attribute_name, now)
        return True
    return False

def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.last_compressor_off_time = time_val

def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    try:
        # Access via Pydantic model
        start_str = config.Heizungssteuerung.NACHTABSENKUNG_START
        end_str = config.Heizungssteuerung.NACHTABSENKUNG_END
        
        now = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        
        if start <= end:
            return start <= now <= end
        else:  # Nacht geht über Mitternacht
            return start <= now or now <= end
    except Exception as e:
        logging.error(f"Fehler bei is_nighttime: {e}")
        return False

def is_solar_window(config, state):
    """Prüft, ob die aktuelle Uhrzeit im Solarfenster nach der Nachtabsenkung liegt."""
    # Moved from telegram_handler.py
    now = datetime.now(state.local_tz)
    try:
        end_time_str = config.Heizungssteuerung.NACHTABSENKUNG_END
        try:
            end_hour, end_minute = map(int, end_time_str.split(':'))
        except ValueError:
            logging.error(f"Ungültiges Zeitformat: NACHTABSENKUNG_END={end_time_str}")
            return False

        if not (0 <= end_hour < 24 and 0 <= end_minute < 60):
            logging.error(f"Ungültige Zeitwerte: Ende={end_time_str}")
            return False

        potential_night_setback_end_today = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if now < potential_night_setback_end_today + timedelta(hours=2):
            night_setback_end_time_today = potential_night_setback_end_today
        else:
            night_setback_end_time_today = potential_night_setback_end_today + timedelta(days=1)
        
        # Solarfenster: 2 Stunden nach Nachtabsenkung
        solar_only_window_start_time_today = night_setback_end_time_today
        solar_only_window_end_time_today = night_setback_end_time_today + timedelta(hours=2)
        within_solar_only_window = solar_only_window_start_time_today <= now < solar_only_window_end_time_today

        # Logge nur bei Statusänderung oder alle 5 Minuten
        if (not hasattr(is_solar_window, 'last_status') or
                is_solar_window.last_status != within_solar_only_window or
                state.last_solar_window_log is None or
                safe_timedelta(now, state.last_solar_window_log, state.local_tz) >= timedelta(minutes=5)):
            logging.debug(
                f"Solarfensterprüfung: Jetzt={now.strftime('%H:%M')}, "
                f"Start={solar_only_window_start_time_today.strftime('%H:%M')}, "
                f"Ende={solar_only_window_end_time_today.strftime('%H:%M')}, "
                f"Ist Solarfenster={within_solar_only_window}"
            )
            state.last_solar_window_log = now

        is_solar_window.last_status = within_solar_only_window
        return within_solar_only_window
    except Exception as e:
        logging.error(f"Fehler in is_solar_window: {e}")
        return False

def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob aktuell der Übergangsmodus (morgens oder abends) aktiv ist, basierend auf Uhrzeit im State."""
    try:
        now_time = datetime.now(state.local_tz).time()
        
        # Values come from state init which reads from config
        morgens_aktiv = state.nachtabsenkung_ende <= now_time <= state.uebergangsmodus_morgens_ende
        abends_aktiv = state.uebergangsmodus_abends_start <= now_time <= state.nachtabsenkung_start
        
        return morgens_aktiv or abends_aktiv
    except Exception as e:
        logging.error(f"Fehler bei ist_uebergangsmodus_aktiv: {e}")
        return False

async def handle_critical_compressor_error(session, state, error_context: str):
    """
    Behandelt kritische Fehler beim Kompressor-Ausschalten.
    """
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    asyncio.create_task(send_telegram_message(
        session, state.config.Telegram.CHAT_ID,
        f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!",
        state.config.Telegram.BOT_TOKEN,
        parse_mode=None
    ))

def get_validated_reduction(config, section: str, key: str, default: float = 0.0) -> float:
    """
    Validiert und gibt Temperaturreduktionswerte zurück.
    Supports both dict-like (legacy) and AppConfig object access.
    """
    try:
        # Try finding the section as an attribute (AppConfig style)
        section_obj = getattr(config, section, None)
        if section_obj:
            value = getattr(section_obj, key, default)
        else:
             # Fallback for dict-like config if still used anywhere (though we aim to replace it)
             if hasattr(config, "__getitem__"):
                 value = config[section].get(key, str(default))
             else:
                 return default

        reduction = float(value)
        if reduction < 0 or reduction > 35:
            logging.warning(f"{key} ({reduction}) außerhalb des gültigen Bereichs (0-35°C), setze auf {default}")
            return default
        return reduction
    except (ValueError, TypeError, AttributeError) as e:
        logging.error(f"Ungültiger Wert für {key}: {e}, verwende {default}")
        return default

async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    """Prüft auf Sensorfehler mit robuster Validierung."""
    errors = []
    if not is_valid_temperature(t_boiler_oben):
        # ... validation logic ...
        errors.append(f"T_Oben invalid: {t_boiler_oben}")

    if not is_valid_temperature(t_boiler_unten):
         errors.append(f"T_Unten invalid: {t_boiler_unten}")
    
    if errors:
        if check_log_throttle(state, "last_sensor_error_time"):
            error_msg = ", ".join(errors)
            logging.error(f"Sensorfehler: {error_msg}")
            asyncio.create_task(send_telegram_message(
                session, state.config.Telegram.CHAT_ID,
                f"⚠️ Sensorfehler: {error_msg}",
                state.config.Telegram.BOT_TOKEN
            ))
        return False
    state.last_sensor_error_time = None
    return True

async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """Prüft Sensorwerte, Verdampfertemperatur und Sicherheitsabschaltung."""
    state.t_oben = t_oben
    state.t_unten = t_unten
    state.t_mittig = t_mittig
    state.t_verd = t_verd
    state.t_boiler = (t_oben + t_unten) / 2 if t_oben is not None and t_unten is not None else None

    if not await check_for_sensor_errors(session, state, t_oben, t_unten):
        state.ausschluss_grund = "Sensorfehler: Ungültige Werte"
        logging.info("Kompressor bleibt aus wegen Sensorfehler")
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        return False

    if t_oben is not None and t_unten is not None and (
            t_oben >= state.config.Heizungssteuerung.SICHERHEITS_TEMP or t_unten >= state.config.Heizungssteuerung.SICHERHEITS_TEMP):
        state.ausschluss_grund = f"Übertemperatur (>= {state.config.Heizungssteuerung.SICHERHEITS_TEMP} Grad)"
        logging.error(f"Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad")
        if state.kompressor_ein:
            result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                    state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet (Sicherheitsabschaltung). Laufzeit: {state.last_runtime}")
            else:
                await handle_critical_compressor_error(session, state, "trotz Übertemperatur")
        asyncio.create_task(send_telegram_message(
            session, state.config.Telegram.CHAT_ID,
            f"⚠️ Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad >= {state.config.Heizungssteuerung.SICHERHEITS_TEMP} Grad",
            state.config.Telegram.BOT_TOKEN,
            parse_mode=None
        ))
        return False

    # Prüfe Verdampfertemperatur
    if not is_valid_temperature(t_verd, min_temp=-20.0, max_temp=50.0):
        state.ausschluss_grund = f"Verdampfertemperatur ungültig"
        logging.warning(state.ausschluss_grund)
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        return False
    
    if t_verd < state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR or getattr(state, 'verdampfer_blocked', False):
        restart_temp = state.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
        
        if t_verd < state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR:
            state.verdampfer_blocked = True
            reason = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR} Grad)"
        elif t_verd < restart_temp:
            state.verdampfer_blocked = True
            reason = f"Verdampfer-Sperre aktiv: Warten auf Erwärmung ({t_verd:.1f} Grad < {restart_temp} Grad)"
        else:
            state.verdampfer_blocked = False
            return True 

        state.ausschluss_grund = reason
        if check_log_throttle(state, "last_verdampfer_notification"):
            logging.warning(state.ausschluss_grund)
            asyncio.create_task(send_telegram_message(
                session, state.config.Telegram.CHAT_ID,
                f"⚠️ Kompressor bleibt aus: {state.ausschluss_grund}",
                state.config.Telegram.BOT_TOKEN
            ))
        else:
            logging.debug(state.ausschluss_grund)

        if state.kompressor_ein:
            result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                 state.kompressor_ein = False
                 state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time, state.local_tz)
                 state.total_runtime_today += state.last_runtime
                 logging.info(f"Kompressor ausgeschaltet wegen zu niedriger Verdampfertemperatur.")
            else:
                 await handle_critical_compressor_error(session, state, "trotz niedriger Verdampfertemperatur")
        return False
    return True

async def check_pressure_and_config(session, state, handle_pressure_check_func: Callable, set_kompressor_status_func: Callable, reload_config_func: Callable, calculate_file_hash_func: Callable, only_pressure: bool = False):
    """Prüft Druckschalter und aktualisiert Konfiguration Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.ausschluss_grund = "Druckschalterfehler"
        logging.info("Kompressor bleibt aus wegen Druckschalterfehler")
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False

    if not only_pressure:
        if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=60):
            # In new architecture, config update handles loading itself, we just trigger it
            # We assume reload_config_func calls state.update_config() or similar
            # If function is simpler now:
            state.update_config() # This reloads config
            # Check if values changed (simplified logic here vs manual hash check)
            # Actually State.update_config does the load.
            state._last_config_check = datetime.now(state.local_tz)
    return True

async def determine_mode_and_setpoints(state, t_unten, t_mittig):
    """Bestimmt den Betriebsmodus und setzt Sollwerte."""
    now = datetime.now(state.local_tz)
    # is_nighttime ist eine einfache Zeitvergleichsfunktion, kein Thread nötig
    is_night = is_nighttime(state.config)

    # Prüfe Solarfenster nur alle 5 Minuten
    within_solar_window = state.last_solar_window_status
    if (state.last_solar_window_check is None or
            safe_timedelta(now, state.last_solar_window_check, state.local_tz) >= timedelta(minutes=5)):
        within_solar_window = is_solar_window(state.config, state)
        state.last_solar_window_check = now
        state.last_solar_window_status = within_solar_window

    # Nacht- und Urlaubsabsenkung sicher abrufen (nur sinnvolle Temperaturwerte 0-20°C)
    nacht_reduction = 0.0
    urlaubs_reduction = 0.0
    
    if is_night:
        nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
    
    if state.urlaubsmodus_aktiv:
        urlaubs_reduction = get_validated_reduction(state.config, "Urlaubsmodus", "URLAUBSABSENKUNG", 0.0)
    
    total_reduction = nacht_reduction + urlaubs_reduction

    # Solarüberschuss-Schwellwerte aus Konfiguration lesen (nur bei Config-Änderung)
    state.solar_ueberschuss_aktiv = (
            state.batpower > state.config.Solarueberschuss.BATPOWER_THRESHOLD or
            (state.soc >= state.config.Solarueberschuss.SOC_THRESHOLD and 
             state.feedinpower > state.config.Solarueberschuss.FEEDINPOWER_THRESHOLD)
    )

    # Prüfe Übergangsmodus (morgens oder abends)
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)

    if state.bademodus_aktiv:
        if state.previous_modus != "Bademodus":
            logging.info("Wechsel zu Bademodus – steuere nach T_Unten")
            state.previous_modus = "Bademodus"
        return {
            "modus": "Bademodus",
            "ausschaltpunkt": state.ausschaltpunkt_erhoeht,
            "einschaltpunkt": state.ausschaltpunkt_erhoeht - 4,
            "regelfuehler": t_unten,
            "nacht_reduction": 0,
            "urlaubs_reduction": 0,
            "solar_ueberschuss_aktiv": False
        }

    if state.previous_modus == "Bademodus":
        logging.info("Wechsel von Bademodus zu Normalmodus")

    if state.solar_ueberschuss_aktiv:
        modus = "Solarüberschuss"
        ausschaltpunkt = state.ausschaltpunkt_erhoeht
        einschaltpunkt = state.einschaltpunkt_erhoeht
        regelfuehler = t_unten
    elif within_uebergangsmodus:
        modus = "Übergangsmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    elif is_night:
        modus = "Nachtmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    else:
        modus = "Normalmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig

    if state.previous_modus != modus:
        logging.info(f"Wechsel zu Modus: {modus}")
        state.previous_modus = modus

    # DEBUG LOGGING für Config-Werte
    logging.debug(
        f"Modus-Ermittlung: {modus} | "
        f"Basis-Werte (State): Ein={state.basis_einschaltpunkt}, Aus={state.basis_ausschaltpunkt} | "
        f"Erhöht-Werte (State): Ein={state.einschaltpunkt_erhoeht}, Aus={state.ausschaltpunkt_erhoeht} | "
        f"Reduktion: Nacht={nacht_reduction}, Urlaub={urlaubs_reduction}, Total={total_reduction} | "
        f"Ergebnis: Ein={einschaltpunkt}, Aus={ausschaltpunkt}, Fühler={regelfuehler}"
    )

    return {
        "modus": modus,
        "ausschaltpunkt": ausschaltpunkt,
        "einschaltpunkt": einschaltpunkt,
        "regelfuehler": regelfuehler,
        "nacht_reduction": nacht_reduction,
        "urlaubs_reduction": urlaubs_reduction,
        "solar_ueberschuss_aktiv": state.solar_ueberschuss_aktiv
    }

async def handle_compressor_off(state, session, regelfuehler, ausschaltpunkt, min_laufzeit, t_oben, set_kompressor_status_func: Callable):
    """Prüft Abschaltbedingungen und schaltet Kompressor aus."""
    abschalten = regelfuehler is not None and regelfuehler >= ausschaltpunkt
    if abschalten and (state.previous_abschalten != abschalten or check_log_throttle(state, "last_abschalt_log")):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Abschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad >= {ausschaltpunkt:.1f} Grad"
        )
        logging.info(state.ausschluss_grund)
    state.previous_abschalten = abschalten

    can_turn_off = True
    if state.kompressor_ein and abschalten:
        elapsed_time = safe_timedelta(datetime.now(state.local_tz), state.start_time or state.last_compressor_on_time,
                                      state.local_tz)
        if elapsed_time.total_seconds() < min_laufzeit.total_seconds() - 0.5:
            can_turn_off = False
            state.ausschluss_grund = f"Mindestlaufzeit nicht erreicht ({min_laufzeit.total_seconds() - elapsed_time.total_seconds():.1f}s)"
            logging.debug(state.ausschluss_grund)

    if abschalten and state.kompressor_ein and can_turn_off:
        result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        if result:
            state.kompressor_ein = False
            set_last_compressor_off_time(state, datetime.now(state.local_tz))
            state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                state.local_tz)
            state.total_runtime_today += state.last_runtime
            state.last_completed_cycle = datetime.now(state.local_tz)
            logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
            return True
        await handle_critical_compressor_error(session, state, "")
    return False

async def handle_compressor_on(state, session, regelfuehler, einschaltpunkt, min_laufzeit, min_pause,
                               within_solar_window, t_oben, set_kompressor_status_func: Callable):
    """Prüft Einschaltbedingungen und schaltet Kompressor ein."""
    now = datetime.now(state.local_tz)
    temp_conditions_met = regelfuehler is not None and regelfuehler <= einschaltpunkt
    if temp_conditions_met and state.previous_temp_conditions != temp_conditions_met:
        logging.info(
            f"[{state.previous_modus}] Einschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad <= {einschaltpunkt:.1f} Grad"
        )
    elif not temp_conditions_met and check_log_throttle(state, "last_no_start_log"):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{f'{regelfuehler:.1f}' if regelfuehler is not None else 'N/A'} Grad > {einschaltpunkt:.1f} Grad"
        )
        logging.debug(state.ausschluss_grund)
    state.previous_temp_conditions = temp_conditions_met

    # Prüfe ob wir im Übergangsmodus sind (nicht gecachten within_solar_window nutzen!)
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
    
    # Kritische Kälte prüfen: Wenn Temperatur unter Nachtabsenkung fällt, trotzdem heizen!
    nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
    night_einschaltpunkt = state.basis_einschaltpunkt - nacht_reduction
    critical_cold = regelfuehler is not None and regelfuehler <= night_einschaltpunkt
    
    if within_uebergangsmodus and critical_cold and not state.solar_ueberschuss_aktiv:
        logging.info(f"Übergangsmodus: Kritische Kälte ({regelfuehler} <= {night_einschaltpunkt}), erlaube Heizbetrieb trotz fehlendem Solarüberschuss.")

    # Im Übergangsmodus nur heizen wenn Solarüberschuss aktiv ist (außer Bademodus oder kritische Kälte)
    # Explizite Logik für bessere Lesbarkeit und Fehlervermeidung
    if within_uebergangsmodus and not state.bademodus_aktiv:
        if not state.solar_ueberschuss_aktiv and not critical_cold:
            solar_conditions_met = False
        else:
            solar_conditions_met = True
    else:
        solar_conditions_met = True
    
    if not solar_conditions_met and check_log_throttle(state, "last_no_start_log"):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten im Übergangsmodus: Solarüberschuss nicht aktiv "
            f"(Morgens: {state.nachtabsenkung_ende.strftime('%H:%M')}–{state.uebergangsmodus_morgens_ende.strftime('%H:%M')}, "
            f"Abends: {state.uebergangsmodus_abends_start.strftime('%H:%M')}–{state.nachtabsenkung_start.strftime('%H:%M')})"
        )
        logging.debug(state.ausschluss_grund)

    pause_ok = True
    if not state.kompressor_ein and temp_conditions_met and solar_conditions_met and state.last_compressor_off_time:
        if state.last_compressor_off_time is None:
            time_since_off = timedelta.max
        else:
            time_since_off = safe_timedelta(now, state.last_compressor_off_time, state.local_tz)
        if time_since_off.total_seconds() < min_pause.total_seconds() - 0.5:
            pause_ok = False
            pause_remaining = min_pause - time_since_off
            reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"
            if check_log_throttle(state, "last_pause_log"):
                logging.info(f"Kompressor START VERHINDERT: {reason}")
                asyncio.create_task(send_telegram_message(
                    session, state.config.Telegram.CHAT_ID,
                    f"⚠️ Kompressor bleibt aus: {reason}...",
                    state.config.Telegram.BOT_TOKEN
                ))
                state.last_pause_telegram_notification = now
                state.current_pause_reason = reason
            state.ausschluss_grund = reason
        else:
            state.current_pause_reason = None
            state.last_pause_log = None
            state.last_pause_telegram_notification = None

    if not state.kompressor_ein and temp_conditions_met and pause_ok and solar_conditions_met:
        can_start_new_cycle = True
        if state.last_completed_cycle and safe_timedelta(now, state.last_completed_cycle,
                                                         state.local_tz).total_seconds() < min_laufzeit.total_seconds() + min_pause.total_seconds():
            can_start_new_cycle = False
            state.ausschluss_grund = (
                f"Neuer Zyklus nicht erlaubt: Warte auf Abschluss von Mindestlaufzeit + Mindestpause "
                f"({min_laufzeit.total_seconds() + min_pause.total_seconds() - safe_timedelta(now, state.last_completed_cycle, state.local_tz).total_seconds():.1f}s)"
            )
            logging.debug(state.ausschluss_grund)

        if can_start_new_cycle:
            logging.info(
                f"Alle Bedingungen für Kompressorstart erfüllt. Versuche einzuschalten (Modus: {state.previous_modus}).")
            result = await set_kompressor_status_func(state, True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = True
                state.start_time = now
                state.last_compressor_on_time = now
                logging.info(f"Kompressor eingeschaltet. Startzeit: {now}")
                state.ausschluss_grund = None
                return True
            state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler beim Einschalten"
            logging.info(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")
    return False

async def handle_mode_switch(state, session, t_oben, t_mittig, set_kompressor_status_func: Callable):
    """Prüft und behandelt Moduswechsel bei Solarüberschuss-Änderung."""
    if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv and not state.bademodus_aktiv:
        effective_ausschaltpunkt = state.previous_ausschaltpunkt or state.aktueller_ausschaltpunkt
        if not state.solar_ueberschuss_aktiv and t_oben is not None and t_mittig is not None:
            if t_oben >= effective_ausschaltpunkt or t_mittig >= effective_ausschaltpunkt:
                result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
                if result:
                    state.kompressor_ein = False
                    set_last_compressor_off_time(state, datetime.now(state.local_tz))
                    state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time)
                    state.total_runtime_today += state.last_runtime
                    logging.info(f"Kompressor ausgeschaltet bei Moduswechsel. Laufzeit: {state.last_runtime}")
                    state.ausschluss_grund = None
                    return True
                await handle_critical_compressor_error(session, state, "bei Moduswechsel")
    return False
