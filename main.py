import os
import glob
import time
import smbus2
from datetime import datetime, timedelta
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
import configparser
import csv
import requests
import hashlib
from telegram import ReplyKeyboardMarkup

# Konfiguration
BASE_DIR = "/sys/bus/w1/devices/"  # Basisverzeichnis für Temperatursensoren
I2C_ADDR = 0x27  # I2C-Adresse des LCD-Displays
I2C_BUS = 1  # I2C-Busnummer
API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"  # Solax-API-URL
GIO21_PIN = 21  # GPIO-Pin für den Kompressor

# Konfigurationsdatei einlesen
config = configparser.ConfigParser()

# Werte aus der Konfigurationsdatei laden
try:
    config.read("config.ini")
    BOT_TOKEN = config.get("Telegram", "BOT_TOKEN")
    CHAT_ID = config.get("Telegram", "CHAT_ID")
    AUSSCHALTPUNKT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
    AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"])
    EINSCHALTPUNKT = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"])
    VERDAMPFERTEMPERATUR = int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"])
    MIN_LAUFZEIT = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))  # Mindestlaufzeit
    MIN_PAUSE = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))  # Mindestpause
    AUSSCHALTPUNKT_KEY = "AUSSCHALTPUNKT"
    AUSSCHALTPUNKT_ERHOEHT_KEY = "AUSSCHALTPUNKT_ERHOEHT"  # Hier wird die Konstante definiert
    NACHTABSENKUNG_KEY = "NACHTABSENKUNG"
    # SolaxCloud-Daten aus der Konfiguration lesen
    TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
    SN = config["SolaxCloud"]["SN"]
except Exception as e:
    logging.error(f"Fehler beim Lesen der Konfiguration: {e}")
    exit() # Programm beenden, da keine gültige Konfiguration vorhanden ist



# Logging-Konfiguration
logging.basicConfig(
    filename="heizungssteuerung.log",  # Logdatei
    level=logging.DEBUG,  # Detaillierteste Protokollierungsstufe
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.info("Heizungssteuerung gestartet.")

# LCD und GPIO initialisieren
lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)
try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(GIO21_PIN, GPIO.OUT)
    GPIO.output(GIO21_PIN, GPIO.LOW)  # Kompressor ausschalten
except Exception as e:
    logging.error(f"Fehler bei der GPIO-Initialisierung: {e}")
    exit(1)

# Globale Variablen
last_api_call = None  # Zeitpunkt des letzten API-Aufrufs
last_api_data = None  # Zuletzt empfangene API-Daten
last_api_timestamp = None  # Zeitstempel der letzten API-Daten
kompressor_ein = False  # Status des Kompressors
start_time = None  # Startzeit des Kompressors
last_runtime = timedelta()  # Letzte Laufzeit des Kompressors
current_runtime = timedelta()  # Aktuelle Laufzeit des Kompressors
total_runtime_today = timedelta()  # Gesamtlaufzeit des Kompressors heute
last_day = datetime.now().date()  # Letzter Tag, an dem die Laufzeit berechnet wurde
aktueller_ausschaltpunkt = AUSSCHALTPUNKT  # Aktueller Ausschaltpunkt
last_shutdown_time = datetime.now()  # Zeitpunkt des letzten Ausschaltens
last_config_hash = None  # Hash-Wert der letzten Konfigurationsdatei
last_log_time = datetime.now() - timedelta(minutes=1)  # Zeitpunkt des letzten Log-Eintrags
last_kompressor_status = None  # Letzter Status des Kompressors
test_counter = 1  # Zähler für Testeinträge
last_update_id = None  # Initialisiere die Variable für die letzte Update-ID
previous_updates_len = 0  # Initialisiere die Variable für die vorherige Länge der Updates
urlaubsmodus_aktiv = False  # Status des Urlaubsmodus
original_einschaltpunkt = EINSCHALTPUNKT  # Ursprünglicher Einschaltpunkt
original_ausschaltpunkt = AUSSCHALTPUNKT  # Ursprünglicher Ausschaltpunkt

print("Variablen erstellt, Programm läuft")

def calculate_file_hash(file_path):
    """
    Berechnet den SHA-256-Hash-Wert einer Datei.
    :param file_path: Der Pfad zur Datei.
    :return: Der Hash-Wert als Hex-String.
    """
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        logging.error(f"Fehler beim Berechnen des Hash-Werts der Datei {file_path}: {e}")
        return None

def get_custom_keyboard():
    """
    Erstellt eine benutzerdefinierte Tastatur mit den verfügbaren Befehlen.
    """
    keyboard = [
        ["🌡️ Temperaturen"],  # Erste Zeile mit einer Schaltfläche
        ["📊 Status"],        # Zweite Zeile mit einer Schaltfläche
        ["🌴 Urlaub"],        # Dritte Zeile mit einer Schaltfläche
        ["🏠 Urlaub aus"],    # Vierte Zeile mit einer Schaltfläche
        ["🆘 Hilfe"]          # Fünfte Zeile mit einer Schaltfläche
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def send_welcome_message(chat_id):
    """
    Sendet eine Willkommensnachricht mit der benutzerdefinierten Tastatur.
    """
    try:
        message = (
            "🤖 Willkommen beim Heizungssteuerungs-Bot!\n\n"
            "Verwende die Tastatur, um Befehle auszuwählen."
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "reply_markup": get_custom_keyboard().to_json()  # Tastatur als JSON senden
        }
        response = requests.post(url, json=data)
        response.raise_for_status()
        logging.info("Willkommensnachricht mit Tastatur gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Willkommensnachricht: {e}")
        return False

def send_unknown_command_message(chat_id):
    """
    Sendet eine Nachricht für unbekannte Befehle und zeigt die Tastatur erneut an.
    """
    try:
        message = (
            "❌ Unbekannter Befehl.\n\n"
            "Verwende die Tastatur, um einen gültigen Befehl auszuwählen."
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": message,
            "reply_markup": get_custom_keyboard().to_json()  # Tastatur als JSON senden
        }
        response = requests.post(url, json=data)
        response.raise_for_status()
        logging.info("Nachricht für unbekannten Befehl gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Nachricht für unbekannten Befehl: {e}")
        return False
def send_telegram_message(message):
    """ Sendet eine Nachricht über Telegram.
    :param message: Die Nachricht, die gesendet werden soll. """
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message}
        response = requests.post(url, data=data)
        response.raise_for_status()  # Löst eine Ausnahme für ungültige Statuscodes aus (4xx oder 5xx)
        logging.info("Telegram-Nachricht gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {e}")
        return False

def send_help_message():
    """
    Sendet eine Nachricht mit den verfügbaren Befehlen und deren Beschreibungen.
    """
    try:
        message = (
            "🤖 Verfügbare Befehle:\n\n"
            "🌡️ *Temperaturen* – Sendet die aktuellen Temperaturen.\n"
            "📊 *Status* – Sendet den aktuellen Status (Temperaturen, Kompressorstatus, Laufzeiten, Sollwerte).\n"
            "🆘 *Hilfe* – Zeigt diese Nachricht an."
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}  # Markdown für Formatierung
        response = requests.post(url, data=data)
        response.raise_for_status()
        logging.info("Hilfe-Nachricht gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Hilfe-Nachricht: {e}")
        return False

# Senden der Willkommensnachricht mit Tastatur beim Start
now = datetime.now()
message = f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
if send_telegram_message(message):
    logging.info("Telegram-Nachricht erfolgreich gesendet.")
else:
    logging.error("Fehler beim Senden der Telegram-Nachricht.")

# Senden der Willkommensnachricht mit Tastatur
send_welcome_message(CHAT_ID)


def send_temperature_telegram(t_boiler_vorne, t_boiler_hinten, t_verd):
    try:
        message = f"🌡️ Aktuelle Temperaturen:\nKessel vorne: {t_boiler_vorne:.2f} °C\nKessel hinten: {t_boiler_hinten:.2f} °C\nVerdampfer: {t_verd:.2f} °C"
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message}
        response = requests.post(url, data=data)
        response.raise_for_status()  # Löst eine Ausnahme für ungültige Statuscodes aus
        logging.info("Telegram-Nachricht mit Temperaturen gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht mit Temperaturen: {e}")
        return False

def send_status_telegram(t_boiler_vorne, t_boiler_hinten, t_verd, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, einschaltpunkt, ausschaltpunkt):
    """
    Sendet den aktuellen Status (Temperaturen, Kompressorstatus, Laufzeiten, Sollwerte) als Telegram-Nachricht.
    """
    try:
        # Debugging: Aktuelle Werte in der Statusnachricht
        logging.debug(f"Statusnachricht: Einschaltpunkt={einschaltpunkt}, Ausschaltpunkt={ausschaltpunkt}")

        message = (
            f"🌡️ Aktuelle Temperaturen:\n"
            f"Boiler vorne: {t_boiler_vorne:.2f} °C\n"
            f"Boiler hinten: {t_boiler_hinten:.2f} °C\n"
            f"Verdampfer: {t_verd:.2f} °C\n\n"
            f"🔧 Kompressorstatus: {'EIN' if kompressor_status else 'AUS'}\n"
            f"⏱️ Aktuelle Laufzeit: {aktuelle_laufzeit}\n"
            f"⏳ Gesamtlaufzeit heute: {gesamtlaufzeit}\n\n"
            f"🎯 Sollwerte:\n"
            f"Einschaltpunkt: {einschaltpunkt} °C\n"  # Verwende übergebene Parameter
            f"Ausschaltpunkt: {ausschaltpunkt} °C"  # Verwende übergebene Parameter
        )
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": message}
        response = requests.post(url, data=data)
        response.raise_for_status()
        logging.info("Telegram-Nachricht mit Status gesendet.")
        return True
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht mit Status: {e}")
        return False

def get_telegram_updates(t_boiler_vorne, t_boiler_hinten, t_verd, offset=None):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": offset} if offset else {}
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()  # Löst eine Ausnahme für HTTP-Fehler aus
        updates = response.json().get('result', [])
        logging.debug(f"API-Antwort: {updates}")
        return updates
    except requests.exceptions.RequestException as e:
        logging.error(f"Fehler bei der Telegram-API-Abfrage: {e}")
        return None


def process_telegram_messages(t_boiler_vorne, t_boiler_hinten, t_verd, updates, last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit):
    global EINSCHALTPUNKT, AUSSCHALTPUNKT  # Zugriff auf globale Sollwerte
    if updates:
        for update in updates:
            message_text = update.get('message', {}).get('text')
            chat_id = update.get('message', {}).get('chat', {}).get('id')
            if message_text and chat_id:
                message_text = message_text.strip().lower()
                if message_text == "🌡️ temperaturen" or message_text == "temperaturen":
                    if t_boiler_vorne != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        send_temperature_telegram(t_boiler_vorne, t_boiler_hinten, t_verd)
                    else:
                        send_telegram_message("Fehler beim Abrufen der Temperaturen.")
                elif message_text == "📊 status" or message_text == "status":
                    if t_boiler_vorne != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        send_status_telegram(
                            t_boiler_vorne, t_boiler_hinten, t_verd, kompressor_status,
                            aktuelle_laufzeit, gesamtlaufzeit, EINSCHALTPUNKT, AUSSCHALTPUNKT  # Änderung hier
                        )
                    else:
                        send_telegram_message("Fehler beim Abrufen des Status.")
                elif message_text == "🆘 hilfe" or message_text == "hilfe":
                    send_help_message()
                elif message_text == "🌴 urlaub" or message_text == "urlaub":
                    aktivere_urlaubsmodus()
                elif message_text == "🏠 urlaub aus" or message_text == "urlaub aus":
                    deaktivere_urlaubsmodus()
                else:
                    send_unknown_command_message(chat_id)
            last_update_id = update['update_id'] + 1
            logging.debug(f"Aktualisierte last_update_id: {last_update_id}")
    return last_update_id

def aktivere_urlaubsmodus():
    """
    Aktiviert den Urlaubsmodus und verringert den Ein- und Ausschaltpunkt.
    """
    global urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt

    try:
        if not urlaubsmodus_aktiv:
            urlaubsmodus_aktiv = True
            original_einschaltpunkt = EINSCHALTPUNKT  # Speichere den ursprünglichen Wert
            original_ausschaltpunkt = AUSSCHALTPUNKT  # Speichere den ursprünglichen Wert

            # Debugging: Aktuelle Werte vor der Änderung
            logging.debug(f"Vor Urlaubsmodus: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")

            # Verringere den Ein- und Ausschaltpunkt um den konfigurierten Wert
            urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABsenkung", 6))  # Fallback-Wert 6
            EINSCHALTPUNKT -= urlaubsabsenkung
            AUSSCHALTPUNKT -= urlaubsabsenkung

            # Debugging: Aktuelle Werte nach der Änderung
            logging.debug(f"Nach Urlaubsmodus: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")

            logging.info(f"Urlaubsmodus aktiviert. Neue Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
            send_telegram_message(f"🌴 Urlaubsmodus aktiviert. Neue Werte:\nEinschaltpunkt: {EINSCHALTPUNKT} °C\nAusschaltpunkt: {AUSSCHALTPUNKT} °C")
    except KeyError as e:
        logging.error(f"Fehler beim Aktivieren des Urlaubsmodus: Abschnitt oder Schlüssel fehlt in der Konfiguration. {e}")
        send_telegram_message("❌ Fehler: Konfiguration für den Urlaubsmodus fehlt oder ist ungültig.")
    except ValueError as e:
        logging.error(f"Fehler beim Aktivieren des Urlaubsmodus: Ungültiger Wert in der Konfiguration. {e}")
        send_telegram_message("❌ Fehler: Ungültiger Wert für die Urlaubsabsenkung in der Konfiguration.")
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Aktivieren des Urlaubsmodus: {e}")
        send_telegram_message("❌ Unerwarteter Fehler beim Aktivieren des Urlaubsmodus.")

def deaktivere_urlaubsmodus():
    """
    Deaktiviert den Urlaubsmodus und stellt die ursprünglichen Ein- und Ausschaltpunkte wieder her.
    """
    global urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT, original_einschaltpunkt, original_ausschaltpunkt

    try:
        if urlaubsmodus_aktiv:
            # Debugging: Aktuelle Werte vor der Wiederherstellung
            logging.debug(f"Vor Deaktivierung: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")

            urlaubsmodus_aktiv = False
            EINSCHALTPUNKT = original_einschaltpunkt  # Stelle den ursprünglichen Wert wieder her
            AUSSCHALTPUNKT = original_ausschaltpunkt  # Stelle den ursprünglichen Wert wieder her

            # Debugging: Aktuelle Werte nach der Wiederherstellung
            logging.debug(f"Nach Deaktivierung: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")

            logging.info(f"Urlaubsmodus deaktiviert. Ursprüngliche Werte: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")
            send_telegram_message(f"🏠 Urlaubsmodus deaktiviert. Ursprüngliche Werte:\nEinschaltpunkt: {EINSCHALTPUNKT} °C\nAusschaltpunkt: {AUSSCHALTPUNKT} °C")
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Deaktivieren des Urlaubsmodus: {e}")
        send_telegram_message("❌ Unerwarteter Fehler beim Deaktivieren des Urlaubsmodus.")

def limit_temperature(temp):
    """Begrenzt die Temperatur auf maximal 70 Grad."""
    return min(temp, 70)

# CSV-Datei initialisieren
def initialize_csv(csv_file, fieldnames):
    """Initialisiert die CSV-Datei mit dem Header, falls sie nicht existiert."""
    try:
        with open(csv_file, 'a', newline='') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not os.path.exists(csv_file):
                writer.writeheader()
    except Exception as e:
        logging.error(f"Fehler bei der CSV-Initialisierung: {e}")
        # Weitere Fehlerbehandlung

csv_file = "heizungsdaten.csv"
fieldnames = ['Zeitstempel', 'T-Vorne', 'T-Hinten', 'T-Boiler', 'T-Verd', 'Kompressorstatus', 'Soll-Temperatur',
              'Ist-Temperatur', 'Aktuelle Laufzeit', 'Letzte Laufzeit', 'Gesamtlaufzeit', 'Solarleistung',
              'Netzbezug/Einspeisung', 'Hausverbrauch', 'Batterieleistung', 'SOC', 'Laufzeitunterschreitung',
              'Pausenzeitunterschreitung']


initialize_csv(csv_file, fieldnames)  # Funktion aufrufen

def load_config():
    config = configparser.ConfigParser()
    try:
        config.read("config.ini")
    except FileNotFoundError:
        print("Fehler: config.ini nicht gefunden!")
        exit()  # Oder eine andere Fehlerbehandlung
    except configparser.Error as e:
        print(f"Fehler beim Lesen von config.ini: {e}")
        exit()  # Oder eine andere Fehlerbehandlung
    return config

def reload_config():
    global AUSSCHALTPUNKT, AUSSCHALTPUNKT_ERHOEHT, EINSCHALTPUNKT, MIN_LAUFZEIT, MIN_PAUSE, TOKEN_ID, SN, VERDAMPFERTEMPERATUR, BOT_TOKEN, CHAT_ID, last_config_hash, urlaubsmodus_aktiv

    config_file = "config.ini"
    current_hash = calculate_file_hash(config_file)

    if last_config_hash is not None and current_hash != last_config_hash:
        logging.info("Konfigurationsdatei wurde geändert.")
        send_telegram_message("🔧 Konfigurationsdatei wurde geändert.")

    try:
        config.read(config_file)

        # Nur Werte aktualisieren, wenn Urlaubsmodus nicht aktiv ist
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

        MIN_LAUFZEIT = timedelta(minutes=MIN_LAUFZEIT_MINUTEN)
        MIN_PAUSE = timedelta(minutes=MIN_PAUSE_MINUTEN)

        TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
        SN = config["SolaxCloud"]["SN"]

        logging.info("Konfiguration erfolgreich neu geladen.")
    except FileNotFoundError:
        logging.error("Konfigurationsdatei config.ini nicht gefunden.")
    except KeyError as e:
        logging.error(f"Fehlender Schlüssel in der Konfigurationsdatei: {e}")
    except ValueError as e:
        logging.error(f"Ungültiger Wert in der Konfigurationsdatei: {e}")
    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}")

    last_config_hash = current_hash

def check_value(value, min_value, max_value, default_value, parameter_name, other_value=None, comparison=None, min_difference=None):
    """
    Überprüft, ob ein Wert innerhalb der Grenzwerte liegt und logisch konsistent ist.
    :param value: Der zu überprüfende Wert.
    :param min_value: Der Minimalwert.
    :param max_value: Der Maximalwert.
    :param default_value: Der Standardwert, falls der Wert ungültig ist.
    :param parameter_name: Der Name des Parameters (für Fehlermeldungen).
    :param other_value: Ein anderer Wert, mit dem verglichen wird (optional).
    :param comparison: Der Vergleichsoperator (z. B. "<", ">=", optional).
    :param min_difference: Der minimale Unterschied zwischen den Werten (optional).
    :return: Der gültige Wert (entweder der ursprüngliche Wert oder der Standardwert).
    """
    # Überprüfe Grenzwerte
    if not (min_value <= value <= max_value):
        logging.error(f"Ungültiger Wert für {parameter_name}: {value}. Muss zwischen {min_value} und {max_value} liegen. Verwende Standardwert: {default_value}.")
        value = default_value  # Verwende den Standardwert

    # Überprüfe logische Konsistenz des ursprünglichen oder Standardwerts
    if other_value is not None and comparison is not None:
        if comparison == "<" and not (value < other_value):
            logging.error(f"{parameter_name} ({value}) darf nicht größer oder gleich {other_value} sein. Verwende Standardwert: {default_value}.")
            value = default_value  # Verwende den Standardwert
        elif comparison == ">=" and not (value >= other_value):
            logging.error(f"{parameter_name} ({value}) darf nicht kleiner sein als {other_value}. Verwende Standardwert: {default_value}.")
            value = default_value  # Verwende den Standardwert

    # Überprüfe Mindestunterschied
    if other_value is not None and min_difference is not None:
        if abs(value - other_value) < min_difference:
            logging.error(f"Der Unterschied zwischen {parameter_name} ({value}) und {other_value} muss mindestens {min_difference} Grad betragen. Verwende Standardwert: {default_value}.")
            value = default_value  # Verwende den Standardwert

    # Überprüfe, ob der Standardwert selbst logisch konsistent ist
    if other_value is not None and comparison is not None:
        if comparison == "<" and not (value < other_value):
            logging.error(f"Standardwert für {parameter_name} ({value}) ist ungültig. Verwende sicheren Rückfallwert.")
            value = other_value - min_difference if min_difference is not None else other_value - 2  # Sichere Differenz
        elif comparison == ">=" and not (value >= other_value):
            logging.error(f"Standardwert für {parameter_name} ({value}) ist ungültig. Verwende sicheren Rückfallwert.")
            value = other_value + min_difference if min_difference is not None else other_value + 2  # Sichere Differenz

    return value


def is_nighttime(config):
    """Prüft, ob es Nacht ist, basierend auf den Zeiten in der Konfiguration.
    Args:        config (configparser.ConfigParser): Die Konfiguration.
    Returns:        bool: True, wenn es Nacht ist, False sonst.
    """
    now = datetime.now()

    try:
        start_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_START", "22:00")
        end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")

        start_hour, start_minute = map(int, start_time_str.split(':'))
        end_hour, end_minute = map(int, end_time_str.split(':'))

        start_time = datetime.datetime.combine(now.date(), datetime.time(start_hour, start_minute))
        end_time = datetime.datetime.combine(now.date(), datetime.time(end_hour, end_minute))

        if start_time > end_time:  # Fall, dass die Nacht über Mitternacht geht
            end_time = datetime.datetime.combine(now.date() + datetime.timedelta(days=1), datetime.time(end_hour, end_minute))

        return start_time <= now <= end_time

    except KeyError as e:
        logging.error(f"Fehlender Schlüssel in der Konfiguration: {e}")
        return False
    except ValueError as e:
        logging.error(f"Ungültiges Zeitformat in der Konfiguration: {e}")
        return False
    except Exception as e:
        logging.error(f"Unerwarteter Fehler in is_nighttime: {e}")
        return False


def calculate_shutdown_point(config, is_night, solax_data):
    """Berechnet den Ausschalttemperaturpunkt unter Berücksichtigung der Nachtabsenkung und des Solarüberschusses.

    Args:
        config (configparser.ConfigParser): Die Konfiguration.
        is_night (bool): True, wenn es Nacht ist, False sonst.
        solax_data (dict): Die Daten von SolaxCloud.

    Returns:
        int: Der berechnete Ausschalttemperaturpunkt.
    """
    try:
        nacht_reduction = int(config["Heizungssteuerung"][NACHTABSENKUNG_KEY]) if is_night else 0

        # Überprüfen auf Solarüberschuss (vereinfacht und lesbarer)
        solar_ueberschuss = (
            solax_data and
            (solax_data.get("batPower", 0) > 600 or
             (solax_data.get("soc", 0) > 95 and solax_data.get("feedinpower", 0) > 600))
        )

        if solar_ueberschuss:
            ausschaltpunkt = int(config["Heizungssteuerung"][AUSSCHALTPUNKT_ERHOEHT_KEY]) - nacht_reduction
        else:
            ausschaltpunkt = int(config["Heizungssteuerung"][AUSSCHALTPUNKT_KEY]) - nacht_reduction

        return ausschaltpunkt

    except (KeyError, ValueError) as e:  # Beide Fehler gleichzeitig behandeln
        logging.error(f"Fehler beim Lesen der Konfiguration: {e}")
        # Hier könntest du einen Standardwert zurückgeben oder eine andere Fehlerbehandlung implementieren.
        # Es ist wichtig, dass das Programm nicht abstürzt, wenn ein Wert fehlt!
        try:
            if is_night:
                return int(config["Heizungssteuerung"][AUSSCHALTPUNKT_KEY]) - int(config["Heizungssteuerung"][NACHTABSENKUNG_KEY])
            else:
                return int(config["Heizungssteuerung"][AUSSCHALTPUNKT_KEY])
        except (KeyError, ValueError) as e:
            logging.error(f"Fehler beim Verwenden des Standardwerts: {e}")
            return 50  # Ultimativer Standardwert

def adjust_shutdown_and_start_points(solax_data, config):
    global aktueller_ausschaltpunkt, aktueller_einschaltpunkt, urlaubsmodus_aktiv, EINSCHALTPUNKT, AUSSCHALTPUNKT

    is_night = is_nighttime(config)

    # Wenn Urlaubsmodus aktiv ist, verwende die angepassten Sollwerte direkt
    if urlaubsmodus_aktiv:
        aktueller_ausschaltpunkt = AUSSCHALTPUNKT
        aktueller_einschaltpunkt = EINSCHALTPUNKT
        logging.info(f"Urlaubsmodus aktiv: Ausschaltpunkt={aktueller_ausschaltpunkt}, Einschaltpunkt={aktueller_einschaltpunkt}")
        return

    # Standardlogik für Nicht-Urlaubsmodus
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
            nacht_reduction = int(config["Heizungssteuerung"][NACHTABSENKUNG_KEY]) if is_night else 0
            aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - nacht_reduction

        logging.info(f"Ausschaltpunkt: {aktueller_ausschaltpunkt}, Einschaltpunkt: {aktueller_einschaltpunkt}")
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Anpassen der Punkte: {e}")
        nacht_reduction = int(config["Heizungssteuerung"][NACHTABSENKUNG_KEY]) if is_night else 0
        aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - nacht_reduction
        aktueller_ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction

        # Fehlerbehandlung: Standardwerte oder andere Logik implementieren
        try:
            nacht_reduction = int(config["Heizungssteuerung"][NACHTABSENKUNG_KEY]) if is_night else 0
            aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - nacht_reduction  # Direkter String
            aktueller_ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction  # Direkter String
        except (KeyError, ValueError) as e:
            logging.error(f"Fehler beim Verwenden der Standardwerte: {e}")
            aktueller_einschaltpunkt = 40  # Standardwert
            aktueller_ausschaltpunkt = 50  # Standardwert

def read_temperature(sensor_id):
    device_file = os.path.join(BASE_DIR, sensor_id, "w1_slave")
    try:
        with open(device_file, "r") as f:
            lines = f.readlines()
            if lines[0].strip()[-3:] == "YES":
                temp_data = lines[1].split("=")[-1]
                temperature = float(temp_data) / 1000.0
                return temperature
            else:
                return None
    except Exception as e:
        print(f"Fehler beim Lesen des Sensors {sensor_id}: {e}")
        return None

def check_boiler_sensors(t_vorne, t_hinten, config):
    """Überprüft die Boiler-Sensoren auf Fehler (Fühlerfehler, Übertemperatur, Fühlerdifferenz).

    Args:
        t_vorne (float/int): Temperatur vorne.
        t_hinten (float/int): Temperatur hinten.
        config (configparser.ConfigParser): Die Konfiguration.

    Returns:
        tuple: Ein Tupel mit zwei Werten:
            - fehler (str or None): Eine Fehlermeldung oder None, wenn kein Fehler vorliegt.
            - is_overtemp (bool): True, wenn eine Übertemperatur vorliegt, False sonst.
    """
    try:
        ausschaltpunkt = int(config["Heizungssteuerung"][AUSSCHALTPUNKT_KEY])
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Lesen des Ausschaltszeitpunkts: {e}")
        ausschaltpunkt = 50  # Standardwert

    fehler = None  # Initialisiere fehler mit None
    is_overtemp = False  # Initialisiere is_overtemp mit False

    if t_vorne is None or t_hinten is None:
        fehler = "Fühlerfehler!"
    elif not isinstance(t_vorne, (int, float)) or not isinstance(t_hinten, (int, float)):
        fehler = "Fühlerfehler! (Ungültiger Datentyp)"
    elif t_vorne >= (ausschaltpunkt + 10) or t_hinten >= (ausschaltpunkt + 10):
        fehler = "Übertemperatur!"
        is_overtemp = True
    elif abs(t_vorne - t_hinten) > 10:
        fehler = "Fühlerdifferenz!"

    return fehler, is_overtemp

def set_kompressor_status(ein, force_off=False):
    global kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time, laufzeit_unterschreitung, pausenzeit_unterschreitung

    now = datetime.now()
    logging.debug(f"set_kompressor_status aufgerufen: ein={ein}, force_off={force_off}")

    if ein:
        if not kompressor_ein:
            # Berechne die vergangene Pausenzeit seit dem letzten Ausschalten
            pause_time = now - last_shutdown_time

            # Überprüfe, ob die Mindestpause eingehalten wurde
            if pause_time < MIN_PAUSE:
                pausenzeit_unterschreitung = str(MIN_PAUSE - pause_time)  # Berechne die fehlende Pausenzeit
                laufzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Laufzeitunterschreitung
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {MIN_PAUSE}).")
                return False, laufzeit_unterschreitung, pausenzeit_unterschreitung  # Rückgabe der Pausenzeitunterschreitung und Laufzeitunterschreitung (N/A)

            # Kompressor einschalten
            kompressor_ein = True
            start_time = now
            current_runtime = timedelta()
            laufzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Unterschreitung
            pausenzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Unterschreitung
            logging.info("Kompressor EIN. Startzeit gesetzt.")
        else:
            # Kompressor läuft bereits
            elapsed_time = now - start_time
            current_runtime = elapsed_time
            laufzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Unterschreitung
            pausenzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Unterschreitung
            logging.info(f"Kompressor läuft ({current_runtime}).")
    else:  # Ausschalten
        if kompressor_ein:
            elapsed_time = now - start_time
            if elapsed_time < MIN_LAUFZEIT and not force_off:
                laufzeit_unterschreitung = str(MIN_LAUFZEIT - elapsed_time)  # Berechne die fehlende Laufzeit
                pausenzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Pausenunterschreitung
                logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}, benötigt: {MIN_LAUFZEIT}).")
                return True, laufzeit_unterschreitung, pausenzeit_unterschreitung  # Rückgabe der Laufzeitunterschreitung und Pausenzeitunterschreitung (N/A)

            # Kompressor tatsächlich ausschalten
            kompressor_ein = False
            current_runtime = elapsed_time
            total_runtime_today += current_runtime
            last_runtime = current_runtime
            last_shutdown_time = now
            pausenzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Pausenunterschreitung
            laufzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Laufzeitunterschreitung
            logging.info(f"Kompressor AUS. Laufzeit: {elapsed_time}, Gesamtlaufzeit heute: {total_runtime_today}")

            start_time = None

        else:
            laufzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Laufzeitunterschreitung
            pausenzeit_unterschreitung = "N/A" # Setze auf "N/A" falls keine Pausenunterschreitung
            logging.info("Kompressor ist bereits aus.")

    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)
    return None, laufzeit_unterschreitung, pausenzeit_unterschreitung  # Keine Unterschreitung, aber die Werte werden immer zurückgegeben

def get_solax_data():
    """Ruft Daten von der Solax-API ab und speichert sie im Cache.

    Returns:
        dict: Die API-Daten oder None bei einem Fehler.
    """
    global last_api_call, last_api_data, last_api_timestamp

    now = datetime.now()

    # Cache-Prüfung (verbessert)
    if last_api_call and now - last_api_call < timedelta(minutes=5):
        logging.debug("Verwende zwischengespeicherte API-Daten.")
        return last_api_data

    try:
        params = {"tokenId": TOKEN_ID, "sn": SN}  # TOKEN_ID und SN sollten global oder als Argumente verfügbar sein
        response = requests.get(API_URL, params=params, timeout=10) # Timeout hinzufügen

        response.raise_for_status()  # HTTP-Fehler überprüfen (4xx oder 5xx)

        data = response.json()
        logging.debug(f"API-Antwort: {data}")

        if data.get("success"):  # Einfachere Prüfung auf Erfolg
            last_api_data = data.get("result")
            last_api_timestamp = now
            last_api_call = now
            return last_api_data
        else:
            error_message = data.get("exception", "Unbekannter Fehler")
            logging.error(f"API-Fehler: {error_message}")

            # Behandlung von Rate-Limiting (verbessert)
            if "exceed the maximum call threshold limit" in error_message or "Request calls within the current minute > threshold" in error_message:
                # Hier wird ein leeres Dictionary mit api_fehler = True zurückgegeben, um anzuzeigen, dass ein API-Fehler vorliegt, aber das Programm nicht abstürzen soll.
                last_api_data = {
                    "acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                    "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                    "api_fehler": True
                }
                last_api_timestamp = now
                last_api_call = now
                return last_api_data
            return None  # Kein Erfolg, aber auch kein Rate-Limit-Fehler

    except requests.exceptions.RequestException as e:  # Spezifischere Exception
        logging.error(f"Fehler bei der API-Anfrage: {e}")
        return None
    except (ValueError, KeyError) as e: # Fehler beim Parsen der JSON-Antwort
        logging.error(f"Fehler beim Verarbeiten der API-Antwort: {e}")
        return None
    except Exception as e: # Alle anderen Fehler
        logging.error(f"Unerwarteter Fehler beim Abrufen der API-Daten: {e}")
        return None

def is_data_old(timestamp):
    if timestamp and (datetime.now() - timestamp) > timedelta(minutes=15):
        return True
    return False

try:
    sensor_ids = glob.glob(BASE_DIR + "28*")
    sensor_ids = [os.path.basename(sensor_id) for sensor_id in sensor_ids]

    if len(sensor_ids) < 3:
        print("Es wurden weniger als 3 DS18B20-Sensoren gefunden!")
        exit(1)

    sensor_ids = sensor_ids[:3]

    while True:
        config = load_config()
        solax_data = get_solax_data()
        if solax_data is None:
            logging.warning("Keine gültigen Solax-Daten erhalten. Verwende Standardwerte.")
            solax_data = {
                "acpower": 0,
                "feedinpower": 0,
                "consumeenergy": 0,
                "batPower": 0,
                "soc": 0,
                "powerdc1": 0,
                "powerdc2": 0,
                "api_fehler": True
            }

        # Nur reload_config aufrufen, wenn sich die Konfiguration geändert hat
        current_hash = calculate_file_hash("config.ini")
        if last_config_hash != current_hash:
            reload_config()
            last_config_hash = current_hash


        adjust_shutdown_and_start_points(solax_data, config)

        # Temperaturen lesen
        try:
            temperatures = ["Fehler"] * 3
            for i, sensor_id in enumerate(sensor_ids):
                temp = read_temperature(sensor_id)
                if temp is not None:
                    temperatures[i] = temp
                logging.debug(f"Sensor {i + 1}: {temperatures[i]:.2f} °C")
        except Exception as e:
            error_message = f"Fehler bei der Temperaturmessung: {e}"
            logging.error(error_message)
            send_telegram_message(error_message)
            continue

        if temperatures[0] != "Fehler" and temperatures[1] != "Fehler":
            t_boiler = (temperatures[0] + temperatures[1]) / 2
        else:
            t_boiler = "Fehler"

        t_verd = temperatures[2] if temperatures[2] != "Fehler" else None
        logging.debug(f"T-Verd: {t_verd:.2f} °C")

        t_boiler_vorne = read_temperature("28-0bd6d4461d84")  # Beispiel, ersetze mit deiner Funktion
        t_boiler_hinten = read_temperature("28-445bd44686f4")  # Beispiel, ersetze mit deiner Funktion
        t_verd = read_temperature("28-213bd4460d65")  # Beispiel, ersetze mit deiner Funktion

        # Debugging: Aktuelle Werte in der Hauptschleife
        logging.debug(f"Hauptschleife: Einschaltpunkt={EINSCHALTPUNKT}, Ausschaltpunkt={AUSSCHALTPUNKT}")

        # Telegram-Updates abrufen
        updates = get_telegram_updates(t_boiler_vorne, t_boiler_hinten, t_verd, last_update_id)
        if updates:
            last_update_id = process_telegram_messages(
                t_boiler_vorne, t_boiler_hinten, t_verd, updates, last_update_id,
                kompressor_ein, str(current_runtime).split('.')[0], str(total_runtime_today).split('.')[0]
            )



        # Fehlerprüfung und Kompressorsteuerung
        fehler, is_overtemp = check_boiler_sensors(temperatures[0], temperatures[1], config)
        if fehler:
            lcd.clear()
            lcd.write_string(f"FEHLER: {fehler}")
            time.sleep(5)
            set_kompressor_status(False, force_off=True)
            continue

        # Kompressorsteuerung basierend auf Temperaturen
        if t_verd is not None and t_verd < VERDAMPFERTEMPERATUR:
            if kompressor_ein:
                set_kompressor_status(False)
                logging.info(f"Verdampfertemperatur unter {VERDAMPFERTEMPERATUR} Grad. Kompressor wurde ausgeschaltet.")
            logging.info(f"Verdampfertemperatur unter {VERDAMPFERTEMPERATUR} Grad. Kompressor bleibt ausgeschaltet.")
        elif t_boiler != "Fehler":
            logging.debug(f"T-Boiler: {t_boiler:.2f}, EINSCHALTPUNKT: {EINSCHALTPUNKT}, aktueller_ausschaltpunkt: {aktueller_ausschaltpunkt}")
            reload_config()  # Konfiguration neu laden, respektiert jetzt Urlaubsmodus
            if t_boiler < EINSCHALTPUNKT and not kompressor_ein:  # Änderung hier
                set_kompressor_status(True)
                logging.info(f"T-Boiler Temperatur unter {EINSCHALTPUNKT} Grad. Kompressor eingeschaltet.")
            elif t_boiler >= aktueller_ausschaltpunkt and kompressor_ein:
                _, laufzeit_unterschreitung, pausenzeit_unterschreitung = set_kompressor_status(False)
                logging.info(f"T-Boiler Temperatur {aktueller_ausschaltpunkt} Grad erreicht. Kompressor ausgeschaltet.")

        # Aktuelle Laufzeit aktualisieren
        if kompressor_ein and start_time:
            current_runtime = datetime.now() - start_time

        # Seite 1: Temperaturen anzeigen
        lcd.clear()
        lcd.write_string(f"T-Vorne: {temperatures[0]:.2f} C")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(f"T-Hinten: {temperatures[1]:.2f} C")
        lcd.cursor_pos = (2, 0)
        if t_boiler != "Fehler":
            lcd.write_string(f"T-Boiler: {t_boiler:.2f} C")
        else:
            lcd.write_string("T-Boiler: Fehler")
        lcd.cursor_pos = (3, 0)
        lcd.write_string(f"T-Verd: {temperatures[2]:.2f} C")

        time.sleep(5)

        # Seite 2: Kompressorstatus, Soll/Ist-Werte und Laufzeiten
        lcd.clear()
        lcd.write_string(f"Kompressor: {'EIN' if kompressor_ein else 'AUS'}")
        lcd.cursor_pos = (1, 0)
        if t_boiler != "Fehler":
            lcd.write_string(f"Soll:{aktueller_ausschaltpunkt:.1f}C Ist:{t_boiler:.1f}C")
        else:
            lcd.write_string("Soll:N/A Ist:Fehler")
        lcd.cursor_pos = (2, 0)
        if kompressor_ein:
            # Aktuelle Laufzeit anzeigen
            lcd.write_string(f"Aktuell: {str(current_runtime).split('.')[0]}")
        else:
            # Letzte Laufzeit anzeigen
            lcd.write_string(f"Letzte: {str(last_runtime).split('.')[0]}")
        lcd.cursor_pos = (3, 0)
        lcd.write_string(f"Gesamt: {str(total_runtime_today).split('.')[0]}")

        time.sleep(5)

        # Seite 3: SolaxCloud-Daten anzeigen
        lcd.clear()
        reload_config()
        if solax_data:
            acpower = solax_data.get("acpower", "N/A")  # AC-Leistung (Solar)
            feedinpower = solax_data.get("feedinpower", "N/A")  # Einspeisung/Bezug
            consumeenergy = solax_data.get("consumeenergy", "N/A")  # Hausverbrauch
            batPower = solax_data.get("batPower", "N/A")  # Batterieladung
            powerdc1 = solax_data.get("powerdc1", 0)  # DC-Leistung 1
            powerdc2 = solax_data.get("powerdc2", 0)  # DC-Leistung 2
            solar = powerdc1 + powerdc2  # Gesamte Solarleistung
            soc = solax_data.get("soc", "N/A")  # SOC (State of Charge)
            old_suffix = " ALT" if is_data_old(last_api_timestamp) else ""

            # Zeile 1: Solarleistung
            lcd.write_string(f"Solar: {solar} W{old_suffix}")

            # Zeile 2: Einspeisung/Bezug
            if feedinpower != "N/A":
                if float(feedinpower) >= 0:
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"in Netz: {feedinpower} W{old_suffix}")
                else:
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"vom Netz: {-float(feedinpower)} W{old_suffix}")
            else:
                lcd.cursor_pos = (1, 0)
                lcd.write_string(f"Netz: N/A{old_suffix}")

            # Zeile 3: Hausverbrauch
            lcd.cursor_pos = (2, 0)
            if consumeenergy != "N/A":
                # Formatierung des Hausverbrauchs
                verbrauch_text = f"Verbrauch: {consumeenergy} W{old_suffix}"
                if len(verbrauch_text) > 20:
                    # Entferne Nachkommastellen, wenn der Text zu lang ist
                    verbrauch_text = f"Verbrauch: {int(float(consumeenergy))} W{old_suffix}"
                lcd.write_string(verbrauch_text)
            else:
                lcd.write_string(f"Verbrauch: N/A{old_suffix}")

            # Zeile 4: Batterieladung und SOC
            lcd.cursor_pos = (3, 0)
            batPower_str = f"{batPower}W"
            soc_str = f"SOC:{soc}%"
            line4_text = f"Bat:{batPower_str},{soc_str}"

            # Falls die Zeile zu lang ist, entferne Nachkommastellen
            if len(line4_text) > 20:
                batPower_str = f"{int(float(batPower))}W"
                soc_str = f"SOC:{int(float(soc))}%"
                line4_text = f"Bat:{batPower_str},{soc_str}"

            lcd.write_string(line4_text)
        else:
            lcd.write_string("Fehler beim")
            lcd.cursor_pos = (1, 0)
            lcd.write_string("Abrufen der")
            lcd.cursor_pos = (2, 0)
            lcd.write_string("Solax-Daten")
            lcd.cursor_pos = (3, 0)
            lcd.write_string("API-Fehler")

        time.sleep(5)

        # Logging-Bedingungen prüfen
        now = datetime.now()
        time_diff = None
        if last_log_time:
            time_diff = now - last_log_time

        # Debug-Meldungen für die Logging-Bedingung
        logging.debug(f"last_log_time: {last_log_time}")
        logging.debug(f"time_diff: {time_diff}")
        logging.debug(f"kompressor_ein: {kompressor_ein}")
        logging.debug(f"last_kompressor_status: {last_kompressor_status}")

        # Sofortiges Logging bei Kompressorstatusänderung ODER minütliches Logging
        if (last_log_time is None or time_diff >= timedelta(minutes=1) or
                kompressor_ein != last_kompressor_status):

            logging.debug("CSV-Schreibbedingung erfüllt")

            # Daten für CSV-Datei sammeln
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            t_vorne = temperatures[0] if temperatures[0] != "Fehler" else "N/A"
            t_hinten = temperatures[1] if temperatures[1] != "Fehler" else "N/A"
            t_boiler_wert = t_boiler if t_boiler != "Fehler" else "N/A"
            t_verd = temperatures[2] if temperatures[2] != "Fehler" else "N/A"
            kompressor_status = "EIN" if kompressor_ein else "AUS"
            soll_temperatur = aktueller_ausschaltpunkt
            ist_temperatur = t_boiler_wert
            aktuelle_laufzeit = str(current_runtime).split('.')[0] if kompressor_ein else "N/A"
            letzte_laufzeit = str(last_runtime).split('.')[0] if not kompressor_ein and last_runtime else "N/A"
            gesamtlaufzeit = str(total_runtime_today).split('.')[0]

            # Solax Daten nur falls vorhanden, sonst "N/A"
            solar = solax_data.get("powerdc1", 0) + solax_data.get("powerdc2", 0) if solax_data else "N/A"
            netz = solax_data.get("feedinpower", "N/A") if solax_data else "N/A"
            verbrauch = solax_data.get("consumeenergy", "N/A") if solax_data else "N/A"
            batterie = solax_data.get("batPower", "N/A") if solax_data else "N/A"
            soc = solax_data.get("soc", "N/A") if solax_data else "N/A"

            # Neue Daten: Laufzeit- und Pausenzeitunterschreitung
            laufzeit_unterschreitung = laufzeit_unterschreitung if 'laufzeit_unterschreitung' in globals() else "N/A"
            pausenzeit_unterschreitung = pausenzeit_unterschreitung if 'pausenzeit_unterschreitung' in globals() else "N/A"

            # Daten in CSV-Datei schreiben
            try:
                with open(csv_file, 'a', newline='') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writerow({
                        'Zeitstempel': now_str,
                        'T-Vorne': t_vorne,
                        'T-Hinten': t_hinten,
                        'T-Boiler': t_boiler_wert,
                        'T-Verd': t_verd,
                        'Kompressorstatus': kompressor_status,
                        'Soll-Temperatur': soll_temperatur,
                        'Ist-Temperatur': ist_temperatur,
                        'Aktuelle Laufzeit': aktuelle_laufzeit,
                        'Letzte Laufzeit': letzte_laufzeit,
                        'Gesamtlaufzeit': gesamtlaufzeit,
                        'Solarleistung': solar,
                        'Netzbezug/Einspeisung': netz,
                        'Hausverbrauch': verbrauch,
                        'Batterieleistung': batterie,
                        'SOC': soc,
                        'Laufzeitunterschreitung': laufzeit_unterschreitung,  # Neue Spalte
                        'Pausenzeitunterschreitung': pausenzeit_unterschreitung  # Neue Spalte
                    })
                logging.info(f"Daten in CSV-Datei geschrieben: {now_str}")
            except Exception as e:
                logging.error(f"Fehler beim Schreiben in die CSV-Datei: {e}")

            # Logging-Zeit und Kompressorstatus aktualisieren
            last_log_time = now
            last_kompressor_status = kompressor_ein

        time.sleep(1)  # Kurze Pause, um die CPU-Last zu reduzieren
        print("Druchgang durchlaufen")

except KeyboardInterrupt:
    logging.info("Programm beendet.")
finally:
    GPIO.cleanup()
    lcd.close()
    logging.info("Heizungssteuerung beendet.")