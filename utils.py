from datetime import datetime, timedelta
import pytz
import logging

def safe_timedelta(now: datetime, timestamp: datetime, local_tz: pytz.BaseTzInfo, default: timedelta = timedelta()) -> timedelta:
    """
    Berechnet die Zeitdifferenz zwischen zwei Zeitstempeln mit Zeitzonensicherheit.

    Args:
        now: Erster Zeitstempel (meist aktueller Zeitpunkt).
        timestamp: Zweiter Zeitstempel (Vergleichszeitpunkt).
        local_tz: Lokale Zeitzone (z.B. pytz.timezone("Europe/Berlin")).
        default: Standardwert, falls die Berechnung fehlschlägt.

    Returns:
        timedelta: Die berechnete Zeitdifferenz oder der default-Wert bei Fehlern.
    """
from datetime import datetime, timedelta
import pytz
import logging

def safe_timedelta(now: datetime, timestamp: datetime, local_tz: pytz.BaseTzInfo, default: timedelta = timedelta()) -> timedelta:
    """
    Berechnet die Zeitdifferenz zwischen zwei Zeitstempeln mit Zeitzonensicherheit.

    Args:
        now: Erster Zeitstempel (meist aktueller Zeitpunkt).
        timestamp: Zweiter Zeitstempel (Vergleichszeitpunkt).
        local_tz: Lokale Zeitzone (z.B. pytz.timezone("Europe/Berlin")).
        default: Standardwert, falls die Berechnung fehlschlägt.

    Returns:
        timedelta: Die berechnete Zeitdifferenz oder der default-Wert bei Fehlern.
    """
    try:
        if now.tzinfo is None:
            now = local_tz.localize(now)
        if timestamp.tzinfo is None:
            timestamp = local_tz.localize(timestamp)
        return now - timestamp
    except Exception as e:
        logging.error(f"Fehler bei safe_timedelta: {e}")
        return default


def safe_float(value, default=0.0, field_name="unknown"):
    """
    Safely convert value to float with comprehensive validation.
    
    Args:
        value: Value to convert (int, float, str, None)
        default: Fallback value if conversion fails
        field_name: Field name for logging
    
    Returns:
        float: Converted value or default
    """
    try:
        if value is None:
            logging.warning(f"API: {field_name} is None, using {default}")
            return default
        
        if isinstance(value, (int, float)):
            return float(value)
        
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in ['n/a', 'null', 'none', 'error', '-']:
                logging.warning(f"API: {field_name}='{value}' invalid, using {default}")
                return default
            return float(value)
        
        logging.error(f"API: {field_name} unexpected type {type(value).__name__}, using {default}")
        return default
    except (ValueError, TypeError) as e:
        logging.error(f"API: Cannot convert {field_name}='{value}': {e}, using {default}")
        return default