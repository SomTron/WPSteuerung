import logging
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Callable

from utils import safe_timedelta
from telegram_handler import is_solar_window, send_telegram_message

# Helper functions moved from main.py
def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    try:
        start_str = config["Heizungssteuerung"].get("NACHT_START", "22:00")
        end_str = config["Heizungssteuerung"].get("NACHT_ENDE", "06:00")
        
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

def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob aktuell der Übergangsmodus (morgens oder abends) aktiv ist, basierend auf Uhrzeit im State."""
    try:
        now_time = datetime.now(state.local_tz).time()
        
        # Morgens
        morgens_aktiv = state.uebergangsmodus_start <= now_time <= state.uebergangsmodus_ende
        
        # Abends
        abends_aktiv = state.uebergangsmodus_abend_start <= now_time <= state.uebergangsmodus_abend_ende
        
        return morgens_aktiv or abends_aktiv
    except Exception as e:
        logging.error(f"Fehler bei ist_uebergangsmodus_aktiv: {e}")
        return False

def set_last_compressor_off_time(state, value):
    # Hilfsfunktion zum Setzen von last_compressor_off_time mit Debugging.
    logging.debug(f"Setze last_compressor_off_time auf: {value}")
    state.last_compressor_off_time = value

async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    """Prüft auf Sensorfehler."""
    if t_boiler_oben is None or t_boiler_unten is None:
        if state.last_sensor_error_time is None or safe_timedelta(datetime.now(state.local_tz), state.last_sensor_error_time, state.local_tz) > timedelta(minutes=5):
            logging.error("Sensorfehler: T_Oben oder T_Unten ist None")
            await send_telegram_message(
                session, state.chat_id,
                "⚠️ Sensorfehler: T_Oben oder T_Unten konnte nicht gelesen werden.",
                state.bot_token
            )
            state.last_sensor_error_time = datetime.now(state.local_tz)
        return False
    state.last_sensor_error_time = None
    return True

# Core control functions
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
            t_oben >= state.sicherheits_temp or t_unten >= state.sicherheits_temp):
        state.ausschluss_grund = f"Übertemperatur (>= {state.sicherheits_temp} Grad)"
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
                logging.critical(
                    "Kritischer Fehler: Kompressor konnte trotz Übertemperatur nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt trotz Übertemperatur eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
        await send_telegram_message(
            session, state.chat_id,
            f"⚠️ Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad >= {state.sicherheits_temp} Grad",
            state.bot_token,
            parse_mode=None
        )
        return False

    if t_verd is not None and t_verd < state.verdampfertemperatur:
        state.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {state.verdampfertemperatur} Grad)"
        logging.warning(state.ausschluss_grund)
        if state.last_verdampfer_notification is None or safe_timedelta(datetime.now(state.local_tz),
                                                                        state.last_verdampfer_notification,
                                                                        state.local_tz) > timedelta(minutes=5):
            await send_telegram_message(
                session, state.chat_id,
                f"⚠️ Kompressor bleibt aus oder wird ausgeschaltet: {state.ausschluss_grund}",
                state.bot_token,
                parse_mode=None
            )
            state.last_verdampfer_notification = datetime.now(state.local_tz)
        if state.kompressor_ein:
            result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                    state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(
                    f"Kompressor ausgeschaltet wegen zu niedriger Verdampfertemperatur. Laufzeit: {state.last_runtime}")
            else:
                logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt trotz niedriger Verdampfertemperatur eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
        return False
    return True

async def check_pressure_and_config(session, state, handle_pressure_check_func: Callable, set_kompressor_status_func: Callable, reload_config_func: Callable, calculate_file_hash_func: Callable):
    """Prüft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
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

    if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=60):
        current_hash = calculate_file_hash_func("config.ini")
        if current_hash != state.last_config_hash:
            await reload_config_func(session, state)
            state.last_config_hash = current_hash
        state._last_config_check = datetime.now(state.local_tz)
    return True

async def determine_mode_and_setpoints(state, t_unten, t_mittig):
    """Bestimmt den Betriebsmodus und setzt Sollwerte."""
    now = datetime.now(state.local_tz)
    is_night = await asyncio.to_thread(is_nighttime, state.config)

    # Prüfe Solarfenster nur alle 5 Minuten
    within_solar_window = state.last_solar_window_status
    if (state.last_solar_window_check is None or
            safe_timedelta(now, state.last_solar_window_check, state.local_tz) >= timedelta(minutes=5)):
        within_solar_window = is_solar_window(state.config, state)
        state.last_solar_window_check = now
        state.last_solar_window_status = within_solar_window

    nacht_reduction = float(state.config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
    urlaubs_reduction = float(
        state.config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 0)) if state.urlaubsmodus_aktiv else 0
    total_reduction = nacht_reduction + urlaubs_reduction

    state.solar_ueberschuss_aktiv = (
            state.batpower > 600.0 or
            (state.soc >= 95.0 and state.feedinpower > 600.0)
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
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    elif is_night:
        modus = "Nachtmodus"
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    else:
        modus = "Normalmodus"
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig

    if state.previous_modus != modus:
        logging.info(f"Wechsel zu Modus: {modus}")
        state.previous_modus = modus

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
    if abschalten and (state.previous_abschalten != abschalten or (
            state.last_abschalt_log is None or
            safe_timedelta(datetime.now(state.local_tz), state.last_abschalt_log, state.local_tz) >= timedelta(
        minutes=5))):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Abschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad >= {ausschaltpunkt:.1f} Grad"
        )
        logging.info(state.ausschluss_grund)
        state.last_abschalt_log = datetime.now(state.local_tz)
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
        logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
        await send_telegram_message(
            session, state.chat_id,
            f"🚨 KRITISCHER FEHLER: Kompressor bleibt eingeschaltet!",
            state.bot_token,
            parse_mode=None
        )
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
    elif not temp_conditions_met and (
            state.last_no_start_log is None or
            safe_timedelta(now, state.last_no_start_log, state.local_tz) >= timedelta(minutes=5)):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad > {einschaltpunkt:.1f} Grad"
        )
        logging.debug(state.ausschluss_grund)
        state.last_no_start_log = now
    state.previous_temp_conditions = temp_conditions_met

    solar_conditions_met = not (not state.bademodus_aktiv and within_solar_window and not state.solar_ueberschuss_aktiv)
    if not solar_conditions_met and (
            state.last_no_start_log is None or
            safe_timedelta(now, state.last_no_start_log, state.local_tz) >= timedelta(minutes=5)):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten im Übergangsmodus: Solarüberschuss nicht aktiv "
            f"({state.uebergangsmodus_start.strftime('%H:%M')}–{state.uebergangsmodus_ende.strftime('%H:%M')})"
        )
        logging.debug(state.ausschluss_grund)
        state.last_no_start_log = now

    pause_ok = True
    if not state.kompressor_ein and temp_conditions_met and solar_conditions_met and state.last_compressor_off_time:
        time_since_off = safe_timedelta(now, state.last_compressor_off_time, state.local_tz, default=timedelta.max)
        if time_since_off.total_seconds() < min_pause.total_seconds() - 0.5:
            pause_ok = False
            pause_remaining = min_pause - time_since_off
            reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"
            if state.last_pause_log is None or safe_timedelta(now, state.last_pause_log, state.local_tz) > timedelta(
                    minutes=5):
                logging.info(f"Kompressor START VERHINDERT: {reason}")
                await send_telegram_message(
                    session, state.chat_id,
                    f"⚠️ Kompressor bleibt aus: {reason}...",
                    state.bot_token,
                    parse_mode=None
                )
                state.last_pause_telegram_notification = now
                state.current_pause_reason = reason
                state.last_pause_log = now
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
                logging.critical("Kritischer Fehler: Kompressor konnte bei Moduswechsel nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt bei Moduswechsel eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
    return False
