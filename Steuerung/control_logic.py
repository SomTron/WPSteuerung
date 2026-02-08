import logging
import asyncio
from datetime import datetime, timedelta
from typing import Callable, Optional
from utils import safe_timedelta

# New Modules
from logic_utils import (
    is_valid_temperature, 
    is_nighttime, 
    is_solar_window, 
    ist_uebergangsmodus_aktiv, 
    get_validated_reduction,
    check_log_throttle
)
from safety_logic import (
    check_sensors_and_safety, 
    handle_critical_compressor_error, 
    verify_compressor_running
)

# Export for tests that still patch control_logic
try:
    from telegram_api import send_telegram_message
except ImportError:
    pass

def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.stats.last_compressor_off_time = time_val

async def check_pressure_and_config(session, state, handle_pressure_check_func: Callable, set_kompressor_status_func: Callable, reload_config_func: Callable, calculate_file_hash_func: Callable, only_pressure: bool = False):
    """Prüft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.control.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.control.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.control.ausschluss_grund = "Druckschalterfehler"
        state.control.blocking_reason = "Druckschalter-Fehler"
        if state.control.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
        return False
    if not only_pressure:
        if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=60):
            state.update_config()
            state._last_config_check = datetime.now(state.local_tz)
    return True

async def determine_mode_and_setpoints(state, t_unten, t_mittig):
    """Bestimmt den Betriebsmodus und setzt Sollwerte."""
    is_night = is_nighttime(state.config)
    within_solar = is_solar_window(state.config, state)
    
    nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0) if is_night else 0.0
    urlaubs_reduction = get_validated_reduction(state.config, "Urlaubsmodus", "URLAUBSABSENKUNG", 0.0) if state.urlaubsmodus_aktiv else 0.0
    total_reduction = nacht_reduction + urlaubs_reduction

    bat_p = state.solar.batpower if state.solar.batpower is not None else 0.0
    soc_v = state.solar.soc if state.solar.soc is not None else 0.0
    feed_p = state.solar.feedinpower if state.solar.feedinpower is not None else 0.0

    state.control.solar_ueberschuss_aktiv = (
            bat_p > state.config.Solarueberschuss.BATPOWER_THRESHOLD or
            (soc_v >= state.config.Solarueberschuss.SOC_THRESHOLD and 
             feed_p > state.config.Solarueberschuss.FEEDINPOWER_THRESHOLD)
    )

    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
    
    # Frostschutz-Check: Wenn im Übergangsmodus/Solarfenster die Temp unter den Nacht-Sollwert fällt
    is_critical_frost = False
    if regelfuehler := t_mittig: # Standard sensor for these modes
        night_einschaltpunkt = state.basis_einschaltpunkt - get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
        if regelfuehler <= night_einschaltpunkt:
            is_critical_frost = True

    if state.bademodus_aktiv:
        res = {"modus": "Bademodus", "ausschaltpunkt": state.ausschaltpunkt_erhoeht, "einschaltpunkt": state.ausschaltpunkt_erhoeht - 4, "regelfuehler": t_unten}
    elif state.control.solar_ueberschuss_aktiv:
        res = {"modus": "Solarüberschuss", "ausschaltpunkt": state.ausschaltpunkt_erhoeht, "einschaltpunkt": state.einschaltpunkt_erhoeht, "regelfuehler": t_unten}
    elif within_uebergangsmodus:
        modus_name = "Übergangsmodus (Frostschutz)" if is_critical_frost else "Übergangsmodus"
        res = {"modus": modus_name, "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}
    elif is_night:
        res = {"modus": "Nachtmodus", "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}
    else:
        res = {"modus": "Normalmodus", "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}

    res["solar_ueberschuss_aktiv"] = state.control.solar_ueberschuss_aktiv
    
    if state.control.previous_modus != res["modus"]:
        # Optional: Logik für Solarüberschuss während Übergangsmodus/Regulär etc. kann hier noch feiner getrennt werden falls gewünscht.
        logging.info(f"Wechsel zu Modus: {res['modus']}")
        state.control.previous_modus = res["modus"]
    
    return res

async def handle_compressor_off(state, session, regelfuehler, ausschaltpunkt, min_laufzeit, t_oben, set_kompressor_status_func: Callable):
    """Prüft Abschaltbedingungen und schaltet aus."""
    if not state.control.kompressor_ein:
        return False

    if regelfuehler is not None and regelfuehler >= ausschaltpunkt:
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(f"Regulär AUS: Regelfühler ({regelfuehler:.1f}) >= Ziel ({ausschaltpunkt:.1f}). Laufzeit: {elapsed}")
                return True
            await handle_critical_compressor_error(session, state, "")
        else:
            state.control.blocking_reason = f"Warte auf Mindestlaufzeit (noch {int((min_laufzeit - elapsed).total_seconds() // 60)}m)"
            if check_log_throttle(state, "log_min_laufzeit_off", interval_min=5):
                logging.info(f"Abschaltwunsch unterdrückt: Mindestlaufzeit noch nicht erreicht. Laufzeit: {elapsed}")
    return False

async def handle_compressor_on(state, session, regelfuehler, einschaltpunkt, ausschaltpunkt, min_laufzeit, min_pause, within_solar_window, t_oben, set_kompressor_status_func: Callable):
    """Prüft Einschaltbedingungen und schaltet ein."""
    now = datetime.now(state.local_tz)
    temp_ok = regelfuehler is not None and regelfuehler <= einschaltpunkt
    
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
    solar_ok = True
    solar_block_reason = None
    if within_uebergangsmodus and not state.control.solar_ueberschuss_aktiv and not state.bademodus_aktiv:
        # Restore critical cold exception: allow even without solar if it's very cold
        nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
        night_einschaltpunkt = state.basis_einschaltpunkt - nacht_reduction
        if regelfuehler is not None and regelfuehler > night_einschaltpunkt:
            solar_ok = False
            solar_block_reason = "Solarfenster (kein Überschuss)"

    pause_ok = True
    pause_remaining = None
    if state.stats.last_compressor_off_time:
        elapsed_pause = safe_timedelta(now, state.stats.last_compressor_off_time, state.local_tz)
        if elapsed_pause < min_pause:
            pause_ok = False
            pause_remaining = min_pause - elapsed_pause
            

    stop_condition = (regelfuehler is not None and regelfuehler >= ausschaltpunkt) or (t_oben is not None and t_oben >= ausschaltpunkt)
    
    if not state.control.kompressor_ein and temp_ok and solar_ok and pause_ok:
        if stop_condition:
            logging.info(f"Einschalten unterdrückt: Ausschaltpunkt ({ausschaltpunkt}) bereits erreicht (Regelfühler={regelfuehler}, Oben={t_oben})")
            state.control.blocking_reason = "Zieltemp erreicht"
            return False
            
        if await set_kompressor_status_func(state, True, t_boiler_oben=t_oben):
            # Clear blocking reason on successful start
            state.control.blocking_reason = None
            logging.info(f"Eingeschaltet um {now}. Grund: Regelfühler ({regelfuehler:.1f}) <= Ein-Ziel ({einschaltpunkt:.1f})")
            return True
    
    # Set blocking reason if conditions not met
    if not state.control.kompressor_ein and temp_ok:
        if not pause_ok and pause_remaining:
            minutes = int(pause_remaining.total_seconds() // 60)
            seconds = int(pause_remaining.total_seconds() % 60)
            state.control.blocking_reason = f"Min. Pause (noch {minutes}m {seconds}s)"
        elif not solar_ok and solar_block_reason:
            state.control.blocking_reason = solar_block_reason
        else:
            state.control.blocking_reason = None
    
    return False

async def handle_mode_switch(state, session, t_oben, t_mittig, set_kompressor_status_func: Callable):
    """Schaltet aus bei Moduswechsel wenn Zieltemp erreicht."""
    if state.control.kompressor_ein and state.control.solar_ueberschuss_aktiv == False and not state.bademodus_aktiv:
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        target = state.control.aktueller_ausschaltpunkt
        
        # Check if targets reached in the new mode
        if t_oben >= target or t_mittig >= target:
            # ONLY switch off if min runtime reached
            if elapsed >= state.min_laufzeit:
                if await set_kompressor_status_func(state, False, force=True):
                    logging.info(f"Modus-Wechsel AUS: T_Oben ({t_oben:.1f}) oder T_Mittig ({t_mittig:.1f}) >= Ziel ({target:.1f}). Laufzeit: {elapsed}")
                    return True
            else:
                if check_log_throttle(state, "log_mode_switch_min_laufzeit", interval_min=5):
                    logging.info(f"Modus-Wechsel AUS unterdrückt: Mindestlaufzeit ({state.min_laufzeit}) noch nicht erreicht. Laufzeit: {elapsed}")
    return False
