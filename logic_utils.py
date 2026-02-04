import logging
import math
from datetime import datetime, timedelta
from typing import Optional
from utils import safe_timedelta

def is_valid_temperature(temp: Optional[float], min_temp: float = -50.0, max_temp: float = 150.0) -> bool:
    """Prüft, ob ein Temperaturwert gültig ist."""
    if temp is None: return False
    if not isinstance(temp, (int, float)): return False
    if math.isnan(temp) or math.isinf(temp): return False
    if temp < min_temp or temp > max_temp: return False
    return True

def check_log_throttle(state, attribute_name: str, interval_minutes: float = 5.0) -> bool:
    """Prüft, ob eine Log-Nachricht gesendet werden soll (Throttling)."""
    last_time = getattr(state, attribute_name, None)
    now = datetime.now(state.local_tz)
    if last_time is None or safe_timedelta(now, last_time, state.local_tz) > timedelta(minutes=interval_minutes):
        setattr(state, attribute_name, now)
        return True
    return False

def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    try:
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
    now = datetime.now(state.local_tz)
    try:
        end_time_str = config.Heizungssteuerung.NACHTABSENKUNG_END
        end_hour, end_minute = map(int, end_time_str.split(':'))
        potential_night_setback_end_today = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        
        if now < potential_night_setback_end_today + timedelta(hours=2):
            night_setback_end_time_today = potential_night_setback_end_today
        else:
            night_setback_end_time_today = potential_night_setback_end_today + timedelta(days=1)
        
        within_solar = night_setback_end_time_today <= now < night_setback_end_time_today + timedelta(hours=2)
        return within_solar
    except Exception as e:
        logging.error(f"Fehler in is_solar_window: {e}")
        return False

def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob aktuell der Übergangsmodus (morgens oder abends) aktiv ist."""
    try:
        now_time = datetime.now(state.local_tz).time()
        morgens_aktiv = state.nachtabsenkung_ende <= now_time <= state.uebergangsmodus_morgens_ende
        abends_aktiv = state.uebergangsmodus_abends_start <= now_time <= state.nachtabsenkung_start
        return morgens_aktiv or abends_aktiv
    except Exception as e:
        logging.error(f"Fehler bei ist_uebergangsmodus_aktiv: {e}")
        return False

def get_validated_reduction(config, section: str, key: str, default: float = 0.0) -> float:
    """Validiert und gibt Temperaturreduktionswerte zurück."""
    try:
        section_obj = getattr(config, section, None)
        if section_obj:
            value = getattr(section_obj, key, default)
        else:
            return default
        reduction = float(value)
        if reduction < 0 or reduction > 35:
            return default
        return reduction
    except:
        return default
