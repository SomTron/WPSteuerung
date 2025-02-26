import os
import glob
import smbus2
import datetime
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
import configparser
import csv
import aiohttp  # Für asynchrone HTTP-Anfragen
import hashlib
from telegram import ReplyKeyboardMarkup
import asyncio  # Für asynchrone Programmierung
import aiofiles  # Für asynchrone Dateioperationen

# Basisverzeichnis für Temperatursensoren
BASE_DIR = "/sys/bus/w1/devices/"
# I2C-Adresse und Busnummer für das LCD
I2C_ADDR = 0x27
I2C_BUS = 1
# API-URL für SolaxCloud
API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"
# GPIO-Pin für den Kompressor
GIO21_PIN = 21

# Konfigurationsdatei einlesen
config = configparser.ConfigParser()
config.read("config.ini")

# Globale Variablen initialisieren
BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]  # Telegram Bot-Token
CHAT_ID = config["Telegram"]["CHAT_ID"]  # Chat-ID für Telegram
AUSSCHALTPUNKT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])  # Normaler Ausschaltpunkt
AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"])  # Erhöhter Ausschaltpunkt
EINSCHALTPUNKT = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"])  # Einschaltpunkt
VERDAMPFERTEMPERATUR = int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"])  # Mindesttemperatur Verdampfer
MIN_LAUFZEIT = datetime.timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))  # Mindestlaufzeit
MIN_PAUSE = datetime.timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))  # Mindestpause
TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]  # SolaxCloud Token-ID
SN = config["SolaxCloud"]["SN"]  # SolaxCloud Seriennummer

# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",  # Logdatei
    level=logging.DEBUG,  # Detaillierte Protokollierung
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Globale Variablen für den Programmstatus
last_api_call = None  # Zeitpunkt des letzten API-Aufrufs
last_api_data = None  # Zuletzt empfangene API-Daten
last_api_timestamp = None  # Zeitstempel der letzten API-Daten
kompressor_ein = False  # Status des Kompressors
start_time = None  # Startzeit des Kompressors
last_runtime = datetime.timedelta()  # Letzte Laufzeit des Kompressors
current_runtime = datetime.timedelta()  # Aktuelle Laufzeit
total_runtime_today = datetime.timedelta()  # Gesamtlaufzeit heute
last_day = datetime.datetime.now().date()  # Letzter Tag für Laufzeitberechnung
aktueller_ausschaltpunkt = AUSSCHALTPUNKT  # Aktueller Ausschaltpunkt
last_shutdown_time = datetime.datetime.now()  # Zeitpunkt des letzten Ausschaltens
last_config_hash = None  # Hash der letzten Konfiguration
last_log_time = datetime.datetime.now() - datetime.timedelta(minutes=1)  # Zeitpunkt des letzten Logs
last_kompressor_status = None  # Letzter Kompressorstatus
last_update_id = None  # Letzte Telegram-Update-ID
urlaubsmodus_aktiv = False  # Status des Urlaubsmodus
original_einschaltpunkt = EINSCHALTPUNKT  # Ursprünglicher Einschaltpunkt
original_ausschaltpunkt = AUSSCHALTPUNKT  # Ursprünglicher Ausschaltpunkt

# LCD global initialisieren, damit es im finally-Block verfügbar ist
lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)


# Asynchrone Funktion zum Senden von Telegram-Nachrichten
async def send_telegram_message(session, chat_id, message, reply_markup=None, parse_mode=None):
    """Sendet eine Nachricht über Telegram."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        if reply_markup:
            data["reply_markup"] = reply_markup.to_json()
        if parse_mode:
            data["parse_mode"] = parse_mode
        async with session.post(url, json=data) as response:
            response.raise_for_status()
            logging.info("Telegram-Nachricht gesendet.")
            return True
    except aiohttp.ClientError as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {e}")
        return False


# Asynchrone Funktion zum Abrufen von Telegram-Updates
async def get_telegram_updates(session, t_boiler_vorne, t_boiler_hinten, t_verd, offset=None):
    """Ruft Updates von der Telegram-API ab."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": offset} if offset else {}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            updates = await response.json()
            return updates.get('result', [])
    except aiohttp.ClientError as e:
        logging.error(f"Fehler bei der Telegram-API-Abfrage: {e}")
        return None


# Asynchrone Funktion zum Abrufen von Solax-Daten
async def get_solax_data(session):
    """Ruft Daten von der Solax-API ab und cached sie."""
    global last_api_call, last_api_data, last_api_timestamp
    now = datetime.datetime.now()
    if last_api_call and now - last_api_call < datetime.timedelta(minutes=5):
        logging.debug("Verwende zwischengespeicherte API-Daten.")
        return last_api_data

    try:
        params = {"tokenId": TOKEN_ID, "sn": SN}
        async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            data = await response.json()
            if data.get("success"):
                last_api_data = data.get("result")
                last_api_timestamp = now
                last_api_call = now
                return last_api_data
            else:
                logging.error(f"API-Fehler: {data.get('exception', 'Unbekannter Fehler')}")
                return None
    except aiohttp.ClientError as e:
        logging.error(f"Fehler bei der API-Anfrage: {e}")
        return None


# Funktion für die benutzerdefinierte Telegram-Tastatur
def get_custom_keyboard():
    """Erstellt eine benutzerdefinierte Tastatur mit verfügbaren Befehlen."""
    keyboard = [
        ["🌡️ Temperaturen"],  # Erste Zeile
        ["📊 Status"],  # Zweite Zeile
        ["🌴 Urlaub"],  # Dritte Zeile
        ["🏠 Urlaub aus"],  # Vierte Zeile
        ["🆘 Hilfe"]  # Fünfte Zeile
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# Asynchrone Hilfsfunktionen für Telegram
async def send_temperature_telegram(session, t_boiler_vorne, t_boiler_hinten, t_verd):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = f"🌡️ Aktuelle Temperaturen:\nKessel vorne: {t_boiler_vorne:.2f} °C\nKessel hinten: {t_boiler_hinten:.2f} °C\nVerdampfer: {t_verd:.2f} °C"
    return await send_telegram_message(session, CHAT_ID, message)


async def send_status_telegram(session, t_boiler_vorne, t_boiler_hinten, t_verd, kompressor_status, aktuelle_laufzeit,
                               gesamtlaufzeit, einschaltpunkt, ausschaltpunkt):
    """Sendet den aktuellen Status über Telegram."""
    message = (
        f"🌡️ Aktuelle Temperaturen:\n"
        f"Boiler vorne: {t_boiler_vorne:.2f} °C\n"
        f"Boiler hinten: {t_boiler_hinten:.2f} °C\n"
        f"Verdampfer: {t_verd:.2f} °C\n\n"
        f"🔧 Kompressorstatus: {'EIN' if kompressor_status else 'AUS'}\n"
        f"⏱️ Aktuelle Laufzeit: {aktuelle_laufzeit}\n"
        f"⏳ Gesamtlaufzeit heute: {gesamtlaufzeit}\n\n"
        f"🎯 Sollwerte:\n"
        f"Einschaltpunkt: {einschaltpunkt} °C\n"
        f"Ausschaltpunkt: {ausschaltpunkt} °C"
    )
    return await send_telegram_message(session, CHAT_ID, message)


async def send_welcome_message(session, chat_id):
    """Sendet eine Willkommensnachricht mit Tastatur."""
    message = (
        "🤖 Willkommen beim Heizungssteuerungs-Bot!\n\n"
        "Verwende die Tastatur, um Befehle auszuwählen."
    )
    return await send_telegram_message(session, chat_id, message, reply_markup=get_custom_keyboard())


async def send_unknown_command_message(session, chat_id):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    message = (
        "❌ Unbekannter Befehl.\n\n"
        "Verwende die Tastatur, um einen gültigen Befehl auszuwählen."
    )
    return await send_telegram_message(session, chat_id, message, reply_markup=get_custom_keyboard())


async def send_help_message(session):
    """Sendet eine Hilfenachricht mit verfügbaren Befehlen."""
    message = (
        "🤖 Verfügbare Befehle:\n\n"
        "🌡️ *Temperaturen* – Sendet die aktuellen Temperaturen.\n"
        "📊 *Status* – Sendet den aktuellen Status.\n"
        "🆘 *Hilfe* – Zeigt diese Nachricht an."
    )
    return await send_telegram_message(session, CHAT_ID, message, parse_mode="Markdown")


# Synchron bleibende Funktionen
def read_temperature(sensor_id):
    """Liest die Temperatur von einem DS18B20-Sensor."""
    device_file = os.path.join(BASE_DIR, sensor_id, "w1_slave")
    try:
        with open(device_file, "r") as f:
            lines = f.readlines()
            if lines[0].strip()[-3:] == "YES":
                temp_data = lines[1].split("=")[-1]
                return float(temp_data) / 1000.0
            return None
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Sensors {sensor_id}: {e}")
        return None


def check_boiler_sensors(t_vorne, t_hinten, config):
    """Prüft die Boiler-Sensoren auf Fehler."""
    try:
        ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
    except (KeyError, ValueError):
        ausschaltpunkt = 50
    fehler = None
    is_overtemp = False
    if t_vorne is None or t_hinten is None:
        fehler = "Fühlerfehler!"
    elif t_vorne >= (ausschaltpunkt + 10) or t_hinten >= (ausschaltpunkt + 10):
        fehler = "Übertemperatur!"
        is_overtemp = True
    elif abs(t_vorne - t_hinten) > 10:
        fehler = "Fühlerdifferenz!"
    return fehler, is_overtemp


def set_kompressor_status(ein, force_off=False):
    """Setzt den Status des Kompressors (EIN/AUS)."""
    global kompressor_ein, start_time, current_runtime, total_runtime_today, last_runtime, last_shutdown_time
    now = datetime.datetime.now()
    if ein:
        if not kompressor_ein:
            pause_time = now - last_shutdown_time
            if pause_time < MIN_PAUSE:
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}).")
                return False
            kompressor_ein = True
            start_time = now
            current_runtime = datetime.timedelta()
            logging.info("Kompressor EIN.")
        else:
            current_runtime = now - start_time
    else:
        if kompressor_ein:
            elapsed_time = now - start_time
            if elapsed_time < MIN_LAUFZEIT and not force_off:
                logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}).")
                return True
            kompressor_ein = False
            current_runtime = elapsed_time
            total_runtime_today += current_runtime
            last_runtime = current_runtime
            last_shutdown_time = now
            start_time = None
            logging.info(f"Kompressor AUS. Laufzeit: {elapsed_time}")
    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)
    return None


# Asynchrone Funktion zum Neuladen der Konfiguration
async def reload_config():
    """Lädt die Konfigurationsdatei asynchron neu und aktualisiert globale Variablen."""
    global AUSSCHALTPUNKT, AUSSCHALTPUNKT_ERHOEHT, EINSCHALTPUNKT, MIN_LAUFZEIT, MIN_PAUSE, TOKEN_ID, SN, VERDAMPFERTEMPERATUR, BOT_TOKEN, CHAT_ID, last_config_hash, urlaubsmodus_aktiv

    config_file = "config.ini"
    current_hash = calculate_file_hash(config_file)

    if last_config_hash is not None and current_hash != last_config_hash:
        logging.info("Konfigurationsdatei wurde geändert.")
        async with aiohttp.ClientSession() as session:
            await send_telegram_message(session, CHAT_ID, "🔧 Konfigurationsdatei wurde geändert.")

    try:
        async with aiofiles.open(config_file, mode='r') as f:
            content = await f.read()
            config = configparser.ConfigParser()
            config.read_string(content)

        if not urlaubsmodus_aktiv:
            AUSSCHALTPUNKT = check_value(
                int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]),
                min_value=30, max_value=80, default_value=50,
                parameter_name="AUSSCHALTPUNKT"
            )
            AUSSCHALTPUNKT_ERHOEHT = check_value(
                int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]),
                min_value=35, max_value=85, default_value=55,
                parameter_name="AUSSCHALTPUNKT_ERHOEHT",
                other_value=AUSSCHALTPUNKT, comparison=">="
            )
            EINSCHALTPUNKT = check_value(
                int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]),
                min_value=20, max_value=70, default_value=40,
                parameter_name="EINSCHALTPUNKT",
                other_value=AUSSCHALTPUNKT, comparison="<",
                min_difference=2
            )

        MIN_LAUFZEIT_MINUTEN = check_value(
            int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]),
            min_value=1, max_value=60, default_value=10,
            parameter_name="MIN_LAUFZEIT"
        )
        MIN_PAUSE_MINUTEN = check_value(
            int(config["Heizungssteuerung"]["MIN_PAUSE"]),
            min_value=1, max_value=60, default_value=20,
            parameter_name="MIN_PAUSE"
        )
        VERDAMPFERTEMPERATUR = check_value(
            int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"]),
            min_value=10, max_value=40, default_value=25,
            parameter_name="VERDAMPFERTEMPERATUR"
        )

        BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]
        CHAT_ID = config["Telegram"]["CHAT_ID"]
        MIN_LAUFZEIT = datetime.timedelta(minutes=MIN_LAUFZEIT_MINUTEN)
        MIN_PAUSE = datetime.timedelta(minutes=MIN_PAUSE_MINUTEN)
        TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
        SN = config["SolaxCloud"]["SN"]

        logging.info("Konfiguration erfolgreich neu geladen.")
        last_config_hash = current_hash

    except FileNotFoundError:
        logging.error("Konfigurationsdatei config.ini nicht gefunden.")
    except KeyError as e:
        logging.error(f"Fehlender Schlüssel in der Konfigurationsdatei: {e}")
    except ValueError as e:
        logging.error(f"Ungültiger Wert in der Konfigurationsdatei: {e}")
    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}")


# Funktion zum Anpassen der Sollwerte (synchron, wird in Thread ausgeführt)
def adjust_shutdown_and_start_points(solax_data, config):
    """Passt die Ein- und Ausschaltpunkte basierend auf Solax-Daten und Nachtzeit an."""
    global aktueller_ausschaltpunkt, EINSCHALTPUNKT, AUSSCHALTPUNKT

    is_night = is_nighttime(config)

    if urlaubsmodus_aktiv:
        aktueller_ausschaltpunkt = AUSSCHALTPUNKT
        aktueller_einschaltpunkt = EINSCHALTPUNKT
        logging.info(
            f"Urlaubsmodus aktiv: Ausschaltpunkt={aktueller_ausschaltpunkt}, Einschaltpunkt={aktueller_einschaltpunkt}")
        return

    aktueller_ausschaltpunkt = calculate_shutdown_point(config, is_night, solax_data)

    try:
        solar_ueberschuss = (
                solax_data is not None and
                (solax_data.get("batPower", 0) > 600 or
                 (solax_data.get("soc", 0) > 95 and solax_data.get("feedinpower", 0) > 600))
        )

        if solar_ueberschuss:
            aktueller_einschaltpunkt = aktueller_ausschaltpunkt
        else:
            nacht_reduction = int(config["Heizungssteuerung"]["NACHTABSENKUNG_KEY"]) if is_night else 0
            aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - nacht_reduction

        logging.info(f"Ausschaltpunkt: {aktueller_ausschaltpunkt}, Einschaltpunkt: {aktueller_einschaltpunkt}")
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Anpassen der Punkte: {e}")
        nacht_reduction = int(config["Heizungssteuerung"]["NACHTABSENKUNG_KEY"]) if is_night else 0
        aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - nacht_reduction
        aktueller_ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction


# Weitere Hilfsfunktionen
def calculate_file_hash(file_path):
    """Berechnet den SHA-256-Hash einer Datei."""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception:
        return None


def load_config():
    """Lädt die Konfigurationsdatei synchron."""
    config = configparser.ConfigParser()
    config.read("config.ini")
    return config


def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist."""
    now = datetime.datetime.now()
    try:
        start_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_START", "22:00")
        end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")
        start_hour, start_minute = map(int, start_time_str.split(':'))
        end_hour, end_minute = map(int, end_time_str.split(':'))
        start_time = datetime.datetime.combine(now.date(), datetime.time(start_hour, start_minute))
        end_time = datetime.datetime.combine(now.date(), datetime.time(end_hour, end_minute))
        if start_time > end_time:
            end_time = datetime.datetime.combine(now.date() + datetime.timedelta(days=1),
                                                 datetime.time(end_hour, end_minute))
        return start_time <= now <= end_time
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False


def calculate_shutdown_point(config, is_night, solax_data):
    """Berechnet den Ausschaltpunkt basierend auf Nachtzeit und Solax-Daten."""
    try:
        nacht_reduction = int(config["Heizungssteuerung"]["NACHTABSENKUNG_KEY"]) if is_night else 0
        solar_ueberschuss = (
                solax_data and
                (solax_data.get("batPower", 0) > 600 or
                 (solax_data.get("soc", 0) > 95 and solax_data.get("feedinpower", 0) > 600))
        )
        if solar_ueberschuss:
            return int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]) - nacht_reduction
        else:
            return int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Lesen der Konfiguration: {e}")
        return 50


def check_value(value, min_value, max_value, default_value, parameter_name, other_value=None, comparison=None,
                min_difference=None):
    """Überprüft und korrigiert einen Konfigurationswert."""
    if not (min_value <= value <= max_value):
        logging.error(f"Ungültiger Wert für {parameter_name}: {value}. Verwende Standardwert: {default_value}.")
        value = default_value
    if other_value is not None and comparison == "<" and not (value < other_value):
        value = default_value
    return value


def is_data_old(timestamp):
    """Prüft, ob Solax-Daten veraltet sind."""
    return timestamp and (datetime.datetime.now() - timestamp) > datetime.timedelta(minutes=15)


# Asynchrone Hauptschleife
async def main_loop():
    """Hauptschleife des Programms, asynchron ausgeführt."""
    global last_update_id, kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time, last_config_hash, last_log_time, last_kompressor_status, urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt

    # GPIO initialisieren
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(GIO21_PIN, GPIO.OUT)
        GPIO.output(GIO21_PIN, GPIO.LOW)
    except Exception as e:
        logging.error(f"Fehler bei der GPIO-Initialisierung: {e}")
        exit(1)

    # Asynchrone HTTP-Sitzung starten
    async with aiohttp.ClientSession() as session:
        # Startnachricht senden
        now = datetime.datetime.now()
        message = f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
        await send_telegram_message(session, CHAT_ID, message)
        await send_welcome_message(session, CHAT_ID)

        # Sensor-IDs definieren
        sensor_map = {
            "vorne": "28-0bd6d4461d84",
            "hinten": "28-445bd44686f4",
            "verd": "28-213bd4460d65"
        }

        # Hauptschleife
        while True:
            config = load_config()  # Synchron, später optimierbar
            current_hash = calculate_file_hash("config.ini")
            if last_config_hash != current_hash:
                await reload_config()  # Asynchrones Neuladen der Konfiguration
                last_config_hash = current_hash

            solax_data = await get_solax_data(session)
            if solax_data is None:
                solax_data = {"acpower": 0, "feedinpower": 0, "consumeenergy": 0, "batPower": 0, "soc": 0,
                              "powerdc1": 0, "powerdc2": 0, "api_fehler": True}

            # Synchroner Aufruf im Thread
            await asyncio.to_thread(adjust_shutdown_and_start_points, solax_data, config)

            # Temperaturen lesen
            t_boiler_vorne = read_temperature(sensor_map["vorne"])
            t_boiler_hinten = read_temperature(sensor_map["hinten"])
            t_verd = read_temperature(sensor_map["verd"])
            t_boiler = (
                                   t_boiler_vorne + t_boiler_hinten) / 2 if t_boiler_vorne is not None and t_boiler_hinten is not None else "Fehler"

            # Telegram-Updates abrufen und verarbeiten
            updates = await get_telegram_updates(session, t_boiler_vorne, t_boiler_hinten, t_verd, last_update_id)
            if updates:
                last_update_id = await process_telegram_messages_async(
                    session, t_boiler_vorne, t_boiler_hinten, t_verd, updates, last_update_id,
                    kompressor_ein, str(current_runtime).split('.')[0], str(total_runtime_today).split('.')[0]
                )

            # Boiler-Sensoren prüfen
            fehler, is_overtemp = check_boiler_sensors(t_boiler_vorne, t_boiler_hinten, config)
            if fehler:
                lcd.clear()
                lcd.write_string(f"FEHLER: {fehler}")
                await asyncio.sleep(5)
                set_kompressor_status(False, force_off=True)
                continue

            # Kompressorsteuerung
            if t_verd is not None and t_verd < VERDAMPFERTEMPERATUR:
                if kompressor_ein:
                    set_kompressor_status(False)
                    logging.info(f"Verdampfertemperatur unter {VERDAMPFERTEMPERATUR} Grad. Kompressor ausgeschaltet.")
            elif t_boiler != "Fehler":
                if t_boiler < EINSCHALTPUNKT and not kompressor_ein:
                    set_kompressor_status(True)
                    logging.info(f"T-Boiler Temperatur unter {EINSCHALTPUNKT} Grad. Kompressor eingeschaltet.")
                elif t_boiler >= aktueller_ausschaltpunkt and kompressor_ein:
                    set_kompressor_status(False)
                    logging.info(
                        f"T-Boiler Temperatur {aktueller_ausschaltpunkt} Grad erreicht. Kompressor ausgeschaltet.")

            if kompressor_ein and start_time:
                current_runtime = datetime.datetime.now() - start_time

            # Display-Seite 1: Temperaturen
            lcd.clear()
            lcd.write_string(f"T-Vorne: {t_boiler_vorne if t_boiler_vorne is not None else 'Fehler':.2f} C")
            lcd.cursor_pos = (1, 0)
            lcd.write_string(f"T-Hinten: {t_boiler_hinten if t_boiler_hinten is not None else 'Fehler':.2f} C")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(f"T-Boiler: {t_boiler if t_boiler != 'Fehler' else 'Fehler':.2f} C")
            lcd.cursor_pos = (3, 0)
            lcd.write_string(f"T-Verd: {t_verd if t_verd is not None else 'Fehler':.2f} C")
            await asyncio.sleep(5)

            # Display-Seite 2: Kompressorstatus und Laufzeiten
            lcd.clear()
            lcd.write_string(f"Kompressor: {'EIN' if kompressor_ein else 'AUS'}")
            lcd.cursor_pos = (1, 0)
            if t_boiler != "Fehler":
                lcd.write_string(f"Soll:{aktueller_ausschaltpunkt:.1f}C Ist:{t_boiler:.1f}C")
            else:
                lcd.write_string("Soll:N/A Ist:Fehler")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(
                f"Aktuell: {str(current_runtime).split('.')[0]}" if kompressor_ein else f"Letzte: {str(last_runtime).split('.')[0]}")
            lcd.cursor_pos = (3, 0)
            lcd.write_string(f"Gesamt: {str(total_runtime_today).split('.')[0]}")
            await asyncio.sleep(5)

            # Display-Seite 3: Solax-Daten
            lcd.clear()
            if solax_data:
                solar = solax_data.get("powerdc1", 0) + solax_data.get("powerdc2", 0)
                feedinpower = solax_data.get("feedinpower", "N/A")
                consumeenergy = solax_data.get("consumeenergy", "N/A")
                batPower = solax_data.get("batPower", "N/A")
                soc = solax_data.get("soc", "N/A")
                old_suffix = " ALT" if is_data_old(last_api_timestamp) else ""
                lcd.write_string(f"Solar: {solar} W{old_suffix}")
                lcd.cursor_pos = (1, 0)
                lcd.write_string(f"Netz: {feedinpower if feedinpower != 'N/A' else 'N/A'}{old_suffix}")
                lcd.cursor_pos = (2, 0)
                lcd.write_string(f"Verbrauch: {consumeenergy if consumeenergy != 'N/A' else 'N/A'}{old_suffix}")
                lcd.cursor_pos = (3, 0)
                lcd.write_string(f"Bat:{batPower}W,SOC:{soc}%")
            else:
                lcd.write_string("Fehler bei Solax-Daten")
            await asyncio.sleep(5)

            # Logging und CSV-Schreiben
            now = datetime.datetime.now()
            if last_log_time is None or (now - last_log_time) >= datetime.timedelta(
                    minutes=1) or kompressor_ein != last_kompressor_status:
                async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                    await csvfile.write(
                        f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                        f"{t_boiler_vorne if t_boiler_vorne is not None else 'N/A'},"
                        f"{t_boiler_hinten if t_boiler_hinten is not None else 'N/A'},"
                        f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                        f"{t_verd if t_verd is not None else 'N/A'},"
                        f"{'EIN' if kompressor_ein else 'AUS'}\n"
                    )
                last_log_time = now
                last_kompressor_status = kompressor_ein

            await asyncio.sleep(1)  # Hauptzyklus-Pause


# Asynchrone Verarbeitung von Telegram-Nachrichten
async def process_telegram_messages_async(session, t_boiler_vorne, t_boiler_hinten, t_verd, updates, last_update_id,
                                          kompressor_status, aktuelle_laufzeit, gesamtlaufzeit):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    global EINSCHALTPUNKT, AUSSCHALTPUNKT
    if updates:
        for update in updates:
            message_text = update.get('message', {}).get('text')
            chat_id = update.get('message', {}).get('chat', {}).get('id')
            if message_text and chat_id:
                message_text = message_text.strip().lower()
                if message_text == "🌡️ temperaturen" or message_text == "temperaturen":
                    if t_boiler_vorne != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        await send_temperature_telegram(session, t_boiler_vorne, t_boiler_hinten, t_verd)
                    else:
                        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen der Temperaturen.")
                elif message_text == "📊 status" or message_text == "status":
                    if t_boiler_vorne != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        await send_status_telegram(session, t_boiler_vorne, t_boiler_hinten, t_verd, kompressor_status,
                                                   aktuelle_laufzeit, gesamtlaufzeit, EINSCHALTPUNKT, AUSSCHALTPUNKT)
                    else:
                        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen des Status.")
                elif message_text == "🆘 hilfe" or message_text == "hilfe":
                    await send_help_message(session)
                elif message_text == "🌴 urlaub" or message_text == "urlaub":
                    await aktivere_urlaubsmodus(session)
                elif message_text == "🏠 urlaub aus" or message_text == "urlaub aus":
                    await deaktivere_urlaubsmodus(session)
                else:
                    await send_unknown_command_message(session, chat_id)
            last_update_id = update['update_id'] + 1
    return last_update_id


# Asynchrone Urlaubsmodus-Funktionen
async def aktivere_urlaubsmodus(session):
    """Aktiviert den Urlaubsmodus und passt Sollwerte an."""
    global urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt
    if not urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = True
        original_einschaltpunkt = EINSCHALTPUNKT
        original_ausschaltpunkt = AUSSCHALTPUNKT
        urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 6))
        EINSCHALTPUNKT -= urlaubsabsenkung
        AUSSCHALTPUNKT -= urlaubsabsenkung
        logging.info(
            f"Urlaubsmodus aktiviert. Neue Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
        await send_telegram_message(session, CHAT_ID,
                                    f"🌴 Urlaubsmodus aktiviert. Neue Werte:\nEinschaltpunkt: {EINSCHALTPUNKT} °C\nAusschaltpunkt: {AUSSCHALTPUNKT} °C")


async def deaktivere_urlaubsmodus(session):
    """Deaktiviert den Urlaubsmodus und stellt ursprüngliche Werte wieder her."""
    global urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt
    if urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = False
        EINSCHALTPUNKT = original_einschaltpunkt
        AUSSCHALTPUNKT = original_ausschaltpunkt
        logging.info(
            f"Urlaubsmodus deaktiviert. Ursprüngliche Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
        await send_telegram_message(session, CHAT_ID,
                                    f"🏠 Urlaubsmodus deaktiviert. Ursprüngliche Werte:\nEinschaltpunkt: {EINSCHALTPUNKT} °C\nAusschaltpunkt: {AUSSCHALTPUNKT} °C")


# Programmstart
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())  # Startet die asynchrone Hauptschleife
    except KeyboardInterrupt:
        logging.info("Programm durch Benutzer beendet.")
    finally:
        GPIO.cleanup()  # Bereinigt GPIO-Pins
        lcd.close()  # Schließt das LCD
        logging.info("Heizungssteuerung beendet.")