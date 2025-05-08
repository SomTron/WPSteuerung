import os
import sys
import smbus2
import pytz
from datetime import datetime, timedelta
import time
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
from logging.handlers import RotatingFileHandler
import configparser
import aiohttp
import hashlib
from telegram import ReplyKeyboardMarkup
import asyncio
import aiofiles
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
from aiohttp import FormData
import pandas as pd
import numpy as np
from dateutil.relativedelta import relativedelta
from telegram_handler import (send_telegram_message, send_welcome_message, telegram_task, get_runtime_bar_chart,
                              get_boiler_temperature_history)


# Basisverzeichnis für Temperatursensoren und Sensor-IDs
BASE_DIR = "/sys/bus/w1/devices/"
SENSOR_IDS = {
    "oben": "28-0bd6d4461d84",
    "mittig": "28-6977d446424a",
    "unten": "28-445bd44686f4",
    "verd": "28-213bd4460d65"
}

# I2C-Adresse und Busnummer für das LCD
I2C_ADDR = 0x27
I2C_BUS = 1
# API-URL für SolaxCloud
API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"
# GPIO-Pins
GIO21_PIN = 21  # Ausgang für Kompressor
PRESSURE_SENSOR_PIN = 17  # Eingang für Druckschalter

# Konfigurationsdatei einlesen
config = configparser.ConfigParser()
config.read("config.ini")

# Globale Variablen initialisieren

AUSSCHALTPUNKT = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45))
AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 52))
EINSCHALTPUNKT = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42))
TEMP_OFFSET = int(config["Heizungssteuerung"].get("TEMP_OFFSET", 3))
VERDAMPFERTEMPERATUR = int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"])
MIN_LAUFZEIT = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))
MIN_PAUSE = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))
UNTERER_FUEHLER_MIN = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MIN", 45))
UNTERER_FUEHLER_MAX = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MAX", 50))



# Globale Variablen für den Programmstatus
last_update_id = None
lcd = None
csv_lock = asyncio.Lock()
gpio_lock = asyncio.Lock()
last_sensor_readings = {}
SENSOR_READ_INTERVAL = timedelta(seconds=5)


PRESSURE_ERROR_DELAY = timedelta(minutes=5)  # 5 Minuten Verzögerung



local_tz = pytz.timezone("Europe/Berlin")
logging.info(f"Programm gestartet: {datetime.now(local_tz)}")

# Neuer Telegram-Handler für Logging
class TelegramHandler(logging.Handler):
    def __init__(self, bot_token, chat_id, session, level=logging.NOTSET):
        super().__init__(level)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.queue = asyncio.Queue()
        self.task = None

    async def send_message(self, message):
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message}
        try:
            async with self.session.post(url, json=payload) as response:
                if response.status != 200:
                    logging.error(f"Telegram send failed: {await response.text()}")
        except Exception as e:
            logging.error(f"Fehler beim Senden an Telegram: {e}", exc_info=True)

    def emit(self, record):
        try:
            msg = self.format(record)
            # Stelle sicher, dass emit im Event-Loop ausgeführt wird
            loop = asyncio.get_event_loop()
            if loop.is_running():
                self.queue.put_nowait(msg)
                if not self.task or self.task.done():
                    self.task = loop.create_task(self.process_queue())
            else:
                # Fallback für keinen laufenden Event-Loop
                time.sleep(0.1)  # Verhindere Blockierung
        except Exception as e:
            logging.error(f"Fehler in TelegramHandler.emit: {e}", exc_info=True)

    async def process_queue(self):
        while not self.queue.empty():
            msg = await self.queue.get()
            await self.send_message(msg)
            self.queue.task_done()

    def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
        super().close()


class State:
    def __init__(self, config):
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)

        # --- Basiswerte ---
        self.gpio_lock = asyncio.Lock()
        self.session = None
        self.config = config

        # --- Laufzeitstatistik ---
        self.current_runtime = timedelta()
        self.last_runtime = timedelta()
        self.total_runtime_today = timedelta()
        self.last_day = now.date()
        self.start_time = None
        self.last_compressor_on_time = None
        self.last_compressor_off_time = None
        self.last_shutdown_time = now  # <-- Jetzt vorhanden!
        self.last_log_time = now - timedelta(minutes=1)
        self.last_kompressor_status = None

        # --- Steuerungslogik ---
        self.kompressor_ein = False
        self.urlaubsmodus_aktiv = False
        self.solar_ueberschuss_aktiv = False
        self.ausschluss_grund = None
        self.t_boiler = None  # Durchschnittliche Boiler-Temperatur

        # --- Telegram-Konfiguration ---
        self.bot_token = config["Telegram"].get("BOT_TOKEN")
        self.chat_id = config["Telegram"].get("CHAT_ID")
        if not self.bot_token or not self.chat_id:
            logging.warning("Telegram BOT_TOKEN oder CHAT_ID fehlt. Telegram-Nachrichten deaktiviert.")

        # --- SolaxCloud-Konfiguration ---
        self.token_id = config["SolaxCloud"].get("TOKEN_ID")
        self.sn = config["SolaxCloud"].get("SN")
        if not self.token_id or not self.sn:
            logging.warning("SolaxCloud TOKEN_ID oder SN fehlt. Solax-Datenabruf eingeschränkt.")

        # --- Heizungsparameter ---
        try:
            self.min_laufzeit = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_LAUFZEIT", 10)))
            self.min_pause = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_PAUSE", 20)))
            self.verdampfertemperatur = int(config["Heizungssteuerung"].get("VERDAMPFERTEMPERATUR", 6))
        except ValueError as e:
            logging.error(f"Fehler beim Parsen von Heizungsparametern: {e}")
            self.min_laufzeit = timedelta(minutes=10)
            self.min_pause = timedelta(minutes=20)
            self.verdampfertemperatur = 6

        self.last_api_call = None
        self.last_api_data = None
        self.last_api_timestamp = None

        # --- Schwellwerte ---
        try:
            self.aktueller_ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45))
            self.aktueller_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42))
            min_hysteresis = int(config["Heizungssteuerung"].get("TEMP_OFFSET", 3))

            if self.aktueller_ausschaltpunkt <= self.aktueller_einschaltpunkt:
                logging.warning(
                    f"Ausschaltpunkt ({self.aktueller_ausschaltpunkt}°C) <= Einschaltpunkt ({self.aktueller_einschaltpunkt}°C), "
                    f"setze Ausschaltpunkt auf Einschaltpunkt + {min_hysteresis}°C"
                )
                self.aktueller_ausschaltpunkt = self.aktueller_einschaltpunkt + min_hysteresis
        except ValueError as e:
            logging.error(f"Fehler beim Einlesen der Schwellwerte: {e}")
            self.aktueller_ausschaltpunkt = 45
            self.aktueller_einschaltpunkt = 42

        # --- Fehler- und Statuszustände ---
        self.last_config_hash = calculate_file_hash("config.ini")
        self.pressure_error_sent = False
        self.last_pressure_error_time = None
        self.last_pressure_state = None
        self.last_pause_log = None  # Zeitpunkt der letzten Pause-Meldung
        self.current_pause_reason = None  # Grund für aktuelle Pause

        # --- Zusätzliche Flags ---
        self.previous_einschaltpunkt = None
        self.previous_solar_ueberschuss_aktiv = False

        # ✅ Debugging erst NACH Initialisierung
        logging.debug(f" - Letzte Abschaltung: {self.last_shutdown_time}")

# Logging einrichten mit Telegram-Handler
async def setup_logging(session, state):
    """
    Richtet das Logging ein, inklusive RotatingFileHandler und optional TelegramHandler.
    Wird nur einmal beim Start aufgerufen.
    """
    try:
        # Entferne alle bisherigen Handler, um Duplikate zu vermeiden
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)

        # Setze Basis-Level
        root_logger.setLevel(logging.DEBUG)

        # --- FileHandler: RotatingFileHandler mit UTF-8 ---
        file_handler = RotatingFileHandler(
            "heizungssteuerung.log",
            maxBytes=100 * 1024 * 1024,  # 100 MB
            backupCount=5,
            encoding="utf-8"
        )
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            "%(asctime)s %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S %z"
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)

        # --- StreamHandler für Konsolenausgabe ---
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setLevel(logging.INFO)
        stream_handler.setFormatter(file_formatter)  # Gleiche Formatierung wie im File
        root_logger.addHandler(stream_handler)

        # --- TelegramHandler nur hinzufügen, wenn Token und Chat-ID vorhanden ---
        if state.bot_token and state.chat_id:
            telegram_handler = TelegramHandler(state.bot_token, state.chat_id, session, level=logging.WARNING)
            telegram_handler.setFormatter(logging.Formatter("%(message)s"))
            root_logger.addHandler(telegram_handler)
            logging.debug("TelegramHandler erfolgreich zum Logging hinzugefügt")
        else:
            logging.warning("Telegram-Benachrichtigungen deaktiviert (fehlendes Token oder Chat-ID)")

        logging.debug("Logging vollständig konfiguriert")

    except Exception as e:
        print(f"Fehler bei Logging-Setup: {e}", file=sys.stderr)
        raise


def reset_sensor_cache():
    """Leert den Temperatur-Cache, um nach Fehlern frische Werte zu lesen."""
    global last_sensor_readings
    last_sensor_readings.clear()
    logging.debug("Sensor-Cache geleert (reset_sensor_cache())")

async def read_temperature_cached(sensor_id):
    now = datetime.now(pytz.timezone("Europe/Berlin"))
    if sensor_id in last_sensor_readings:
        last_time, value = last_sensor_readings[sensor_id]
        if now - last_time < SENSOR_READ_INTERVAL:
            return value
    # Lese tatsächlich
    temp = await asyncio.to_thread(read_temperature, sensor_id)
    last_sensor_readings[sensor_id] = (now, temp)
    return temp

# Funktion zur LCD-Initialisierung (angepasst)
async def initialize_lcd(session):
    global lcd
    try:
        lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)
        lcd.clear()
        logging.info("LCD erfolgreich initialisiert")
    except Exception as e:
        logging.error(f"Fehler bei der LCD-Initialisierung: {e}")
        lcd = None


# Asynchrone Funktion zum Abrufen von Solax-Daten
async def get_solax_data(session, state):
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    logging.debug(f"get_solax_data: now={now}, tzinfo={now.tzinfo}, last_api_call={state.last_api_call}, tzinfo={state.last_api_call.tzinfo if state.last_api_call else None}")

    # Stelle sicher, dass state.last_api_call zeitzonenbewusst ist
    if state.last_api_call and state.last_api_call.tzinfo is None:
        state.last_api_call = local_tz.localize(state.last_api_call)
        logging.debug(f"state.last_api_call lokalisiert: {state.last_api_call}")

    if state.last_api_call and (now - state.last_api_call) < timedelta(minutes=5):
        logging.debug("Verwende zwischengespeicherte API-Daten.")
        return state.last_api_data

    max_retries = 3
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            params = {"tokenId": state.token_id, "sn": state.sn}
            async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("success"):
                    state.last_api_data = data.get("result")
                    state.last_api_timestamp = now
                    state.last_api_call = now
                    logging.debug(f"Solax-Daten erfolgreich abgerufen: {state.last_api_data}")
                    return state.last_api_data
                else:
                    logging.error(f"API-Fehler: {data.get('exception', 'Unbekannter Fehler')}")
                    return None
        except aiohttp.ClientError as e:
            logging.error(f"Fehler bei der API-Anfrage (Versuch {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Maximale Wiederholungen erreicht, verwende Fallback-Daten.")
                fallback_data = {
                    "acpower": 0,
                    "feedinpower": 0,
                    "batPower": 0,
                    "soc": 0,
                    "powerdc1": 0,
                    "powerdc2": 0,
                    "consumeenergy": 0,
                    "api_fehler": True
                }
                return fallback_data

async def fetch_solax_data(session, state, now):
    """
    Holt die aktuellen Solax-Daten und gibt sie mit Fallback-Werten zurück.
    """
    fallback_data = {
        "acpower": 0,
        "feedinpower": 0,
        "consumeenergy": 0,
        "batPower": 0,
        "soc": 0,
        "powerdc1": 0,
        "powerdc2": 0,
        "api_fehler": True
    }

    try:
        solax_data = await get_solax_data(session, state) or fallback_data.copy()

        # Upload-Zeit prüfen und Verzögerung berechnen (mit Zeitzone)
        if "utcDateTime" in solax_data:
            upload_time = pd.to_datetime(solax_data["utcDateTime"]).tz_convert("Europe/Berlin")
            delay = (now - upload_time).total_seconds()
            logging.debug(f"Solax-Datenverzögerung: {delay:.1f} Sekunden")

        acpower = solax_data.get("acpower", "N/A")
        feedinpower = solax_data.get("feedinpower", "N/A")
        batPower = solax_data.get("batPower", "N/A")
        soc = solax_data.get("soc", "N/A")
        powerdc1 = solax_data.get("powerdc1", "N/A")
        powerdc2 = solax_data.get("powerdc2", "N/A")
        consumeenergy = solax_data.get("consumeenergy", "N/A")

        return {
            "solax_data": solax_data,
            "acpower": acpower,
            "feedinpower": feedinpower,
            "batPower": batPower,
            "soc": soc,
            "powerdc1": powerdc1,
            "powerdc2": powerdc2,
            "consumeenergy": consumeenergy,
        }

    except Exception as e:
        logging.error(f"Fehler beim Abrufen von Solax-Daten: {e}", exc_info=True)

        # Fallback-Werte setzen
        return {
            "solax_data": fallback_data,
            "acpower": "N/A",
            "feedinpower": "N/A",
            "batPower": "N/A",
            "soc": "N/A",
            "powerdc1": "N/A",
            "powerdc2": "N/A",
            "consumeenergy": "N/A",
        }

def get_power_source(solax_data):
    pv_production = solax_data.get("powerdc1", 0) + solax_data.get("powerdc2", 0)
    bat_power = solax_data.get("batPower", 0)
    feedin_power = solax_data.get("feedinpower", 0)
    consumption = solax_data.get("consumeenergy", 0)

    # Neue Bedingung: Wenn die negative batPower größer ist als die PV-Produktion
    if bat_power < 0 and abs(bat_power) > pv_production:
        return "Strom aus der Batterie"

    # Bestehende Bedingungen mit Anpassungen
    if pv_production > 0 and (bat_power >= 0 or feedin_power > 0):
        return "Direkter PV-Strom"

    if pv_production == 0 and bat_power == 0 and feedin_power == 0:
        return "Keine aktive Energiequelle"

    elif feedin_power < 0:
        return "Strom vom Netz"

    elif bat_power < 0:
        return "Strom aus der Batterie"

    else:
        return "Unbekannt"  # Fallback für edge cases wie batPower = 0, feedinpower = 0


def calculate_runtimes():
    try:
        # Lese die CSV-Datei
        df = pd.read_csv("heizungsdaten.csv", on_bad_lines="skip", parse_dates=["Zeitstempel"])

        # Aktuelles Datum mit Zeitzone
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)

        # Zeitzone für Zeitstempel in der CSV sicherstellen
        if df["Zeitstempel"].dt.tz is None:
            df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz)
        else:
            df["Zeitstempel"] = df["Zeitstempel"].dt.tz_convert(local_tz)

        logging.debug(f"calculate_runtimes: now={now}, tzinfo={now.tzinfo}, Zeitstempel tz={df['Zeitstempel'].dt.tz}")

        # Zeiträume definieren
        time_periods = {
            "Aktuelle Woche": (now - timedelta(days=7), now),
            "Vorherige Woche": (now - timedelta(days=14), now - timedelta(days=7)),
            "Aktueller Monat": (now - timedelta(days=30), now),
            "Vorheriger Monat": (now - timedelta(days=60), now - timedelta(days=30)),
        }

        # Berechne die Laufzeiten für jeden Zeitraum
        runtimes = {}
        for period, (start_date, end_date) in time_periods.items():
            logging.debug(
                f"Zeitraum {period}: start_date={start_date}, tzinfo={start_date.tzinfo}, end_date={end_date}, tzinfo={end_date.tzinfo}")
            runtime_percentage, runtime_duration = calculate_runtime(df, start_date, end_date)
            runtimes[period] = {
                "percentage": runtime_percentage,
                "duration": runtime_duration
            }

        return runtimes
    except Exception as e:
        logging.error(f"Fehler beim Berechnen der Laufzeiten: {e}")
        return None


def calculate_runtime(df, start_date, end_date):
    """Berechnet die Laufzeit in Prozent und die tatsächliche Laufzeit für einen bestimmten Zeitraum."""
    # Filtere die Daten für den Zeitraum
    mask = (df["Zeitstempel"] >= start_date) & (df["Zeitstempel"] < end_date)
    filtered_df = df.loc[mask]

    # Initialisiere Variablen für die Laufzeitberechnung
    total_runtime = timedelta()  # Gesamtlaufzeit
    previous_time = None
    kompressor_was_on = False

    # Iteriere durch die gefilterten Daten
    for index, row in filtered_df.iterrows():
        current_time = row["Zeitstempel"]
        kompressor_is_on = row["Kompressor"] == "EIN"

        # Berechne die Zeitdifferenz zum vorherigen Eintrag
        if previous_time is not None:
            time_diff = current_time - previous_time

            # Wenn der Kompressor eingeschaltet war, addiere die Zeitdifferenz zur Laufzeit
            if kompressor_was_on:
                total_runtime += time_diff

        # Aktualisiere den vorherigen Zeitstempel und den Kompressorstatus
        previous_time = current_time
        kompressor_was_on = kompressor_is_on

    # Gesamtzeit des Zeitraums in Stunden
    total_hours = (end_date - start_date).total_seconds() / 3600

    # Laufzeit in Prozent
    runtime_percentage = (total_runtime.total_seconds() / 3600 / total_hours) * 100

    # Tatsächliche Laufzeit in Stunden und Minuten
    runtime_hours = int(total_runtime.total_seconds() // 3600)
    runtime_minutes = int((total_runtime.total_seconds() % 3600) // 60)
    runtime_duration = f"{runtime_hours}h {runtime_minutes}min"

    return runtime_percentage, runtime_duration


async def send_runtimes_telegram(session, state):  # Nimm 'state' als Argument entgegen
    """Sendet die Laufzeiten über Telegram."""
    runtimes = calculate_runtimes()
    if runtimes:
        message = (
            "⏱️ Laufzeiten:\n\n"
            f"• Aktuelle Woche: {runtimes['Aktuelle Woche']['percentage']:.1f}% ({runtimes['Aktuelle Woche']['duration']})\n"
            f"• Vorherige Woche: {runtimes['Vorherige Woche']['percentage']:.1f}% ({runtimes['Vorherige Woche']['duration']})\n"
            f"• Aktueller Monat: {runtimes['Aktueller Monat']['percentage']:.1f}% ({runtimes['Aktueller Monat']['duration']})\n"
            f"• Vorheriger Monat: {runtimes['Vorheriger Monat']['percentage']:.1f}% ({runtimes['Vorheriger Monat']['duration']})\n"
        )
        await send_telegram_message(session, state.chat_id, message)  # Verwende state.chat_id
    else:
        await send_telegram_message(session, state.chat_id, "Fehler beim Abrufen der Laufzeiten.")  # Verwende state.chat_id


# test.py (angepasste shutdown-Funktion)
async def shutdown(session, state):  # Nimm 'state' als Argument entgegen
    """Führt die Abschaltprozedur durch und informiert über Telegram."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        logging.debug(f"shutdown: now={now}, tzinfo={now.tzinfo}")

        # Nur GPIO.output aufrufen, wenn GPIO noch initialisiert ist
        if GPIO.getmode() is not None:  # Prüft, ob ein Modus (BCM oder BOARD) gesetzt ist
            GPIO.output(GIO21_PIN, GPIO.LOW)
            logging.info("Kompressor GPIO auf LOW gesetzt")
        else:
            logging.warning("GPIO-Modus nicht gesetzt, überspringe GPIO.output")

        message = f"🛑 Programm beendet um {now.strftime('%d.%m.%Y um %H:%M:%S')}"
        await send_telegram_message(session, state.chat_id, message, state.bot_token)  # Verwende state.chat_id und state.bot_token

        # LCD nur schließen, wenn es existiert
        if lcd is not None:
            lcd.clear()
            lcd.write_string("System aus")
            lcd.close()
            logging.info("LCD heruntergefahren")

        # GPIO nur bereinigen, wenn es initialisiert ist
        if GPIO.getmode() is not None:
            GPIO.cleanup()
            logging.info("GPIO-Ressourcen bereinigt")
        else:
            logging.warning("GPIO bereits bereinigt, überspringe cleanup")

        reset_sensor_cache()

    except Exception as e:
        logging.error(f"Fehler beim Herunterfahren: {e}", exc_info=True)
    finally:
        logging.info("System heruntergefahren")



# Synchron bleibende Funktionen
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


def check_pressure(state):
    """Prüft den Druckschalter (GPIO 17) mit Pull-up und NO-Schalter."""
    raw_value = GPIO.input(PRESSURE_SENSOR_PIN)
    pressure_ok = raw_value == GPIO.LOW  # LOW = Druck OK, HIGH = Fehler

    # Logging nur bei erstem Aufruf oder Änderung des Status
    if state.last_pressure_state is None or state.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {raw_value} -> {'OK' if pressure_ok else 'Fehler'} (LOW=OK, HIGH=Fehler)")
        state.last_pressure_state = pressure_ok  # Aktualisiere den letzten Status

    return pressure_ok

async def handle_pressure_check(session, state):
    pressure_ok = await asyncio.to_thread(check_pressure, state)

    if not pressure_ok:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force_off=True)
            if result:
                state.kompressor_ein = False
                state.last_compressor_off_time = datetime.now(state.local_tz)
                logging.info("Kompressor ausgeschaltet (Druckschalter offen).")

        reset_sensor_cache()

        state.ausschluss_grund = "Druckschalter offen"
        if not state.pressure_error_sent:
            if state.bot_token and state.chat_id:
                await send_telegram_message(
                    session, state.chat_id,
                    "⚠️ Druckschalter offen!", state.bot_token
                )
                state.pressure_error_sent = True
                state.last_pressure_error_time = datetime.now(state.local_tz)
        return False

    if state.pressure_error_sent and (
        datetime.now(state.local_tz) - state.last_pressure_error_time
    ) >= PRESSURE_ERROR_DELAY:
        if state.bot_token and state.chat_id:
            await send_telegram_message(
                session, state.chat_id,
                "✅ Druckschalter wieder normal.", state.bot_token
            )
            state.pressure_error_sent = False
            state.last_pressure_error_time = None
    return True

async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    fehler, is_overtemp = await check_boiler_sensors(t_boiler_oben, t_boiler_unten, state.config)

    if fehler:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force_off=True)
            if result:
                state.kompressor_ein = False
                state.last_compressor_off_time = datetime.now(state.local_tz)
                logging.info(f"Kompressor ausgeschaltet (Sensorfehler: {fehler}).")
        state.ausschluss_grund = fehler

        if is_overtemp:
            try:
                SICHERHEITS_TEMP = int(state.config["Heizungssteuerung"]["SICHERHEITS_TEMP"])
            except (KeyError, ValueError):
                SICHERHEITS_TEMP = 51

            now = datetime.now(state.local_tz)
            if (state.last_overtemp_notification is None or
                (now - state.last_overtemp_notification).total_seconds() >= NOTIFICATION_COOLDOWN):
                if state.bot_token and state.chat_id:
                    message = (
                        f"⚠️ Sicherheitsabschaltung: "
                        f"T_Oben={'N/A' if t_boiler_oben is None else t_boiler_oben:.1f}°C, "
                        f"T_Unten={'N/A' if t_boiler_unten is None else t_boiler_unten:.1f}°C >= {SICHERHEITS_TEMP}°C"
                    )
                    await send_telegram_message(session, state.chat_id, message, state.bot_token)
                    state.last_overtemp_notification = now
        await asyncio.sleep(2)
        return False
    return True

async def check_boiler_sensors(t_boiler_oben, t_boiler_unten, config):
    try:
        SICHERHEITS_TEMP = int(config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 52))
        logging.debug(f"SICHERHEITS_TEMP erfolgreich geladen: {SICHERHEITS_TEMP}")
    except ValueError:
        SICHERHEITS_TEMP = 52
        logging.warning(f"SICHERHEITS_TEMP ungültig, verwende Standard: {SICHERHEITS_TEMP}")

    fehler = None
    is_overtemp = False
    try:
        if t_boiler_oben is None or t_boiler_unten is None:
            fehler = "Fühlerfehler!"
            logging.error(f"Fühlerfehler erkannt: oben={'N/A' if t_boiler_oben is None else t_boiler_oben}, "
                          f"unten={'N/A' if t_boiler_unten is None else t_boiler_unten}")
        elif t_boiler_oben >= SICHERHEITS_TEMP or t_boiler_unten >= SICHERHEITS_TEMP:
            fehler = "Übertemperatur!"
            is_overtemp = True
            logging.error(
                f"Übertemperatur erkannt: oben={t_boiler_oben:.1f}°C, unten={t_boiler_unten:.1f}°C, Grenze={SICHERHEITS_TEMP}°C")
        elif abs(t_boiler_oben - t_boiler_unten) > 50:
            fehler = "Fühlerdifferenz!"
            logging.warning(
                f"Fühlerdifferenz erkannt: oben={t_boiler_oben:.1f}°C, unten={t_boiler_unten:.1f}°C, "
                f"Differenz={abs(t_boiler_oben - t_boiler_unten):.1f}°C")
        logging.debug(
            f"Sensorprüfung: T_Oben={'N/A' if t_boiler_oben is None else t_boiler_oben:.1f}°C, "
            f"T_Unten={'N/A' if t_boiler_unten is None else t_boiler_unten:.1f}°C, SICHERHEITS_TEMP={SICHERHEITS_TEMP}°C")
    except Exception as e:
        fehler = "Sensorprüfungsfehler!"
        logging.error(f"Fehler bei Sensorprüfung: {e}", exc_info=True)

    return fehler, is_overtemp


async def set_kompressor_status(state, ein, force_off=False, t_boiler_oben=None):
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    SICHERHEITS_TEMP = 52  # Sicherheitsgrenze aus config.ini
    max_attempts = 3
    attempt_delay = 0.2

    async with state.gpio_lock:  # Verwende gpio_lock für Synchronisation
        logging.debug(f"set_kompressor_status: ein={ein}, force_off={force_off}, t_boiler_oben={t_boiler_oben}, "
                      f"kompressor_ein={state.kompressor_ein}, current_GPIO_state={GPIO.input(GIO21_PIN)}")

        try:
            # Prüfe GPIO-Initialisierung
            if GPIO.getmode() is None:
                logging.critical("GPIO nicht initialisiert, kann Kompressorstatus nicht setzen!")
                return False

            # Logik für Einschalten
            if ein:
                if not state.kompressor_ein:
                    pause_time = now - state.last_shutdown_time if state.last_shutdown_time else timedelta()
                    if pause_time < state.min_pause and not force_off:
                        logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {state.min_pause})")
                        state.ausschluss_grund = f"Zu kurze Pause ({pause_time.total_seconds():.1f}s)"
                        return False
                    state.kompressor_ein = True
                    state.start_time = now
                    state.last_compressor_on_time = now  # Aktualisiere Einschaltzeit
                    state.current_runtime = timedelta()
                    state.ausschluss_grund = None
                    logging.info(f"Kompressor EIN geschaltet. Startzeit: {state.start_time}")
                else:
                    logging.debug("Kompressor bereits eingeschaltet")
            # Logik für Ausschalten
            else:
                if state.kompressor_ein:
                    elapsed_time = now - state.start_time if state.start_time else timedelta()
                    should_turn_off = force_off or (t_boiler_oben is not None and t_boiler_oben >= SICHERHEITS_TEMP)
                    if elapsed_time < state.min_laufzeit and not should_turn_off:
                        logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}, benötigt: {state.min_laufzeit})")
                        return True
                    state.kompressor_ein = False
                    state.current_runtime = elapsed_time
                    state.total_runtime_today += state.current_runtime
                    state.last_runtime = state.current_runtime
                    state.last_shutdown_time = now
                    state.start_time = None
                    logging.info(f"Kompressor AUS geschaltet. Laufzeit: {elapsed_time}")
                else:
                    logging.debug("Kompressor bereits ausgeschaltet")

            # GPIO-Steuerung mit Wiederholungslogik
            target_state = GPIO.HIGH if ein else GPIO.LOW
            for attempt in range(max_attempts):
                try:
                    logging.debug(f"Setze GPIO 21 auf {'HIGH' if ein else 'LOW'}, Versuch {attempt + 1}/{max_attempts}")
                    GPIO.output(GIO21_PIN, target_state)
                    await asyncio.sleep(attempt_delay)  # Asynchrone Pause für Stabilisierung
                    actual_state = GPIO.input(GIO21_PIN)
                    if actual_state == target_state:
                        logging.info(f"GPIO 21 erfolgreich auf {'HIGH' if ein else 'LOW'} gesetzt, tatsächlicher Zustand: {actual_state}")
                        return True
                    else:
                        logging.warning(f"GPIO-Fehler: GPIO 21 sollte {'HIGH' if ein else 'LOW'} sein, ist aber {actual_state}, Versuch {attempt + 1}/{max_attempts}")
                except Exception as e:
                    logging.error(f"Fehler beim Setzen von GPIO 21, Versuch {attempt + 1}/{max_attempts}: {e}", exc_info=True)

            # Wenn alle Versuche fehlschlagen
            logging.critical(f"Kritischer Fehler: GPIO 21 konnte nicht auf {'HIGH' if ein else 'LOW'} gesetzt werden nach {max_attempts} Versuchen!")
            if not ein:  # Bei fehlgeschlagener Abschaltung
                state.kompressor_ein = True  # Setze Zustand zurück, um Konsistenz zu wahren
                logging.error("Kompressor bleibt eingeschaltet wegen GPIO-Fehler!")
                if state.bot_token and state.chat_id:
                    await state.session.post(
                        f"https://api.telegram.org/bot{state.bot_token}/sendMessage",
                        json={"chat_id": state.chat_id, "text": "🚨 KRITISCHER FEHLER: Kompressor konnte nicht ausgeschaltet werden!"}
                    )
            return False

        except Exception as e:
            logging.error(f"Kritischer Fehler in set_kompressor_status: {e}", exc_info=True)
            if not ein and state.kompressor_ein:  # Bei fehlgeschlagener Abschaltung
                state.kompressor_ein = True  # Zustand zurücksetzen
                logging.error("Kompressor bleibt eingeschaltet wegen Ausnahme!")
                if state.bot_token and state.chat_id:
                    await state.session.post(
                        f"https://api.telegram.org/bot{state.bot_token}/sendMessage",
                        json={"chat_id": state.chat_id, "text": f"🚨 KRITISCHER FEHLER: Kompressor konnte nicht ausgeschaltet werden: {e}"}
                    )
            return False



# Funktion zum Anpassen der Sollwerte (synchron, wird in Thread ausgeführt)
def adjust_shutdown_and_start_points(solax_data, config, state):
    """
    Passt die Sollwerte basierend auf dem aktuellen Modus und den Solax-Daten an.
    """
    if not hasattr(adjust_shutdown_and_start_points, "last_night"):
        adjust_shutdown_and_start_points.last_night = None
        adjust_shutdown_and_start_points.last_config_hash = None
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = None
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = None

    is_night = is_nighttime(config)
    current_config_hash = calculate_file_hash("config.ini")

    # Debugging: Aufrufbedingungen
    logging.debug(f"adjust_shutdown_and_start_points: is_night={is_night}, current_config_hash={current_config_hash}, "
                  f"last_night={adjust_shutdown_and_start_points.last_night}, last_config_hash={adjust_shutdown_and_start_points.last_config_hash}")

    if (is_night == adjust_shutdown_and_start_points.last_night and
            current_config_hash == adjust_shutdown_and_start_points.last_config_hash):
        logging.debug("Keine Änderung in Nachtzeit oder Konfiguration, überspringe Berechnung")
        return

    adjust_shutdown_and_start_points.last_night = is_night
    adjust_shutdown_and_start_points.last_config_hash = current_config_hash

    old_ausschaltpunkt = state.aktueller_ausschaltpunkt
    old_einschaltpunkt = state.aktueller_einschaltpunkt

    state.aktueller_ausschaltpunkt, state.aktueller_einschaltpunkt = calculate_shutdown_point(
        config, is_night, solax_data, state
    )

    MIN_EINSCHALTPUNKT = 20
    if state.aktueller_einschaltpunkt < MIN_EINSCHALTPUNKT:
        state.aktueller_einschaltpunkt = MIN_EINSCHALTPUNKT
        logging.warning(f"Einschaltpunkt auf Mindestwert {MIN_EINSCHALTPUNKT} gesetzt.")

    if (state.aktueller_ausschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt or
            state.aktueller_einschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt):
        logging.info(
            f"Sollwerte angepasst: Ausschaltpunkt={old_ausschaltpunkt} -> {state.aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {state.aktueller_einschaltpunkt}, "
            f"Solarüberschuss_aktiv={state.solar_ueberschuss_aktiv}"
        )
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = state.aktueller_ausschaltpunkt
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = state.aktueller_einschaltpunkt


def load_and_validate_config():
    defaults = {
        "Heizungssteuerung": {
            "AUSSCHALTPUNKT": "45",
            "EINSCHALTPUNKT": "42",
            "AUSSCHALTPUNKT_ERHOEHT": "50",
            "EINSCHALTPUNKT_ERHOEHT": "46",
            "NACHTABSENKUNG": "0",
            "VERDAMPFERTEMPERATUR": "5",
            "MIN_PAUSE": "5",
            "SICHERHEITS_TEMP": "52",
            "HYSTERESE_MIN": "2"
        },
        "Urlaubsmodus": {
            "URLAUBSABSENKUNG": "0"
        },
        "Telegram": {
            "CHAT_ID": "",
            "BOT_TOKEN": ""
        },
        "SolaxCloud": {
            "TOKEN_ID": "",
            "SN": ""
        }
    }

    config = configparser.ConfigParser()
    read_ok = config.read("config.ini")

    if not read_ok:
        logging.warning("Konfigurationsdatei konnte nicht gefunden oder gelesen werden. Verwende Standardwerte.")

    # Füge fehlende Sections/Werte hinzu
    for section, keys in defaults.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, default in keys.items():
            if not config.has_option(section, key):
                config.set(section, key, default)
                logging.debug(f"[{section}] {key} fehlt → Standardwert gesetzt: {default}")

    return config

# Asynchrone Funktion zum Neuladen der Konfiguration
async def reload_config(session, state):
    try:
        new_config = load_and_validate_config()
        current_hash = calculate_file_hash("config.ini")

        if hasattr(state, "last_config_hash") and state.last_config_hash == current_hash:
            logging.debug("Keine Änderung an der Konfigurationsdatei festgestellt.")
            return

        logging.info("Neue Konfiguration erkannt – wird geladen...")

        # Heizungsparameter
        state.aktueller_ausschaltpunkt = new_config.getint("Heizungssteuerung", "AUSSCHALTPUNKT")
        state.aktueller_einschaltpunkt = new_config.getint("Heizungssteuerung", "EINSCHALTPUNKT")
        state.min_pause = timedelta(minutes=new_config.getint("Heizungssteuerung", "MIN_PAUSE"))

        # Telegram
        old_token = state.bot_token
        old_chat_id = state.chat_id
        state.bot_token = new_config.get("Telegram", "BOT_TOKEN")
        state.chat_id = new_config.get("Telegram", "CHAT_ID")

        if not state.bot_token or not state.chat_id:
            logging.warning("Telegram-Token oder Chat-ID fehlt. Nachrichten deaktiviert.")

        # Benachrichtigung bei Änderung
        if state.bot_token and state.chat_id and (old_token != state.bot_token or old_chat_id != state.chat_id):
            await send_telegram_message(session, state.chat_id, "🔧 Konfiguration neu geladen.", state.bot_token)

        state.last_config_hash = current_hash
        logging.info("Konfiguration erfolgreich neu geladen.")

    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}", exc_info=True)
        if state.bot_token and state.chat_id:
            await send_telegram_message(session, state.chat_id,
                                      f"⚠️ Fehler beim Neuladen der Konfiguration: {str(e)}",
                                      state.bot_token)

def calculate_file_hash(file_path):
    """Berechnet den SHA-256-Hash einer Datei."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        hash_value = sha256_hash.hexdigest()
        logging.debug(f"Hash für {file_path} berechnet: {hash_value}")
        return hash_value
    except Exception as e:
        logging.error(f"Fehler beim Berechnen des Hash-Werts für {file_path}: {e}")
        return None



async def log_to_csv(state, now, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, solax_data,
                     aktueller_einschaltpunkt, aktueller_ausschaltpunkt, solar_ueberschuss_aktiv,
                     nacht_reduction, power_source):
    should_log = (state.last_log_time is None or
                  (now - state.last_log_time) >= timedelta(minutes=1) or
                  state.kompressor_ein != state.last_kompressor_status)

    if should_log:
        async with csv_lock:
            async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                csv_line = (
                    f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                    f"{t_boiler_oben if t_boiler_oben is not None else 'N/A'},"
                    f"{t_boiler_unten if t_boiler_unten is not None else 'N/A'},"
                    f"{t_boiler_mittig if t_boiler_mittig is not None else 'N/A'},"
                    f"{state.t_boiler if state.t_boiler != 'Fehler' else 'N/A'},"
                    f"{t_verd if t_verd is not None else 'N/A'},"
                    f"{'EIN' if state.kompressor_ein else 'AUS'},"
                    f"{solax_data.get('acpower', 'N/A')},{solax_data.get('feedinpower', 'N/A')},"
                    f"{solax_data.get('batPower', 'N/A')},{solax_data.get('soc', 'N/A')},"
                    f"{solax_data.get('powerdc1', 'N/A')},{solax_data.get('powerdc2', 'N/A')},"
                    f"{solax_data.get('consumeenergy', 'N/A')},"
                    f"{state.aktueller_einschaltpunkt},{state.aktueller_ausschaltpunkt},"
                    f"{int(state.solar_ueberschuss_aktiv)},"
                    f"{nacht_reduction},"
                    f"{power_source}\n"
                )
                await csvfile.write(csv_line)
                await csvfile.flush()
            state.last_log_time = now
            state.last_kompressor_status = state.kompressor_ein

def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    logging.debug(f"is_nighttime: now={now}, tzinfo={now.tzinfo}")
    try:
        start_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_START", "22:00")
        end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")
        start_hour, start_minute = map(int, start_time_str.split(':'))
        end_hour, end_minute = map(int, end_time_str.split(':'))

        # Aktuelle Zeit in Stunden und Minuten umrechnen
        now_time = now.hour * 60 + now.minute
        start_time_minutes = start_hour * 60 + start_minute
        end_time_minutes = end_hour * 60 + end_minute

        if start_time_minutes > end_time_minutes:  # Über Mitternacht
            is_night = now_time >= start_time_minutes or now_time <= end_time_minutes
        else:
            is_night = start_time_minutes <= now_time <= end_time_minutes

        logging.debug(
            f"Nachtzeitprüfung: Jetzt={now_time}, Start={start_time_minutes}, Ende={end_time_minutes}, Ist Nacht={is_night}")
        return is_night
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False


def calculate_shutdown_point(config, is_night, solax_data, state):
    """Berechnet die Sollwerte basierend auf Modus und Absenkungen."""
    try:
        nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
        urlaubs_reduction = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 0)) if state.urlaubsmodus_aktiv else 0
        total_reduction = nacht_reduction + urlaubs_reduction

        bat_power = solax_data.get("batPower", 0)
        feedin_power = solax_data.get("feedinpower", 0)
        soc = solax_data.get("soc", 0)

        # Solarüberschuss-Logik mit korrekter Hysterese
        was_active = state.solar_ueberschuss_aktiv
        MIN_SOLAR_POWER_ACTIVE = 550
        MIN_SOLAR_POWER_INACTIVE = 600

        if solax_data.get("api_fehler", False):
            logging.warning("API-Fehler: Solardaten nicht verfügbar – Solarüberschuss deaktiviert")
            state.solar_ueberschuss_aktiv = False
        elif state.solar_ueberschuss_aktiv:
            # Ausschaltlogik: Bleibe aktiv, solange genug Überschuss da
            state.solar_ueberschuss_aktiv = bat_power > MIN_SOLAR_POWER_ACTIVE or (soc > 90 and feedin_power > MIN_SOLAR_POWER_ACTIVE)
        else:
            # Einschaltlogik: Nur bei starkem Überschuss starten
            state.solar_ueberschuss_aktiv = bat_power > MIN_SOLAR_POWER_ACTIVE or (soc > 95 and feedin_power > MIN_SOLAR_POWER_ACTIVE)

        # Sollwerte berechnen
        if state.solar_ueberschuss_aktiv:
            ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 50)) - total_reduction
            einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 46)) - total_reduction
        else:
            ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45)) - total_reduction
            einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42)) - total_reduction

        # Minimaltemperatur schützen
        MIN_TEMPERATUR = 20
        ausschaltpunkt = max(MIN_TEMPERATUR, ausschaltpunkt)
        einschaltpunkt = max(MIN_TEMPERATUR, einschaltpunkt)

        # Validierung: Ausschaltpunkt muss immer über Einschaltpunkt liegen
        HYSTERESE_MIN = int(config["Heizungssteuerung"].get("HYSTERESE_MIN", 2))
        if ausschaltpunkt <= einschaltpunkt:
            logging.warning(
                f"Ausschaltpunkt ({ausschaltpunkt}°C) ≤ Einschaltpunkt ({einschaltpunkt}°C), "
                f"setze Ausschaltpunkt auf Einschaltpunkt + {HYSTERESE_MIN}°C"
            )
            ausschaltpunkt = einschaltpunkt + HYSTERESE_MIN

        # Debugging-Ausgabe
        logging.debug(
            f"Sollwerte berechnet: Ausschaltpunkt={ausschaltpunkt}, Einschaltpunkt={einschaltpunkt}, "
            f"Nacht={is_night}, Urlaub={state.urlaubsmodus_aktiv}, Solar={state.solar_ueberschuss_aktiv}, "
            f"Reduction=Nacht({nacht_reduction})+Urlaub({urlaubs_reduction}), "
            f"batPower={bat_power}, soc={soc}, feedin={feedin_power}"
        )

        return ausschaltpunkt, einschaltpunkt, state.solar_ueberschuss_aktiv

    except (KeyError, ValueError) as e:
        logging.error(f"Fehler in calculate_shutdown_point: {e}", exc_info=True)
        ausschaltpunkt = 45
        einschaltpunkt = 42
        state.solar_ueberschuss_aktiv = False
        logging.warning(f"Verwende Standard-Sollwerte: Ausschaltpunkt={ausschaltpunkt}, Einschaltpunkt={einschaltpunkt}, Solarüberschuss_aktiv={state.solar_ueberschuss_aktiv}")
        return ausschaltpunkt, einschaltpunkt, state.solar_ueberschuss_aktiv


def check_value(value, min_value, max_value, default_value, parameter_name, other_value=None, comparison=None,
                min_difference=None):
    """Überprüft und korrigiert einen Konfigurationswert."""
    if not (min_value <= value <= max_value):
        logging.warning(f"Ungültiger Wert für {parameter_name}: {value}. Verwende Standardwert: {default_value}.")
        value = default_value
    if other_value is not None and comparison == "<" and not (value < other_value):
        logging.warning(
            f"{parameter_name} ({value}) ungültig im Vergleich zu {other_value}, verwende Standardwert: {default_value}")
        value = default_value
    return value


def is_data_old(timestamp):
    """Prüft, ob Solax-Daten veraltet sind."""
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    is_old = timestamp and (now - timestamp) > timedelta(minutes=15)
    logging.debug(f"Prüfe Solax-Datenalter: now={now}, tzinfo={now.tzinfo}, Zeitstempel={timestamp}, tzinfo={timestamp.tzinfo if timestamp else None}, Ist alt={is_old}")
    return is_old


# Asynchrone Task für Display-Updates
async def display_task(state):
    """
    Separate Task für Display-Updates, entkoppelt von der Hauptschleife.
    Nutzt das State-Objekt für den Zugriff auf globale Zustände.
    """
    global lcd  # LCD bleibt global, da es hardwarebezogen ist
    async with aiohttp.ClientSession() as session:
        while True:
            if lcd is None:
                logging.debug("LCD nicht verfügbar, überspringe Display-Update")
                await asyncio.sleep(5)
                continue

            try:
                # Seite 1: Temperaturen
                t_boiler_oben = await read_temperature_cached(SENSOR_IDS["oben"])
                t_boiler_unten = await read_temperature_cached(SENSOR_IDS["unten"])
                t_verd = await read_temperature_cached(SENSOR_IDS["verd"])
                t_boiler = (
                    (t_boiler_oben + t_boiler_unten) / 2
                    if t_boiler_oben is not None and t_boiler_unten is not None
                    else "Fehler"
                )
                pressure_ok = await asyncio.to_thread(check_pressure, state)

                lcd.clear()
                if not pressure_ok:
                    lcd.write_string("FEHLER: Druck zu niedrig")
                    logging.error(f"Display zeigt Druckfehler: Druckschalter={pressure_ok}")
                else:
                    # Temperaturwerte formatieren
                    oben_str = f"{t_boiler_oben:.2f}" if isinstance(t_boiler_oben, (int, float)) else "Fehler"
                    unten_str = f"{t_boiler_unten:.2f}" if isinstance(t_boiler_unten, (int, float)) else "Fehler"
                    boiler_str = f"{t_boiler:.2f}" if isinstance(t_boiler, (int, float)) else "Fehler"
                    verd_str = f"{t_verd:.2f}" if isinstance(t_verd, (int, float)) else "Fehler"

                    lcd.write_string(f"T-Oben: {oben_str} C")
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"T-Unten: {unten_str} C")
                    lcd.cursor_pos = (2, 0)
                    lcd.write_string(f"T-Boiler: {boiler_str} C")
                    lcd.cursor_pos = (3, 0)
                    lcd.write_string(f"T-Verd: {verd_str} C")
                    logging.debug(
                        f"Display-Seite 1 aktualisiert: oben={oben_str}, unten={unten_str}, boiler={boiler_str}, verd={verd_str}"
                    )
                await asyncio.sleep(5)

                # Seite 2: Kompressorstatus
                lcd.clear()
                lcd.write_string(f"Kompressor: {'EIN' if state.kompressor_ein else 'AUS'}")
                lcd.cursor_pos = (1, 0)
                boiler_str = f"{t_boiler:.1f}" if isinstance(t_boiler, (int, float)) else "Fehler"
                lcd.write_string(f"Soll:{state.aktueller_ausschaltpunkt:.1f}C Ist:{boiler_str}C")
                lcd.cursor_pos = (2, 0)
                lcd.write_string(
                    f"Aktuell: {str(state.current_runtime).split('.')[0]}"
                    if state.kompressor_ein
                    else f"Letzte: {str(state.last_runtime).split('.')[0]}"
                )
                lcd.cursor_pos = (3, 0)
                lcd.write_string(f"Gesamt: {str(state.total_runtime_today).split('.')[0]}")
                logging.debug(
                    f"Display-Seite 2 aktualisiert: Status={'EIN' if state.kompressor_ein else 'AUS'}, Laufzeit={state.current_runtime if state.kompressor_ein else state.last_runtime}"
                )
                await asyncio.sleep(5)

                # Seite 3: Solax-Daten
                lcd.clear()
                if state.last_api_data:
                    solar = state.last_api_data.get("powerdc1", 0) + state.last_api_data.get("powerdc2", 0)
                    feedinpower = state.last_api_data.get("feedinpower", "N/A")
                    consumeenergy = state.last_api_data.get("consumeenergy", "N/A")
                    batPower = state.last_api_data.get("batPower", "N/A")
                    soc = state.last_api_data.get("soc", "N/A")
                    old_suffix = " ALT" if is_data_old(state.last_api_timestamp) else ""
                    lcd.write_string(f"Solar: {solar} W{old_suffix}")
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"Netz: {feedinpower if feedinpower != 'N/A' else 'N/A'}{old_suffix}")
                    lcd.cursor_pos = (2, 0)
                    lcd.write_string(f"Verbrauch: {consumeenergy if consumeenergy != 'N/A' else 'N/A'}{old_suffix}")
                    lcd.cursor_pos = (3, 0)
                    lcd.write_string(f"Bat:{batPower}W,SOC:{soc}%")
                    logging.debug(
                        f"Display-Seite 3 aktualisiert: Solar={solar}, Netz={feedinpower}, Verbrauch={consumeenergy}, Batterie={batPower}, SOC={soc}"
                    )
                else:
                    lcd.write_string("Fehler bei Solax-Daten")
                    logging.warning("Keine Solax-Daten für Display verfügbar")
                await asyncio.sleep(5)

            except Exception as e:
                error_msg = f"Fehler beim Display-Update: {e}"
                logging.error(error_msg)
                await send_telegram_message(session, state.chat_id, error_msg, state.bot_token)
                lcd = None  # Setze lcd auf None bei Fehler während der Nutzung
                await asyncio.sleep(5)


async def initialize_gpio():
    """
    Initialisiert GPIO-Pins mit Wiederholungslogik für Robustheit.

    Versucht bis zu 3 Mal, die GPIO-Pins zu initialisieren, mit einer Pause von 1 Sekunde zwischen den Versuchen.

    Returns:
        bool: True bei erfolgreicher Initialisierung, False bei wiederholtem Fehlschlag.
    """
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(GIO21_PIN, GPIO.OUT)
            GPIO.output(GIO21_PIN, GPIO.LOW)
            GPIO.setup(PRESSURE_SENSOR_PIN, GPIO.IN)  # Externer Pull-up, kein interner Widerstand
            logging.info("GPIO erfolgreich initialisiert: Kompressor=GPIO21, Druckschalter=GPIO17 (Pull-up)")
            return True
        except Exception as e:
            logging.error(f"GPIO-Initialisierung fehlgeschlagen (Versuch {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(1)
    logging.critical("GPIO-Initialisierung nach mehreren Versuchen fehlgeschlagen.")
    return False


# Asynchrone Hauptschleife
async def main_loop(config, state, session):
    """Hauptschleife des Programms mit State-Objekt."""
    local_tz = pytz.timezone("Europe/Berlin")
    NOTIFICATION_COOLDOWN = 600
    PRESSURE_ERROR_DELAY = timedelta(minutes=5)
    WATCHDOG_MAX_WARNINGS = 3
    csv_lock = asyncio.Lock()

    try:
        # GPIO-Initialisierung
        if not await initialize_gpio():
            logging.critical("GPIO-Initialisierung fehlgeschlagen!")
            raise RuntimeError("GPIO-Initialisierung fehlgeschlagen")

        # LCD-Initialisierung
        await initialize_lcd(session)

        # Startnachrichten
        now = datetime.now(local_tz)
        await send_telegram_message(
            session, state.chat_id,
            f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}",
            state.bot_token
        )
        await send_welcome_message(session, state.chat_id, state.bot_token)

        # Telegram-Task starten
        logging.info("Initialisiere telegram_task")
        telegram_task_handle = asyncio.create_task(telegram_task(
            session=session,
            read_temperature_func=read_temperature,
            sensor_ids=SENSOR_IDS,
            kompressor_status_func=lambda: state.kompressor_ein,
            current_runtime_func=lambda: state.current_runtime,
            total_runtime_func=lambda: state.total_runtime_today,
            config=config,
            get_solax_data_func=get_solax_data,
            state=state,
            get_temperature_history_func=get_boiler_temperature_history,
            get_runtime_bar_chart_func=get_runtime_bar_chart,
            is_nighttime_func=is_nighttime
        ))

        # Initialisiere Zeitstempel und Zustandsvariablen
        state.last_log_time = None if not hasattr(state, 'last_log_time') else state.last_log_time
        if state.last_log_time and state.last_log_time.tzinfo is None:
            state.last_log_time = local_tz.localize(state.last_log_time)
        state.last_day = now.date() if not hasattr(state, 'last_day') else state.last_day
        state.last_compressor_on_time = None if not hasattr(state, 'last_compressor_on_time') else state.last_compressor_on_time
        if state.last_compressor_on_time and state.last_compressor_on_time.tzinfo is None:
            state.last_compressor_on_time = local_tz.localize(state.last_compressor_on_time)
        state.last_pressure_error_time = None if not hasattr(state, 'last_pressure_error_time') else state.last_pressure_error_time
        if state.last_pressure_error_time and state.last_pressure_error_time.tzinfo is None:
            state.last_pressure_error_time = local_tz.localize(state.last_pressure_error_time)
        state.last_overtemp_notification = None if not hasattr(state, 'last_overtemp_notification') else state.last_overtemp_notification
        if state.last_overtemp_notification and state.last_overtemp_notification.tzinfo is None:
            state.last_overtemp_notification = local_tz.localize(state.last_overtemp_notification)
        state.previous_ausschaltpunkt = None if not hasattr(state, 'previous_ausschaltpunkt') else state.previous_ausschaltpunkt
        state.previous_einschaltpunkt = None if not hasattr(state, 'previous_einschaltpunkt') else state.previous_einschaltpunkt
        state.previous_solar_ueberschuss_aktiv = state.solar_ueberschuss_aktiv if hasattr(state, 'solar_ueberschuss_aktiv') else False

        # Variables for the solar-only start window after night setback
        night_setback_end_time_today = None
        solar_only_window_end_time_today = None

        # Watchdog-Variablen
        last_cycle_time = datetime.now(local_tz)
        watchdog_warning_count = 0

        while True:
            try:
                now = datetime.now(local_tz)
                logging.debug(f"Schleifeniteration: {now}")

                # --- Calculate the solar-only window for the current day ---
                try:
                    end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")
                    end_hour, end_minute = map(int, end_time_str.split(':'))
                    potential_night_setback_end_today = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
                    if now < potential_night_setback_end_today + timedelta(hours=2):
                        night_setback_end_time_today = potential_night_setback_end_today
                    else:
                        night_setback_end_time_today = potential_night_setback_end_today + timedelta(days=1)
                    solar_only_window_start_time_today = night_setback_end_time_today
                    solar_only_window_end_time_today = night_setback_end_time_today + timedelta(hours=2)
                    within_solar_only_window = solar_only_window_start_time_today <= now < solar_only_window_end_time_today
                    logging.debug(f"Solar-only window: Start={solar_only_window_start_time_today.strftime('%Y-%m-%d %H:%M')}, End={solar_only_window_end_time_today.strftime('%Y-%m-%d %H:%M')}, Now={now.strftime('%Y-%m-%d %H:%M')}, Within window={within_solar_only_window}")
                except Exception as e:
                    logging.error(f"Fehler bei der Berechnung des Solar-Fensters: {e}", exc_info=True)
                    within_solar_only_window = False

                # Tageswechsel prüfen
                should_check_day = (state.last_log_time is None or
                                    (now - state.last_log_time) >= timedelta(minutes=1))
                if should_check_day and now.date() != state.last_day:
                    logging.info(f"Neuer Tag erkannt: {now.date()}. Setze Gesamtlaufzeit zurück.")
                    state.total_runtime_today = timedelta()
                    state.last_day = now.date()

                # Konfiguration neu laden
                current_hash = calculate_file_hash("config.ini")
                if state.last_config_hash != current_hash:
                    await reload_config(session, state)
                    state.last_config_hash = current_hash

                # Solax-Daten abrufen
                solax_result = await fetch_solax_data(session, state, now)
                solax_data = solax_result["solax_data"]
                acpower = solax_result["acpower"]
                feedinpower = solax_result["feedinpower"]
                batPower = solax_result["batPower"]
                soc = solax_result["soc"]
                powerdc1 = solax_result["powerdc1"]
                powerdc2 = solax_result["powerdc2"]
                consumeenergy = solax_result["consumeenergy"]

                # Sollwerte berechnen
                try:
                    is_night = await asyncio.to_thread(is_nighttime, config)
                    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
                    ausschaltpunkt, einschaltpunkt, solar_ueberschuss_aktiv = await asyncio.to_thread(
                        calculate_shutdown_point, config, is_night, solax_data, state)
                    state.aktueller_ausschaltpunkt = ausschaltpunkt
                    state.aktueller_einschaltpunkt = einschaltpunkt
                    state.solar_ueberschuss_aktiv = solar_ueberschuss_aktiv
                except Exception as e:
                    logging.error(f"Fehler in calculate_shutdown_point: {e}", exc_info=True)
                    is_night = False
                    nacht_reduction = 0
                    state.aktueller_ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 55))
                    state.aktueller_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 50))

                # Moduswechsel speichern
                if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv:
                    state.previous_ausschaltpunkt = state.aktueller_ausschaltpunkt
                    state.previous_einschaltpunkt = state.aktueller_einschaltpunkt
                state.previous_solar_ueberschuss_aktiv = state.solar_ueberschuss_aktiv

                # Sensorwerte lesen
                t_boiler_oben = await read_temperature_cached(SENSOR_IDS["oben"])
                t_boiler_unten = await read_temperature_cached(SENSOR_IDS["unten"])
                t_boiler_mittig = await read_temperature_cached(SENSOR_IDS["mittig"])
                t_verd = await read_temperature_cached(SENSOR_IDS["verd"])
                t_boiler = (
                    (t_boiler_oben + t_boiler_unten) / 2 if t_boiler_oben is not None and t_boiler_unten is not None else "Fehler"
                )
                state.t_boiler = t_boiler

                # Debugging-Logs für Sensorwerte und Sollwerte
                logging.debug(f"Sensorwerte: T_Oben={t_boiler_oben if t_boiler_oben is not None else 'N/A'}°C, "
                              f"T_Mittig={t_boiler_mittig if t_boiler_mittig is not None else 'N/A'}°C, "
                              f"T_Unten={t_boiler_unten if t_boiler_unten is not None else 'N/A'}°C, "
                              f"T_Verd={t_verd if t_verd is not None else 'N/A'}°C")
                logging.debug(f"Sollwerte: Einschaltpunkt={state.aktueller_einschaltpunkt}°C, "
                              f"Ausschaltpunkt={state.aktueller_ausschaltpunkt}°C, "
                              f"Solarüberschuss_aktiv={state.solar_ueberschuss_aktiv}")

                # Druck- und Sensorfehler prüfen
                if not await handle_pressure_check(session, state):
                    logging.info("Kompressor bleibt aus wegen Druckschalterfehler")
                    await asyncio.sleep(2)
                    continue

                sensor_ok = await check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten)
                if not sensor_ok:
                    logging.info("Kompressor bleibt aus wegen Sensorfehler")
                    continue

                # Kompressorsteuerung
                power_source = get_power_source(solax_data) if solax_data else "Unbekannt"
                logging.debug(f"Power Source: {power_source}, Feedinpower={feedinpower}, BatPower={batPower}, SOC={soc}")

                # Prüfe GPIO-Zustand gegen Softwarestatus
                actual_gpio_state = GPIO.input(GIO21_PIN)
                if state.kompressor_ein and actual_gpio_state == GPIO.LOW:
                    logging.critical("Inkonsistenz: state.kompressor_ein=True, aber GPIO 21 ist LOW!")
                    state.kompressor_ein = False
                    state.last_shutdown_time = now
                    state.start_time = None
                    await send_telegram_message(
                        session, state.chat_id,
                        "🚨 Inkonsistenz: Kompressorstatus korrigiert (war eingeschaltet, GPIO war LOW)!",
                        state.bot_token
                    )
                elif not state.kompressor_ein and actual_gpio_state == GPIO.HIGH:
                    logging.critical("Inkonsistenz: state.kompressor_ein=False, aber GPIO 21 ist HIGH!")
                    result = await set_kompressor_status(state, False, force_off=True)
                    if not result:
                        logging.critical("Kritischer Fehler: Konnte Kompressor nicht ausschalten!")
                        await send_telegram_message(
                            session, state.chat_id,
                            "🚨 KRITISCHER FEHLER: Kompressor bleibt eingeschaltet trotz Inkonsistenz!",
                            state.bot_token
                        )

                if t_boiler_oben is not None and t_boiler_unten is not None and t_boiler_mittig is not None:
                    try:
                        SICHERHEITS_TEMP = int(config["Heizungssteuerung"]["SICHERHEITS_TEMP"])
                    except (KeyError, ValueError):
                        SICHERHEITS_TEMP = 51
                        logging.warning(f"SICHERHEITS_TEMP ungültig, verwende Standard: {SICHERHEITS_TEMP}")

                    # Sicherheitsabschaltung
                    if (t_boiler_oben >= SICHERHEITS_TEMP or t_boiler_unten >= SICHERHEITS_TEMP):
                        if state.kompressor_ein:
                            result = await set_kompressor_status(state, False, force_off=True)
                            if result:
                                state.kompressor_ein = False
                                state.last_compressor_off_time = now
                                if state.last_compressor_on_time:
                                    state.last_runtime = now - state.last_compressor_on_time
                                    state.total_runtime_today += state.last_runtime
                                    logging.info("Kompressor erfolgreich ausgeschaltet (Sicherheitsabschaltung).")
                                else:
                                    state.last_runtime = timedelta(0)
                                    logging.info("Kompressor ausgeschaltet (Sicherheitsabschaltung). Laufzeit unbekannt.")
                                state.ausschluss_grund = None
                            else:
                                logging.critical(
                                    "Kritischer Fehler: Kompressor konnte trotz Übertemperatur nicht ausgeschaltet werden!")
                                await send_telegram_message(
                                    session, state.chat_id,
                                    "🚨 KRITISCHER FEHLER: Kompressor bleibt trotz Übertemperatur eingeschaltet!",
                                    state.bot_token
                                )
                            logging.error(
                                f"Sicherheitsabschaltung: T_Oben={t_boiler_oben:.1f}°C, T_Unten={t_boiler_unten:.1f}°C >= {SICHERHEITS_TEMP}°C"
                            )
                            await send_telegram_message(
                                session, state.chat_id,
                                f"⚠️ Sicherheitsabschaltung: T_Oben={t_boiler_oben:.1f}°C, T_Unten={t_boiler_unten:.1f}°C >= {SICHERHEITS_TEMP}°C",
                                state.bot_token
                            )
                        state.ausschluss_grund = f"Übertemperatur (>= {SICHERHEITS_TEMP}°C)"
                        await asyncio.sleep(2)
                        continue

                    # Moduswechsel prüfen
                    if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv:
                        effective_ausschaltpunkt = (
                            state.previous_ausschaltpunkt if state.previous_ausschaltpunkt is not None else state.aktueller_ausschaltpunkt
                        )
                        if not state.solar_ueberschuss_aktiv:
                            if (t_boiler_oben >= effective_ausschaltpunkt or t_boiler_mittig >= effective_ausschaltpunkt):
                                result = await set_kompressor_status(state, False, force_off=True)
                                if result:
                                    state.kompressor_ein = False
                                    state.last_compressor_off_time = now
                                    if state.last_compressor_on_time:
                                        state.last_runtime = now - state.last_compressor_on_time
                                        state.total_runtime_today += state.last_runtime
                                    else:
                                        state.last_runtime = timedelta(0)
                                    state.ausschluss_grund = None
                                    logging.info(
                                        f"Kompressor ausgeschaltet bei Moduswechsel (T_Oben={t_boiler_oben:.1f}°C >= {effective_ausschaltpunkt}°C oder "
                                        f"T_Mittig={t_boiler_mittig:.1f}°C >= {effective_ausschaltpunkt}°C). Laufzeit: {state.last_runtime}"
                                    )
                                else:
                                    logging.critical(
                                        "Kritischer Fehler: Kompressor konnte bei Moduswechsel nicht ausgeschaltet werden!")
                                    await send_telegram_message(
                                        session, state.chat_id,
                                        "🚨 KRITISCHER FEHLER: Kompressor bleibt bei Moduswechsel eingeschaltet!",
                                        state.bot_token
                                    )

                    # Kompressorsteuerung
                    if state.solar_ueberschuss_aktiv:
                        temp_conditions_met_to_start = (
                            t_boiler_oben < state.aktueller_einschaltpunkt or
                            t_boiler_mittig < state.aktueller_einschaltpunkt or
                            t_boiler_unten < state.aktueller_einschaltpunkt
                        )
                        logging.debug(f"Solarüberschussmodus: temp_conditions_met_to_start={temp_conditions_met_to_start}, "
                                      f"T_Oben={t_boiler_oben:.1f}°C, T_Mittig={t_boiler_mittig:.1f}°C, T_Unten={t_boiler_unten:.1f}°C, "
                                      f"Einschaltpunkt={state.aktueller_einschaltpunkt}°C")
                    else:
                        temp_conditions_met_to_start = (
                            t_boiler_oben < state.aktueller_einschaltpunkt or
                            t_boiler_mittig < state.aktueller_einschaltpunkt
                        )
                        logging.debug(f"Normalmodus: temp_conditions_met_to_start={temp_conditions_met_to_start}, "
                                      f"T_Oben={t_boiler_oben:.1f}°C, T_Mittig={t_boiler_mittig:.1f}°C, "
                                      f"Einschaltpunkt={state.aktueller_einschaltpunkt}°C")

                    pause_ok = True
                    if state.last_compressor_off_time:
                        time_since_off = now - state.last_compressor_off_time
                        pause_remaining = state.min_pause - time_since_off
                        if time_since_off < state.min_pause:
                            pause_ok = False
                            reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"

                            # Prüfe, ob Grund gleich wie letzte Meldung und ob genug Zeit vergangen ist
                            same_reason = getattr(state, 'last_pause_reason', None) == reason
                            if not hasattr(state, 'last_pause_log') or (
                                    now - state.last_pause_log).total_seconds() > 300 or not same_reason:
                                logging.info(f"Kompressor bleibt aus: {reason}")
                                state.last_pause_log = now
                                state.last_pause_reason = reason

                            state.ausschluss_grund = reason
                        else:
                            # Reset nach Ablauf der Pause
                            state.last_pause_log = None
                            state.last_pause_reason = None

                    solar_window_conditions_met_to_start = True
                    if within_solar_only_window:
                        if power_source != "Direkter PV-Strom":
                            solar_window_conditions_met_to_start = False
                            state.ausschluss_grund = f"Warte auf direkten Solarstrom nach Nachtabsenkung ({solar_only_window_start_time_today.strftime('%H:%M')}-{solar_only_window_end_time_today.strftime('%H:%M')})"
                            logging.debug(f"Kompressorstart verhindert: Nicht genug direkter Solarstrom im Fenster nach Nachtabsenkung.")

                    if not state.kompressor_ein and temp_conditions_met_to_start and pause_ok and solar_window_conditions_met_to_start:
                        logging.info("Alle Bedingungen für Kompressorstart erfüllt. Versuche einzuschalten.")
                        result = await set_kompressor_status(state, True)
                        if result:
                            state.kompressor_ein = True
                            state.last_compressor_on_time = now
                            state.last_compressor_off_time = None
                            state.ausschluss_grund = None
                            logging.info(f"Kompressor erfolgreich eingeschaltet. Startzeit: {now}")
                        else:
                            state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler beim Einschalten"
                            logging.warning(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")

                    elif (t_boiler_oben >= state.aktueller_ausschaltpunkt or t_boiler_mittig >= state.aktueller_ausschaltpunkt):
                        if state.kompressor_ein:
                            logging.info(f"Abschaltbedingung erreicht: T_Oben={t_boiler_oben:.1f}°C, T_Mittig={t_boiler_mittig:.1f}°C, Ausschaltpunkt={state.aktueller_ausschaltpunkt}°C. Versuche auszuschalten.")
                            result = await set_kompressor_status(state, False, force_off=True)
                            if result:
                                state.kompressor_ein = False
                                state.last_compressor_off_time = now
                                if state.last_compressor_on_time:
                                    state.last_runtime = now - state.last_compressor_on_time
                                    state.total_runtime_today += state.last_runtime
                                    logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
                                else:
                                    state.last_runtime = timedelta(0)
                                    logging.info("Kompressor ausgeschaltet. Laufzeit unbekannt (Startzeit nicht erfasst).")
                                state.ausschluss_grund = None
                            else:
                                logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
                                await send_telegram_message(
                                    session, state.chat_id,
                                    "🚨 KRITISCHER FEHLER: Kompressor bleibt eingeschaltet!",
                                    state.bot_token
                                )

                # Laufzeit aktualisieren
                if state.kompressor_ein and state.last_compressor_on_time:
                    state.current_runtime = now - state.last_compressor_on_time
                else:
                    state.current_runtime = timedelta(0)

                # CSV-Protokollierung
                await log_to_csv(state, now, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, solax_data,
                                 state.aktueller_einschaltpunkt, state.aktueller_ausschaltpunkt,
                                 state.solar_ueberschuss_aktiv, nacht_reduction, power_source)

                # Watchdog
                cycle_duration = (datetime.now(local_tz) - last_cycle_time).total_seconds()
                if cycle_duration > 30:
                    watchdog_warning_count += 1
                    logging.error(
                        f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {watchdog_warning_count}/{WATCHDOG_MAX_WARNINGS}")
                    if watchdog_warning_count >= WATCHDOG_MAX_WARNINGS:
                        result = await set_kompressor_status(state, False, force_off=True)
                        await send_telegram_message(
                            session, state.chat_id,
                            "🚨 Watchdog-Fehler: Programm beendet.", state.bot_token
                        )
                        await shutdown(session, state)
                        raise SystemExit("Watchdog-Exit")

                last_cycle_time = datetime.now(local_tz)
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Fehler in der Hauptschleife: {e}", exc_info=True)
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        if 'telegram_task_handle' in locals():
            telegram_task_handle.cancel()
        if 'display_task_handle' in locals():
            display_task_handle.cancel()
        await asyncio.gather(
            telegram_task_handle if 'telegram_task_handle' in locals() else asyncio.sleep(0),
            display_task_handle if 'display_task_handle' in locals() else asyncio.sleep(0),
            return_exceptions=True
        )
        raise

    finally:
        await shutdown(session, state)

async def run_program():
    logging.info("Programm gestartet.")

    config = None
    state = None
    session = None

    try:
        # Konfiguration laden
        logging.debug("Lade Konfigurationsdatei...")
        config = load_and_validate_config()

        # State-Objekt erstellen
        logging.debug("Erzeuge State-Objekt...")
        state = State(config)

        async with aiohttp.ClientSession() as session:
            state.session = session

            # Logging mit TelegramHandler einrichten
            logging.debug("Richte Logging mit TelegramHandler ein...")
            await setup_logging(session, state)
            logging.info("Logging erfolgreich konfiguriert")

            # LCD-Initialisierung
            logging.debug("LCD-Initialisierung beginnt...")
            await initialize_lcd(session)

            # GPIO-Initialisierung
            logging.debug("GPIO-Initialisierung beginnt...")
            if not await initialize_gpio():
                raise RuntimeError("GPIO-Initialisierung fehlgeschlagen")

            # CSV-Header schreiben
            if not os.path.exists("heizungsdaten.csv"):
                async with aiofiles.open("heizungsdaten.csv", 'w', newline='') as csvfile:
                    header = (
                        "Zeitstempel,T_Oben,T_Unten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
                        "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
                        "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Nachtabsenkung,PowerSource\n"
                    )
                    await csvfile.write(header)
                    logging.info("CSV-Header geschrieben.")

            # Willkommensnachricht senden
            now = datetime.now(pytz.timezone("Europe/Berlin"))
            if state.bot_token and state.chat_id:
                await send_telegram_message(
                    session,
                    state.chat_id,
                    f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}",
                    state.bot_token
                )

            logging.info("Initialisierung abgeschlossen. Hauptschleife startet...")
            await main_loop(config, state, session)

    except Exception as e:
        logging.critical(f"Kritischer Fehler im Hauptprogramm: {e}", exc_info=True)
        if state and state.bot_token and state.chat_id:
            try:
                await send_telegram_message(
                    session,
                    state.chat_id,
                    f"🛑 Kritischer Fehler:\n{str(e)}",
                    state.bot_token
                )
            except:
                logging.warning("Konnte Fehlermeldung per Telegram nicht senden.")
        raise

    except asyncio.CancelledError:
        logging.info("Programm durch Benutzerabbruch beendet.")
        if state and state.bot_token and state.chat_id:
            try:
                await send_telegram_message(
                    session,
                    state.chat_id,
                    f"🛑 Programm manuell beendet.",
                    state.bot_token
                )
            except:
                logging.warning("Konnte Abbruchmeldung per Telegram nicht senden.")

    finally:
        logging.info("Starte Shutdown-Prozedur...")
        try:
            await shutdown(session, state)
        except Exception as e:
            logging.error(f"Fehler während des Shutdowns: {e}")

if __name__ == "__main__":
    asyncio.run(run_program())