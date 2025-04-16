import os
import sys
import smbus2
from datetime import datetime, timedelta
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
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
from dateutil.relativedelta import relativedelta
from telegram_handler import (send_telegram_message, send_welcome_message, telegram_task)


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
BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]
CHAT_ID = config["Telegram"]["CHAT_ID"]
AUSSCHALTPUNKT = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45))
AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 52))
EINSCHALTPUNKT = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42))
TEMP_OFFSET = int(config["Heizungssteuerung"].get("TEMP_OFFSET", 3))
VERDAMPFERTEMPERATUR = int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"])
MIN_LAUFZEIT = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))
MIN_PAUSE = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))
UNTERER_FUEHLER_MIN = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MIN", 45))
UNTERER_FUEHLER_MAX = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MAX", 50))
TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
SN = config["SolaxCloud"]["SN"]


# Globale Variablen für den Programmstatus
last_update_id = None
lcd = None
csv_lock = asyncio.Lock()


PRESSURE_ERROR_DELAY = timedelta(minutes=5)  # 5 Minuten Verzögerung




# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logging.info(f"Programm gestartet: {datetime.now()}")

# Neuer Telegram-Handler für Logging
class TelegramHandler(logging.Handler):
    def __init__(self, bot_token, chat_id, session):
        super().__init__()
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.session = session
        self.setLevel(logging.WARNING)  # Nur Warnings und Errors senden

    async def send_telegram(self, message):
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message[:4096]}  # Telegram-Nachrichtenlänge begrenzen
        try:
            async with self.session.post(url, json=data) as response:
                response.raise_for_status()
                logging.debug(f"Telegram-Nachricht gesendet: {message}")
        except aiohttp.ClientError as e:
            logging.error(f"Fehler beim Senden an Telegram: {e}, Nachricht: {message}")  # Ins Log schreiben

    def emit(self, record):
        try:
            msg = self.format(record)
            task = asyncio.create_task(self.send_telegram(msg))
            task.add_done_callback(lambda t: logging.debug(f"Telegram-Task abgeschlossen: {t.result()}"))
        except Exception as e:
            logging.error(f"Fehler in TelegramHandler.emit: {e}", exc_info=True)

class State:
    def __init__(self, config):
        self.current_runtime = timedelta()
        self.ausschluss_grund = None
        self.last_config_hash = calculate_file_hash("config.ini")
        self.kompressor_ein = False
        self.total_runtime_today = timedelta()
        self.last_day = datetime.now().date()
        self.last_shutdown_time = datetime.now()
        self.last_log_time = datetime.now() - timedelta(minutes=1)
        self.last_kompressor_status = None
        self.urlaubsmodus_aktiv = False
        self.solar_ueberschuss_aktiv = False
        self.last_runtime = timedelta()
        self.aktueller_ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45))
        self.aktueller_einschaltpunkt = self.aktueller_ausschaltpunkt - int(config["Heizungssteuerung"].get("TEMP_OFFSET", 3))
        self.pressure_error_sent = False
        self.last_pressure_error_time = None
        self.t_boiler = None
        self.start_time = None
        self.last_pressure_state = None
        # Neue Attribute
        self.bot_token = config["Telegram"]["BOT_TOKEN"]
        self.chat_id = config["Telegram"]["CHAT_ID"]
        self.token_id = config["SolaxCloud"]["TOKEN_ID"]
        self.sn = config["SolaxCloud"]["SN"]
        self.min_laufzeit = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_LAUFZEIT", 10)))
        self.min_pause = timedelta(minutes=int(config["Heizungssteuerung"].get("MIN_PAUSE", 20)))
        self.verdampfertemperatur = int(config["Heizungssteuerung"].get("VERDAMPFERTEMPERATUR", 6))
        self.last_api_call = None
        self.last_api_data = None
        self.last_api_timestamp = None

# Logging einrichten mit Telegram-Handler
async def setup_logging(session):
    logging.basicConfig(
        filename="heizungssteuerung.log",
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    telegram_handler = TelegramHandler(BOT_TOKEN, CHAT_ID, session)
    telegram_handler.setLevel(logging.WARNING)
    telegram_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(telegram_handler)
    logging.info("Logging mit Telegram-Handler initialisiert")
    # Testnachricht
    logging.error("Test: Telegram-Handler initialisiert")  # Sollte per Telegram gesendet werden

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
    now = datetime.now()
    if state.last_api_call and now - state.last_api_call < timedelta(minutes=5):
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
                    logging.info(f"Solax-Daten erfolgreich abgerufen: {state.last_api_data}")
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


def get_power_source(solax_data):
    pv_production = solax_data.get("powerdc1", 0) + solax_data.get("powerdc2", 0)
    bat_power = solax_data.get("batPower", 0)
    feedin_power = solax_data.get("feedinpower", 0)
    consumption = solax_data.get("consumeenergy", 0)

    if pv_production > 0 and (bat_power >= 0 or feedin_power > 0):
        return "Direkter PV-Strom"
    elif bat_power < 0 and feedin_power >= 0 and pv_production <= consumption:
        return "Strom aus der Batterie"
    elif feedin_power < 0:
        return "Strom vom Netz"
    else:
        return "Unbekannt"  # Fallback für edge cases wie batPower = 0, feedinpower = 0


def calculate_runtimes():
    try:
        # Lese die CSV-Datei
        df = pd.read_csv("heizungsdaten.csv", on_bad_lines="skip", parse_dates=["Zeitstempel"])

        # Aktuelles Datum
        now = datetime.now()

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


async def send_runtimes_telegram(session):
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
        await send_telegram_message(session, CHAT_ID, message)
    else:
        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen der Laufzeiten.")


async def send_unknown_command_message(session, chat_id):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    message = (
        "❌ Unbekannter Befehl.\n\n"
        "Verwende die Tastatur, um einen gültigen Befehl auszuwählen."
    )
    return await send_telegram_message(session, chat_id, message, reply_markup=get_custom_keyboard())

async def is_nighttime(config):
    """Prüft, ob es Nacht ist basierend auf der Konfiguration."""
    now = datetime.now()
    night_start = int(config["Heizungssteuerung"].get("NACHT_START", 22))
    night_end = int(config["Heizungssteuerung"].get("NACHT_ENDE", 6))
    return now.hour >= night_start or now.hour < night_end

# test.py (angepasste shutdown-Funktion)
async def shutdown(session):
    """Führt die Abschaltprozedur durch und informiert über Telegram."""
    try:
        # Nur GPIO.output aufrufen, wenn GPIO noch initialisiert ist
        if GPIO.getmode() is not None:  # Prüft, ob ein Modus (BCM oder BOARD) gesetzt ist
            GPIO.output(GIO21_PIN, GPIO.LOW)
            logging.info("Kompressor GPIO auf LOW gesetzt")
        else:
            logging.warning("GPIO-Modus nicht gesetzt, überspringe GPIO.output")

        message = f"🛑 Programm beendet um {datetime.now().strftime('%d.%m.%Y um %H:%M:%S')}"
        await send_telegram_message(session, CHAT_ID, message, BOT_TOKEN)

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

    except Exception as e:
        logging.error(f"Fehler beim Herunterfahren: {e}", exc_info=True)
    finally:
        logging.info("System heruntergefahren")

async def run_program():
    async with aiohttp.ClientSession() as session:
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
            await main_loop(session)
        except KeyboardInterrupt:
            logging.info("Programm durch Benutzer beendet.")
        finally:
            await shutdown(session)


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
            # Prüfe, ob der Sensor korrekt gelesen wurde (CRC-Check)
            if lines[0].strip()[-3:] == "YES":
                # Extrahiere die Temperatur aus der zweiten Zeile (in Milligrad Celsius)
                temp_data = lines[1].split("=")[-1]
                temp = float(temp_data) / 1000.0
                # Plausibilitätsprüfung: Temperaturen außerhalb -20°C bis 100°C sind unwahrscheinlich
                if temp < -20 or temp > 100:
                    logging.error(
                        f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C. Sensor als fehlerhaft betrachtet.")
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
        logging.error(f"Fehler beim Lesen des Sensors {sensor_id}: {e}")
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


def check_boiler_sensors(t_oben, t_unten, config):
    """Prüft die Boiler-Sensoren auf Fehler."""
    try:
        ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
    except (KeyError, ValueError):
        ausschaltpunkt = 50
        logging.warning(f"Ausschaltpunkt nicht gefunden, verwende Standard: {ausschaltpunkt}")
    fehler = None
    is_overtemp = False
    if t_oben is None or t_unten is None:
        fehler = "Fühlerfehler!"
        logging.error(f"Fühlerfehler erkannt: oben={t_oben}, unten={t_unten}")
    elif t_oben >= (ausschaltpunkt + 10) or t_unten >= (ausschaltpunkt + 10):
        fehler = "Übertemperatur!"
        is_overtemp = True
        logging.error(f"Übertemperatur erkannt: oben={t_oben}, unten={t_unten}, Grenze={ausschaltpunkt + 10}")
    elif abs(t_oben - t_unten) > 50:
        fehler = "Fühlerdifferenz!"
        logging.warning(
            f"Fühlerdifferenz erkannt: oben={t_oben}, unten={t_unten}, Differenz={abs(t_oben - t_unten)}")
    return fehler, is_overtemp


def set_kompressor_status(state, ein, force_off=False):
    """Setzt den Status des Kompressors (EIN/AUS) und überprüft den GPIO-Pin."""
    now = datetime.now()
    if ein:
        if not state.kompressor_ein:
            pause_time = now - state.last_shutdown_time
            if pause_time < state.min_pause and not force_off:
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {state.min_pause})")
                state.ausschluss_grund = f"Zu kurze Pause ({pause_time.total_seconds():.1f}s < {state.min_pause.total_seconds():.1f}s)"
                return False
            state.kompressor_ein = True
            state.start_time = now
            state.current_runtime = timedelta()
            state.ausschluss_grund = None  # Kein Ausschlussgrund, wenn Kompressor läuft
            logging.info(f"Kompressor EIN geschaltet. Startzeit: {state.start_time}")
        else:
            state.current_runtime = now - state.start_time
            logging.debug(f"Kompressor läuft bereits, aktuelle Laufzeit: {state.current_runtime}")
    else:
        if state.kompressor_ein:
            elapsed_time = now - state.start_time
            if elapsed_time < state.min_laufzeit and not force_off:
                logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}, benötigt: {state.min_laufzeit})")
                return True
            state.kompressor_ein = False
            state.current_runtime = elapsed_time
            state.total_runtime_today += state.current_runtime
            state.last_runtime = state.current_runtime
            state.last_shutdown_time = now
            state.start_time = None
            logging.info(
                f"Kompressor AUS geschaltet. Laufzeit: {elapsed_time}, Gesamtlaufzeit heute: {state.total_runtime_today}")
        else:
            logging.debug("Kompressor bereits ausgeschaltet")
    # GPIO-Status setzen und prüfen
    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)
    actual_state = GPIO.input(GIO21_PIN)  # Annahme: Pin kann als Eingang gelesen werden
    if actual_state != (GPIO.HIGH if ein else GPIO.LOW):
        logging.error(f"GPIO-Fehler: Kompressor-Status sollte {'EIN' if ein else 'AUS'} sein, ist aber {actual_state}")
    return None


# Asynchrone Funktion zum Neuladen der Konfiguration
async def reload_config(session, state, config):
    """Lädt die Konfigurationsdatei neu und aktualisiert die Parameter."""
    try:
        logging.info("Lade Konfigurationsdatei neu...")
        new_config = configparser.ConfigParser()
        new_config.read("config.ini")

        # Validierte Konfiguration
        validated_config = {
            "Heizungssteuerung": {
                "AUSSCHALTPUNKT": new_config.getint("Heizungssteuerung", "AUSSCHALTPUNKT", fallback=45),
                "EINSCHALTPUNKT": new_config.getint("Heizungssteuerung", "EINSCHALTPUNKT", fallback=42),
                "AUSSCHALTPUNKT_ERHOEHT": new_config.getint("Heizungssteuerung", "AUSSCHALTPUNKT_ERHOEHT", fallback=50),
                "EINSCHALTPUNKT_ERHOEHT": new_config.getint("Heizungssteuerung", "EINSCHALTPUNKT_ERHOEHT", fallback=46),
                "NACHTABSENKUNG": new_config.getint("Heizungssteuerung", "NACHTABSENKUNG", fallback=0),
                "VERDAMPFERTEMPERATUR": new_config.getint("Heizungssteuerung", "VERDAMPFERTEMPERATUR", fallback=5),
                "MIN_PAUSE": new_config.getint("Heizungssteuerung", "MIN_PAUSE", fallback=5),
            },
            "Urlaubsmodus": {
                "URLAUBSABSENKUNG": new_config.getint("Urlaubsmodus", "URLAUBSABSENKUNG", fallback=0)
            },
            "Telegram": {
                "CHAT_ID": new_config.get("Telegram", "CHAT_ID", fallback=""),
                "TOKEN": new_config.get("Telegram", "TOKEN", fallback="")
            }
        }

        # Aktualisiere config
        config.clear()
        config.update(validated_config)

        # Aktualisiere state.bot_token
        state.bot_token = validated_config["Telegram"]["TOKEN"]
        if not state.bot_token:
            logging.warning("Kein gültiger Telegram-Token in der Konfiguration gefunden.")

        # Solax-Daten für calculate_shutdown_point
        solax_data = await get_solax_data(session, state) or {
            "acpower": 0, "feedinpower": 0, "consumeenergy": 0,
            "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
            "api_fehler": True
        }

        # Aktualisiere Sollwerte
        state.aktueller_ausschaltpunkt, state.aktueller_einschaltpunkt = calculate_shutdown_point(
            validated_config, await asyncio.to_thread(is_nighttime, validated_config), solax_data, state
        )

        logging.info("Konfiguration erfolgreich neu geladen.")
        await send_telegram_message(session, validated_config["Telegram"]["CHAT_ID"],
                                  "🔧 Konfigurationsdatei wurde geändert.", state.bot_token)
    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}", exc_info=True)
        await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                                  f"⚠️ Fehler beim Neuladen der Konfiguration: {str(e)}", state.bot_token)


# Funktion zum Anpassen der Sollwerte (synchron, wird in Thread ausgeführt)
def adjust_shutdown_and_start_points(solax_data, config, state):
    """
    Passt die Sollwerte (Ausschaltpunkt und Einschaltpunkt) basierend auf dem aktuellen Modus und den Solax-Daten an.
    Verwendet das State-Objekt zur Verwaltung der Zustände.
    """
    # Initialisiere statische Attribute, falls noch nicht vorhanden
    if not hasattr(adjust_shutdown_and_start_points, "last_night"):
        adjust_shutdown_and_start_points.last_night = None
        adjust_shutdown_and_start_points.last_config_hash = None
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = None
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = None

    # Prüfe, ob Nachtzeit vorliegt
    is_night = is_nighttime(config)
    current_config_hash = calculate_file_hash("config.ini")

    # Wenn sich weder die Nachtzeit noch die Konfiguration geändert hat, breche ab
    if (is_night == adjust_shutdown_and_start_points.last_night and
            current_config_hash == adjust_shutdown_and_start_points.last_config_hash):
        return

    # Aktualisiere statische Attribute
    adjust_shutdown_and_start_points.last_night = is_night
    adjust_shutdown_and_start_points.last_config_hash = current_config_hash

    # Speichere alte Sollwerte für Logging
    old_ausschaltpunkt = state.aktueller_ausschaltpunkt
    old_einschaltpunkt = state.aktueller_einschaltpunkt

    # Berechne neue Sollwerte
    state.aktueller_ausschaltpunkt, state.aktueller_einschaltpunkt = calculate_shutdown_point(
        config, is_night, solax_data, state
    )

    # Mindestwert für Einschaltpunkt prüfen
    MIN_EINSCHALTPUNKT = 20
    if state.aktueller_einschaltpunkt < MIN_EINSCHALTPUNKT:
        state.aktueller_einschaltpunkt = MIN_EINSCHALTPUNKT
        logging.warning(f"Einschaltpunkt auf Mindestwert {MIN_EINSCHALTPUNKT} gesetzt.")

    # Logge Änderungen der Sollwerte, falls sie sich geändert haben
    if (state.aktueller_ausschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt or
            state.aktueller_einschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt):
        logging.info(
            f"Sollwerte angepasst: Ausschaltpunkt={old_ausschaltpunkt} -> {state.aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {state.aktueller_einschaltpunkt}, "
            f"Solarüberschuss_aktiv={state.solar_ueberschuss_aktiv}"
        )
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = state.aktueller_ausschaltpunkt
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = state.aktueller_einschaltpunkt


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


def load_config():
    """Lädt die Konfigurationsdatei synchron."""
    config = configparser.ConfigParser()
    config.read("config.ini")
    logging.debug(f"Konfiguration geladen: {dict(config['Heizungssteuerung'])}")
    return config


def validate_config(config):
    """Validiert die Konfigurationswerte und setzt Fallbacks bei Fehlern."""
    defaults = {
        "Heizungssteuerung": {
            "AUSSCHALTPUNKT": "50",
            "AUSSCHALTPUNKT_ERHOEHT": "55",
            "TEMP_OFFSET": "10",  # Neuer Standardwert für Offset (z.B. 10°C)
            "VERDAMPFERTEMPERATUR": "25",
            "MIN_LAUFZEIT": "10",
            "MIN_PAUSE": "20",
            "NACHTABSENKUNG": "0"
        },
        "Telegram": {"state.bot_token": "", "CHAT_ID": ""},
        "SolaxCloud": {"TOKEN_ID": "", "SN": ""}
    }
    for section in defaults:
        if section not in config:
            config[section] = {}
            logging.warning(f"Abschnitt {section} fehlt in config.ini, wird mit Standardwerten erstellt.")
        for key, default in defaults[section].items():
            try:
                if key in config[section]:
                    if key not in ["state.bot_token", "CHAT_ID", "TOKEN_ID", "SN"]:
                        value = int(config[section][key])
                        min_val = 0 if key not in ["AUSSCHALTPUNKT", "AUSSCHALTPUNKT_ERHOEHT"] else 20
                        max_val = 100 if key not in ["MIN_LAUFZEIT", "MIN_PAUSE"] else 60
                        if not (min_val <= value <= max_val):
                            logging.warning(
                                f"Ungültiger Wert für {key} in {section}: {value}. Verwende Standardwert: {default}")
                            config[section][key] = default
                        else:
                            config[section][key] = str(value)
                    else:
                        config[section][key] = config[section][key]
                else:
                    config[section][key] = default
                    logging.warning(f"Schlüssel {key} in {section} fehlt, verwende Standardwert: {default}")
            except ValueError as e:
                config[section][key] = default
                logging.error(f"Ungültiger Wert für {key} in {section}: {e}, verwende Standardwert: {default}")
    logging.debug(f"Validierte Konfiguration: {dict(config['Heizungssteuerung'])}")
    return config


def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    now = datetime.now()
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
    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
    urlaubs_reduction = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 0)) if state.urlaubsmodus_aktiv else 0
    total_reduction = nacht_reduction + urlaubs_reduction
    bat_power = solax_data.get("batPower", 0)
    feedin_power = solax_data.get("feedinpower", 0)
    soc = solax_data.get("soc", 0)

    # Solarüberschuss-Logik
    was_active = state.solar_ueberschuss_aktiv
    state.solar_ueberschuss_aktiv = bat_power > 600 or (soc > 95 and feedin_power > 600)

    if state.solar_ueberschuss_aktiv and not was_active:
        logging.info(f"Solarüberschuss aktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")
    elif was_active and not state.solar_ueberschuss_aktiv:
        logging.info(f"Solarüberschuss deaktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")

    if state.solar_ueberschuss_aktiv:
        ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 50)) - total_reduction
        einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 46)) - total_reduction
    else:
        ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45)) - total_reduction
        einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 42)) - total_reduction

    logging.debug(f"Sollwerte: Ausschaltpunkt={ausschaltpunkt}, Einschaltpunkt={einschaltpunkt}, Nachtabsenkung={nacht_reduction}, Urlaubsabsenkung={urlaubs_reduction}, Solarüberschuss={state.solar_ueberschuss_aktiv}")
    return ausschaltpunkt, einschaltpunkt


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
    is_old = timestamp and (datetime.now() - timestamp) > timedelta(minutes=15)
    logging.debug(f"Prüfe Solax-Datenalter: Zeitstempel={timestamp}, Ist alt={is_old}")
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
                t_boiler_oben = await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"])
                t_boiler_unten = await asyncio.to_thread(read_temperature, SENSOR_IDS["unten"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
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

async def get_runtime_bar_chart(session, days=7, state=None):
    """Erstellt ein Balkendiagramm der Kompressorlaufzeiten für die letzten 'days' Tage."""
    if state is None:
        logging.error("State-Objekt nicht übergeben, kann Telegram-Nachricht nicht senden.")
        return

    try:
        today = datetime.now().date()
        start_date = today - timedelta(days=days - 1)
        runtime_data = []
        dates = []

        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            lines = await csvfile.readlines()
            lines = lines[1:]  # Header überspringen

            for line in lines:
                parts = line.strip().split(',')
                if len(parts) >= 18:  # Mindestens bis nacht_reduction
                    timestamp_str = parts[0].strip()
                    try:
                        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        date = timestamp.date()
                        kompressor_status = parts[6].strip()
                        # Laufzeit nur berechnen, wenn Kompressor EIN war
                        runtime_str = "0:00:00"  # Standard, wenn AUS
                        if kompressor_status == "EIN":
                            # Hier könnten wir die tatsächliche Laufzeit berechnen, z. B. Zeitdifferenz
                            # Für jetzt: Annahme 1 Minute pro EIN-Eintrag (anpassen nach Bedarf)
                            runtime_str = "0:01:00"
                        if date >= start_date and date <= today:
                            if date not in dates:
                                dates.append(date)
                                runtime_data.append(timedelta())
                            runtime_index = dates.index(date)
                            h, m, s = map(int, runtime_str.split(':'))
                            runtime_data[runtime_index] += timedelta(hours=h, minutes=m, seconds=s)
                    except (ValueError, IndexError) as e:
                        logging.warning(f"Fehler beim Parsen der Zeile: {line.strip()}, Fehler: {e}")
                        continue

        if not dates:
            logging.warning("Keine Laufzeitdaten für die angegebenen Tage gefunden.")
            await send_telegram_message(session, CHAT_ID, "Keine Laufzeitdaten verfügbar.", state.bot_token)
            return

        dates = sorted(dates)
        runtime_hours = [td.total_seconds() / 3600 for td in runtime_data]

        plt.figure(figsize=(10, 6))
        plt.bar(dates, runtime_hours, color='skyblue')
        plt.xlabel("Datum")
        plt.ylabel("Laufzeit (Stunden)")
        plt.title(f"Kompressorlaufzeiten (letzte {days} Tage)")
        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", CHAT_ID)
        form.add_field("caption", f"📊 Kompressorlaufzeiten (letzte {days} Tage)")
        form.add_field("photo", buf, filename="runtime_chart.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Laufzeitdiagramm für {days} Tage gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Laufzeitdiagramms: {str(e)}")
        await send_telegram_message(session, CHAT_ID, f"Fehler beim Abrufen der Laufzeiten: {str(e)}", state.bot_token)

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


async def get_boiler_temperature_history(session, hours, state, config):
    logging.debug(f"get_boiler_temperature_history aufgerufen mit hours={hours}, state.bot_token={state.bot_token}")
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    try:
        # Zeitfenster definieren
        now = datetime.now()
        time_ago = now - timedelta(hours=hours)

        # Verfügbare Spalten dynamisch ermitteln
        expected_columns = [
            "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "Kompressor",
            "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "PowerSource"
        ]
        try:
            # Lade CSV ohne usecols, um Header zu prüfen
            df = pd.read_csv("heizungsdaten.csv", parse_dates=["Zeitstempel"], nrows=1)
            available_columns = [col for col in expected_columns if col in df.columns]
            if not available_columns:
                raise ValueError("Keine der erwarteten Spalten in der CSV gefunden.")

            # Lade CSV mit verfügbaren Spalten
            df = pd.read_csv(
                "heizungsdaten.csv",
                parse_dates=["Zeitstempel"],
                usecols=available_columns
            )
            # Filtere Daten im gewünschten Zeitfenster
            df = df[df["Zeitstempel"] >= time_ago]
        except Exception as e:
            logging.error(f"Fehler beim Laden der CSV: {e}")
            await send_telegram_message(session, state.chat_id, f"Fehler beim Laden der Daten: {str(e)}", state.bot_token)
            return

        if df.empty:
            logging.warning(f"Keine Daten im Zeitfenster ({hours}h) gefunden.")
            await send_telegram_message(session, state.chat_id, "Keine Daten für den Verlauf verfügbar.", state.bot_token)
            return

        # Fehlerbehandlung für Temperaturen
        temp_columns = [col for col in ["T_Oben", "T_Unten", "T_Mittig"] if col in df.columns]
        if not temp_columns:
            logging.error("Keine Temperaturspalten (T_Oben, T_Unten, T_Mittig) verfügbar.")
            await send_telegram_message(session, state.chat_id, "Keine Temperaturdaten verfügbar.", state.bot_token)
            return

        df = df.dropna(subset=temp_columns)  # Entferne Zeilen mit fehlenden Temperaturen
        for col in temp_columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=temp_columns)  # Entferne Zeilen mit ungültigen Temperaturen

        # Fehlerbehandlung für andere Spalten
        if "Einschaltpunkt" in df.columns:
            df["Einschaltpunkt"] = pd.to_numeric(df["Einschaltpunkt"], errors="coerce").fillna(42)
        else:
            df["Einschaltpunkt"] = 42
            logging.warning("Spalte 'Einschaltpunkt' fehlt, verwende Standardwert 42.")

        if "Ausschaltpunkt" in df.columns:
            df["Ausschaltpunkt"] = pd.to_numeric(df["Ausschaltpunkt"], errors="coerce").fillna(45)
        else:
            df["Ausschaltpunkt"] = 45
            logging.warning("Spalte 'Ausschaltpunkt' fehlt, verwende Standardwert 45.")

        if "Solarüberschuss" in df.columns:
            df["Solarüberschuss"] = pd.to_numeric(df["Solarüberschuss"], errors="coerce").fillna(0).astype(int)
        else:
            df["Solarüberschuss"] = 0
            logging.warning("Spalte 'Solarüberschuss' fehlt, verwende Standardwert 0.")

        if "PowerSource" in df.columns:
            df["PowerSource"] = df["PowerSource"].fillna("Unbekannt").replace(["N/A", "Fehler"], "Unbekannt")
        else:
            df["PowerSource"] = "Unbekannt"
            logging.warning("Spalte 'PowerSource' fehlt, verwende Standardwert 'Unbekannt'.")

        if "Kompressor" in df.columns:
            df["Kompressor"] = df["Kompressor"].replace({"EIN": 1, "AUS": 0}).fillna(0)
        else:
            df["Kompressor"] = 0
            logging.warning("Spalte 'Kompressor' fehlt, verwende Standardwert 0.")

        # Sampling für gleichmäßige Datenpunkte
        target_points = 50
        if len(df) > target_points:
            df = df.iloc[::len(df) // target_points].head(target_points)

        # Daten für Plot vorbereiten
        timestamps = df["Zeitstempel"]
        t_oben = df["T_Oben"] if "T_Oben" in df.columns else None
        t_unten = df["T_Unten"] if "T_Unten" in df.columns else None
        t_mittig = df["T_Mittig"] if "T_Mittig" in df.columns else None
        einschaltpunkte = df["Einschaltpunkt"]
        ausschaltpunkte = df["Ausschaltpunkt"]
        kompressor_status = df["Kompressor"]
        power_sources = df["PowerSource"]
        solar_ueberschuss = df["Solarüberschuss"]

        # Diagramm erstellen
        plt.figure(figsize=(12, 6))
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "Unbekannt": "gray"
        }

        # Kompressorstatus als Hintergrund
        untere_grenze = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MIN", 20))
        obere_grenze = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 55))
        for source in color_map:
            mask = (power_sources == source) & (kompressor_status == 1)
            if mask.any():
                plt.fill_between(timestamps[mask], 0, max(untere_grenze, obere_grenze) + 5,
                                 color=color_map[source], alpha=0.2, label=f"Kompressor EIN ({source})")

        # Temperaturen plotten, falls verfügbar
        if t_oben is not None:
            plt.plot(timestamps, t_oben, label="T_Oben", marker="o", color="blue")
        if t_unten is not None:
            plt.plot(timestamps, t_unten, label="T_Unten", marker="x", color="red")
        if t_mittig is not None:
            plt.plot(timestamps, t_mittig, label="T_Mittig", marker="^", color="purple")

        # Sollwerte plotten
        plt.plot(timestamps, einschaltpunkte, label="Einschaltpunkt (historisch)", linestyle="--", color="green")
        plt.plot(timestamps, ausschaltpunkte, label="Ausschaltpunkt (historisch)", linestyle="--", color="orange")

        # Aktuelle Sollwerte, wenn Solarüberschuss aktiv
        if solar_ueberschuss.any():
            plt.axhline(y=state.aktueller_einschaltpunkt, color="purple", linestyle="-.",
                        label=f"Einschaltpunkt ({state.aktueller_einschaltpunkt}°C)")
            plt.axhline(y=state.aktueller_ausschaltpunkt, color="cyan", linestyle="-.",
                        label=f"Ausschaltpunkt ({state.aktueller_ausschaltpunkt}°C)")

        plt.xlim(time_ago, now)
        plt.ylim(0, max(untere_grenze, obere_grenze) + 5)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf (letzte {hours} Stunden)")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left")
        plt.tight_layout()

        # Diagramm speichern und senden
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        form.add_field("caption",
                       f"📈 Verlauf {hours}h (T_Oben = blau, T_Unten = rot, T_Mittig = lila, Kompressor EIN: grün=PV, gelb=Batterie, rot=Netz)")
        form.add_field("photo", buf, filename="temperature_graph.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Temperaturdiagramm für {hours}h gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Temperaturverlaufs: {e}")
        await send_telegram_message(session, state.chat_id, f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}",
                                    state.bot_token)

# Asynchrone Hauptschleife
async def main_loop(session, config, state):
    """Hauptschleife des Programms mit State-Objekt."""
    if not await initialize_gpio():
        logging.critical("Programm wird aufgrund fehlender GPIO-Initialisierung beendet.")
        exit(1)
    await initialize_lcd(session)
    now = datetime.now()
    await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                               f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}",
                               state.bot_token)
    await send_welcome_message(session, config["Telegram"]["CHAT_ID"], state.bot_token)

    try:
        telegram_task_handle = asyncio.create_task(
            telegram_task(session, state.bot_token, config["Telegram"]["CHAT_ID"], read_temperature, SENSOR_IDS,
                          lambda: state.kompressor_ein,
                          lambda: str(state.current_runtime).split('.')[0],
                          lambda: str(state.total_runtime_today).split('.')[0],
                          config, get_solax_data, state,
                          get_boiler_temperature_history, get_runtime_bar_chart,
                          lambda cfg=config: is_nighttime(cfg))
        )
    except TypeError as e:
        logging.error(f"TypeError beim Starten von telegram_task: {e}", exc_info=True)
        raise

    display_task_handle = asyncio.create_task(display_task(state))  # Übergabe von state
    last_cycle_time = datetime.now()
    last_compressor_off_time = None  # Zeitpunkt des letzten Ausschaltens
    watchdog_warning_count = 0
    WATCHDOG_MAX_WARNINGS = 3
    PRESSURE_ERROR_DELAY = timedelta(minutes=5)

    try:
        while True:
            try:
                now = datetime.now()
                should_check_day = (state.last_log_time is None or
                                    (now - state.last_log_time) >= timedelta(minutes=1))
                if should_check_day and now.date() != state.last_day:
                    logging.info(f"Neuer Tag erkannt: {now.date()}. Setze Gesamtlaufzeit zurück.")
                    state.total_runtime_today = timedelta()
                    state.last_day = now.date()

                current_hash = calculate_file_hash("config.ini")
                if state.last_config_hash != current_hash:
                    await reload_config(session, state, config)
                    state.last_config_hash = current_hash

                try:
                    solax_data = await get_solax_data(session, state) or {
                        "acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                        "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                        "api_fehler": True
                    }
                    logging.debug(f"Solax-Daten: {solax_data}")
                    acpower = solax_data.get("acpower", "N/A")
                    feedinpower = solax_data.get("feedinpower", "N/A")
                    batPower = solax_data.get("batPower", "N/A")
                    soc = solax_data.get("soc", "N/A")
                    powerdc1 = solax_data.get("powerdc1", "N/A")
                    powerdc2 = solax_data.get("powerdc2", "N/A")
                    consumeenergy = solax_data.get("consumeenergy", "N/A")
                except Exception as e:
                    logging.error(f"Fehler beim Abrufen von Solax-Daten: {e}", exc_info=True)
                    solax_data = {
                        "acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                        "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                        "api_fehler": True
                    }
                    acpower = feedinpower = batPower = soc = powerdc1 = powerdc2 = consumeenergy = "N/A"

                try:
                    is_night = await asyncio.to_thread(is_nighttime, config)
                    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
                    state.aktueller_ausschaltpunkt, state.aktueller_einschaltpunkt = calculate_shutdown_point(
                        config, is_night, solax_data, state)
                except Exception as e:
                    logging.error(f"Fehler in calculate_shutdown_point: {e}", exc_info=True)
                    nacht_reduction = 0
                    state.aktueller_ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 55))
                    state.aktueller_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPOINT_ERHOEHT", 50))

                t_boiler_oben = await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"])
                t_boiler_unten = await asyncio.to_thread(read_temperature, SENSOR_IDS["unten"])
                t_boiler_mittig = await asyncio.to_thread(read_temperature, SENSOR_IDS["mittig"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                t_boiler = (
                    (t_boiler_oben + t_boiler_unten) / 2 if t_boiler_oben is not None and t_boiler_unten is not None else "Fehler"
                )
                state.t_boiler = t_boiler  # Aktualisiere state.t_boiler
                pressure_ok = await asyncio.to_thread(check_pressure, state)

                if not pressure_ok:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, state, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info("Kompressor ausgeschaltet (Druckschalter offen).")
                    state.ausschluss_grund = "Druckschalter offen"
                    if not state.pressure_error_sent:
                        await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                                                   "⚠️ Druckschalter offen!", state.bot_token)
                        state.pressure_error_sent = True
                        state.last_pressure_error_time = now
                    await asyncio.sleep(2)
                    continue

                if state.pressure_error_sent and (
                        state.last_pressure_error_time is None or
                        (now - state.last_pressure_error_time) >= PRESSURE_ERROR_DELAY):
                    await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                                               "✅ Druckschalter wieder normal.", state.bot_token)
                    state.pressure_error_sent = False
                    state.last_pressure_error_time = None

                fehler, is_overtemp = check_boiler_sensors(t_boiler_oben, t_boiler_unten, config)
                if fehler:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, state, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info(f"Kompressor ausgeschaltet (Sensorfehler: {fehler}).")
                    state.ausschluss_grund = fehler
                    await asyncio.sleep(2)
                    continue

                if state.last_pressure_error_time and (now - state.last_pressure_error_time) < PRESSURE_ERROR_DELAY:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, state, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info("Kompressor ausgeschaltet (Druckfehler-Sperre).")
                    remaining_time = (PRESSURE_ERROR_DELAY - (now - state.last_pressure_error_time)).total_seconds()
                    state.ausschluss_grund = f"Druckfehler-Sperre ({remaining_time:.0f}s verbleibend)"
                    await asyncio.sleep(2)
                    continue

                if t_verd is not None and t_verd < state.verdampfertemperatur:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, state, False)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info(f"Kompressor ausgeschaltet (Verdampfer zu kalt: {t_verd:.1f}°C).")
                    state.ausschluss_grund = f"Verdampfer zu kalt ({t_verd:.1f}°C)"
                    await asyncio.sleep(2)
                    continue

                if t_boiler_oben is not None and t_boiler_unten is not None and t_boiler_mittig is not None:
                    EINSCHALTPOINT = state.aktueller_einschaltpunkt
                    AUSSCHALTPOINT = state.aktueller_ausschaltpunkt
                    TOLERANZ = 1.0
                    AUSSCHALTPOINT_UNTEN = AUSSCHALTPOINT

                    if state.solar_ueberschuss_aktiv:
                        logging.debug(
                            f"Solarüberschuss aktiv, prüfe Einschaltbedingungen: "
                            f"T_Oben={t_boiler_oben:.1f}, T_Unten={t_boiler_unten:.1f}, T_Mittig={t_boiler_mittig:.1f}, "
                            f"Einschaltpunkt={EINSCHALTPOINT}, Ausschaltpunkt={AUSSCHALTPOINT}"
                        )

                        if (t_boiler_unten < EINSCHALTPOINT and
                                t_boiler_mittig <= AUSSCHALTPOINT + TOLERANZ and
                                t_boiler_oben <= AUSSCHALTPOINT + TOLERANZ):
                            if not state.kompressor_ein:
                                if last_compressor_off_time and (
                                        now - last_compressor_off_time).total_seconds() < state.min_pause.total_seconds():
                                    pause_remaining = state.min_pause.total_seconds() - (
                                            now - last_compressor_off_time).total_seconds()
                                    state.ausschluss_grund = f"Zu kurze Pause ({pause_remaining:.1f}s verbleibend)"
                                    logging.info(
                                        f"Kompressor bleibt aus (zu kurze Pause: {(now - last_compressor_off_time)}, benötigt: {state.min_pause})"
                                    )
                                else:
                                    logging.info(
                                        f"Versuche, Kompressor einzuschalten "
                                        f"(T_Unten < {EINSCHALTPOINT} °C, T_Mittig ≤ {AUSSCHALTPOINT + TOLERANZ} °C, "
                                        f"T_Oben ≤ {AUSSCHALTPOINT + TOLERANZ} °C)."
                                    )
                                    result = await asyncio.to_thread(set_kompressor_status, state, True)
                                    if result is False:
                                        state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler"
                                        logging.warning(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")
                                    else:
                                        state.kompressor_ein = True
                                        state.last_compressor_on_time = now
                                        last_compressor_off_time = None
                                        state.ausschluss_grund = None
                                        logging.info(f"Kompressor erfolgreich eingeschaltet. Startzeit: {now}")

                        elif (t_boiler_unten >= AUSSCHALTPOINT_UNTEN or
                              t_boiler_mittig > AUSSCHALTPOINT + TOLERANZ or
                              t_boiler_oben > AUSSCHALTPOINT + TOLERANZ):
                            if state.kompressor_ein:
                                await asyncio.to_thread(set_kompressor_status, state, False)
                                state.kompressor_ein = False
                                last_compressor_off_time = now
                                state.last_runtime = now - state.last_compressor_on_time
                                state.total_runtime_today += state.last_runtime
                                state.ausschluss_grund = None
                                logging.info(
                                    f"Kompressor ausgeschaltet "
                                    f"(T_Unten ≥ {AUSSCHALTPOINT_UNTEN} °C oder "
                                    f"T_Mittig > {AUSSCHALTPOINT + TOLERANZ} °C oder "
                                    f"T_Oben > {AUSSCHALTPOINT + TOLERANZ} °C). Laufzeit: {state.last_runtime}"
                                )
                    else:
                        logging.debug(
                            f"Normalmodus, prüfe Einschaltbedingungen: "
                            f"T_Oben={t_boiler_oben:.1f}, T_Mittig={t_boiler_mittig:.1f}, "
                            f"Einschaltpunkt={state.aktueller_einschaltpunkt}, Ausschaltpunkt={state.aktueller_ausschaltpunkt}"
                        )
                        if (t_boiler_oben < state.aktueller_einschaltpunkt or
                                t_boiler_mittig < state.aktueller_einschaltpunkt):
                            if not state.kompressor_ein:
                                if last_compressor_off_time and (
                                        now - last_compressor_off_time).total_seconds() < state.min_pause.total_seconds():
                                    pause_remaining = state.min_pause.total_seconds() - (
                                            now - last_compressor_off_time).total_seconds()
                                    state.ausschluss_grund = f"Zu kurze Pause ({pause_remaining:.1f}s verbleibend)"
                                    logging.info(
                                        f"Kompressor bleibt aus (zu kurze Pause: {(now - last_compressor_off_time)}, benötigt: {state.min_pause})"
                                    )
                                else:
                                    logging.info(
                                        f"Versuche, Kompressor einzuschalten (ein Fühler < {state.aktueller_einschaltpunkt} °C)."
                                    )
                                    result = await asyncio.to_thread(set_kompressor_status, state, True)
                                    if result is False:
                                        state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler"
                                        logging.warning(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")
                                    else:
                                        state.kompressor_ein = True
                                        state.last_compressor_on_time = now
                                        last_compressor_off_time = None
                                        state.ausschluss_grund = None
                                        logging.info(f"Kompressor erfolgreich eingeschaltet. Startzeit: {now}")

                        elif (t_boiler_oben >= state.aktueller_ausschaltpunkt or
                              t_boiler_mittig >= state.aktueller_ausschaltpunkt):
                            if state.kompressor_ein:
                                await asyncio.to_thread(set_kompressor_status, state, False)
                                state.kompressor_ein = False
                                last_compressor_off_time = now
                                state.last_runtime = now - state.last_compressor_on_time
                                state.total_runtime_today += state.last_runtime
                                state.ausschluss_grund = None
                                logging.info(
                                    f"Kompressor ausgeschaltet (ein Fühler ≥ {state.aktueller_ausschaltpunkt} °C). Laufzeit: {state.last_runtime}"
                                )

                if state.kompressor_ein and state.last_compressor_on_time:
                    state.current_runtime = now - state.last_compressor_on_time
                else:
                    state.current_runtime = timedelta(0)

                should_log = (state.last_log_time is None or
                              (now - state.last_log_time) >= timedelta(minutes=1) or
                              state.kompressor_ein != state.last_kompressor_status)

                if should_log:
                    async with csv_lock:
                        async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                            power_source = get_power_source(solax_data) if solax_data else "Unbekannt"
                            csv_line = (
                                f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                                f"{t_boiler_oben if t_boiler_oben is not None else 'N/A'},"
                                f"{t_boiler_unten if t_boiler_unten is not None else 'N/A'},"
                                f"{t_boiler_mittig if t_boiler_mittig is not None else 'N/A'},"
                                f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                                f"{t_verd if t_verd is not None else 'N/A'},"
                                f"{'EIN' if state.kompressor_ein else 'AUS'},"
                                f"{acpower},{feedinpower},{batPower},{soc},{powerdc1},{powerdc2},{consumeenergy},"
                                f"{state.aktueller_einschaltpunkt},{state.aktueller_ausschaltpunkt},"
                                f"{int(state.solar_ueberschuss_aktiv)},{nacht_reduction},"
                                f"{power_source}\n"
                            )
                            await csvfile.write(csv_line)
                            logging.debug(f"CSV-Eintrag geschrieben: {csv_line.strip()}")

                        state.last_log_time = now
                        state.last_kompressor_status = state.kompressor_ein

                cycle_duration = (datetime.now() - last_cycle_time).total_seconds()
                if cycle_duration > 30:
                    watchdog_warning_count += 1
                    logging.error(
                        f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {watchdog_warning_count}/{WATCHDOG_MAX_WARNINGS}")
                    if watchdog_warning_count >= WATCHDOG_MAX_WARNINGS:
                        await asyncio.to_thread(set_kompressor_status, state, False, force_off=True)
                        await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                                                   "🚨 Watchdog-Fehler: Programm beendet.", state.bot_token)
                        await shutdown(session)
                        raise SystemExit("Watchdog-Exit")

                last_cycle_time = datetime.now()
                await asyncio.sleep(2)

            except Exception as e:
                logging.error(f"Fehler in der Hauptschleife: {e}", exc_info=True)
                await asyncio.sleep(30)

    except asyncio.CancelledError:
        telegram_task_handle.cancel()
        display_task_handle.cancel()
        await asyncio.gather(telegram_task_handle, display_task_handle, return_exceptions=True)
        raise

    finally:
        await shutdown(session)

async def run_program():
    async with aiohttp.ClientSession() as session:
        config = configparser.ConfigParser()
        config.read("config.ini")
        state = State(config)
        try:
            await main_loop(session, config, state)
        except KeyboardInterrupt:
            logging.info("Programm durch Benutzer abgebrochen (Ctrl+C).")
        except asyncio.CancelledError:
            logging.info("Hauptschleife abgebrochen.")
        finally:
            await shutdown(session)

if __name__ == "__main__":
    try:
        asyncio.run(run_program())
    except KeyboardInterrupt:
        # Verhindert, dass der KeyboardInterrupt-Traceback im Terminal erscheint
        logging.info("Programm beendet.")
        sys.exit(0)