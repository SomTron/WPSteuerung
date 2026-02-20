import asyncio
import logging
import os
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, Tuple

class SensorManager:
    def __init__(self, base_dir: str = "/sys/bus/w1/devices/"):
        self.base_dir = base_dir
        self.last_sensor_readings: Dict[str, Tuple[datetime, float]] = {}
        self.sensor_read_interval = timedelta(seconds=5)
        self.tz = pytz.timezone("Europe/Berlin")
        # Sensor IDs mapping (könnte auch aus Config kommen, hier fest wie in main.py)
        self.sensor_ids = {
            "oben": "28-0bd6d4461d84",
            "mittig": "28-6977d446424a",
            "unten": "28-445bd44686f4",
            "verd": "28-213bd4460d65",
            "vorlauf": "28-2ce8d446a504"
        }
        # Consecutive failure tracking per sensor key
        self.consecutive_failures: Dict[str, int] = {key: 0 for key in self.sensor_ids}
        self.max_consecutive_failures: int = 5
        self.critical_failure: bool = False
        self.critical_failure_sensor: Optional[str] = None

    def reset_cache(self):
        """Leert den Temperatur-Cache."""
        self.last_sensor_readings.clear()
        logging.debug("Sensor-Cache geleert")

    def read_temperature_raw(self, sensor_id: str) -> Optional[float]:
        """Liest die Temperatur von einem DS18B20-Sensor (synchron, blocking)."""
        device_file = os.path.join(self.base_dir, sensor_id, "w1_slave")
        try:
            if not os.path.exists(device_file):
                # logging.warning(f"Sensor-Datei nicht gefunden: {device_file}")
                return None
                
            with open(device_file, "r") as f:
                lines = f.readlines()
                if len(lines) < 2:
                    logging.error(f"Sensor {sensor_id}: Zu wenige Zeilen in w1_slave ({len(lines)})")
                    return None
                if lines[0].strip().endswith("YES"):
                    temp_data = lines[1].split("=")[-1]
                    try:
                        temp = float(temp_data) / 1000.0
                    except ValueError:
                         logging.error(f"Fehler beim Parsen der Temperatur: {temp_data}")
                         return None

                    if temp < -20 or temp > 100:
                        logging.error(f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C")
                        return None
                    return temp
                else:
                    logging.warning(f"Ungültige Daten von Sensor {sensor_id}: CRC-Fehler")
                    return None
        except Exception as e:
            logging.error(f"Fehler beim Lesen von Sensor {sensor_id}: {e}")
            return None

    async def read_temperature(self, sensor_key: str, retries: int = 3) -> Optional[float]:
        """
        Liest die Temperatur asynchron mit Caching und Retry-Logik.
        sensor_key: 'oben', 'mittig', 'unten', 'verd', 'vorlauf'
        """
        sensor_id = self.sensor_ids.get(sensor_key)
        if not sensor_id:
            logging.error(f"Unbekannter Sensor-Key: {sensor_key}")
            return None

        now = datetime.now(self.tz)
        
        # Cache prüfen (Key = sensor_key für Konsistenz)
        if sensor_key in self.last_sensor_readings:
            last_time, value = self.last_sensor_readings[sensor_key]
            if now - last_time < self.sensor_read_interval:
                return value

        # Tatsächliches Lesen mit Retry-Logik
        for attempt in range(retries):
            try:
                temp = await asyncio.wait_for(asyncio.to_thread(self.read_temperature_raw, sensor_id), timeout=5.0)
                if temp is not None:
                    self.last_sensor_readings[sensor_key] = (now, temp)
                    self.consecutive_failures[sensor_key] = 0
                    return temp
                
                # Wenn temp None ist (z.B. CRC-Fehler), auch retryen
                if attempt < retries - 1:
                    logging.warning(f"Sensor {sensor_key} ({sensor_id}) lieferte None. Retry {attempt + 1}/{retries}...")
                    await asyncio.sleep(0.2)
            except asyncio.TimeoutError:
                if attempt < retries - 1:
                    logging.warning(f"Timeout bei Sensor {sensor_key} ({sensor_id}). Retry {attempt + 1}/{retries}...")
                    await asyncio.sleep(0.2)
                else:
                    logging.error(f"Finaler Timeout bei Sensor {sensor_key} ({sensor_id}) nach {retries} Versuchen.")
            except Exception as e:
                logging.error(f"Unerwarteter Fehler beim Lesen von Sensor {sensor_key}: {e}")
                break
        
        # Alle Retries fehlgeschlagen → Consecutive-Failure-Counter erhöhen
        self.consecutive_failures[sensor_key] = self.consecutive_failures.get(sensor_key, 0) + 1
        fail_count = self.consecutive_failures[sensor_key]
        logging.warning(f"Sensor {sensor_key}: {fail_count}/{self.max_consecutive_failures} aufeinanderfolgende Fehler")
        
        if fail_count >= self.max_consecutive_failures:
            self.critical_failure = True
            self.critical_failure_sensor = sensor_key
            logging.critical(f"KRITISCH: Sensor {sensor_key} hat {fail_count}x hintereinander versagt! Sicherheitsabschaltung wird ausgelöst.")
        
        return None

    async def get_all_temperatures(self) -> Dict[str, Optional[float]]:
        """Liest alle Sensoren parallel."""
        keys = list(self.sensor_ids.keys())
        tasks = [self.read_temperature(key) for key in keys]
        results = await asyncio.gather(*tasks)
        return dict(zip(keys, results))
