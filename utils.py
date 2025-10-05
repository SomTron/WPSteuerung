import logging
import os
from datetime import datetime, timedelta
import pytz

BASE_DIR = "/sys/bus/w1/devices/"  # Global for sensor path


def safe_timedelta(now, then, tz):
    """Berechnet die Zeitdifferenz zwischen zwei Datumsangaben."""
    try:
        if then.tzinfo is None:
            then = tz.localize(then)
        if now.tzinfo is None:
            now = tz.localize(now)
        return now - then
    except Exception as e:
        logging.error(f"Fehler in safe_timedelta: {e}")
        return timedelta(seconds=0)


def read_temperature(sensor_id):
    """Liest die Temperatur von einem DS18B20-Sensor.

    Args:
        sensor_id (str): Die ID des Sensors (z.B. '28-0bd6d4461d84').

    Returns:
        float or None: Die Temperatur in °C oder None bei Fehlern.
    """
    device_file = os.path.join(BASE_DIR, sensor_id, "w1_slave")
    try:
        with open(device_file, "r") as f:
            lines = f.readlines()
            if len(lines) < 2:
                logging.error(f"Sensor {sensor_id}: Zu wenige Zeilen in w1_slave ({len(lines)})")
                return None
            if lines[0].strip()[-3:] == "YES":
                temp_data = lines[1].split("=")[-1]
                temp = float(temp_data) / 1000.0
                if temp < -20 or temp > 100:
                    logging.error(f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C")
                    return None
                logging.debug(f"Temperatur von Sensor {sensor_id} gelesen: {temp} °C")
                return temp
            else:
                logging.warning(f"Ungültige Daten von Sensor {sensor_id}: CRC-Fehler")
                return None
    except FileNotFoundError:
        logging.error(f"Sensor-Datei nicht gefunden: {device_file}")
        return None
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Sensors {sensor_id}: {str(e)}")
        return None


def is_nighttime(config):
    """Prüft, ob die aktuelle Uhrzeit in der Nachtabsenkungszeit liegt."""
    try:
        now = datetime.now(pytz.timezone("Europe/Berlin"))
        start_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_START", "22:00")
        end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")
        start_hour, start_minute = map(int, start_time_str.split(':'))
        end_hour, end_minute = map(int, end_time_str.split(':'))
        start_time = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
        end_time = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if start_hour >= end_hour:
            end_time += timedelta(days=1)
        if start_time <= now < end_time:
            return True
        return False
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False


def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob der Übergangsmodus (Solarfenster) aktiv ist."""
    from telegram_handler import is_solar_window  # Import here to avoid circularity
    try:
        return is_solar_window(state.config, state)
    except Exception as e:
        logging.error(f"Fehler in ist_uebergangsmodus_aktiv: {e}")
        return False