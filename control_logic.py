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
    state.last_compressor_off_time = time_val

async def check_pressure_and_config(session, state, handle_pressure_check_func: Callable, set_kompressor_status_func: Callable, reload_config_func: Callable, calculate_file_hash_func: Callable, only_pressure: bool = False):
    """Prüft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.ausschluss_grund = "Druckschalterfehler"
        if state.kompressor_ein: await set_kompressor_status_func(state, False, force=True)
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

    bat_p = state.batpower if state.batpower is not None else 0.0
    soc_v = state.soc if state.soc is not None else 0.0
    feed_p = state.feedinpower if state.feedinpower is not None else 0.0

    state.solar_ueberschuss_aktiv = (
            bat_p > state.config.Solarueberschuss.BATPOWER_THRESHOLD or
            (soc_v >= state.config.Solarueberschuss.SOC_THRESHOLD and 
             feed_p > state.config.Solarueberschuss.FEEDINPOWER_THRESHOLD)
    )

    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)

    if state.bademodus_aktiv:
        res = {"modus": "Bademodus", "ausschaltpunkt": state.ausschaltpunkt_erhoeht, "einschaltpunkt": state.ausschaltpunkt_erhoeht - 4, "regelfuehler": t_unten}
    elif state.solar_ueberschuss_aktiv:
        res = {"modus": "Solarüberschuss", "ausschaltpunkt": state.ausschaltpunkt_erhoeht, "einschaltpunkt": state.einschaltpunkt_erhoeht, "regelfuehler": t_unten}
    elif within_uebergangsmodus:
        res = {"modus": "Übergangsmodus", "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}
    elif is_night:
        res = {"modus": "Nachtmodus", "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}
    else:
        res = {"modus": "Normalmodus", "ausschaltpunkt": state.basis_ausschaltpunkt - total_reduction, "einschaltpunkt": state.basis_einschaltpunkt - total_reduction, "regelfuehler": t_mittig}

    res["solar_ueberschuss_aktiv"] = state.solar_ueberschuss_aktiv
    
    if state.previous_modus != res["modus"]:
        logging.info(f"Wechsel zu Modus: {res['modus']}")
        state.previous_modus = res["modus"]
    
    return res

async def handle_compressor_off(state, session, regelfuehler, ausschaltpunkt, min_laufzeit, t_oben, set_kompressor_status_func: Callable):
    """Prüft Abschaltbedingungen und schaltet aus."""
    if regelfuehler is not None and regelfuehler >= ausschaltpunkt:
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.kompressor_ein = False
                set_last_compressor_off_time(state, datetime.now(state.local_tz))
                state.total_runtime_today += elapsed
                state.last_completed_cycle = datetime.now(state.local_tz)
                logging.info(f"Ausgeschaltet. Laufzeit: {elapsed}")
                return True
            await handle_critical_compressor_error(session, state, "")
    return False

async def handle_compressor_on(state, session, regelfuehler, einschaltpunkt, min_laufzeit, min_pause, within_solar_window, t_oben, set_kompressor_status_func: Callable):
    """Prüft Einschaltbedingungen und schaltet ein."""
    now = datetime.now(state.local_tz)
    temp_ok = regelfuehler is not None and regelfuehler <= einschaltpunkt
    
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
    solar_ok = True
    if within_uebergangsmodus and not state.solar_ueberschuss_aktiv and not state.bademodus_aktiv:
        # Restore critical cold exception: allow even without solar if it's very cold
        nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
        night_einschaltpunkt = state.basis_einschaltpunkt - nacht_reduction
        if regelfuehler is not None and regelfuehler > night_einschaltpunkt:
            solar_ok = False

    pause_ok = True
    if state.last_compressor_off_time:
        if safe_timedelta(now, state.last_compressor_off_time, state.local_tz) < min_pause:
            pause_ok = False

    if not state.kompressor_ein and temp_ok and solar_ok and pause_ok:
        if await set_kompressor_status_func(state, True, t_boiler_oben=t_oben):
            state.kompressor_ein = True
            state.last_compressor_on_time = now
            state.kompressor_verification_start_time = now
            state.kompressor_verification_start_t_verd = state.t_verd
            state.kompressor_verification_start_t_unten = state.t_unten
            logging.info(f"Eingeschaltet um {now}")
            return True
    return False

async def handle_mode_switch(state, session, t_oben, t_mittig, set_kompressor_status_func: Callable):
    """Schaltet aus bei Moduswechsel wenn Zieltemp erreicht."""
    if state.kompressor_ein and state.solar_ueberschuss_aktiv == False and not state.bademodus_aktiv:
        if t_oben >= state.aktueller_ausschaltpunkt or t_mittig >= state.aktueller_ausschaltpunkt:
            if await set_kompressor_status_func(state, False, force=True):
                state.kompressor_ein = False
                set_last_compressor_off_time(state, datetime.now(state.local_tz))
                return True
    return False
