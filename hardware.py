import logging
import asyncio
try:
    import RPi.GPIO as GPIO
    from RPLCD.i2c import CharLCD
except ImportError:
    logging.warning("RPi.GPIO oder RPLCD nicht verfügbar. Mocking wird aktiviert (falls nicht auf Raspberry Pi).")
    # Simple Mock classes would go here or be handled by conditional imports
    GPIO = None
    CharLCD = None

class HardwareManager:
    def __init__(self, i2c_addr=0x27, i2c_bus=1):
        self.GIO21_PIN = 21  # Kompressor
        self.PRESSURE_SENSOR_PIN = 17 # Druckschalter
        self.lcd = None
        self.i2c_addr = i2c_addr
        self.i2c_bus = i2c_bus
        self.gpio_initialized = False

    def init_gpio(self):
        """Initialisiert GPIO Pins."""
        if GPIO:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setup(self.GIO21_PIN, GPIO.OUT)
                GPIO.setup(self.PRESSURE_SENSOR_PIN, GPIO.IN)  # Legacy: Externer Pull-up vorhanden
                # Sicherstellen, dass Kompressor initial aus ist, falls Logik das verlangt? 
                # Besser: Status quo behalten oder explizit ausschalten.
                # GPIO.output(self.GIO21_PIN, GPIO.LOW) 
                self.gpio_initialized = True
                logging.info("GPIO initialisiert")
            except Exception as e:
                logging.error(f"Fehler bei GPIO Init: {e}")
        else:
            logging.info("Mock GPIO aktiviert (keine echte Hardware)")

    async def init_lcd(self):
        """Initialisiert das LCD Display."""
        if CharLCD:
            try:
                # RPLCD accesses I2C, which might block slightly, but usually fast enough.
                # Running in thread if needed, but usually fine directly.
                self.lcd = await asyncio.to_thread(
                    lambda: CharLCD('PCF8574', self.i2c_addr, port=self.i2c_bus, cols=20, rows=4)
                )
                self.lcd.clear()
                logging.info("LCD initialisiert")
            except Exception as e:
                logging.error(f"Fehler bei LCD Init: {e}")
                self.lcd = None

    def set_compressor_state(self, state: bool):
        """Schaltet den Kompressor an (True) oder aus (False)."""
        if self.gpio_initialized and GPIO:
            try:
                GPIO.output(self.GIO21_PIN, GPIO.HIGH if state else GPIO.LOW)
            except Exception as e:
                logging.error(f"Fehler beim Schalten des Kompressors: {e}")

    def read_pressure_sensor(self) -> bool:
        """Liest den Druckschalter. True = OK (Geschlossen?), False = Fehler (Offen?)."""
        # Annahme: Normal Closed? Muss Logik in main.py prüfen.
        # main.py: PRESSURE_SENSOR_PIN = 17 
        # Logik in main.py war: if GPIO.input(PRESSURE_SENSOR_PIN) == GPIO.HIGH (oder LOW?)
        # Standard PullUP -> Wenn Schalter schließt nach GND -> LOW.
        # Ich muss nachsehen, wie es in main.py verwendet wurde.
        if self.gpio_initialized and GPIO:
            try:
                # Returnwert muss zur Logik passen.
                # Legacy logic: raw_value == GPIO.LOW -> OK (True)
                return GPIO.input(self.PRESSURE_SENSOR_PIN) == GPIO.LOW
            except Exception as e:
                logging.error(f"Fehler beim Lesen des Drucksensors: {e}")
                return False # Fehler-Status als Fallback
        return True # Mock: Immer OK

    def write_lcd(self, line1="", line2="", line3="", line4=""):
        """Schreibt auf das LCD Display."""
        if self.lcd:
            try:
                self.lcd.cursor_pos = (0, 0)
                self.lcd.write_string(line1.ljust(20)[:20])
                self.lcd.cursor_pos = (1, 0)
                self.lcd.write_string(line2.ljust(20)[:20])
                self.lcd.cursor_pos = (2, 0)
                self.lcd.write_string(line3.ljust(20)[:20])
                self.lcd.cursor_pos = (3, 0)
                self.lcd.write_string(line4.ljust(20)[:20])
            except Exception as e:
                logging.error(f"Fehler beim Schreiben auf LCD: {e}")

    def cleanup(self):
        """Bereinigt GPIO und LCD Ressourcen."""
        if self.gpio_initialized and GPIO:
            GPIO.output(self.GIO21_PIN, GPIO.LOW)
            GPIO.cleanup()
            self.gpio_initialized = False
            logging.info("GPIO Cleanup durchgeführt")
        
        if self.lcd:
            try:
                self.lcd.clear()
                self.lcd.write_string("System aus")
                self.lcd.close()
            except Exception as e:
                logging.error(f"Fehler beim LCD Cleanup: {e}")
