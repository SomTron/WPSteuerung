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