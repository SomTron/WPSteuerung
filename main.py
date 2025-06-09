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
import socket
import numpy as np
from typing import Optional
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

            # Prüfe, ob ein Event Loop für diesen Thread existiert
            try:
                loop = asyncio.get_event_loop_policy().get_event_loop()
            except RuntimeError:
                # Kein Loop verfügbar → ignorieren
                return

            if loop and loop.is_running():
                # Nachricht in die Queue legen
                self.queue.put_nowait(msg)

                # Task zur Verarbeitung starten oder neu erstellen
                if not self.task or self.task.done():
                    self.task = asyncio.run_coroutine_threadsafe(self.process_queue(), loop)
            else:
                # Optional: Log-Nachricht puffern oder ignorieren
                pass

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
        self.start_time = None  # Startzeit des Kompressors (None, wenn aus)
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
            self.einschaltpunkt_erhoeht = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", "42"))
            self.ausschaltpunkt_erhoeht = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", "48"))
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
            #logging.debug(
            #   f"Übergangsmodus-Zeiten gesetzt: Start={self.uebergangsmodus_start}, Ende={self.uebergangsmodus_ende}")
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


# Neue Hilfsfunktion für sichere Zeitdifferenzberechnung
def safe_timedelta(now, timestamp, default=timedelta()):
    """
    Berechnet die Zeitdifferenz sicher, behandelt None-Werte und Typfehler.

    Args:
        now: Aktueller Zeitstempel (datetime)
        timestamp: Zu vergleichender Zeitstempel (datetime oder None)
        default: Standardwert, falls Berechnung fehlschlägt (timedelta)

    Returns:
        timedelta: Zeitdifferenz oder Standardwert
    """
    if timestamp is None:
        logging.warning(f"Zeitstempel ist None, verwende Standard: {default}")
        return default
    try:
        if timestamp.tzinfo is None:
            logging.warning(f"Zeitzone fehlt für Zeitstempel: {timestamp}. Lokalisiere auf Europe/Berlin.")
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

    # Zeitzone für letzten Call prüfen
    if state.last_api_call and state.last_api_call.tzinfo is None:
        state.last_api_call = local_tz.localize(state.last_api_call)

    # Innerhalb der 5-Minuten-Sperre: nicht erneut abfragen
    if state.last_api_call and (now - state.last_api_call) < timedelta(minutes=5):
        logging.debug("Solax-API nicht abgefragt – innerhalb 5-Minuten-Zeitraum")
        return state.last_api_data

    max_retries = 3
    retry_delay = 5

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Python aiohttp)",
        "Host": "www.solaxcloud.com"
    }

    for attempt in range(max_retries):
        try:
            params = {"tokenId": state.token_id, "sn": state.sn}
            logging.debug(f"Solax-API Anfrage wird gesendet (Versuch {attempt + 1}) – params: {params}")

            async with session.get(
                API_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers
            ) as response:
                logging.debug(f"HTTP-Status: {response.status}")
                response.raise_for_status()

                data = await response.json()
                logging.debug(f"Antwortdaten (Roh): {data}")

                if data.get("success"):
                    result = data.get("result", {})
                    logging.info(f"Solax-API erfolgreich (Versuch {attempt + 1})")
                    state.last_api_data = result
                    state.last_api_timestamp = now
                    state.last_api_call = now
                    return result
                else:
                    logging.error(f"Solax-API meldet Fehler: {data.get('exception', 'Unbekannter Fehler')} (Versuch {attempt + 1})")
                    return None

        except asyncio.TimeoutError:
            logging.error(f"❌ Timeout bei Solax-API (Versuch {attempt + 1}/{max_retries})")
        except aiohttp.ClientError as e:
            logging.error(f"❌ ClientError bei API-Anfrage (Versuch {attempt + 1}/{max_retries}): {e}")
        except Exception as e:
            logging.error(f"❌ Unerwarteter Fehler bei API-Anfrage (Versuch {attempt + 1}): {e}", exc_info=True)

        if attempt < max_retries - 1:
            logging.debug(f"Warte {retry_delay} Sekunden vor erneutem Versuch...")
            await asyncio.sleep(retry_delay)

    logging.error("🚫 Maximale Wiederholungen erreicht, verwende Fallback-Daten.")
    return {
        "acpower": 0,
        "feedinpower": 0,
        "batPower": 0,
        "soc": 0,
        "powerdc1": 0,
        "powerdc2": 0,
        "consumeenergy": 0,
        "api_fehler": True
    }



async def solax_updater(state):
    import socket
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Python aiohttp)",
        "Host": "www.solaxcloud.com"
    }

    # 🕒 Warte auf Netzwerkstabilisierung nach Start
    logging.info("Solax-Updater startet in 60 Sekunden...")
    await asyncio.sleep(60)

    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        while True:
            try:
                now = datetime.now(pytz.timezone("Europe/Berlin"))

                # Prüfen, ob 5 Minuten seit letzter Abfrage vergangen sind
                if not state.last_api_call or (now - state.last_api_call) > timedelta(minutes=5):
                    logging.debug("Starte Solax-API-Abfrage...")

                    data = await get_solax_data(session, state)

                    if data is not None and not data.get("api_fehler", False):
                        result = {
                            "solax_data": data,
                            "acpower": data.get("acpower", "N/A"),
                            "feedinpower": data.get("feedinpower", "N/A"),
                            "batPower": data.get("batPower", "N/A"),
                            "soc": data.get("soc", "N/A"),
                            "powerdc1": data.get("powerdc1", "N/A"),
                            "powerdc2": data.get("powerdc2", "N/A"),
                            "consumeenergy": data.get("consumeenergy", "N/A"),
                        }
                        state.last_api_data = result
                        state.last_api_timestamp = now
                        state.api_verfügbar = True
                        logging.debug("Solax-Daten erfolgreich aktualisiert.")
                    else:
                        logging.warning("⚠️ get_solax_data() lieferte None oder Fallback – API evtl. nicht erreichbar")
                        state.api_verfügbar = False
                else:
                    logging.debug("Solax-Abfrage übersprungen – noch innerhalb 5 Minuten")
            except Exception as e:
                logging.error(f"❌ Solax-Updater Fehler: {e}", exc_info=True)
                state.api_verfügbar = False

            await asyncio.sleep(30)




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

    if not pressure_ok:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True)
            if result:
                state.kompressor_ein = False
                now_correct = datetime.now(local_tz)  # Sicherstellen, dass aktuelle Zeit verwendet wird
                set_last_compressor_off_time(state, now_correct)  # Korrekte Zuweisung
                state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
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
    fehler, is_overtemp = await check_boiler_sensors(t_boiler_oben, t_boiler_unten, state.config)

    if fehler:
        if state.kompressor_ein:
            result = await set_kompressor_status(state, False, force=True)
            if result:
                state.kompressor_ein = False
                now_correct = datetime.now(local_tz)  # Sicherstellen, dass aktuelle Zeit verwendet wird
                set_last_compressor_off_time(state, now_correct)  # Korrekte Zuweisung
                state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
                logging.info(f"Kompressor ausgeschaltet (Sensorfehler: {fehler}).")
            reset_sensor_cache()
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
        force (bool): Wenn True, werden Mindestlaufzeit/Mindestpause ignoriert (z.B. bei Fehlern).
        t_boiler_oben (Optional[float]): Aktuelle obere Boilertemperatur für Sicherheitsabschaltung.

    Returns:
        bool: True bei Erfolg, False bei Fehlschlag oder wenn Aktion nicht durchgeführt wurde.
    """
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)

    max_attempts = 3
    attempt_delay = float(state.config["Heizungssteuerung"].get("GPIO_ATTEMPT_DELAY", 0.1))

    # Lese Konfigurationswerte sicher
    try:
        SICHERHEITS_TEMP = float(state.config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 52.0))
        min_laufzeit = timedelta(seconds=int(state.config["Heizungssteuerung"].get("MIN_LAUFZEIT_S", 900)))
        min_pause = timedelta(minutes=int(state.config["Heizungssteuerung"].get("MIN_PAUSE", 20)))
    except (KeyError, ValueError, configparser.Error) as e:
        logging.error(f"Fehler beim Lesen der Konfiguration für set_kompressor_status: {e}. Verwende Standardwerte.")
        SICHERHEITS_TEMP = 52.0
        min_laufzeit = timedelta(seconds=900)
        min_pause = timedelta(minutes=20)

    state.min_laufzeit = min_laufzeit
    state.min_pause = min_pause

    async with state.gpio_lock:
        logging.debug(f"Aufruf set_kompressor_status: Ziel={'EIN' if ein else 'AUS'}, Force={force}, "
                      f"Aktuell kompressor_ein={state.kompressor_ein}")
        try:
            if GPIO.getmode() is None:
                logging.critical("GPIO nicht initialisiert in set_kompressor_status!")
                return False

            current_physical_state = GPIO.input(21)
            current_intended_state = state.kompressor_ein

            if force:
                logging.debug("Force=True: Sicherheits- und Pausenprüfung übersprungen.")
                if not ein:
                    logging.debug("Force=True und Ausschalten: Umgehe alle Prüfungen")
                    state.start_time = None

            if ein:
                if current_intended_state or current_physical_state == GPIO.HIGH:
                    if not current_intended_state:
                        logging.warning("GPIO war HIGH, obwohl state.kompressor_ein=False. Korrigiere Zustand.")
                        state.kompressor_ein = True
                        now_correct = datetime.now(local_tz)
                        state.start_time = now_correct
                        state.last_compressor_on_time = now_correct
                        logging.info(f"Kompressor eingeschaltet. Startzeit: {now_correct}")
                    logging.debug("Kompressor ist bereits an oder GPIO ist bereits HIGH.")
                    return True
            else:
                if not current_intended_state and current_physical_state == GPIO.LOW:
                    logging.debug("Kompressor ist bereits aus oder GPIO ist bereits LOW.")
                    return True

            if not force and ein and t_boiler_oben is not None and t_boiler_oben >= SICHERHEITS_TEMP:
                logging.warning(f"Sicherheitsabschaltung: T_Oben={t_boiler_oben:.1f}°C >= {SICHERHEITS_TEMP}°C")
                return False

            if ein and not force and state.kompressor_ein:
                if state.start_time is None and state.last_compressor_on_time is not None:
                    state.start_time = state.last_compressor_on_time
                elapsed_time = now - state.start_time if state.start_time else timedelta(seconds=9999)
                if elapsed_time.total_seconds() < min_laufzeit.total_seconds() - 0.5:
                    grund = f"Minimale Laufzeit ({min_laufzeit.total_seconds():.0f}s) nicht erreicht ({elapsed_time.total_seconds():.0f}s)"
                    if state.ausschluss_grund != grund or (
                            state.last_ausschluss_log is None or (now - state.last_ausschluss_log) >= timedelta(seconds=30)):
                        logging.info(f"Kompressor START VERHINDERT: {grund}")
                        state.ausschluss_grund = grund
                        state.last_ausschluss_log = now
                    state.start_time = None
                    return False

            if not ein and not force:
                time_since_off = safe_timedelta(now, state.last_compressor_off_time)
                if time_since_off < min_pause:
                    grund = f"Minimale Pause ({min_pause.total_seconds():.0f}s) nicht erreicht ({time_since_off.total_seconds():.0f}s)"
                    if state.ausschluss_grund != grund or (
                            state.last_ausschluss_log is None or (now - state.last_ausschluss_log) >= timedelta(seconds=30)):
                        logging.info(f"Kompressor START VERHINDERT: {grund}")
                        state.ausschluss_grund = grund
                        state.last_ausschluss_log = now
                    return False

            target_gpio = GPIO.HIGH if ein else GPIO.LOW
            success = False
            for attempt in range(max_attempts):
                GPIO.output(21, target_gpio)
                await asyncio.sleep(attempt_delay)
                readback = GPIO.input(21)
                if readback == target_gpio:
                    success = True
                    break

            if not success:
                if ein:
                    logging.critical(f"Konnte GPIO 21 nicht auf HIGH setzen nach {max_attempts} Versuchen!")
                    state.kompressor_ein = False
                    now_correct = datetime.now(local_tz)
                    set_last_compressor_off_time(state, now_correct)
                    state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                    state.total_runtime_today += state.last_runtime
                    state.start_time = None
                    logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
                    if state.bot_token and state.chat_id and state.session:
                        asyncio.create_task(send_telegram_message(
                            state.session, state.chat_id,
                            "🚨 KRITISCHER FEHLER: Kompressor konnte nicht eingeschaltet werden!",
                            state.bot_token))
                    return False
                else:
                    logging.critical(f"Konnte GPIO 21 nicht auf LOW setzen nach {max_attempts} Versuchen!")
                    state.kompressor_ein = True
                    now_correct = datetime.now(local_tz)
                    state.start_time = now_correct
                    state.last_compressor_on_time = now_correct
                    logging.info(f"Kompressor eingeschaltet. Startzeit: {now_correct}")
                    if state.bot_token and state.chat_id and state.session:
                        asyncio.create_task(send_telegram_message(
                            state.session, state.chat_id,
                            "🚨 KRITISCHER FEHLER: Kompressor konnte nicht ausgeschaltet werden!",
                            state.bot_token))
                    return False

            if ein:
                state.kompressor_ein = True
                now_correct = datetime.now(local_tz)
                state.start_time = now_correct
                state.last_compressor_on_time = now_correct
                state.current_runtime = timedelta()
                state.ausschluss_grund = None
                logging.info(f"KOMPRESSOR EINGESCHALTET um {now_correct.strftime('%H:%M:%S')}.")
            else:
                state.kompressor_ein = False
                state.start_time = None
                now_correct = datetime.now(local_tz)
                set_last_compressor_off_time(state, now_correct)
                state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                state.total_runtime_today += state.last_runtime
                logging.info(f"KOMPRESSOR AUSGESCHALTET um {now_correct.strftime('%H:%M:%S')}. Laufzeit: {state.last_runtime}")

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
        # Immer die aktuellste Konfiguration verwenden!
        config = state.config
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

        # +++ NEU: Kein Solarmodus, wenn Nachtmodus aktiv +++
        if is_night:
            logging.debug("Nachtmodus aktiv → Solarüberschuss deaktiviert")
            state.solar_ueberschuss_aktiv = False
        elif solax_data.get("api_fehler", False):
            logging.warning("API-Fehler: Solardaten nicht verfügbar – Solarüberschuss deaktiviert")
            state.solar_ueberschuss_aktiv = False
        elif state.solar_ueberschuss_aktiv:
            # Ausschaltlogik: Bleibe aktiv, solange genug Überschuss da
            state.solar_ueberschuss_aktiv = bat_power > MIN_SOLAR_POWER_ACTIVE or (
                        soc > 90 and feedin_power > MIN_SOLAR_POWER_ACTIVE)
        else:
            # Einschaltlogik: Nur bei starkem Überschuss starten
            state.solar_ueberschuss_aktiv = bat_power > MIN_SOLAR_POWER_ACTIVE or (
                        soc > 95 and feedin_power > MIN_SOLAR_POWER_ACTIVE)

        # Sollwerte berechnen
        if state.solar_ueberschuss_aktiv:
            ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 50)) - total_reduction
            einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 46)) - total_reduction
        else:
            ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45)) - total_reduction
            einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42)) - total_reduction

        # Minimaltemperatur schützen
        MIN_TEMPERATUR = 15
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
                logging.warning("GPIO-Inkonsistenz erkannt – Synchronisiere...")

                if state.kompressor_ein:
                    await set_kompressor_status(state, True)  # Einschalten (normal)
                else:
                    await set_kompressor_status(state, False, force=True)  # Ausschalten (erzwingen)
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


# Asynchrone Hauptschleife
async def main_loop(config, state, session):
    """Hauptschleife des Programms mit State-Objekt."""
    local_tz = pytz.timezone("Europe/Berlin")
    NOTIFICATION_COOLDOWN = 600
    PRESSURE_ERROR_DELAY = timedelta(minutes=5)
    WATCHDOG_MAX_WARNINGS = 3
    csv_lock = asyncio.Lock()
    now = datetime.now(local_tz)

    try:
        # GPIO-Initialisierung
        if not await initialize_gpio():
            logging.critical("GPIO-Initialisierung fehlgeschlagen!")
            raise RuntimeError("GPIO-Initialisierung fehlgeschlagen")


        # Erzwinge sicheren Startzustand: Kompressor AUS
        try:
            GPIO.output(GIO21_PIN, GPIO.LOW)
            state.kompressor_ein = False
            state.start_time = None
            now_correct = datetime.now(local_tz)
            set_last_compressor_off_time(state, now_correct)
            logging.info("Kompressor-Startstatus gesetzt: AUS (GPIO LOW erzwungen)")
        except Exception as e:
            logging.error(f"Fehler beim Setzen des Startzustands für Kompressor: {e}", exc_info=True)

        # LCD-Initialisierung
        await initialize_lcd(session)

        # Starte Watchdog für GPIO
        logging.info("Starte GPIO-Watchdog zur Zustandsüberwachung")
        asyncio.create_task(watchdog_gpio(state))

        # Warte auf Netzwerkverbindung
        logging.info("Prüfe Netzwerkverbindung vor dem Senden der Startnachrichten...")
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            if await check_network(session):
                logging.info("Netzwerkverbindung erfolgreich.")
                break
            logging.warning(f"Netzwerkprüfung fehlgeschlagen (Versuch {attempt}/{max_attempts}), warte 5 Sekunden...")
            await asyncio.sleep(5)
        else:
            logging.error("Keine Netzwerkverbindung nach mehreren Versuchen. Überspringe Startnachrichten.")

        # Startnachrichten
        if state.bot_token and state.chat_id:
            if not await send_telegram_message(
                    session, state.chat_id,
                    f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}",
                    state.bot_token
            ):
                logging.warning("Startnachricht konnte nicht gesendet werden, fahre fort.")
            if not await send_welcome_message(session, state.chat_id, state.bot_token):
                logging.warning("Willkommensnachricht konnte nicht gesendet werden, fahre fort.")
        else:
            logging.warning("Telegram-Konfiguration fehlt, überspringe Startnachrichten.")

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

        # Starte Solax-Updater (asynchrone Hintergrundabfrage)
        asyncio.create_task(solax_updater(state))

        # Initialisiere Zeitstempel
        state.last_log_time = state.last_log_time or now
        if state.last_log_time.tzinfo is None:
            state.last_log_time = local_tz.localize(state.last_log_time)
        state.last_day = state.last_day or now.date()
        state.last_compressor_on_time = state.last_compressor_on_time or now
        if state.last_compressor_on_time.tzinfo is None:
            state.last_compressor_on_time = local_tz.localize(state.last_compressor_on_time)
        if state.last_compressor_off_time is None:
            #set_last_compressor_off_time(state, now - state.min_pause)
            logging.info(f"last_compressor_off_time war None, ausgeklammert initialisiert auf {state.last_compressor_off_time}")
        if state.last_compressor_off_time.tzinfo is None:
            #set_last_compressor_off_time(state, local_tz.localize(state.last_compressor_off_time))
            logging.info(f"last_compressor_off_time.tzinfo war None, ausgeklammert initialisiert auf {state.last_compressor_off_time}")
        state.last_pressure_error_time = state.last_pressure_error_time or now
        if state.last_pressure_error_time.tzinfo is None:
            state.last_pressure_error_time = local_tz.localize(state.last_pressure_error_time)
        state.last_overtemp_notification = state.last_overtemp_notification or now
        if state.last_overtemp_notification.tzinfo is None:
            state.last_overtemp_notification = local_tz.localize(state.last_overtemp_notification)
        state.previous_ausschaltpunkt = state.previous_ausschaltpunkt or None
        state.previous_einschaltpunkt = state.previous_einschaltpunkt or None
        state.previous_solar_ueberschuss_aktiv = state.solar_ueberschuss_aktiv or False

        # Variables for the solar-only start window after night setback
        night_setback_end_time_today = None
        solar_only_window_end_time_today = None

        # Watchdog-Variablen
        last_cycle_time = datetime.now(local_tz)
        watchdog_warning_count = 0

        while True:
            try:
                now = datetime.now(local_tz)
                #logging.debug(f"Schleifeniteration: {now}")

                # Sensorwerte lesen (vor allen Bedingungen)
                t_boiler_oben = await read_temperature_cached(SENSOR_IDS["oben"])
                t_boiler_unten = await read_temperature_cached(SENSOR_IDS["unten"])
                t_boiler_mittig = await read_temperature_cached(SENSOR_IDS["mittig"])
                t_verd = await read_temperature_cached(SENSOR_IDS["verd"])
                t_boiler = (
                    (t_boiler_oben + t_boiler_unten) / 2 if t_boiler_oben is not None and t_boiler_unten is not None else None
                )
                state.t_boiler = t_boiler

                # Initialisierung der Einschaltbedingungen
                temp_conditions_met_to_start = False
                solar_window_conditions_met_to_start = True

                # Debugging-Logs für Sensorwerte
                #logging.debug(f"Sensorwerte: T_Oben={t_boiler_oben if t_boiler_oben is not None else 'N/A'}°C, "
                #              f"T_Mittig={t_boiler_mittig if t_boiler_mittig is not None else 'N/A'}°C, "
                #              f"T_Unten={t_boiler_unten if t_boiler_unten is not None else 'N/A'}°C, "
                #              f"T_Verd={t_verd if t_verd is not None else 'N/A'}°C")

                # Sensorfehler prüfen
                sensor_ok = await check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten)
                if not sensor_ok:
                    logging.info("Kompressor bleibt aus wegen Sensorfehler")
                    state.ausschluss_grund = "Sensorfehler: Ungültige Werte"
                    if state.kompressor_ein:
                        await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                    continue

                # Sicherheitsabschaltung
                if t_boiler_oben is not None and t_boiler_unten is not None:
                    if t_boiler_oben >= state.sicherheits_temp or t_boiler_unten >= state.sicherheits_temp:
                        state.ausschluss_grund = f"Übertemperatur (>= {state.sicherheits_temp}°C)"
                        logging.error(
                            f"Sicherheitsabschaltung: T_Oben={t_boiler_oben:.1f}°C, T_Unten={t_boiler_unten:.1f}°C >= {state.sicherheits_temp}°C"
                        )
                        if state.kompressor_ein:
                            result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                            if result:
                                state.kompressor_ein = False
                                #set_last_compressor_off_time(state, now)
                                state.last_runtime = safe_timedelta(now, state.last_compressor_on_time, default=timedelta())
                                state.total_runtime_today += state.last_runtime
                                logging.info(f"Kompressor ausgeschaltet (Sicherheitsabschaltung). Laufzeit: {state.last_runtime}")
                                logging.debug(
                                    f"[Hauptschleife] Kompressor ausgeschaltet, last_compressor_off_time auf {state.last_compressor_off_time} gesetzt.")
                            else:
                                logging.critical("Kritischer Fehler: Kompressor konnte trotz Übertemperatur nicht ausgeschaltet werden!")
                                await send_telegram_message(
                                    session, state.chat_id,
                                    "🚨 KRITISCHER FEHLER: Kompressor bleibt trotz Übertemperatur eingeschaltet!",
                                    state.bot_token
                                )
                        if state.bot_token and state.chat_id:
                            await send_telegram_message(
                                session, state.chat_id,
                                f"⚠️ Sicherheitsabschaltung: T_Oben={t_boiler_oben:.1f}°C, T_Unten={t_boiler_unten:.1f}°C >= {state.sicherheits_temp}°C",
                                state.bot_token
                            )
                        await asyncio.sleep(2)
                        continue

                # Verdampfertemperatur prüfen
                VERDAMFER_NOTIFICATION_INTERVAL = timedelta(minutes=5)
                if t_verd is not None and t_verd < state.verdampfertemperatur:
                    state.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f}°C < {state.verdampfertemperatur}°C)"
                    logging.warning(state.ausschluss_grund)
                    if state.bot_token and state.chat_id and (
                            state.last_verdampfer_notification is None or
                            safe_timedelta(now, state.last_verdampfer_notification) > VERDAMFER_NOTIFICATION_INTERVAL):
                        await send_telegram_message(
                            session, state.chat_id,
                            f"⚠️ Kompressor bleibt aus oder wird ausgeschaltet: {state.ausschluss_grund}",
                            state.bot_token
                        )
                        state.last_verdampfer_notification = now
                    if state.kompressor_ein:
                        result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                        if result:
                            state.kompressor_ein = False
                            #set_last_compressor_off_time(state, now)
                            state.last_runtime = safe_timedelta(now, state.last_compressor_on_time, default=timedelta())
                            state.total_runtime_today += state.last_runtime
                            logging.info(f"Kompressor ausgeschaltet wegen zu niedriger Verdampfertemperatur. Laufzeit: {state.last_runtime}")
                        else:
                            logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
                            await send_telegram_message(
                                session, state.chat_id,
                                "🚨 KRITISCHER FEHLER: Kompressor bleibt trotz niedriger Verdampfertemperatur eingeschaltet!",
                                state.bot_token
                            )
                    await asyncio.sleep(2)
                    continue

                # Druckfehler prüfen
                if not await handle_pressure_check(session, state):
                    logging.info("Kompressor bleibt aus wegen Druckschalterfehler")
                    state.ausschluss_grund = "Druckschalterfehler"
                    if state.kompressor_ein:
                        await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                    await asyncio.sleep(2)
                    continue

                # Kompressorsteuerung: Einschaltprüfung
                pause_ok = True
                reason = None
                if not state.kompressor_ein and temp_conditions_met_to_start and solar_window_conditions_met_to_start:
                    time_since_off = safe_timedelta(now, state.last_compressor_off_time, default=timedelta.max)
                    logging.debug(f"Prüfe Mindestpause: time_since_off={time_since_off}, min_pause={state.min_pause}")
                    logging.debug(f"[Hauptschleife] now={now} (tzinfo={now.tzinfo}), "
                                  f"last_compressor_off_time={state.last_compressor_off_time} (tzinfo={state.last_compressor_off_time.tzinfo}), "
                                  f"time_since_off={time_since_off.total_seconds()}s, "
                                  f"min_pause={state.min_pause.total_seconds()}s")
                    if time_since_off.total_seconds() < state.min_pause.total_seconds() - 0.5:
                        pause_ok = False
                        pause_remaining = state.min_pause - time_since_off
                        reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"
                        COOLDOWN_SEKUNDEN = 300
                        same_reason = getattr(state, 'last_pause_reason', None) == reason
                        last_logged = getattr(state, 'last_pause_log', None)
                        enough_time_passed = last_logged is None or (
                                safe_timedelta(now, last_logged).total_seconds() > COOLDOWN_SEKUNDEN)
                        if not same_reason or enough_time_passed:
                            logging.info(f"Kompressor START VERHINDERT: {reason}")
                            if state.bot_token and state.chat_id:
                                await send_telegram_message(
                                    session, state.chat_id,
                                    f"⚠️ Kompressor bleibt aus: {reason}",
                                    state.bot_token
                                )
                                state.last_pause_telegram_notification = now
                            state.last_pause_reason = reason
                            state.last_pause_log = now
                        state.ausschluss_grund = reason
                    else:
                        state.last_pause_reason = None
                        state.last_pause_log = None
                        state.last_pause_telegram_notification = None
                        state.ausschluss_grund = None

                # Übergangsmodus aktiv?
                within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
                logging.debug(f"Übergangsmodus aktiv: {within_uebergangsmodus}")

                # Tageswechsel prüfen
                should_check_day = state.last_log_time is None or safe_timedelta(now, state.last_log_time) >= timedelta(minutes=1)
                if should_check_day and now.date() != state.last_day:
                    logging.info(f"Neuer Tag erkannt: {now.date()}. Setze Gesamtlaufzeit zurück.")
                    state.total_runtime_today = timedelta()
                    state.last_day = now.date()

                # Konfigurationsprüfung
                CONFIG_CHECK_INTERVAL = timedelta(seconds=60)
                if safe_timedelta(now, state._last_config_check) > CONFIG_CHECK_INTERVAL:
                    current_hash = calculate_file_hash("config.ini")
                    if current_hash != state.last_config_hash:
                        await reload_config(session, state)
                        state.last_config_hash = current_hash
                    state._last_config_check = now

                # Solax-Daten abrufen (aus Updater-Task gepuffert)
                solax_result = state.last_api_data or {
                    "solax_data": {},
                    "acpower": "N/A",
                    "feedinpower": "N/A",
                    "batPower": "N/A",
                    "soc": "N/A",
                    "powerdc1": "N/A",
                    "powerdc2": "N/A",
                    "consumeenergy": "N/A",
                    "api_fehler": True
                }

                # Sicherer Zugriff auf Unterstruktur
                solax_data = solax_result.get("solax_data", {})

                # Optional: Warnung, wenn leer oder ungültig
                if not solax_data or solax_data.get("api_fehler"):
                    logging.warning(
                        "Solax-Daten fehlen oder sind ungültig – evtl. noch keine Verbindung oder Fallback aktiv")

                # Power Source ermitteln
                power_source = get_power_source(solax_data) if solax_data else "Unbekannt"
                logging.debug(f"Power Source: {power_source}")

                # Optional: Prüfen, ob Daten zu alt
                if is_data_old(state.last_api_timestamp):
                    logging.warning("Solax-Daten zu alt – Solarüberschuss deaktiviert")
                    state.solar_ueberschuss_aktiv = False

                # Sollwerte berechnen
                try:
                    is_night = await asyncio.to_thread(is_nighttime, state.config)  # <- hier state.config nutzen
                    nacht_reduction = int(state.config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
                    ausschaltpunkt, einschaltpunkt, solar_ueberschuss_aktiv = await asyncio.to_thread(
                        calculate_shutdown_point, state.config, is_night, solax_data,
                        state)  # <- state.config übergeben
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

                # Prüfe GPIO-Zustand gegen Softwarestatus
                actual_gpio_state = GPIO.input(21)
                if state.kompressor_ein and actual_gpio_state == GPIO.LOW:
                    logging.critical(
                        "Inkonsistenz: state.kompressor_ein=True, aber GPIO 21 ist LOW! Zustand wird korrigiert.")
                    state.kompressor_ein = False
                    state.start_time = None
                    now_correct = now
                    set_last_compressor_off_time(state, now_correct)
                    state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                    state.total_runtime_today += state.last_runtime
                    await send_telegram_message(
                        session, state.chat_id,
                        "🚨 Inkonsistenz: Kompressorstatus korrigiert (war eingeschaltet, GPIO war LOW)!",
                        state.bot_token
                    )
                    reset_sensor_cache()

                elif not state.kompressor_ein and actual_gpio_state == GPIO.HIGH:
                    logging.critical("Inkonsistenz: state.kompressor_ein=False, aber GPIO 21 ist HIGH!")
                    result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                    if result:
                        now_correct = now
                        set_last_compressor_off_time(state, now_correct)
                        state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                        state.total_runtime_today += state.last_runtime
                        state.kompressor_ein = False
                        state.start_time = None
                        logging.info(
                            f"Kompressorstatus nach GPIO-Inkonsistenz korrigiert. Laufzeit: {state.last_runtime}")
                    else:
                        logging.critical("Kritischer Fehler: Konnte Kompressor nicht ausschalten!")
                        await send_telegram_message(
                            session, state.chat_id,
                            "🚨 KRITISCHER FEHLER: Kompressor bleibt trotz Inkonsistenz eingeschaltet!",
                            state.bot_token
                        )
                    reset_sensor_cache()

                # Kompressorsteuerung
                #temp_conditions_met_to_start = False
                #solar_window_conditions_met_to_start = True
                if state.solar_ueberschuss_aktiv:
                    if t_boiler_unten is not None:
                        temp_conditions_met_to_start = t_boiler_unten < 43.0
                        logging.debug(f"[Solarmodus] T_Unten={t_boiler_unten:.1f}°C, Einschaltpunkt=43.0°C")
                    else:
                        if t_boiler_mittig is not None:
                            temp_conditions_met_to_start = t_boiler_mittig < state.aktueller_einschaltpunkt
                            logging.debug(
                                f"[Normalmodus] T_Mittig={t_boiler_mittig:.1f}°C, Einschaltpunkt={state.aktueller_einschaltpunkt}°C")

                # Solar-Fenster prüfen
                if within_uebergangsmodus and power_source != "Direkter PV-Strom":
                    solar_window_conditions_met_to_start = False
                    state.ausschluss_grund = (
                        f"Warte auf direkten Solarstrom im Übergangsmodus "
                        f"({state.uebergangsmodus_start.strftime('%H:%M')}–{state.uebergangsmodus_ende.strftime('%H:%M')})"
                    )
                    logging.debug(state.ausschluss_grund)

                # Kompressor einschalten
                if not state.kompressor_ein and temp_conditions_met_to_start and pause_ok and solar_window_conditions_met_to_start:
                    logging.info("Alle Bedingungen für Kompressorstart erfüllt. Versuche einzuschalten.")
                    result = await set_kompressor_status(state, True, t_boiler_oben=t_boiler_oben)
                    if result:
                        state.kompressor_ein = True
                        now = datetime.now(local_tz)
                        state.start_time = now
                        state.last_compressor_on_time = now  # ✅ Einheitlich setzen
                        logging.info(f"Kompressor eingeschaltet. Startzeit: {now}")
                        state.ausschluss_grund = None
                    else:
                        state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler beim Einschalten"
                        logging.info(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")

                # --- [Abschaltbedingung je nach Modus] ---
                abschalten = False
                if state.solar_ueberschuss_aktiv:
                    # Solarmodus: Nur T_Unten prüfen gegen erhöhten Ausschaltpunkt
                    if t_boiler_unten is not None and t_boiler_unten >= state.aktueller_ausschaltpunkt:
                        abschalten = True
                        logging.info(
                            f"[Solarmodus] Abschaltbedingung erreicht: T_Unten={t_boiler_unten:.1f}°C >= {state.aktueller_ausschaltpunkt}°C")
                elif t_boiler_mittig is not None and t_boiler_mittig >= state.aktueller_ausschaltpunkt:
                    # Normalmodus: Mittlerer Sensor prüfen gegen normalen Ausschaltpunkt
                    abschalten = True
                    logging.info(
                        f"[Normalmodus] Abschaltbedingung erreicht: T_Mittig={t_boiler_mittig:.1f}°C >= {state.aktueller_ausschaltpunkt}°C")

                # --- [Kompressor ausschalten falls nötig] ---
                if abschalten and state.kompressor_ein:
                    # Nur ausschalten, wenn die Mindestlaufzeit erreicht ist, außer bei force=True
                    now = datetime.now(local_tz)

                    if state.start_time is not None:
                        elapsed_time = now - state.start_time
                        if elapsed_time >= state.min_laufzeit:
                            logging.debug(
                                f"Mindestlaufzeit ({state.min_laufzeit.total_seconds()}s) erreicht. Schalte Kompressor aus.")
                            result = await set_kompressor_status(state, False, force=False, t_boiler_oben=t_boiler_oben)
                        else:
                            logging.debug(
                                f"Mindestlaufzeit noch nicht erreicht. Verbleibend: {(state.min_laufzeit - elapsed_time).total_seconds():.1f}s")
                            result = True  # Keine Aktion nötig
                    else:
                        logging.warning("Startzeit des Kompressors unbekannt. Ausschaltung übersprungen.")
                        result = True  # Keine Aktion nötig

                    if result:
                        state.kompressor_ein = False
                        now_correct = now
                        set_last_compressor_off_time(state, now_correct)
                        state.last_runtime = safe_timedelta(now_correct, state.last_compressor_on_time)
                        state.total_runtime_today += state.last_runtime
                        logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
                    else:
                        logging.critical("Kritischer Fehler: Kompressor konnte nicht ausgeschaltet werden!")
                        await send_telegram_message(
                            session, state.chat_id,
                            "🚨 KRITISCHER FEHLER: Kompressor bleibt eingeschaltet!",
                            state.bot_token
                        )

                # Moduswechsel prüfen
                if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv:
                    effective_ausschaltpunkt = state.previous_ausschaltpunkt or state.aktueller_ausschaltpunkt
                    if not state.solar_ueberschuss_aktiv and t_boiler_oben is not None and t_boiler_mittig is not None:
                        if t_boiler_oben >= effective_ausschaltpunkt or t_boiler_mittig >= effective_ausschaltpunkt:
                            result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                            if result:
                                state.kompressor_ein = False
                                #set_last_compressor_off_time(state, now)
                                state.last_runtime = safe_timedelta(now, state.last_compressor_on_time, default=timedelta())
                                state.total_runtime_today += state.last_runtime
                                logging.info(f"Kompressor ausgeschaltet bei Moduswechsel. Laufzeit: {state.last_runtime}")
                                state.ausschluss_grund = None
                            else:
                                logging.critical("Kritischer Fehler: Kompressor konnte bei Moduswechsel nicht ausgeschaltet werden!")
                                await send_telegram_message(
                                    session, state.chat_id,
                                    "🚨 KRITISCHER FEHLER: Kompressor bleibt bei Moduswechsel eingeschaltet!",
                                    state.bot_token
                                )

                # Laufzeit aktualisieren
                if state.kompressor_ein:
                    state.current_runtime = safe_timedelta(now, state.last_compressor_on_time, default=timedelta())
                else:
                    state.current_runtime = timedelta()

                # CSV-Protokollierung
                await log_to_csv(state, now, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, solax_data,
                                 state.aktueller_einschaltpunkt, state.aktueller_ausschaltpunkt,
                                 state.solar_ueberschuss_aktiv, nacht_reduction, power_source)

                # Watchdog
                cycle_duration = safe_timedelta(now, last_cycle_time, default=timedelta()).total_seconds()
                if cycle_duration > 30:
                    watchdog_warning_count += 1
                    logging.error(f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {watchdog_warning_count}/{WATCHDOG_MAX_WARNINGS}")
                    if watchdog_warning_count >= WATCHDOG_MAX_WARNINGS:
                        result = await set_kompressor_status(state, False, force=True, t_boiler_oben=t_boiler_oben)
                        await send_telegram_message(
                            session, state.chat_id,
                            "🚨 Watchdog-Fehler: Programm beendet.", state.bot_token
                        )
                        await shutdown(session, state)
                        raise SystemExit("Watchdog-Exit")
                else:
                    watchdog_warning_count = 0

                last_cycle_time = now
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Fehler in der Hauptschleife: {e}", exc_info=True)
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        if 'telegram_task_handle' in locals():
            telegram_task_handle.cancel()
        await asyncio.gather(
            telegram_task_handle if 'telegram_task_handle' in locals() else asyncio.sleep(0),
            return_exceptions=True
        )
        raise

    finally:
        await shutdown(session, state)


async def run_program():
    async with aiohttp.ClientSession() as session:
        config = configparser.ConfigParser()
        try:
            config.read("config.ini")
            if not config.sections():
                raise ValueError("Konfiguration konnte nicht geladen werden")
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfiguration: {e}", exc_info=True)
            raise

        state = State(config)

        # CSV-Initialisierung
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
            await setup_logging(session, state)
            await main_loop(config, state, session)
        except KeyboardInterrupt:
            logging.info("Programm durch Benutzer abgebrochen (Ctrl+C).")
        except asyncio.CancelledError:
            logging.info("Hauptschleife abgebrochen.")
        except Exception as e:
            logging.error(f"Unerwarteter Fehler in run_program: {e}", exc_info=True)
            raise
        finally:
            await shutdown(session, state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)  # Fallback-Logging vor setup_logging
    try:
        asyncio.run(run_program())
    except Exception as e:
        logging.error(f"Fehler beim Starten des Skripts: {e}", exc_info=True)
        raise