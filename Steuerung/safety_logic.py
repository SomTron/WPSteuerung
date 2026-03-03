import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Callable
from telegram_api import send_telegram_message
from logic_utils import is_valid_temperature, check_log_throttle
from utils import safe_timedelta

# Constants
MIN_VERDAMPFER_TEMP = -20.0
MAX_VERDAMPFER_TEMP = 50.0
VORLAUF_RISE_THRESHOLD = 2.0
UNTEN_CHANGE_THRESHOLD = 0.2
VERIFICATION_DELAY_DEFAULT = 20
VERIFICATION_CHECK_INTERVAL = 1 # Minute

def _fire_and_log(coro, context: str = "background task"):
    """Erstellt einen Task und loggt Fehler statt sie zu verschlucken."""
    task = asyncio.create_task(coro)
    def _on_done(t):
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            logging.error(f"Fehler in {context}: {exc}")
    task.add_done_callback(_on_done)
    return task

async def handle_critical_compressor_error(session, state, error_context: str):
    """Behandelt kritische Fehler beim Kompressor-Ausschalten."""
    msg = f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!"
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    _fire_and_log(
        send_telegram_message(session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN),
        context="handle_critical_compressor_error"
    )

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
        if state.control.kompressor_ein: await set_kompressor_status_func(False, force=True)
        return False

    safety_temp = state.config.Heizungssteuerung.SICHERHEITS_TEMP
    if (t_oben is not None and t_oben >= safety_temp) or (t_unten is not None and t_unten >= safety_temp):
        state.control.ausschluss_grund = f"Übertemperatur (>= {safety_temp} Grad)"
        state.control.blocking_reason = f"Sicherheitstemp (>= {safety_temp}°C)"
        if state.control.kompressor_ein: await set_kompressor_status_func(False, force=True)
        return False

    if not is_valid_temperature(t_verd, min_temp=MIN_VERDAMPFER_TEMP, max_temp=MAX_VERDAMPFER_TEMP):
        state.control.ausschluss_grund = "Verdampfertemperatur ungültig"
        state.control.blocking_reason = "Verdampfer ungültig"
        if state.control.kompressor_ein: await set_kompressor_status_func(False, force=True)
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
        
        if state.control.kompressor_ein: await set_kompressor_status_func(False, force=True)
        return False
    
    state.verdampfer_blocked = False
    return True

async def verify_compressor_running(state, session, current_t_vorlauf, current_t_unten, verification_delay_minutes=VERIFICATION_DELAY_DEFAULT):
    """Verifiziert den Lauf des Kompressors über Temperaturänderungen am Vorlauf."""
    now = datetime.now(state.local_tz)
    if not state.control.kompressor_ein or state.kompressor_verification_start_time is None:
        state.kompressor_verification_start_time = None
        return True, None
    
    elapsed = safe_timedelta(now, state.kompressor_verification_start_time, state.local_tz)
    if elapsed < timedelta(minutes=verification_delay_minutes): return True, None
    
    if state.kompressor_verification_last_check:
        if safe_timedelta(now, state.kompressor_verification_last_check, state.local_tz) < timedelta(minutes=VERIFICATION_CHECK_INTERVAL):
            return True, None
    state.kompressor_verification_last_check = now

    # Vorlauf muss STEIGEN
    vorlauf_delta = current_t_vorlauf - (state.kompressor_verification_start_t_vorlauf or current_t_vorlauf)
    unten_delta = abs(current_t_unten - state.kompressor_verification_start_t_unten)
    
    # Kriterium: Vorlauf steigt um mindestens threshold
    vorlauf_ok = vorlauf_delta >= VORLAUF_RISE_THRESHOLD
    unten_ok = unten_delta >= UNTEN_CHANGE_THRESHOLD
    
    if vorlauf_ok and unten_ok:
        state.kompressor_verification_failed = False
        state.kompressor_verification_error_count = 0
        return True, None
    
    state.kompressor_verification_failed = True
    state.kompressor_verification_error_count += 1
    
    error_parts = []
    if not vorlauf_ok: error_parts.append(f"Vorlauf: nur {vorlauf_delta:.1f}°C Anstieg (Soll: >={VORLAUF_RISE_THRESHOLD}°C)")
    if not unten_ok: error_parts.append(f"Unterer Fühler: nur {unten_delta:.1f}°C Änderung (Soll: >={UNTEN_CHANGE_THRESHOLD}°C)")
    
    error_msg = "⚠️ Wärmepumpe läuft möglicherweise NICHT:\n" + "\n".join(error_parts)
    if state.bot_token:
        # Cast to string to prevent MagicMock serialization errors in telegram_api
        _fire_and_log(
            send_telegram_message(session, state.config.Telegram.CHAT_ID, f"{error_msg}\nFehler #{state.kompressor_verification_error_count}", state.config.Telegram.BOT_TOKEN),
            context="verify_compressor_running"
        )
    return False, error_msg
