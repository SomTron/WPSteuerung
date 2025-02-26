import os
import glob
import smbus2
import datetime
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
import configparser
import csv
import aiohttp
import hashlib
from telegram import ReplyKeyboardMarkup
import asyncio
import aiofiles

# Basisverzeichnis für Temperatursensoren und Sensor-IDs
BASE_DIR = "/sys/bus/w1/devices/"
SENSOR_IDS = {
    "vorne": "28-0bd6d4461d84",
    "hinten": "28-445bd44686f4",
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
AUSSCHALTPUNKT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"])
EINSCHALTPUNKT = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"])
VERDAMPFERTEMPERATUR = int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"])
MIN_LAUFZEIT = datetime.timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))
MIN_PAUSE = datetime.timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))
TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
SN = config["SolaxCloud"]["SN"]



# Globale Variablen für den Programmstatus
last_api_call = None
last_api_data = None
last_api_timestamp = None
kompressor_ein = False
start_time = None
last_runtime = datetime.timedelta()
current_runtime = datetime.timedelta()
total_runtime_today = datetime.timedelta()
last_day = datetime.datetime.now().date()
aktueller_ausschaltpunkt = AUSSCHALTPUNKT
last_shutdown_time = datetime.datetime.now()
last_config_hash = None
last_log_time = datetime.datetime.now() - datetime.timedelta(minutes=1)
last_kompressor_status = None
last_update_id = None
urlaubsmodus_aktiv = False
original_einschaltpunkt = EINSCHALTPUNKT
original_ausschaltpunkt = AUSSCHALTPUNKT
pressure_error_sent = False



# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# LCD global initialisieren
lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)


# Asynchrone Funktion zum Senden von Telegram-Nachrichten
async def send_telegram_message(session, chat_id, message, reply_markup=None, parse_mode=None):
    """
    Sendet eine Nachricht über die Telegram-API.

    Args:
        session (aiohttp.ClientSession): Die HTTP-Sitzung für die API-Anfrage.
        chat_id (str): Die ID des Chatrooms, an den die Nachricht gesendet wird.
        message (str): Der Text der zu sendenden Nachricht.
        reply_markup (telegram.ReplyKeyboardMarkup, optional): Tastaturmarkup für interaktive Antworten.
        parse_mode (str, optional): Formatierungsmodus der Nachricht (z.B. "Markdown").

    Returns:
        bool: True bei Erfolg, False bei Fehler.
    """
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        if reply_markup:
            data["reply_markup"] = reply_markup.to_json()
        if parse_mode:
            data["parse_mode"] = parse_mode
        async with session.post(url, json=data) as response:
            response.raise_for_status()
            logging.info(f"Telegram-Nachricht gesendet: {message}")
            logging.debug(f"Antwort-Details: URL={url}, Daten={data}")
            return True
    except aiohttp.ClientError as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {e}, Nachricht={message}")
        return False


# Asynchrone Funktion zum Abrufen von Telegram-Updates
async def get_telegram_updates(session, offset=None):
    """Ruft Updates von der Telegram-API ab."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": offset} if offset else {}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            updates = await response.json()
            logging.debug(f"Telegram-Updates empfangen: {updates}")
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
                logging.info(f"Solax-Daten erfolgreich abgerufen: {last_api_data}")
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
        ["🌡️ Temperaturen"],
        ["📊 Status"],
        ["🌴 Urlaub"],
        ["🏠 Urlaub aus"],
        ["🆘 Hilfe"]
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
        "🌴 *Urlaub* – Aktiviert den Urlaubsmodus.\n"
        "🏠 *Urlaub aus* – Deaktiviert den Urlaubsmodus.\n"
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
                temp = float(temp_data) / 1000.0
                # Plausibilitätsprüfung: Temperaturen außerhalb -20°C bis 100°C sind unwahrscheinlich
                if temp < -20 or temp > 100:
                    logging.error(f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C. Sensor als fehlerhaft betrachtet.")
                    return None
                logging.debug(f"Temperatur von Sensor {sensor_id} gelesen: {temp} °C")
                return temp
            logging.warning(f"Ungültige Daten von Sensor {sensor_id}")
            return None
    except Exception as e:
        logging.error(f"Fehler beim Lesen des Sensors {sensor_id}: {e}")
        return None


def check_pressure():
    """Prüft den Druckschalter (GPIO 17)."""
    pressure_ok = GPIO.input(PRESSURE_SENSOR_PIN)  # HIGH = Druck OK, LOW = Druck zu niedrig
    logging.debug(f"Druckschalter-Status: {pressure_ok} (HIGH=OK, LOW=zu niedrig)")
    return pressure_ok


def check_boiler_sensors(t_vorne, t_hinten, config):
    """Prüft die Boiler-Sensoren auf Fehler."""
    try:
        ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
    except (KeyError, ValueError):
        ausschaltpunkt = 50
        logging.warning(f"Ausschaltpunkt nicht gefunden, verwende Standard: {ausschaltpunkt}")
    fehler = None
    is_overtemp = False
    if t_vorne is None or t_hinten is None:
        fehler = "Fühlerfehler!"
        logging.error(f"Fühlerfehler erkannt: vorne={t_vorne}, hinten={t_hinten}")
    elif t_vorne >= (ausschaltpunkt + 10) or t_hinten >= (ausschaltpunkt + 10):
        fehler = "Übertemperatur!"
        is_overtemp = True
        logging.error(f"Übertemperatur erkannt: vorne={t_vorne}, hinten={t_hinten}, Grenze={ausschaltpunkt + 10}")
    elif abs(t_vorne - t_hinten) > 10:
        fehler = "Fühlerdifferenz!"
        logging.warning(
            f"Fühlerdifferenz erkannt: vorne={t_vorne}, hinten={t_hinten}, Differenz={abs(t_vorne - t_hinten)}")
    return fehler, is_overtemp


def set_kompressor_status(ein, force_off=False):
    global kompressor_ein, start_time, current_runtime, total_runtime_today, last_runtime, last_shutdown_time
    now = datetime.datetime.now()
    if ein:
        if not kompressor_ein:
            pause_time = now - last_shutdown_time
            if pause_time < MIN_PAUSE and not force_off:
                logging.warning(f"Kompressor-Einschaltung verweigert: Zu kurze Pause ({pause_time} < {MIN_PAUSE})")
                return False
            kompressor_ein = True
            start_time = now
            current_runtime = datetime.timedelta()
            logging.info(f"Kompressor EIN geschaltet. Startzeit: {start_time}")
        else:
            current_runtime = now - start_time
    else:
        if kompressor_ein:
            elapsed_time = now - start_time
            if elapsed_time < MIN_LAUFZEIT and not force_off:
                logging.warning(
                    f"Kompressor-Ausschaltung verweigert: Zu kurze Laufzeit ({elapsed_time} < {MIN_LAUFZEIT})")
                return True
            kompressor_ein = False
            current_runtime = elapsed_time
            total_runtime_today += current_runtime
            last_runtime = current_runtime
            last_shutdown_time = now
            start_time = None
            logging.info(f"Kompressor AUS geschaltet. Laufzeit: {elapsed_time}")

    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)
    # Prüfen, ob der GPIO-Status korrekt gesetzt wurde
    actual_state = GPIO.input(GIO21_PIN)  # Annahme: Pin kann auch als Eingang gelesen werden
    if actual_state != (GPIO.HIGH if ein else GPIO.LOW):
        logging.error(f"GPIO-Fehler: Kompressor-Status sollte {'EIN' if ein else 'AUS'} sein, ist aber {actual_state}")
    return None


# Asynchrone Funktion zum Neuladen der Konfiguration
async def reload_config(session):
    """Lädt die Konfigurationsdatei asynchron neu und aktualisiert globale Variablen."""
    global AUSSCHALTPUNKT, AUSSCHALTPUNKT_ERHOEHT, EINSCHALTPUNKT, MIN_LAUFZEIT, MIN_PAUSE, TOKEN_ID, SN, VERDAMPFERTEMPERATUR, BOT_TOKEN, CHAT_ID, last_config_hash, urlaubsmodus_aktiv

    config_file = "config.ini"
    current_hash = calculate_file_hash(config_file)

    if last_config_hash is not None and current_hash != last_config_hash:
        logging.info(f"Konfigurationsdatei geändert. Alter Hash: {last_config_hash}, Neuer Hash: {current_hash}")
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

        logging.info(
            f"Konfiguration erfolgreich neu geladen: AUSSCHALTPUNKT={AUSSCHALTPUNKT}, EINSCHALTPUNKT={EINSCHALTPUNKT}, MIN_LAUFZEIT={MIN_LAUFZEIT}")
        logging.debug(f"Vollständige Konfiguration: {dict(config['Heizungssteuerung'])}")
        last_config_hash = current_hash

    except FileNotFoundError:
        logging.error("Konfigurationsdatei config.ini nicht gefunden!")
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

        logging.info(
            f"Sollwerte angepasst: Ausschaltpunkt={aktueller_ausschaltpunkt}, Einschaltpunkt={aktueller_einschaltpunkt}, Solarüberschuss={solar_ueberschuss}, Nachtzeit={is_night}")
        logging.debug(f"Solax-Daten für Anpassung: {solax_data}")
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Anpassen der Punkte: {e}, Solax-Daten={solax_data}")
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
            "EINSCHALTPUNKT": "40",
            "VERDAMPFERTEMPERATUR": "25",
            "MIN_LAUFZEIT": "10",
            "MIN_PAUSE": "20",
            "NACHTABSENKUNG_KEY": "0"
        },
        "Telegram": {"BOT_TOKEN": "", "CHAT_ID": ""},
        "SolaxCloud": {"TOKEN_ID": "", "SN": ""}
    }
    for section in defaults:
        if section not in config:
            config[section] = {}
            logging.warning(f"Abschnitt {section} fehlt in config.ini, wird mit Standardwerten erstellt.")
        for key, default in defaults[section].items():
            try:
                if key in config[section]:
                    if key not in ["BOT_TOKEN", "CHAT_ID", "TOKEN_ID", "SN"]:
                        value = int(config[section][key])
                        min_val = 0 if key not in ["AUSSCHALTPUNKT", "AUSSCHALTPUNKT_ERHOEHT", "EINSCHALTPUNKT"] else 20
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
        is_night = start_time <= now <= end_time
        logging.debug(f"Nachtzeitprüfung: Jetzt={now}, Start={start_time}, Ende={end_time}, Ist Nacht={is_night}")
        return is_night
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
            shutdown_point = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]) - nacht_reduction
        else:
            shutdown_point = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction
        logging.debug(
            f"Ausschaltpunkt berechnet: Solarüberschuss={solar_ueberschuss}, Nachtreduktion={nacht_reduction}, Ergebnis={shutdown_point}")
        return shutdown_point
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Berechnen des Ausschaltpunkts: {e}, Solax-Daten={solax_data}")
        return 50


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
    is_old = timestamp and (datetime.datetime.now() - timestamp) > datetime.timedelta(minutes=15)
    logging.debug(f"Prüfe Solax-Datenalter: Zeitstempel={timestamp}, Ist alt={is_old}")
    return is_old


# Asynchrone Task für Telegram-Updates
async def telegram_task(session):
    """Separate Task für schnelle Telegram-Update-Verarbeitung."""
    global last_update_id, kompressor_ein, current_runtime, total_runtime_today, EINSCHALTPUNKT, AUSSCHALTPUNKT
    while True:
        updates = await get_telegram_updates(session, last_update_id)
        if updates:
            last_update_id = await process_telegram_messages_async(
                session,
                await asyncio.to_thread(read_temperature, SENSOR_IDS["vorne"]),  # Boiler vorne
                await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"]),  # Boiler hinten
                await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"]),  # Verdampfer
                updates,
                last_update_id,
                kompressor_ein,
                str(current_runtime).split('.')[0],
                str(total_runtime_today).split('.')[0]
            )
        await asyncio.sleep(0.1)  # Schnelles Polling für Telegram


# Asynchrone Task für Display-Updates
async def display_task():
    """Separate Task für Display-Updates, entkoppelt von der Hauptschleife."""
    while True:
        # Seite 1: Temperaturen
        t_boiler_vorne = await asyncio.to_thread(read_temperature, SENSOR_IDS["vorne"])
        t_boiler_hinten = await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"])
        t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
        t_boiler = (
                               t_boiler_vorne + t_boiler_hinten) / 2 if t_boiler_vorne is not None and t_boiler_hinten is not None else "Fehler"
        pressure_ok = await asyncio.to_thread(check_pressure)

        lcd.clear()
        if not pressure_ok:
            lcd.write_string("FEHLER: Druck zu niedrig")
            logging.error(f"Display zeigt Druckfehler: Druckschalter={pressure_ok}")
        else:
            lcd.write_string(f"T-Vorne: {t_boiler_vorne if t_boiler_vorne is not None else 'Fehler':.2f} C")
            lcd.cursor_pos = (1, 0)
            lcd.write_string(f"T-Hinten: {t_boiler_hinten if t_boiler_hinten is not None else 'Fehler':.2f} C")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(f"T-Boiler: {t_boiler if t_boiler != 'Fehler' else 'Fehler':.2f} C")
            lcd.cursor_pos = (3, 0)
            lcd.write_string(f"T-Verd: {t_verd if t_verd is not None else 'Fehler':.2f} C")
            logging.debug(
                f"Display-Seite 1 aktualisiert: vorne={t_boiler_vorne}, hinten={t_boiler_hinten}, boiler={t_boiler}, verd={t_verd}")
        await asyncio.sleep(5)

        # Seite 2: Kompressorstatus
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
        logging.debug(
            f"Display-Seite 2 aktualisiert: Status={'EIN' if kompressor_ein else 'AUS'}, Laufzeit={current_runtime if kompressor_ein else last_runtime}")
        await asyncio.sleep(5)

        # Seite 3: Solax-Daten
        lcd.clear()
        if last_api_data:
            solar = last_api_data.get("powerdc1", 0) + last_api_data.get("powerdc2", 0)
            feedinpower = last_api_data.get("feedinpower", "N/A")
            consumeenergy = last_api_data.get("consumeenergy", "N/A")
            batPower = last_api_data.get("batPower", "N/A")
            soc = last_api_data.get("soc", "N/A")
            old_suffix = " ALT" if is_data_old(last_api_timestamp) else ""
            lcd.write_string(f"Solar: {solar} W{old_suffix}")
            lcd.cursor_pos = (1, 0)
            lcd.write_string(f"Netz: {feedinpower if feedinpower != 'N/A' else 'N/A'}{old_suffix}")
            lcd.cursor_pos = (2, 0)
            lcd.write_string(f"Verbrauch: {consumeenergy if consumeenergy != 'N/A' else 'N/A'}{old_suffix}")
            lcd.cursor_pos = (3, 0)
            lcd.write_string(f"Bat:{batPower}W,SOC:{soc}%")
            logging.debug(
                f"Display-Seite 3 aktualisiert: Solar={solar}, Netz={feedinpower}, Verbrauch={consumeenergy}, Batterie={batPower}, SOC={soc}")
        else:
            lcd.write_string("Fehler bei Solax-Daten")
            logging.warning("Keine Solax-Daten für Display verfügbar")
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
            GPIO.setup(PRESSURE_SENSOR_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
            logging.info("GPIO erfolgreich initialisiert: Kompressor=GPIO21, Druckschalter=GPIO17")
            return True
        except Exception as e:
            logging.error(f"GPIO-Initialisierung fehlgeschlagen (Versuch {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(1)  # Warte asynchron 1 Sekunde vor dem nächsten Versuch
    logging.critical("GPIO-Initialisierung nach mehreren Versuchen fehlgeschlagen.")
    return False


# Asynchrone Hauptschleife
async def main_loop():
    """
    Hauptschleife des Programms, die Steuerung und Überwachung asynchron ausführt.

    Initialisiert die Hardware, startet asynchrone Tasks für Telegram und Display,
    und steuert den Kompressor basierend auf Temperatur- und Drucksensorwerten.
    Überwacht die Konfigurationsdatei auf Änderungen und speichert regelmäßig Daten in eine CSV-Datei.

    Verwendet globale Variablen:
        last_update_id, kompressor_ein, start_time, current_runtime, total_runtime_today,
        last_day, last_runtime, last_shutdown_time, last_config_hash, last_log_time,
        last_kompressor_status, urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT,
        original_einschaltpunkt, original_ausschaltpunkt, pressure_error_sent

    Raises:
        asyncio.CancelledError: Bei Programmabbruch (z.B. durch Ctrl+C), um Tasks sauber zu beenden.
    """
    # Zugriff auf globale Variablen für Zustandsverwaltung
    global last_update_id, kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time, last_config_hash, last_log_time, last_kompressor_status, urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt, pressure_error_sent

    # GPIO-Pins initialisieren (Kompressor und Druckschalter)
    # Beendet das Programm bei wiederholtem Fehlschlag
    if not await initialize_gpio():
        logging.critical("Programm wird aufgrund fehlender GPIO-Initialisierung beendet.")
        exit(1)

    # Asynchrone HTTP-Sitzung für Telegram- und Solax-API-Anfragen starten
    async with aiohttp.ClientSession() as session:
        # Startnachricht mit aktuellem Zeitstempel an Telegram senden
        now = datetime.datetime.now()
        message = f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
        await send_telegram_message(session, CHAT_ID, message)
        # Willkommensnachricht mit Tastatur an Telegram senden
        await send_welcome_message(session, CHAT_ID)

        # Asynchrone Tasks starten: Telegram-Updates und Display-Anzeige
        telegram_task_handle = asyncio.create_task(telegram_task(session))  # Verarbeitet Telegram-Nachrichten
        display_task_handle = asyncio.create_task(display_task())  # Aktualisiert das LCD-Display

        try:
            # Endlosschleife für die Hauptsteuerung
            while True:
                # Konfiguration laden und validieren, um sicherzustellen, dass Werte gültig sind
                config = validate_config(load_config())

                # Prüfen, ob sich die Konfigurationsdatei geändert hat
                current_hash = calculate_file_hash("config.ini")
                if last_config_hash != current_hash:
                    await reload_config(session)  # Konfiguration neu laden, falls geändert
                    last_config_hash = current_hash

                # Solax-Daten von der API abrufen, mit Fallback bei Fehler
                solax_data = await get_solax_data(session)
                if solax_data is None:
                    # Fallback-Daten verwenden, falls API-Abfrage fehlschlägt
                    solax_data = {
                        "acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                        "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                        "api_fehler": True
                    }

                # Sollwerte (Ein-/Ausschaltpunkte) basierend auf Solax-Daten und Nachtzeit anpassen
                await asyncio.to_thread(adjust_shutdown_and_start_points, solax_data, config)

                # Temperaturen von den drei Sensoren (vorne, hinten, Verdampfer) auslesen
                t_boiler_vorne = await asyncio.to_thread(read_temperature, SENSOR_IDS["vorne"])
                t_boiler_hinten = await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                # Durchschnittstemperatur des Boilers berechnen, falls beide Sensoren gültig
                t_boiler = (t_boiler_vorne + t_boiler_hinten) / 2 if t_boiler_vorne is not None and t_boiler_hinten is not None else "Fehler"


                #Sicherheit Max Temp Boiler:
                MAX_SAFE_TEMP = 80  # Maximale sichere Temperatur in °C

                if t_boiler_vorne is not None and t_boiler_vorne > MAX_SAFE_TEMP or \
                        t_boiler_hinten is not None and t_boiler_hinten > MAX_SAFE_TEMP:
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    if not pressure_error_sent:  # Wiederverwendung der Variable für allgemeine Fehler
                        await send_telegram_message(session, CHAT_ID,
                                                    "❌ Fehler: Übertemperatur erkannt! Kompressor ausgeschaltet.")
                        pressure_error_sent = True  # Verhindert Mehrfachmeldungen
                    logging.error(
                        f"Übertemperatur erkannt: vorne={t_boiler_vorne}, hinten={t_boiler_hinten}, Kompressor ausgeschaltet")
                    continue


                # Druckschalter prüfen (GPIO 17): LOW = Druck zu niedrig, HIGH = Druck OK
                pressure_ok = await asyncio.to_thread(check_pressure)
                if not pressure_ok:
                    # Bei zu niedrigem Druck: Kompressor ausschalten und einmalig melden
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    if not pressure_error_sent:
                        await send_telegram_message(session, CHAT_ID, "❌ Fehler: Druck zu niedrig! Kompressor ausgeschaltet.")
                        pressure_error_sent = True
                        logging.error(f"Druck zu niedrig erkannt: Kompressor ausgeschaltet, Temperaturen: vorne={t_boiler_vorne}, hinten={t_boiler_hinten}, verd={t_verd}")
                    continue  # Überspringe den Rest der Schleife, bis Druck wieder OK
                else:
                    # Druck wieder normal: Fehlermeldungsstatus zurücksetzen
                    if pressure_error_sent:
                        logging.info("Druck wieder normal, Fehlermeldungsstatus zurückgesetzt")
                        pressure_error_sent = False

                # Boiler-Sensoren auf Fehler (Ausfall, Übertemperatur, Differenz) prüfen
                fehler, is_overtemp = check_boiler_sensors(t_boiler_vorne, t_boiler_hinten, config)
                if fehler:
                    # Bei Fehler: Kompressor ausschalten und Schleife fortsetzen
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    logging.info(f"Kompressor wegen Fehler ausgeschaltet: {fehler}")
                    continue

                # Kompressorsteuerung basierend auf Temperaturen
                if t_verd is not None and t_verd < VERDAMPFERTEMPERATUR:
                    # Verdampfertemperatur zu niedrig: Kompressor ausschalten
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)
                elif t_boiler != "Fehler":
                    # Boiler-Temperatur unter Einschaltpunkt: Kompressor einschalten
                    if t_boiler < EINSCHALTPUNKT and not kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, True)
                    # Boiler-Temperatur über Ausschaltpunkt: Kompressor ausschalten
                    elif t_boiler >= aktueller_ausschaltpunkt and kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)

                # Aktuelle Laufzeit berechnen, wenn Kompressor aktiv
                if kompressor_ein and start_time:
                    current_runtime = datetime.datetime.now() - start_time

                # Datenlogging in CSV-Datei, bei Statusänderung oder alle Minute
                now = datetime.datetime.now()
                if last_log_time is None or (now - last_log_time) >= datetime.timedelta(minutes=1) or kompressor_ein != last_kompressor_status:
                    async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                        # CSV-Zeile mit Zeitstempel und aktuellen Werten erstellen
                        csv_line = (
                            f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                            f"{t_boiler_vorne if t_boiler_vorne is not None else 'N/A'},"
                            f"{t_boiler_hinten if t_boiler_hinten is not None else 'N/A'},"
                            f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                            f"{t_verd if t_verd is not None else 'N/A'},"
                            f"{'EIN' if kompressor_ein else 'AUS'}\n"
                        )
                        await csvfile.write(csv_line)
                        logging.info(f"CSV-Eintrag geschrieben: {csv_line.strip()}")
                        logging.debug(f"Zusätzliche Daten: TotalRuntime={total_runtime_today}, LastShutdown={last_shutdown_time}")
                    last_log_time = now
                    last_kompressor_status = kompressor_ein

                # Pause von 0,5 Sekunden, um CPU-Last zu reduzieren
                await asyncio.sleep(0.5)

        except asyncio.CancelledError:
            # Bei Programmabbruch (z.B. Ctrl+C): Tasks sauber beenden
            logging.info("Hauptschleife abgebrochen, Tasks werden beendet.")
            telegram_task_handle.cancel()
            display_task_handle.cancel()
            # Warten, bis alle Tasks abgebrochen sind, Fehler ignorieren
            await asyncio.gather(telegram_task_handle, display_task_handle, return_exceptions=True)
            raise

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
                logging.debug(f"Telegram-Nachricht empfangen: Text={message_text}, Chat-ID={chat_id}")
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
                    if urlaubsmodus_aktiv:
                        await send_telegram_message(session, CHAT_ID, "🌴 Urlaubsmodus ist bereits aktiviert.")
                        logging.info("Urlaubsmodus bereits aktiv, keine Änderung")
                    else:
                        await aktivere_urlaubsmodus(session)
                elif message_text == "🏠 urlaub aus" or message_text == "urlaub aus":
                    if not urlaubsmodus_aktiv:
                        await send_telegram_message(session, CHAT_ID, "🏠 Urlaubsmodus ist bereits deaktiviert.")
                        logging.info("Urlaubsmodus bereits deaktiviert, keine Änderung")
                    else:
                        await deaktivere_urlaubsmodus(session)
                else:
                    await send_unknown_command_message(session, chat_id)
            last_update_id = update['update_id'] + 1
            logging.debug(f"last_update_id aktualisiert: {last_update_id}")
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
            f"Urlaubsmodus aktiviert. Alte Werte: Einschaltpunkt={original_einschaltpunkt}, Ausschaltpunkt={original_ausschaltpunkt}, Neue Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
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
            f"Urlaubsmodus deaktiviert. Wiederhergestellte Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
        await send_telegram_message(session, CHAT_ID,
                                    f"🏠 Urlaubsmodus deaktiviert. Ursprüngliche Werte:\nEinschaltpunkt: {EINSCHALTPUNKT} °C\nAusschaltpunkt: {AUSSCHALTPUNKT} °C")


# Programmstart
if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logging.info("Programm durch Benutzer beendet.")
    finally:
        # Sicherstellen, dass der Kompressor aus ist, bevor GPIO bereinigt wird
        GPIO.output(GIO21_PIN, GPIO.LOW)  # Kompressor ausschalten
        GPIO.cleanup()  # GPIO-Pins bereinigen
        lcd.close()  # LCD schließen
        logging.info("Heizungssteuerung sicher beendet, Hardware in sicherem Zustand.")