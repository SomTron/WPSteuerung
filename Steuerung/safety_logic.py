import logging
import asyncio
from datetime import datetime, timedelta
from typing import Callable
from telegram_api import send_telegram_message
from logic_utils import is_valid_temperature, check_log_throttle
from utils import safe_timedelta
from constants import (
    TEMP_VERD_MIN_VALID, TEMP_VERD_MAX_VALID,
    COMPRESSOR_VERIFICATION_DELAY_MIN, COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN,
    COMPRESSOR_VERD_DELTA_MIN, COMPRESSOR_VERD_START_TEMP_COLD,
    COMPRESSOR_VERD_DELTA_COLD_MIN, COMPRESSOR_VERD_COLD_MAX,
    COMPRESSOR_UNTEN_DELTA_MIN,
)

async def handle_critical_compressor_error(session, state, error_context: str):
    """Behandelt kritische Fehler beim Kompressor-Ausschalten."""
    msg = f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!"
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    # Kritische Alarme awaiten statt fire-and-forget: Task-Referenz wuerde sonst
    # vom GC eingesammelt, bevor die Nachricht zugestellt ist (stiller Fehler).
    await send_telegram_message(
        session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN)


async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    """Prueft auf Sensorfehler und setzt Zeitstempel bei Fehlern."""
    errors = []
    if not is_valid_temperature(t_boiler_oben):
        errors.append(f"T_Oben invalid: {t_boiler_oben}")
    if not is_valid_temperature(t_boiler_unten):
        errors.append(f"T_Unten invalid: {t_boiler_unten}")
    
    if errors:
        error_msg = ", ".join(errors)
        state.control.blocking_reason = f"Sensorfehler: {error_msg}"
        state.last_sensor_error_time = datetime.now(state.local_tz)
        if check_log_throttle(state, "last_sensor_error_time"):
            logging.error(f"Sensorfehler: {error_msg}")
        return False
    state.last_sensor_error_time = None
    return True

async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """Sicherheitsabschaltung und Sensorprüfung."""
    state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd = t_oben, t_unten, t_mittig, t_verd
    state.sensors.t_boiler = t_oben if t_oben is not None else ((t_mittig if t_mittig is not None else t_unten))
    
    if not await check_for_sensor_errors(session, state, t_oben, t_unten):
        state.control.ausschluss_grund = "Sensorfehler"
        state.control.blocking_reason = "Sensor-Fehler"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False

    safety_temp = None
    if hasattr(state, 'priority_config') and getattr(state.priority_config, 'sicherheit', None):
        # Waehrend einer aktiven Legionellenfahrt wird die Sicherheitsschwelle
        # dynamisch auf legionellen_max_temp_c angehoben, damit die Prophylaxe
        # (Ziel 60/65C) nicht vom Ueberhitzungsschutz abgebrochen wird.
        # Prueft sowohl legionellen_aktiv als auch legionellen_temp_override
        # (robust gegen Mocks/Neustart-Zustaende).
        legionellen_betrieb = (
            getattr(state, 'legionellen_aktiv', False) is True
            or getattr(state, 'legionellen_temp_override', None) is not None
        )
        if legionellen_betrieb:
            lle_val = getattr(
                getattr(state.priority_config, 'legionellen', None),
                'legionellen_max_temp_c', None,
            )
            if isinstance(lle_val, (int, float)) and float(lle_val) > 40.0:
                safety_temp = float(lle_val)
        if safety_temp is None:
            val = getattr(state.priority_config.sicherheit, 'ueberhitzung_c', None)
            # Nur echte numerische Werte (int/float) akzeptieren, keine Mocks/MagicMocks
            if isinstance(val, (int, float)):
                try:
                    candidate = float(val)
                    if candidate > 40.0:  # Plausibilitätscheck: Sicherheitstemp muss > 40°C sein
                        safety_temp = candidate
                except (TypeError, ValueError):
                    pass
            
    if safety_temp is None:
        val = getattr(getattr(state, 'config', None), 'Heizungssteuerung', None)
        if val is not None:
            raw_temp = getattr(val, 'SICHERHEITS_TEMP', 58.0)
            try:
                safety_temp = float(raw_temp) if isinstance(raw_temp, (int, float)) else 58.0
                if not (40.0 < safety_temp < 150.0):
                    safety_temp = 58.0
            except (TypeError, ValueError):
                safety_temp = 58.0
        else:
            safety_temp = 58.0

    if (t_oben is not None and t_oben >= safety_temp) or (t_unten is not None and t_unten >= safety_temp):
        state.control.ausschluss_grund = f"Übertemperatur (>= {safety_temp} Grad)"
        state.control.blocking_reason = f"Sicherheitstemp (>= {safety_temp}°C)"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False

    if not is_valid_temperature(t_verd, min_temp=TEMP_VERD_MIN_VALID, max_temp=TEMP_VERD_MAX_VALID):
        state.control.ausschluss_grund = "Verdampfertemperatur ungültig"
        state.control.blocking_reason = "Verdampfer ungültig"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False
    
    verd_limit = state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR
    restart_temp = state.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
    
    # Logic for evaporator hysteresis
    already_blocked = getattr(state, 'verdampfer_blocked', False)
    too_cold = t_verd < verd_limit
    recovering = already_blocked and t_verd < restart_temp
    
    if too_cold or recovering:
        state.verdampfer_blocked = True
        # Verdampfer-Abschaltung tracken (nur bei erstem Blockieren pro Zyklus)
        if not already_blocked:
            now = datetime.now(state.local_tz)
            state.verdampfer_shutdowns.append(now)
            # Altvte Einträge außerhalb der letzten Stunde bereinigen
            cutoff = now - timedelta(hours=1)
            state.verdampfer_shutdowns = [t for t in state.verdampfer_shutdowns if t >= cutoff]
            # Warnung bei häufigem Vereisen (> 2 Abschaltungen/Stunde)
            if len(state.verdampfer_shutdowns) > 2:
                logging.warning(
                    f"Verdampfer-Vereisung: {len(state.verdampfer_shutdowns)} Abschaltungen in der letzten Stunde. "
                    "Mögliche Ursachen: schlechter Luftstrom, Kältemittelmangel, oder Filter verschmutzt."
                )
        if already_blocked:
            state.control.ausschluss_grund = f"Verdampfer: Warten auf Erwärmung ({t_verd:.1f} Grad < {restart_temp} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C, warte auf >{restart_temp}°C)"
        else:
            state.control.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {verd_limit} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C < {verd_limit}°C)"
        
        if state.control.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False
    
    state.verdampfer_blocked = False
    return True

async def verify_compressor_running(state, session, current_t_verd, current_t_unten, verification_delay_minutes=COMPRESSOR_VERIFICATION_DELAY_MIN):
    """Verifiziert den Lauf des Kompressors über Temperaturänderungen."""
    now = datetime.now(state.local_tz)
    if not state.control.kompressor_ein or state.kompressor_verification_start_time is None:
        state.kompressor_verification_start_time = None
        return True, None

    elapsed = safe_timedelta(now, state.kompressor_verification_start_time, state.local_tz)
    if elapsed < timedelta(minutes=verification_delay_minutes):
        return True, None

    if state.kompressor_verification_last_check:
        if safe_timedelta(now, state.kompressor_verification_last_check, state.local_tz) < timedelta(minutes=COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN):
            return True, None
    state.kompressor_verification_last_check = now

    verd_delta = state.kompressor_verification_start_t_verd - current_t_verd
    unten_delta = abs(current_t_unten - state.kompressor_verification_start_t_unten)

    verd_ok = verd_delta >= COMPRESSOR_VERD_DELTA_MIN
    if not verd_ok and state.kompressor_verification_start_t_verd < COMPRESSOR_VERD_START_TEMP_COLD:
        if verd_delta >= COMPRESSOR_VERD_DELTA_COLD_MIN and current_t_verd < COMPRESSOR_VERD_COLD_MAX:
            verd_ok = True

    unten_ok = unten_delta >= COMPRESSOR_UNTEN_DELTA_MIN
    
    if verd_ok and unten_ok:
        state.kompressor_verification_failed = False
        state.kompressor_verification_error_count = 0
        return True, None
    
    state.kompressor_verification_failed = True
    state.kompressor_verification_error_count += 1
    
    error_parts = []
    if not verd_ok:
        error_parts.append(f"Verdampfer: nur {verd_delta:.1f}°C Abfall (Soll: >{COMPRESSOR_VERD_DELTA_MIN}°C)")
    if not unten_ok:
        error_parts.append(f"Unterer Fühler: nur {unten_delta:.1f}°C Änderung (Soll: >{COMPRESSOR_UNTEN_DELTA_MIN}°C)")
    
    error_msg = "⚠️ Wärmepumpe läuft möglicherweise NICHT:\n" + "\n".join(error_parts)
    if state.bot_token:
        # Cast to string to prevent MagicMock serialization errors in telegram_api
        asyncio.create_task(send_telegram_message(session, state.config.Telegram.CHAT_ID, f"{error_msg}\nFehler #{state.kompressor_verification_error_count}", state.config.Telegram.BOT_TOKEN))
    return False, error_msg
