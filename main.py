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
from typing import Optional
from utils import safe_timedelta
from dateutil.relativedelta import relativedelta
from telegram_handler import (send_telegram_message, send_welcome_message, telegram_task, get_runtime_bar_chart,
                              get_boiler_temperature_history, deaktivere_urlaubsmodus, is_solar_window)

#git-test

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



# Globale Variablen für den Programmstatus
last_update_id = None
lcd = None
csv_lock = asyncio.Lock()
gpio_lock = asyncio.Lock()
last_sensor_readings = {}
SENSOR_READ_INTERVAL = timedelta(seconds=5)

NOTIFICATION_COOLDOWN = 600
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
        self.loop = None
        self._loop_owner = False

    async def send_message(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "Markdown"  # Optional, wenn Markdown benötigt wird
        }
        async with aiohttp.ClientSession() as session:  # Neue Sitzung pro Anfrage
            try:
                async with session.post(url, json=payload, timeout=20) as response:
                    if response.status == 200:
                        logging.info(f"Telegram-Nachricht gesendet: {message[:100]}...")
                        return True
                    else:
                        error_text = await response.text()
                        logging.error(f"Fehler beim Senden an Telegram: {response.status} - {error_text}")
                        return False
            except aiohttp.ClientConnectionError as e:
                logging.error(f"Netzwerkfehler beim Senden an Telegram: {e}")
                return False
            except asyncio.TimeoutError:
                logging.error("Timeout beim Senden an Telegram")
                return False
            except Exception as e:
                logging.error(f"Unerwarteter Fehler beim Senden an Telegram: {e}", exc_info=True)
                return False

    def emit(self, record):
        try:
            msg = self.format(record)
            # Skip if session is closed
            if self.session and self.session.closed:
                logging.debug("Session is closed, skipping Telegram message")
                return
            # Get or set the event loop
            if self.loop is None:
                try:
                    self.loop = asyncio.get_running_loop()
                except RuntimeError:
                    logging.debug("No running loop, skipping Telegram message")
                    return

            # Check if loop is closed
            if self.loop.is_closed():
                logging.debug("Event loop is closed, skipping message")
                return

            # Put message in queue
            self.queue.put_nowait(msg)

            # Schedule queue processing if not already running
            if not self.task or self.task.done():
                self.task = self.loop.create_task(self.process_queue())
        except Exception as e:
            logging.error(f"Error in TelegramHandler.emit: {e}", exc_info=True)

    async def process_queue(self):
        while not self.queue.empty():
            try:
                msg = await self.queue.get()
                await self.send_message(msg)
                self.queue.task_done()
            except Exception as e:
                logging.error(f"Error processing queue in TelegramHandler: {e}", exc_info=True)

    def close(self):
        try:
            if self.task and not self.task.done():
                self.task.cancel()
            if self._loop_owner and self.loop and not self.loop.is_closed():
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
                self.loop.close()
            self.loop = None
        except Exception as e:
            logging.error(f"Error closing TelegramHandler: {e}", exc_info=True)
        finally:
            super().close()


class State:
    def __init__(self, config):
        local_tz = pytz.timezone("Europe/Berlin")
        self.local_tz = local_tz
        now = datetime.now(local_tz)
        self.local_tz = pytz.timezone("Europe/Berlin")
        self.last_solar_window_log = None
        self.last_solar_window_status = False
        self.last_solar_window_check = None

        # --- Basiswerte ---
        self.gpio_lock = asyncio.Lock()
        self.session = None
        self.config = config

        # --- Urlaubsmodus-Zeitsteuerung ---
        self.urlaubsmodus_start = None
        self.urlaubsmodus_ende = None
        self.awaiting_urlaub_duration = False
        self.awaiting_custom_duration = False

        # --- Bademodus ---
        self.bademodus_aktiv = False
        self.previous_bademodus_aktiv = False  # Für Änderungserkennung

        # --- Laufzeitstatistik ---
        self.current_runtime = timedelta()
        self.last_runtime = timedelta()
        self.total_runtime_today = timedelta()
        self.last_day = now.date()
        self.start_time = None
        self.last_compressor_on_time = now
        self.last_compressor_off_time = now
        self.last_log_time = now - timedelta(minutes=1)
        self._last_config_check = now
        self.last_kompressor_status = None

        # --- Steuerungslogik ---
        self.kompressor_ein = False
        self.urlaubsmodus_aktiv = False
        self.solar_ueberschuss_aktiv = False
        self.ausschluss_grund = None
        self.t_boiler = None

        # --- Telegram-Konfiguration ---
        self.bot_token = config["Telegram"].get("BOT_TOKEN", "")
        self.chat_id = config["Telegram"].get("CHAT_ID", "")
        if not self.bot_token or not self.chat_id:
            logging.warning("Telegram BOT_TOKEN oder CHAT_ID fehlt. Telegram-Nachrichten deaktiviert.")
        self.last_pause_telegram_notification = None
        self.last_verdampfer_notification = None
        self.last_overtemp_notification = now

        # --- SolaxCloud-Konfiguration ---
        self.token_id = config["SolaxCloud"].get("TOKEN_ID", "")
        self.sn = config["SolaxCloud"].get("SN", "")
        if not self.token_id or not self.sn:
            logging.warning("SolaxCloud TOKEN_ID oder SN fehlt. Solax-Datenabruf eingeschränkt.")
        self.last_api_call = None
        self.last_api_data = None
        self.last_api_timestamp = None

        # --- Heizungsparameter ---
        try:
            self.sicherheits_temp = float(config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 52.0))
            self.min_laufzeit = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_LAUFZEIT", 10)))
            self.min_pause = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_PAUSE", 20)))
            self.verdampfertemperatur = float(config["Heizungssteuerung"].get("VERDAMPFERTEMPERATUR", 6.0))
        except (KeyError, ValueError, configparser.Error) as e:
            logging.error(f"Fehler beim Laden der Heizungsparameter: {e}. Verwende Standardwerte.")
            self.sicherheits_temp = 52.0
            self.min_laufzeit = timedelta(minutes=10)
            self.min_pause = timedelta(minutes=20)
            self.verdampfertemperatur = 6.0

        # --- Erhöhte Schwellwerte ---
        try:
            self.einschaltpunkt_erhoeht = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 42))
            self.ausschaltpunkt_erhoeht = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 48))
        except ValueError as e:
            logging.warning(f"Fehler beim Einlesen der erhöhten Schwellwerte: {e}. Verwende Standardwerte.")
            self.einschaltpunkt_erhoeht = 42
            self.ausschaltpunkt_erhoeht = 48

        # --- Übergangsmodus-Zeitpunkte ---
        try:
            self.uebergangsmodus_start = datetime.strptime(
                config["Heizungssteuerung"].get("UEBERGANGSMODUS_START", "00:00"), "%H:%M"
            ).time()
            self.uebergangsmodus_ende = datetime.strptime(
                config["Heizungssteuerung"].get("UEBERGANGSMODUS_ENDE", "00:00"), "%H:%M"
            ).time()
        except Exception as e:
            logging.error(f"Fehler beim Einlesen der Übergangsmodus-Zeiten: {e}")
            self.uebergangsmodus_start = time(0, 0)
            self.uebergangsmodus_ende = time(0, 0)

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
        except (KeyError, ValueError) as e:
            logging.error(f"Fehler beim Einlesen der Schwellwerte: {e}. Verwende Standardwerte.")
            self.aktueller_ausschaltpunkt = 45
            self.aktueller_einschaltpunkt = 42

        self.previous_ausschaltpunkt = self.aktueller_ausschaltpunkt
        self.previous_einschaltpunkt = self.aktueller_einschaltpunkt
        self.previous_solar_ueberschuss_aktiv = False
        self.einschaltpunkt = self.aktueller_einschaltpunkt
        self.ausschaltpunkt = self.aktueller_ausschaltpunkt

        # --- Fehler- und Statuszustände ---
        self.last_config_hash = calculate_file_hash("config.ini")
        self.pressure_error_sent = False
        self.last_pressure_error_time = now
        self.last_pressure_state = None
        self.last_pause_log = None
        self.current_pause_reason = None
        self.previous_pressure_state = None
        self.last_no_start_log = None
        self.last_completed_cycle = None


        # --- Sensorwerte ---
        self.t_oben = None
        self.t_unten = None
        self.t_mittig = None
        self.t_verd = None

        # --- Solax-Daten ---
        self.acpower = None
        self.feedinpower = None
        self.batpower = None
        self.soc = None
        self.powerdc1 = None
        self.powerdc2 = None
        self.consumeenergy = None
        self.solarueberschuss = 0
        self.power_source = "unbekannt"

        # --- Nachtabsenkung ---
        self.nachtabsenkung = False
        self.nacht_reduction = 0

        # --- Logging-Optimierung ---
        self.last_solar_window_check = now
        self.last_abschalt_log = now
        self.previous_abschalten = False
        self.previous_temp_conditions = False
        self.previous_modus = None

        # --- Debugging ---
        logging.debug(f"State initialisiert: sicherheits_temp={self.sicherheits_temp}, "
                      f"min_laufzeit={self.min_laufzeit}, min_pause={self.min_pause}, "
                      f"verdampfertemperatur={self.verdampfertemperatur}, "
                      f"letzte_abschaltung={self.last_compressor_off_time}")


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
        # Unterdrücke Matplotlib-Debug-Meldungen
        logging.getLogger('matplotlib').setLevel(logging.WARNING)
        logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
        matplotlib.set_loglevel("warning")


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
        stream_handler.setFormatter(file_formatter)
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


# Neue Hilfsfunktion für sichere Zeitdifferenzberechnung
def safe_timedelta(now, timestamp, local_tz, default=timedelta()):
    """
    Berechnet die Zeitdifferenz sicher, behandelt None-Werte und Typfehler.

    Args:
        now: Aktueller Zeitstempel (datetime)
        timestamp: Zu vergleichender Zeitstempel (datetime oder None)
        local_tz: Zeitzonen-Objekt (z. B. pytz.timezone("Europe/Berlin"))
        default: Standardwert, falls Berechnung fehlschlägt (timedelta)

    Returns:
        timedelta: Zeitdifferenz oder Standardwert
    """
    if timestamp is None:
        logging.warning(f"Zeitstempel ist None, verwende Standard: {default}")
        return default
    try:
        if timestamp.tzinfo is None:
            logging.warning(f"Zeitzone fehlt für Zeitstempel: {timestamp}. Lokalisiere auf {local_tz.zone}.")
            timestamp = local_tz.localize(timestamp)
        return now - timestamp
    except TypeError as e:
        logging.error(f"Fehler bei Zeitdifferenzberechnung: {e}, now={now}, timestamp={timestamp}")
        return default

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

async def safe_send_telegram_message(bot_token, chat_id, message):
    if not bot_token or not chat_id:
        logging.warning("Telegram-Token oder Chat-ID fehlt. Nachricht wird nicht gesendet.")
        return

    url = f"https://api.telegram.org/bot {bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as response:
                if response.status != 200:
                    logging.error(f"Telegram send failed: {await response.text()}")
        except Exception as e:
            logging.error(f"Fehler beim Senden an Telegram: {e}", exc_info=True)

# Asynchrone Funktion zum Abrufen von Solax-Daten
async def get_solax_data(session, state):
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    #logging.debug(f"get_solax_data: now={now}, tzinfo={now.tzinfo}, last_api_call={state.last_api_call}, tzinfo={state.last_api_call.tzinfo if state.last_api_call else None}")

    # Stelle sicher, dass state.last_api_call zeitzonenbewusst ist
    if state.last_api_call and state.last_api_call.tzinfo is None:
        state.last_api_call = local_tz.localize(state.last_api_call)
        #logging.debug(f"state.last_api_call lokalisiert: {state.last_api_call}")

    if state.last_api_call and (now - state.last_api_call) < timedelta(minutes=5):
        #logging.debug("Verwende zwischengespeicherte API-Daten.")
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
                    #logging.debug(f"Solax-Daten erfolgreich abgerufen: {state.last_api_data}")
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
            #logging.debug(f"Solax-Datenverzögerung: {delay:.1f} Sekunden")

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


async def shutdown(session, state):
    """Führt die Abschaltprozedur durch und informiert über Telegram."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        logging.debug(f"shutdown: now={now}, tzinfo={now.tzinfo}")

        # Nur GPIO.output aufrufen, wenn GPIO noch initialisiert ist
        if GPIO.getmode() is not None:
            GPIO.output(GIO21_PIN, GPIO.LOW)
            logging.info("Kompressor GPIO auf LOW gesetzt")
        else:
            logging.warning("GPIO-Modus nicht gesetzt, überspringe GPIO.output")

        # Telegram-Nachricht senden, bevor die Session geschlossen wird
        if state.bot_token and state.chat_id:
            message = f"🛑 Programm beendet um {now.strftime('%d.%m.%Y um %H:%M:%S')}"
            await send_telegram_message(session, state.chat_id, message, state.bot_token)

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
        # Ensure all pending Telegram tasks are completed before closing the session
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if isinstance(handler, TelegramHandler):
                await handler.process_queue()  # Process any remaining messages
                handler.close()  # Explicitly close the handler
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
                #logging.debug(f"Temperatur von Sensor {sensor_id} gelesen: {temp} °C")
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
    if state.previous_pressure_state is None or state.previous_pressure_state != pressure_ok:
        logging.info(f"Druckschalter geändert zu: {'OK' if pressure_ok else 'Fehler'}")
        state.previous_pressure_state = pressure_ok

    if not pressure_ok:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True)
            if result:
                state.kompressor_ein = False
                now_correct = datetime.now(local_tz)  # Sicherstellen, dass aktuelle Zeit verwendet wird
                set_last_compressor_off_time(state, now_correct)  # Korrekte Zuweisung
                state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time, state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
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
    try:
        SICHERHEITS_TEMP = int(state.config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 52))
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
    except Exception as e:
        fehler = "Sensorprüfungsfehler!"
        logging.error(f"Fehler bei Sensorprüfung: {e}", exc_info=True)

    if fehler:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True)
            if result:
                state.kompressor_ein = False
                now_correct = datetime.now(state.local_tz)
                set_last_compressor_off_time(state, now_correct)
                state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time, state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
                logging.info(f"Kompressor ausgeschaltet (Sensorfehler: {fehler}).")
            reset_sensor_cache()
        state.ausschluss_grund = fehler

        if is_overtemp:
            now = datetime.now(state.local_tz)
            if (state.last_overtemp_notification is None or
                safe_timedelta(now, state.last_overtemp_notification, state.local_tz).total_seconds() >= NOTIFICATION_COOLDOWN):
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
        #logging.debug(f"SICHERHEITS_TEMP erfolgreich geladen: {SICHERHEITS_TEMP}")
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
        #logging.debug(
        #    f"Sensorprüfung: T_Oben={'N/A' if t_boiler_oben is None else t_boiler_oben:.1f}°C, "
        #    f"T_Unten={'N/A' if t_boiler_unten is None else t_boiler_unten:.1f}°C, SICHERHEITS_TEMP={SICHERHEITS_TEMP}°C")
    except Exception as e:
        fehler = "Sensorprüfungsfehler!"
        logging.error(f"Fehler bei Sensorprüfung: {e}", exc_info=True)

    return fehler, is_overtemp


async def set_kompressor_status(state, ein: bool, force: bool = False, t_boiler_oben: Optional[float] = None):
    """
    Setzt den Zustand des Kompressors (GPIO 21) sicher und robust.

    Args:
        state: Das State-Objekt mit allen relevanten Zuständen und Konfigurationen.
        ein (bool): True, um den Kompressor einzuschalten, False zum Ausschalten.
        force (bool): Wenn True, werden Sicherheitsprüfungen übersprungen (z.B. bei Fehlern).
        t_boiler_oben (Optional[float]): Aktuelle obere Boilertemperatur für Sicherheitsabschaltung.

    Returns:
        bool: True bei Erfolg, False bei Fehlschlag oder wenn Aktion nicht durchgeführt wurde.
    """
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    max_attempts = 3
    attempt_delay = float(state.config["Heizungssteuerung"].get("GPIO_ATTEMPT_DELAY", 0.2))

    try:
        SICHERHEITS_TEMP = float(state.config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 52.0))

        async with state.gpio_lock:
            logging.debug(f"Aufruf set_kompressor_status: Ziel={'EIN' if ein else 'AUS'}, Force={force}, "
                          f"Aktuell kompressor_ein={state.kompressor_ein}")
            current_physical_state = GPIO.input(21)

            if force:
                logging.debug("Force=True: Sicherheitsprüfung übersprungen.")
            elif t_boiler_oben is not None and t_boiler_oben >= SICHERHEITS_TEMP:
                logging.warning(f"Übertemperatur: T_Oben={t_boiler_oben:.1f}°C >= {SICHERHEITS_TEMP}°C")
                state.ausschluss_grund = f"Übertemperatur (>= {SICHERHEITS_TEMP}°C)"
                return False

            target_gpio = GPIO.HIGH if ein else GPIO.LOW
            success = False
            for attempt in range(max_attempts):
                GPIO.output(21, target_gpio)
                await asyncio.sleep(attempt_delay)
                readback = GPIO.input(21)
                logging.debug(f"GPIO Schreibversuch {attempt + 1}: Ziel={target_gpio}, Gelesen={readback}")
                if readback == target_gpio:
                    success = True
                    break

            if not success:
                logging.critical(
                    f"Konnte GPIO 21 nicht auf {'HIGH' if ein else 'LOW'} setzen nach {max_attempts} Versuchen!")
                if state.bot_token and state.chat_id and state.session:
                    await send_telegram_message(
                        state.session, state.chat_id,
                        f"🚨 KRITISCHER FEHLER: Kompressor konnte nicht {'eingeschaltet' if ein else 'ausgeschaltet'} werden!",
                        state.bot_token
                    )
                return False

            # Status aktualisieren
            state.kompressor_ein = ein
            if ein:
                state.start_time = now
                state.last_compressor_on_time = now
                state.current_runtime = timedelta()
                logging.info(f"KOMPRESSOR EINGESCHALTET um {now.strftime('%H:%M:%S')}.")
            else:
                state.start_time = None
                set_last_compressor_off_time(state, now)
                if state.last_compressor_on_time is not None:
                    state.last_runtime = safe_timedelta(now, state.last_compressor_on_time, local_tz)
                else:
                    state.last_runtime = timedelta()
                    logging.debug("last_compressor_on_time ist None, setze last_runtime auf 0")
                state.total_runtime_today += state.last_runtime
                logging.info(f"KOMPRESSOR AUSGESCHALTET um {now.strftime('%H:%M:%S')}. Laufzeit: {state.last_runtime}")
            state.ausschluss_grund = None
            return True

    except Exception as e:
        logging.error(f"Unerwarteter Fehler in set_kompressor_status: {e}", exc_info=True)
        current_physical_state = GPIO.input(21) if GPIO.getmode() is not None else None
        state.kompressor_ein = (current_physical_state == GPIO.HIGH) if current_physical_state is not None else False
        state.start_time = None
        return False



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
            #logging.debug("Keine Änderung an der Konfigurationsdatei festgestellt.")
            return

        logging.info("Neue Konfiguration erkannt – wird geladen...")

        # --- Heizungsparameter mit Plausibilitätsprüfung ---
        def get_int_checked(section, key, default, min_val=None, max_val=None):
            try:
                val = new_config.getint(section, key, fallback=default)
                if (min_val is not None and val < min_val) or (max_val is not None and val > max_val):
                    raise ValueError(f"{key} außerhalb gültiger Grenzen")
                return val
            except Exception as e:
                logging.warning(f"Ungültiger Wert für {key}, verwende alten Wert ({getattr(state, key.lower(), default)}): {e}")
                return getattr(state, key.lower(), default)

        ausschalt = get_int_checked("Heizungssteuerung", "AUSSCHALTPUNKT", 55, 20, 90)
        einschalt = get_int_checked("Heizungssteuerung", "EINSCHALTPUNKT", 50, 10, 85)
        if einschalt >= ausschalt:
            raise ValueError("EINSCHALTPUNKT muss kleiner als AUSSCHALTPUNKT sein.")

        state.aktueller_ausschaltpunkt = ausschalt
        state.aktueller_einschaltpunkt = einschalt

        # --- Erhöhte Sollwerte ---
        state.einschaltpunkt_erhoeht = get_int_checked("Heizungssteuerung", "EINSCHALTPUNKT_ERHOEHT", 42)
        state.ausschaltpunkt_erhoeht = get_int_checked("Heizungssteuerung", "AUSSCHALTPUNKT_ERHOEHT", 48)

        min_pause_min = get_int_checked("Heizungssteuerung", "MIN_PAUSE", 20, 0, 1440)
        state.min_pause = timedelta(minutes=min_pause_min)

        state.sicherheits_temp = get_int_checked("Heizungssteuerung", "SICHERHEITS_TEMP", 65, 50, 90)
        state.verdampfertemperatur = get_int_checked("Heizungssteuerung", "VERDAMPFERTEMPERATUR", -10, -30, 10)

        # --- Übergangsmodus-Zeiten ---
        try:
            start_str = new_config["Heizungssteuerung"].get("UEBERGANGSMODUS_START", "06:00")
            ende_str = new_config["Heizungssteuerung"].get("UEBERGANGSMODUS_ENDE", "08:00")
            start_time = datetime.strptime(start_str, "%H:%M").time()
            end_time = datetime.strptime(ende_str, "%H:%M").time()
            state.uebergangsmodus_start = start_time
            state.uebergangsmodus_ende = end_time
            logging.info(f"Übergangsmodus-Zeiten neu geladen: Start={start_time}, Ende={end_time}")
        except Exception as e:
            logging.error(f"Ungültige Übergangsmodus-Zeitangaben – behalte alte Werte: {e}")

        # --- Telegram ---
        old_token = state.bot_token
        old_chat_id = state.chat_id
        state.bot_token = new_config.get("Telegram", "BOT_TOKEN", fallback=state.bot_token)
        state.chat_id = new_config.get("Telegram", "CHAT_ID", fallback=state.chat_id)

        if not state.bot_token or not state.chat_id:
            logging.warning("Telegram-Token oder Chat-ID fehlt. Nachrichten deaktiviert.")

        if state.bot_token and state.chat_id and (old_token != state.bot_token or old_chat_id != state.chat_id):
            await send_telegram_message(session, state.chat_id, "🔧 Konfiguration neu geladen.", state.bot_token)

        # --- Abschluss ---
        state.last_config_hash = current_hash
        logging.info("Konfiguration erfolgreich neu geladen.")

    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}", exc_info=True)
        if state.bot_token and state.chat_id:
            await send_telegram_message(
                session, state.chat_id,
                f"⚠️ Fehler beim Neuladen der Konfiguration: {str(e)}",
                state.bot_token
            )


def calculate_file_hash(file_path):
    """Berechnet den SHA-256-Hash einer Datei."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        hash_value = sha256_hash.hexdigest()
        #logging.debug(f"Hash für {file_path} berechnet: {hash_value}")
        return hash_value
    except Exception as e:
        logging.error(f"Fehler beim Berechnen des Hash-Werts für {file_path}: {e}")
        return None

async def check_network(session, timeout=5):
    """Prüfe die Netzwerkverbindung durch eine Testanfrage."""
    try:
        async with session.get("https://www.google.com", timeout=timeout) as response:
            return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logging.error(f"Netzwerkprüfung fehlgeschlagen: {e}")
        return False

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

async def log_debug_state(state, t_boiler_oben, t_boiler_mittig, t_boiler_unten, t_verd, solax_data,
                          is_night, within_uebergangsmodus, power_source, temp_conditions_met_to_start,
                          nacht_reduction, urlaubs_reduction):
    """
    Protokolliert den aktuellen Zustand der Heizungssteuerung im Debug-Log, aber nur einmal pro Minute.

    Args:
        state: Das State-Objekt mit den aktuellen Zustandsvariablen.
        t_boiler_oben: Temperatur oben im Boiler (°C).
        t_boiler_mittig: Temperatur mittig im Boiler (°C).
        t_boiler_unten: Temperatur unten im Boiler (°C).
        t_verd: Verdampfertemperatur (°C).
        solax_data: Solax-Daten (Dictionary).
        is_night: Boolean, ob Nachtzeit aktiv ist.
        within_uebergangsmodus: Boolean, ob Übergangsmodus aktiv ist.
        power_source: String, aktuelle Stromquelle (z.B. "Direkter PV-Strom").
        temp_conditions_met_to_start: Boolean, ob Temperaturbedingungen für Kompressorstart erfüllt sind.
        nacht_reduction: Float, Absenkung durch Nachtmodus.
        urlaubs_reduction: Float, Absenkung durch Urlaubsmodus.
    """
    # Zeitprüfung: Log nur schreiben, wenn mindestens 1 Minute seit dem letzten Log vergangen ist
    now = datetime.now(pytz.timezone("Europe/Berlin"))
    if hasattr(state, 'last_debug_log_time') and state.last_debug_log_time is not None:
        time_since_last_log = safe_timedelta(now, state.last_debug_log_time, state.local_tz)
        if time_since_last_log.total_seconds() < 60:
            return  # Kein Log, wenn weniger als 1 Minute vergangen ist

    # --- Bestimme den aktuellen Modus ---
    if state.solar_ueberschuss_aktiv:
        modus = "Solarmodus"
    elif is_night:
        modus = "Nachtmodus"
    elif within_uebergangsmodus:
        modus = "Übergangsmodus"
    else:
        modus = "Normalmodus"

    # --- Bestimme den regelnden Fühler basierend auf dem Modus ---
    if modus == "Solarmodus":
        regelfuehler = "unten"
    elif modus in ["Nachtmodus", "Normalmodus", "Übergangsmodus"]:
        regelfuehler = "mittig"
    else:
        regelfuehler = "unbekannt"

    # --- Temperaturwerte formatieren ---
    t_oben_log = f"{t_boiler_oben:.1f}" if t_boiler_oben is not None else "N/A"
    t_mitte_log = f"{t_boiler_mittig:.1f}" if t_boiler_mittig is not None else "N/A"
    t_unten_log = f"{t_boiler_unten:.1f}" if t_boiler_unten is not None else "N/A"
    t_verd_log = f"{t_verd:.1f}" if t_verd is not None else "N/A"

    # --- Solax-Daten formatieren ---
    bat_power_str = f"{solax_data.get('batPower', 0.0):.1f}" if solax_data else "0.0"
    soc_str = f"{solax_data.get('soc', 0.0):.1f}" if solax_data else "0.0"
    feedin_str = f"{state.feedin_power:.1f}" if hasattr(state, 'feedin_power') and state.feedin_power is not None else "0.0"

    # --- Kombinierte Absenkung für den Log ---
    reduction = f"Nacht({nacht_reduction:.1f})+Urlaub({urlaubs_reduction:.1f})"

    # --- Debug-Log erzeugen ---
    logging.debug(
        f"[Modus: {modus}] "
        f"Regel-Fühler: {regelfuehler} | "
        f"T_Oben={t_oben_log}°C | "
        f"T_Mitte={t_mitte_log}°C | "
        f"T_Unten={t_unten_log}°C | "
        f"T_Verd={t_verd_log}°C | "
        f"Einschaltpunkt={state.aktueller_einschaltpunkt:.1f}°C | "
        f"Ausschaltpunkt={state.aktueller_ausschaltpunkt:.1f}°C | "
        f"temp_conditions_met_to_start={temp_conditions_met_to_start} | "
        f"Solarüberschuss aktiv: {state.solar_ueberschuss_aktiv} | "
        f"Nacht={is_night} | "
        f"Urlaub={state.urlaubsmodus_aktiv} | "
        f"Solar={state.solar_ueberschuss_aktiv} | "
        f"Reduction={reduction} | "
        f"batPower={bat_power_str}W | "
        f"soc={soc_str}% | "
        f"feedin={feedin_str}W | "
        f"Übergangsmodus={within_uebergangsmodus} | "
        f"Power Source={power_source}"
    )

    # Zeit des letzten Logs aktualisieren
    state.last_debug_log_time = now


def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    #logging.debug(f"is_nighttime: now={now}, tzinfo={now.tzinfo}")
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

        #logging.debug(
        #    f"Nachtzeitprüfung: Jetzt={now_time}, Start={start_time_minutes}, Ende={end_time_minutes}, Ist Nacht={is_night}")
        return is_night
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False

def ist_uebergangsmodus_aktiv(state) -> bool:
    """Prüft, ob aktuell Übergangsmodus aktiv ist, basierend auf Uhrzeit im State."""
    now = datetime.now(pytz.timezone("Europe/Berlin")).time()
    start = state.uebergangsmodus_start
    ende = state.uebergangsmodus_ende

    if start < ende:
        return start <= now <= ende
    else:
        # z. B. 22:00 – 03:00
        return now >= start or now <= ende


def calculate_shutdown_point(config, is_night, solax_data, state):
    """
    Berechnet die Sollwerte basierend auf Modus und Absenkungen.
    ACHTUNG: 'config' wird nur bei Fehlern verwendet – im Normalfall wird state.config genutzt.
    """
    try:
        current_config = state.config
        nacht_reduction = float(current_config["Heizungssteuerung"].get("NACHTABSENKUNG", 0.0)) if is_night else 0.0
        urlaubs_reduction = float(
            current_config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 0.0)) if state.urlaubsmodus_aktiv else 0.0
        total_reduction = nacht_reduction + urlaubs_reduction

        bat_power = solax_data.get("batPower", 0.0)  # Auch hier float
        feedin_power = solax_data.get("feedinpower", 0.0)  # Auch hier float
        soc = solax_data.get("soc", 0.0)  # Auch hier float

        # Solarüberschuss-Logik mit korrekter Hysterese
        MIN_SOLAR_POWER_ACTIVE_THRESHOLD = 550.0  # Schwellwert, um im Solar-Modus ZU BLEIBEN
        MIN_SOLAR_POWER_INACTIVE_THRESHOLD = 600.0  # Schwellwert, um in den Solar-Modus ZU GEHEN (höher)

        # Falls ein API-Fehler vorliegt, Solarüberschuss deaktivieren
        if solax_data.get("api_fehler", False):
            logging.warning("API-Fehler: Solardaten nicht verfügbar – Solarüberschuss deaktiviert")
            state.solar_ueberschuss_aktiv = False
        # Wenn der Solarüberschuss aktiv IST, prüft man, ob er noch aktiv BLEIBEN soll
        elif state.solar_ueberschuss_aktiv:
            state.solar_ueberschuss_aktiv = (
                    bat_power > MIN_SOLAR_POWER_ACTIVE_THRESHOLD or
                    (soc >= 90.0 and feedin_power > MIN_SOLAR_POWER_ACTIVE_THRESHOLD)  # soc >= 90 für Puffer
            )
        # Wenn der Solarüberschuss NICHT aktiv IST, prüft man, ob er aktiv WERDEN soll
        else:
            state.solar_ueberschuss_aktiv = (
                    bat_power > MIN_SOLAR_POWER_INACTIVE_THRESHOLD or  # Höhere Schwelle zum Starten
                    (soc >= 95.0 and feedin_power > MIN_SOLAR_POWER_INACTIVE_THRESHOLD)  # Höhere Schwelle & soc >= 95
            )

        # Basis-Sollwerte aus der Konfiguration (als Float lesen)
        base_ausschaltpunkt = float(current_config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45.0))
        base_einschaltpunkt = float(current_config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42.0))

        # Erhöhte Sollwerte für den Solar-Modus (als Float lesen)
        erhoeht_ausschaltpunkt = float(current_config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 50.0))
        erhoeht_einschaltpunkt = float(current_config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 46.0))

        # Sollwerte basierend auf Solarüberschuss-Modus setzen
        if state.solar_ueberschuss_aktiv:
            ausschaltpunkt = erhoeht_ausschaltpunkt - total_reduction
            einschaltpunkt = erhoeht_einschaltpunkt - total_reduction
        else:
            ausschaltpunkt = base_ausschaltpunkt - total_reduction
            einschaltpunkt = base_einschaltpunkt - total_reduction

        # Minimaltemperatur schützen
        MIN_TEMPERATUR = 15.0  # Auch hier float
        ausschaltpunkt = max(MIN_TEMPERATUR, ausschaltpunkt)
        einschaltpunkt = max(MIN_TEMPERATUR, einschaltpunkt)

        # Validierung der Hysterese nach ALLEN Anpassungen
        HYSTERESE_MIN = float(current_config["Heizungssteuerung"].get("HYSTERESE_MIN", 2.0))  # Auch hier float
        if ausschaltpunkt <= einschaltpunkt:
            logging.warning(
                f"Sollwert-Korrektur: Ausschaltpunkt ({ausschaltpunkt:.1f}°C) war <= Einschaltpunkt ({einschaltpunkt:.1f}°C). "
                f"Passe Ausschaltpunkt auf {einschaltpunkt + HYSTERESE_MIN:.1f}°C an, um Mindesthysterese von {HYSTERESE_MIN:.1f}°C zu gewährleisten."
            )
            ausschaltpunkt = einschaltpunkt + HYSTERESE_MIN

        # Rückgabe der berechneten Werte
        return ausschaltpunkt, einschaltpunkt, state.solar_ueberschuss_aktiv, feedin_power, nacht_reduction, urlaubs_reduction

    except (KeyError, ValueError) as e:
        logging.error(f"Fehler in calculate_shutdown_point: {e}", exc_info=True)
        fallback_ausschaltpunkt = 45.0
        fallback_einschaltpunkt = 42.0
        state.solar_ueberschuss_aktiv = False
        feedin_power = 0.0
        nacht_reduction = 0.0
        urlaubs_reduction = 0.0
        logging.warning(
            f"Verwende Standard-Sollwerte im Fehlerfall: Ausschaltpunkt={fallback_ausschaltpunkt:.1f}, "
            f"Einschaltpunkt={fallback_einschaltpunkt:.1f}, Solarüberschuss_aktiv={state.solar_ueberschuss_aktiv}, "
            f"feedin={feedin_power:.1f}, nacht_reduction={nacht_reduction:.1f}, urlaubs_reduction={urlaubs_reduction:.1f}"
        )
        return fallback_ausschaltpunkt, fallback_einschaltpunkt, state.solar_ueberschuss_aktiv, feedin_power, nacht_reduction, urlaubs_reduction

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
    #logging.debug(f"Prüfe Solax-Datenalter: now={now}, tzinfo={now.tzinfo}, Zeitstempel={timestamp}, tzinfo={timestamp.tzinfo if timestamp else None}, Ist alt={is_old}")
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


async def watchdog_gpio(state):
    while True:
        try:
            actual_gpio = GPIO.input(21)
            expected_gpio = GPIO.HIGH if state.kompressor_ein else GPIO.LOW
            if actual_gpio != expected_gpio:
                # Prüfe Mindestlaufzeit, bevor eine Inkonsistenz gemeldet wird
                if not state.kompressor_ein:
                    now = datetime.now(pytz.timezone("Europe/Berlin"))
                    elapsed_time = safe_timedelta(now, state.start_time if state.start_time else state.last_compressor_on_time, state.local_tz)
                    min_laufzeit = timedelta(seconds=int(state.config["Heizungssteuerung"].get("MIN_LAUFZEIT_S", 900)))
                    if state.kompressor_ein and elapsed_time.total_seconds() < min_laufzeit.total_seconds():
                        logging.debug(f"Keine Inkonsistenz: Mindestlaufzeit ({min_laufzeit.total_seconds()}s) nicht erreicht, verbleibend: {min_laufzeit.total_seconds() - elapsed_time.total_seconds():.1f}s")
                        await asyncio.sleep(10)
                        continue
                logging.critical(f"GPIO-Inkonsistenz: state.kompressor_ein={state.kompressor_ein}, GPIO={actual_gpio}")
                result = await set_kompressor_status(state, state.kompressor_ein, force=True)
                if not result:
                    logging.critical("GPIO-Inkonsistenz konnte nicht behoben werden!")
                    if state.session and state.bot_token and state.chat_id:
                        await send_telegram_message(
                            state.session, state.chat_id,
                            f"🚨 KRITISCHER FEHLER: GPIO-Inkonsistenz konnte nicht behoben werden!",
                            state.bot_token
                        )
        except Exception as e:
            logging.error("Fehler im GPIO-Watchdog", exc_info=True)
            if state.session and state.bot_token and state.chat_id:
                await send_telegram_message(
                    state.session, state.chat_id,
                    f"🚨 Fehler im GPIO-Watchdog:\n{e}", state.bot_token
                )
        await asyncio.sleep(60)


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

def set_last_compressor_off_time(state, value):
    """Hilfsfunktion zum Setzen von last_compressor_off_time mit Debugging."""
    logging.debug(f"Setze last_compressor_off_time: {state.last_compressor_off_time} -> {value}")
    state.last_compressor_off_time = value


async def initialize_system(state, session, now):
    """Initialisiert GPIO, Kompressorstatus, LCD und Telegram-Nachrichten."""
    if not await initialize_gpio():
        logging.critical("GPIO-Initialisierung fehlgeschlagen!")
        raise RuntimeError("GPIO-Initialisierung fehlgeschlagen")

    actual_gpio_state = GPIO.input(21)
    if actual_gpio_state == GPIO.HIGH:
        logging.info("Kompressor ist beim Start eingeschaltet (GPIO HIGH)")
        state.kompressor_ein = True
        state.start_time = now
        state.last_compressor_on_time = now
        logging.info(f"Kompressor eingeschaltet. Startzeit: {now}")
    else:
        logging.info("Kompressor ist beim Start ausgeschaltet (GPIO LOW)")
        state.kompressor_ein = False
        set_last_compressor_off_time(state, now)

    await initialize_lcd(session)
    logging.info("Starte GPIO-Watchdog zur Zustandsüberwachung")
    asyncio.create_task(watchdog_gpio(state))

    for attempt in range(1, 4):
        if await check_network(session):
            logging.info("Netzwerkverbindung erfolgreich.")
            break
        logging.warning(f"Netzwerkprüfung fehlgeschlagen (Versuch {attempt}/3), warte 5 Sekunden...")
        await asyncio.sleep(5)
    else:
        logging.error("Keine Netzwerkverbindung nach mehreren Versuchen. Überspringe Startnachrichten.")
        return False

    if state.bot_token and state.chat_id:
        await send_telegram_message(
            session, state.chat_id,
            f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}",
            state.bot_token,
            parse_mode=None
        )
        await send_welcome_message(session, state.chat_id, state.bot_token, state)
    else:
        logging.warning("Telegram-Konfiguration fehlt, überspringe Startnachrichten.")
    return True


async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd):
    """Prüft Sensorwerte, Verdampfertemperatur und Sicherheitsabschaltung."""
    state.t_oben = t_oben
    state.t_unten = t_unten
    state.t_mittig = t_mittig
    state.t_verd = t_verd
    state.t_boiler = (t_oben + t_unten) / 2 if t_oben is not None and t_unten is not None else None

    if not await check_for_sensor_errors(session, state, t_oben, t_unten):
        state.ausschluss_grund = "Sensorfehler: Ungültige Werte"
        logging.info("Kompressor bleibt aus wegen Sensorfehler")
        if state.kompressor_ein:
            await set_kompressor_status(state, False, force=True, t_boiler_oben=t_oben)
        return False

    if t_oben is not None and t_unten is not None and (
            t_oben >= state.sicherheits_temp or t_unten >= state.sicherheits_temp):
        state.ausschluss_grund = f"Übertemperatur (>= {state.sicherheits_temp} Grad)"
        logging.error(f"Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad")
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time, state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet (Sicherheitsabschaltung). Laufzeit: {state.last_runtime}")
            else:
                logging.critical(
                    "Kritischer Fehler: Kompressor konnte trotz Übertemperatur nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt trotz Übertemperatur eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
        await send_telegram_message(
            session, state.chat_id,
            f"⚠️ Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad >= {state.sicherheits_temp} Grad",
            state.bot_token,
            parse_mode=None
        )
        return False

    if t_verd is not None and t_verd < state.verdampfertemperatur:
        state.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {state.verdampfertemperatur} Grad)"
        logging.warning(state.ausschluss_grund)
        if state.last_verdampfer_notification is None or safe_timedelta(datetime.now(state.local_tz),
                                                                        state.last_verdampfer_notification, state.local_tz) > timedelta(minutes=5):
            await send_telegram_message(
                session, state.chat_id,
                f"⚠️ Kompressor bleibt aus oder wird ausgeschaltet: {state.ausschluss_grund}",
                state.bot_token,
                parse_mode=None
            )
            state.last_verdampfer_notification = datetime.now(state.local_tz)
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time, state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(
                    f"Kompressor ausgeschaltet wegen zu niedriger Verdampfertemperatur. Laufzeit: {state.last_runtime}")
            else:
                logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt trotz niedriger Verdampfertemperatur eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
        return False
    return True


async def check_pressure_and_config(session, state):
    """Prüft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check(session, state)
    if state.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.ausschluss_grund = "Druckschalterfehler"
        logging.info("Kompressor bleibt aus wegen Druckschalterfehler")
        if state.kompressor_ein:
            await set_kompressor_status(state, False, force=True)
        return False

    if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=60):
        current_hash = calculate_file_hash("config.ini")
        if current_hash != state.last_config_hash:
            await reload_config(session, state)
            state.last_config_hash = current_hash
        state._last_config_check = datetime.now(state.local_tz)
    return True


async def determine_mode_and_setpoints(state, t_unten, t_mittig):
    """Bestimmt den Betriebsmodus und setzt Sollwerte."""
    from telegram_handler import is_solar_window  # Import hier, um Zirkelimporte zu vermeiden
    now = datetime.now(state.local_tz)
    is_night = await asyncio.to_thread(is_nighttime, state.config)

    # Prüfe Solarfenster nur alle 5 Minuten
    within_solar_window = state.last_solar_window_status
    if (state.last_solar_window_check is None or
            safe_timedelta(now, state.last_solar_window_check, state.local_tz) >= timedelta(minutes=5)):
        within_solar_window = is_solar_window(state.config, state)
        state.last_solar_window_check = now
        state.last_solar_window_status = within_solar_window

    nacht_reduction = float(state.config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
    urlaubs_reduction = float(
        state.config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 0)) if state.urlaubsmodus_aktiv else 0
    total_reduction = nacht_reduction + urlaubs_reduction

    state.solar_ueberschuss_aktiv = (
            state.batpower > 600.0 or
            (state.soc >= 95.0 and state.feedinpower > 600.0)
    )

    if state.bademodus_aktiv:
        if state.previous_modus != "Bademodus":
            logging.info("Wechsel zu Bademodus – steuere nach T_Unten")
            state.previous_modus = "Bademodus"
        return {
            "modus": "Bademodus",
            "ausschaltpunkt": state.ausschaltpunkt_erhoeht,
            "einschaltpunkt": state.ausschaltpunkt_erhoeht - 4,
            "regelfuehler": t_unten,
            "nacht_reduction": 0,
            "urlaubs_reduction": 0,
            "solar_ueberschuss_aktiv": False
        }

    if state.previous_modus == "Bademodus":
        logging.info("Wechsel von Bademodus zu Normalmodus")

    if state.solar_ueberschuss_aktiv:
        modus = "Solarüberschuss"
        ausschaltpunkt = state.ausschaltpunkt_erhoeht
        einschaltpunkt = state.einschaltpunkt_erhoeht
        regelfuehler = t_unten
    elif within_solar_window:
        modus = "Übergangsmodus"
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    elif is_night:
        modus = "Nachtmodus"
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    else:
        modus = "Normalmodus"
        ausschaltpunkt = state.aktueller_ausschaltpunkt - total_reduction
        einschaltpunkt = state.aktueller_einschaltpunkt - total_reduction
        regelfuehler = t_mittig

    if state.previous_modus != modus:
        logging.info(f"Wechsel zu Modus: {modus}")
        state.previous_modus = modus

    return {
        "modus": modus,
        "ausschaltpunkt": ausschaltpunkt,
        "einschaltpunkt": einschaltpunkt,
        "regelfuehler": regelfuehler,
        "nacht_reduction": nacht_reduction,
        "urlaubs_reduction": urlaubs_reduction,
        "solar_ueberschuss_aktiv": state.solar_ueberschuss_aktiv
    }


async def handle_compressor_off(state, session, regelfuehler, ausschaltpunkt, min_laufzeit, t_oben):
    """Prüft Abschaltbedingungen und schaltet Kompressor aus."""
    abschalten = regelfuehler is not None and regelfuehler >= ausschaltpunkt
    if abschalten and (state.previous_abschalten != abschalten or (
            state.last_abschalt_log is None or
            safe_timedelta(datetime.now(state.local_tz), state.last_abschalt_log, state.local_tz) >= timedelta(minutes=5))):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Abschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad >= {ausschaltpunkt:.1f} Grad"
        )
        logging.info(state.ausschluss_grund)
        state.last_abschalt_log = datetime.now(state.local_tz)
    state.previous_abschalten = abschalten

    can_turn_off = True
    if state.kompressor_ein and abschalten:
        elapsed_time = safe_timedelta(datetime.now(state.local_tz), state.start_time or state.last_compressor_on_time, state.local_tz)
        if elapsed_time.total_seconds() < min_laufzeit.total_seconds() - 0.5:
            can_turn_off = False
            state.ausschluss_grund = f"Mindestlaufzeit nicht erreicht ({min_laufzeit.total_seconds() - elapsed_time.total_seconds():.1f}s)"
            logging.debug(state.ausschluss_grund)

    if abschalten and state.kompressor_ein and can_turn_off:
        result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_oben)
        if result:
            state.kompressor_ein = False
            set_last_compressor_off_time(state, datetime.now(state.local_tz))
            state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time, state.local_tz)
            state.total_runtime_today += state.last_runtime
            state.last_completed_cycle = datetime.now(state.local_tz)
            logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
            return True
        logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
        await send_telegram_message(
            session, state.chat_id,
            f"🚨 KRITISCHER FEHLER: Kompressor bleibt eingeschaltet!",
            state.bot_token,
            parse_mode=None
        )
    return False


async def handle_compressor_on(state, session, regelfuehler, einschaltpunkt, min_laufzeit, min_pause, within_solar_window, t_oben):
    """Prüft Einschaltbedingungen und schaltet Kompressor ein."""
    now = datetime.now(state.local_tz)
    temp_conditions_met = regelfuehler is not None and regelfuehler <= einschaltpunkt
    if temp_conditions_met and state.previous_temp_conditions != temp_conditions_met:
        logging.info(
            f"[{state.previous_modus}] Einschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad <= {einschaltpunkt:.1f} Grad"
        )
    elif not temp_conditions_met and (
            state.last_no_start_log is None or
            safe_timedelta(now, state.last_no_start_log, state.local_tz) >= timedelta(minutes=5)):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad > {einschaltpunkt:.1f} Grad"
        )
        logging.debug(state.ausschluss_grund)
        state.last_no_start_log = now
    state.previous_temp_conditions = temp_conditions_met

    solar_conditions_met = not (not state.bademodus_aktiv and within_solar_window and not state.solar_ueberschuss_aktiv)
    if not solar_conditions_met and (
            state.last_no_start_log is None or
            safe_timedelta(now, state.last_no_start_log, state.local_tz) >= timedelta(minutes=5)):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten im Übergangsmodus: Solarüberschuss nicht aktiv "
            f"({state.uebergangsmodus_start.strftime('%H:%M')}–{state.uebergangsmodus_ende.strftime('%H:%M')})"
        )
        logging.debug(state.ausschluss_grund)
        state.last_no_start_log = now

    pause_ok = True
    if not state.kompressor_ein and temp_conditions_met and solar_conditions_met and state.last_compressor_off_time:
        time_since_off = safe_timedelta(now, state.last_compressor_off_time, state.local_tz, default=timedelta.max)
        if time_since_off.total_seconds() < min_pause.total_seconds() - 0.5:
            pause_ok = False
            pause_remaining = min_pause - time_since_off
            reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"
            if state.last_pause_log is None or safe_timedelta(now, state.last_pause_log, state.local_tz) > timedelta(minutes=5):
                logging.info(f"Kompressor START VERHINDERT: {reason}")
                await send_telegram_message(
                    session, state.chat_id,
                    f"⚠️ Kompressor bleibt aus: {reason}...",
                    state.bot_token,
                    parse_mode=None
                )
                state.last_pause_telegram_notification = now
                state.current_pause_reason = reason
                state.last_pause_log = now
            state.ausschluss_grund = reason
        else:
            state.current_pause_reason = None
            state.last_pause_log = None
            state.last_pause_telegram_notification = None

    if not state.kompressor_ein and temp_conditions_met and pause_ok and solar_conditions_met:
        can_start_new_cycle = True
        if state.last_completed_cycle and safe_timedelta(now, state.last_completed_cycle,
                                                         state.local_tz).total_seconds() < min_laufzeit.total_seconds() + min_pause.total_seconds():
            can_start_new_cycle = False
            state.ausschluss_grund = (
                f"Neuer Zyklus nicht erlaubt: Warte auf Abschluss von Mindestlaufzeit + Mindestpause "
                f"({min_laufzeit.total_seconds() + min_pause.total_seconds() - safe_timedelta(now, state.last_completed_cycle, state.local_tz).total_seconds():.1f}s)"
            )
            logging.debug(state.ausschluss_grund)

        if can_start_new_cycle:
            logging.info(f"Alle Bedingungen für Kompressorstart erfüllt. Versuche einzuschalten (Modus: {state.previous_modus}).")
            result = await set_kompressor_status(state, True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = True
                state.start_time = now
                state.last_compressor_on_time = now
                logging.info(f"Kompressor eingeschaltet. Startzeit: {now}")
                state.ausschluss_grund = None
                return True
            state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler beim Einschalten"
            logging.info(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")
    return False


async def handle_mode_switch(state, session, t_oben, t_mittig):
    """Prüft und behandelt Moduswechsel bei Solarüberschuss-Änderung."""
    if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv and not state.bademodus_aktiv:
        effective_ausschaltpunkt = state.previous_ausschaltpunkt or state.aktueller_ausschaltpunkt
        if not state.solar_ueberschuss_aktiv and t_oben is not None and t_mittig is not None:
            if t_oben >= effective_ausschaltpunkt or t_mittig >= effective_ausschaltpunkt:
                result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_oben)
                if result:
                    state.kompressor_ein = False
                    set_last_compressor_off_time(state, datetime.now(state.local_tz))
                    state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time)
                    state.total_runtime_today += state.last_runtime
                    logging.info(f"Kompressor ausgeschaltet bei Moduswechsel. Laufzeit: {state.last_runtime}")
                    state.ausschluss_grund = None
                    return True
                logging.critical("Kritischer Fehler: Kompressor konnte bei Moduswechsel nicht ausgeschaltet werden!")
                await send_telegram_message(
                    session, state.chat_id,
                    f"🚨 KRITISCHER FEHLER: Kompressor bleibt bei Moduswechsel eingeschaltet!",
                    state.bot_token,
                    parse_mode=None
                )
    return False


async def update_runtime_and_log(state, session, t_oben, t_unten, t_mittig, t_verd, solax_data, power_source):
    """Aktualisiert Laufzeit und protokolliert Daten in CSV."""
    now = datetime.now(state.local_tz)
    if state.kompressor_ein:
        state.current_runtime = safe_timedelta(now, state.last_compressor_on_time, state.local_tz, default=timedelta())
    else:
        state.current_runtime = timedelta()

    if state.last_log_time is None or safe_timedelta(now, state.last_log_time, state.local_tz) >= timedelta(seconds=60):
        await log_to_csv(
            state, now, t_oben, t_unten, t_mittig, t_verd, solax_data,
            state.aktueller_einschaltpunkt, state.aktueller_ausschaltpunkt,
            state.solar_ueberschuss_aktiv, state.nacht_reduction, power_source
        )
        state.last_log_time = now

    if now.date() != state.last_day:
        logging.info(f"Neuer Tag erkannt: {now.date()}. Setze Gesamtlaufzeit zurück.")
        state.total_runtime_today = timedelta()
        state.last_day = now.date()


async def check_watchdog(state, session, last_cycle_time):
    """Prüft den Watchdog für Zykluszeitüberschreitungen."""
    now = datetime.now(state.local_tz)
    if last_cycle_time is not None:
        cycle_duration = safe_timedelta(now, last_cycle_time, state.local_tz).total_seconds()
    else:
        cycle_duration = 0
        logging.debug("last_cycle_time ist None, setze cycle_duration auf 0")

    if cycle_duration > 30:
        state.watchdog_warning_count += 1
        logging.error(
            f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {state.watchdog_warning_count}/3")
        if state.watchdog_warning_count >= 3:
            result = await set_kompressor_status(state, False, force=True)
            await send_telegram_message(
                session, state.chat_id,
                f"🚨 Watchdog-Fehler: Programm beendet.",
                state.bot_token,
                parse_mode=None
            )
            await shutdown(session, state)
            raise SystemExit("Watchdog-Exit")
    else:
        state.watchdog_warning_count = 0
    return now

async def main_loop(config, state, session):
    """Hauptschleife des Programms mit State-Objekt."""
    local_tz = pytz.timezone("Europe/Berlin")
    min_laufzeit = timedelta(seconds=int(state.config["Heizungssteuerung"].get("MIN_LAUFZEIT_S", 900)))
    min_pause = timedelta(seconds=int(state.config["Heizungssteuerung"].get("MIN_AUSZEIT_S", 900)))

    try:
        # Initialisierung
        now = datetime.now(local_tz)
        if not await initialize_system(state, session, now):
            await asyncio.sleep(30)
            return

        # Starte Telegram-Task
        telegram_task_handle = asyncio.create_task(telegram_task(
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

        # Watchdog-Variablen
        last_cycle_time = now
        state.watchdog_warning_count = 0

        while True:
            try:
                now = datetime.now(local_tz)

                # Bademodus-Änderung prüfen
                if state.bademodus_aktiv != state.previous_bademodus_aktiv:
                    logging.info(f"Bademodus geändert zu: {state.bademodus_aktiv}")
                    state.previous_bademodus_aktiv = state.bademodus_aktiv

                # Urlaubsmodus prüfen
                if state.urlaubsmodus_aktiv and state.urlaubsmodus_ende and now >= state.urlaubsmodus_ende:
                    await deaktivere_urlaubsmodus(session, state.chat_id, state.bot_token, config, state)
                    await send_telegram_message(
                        session, state.chat_id,
                        "🌴 Urlaubsmodus wurde automatisch beendet (Zeit abgelaufen).",
                        state.bot_token,
                        parse_mode=None
                    )

                # Sensorwerte lesen
                t_oben = await read_temperature_cached(SENSOR_IDS["oben"])
                t_unten = await read_temperature_cached(SENSOR_IDS["unten"])
                t_mittig = await read_temperature_cached(SENSOR_IDS["mittig"])
                t_verd = await read_temperature_cached(SENSOR_IDS["verd"])

                # Sensor- und Sicherheitsprüfungen
                if not await check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd):
                    await asyncio.sleep(2)
                    continue

                # Druckschalter und Konfiguration prüfen
                if not await check_pressure_and_config(session, state):
                    await asyncio.sleep(2)
                    continue

                # Solax-Daten abrufen
                solax_result = await fetch_solax_data(session, state, now)
                state.acpower = solax_result.get("acpower", 0)
                state.feedinpower = solax_result.get("feedinpower", 0)
                state.batpower = solax_result.get("batPower", 0)
                state.soc = solax_result.get("soc", 0)
                state.powerdc1 = solax_result.get("powerdc1", 0)
                state.powerdc2 = solax_result.get("powerdc2", 0)
                state.consumeenergy = solax_result.get("consumeenergy", 0)
                state.solarueberschuss = state.powerdc1 + state.powerdc2
                state.power_source = get_power_source(solax_result["solax_data"]) if solax_result["solax_data"] else "Unbekannt"

                # Betriebsmodus bestimmen
                mode_info = await determine_mode_and_setpoints(state, t_unten, t_mittig)
                state.aktueller_ausschaltpunkt = mode_info["ausschaltpunkt"]
                state.aktueller_einschaltpunkt = mode_info["einschaltpunkt"]
                state.nacht_reduction = mode_info["nacht_reduction"]
                state.solar_ueberschuss_aktiv = mode_info["solar_ueberschuss_aktiv"]

                # Kompressor ausschalten
                if await handle_compressor_off(state, session, mode_info["regelfuehler"], mode_info["ausschaltpunkt"], min_laufzeit, t_oben):
                    await asyncio.sleep(2)
                    continue

                # Kompressor einschalten
                await handle_compressor_on(state, session, mode_info["regelfuehler"], mode_info["einschaltpunkt"],
                                           min_laufzeit, min_pause, state.last_solar_window_status, t_oben)

                # Moduswechsel behandeln
                await handle_mode_switch(state, session, t_oben, t_mittig)

                # Laufzeit und CSV-Protokollierung
                await update_runtime_and_log(state, session, t_oben, t_unten, t_mittig, t_verd, solax_result["solax_data"], state.power_source)

                # Watchdog prüfen
                last_cycle_time = await check_watchdog(state, session, last_cycle_time)

                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Fehler in der Hauptschleife: {e}", exc_info=True)
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        telegram_task_handle.cancel()
        await asyncio.gather(telegram_task_handle, return_exceptions=True)
        raise

    finally:
        await shutdown(session, state)


async def run_program():
    async with aiohttp.ClientSession() as session:
        config = configparser.ConfigParser()
        try:
            logging.info("Lese Konfigurationsdatei...")
            config.read("config.ini")
            if not config.sections():
                raise ValueError("Konfiguration konnte nicht geladen werden")
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfiguration: {e}", exc_info=True)
            raise

        state = State(config)
        state.session = session
        logging.info("State-Objekt initialisiert")

        # CSV-Initialisierung
        logging.info("Initialisiere CSV-Datei...")
        if not os.path.exists("heizungsdaten.csv"):
            async with aiofiles.open("heizungsdaten.csv", 'w', newline='') as csvfile:
                header = (
                    "Zeitstempel,T_Oben,T_Unten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
                    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
                    "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Nachtabsenkung,PowerSource\n"
                )
                await csvfile.write(header)
                logging.info("CSV-Header geschrieben: " + header.strip())

        try:
            logging.info("Richte Logging ein...")
            await setup_logging(session, state)
            logging.info("Starte main_loop...")
            await main_loop(config, state, session)
        except KeyboardInterrupt:
            logging.info("Programm durch Benutzer abgebrochen (Ctrl+C).")
        except asyncio.CancelledError:
            logging.info("Hauptschleife abgebrochen.")
        except Exception as e:
            logging.error(f"Unerwarteter Fehler in run_program: {e}", exc_info=True)
            # Process remaining Telegram messages before raising
            root_logger = logging.getLogger()
            for handler in root_logger.handlers:
                if isinstance(handler, TelegramHandler):
                    await handler.process_queue()
                    handler.close()
            raise
        finally:
            logging.info("Führe shutdown aus...")
            # Process remaining Telegram messages and close handlers
            root_logger = logging.getLogger()
            for handler in root_logger.handlers:
                if isinstance(handler, TelegramHandler):
                    await handler.process_queue()
                    handler.close()
            await shutdown(session, state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)  # Fallback-Logging vor setup_logging
    try:
        asyncio.run(run_program())
    except Exception as e:
        logging.error(f"Fehler beim Starten des Skripts: {e}", exc_info=True)
        raise