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
        # Sensor IDs mapping (könnte auch aus Config kommen, hier fest wie in main.py)
        self.sensor_ids = {
            "oben": "28-0bd6d4461d84",
            "mittig": "28-6977d446424a",
            "unten": "28-445bd44686f4",
            "verd": "28-213bd4460d65"
        }

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
                if lines[0].strip()[-3:] == "YES":
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

    async def read_temperature(self, sensor_key: str) -> Optional[float]:
        """
        Liest die Temperatur asynchron mit Caching.
        sensor_key: 'oben', 'mittig', 'unten', 'verd'
        """
        sensor_id = self.sensor_ids.get(sensor_key)
        if not sensor_id:
            logging.error(f"Unbekannter Sensor-Key: {sensor_key}")
            return None

        now = datetime.now(pytz.timezone("Europe/Berlin"))
        
        # Cache prüfen
        if sensor_id in self.last_sensor_readings:
            last_time, value = self.last_sensor_readings[sensor_id]
            if now - last_time < self.sensor_read_interval:
                return value

        # Tatsächliches Lesen (in Thread, da Datei-IO blockieren kann)
        temp = await asyncio.to_thread(self.read_temperature_raw, sensor_id)
        
        if temp is not None:
             self.last_sensor_readings[sensor_id] = (now, temp)
        
        return temp

    async def get_all_temperatures(self) -> Dict[str, Optional[float]]:
        """Liest alle Sensoren parallel."""
        tasks = []
        keys = ["oben", "mittig", "unten", "verd"]
        for key in keys:
            tasks.append(self.read_temperature(key))
        
        results = await asyncio.gather(*tasks)
        return dict(zip(keys, results))
