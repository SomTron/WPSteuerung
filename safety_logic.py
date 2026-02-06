import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable
from telegram_api import send_telegram_message
from logic_utils import is_valid_temperature, check_log_throttle
from utils import safe_timedelta

async def handle_critical_compressor_error(session, state, error_context: str):
    """Behandelt kritische Fehler beim Kompressor-Ausschalten."""
    msg = f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!"
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    asyncio.create_task(send_telegram_message(
        session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN))

async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    """Prüft auf Sensorfehler."""
    errors = []
    if not is_valid_temperature(t_boiler_oben): errors.append(f"T_Oben invalid: {t_boiler_oben}")
    if not is_valid_temperature(t_boiler_unten): errors.append(f"T_Unten invalid: {t_boiler_unten}")
    
    if errors:
        error_msg = ", ".join(errors)
        state.control.blocking_reason = f"Sensorfehler: {error_msg}"
        if check_log_throttle(state, "last_sensor_error_time"):
            logging.error(f"Sensorfehler: {error_msg}")
        return False
    # print("DEBUG: No sensor errors")
    state.last_sensor_error_time = None
    return True

async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """Sicherheitsabschaltung und Sensorprüfung."""
    state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd = t_oben, t_unten, t_mittig, t_verd
    state.sensors.t_boiler = (t_oben + t_unten) / 2 if t_oben is not None and t_unten is not None else None
    
    if not await check_for_sensor_errors(session, state, t_oben, t_unten):
        state.control.ausschluss_grund = "Sensorfehler"
        state.control.blocking_reason = "Sensor-Fehler"
        if state.control.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
        return False

    safety_temp = state.config.Heizungssteuerung.SICHERHEITS_TEMP
    if (t_oben is not None and t_oben >= safety_temp) or (t_unten is not None and t_unten >= safety_temp):
        state.control.ausschluss_grund = f"Übertemperatur (>= {safety_temp} Grad)"
        state.control.blocking_reason = f"Sicherheitstemp (>= {safety_temp}°C)"
        if state.control.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
        return False

    if not is_valid_temperature(t_verd, min_temp=-20.0, max_temp=50.0):
        state.control.ausschluss_grund = "Verdampfertemperatur ungültig"
        state.control.blocking_reason = "Verdampfer ungültig"
        if state.control.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
        return False
    
    verd_limit = state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR
    restart_temp = state.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
    
    # Logic for evaporator hysteresis
    already_blocked = getattr(state, 'verdampfer_blocked', False)
    too_cold = t_verd < verd_limit
    recovering = already_blocked and t_verd < restart_temp
    
    if too_cold or recovering:
        state.verdampfer_blocked = True
        if already_blocked:
            state.control.ausschluss_grund = f"Verdampfer: Warten auf Erwärmung ({t_verd:.1f} Grad < {restart_temp} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C, warte auf >{restart_temp}°C)"
        else:
            state.control.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {verd_limit} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C < {verd_limit}°C)"
        
        if state.control.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
        return False
    
    state.verdampfer_blocked = False
    return True

async def verify_compressor_running(state, session, current_t_verd, current_t_unten, verification_delay_minutes=10):
    """Verifiziert den Lauf des Kompressors über Temperaturänderungen."""
    now = datetime.now(state.local_tz)
    if not state.control.kompressor_ein or state.kompressor_verification_start_time is None:
        state.kompressor_verification_start_time = None
        return True, None
    
    elapsed = safe_timedelta(now, state.kompressor_verification_start_time, state.local_tz)
    if elapsed < timedelta(minutes=verification_delay_minutes): return True, None
    
    if state.kompressor_verification_last_check:
        if safe_timedelta(now, state.kompressor_verification_last_check, state.local_tz) < timedelta(minutes=1):
            return True, None
    state.kompressor_verification_last_check = now

    verd_delta = state.kompressor_verification_start_t_verd - current_t_verd
    unten_delta = abs(current_t_unten - state.kompressor_verification_start_t_unten)
    
    verd_ok = verd_delta >= 1.5
    if not verd_ok and state.kompressor_verification_start_t_verd < 15.0:
        if verd_delta >= -0.5 and current_t_verd < 12.0:
            verd_ok = True
    
    unten_ok = unten_delta >= 0.2
    
    if verd_ok and unten_ok:
        state.kompressor_verification_failed = False
        state.kompressor_verification_error_count = 0
        return True, None
    
    state.kompressor_verification_failed = True
    state.kompressor_verification_error_count += 1
    
    error_parts = []
    if not verd_ok: error_parts.append(f"Verdampfer: nur {verd_delta:.1f}°C Abfall (Soll: >1.5°C)")
    if not unten_ok: error_parts.append(f"Unterer Fühler: nur {unten_delta:.1f}°C Änderung (Soll: >0.2°C)")
    
    error_msg = "⚠️ Wärmepumpe läuft möglicherweise NICHT:\n" + "\n".join(error_parts)
    if state.bot_token:
        # Cast to string to prevent MagicMock serialization errors in telegram_api
        asyncio.create_task(send_telegram_message(session, state.config.Telegram.CHAT_ID, f"{error_msg}\nFehler #{state.kompressor_verification_error_count}", state.config.Telegram.BOT_TOKEN))
    return False, error_msg
