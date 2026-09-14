import asyncio
import logging
import os
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, Tuple
from constants import DEFAULT_TIMEZONE, SENSOR_RETRY_COUNT

class SensorManager:
    def __init__(self, base_dir: str = "/sys/bus/w1/devices/"):
        self.base_dir = base_dir
        self.last_sensor_readings: Dict[str, Tuple[datetime, float]] = {}
        self.sensor_read_interval = timedelta(seconds=5)
        # Fehlerzähler je Sensor (Degradations-Trend). Ein zufälliger Lesefehler
        # kann passieren; eine steigende Rate deutet auf alternde Verkabelung
        # oder einen defekten Sensor hin (Empfehlung "Sensor-Degradation tracken").
        self.sensor_error_counts: Dict[str, int] = {}
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

    def _track_sensor_error(self, sensor_id: str) -> None:
        """Erhoeht den Fehlerzaehler und warnt auf logarithmischen Schwellen.

        Die Warnung erscheint bei 1, 2, 4, 8, 16, ... Fehlern (Verdopplung),
        damit eine beginnende Instabilitaet sichtbar wird, ohne bei jedem
        Einzelfehler die Konsole/Telegram zu fluten."""
        n = self.sensor_error_counts.get(sensor_id, 0) + 1
        self.sensor_error_counts[sensor_id] = n
        # "n ist Zweierpotenz" -> nur dann logan
        if n & (n - 1) == 0:
            logging.warning(
                f"Sensor-Degradation {sensor_id}: {n}. Fehler seit Start "
                f"(Trend pruefen: Verkabelung/defekter Sensor moeglich)"
            )

    def read_temperature_raw(self, sensor_id: str) -> Optional[float]:
        """Liest die Temperatur von einem DS18B20-Sensor (synchron, blocking)."""
        device_file = os.path.join(self.base_dir, sensor_id, "w1_slave")
        try:
            if not os.path.exists(device_file):
                self._track_sensor_error(sensor_id)
                return None
                
            with open(device_file, "r") as f:
                lines = f.readlines()
                if len(lines) < 2:
                    logging.error(f"Sensor {sensor_id}: Zu wenige Zeilen in w1_slave ({len(lines)})")
                    self._track_sensor_error(sensor_id)
                    return None
                if lines[0].strip()[-3:] == "YES":
                    temp_data = lines[1].split("=")[-1]
                    try:
                        temp = float(temp_data) / 1000.0
                    except ValueError:
                         logging.error(f"Fehler beim Parsen der Temperatur: {temp_data}")
                         self._track_sensor_error(sensor_id)
                         return None

                    if temp < -20 or temp > 100:
                        logging.error(f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C")
                        self._track_sensor_error(sensor_id)
                        return None
                    return temp
                else:
                    logging.warning(f"Ungültige Daten von Sensor {sensor_id}: CRC-Fehler")
                    self._track_sensor_error(sensor_id)
                    return None
        except Exception as e:
            logging.error(f"Fehler beim Lesen von Sensor {sensor_id}: {e}")
            self._track_sensor_error(sensor_id)
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

        now = datetime.now(pytz.timezone(DEFAULT_TIMEZONE))
        
        # Cache prüfen
        if sensor_id in self.last_sensor_readings:
            last_time, value = self.last_sensor_readings[sensor_id]
            if now - last_time < self.sensor_read_interval:
                return value

                # Tatsächliches Lesen mit Retry (in Thread, da Datei-IO blockieren kann)
        temp = None
        for attempt in range(SENSOR_RETRY_COUNT):
            try:
                temp = await asyncio.wait_for(asyncio.to_thread(self.read_temperature_raw, sensor_id), timeout=5.0)
                if temp is not None:
                    break
                # Bei None (z.B. CRC-Fehler): kurze Pause und nochmal versuchen
                if attempt < SENSOR_RETRY_COUNT - 1:
                    await asyncio.sleep(0.2)
            except asyncio.TimeoutError:
                logging.error(f"Timeout bei Sensor {sensor_key} ({sensor_id}) (Versuch {attempt + 1}/{SENSOR_RETRY_COUNT})")
                if attempt < SENSOR_RETRY_COUNT - 1:
                    await asyncio.sleep(0.2)
        
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
