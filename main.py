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
    "hinten": "28-445bd44686f4",
    "mittig": "28-6977d446424a",
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
last_api_call = None
last_api_data = None
last_api_timestamp = None
kompressor_ein = False
start_time = None
last_runtime = timedelta()
current_runtime = timedelta()
total_runtime_today = timedelta()
last_day = datetime.now().date()
last_shutdown_time = datetime.now()
last_config_hash = None
last_log_time = datetime.now() - timedelta(minutes=1)
last_kompressor_status = None
last_update_id = None
urlaubsmodus_aktiv = False
pressure_error_sent = False
aktueller_ausschaltpunkt = AUSSCHALTPUNKT
aktueller_einschaltpunkt = AUSSCHALTPUNKT - TEMP_OFFSET  # Einschaltpunkt basiert auf Offset
original_ausschaltpunkt = AUSSCHALTPUNKT
original_einschaltpunkt = AUSSCHALTPUNKT - TEMP_OFFSET  # Konsistenz im Urlaubsmodus
ausschluss_grund = None  # Grund, warum der Kompressor nicht läuft (z.B. "Zu kurze Pause")
t_boiler = None
solar_ueberschuss_aktiv = False
lcd = None
last_pressure_error_time = None  # Zeitpunkt des letzten Druckfehlers
PRESSURE_ERROR_DELAY = timedelta(minutes=5)  # 5 Minuten Verzögerung
last_pressure_state = None
csv_lock = asyncio.Lock()


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
        self.ausschluss_grund = None
        self.last_config_hash = calculate_file_hash("config.ini")
        self.kompressor_ein = False
        self.current_runtime = timedelta()
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
async def get_solax_data(session):
    global last_api_call, last_api_data, last_api_timestamp
    now = datetime.now()
    if last_api_call and now - last_api_call < timedelta(minutes=5):
        logging.debug("Verwende zwischengespeicherte API-Daten.")
        return last_api_data

    max_retries = 3
    retry_delay = 5  # Sekunden zwischen Wiederholungen

    for attempt in range(max_retries):
        try:
            params = {"tokenId": TOKEN_ID, "sn": SN}
            async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
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
            logging.error(f"Fehler bei der API-Anfrage (Versuch {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Maximale Wiederholungen erreicht, verwende Fallback-Daten.")
                # Fallback-Werte, wenn keine API-Daten verfügbar sind
                fallback_data = {
                    "acpower": 0,
                    "feedinpower": 0,
                    "batPower": 0,
                    "soc": 0,
                    "powerdc1": 0,
                    "powerdc2": 0,
                    "consumeenergy": 0,
                    "api_fehler": True  # Kennzeichnung, dass Fallback-Daten verwendet wurden
                }
                # Keine Telegram-Nachricht mehr senden
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
                    "Zeitstempel,T_Oben,T_Hinten,T_Mittig,T_Boiler,T_Verd,Kompressor,"  # T_Mittig hinzugefügt
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


def check_pressure():
    """Prüft den Druckschalter (GPIO 17) mit Pull-up und NO-Schalter."""
    global last_pressure_state
    raw_value = GPIO.input(PRESSURE_SENSOR_PIN)
    pressure_ok = raw_value == GPIO.LOW  # LOW = Druck OK, HIGH = Fehler

    # Logging nur bei erstem Aufruf oder Änderung des Status
    if last_pressure_state is None or last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {raw_value} -> {'OK' if pressure_ok else 'Fehler'} (LOW=OK, HIGH=Fehler)")
        last_pressure_state = pressure_ok  # Aktualisiere den letzten Status

    return pressure_ok


def check_boiler_sensors(t_oben, t_hinten, config):
    """Prüft die Boiler-Sensoren auf Fehler."""
    try:
        ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
    except (KeyError, ValueError):
        ausschaltpunkt = 50
        logging.warning(f"Ausschaltpunkt nicht gefunden, verwende Standard: {ausschaltpunkt}")
    fehler = None
    is_overtemp = False
    if t_oben is None or t_hinten is None:
        fehler = "Fühlerfehler!"
        logging.error(f"Fühlerfehler erkannt: oben={t_oben}, hinten={t_hinten}")
    elif t_oben >= (ausschaltpunkt + 10) or t_hinten >= (ausschaltpunkt + 10):
        fehler = "Übertemperatur!"
        is_overtemp = True
        logging.error(f"Übertemperatur erkannt: oben={t_oben}, hinten={t_hinten}, Grenze={ausschaltpunkt + 10}")
    elif abs(t_oben - t_hinten) > 50:
        fehler = "Fühlerdifferenz!"
        logging.warning(
            f"Fühlerdifferenz erkannt: oben={t_oben}, hinten={t_hinten}, Differenz={abs(t_oben - t_hinten)}")
    return fehler, is_overtemp


def set_kompressor_status(ein, force_off=False):
    """Setzt den Status des Kompressors (EIN/AUS) und überprüft den GPIO-Pin.

    Args:
        ein (bool): True zum Einschalten, False zum Ausschalten.
        force_off (bool): Erzwingt das Ausschalten unabhängig von Mindestlaufzeit.

    Returns:
        bool or None: False, wenn Einschalten fehlschlägt; True, wenn Ausschalten verweigert wird; None bei Erfolg.
    """
    global kompressor_ein, start_time, current_runtime, total_runtime_today, last_runtime, last_shutdown_time, ausschluss_grund
    now = datetime.now()
    if ein:
        if not kompressor_ein:
            pause_time = now - last_shutdown_time
            if pause_time < MIN_PAUSE and not force_off:
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {MIN_PAUSE})")
                ausschluss_grund = f"Zu kurze Pause ({pause_time.total_seconds():.1f}s < {MIN_PAUSE.total_seconds():.1f}s)"
                return False
            kompressor_ein = True
            start_time = now
            current_runtime = timedelta()
            ausschluss_grund = None  # Kein Ausschlussgrund, wenn Kompressor läuft
            logging.info(f"Kompressor EIN geschaltet. Startzeit: {start_time}")
        else:
            current_runtime = now - start_time
            logging.debug(f"Kompressor läuft bereits, aktuelle Laufzeit: {current_runtime}")
    else:
        if kompressor_ein:
            elapsed_time = now - start_time
            if elapsed_time < MIN_LAUFZEIT and not force_off:
                logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}, benötigt: {MIN_LAUFZEIT})")
                return True
            kompressor_ein = False
            current_runtime = elapsed_time
            total_runtime_today += current_runtime
            last_runtime = current_runtime
            last_shutdown_time = now
            start_time = None
            logging.info(
                f"Kompressor AUS geschaltet. Laufzeit: {elapsed_time}, Gesamtlaufzeit heute: {total_runtime_today}")
        else:
            logging.debug("Kompressor bereits ausgeschaltet")

    # GPIO-Status setzen und prüfen
    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)
    actual_state = GPIO.input(GIO21_PIN)  # Annahme: Pin kann als Eingang gelesen werden
    if actual_state != (GPIO.HIGH if ein else GPIO.LOW):
        logging.error(f"GPIO-Fehler: Kompressor-Status sollte {'EIN' if ein else 'AUS'} sein, ist aber {actual_state}")
        # Optional: Hier könnte man weitere Maßnahmen treffen (z.B. Programmabbruch oder erneuter Versuch)

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
        solax_data = await get_solax_data(session) or {
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
def adjust_shutdown_and_start_points(solax_data, config):
    global aktueller_ausschaltpunkt, aktueller_einschaltpunkt, solar_ueberschuss_aktiv
    if not hasattr(adjust_shutdown_and_start_points, "last_night"):
        adjust_shutdown_and_start_points.last_night = None
        adjust_shutdown_and_start_points.last_config_hash = None
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = None
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = None

    is_night = is_nighttime(config)
    current_config_hash = calculate_file_hash("config.ini")

    if (is_night == adjust_shutdown_and_start_points.last_night and
            current_config_hash == adjust_shutdown_and_start_points.last_config_hash):
        return

    adjust_shutdown_and_start_points.last_night = is_night
    adjust_shutdown_and_start_points.last_config_hash = current_config_hash

    old_ausschaltpunkt = aktueller_ausschaltpunkt
    old_einschaltpunkt = aktueller_einschaltpunkt

    ausschaltpunkt, einschaltpunkt = calculate_shutdown_point(config, is_night, solax_data)
    aktueller_ausschaltpunkt = ausschaltpunkt
    aktueller_einschaltpunkt = einschaltpunkt

    MIN_EINSCHALTPUNKT = 20
    if aktueller_einschaltpunkt < MIN_EINSCHALTPUNKT:
        aktueller_einschaltpunkt = MIN_EINSCHALTPUNKT
        logging.warning(f"Einschaltpunkt auf Mindestwert {MIN_EINSCHALTPUNKT} gesetzt.")

    if (aktueller_ausschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt or
            aktueller_einschaltpunkt != adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt):
        logging.info(
            f"Sollwerte angepasst: Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}, "
            f"Solarüberschuss_aktiv={solar_ueberschuss_aktiv}"
        )
        adjust_shutdown_and_start_points.last_aktueller_ausschaltpunkt = aktueller_ausschaltpunkt
        adjust_shutdown_and_start_points.last_aktueller_einschaltpunkt = aktueller_einschaltpunkt


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
async def display_task():
    """Separate Task für Display-Updates, entkoppelt von der Hauptschleife."""
    global lcd
    async with aiohttp.ClientSession() as session:
        while True:
            if lcd is None:
                logging.debug("LCD nicht verfügbar, überspringe Display-Update")
                await asyncio.sleep(5)
                continue

            try:
                # Seite 1: Temperaturen
                t_boiler_oben = await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"])
                t_boiler_hinten = await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                t_boiler = (
                                   t_boiler_oben + t_boiler_hinten) / 2 if t_boiler_oben is not None and t_boiler_hinten is not None else "Fehler"
                pressure_ok = await asyncio.to_thread(check_pressure)

                lcd.clear()
                if not pressure_ok:
                    lcd.write_string("FEHLER: Druck zu niedrig")
                    logging.error(f"Display zeigt Druckfehler: Druckschalter={pressure_ok}")
                else:
                    # Prüfe Typ und formatiere entsprechend
                    oben_str = f"{t_boiler_oben:.2f}" if isinstance(t_boiler_oben, (int, float)) else "Fehler"
                    hinten_str = f"{t_boiler_hinten:.2f}" if isinstance(t_boiler_hinten, (int, float)) else "Fehler"
                    boiler_str = f"{t_boiler:.2f}" if isinstance(t_boiler, (int, float)) else "Fehler"
                    verd_str = f"{t_verd:.2f}" if isinstance(t_verd, (int, float)) else "Fehler"

                    lcd.write_string(f"T-Oben: {oben_str} C")
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"T-Hinten: {hinten_str} C")
                    lcd.cursor_pos = (2, 0)
                    lcd.write_string(f"T-Boiler: {boiler_str} C")
                    lcd.cursor_pos = (3, 0)
                    lcd.write_string(f"T-Verd: {verd_str} C")
                    logging.debug(
                        f"Display-Seite 1 aktualisiert: oben={oben_str}, hinten={hinten_str}, boiler={boiler_str}, verd={verd_str}")
                await asyncio.sleep(5)

                # Seite 2: Kompressorstatus
                lcd.clear()
                lcd.write_string(f"Kompressor: {'EIN' if kompressor_ein else 'AUS'}")
                lcd.cursor_pos = (1, 0)
                boiler_str = f"{t_boiler:.1f}" if isinstance(t_boiler, (int, float)) else "Fehler"
                lcd.write_string(f"Soll:{aktueller_ausschaltpunkt:.1f}C Ist:{boiler_str}C")
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

            except Exception as e:
                error_msg = f"Fehler beim Display-Update: {e}"
                logging.error(error_msg)
                await send_telegram_message(session, CHAT_ID, error_msg)
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



async def get_boiler_temperature_history(session, hours, state):
    logging.debug(f"get_boiler_temperature_history aufgerufen mit hours={hours}, state.bot_token={state.bot_token}")
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    global UNTERER_FUEHLER_MIN, UNTERER_FUEHLER_MAX
    try:
        # Listen für Daten
        temp_oben = []
        temp_hinten = []
        temp_mittig = []
        einschaltpunkte = []
        ausschaltpunkte = []
        kompressor_status = []
        solar_ueberschuss_periods = []

        # CSV-Datei asynchron lesen
        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            lines = await csvfile.readlines()
            lines = lines[1:][::-1]  # Header überspringen und umkehren (neueste zuerst)

            for line in lines:
                parts = line.strip().split(',')
                if len(parts) >= 13:  # Mindestens bis ConsumeEnergy
                    while len(parts) < 19:
                        parts.append("N/A")

                    timestamp_str = parts[0].strip()
                    timestamp_str = ''.join(c for c in timestamp_str if c.isprintable())

                    try:
                        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        t_oben, t_hinten, t_mittig = parts[1], parts[2], parts[3]
                        kompressor = parts[6]
                        einschaltpunkt = parts[14] if parts[14].strip() and parts[14] not in ("N/A", "Fehler") else "42"
                        ausschaltpunkt = parts[15] if parts[15].strip() and parts[15] not in ("N/A", "Fehler") else "45"
                        solar_ueberschuss = parts[16] if parts[16].strip() and parts[16] not in ("N/A", "Fehler") else "0"
                        power_source = parts[18] if parts[18].strip() and parts[18] not in ("N/A", "Fehler") else "Unbekannt"

                        if not (t_oben.strip() and t_oben not in ("N/A", "Fehler")) or \
                           not (t_hinten.strip() and t_hinten not in ("N/A", "Fehler")) or \
                           not (t_mittig.strip() and t_mittig not in ("N/A", "Fehler")):
                            logging.warning(f"Übersprungene Zeile wegen fehlender Temperaturen: {line.strip()}")
                            continue

                        temp_oben.append((timestamp, float(t_oben)))
                        temp_hinten.append((timestamp, float(t_hinten)))
                        temp_mittig.append((timestamp, float(t_mittig)))
                        einschaltpunkte.append((timestamp, float(einschaltpunkt)))
                        ausschaltpunkte.append((timestamp, float(ausschaltpunkt)))
                        kompressor_status.append((timestamp, 1 if kompressor == "EIN" else 0, power_source))
                        if int(solar_ueberschuss) == 1:
                            solar_ueberschuss_periods.append((timestamp, UNTERER_FUEHLER_MIN))
                            solar_ueberschuss_periods.append((timestamp, UNTERER_FUEHLER_MAX))
                    except ValueError as e:
                        logging.error(f"Fehler beim Parsen der Zeile: {line.strip()}, Zeitstempel: '{timestamp_str}', Fehler: {e}")
                        continue
                else:
                    logging.warning(f"Zeile mit unzureichenden Spalten übersprungen: {line.strip()}")

        # Zeitfenster definieren
        now = datetime.now()
        time_ago = now - timedelta(hours=hours)
        target_points = 50
        total_seconds = hours * 3600
        target_interval = total_seconds / (target_points - 1) if target_points > 1 else total_seconds

        # Filtere Daten
        filtered_oben = [(ts, val) for ts, val in temp_oben if ts >= time_ago]
        filtered_hinten = [(ts, val) for ts, val in temp_hinten if ts >= time_ago]
        filtered_mittig = [(ts, val) for ts, val in temp_mittig if ts >= time_ago]
        filtered_einschalt = [(ts, val) for ts, val in einschaltpunkte if ts >= time_ago]
        filtered_ausschalt = [(ts, val) for ts, val in ausschaltpunkte if ts >= time_ago]
        filtered_kompressor = [(ts, val, ps) for ts, val, ps in kompressor_status if ts >= time_ago]
        filtered_solar_ueberschuss = [(ts, val) for ts, val in solar_ueberschuss_periods if ts >= time_ago]

        # Sampling-Funktion
        def sample_data(data, interval, num_points):
            if not data:
                return []
            if len(data) <= num_points:
                return data[::-1]
            sampled = []
            last_added = None
            for item in data:
                ts = item[0]
                if last_added is None or (last_added - ts).total_seconds() >= interval:
                    sampled.append(item)
                    last_added = ts
                if len(sampled) >= num_points:
                    break
            return sampled[::-1]

        sampled_oben = sample_data(filtered_oben, target_interval, target_points)
        sampled_hinten = sample_data(filtered_hinten, target_interval, target_points)
        sampled_mittig = sample_data(filtered_mittig, target_interval, target_points)
        sampled_einschalt = sample_data(filtered_einschalt, target_interval, target_points)
        sampled_ausschalt = sample_data(filtered_ausschalt, target_interval, target_points)
        sampled_kompressor = sample_data(filtered_kompressor, target_interval, target_points)
        sampled_solar_min = sample_data(
            [(ts, val) for ts, val in filtered_solar_ueberschuss if val == UNTERER_FUEHLER_MIN], target_interval,
            target_points)
        sampled_solar_max = sample_data(
            [(ts, val) for ts, val in filtered_solar_ueberschuss if val == UNTERER_FUEHLER_MAX], target_interval,
            target_points)

        # Diagramm erstellen
        plt.figure(figsize=(12, 6))

        # Farben basierend auf PowerSource definieren
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "Unbekannt": "gray"
        }

        # Kompressorstatus als Hintergrundfläche mit variablen Farben
        if sampled_kompressor:
            timestamps_komp = [item[0] for item in sampled_kompressor]
            komp_vals = [item[1] for item in sampled_kompressor]
            power_sources = [item[2] for item in sampled_kompressor]

            current_start_idx = 0
            for i in range(1, len(timestamps_komp)):
                if power_sources[i] != power_sources[current_start_idx] or i == len(timestamps_komp) - 1:
                    segment_timestamps = timestamps_komp[current_start_idx:i + 1]
                    segment_vals = komp_vals[current_start_idx:i + 1]
                    color = color_map.get(power_sources[current_start_idx], "gray")
                    plt.fill_between(segment_timestamps, 0, max(UNTERER_FUEHLER_MAX, AUSSCHALTPUNKT_ERHOEHT) + 5,
                                     where=[val == 1 for val in segment_vals], color=color, alpha=0.2,
                                     label=f"Kompressor EIN ({power_sources[current_start_idx]})" if current_start_idx == 0 else None)
                    current_start_idx = i

            handles, labels = plt.gca().get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            plt.legend(by_label.values(), by_label.keys(), loc="lower left")

        # Temperaturen und Sollwerte plotten
        if sampled_oben:
            timestamps_oben, t_oben_vals = zip(*sampled_oben)
            plt.plot(timestamps_oben, t_oben_vals, label="T_Oben", marker="o", color="blue")
        if sampled_hinten:
            timestamps_hinten, t_hinten_vals = zip(*sampled_hinten)
            plt.plot(timestamps_hinten, t_hinten_vals, label="T_Hinten", marker="x", color="red")
        if sampled_mittig:
            timestamps_mittig, t_mittig_vals = zip(*sampled_mittig)
            plt.plot(timestamps_mittig, t_mittig_vals, label="T_Mittig", marker="^", color="purple")
        if sampled_einschalt:
            timestamps_einschalt, einschalt_vals = zip(*sampled_einschalt)
            plt.plot(timestamps_einschalt, einschalt_vals, label="Einschaltpunkt (historisch)", linestyle='--',
                     color="green")
        if sampled_ausschalt:
            timestamps_ausschalt, ausschalt_vals = zip(*sampled_ausschalt)
            plt.plot(timestamps_ausschalt, ausschalt_vals, label="Ausschaltpunkt (historisch)", linestyle='--',
                     color="orange")

        if sampled_solar_min:
            timestamps_min, min_vals = zip(*sampled_solar_min)
            plt.plot(timestamps_min, [state.aktueller_einschaltpunkt] * len(timestamps_min),
                     color='purple', linestyle='-.',
                     label=f'Einschaltpunkt ({state.aktueller_einschaltpunkt}°C)')
        if sampled_solar_max:
            timestamps_max, max_vals = zip(*sampled_solar_max)
            plt.plot(timestamps_max, [state.aktueller_ausschaltpunkt] * len(timestamps_max),
                     color='cyan', linestyle='-.',
                     label=f'Ausschaltpunkt ({state.aktueller_ausschaltpunkt}°C)')

        plt.xlim(time_ago, now)
        plt.ylim(0, max(UNTERER_FUEHLER_MAX, AUSSCHALTPUNKT_ERHOEHT) + 5)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf (letzte {hours} Stunden)")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", CHAT_ID)
        form.add_field("caption",
                       f"📈 Verlauf {hours}h (T_Oben = blau, T_Hinten = rot, T_Mittig = lila, Kompressor EIN: grün=PV, gelb=Batterie, rot=Netz)")
        form.add_field("photo", buf, filename="temperature_graph.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Temperaturdiagramm für {hours}h mit Kompressorstatus gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen oder Senden des Temperaturverlaufs ({hours}h): {e}")
        await send_telegram_message(session, CHAT_ID, f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}", state.bot_token)

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
                         state.kompressor_ein, str(state.current_runtime).split('.')[0],
                         str(state.total_runtime_today).split('.')[0], config, get_solax_data, state,
                         get_boiler_temperature_history, get_runtime_bar_chart, is_nighttime)
        )
    except TypeError as e:
        logging.error(f"TypeError beim Starten von telegram_task: {e}", exc_info=True)
        raise

    display_task_handle = asyncio.create_task(display_task())

    last_cycle_time = datetime.now()
    last_compressor_off_time = None  # Zeitpunkt des letzten Ausschaltens
    watchdog_warning_count = 0
    WATCHDOG_MAX_WARNINGS = 3
    PRESSURE_ERROR_DELAY = timedelta(minutes=5)

    try:
        while True:
            try:
                now = datetime.now()
                should_check_day = (state.last_log_time is None or (now - state.last_log_time) >= timedelta(minutes=1))
                if should_check_day and now.date() != state.last_day:
                    logging.info(f"Neuer Tag erkannt: {now.date()}. Setze Gesamtlaufzeit zurück.")
                    state.total_runtime_today = timedelta()
                    state.last_day = now.date()

                current_hash = calculate_file_hash("config.ini")
                if state.last_config_hash != current_hash:
                    await reload_config(session, state, config)
                    state.last_config_hash = current_hash

                try:
                    solax_data = await get_solax_data(session) or {
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
                    state.aktueller_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT_ERHOEHT", 50))

                t_boiler_oben = await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"])
                t_boiler_hinten = await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"])
                t_boiler_mittig = await asyncio.to_thread(read_temperature, SENSOR_IDS["mittig"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                t_boiler = (
                    t_boiler_oben + t_boiler_hinten) / 2 if t_boiler_oben is not None and t_boiler_hinten is not None else "Fehler"
                pressure_ok = await asyncio.to_thread(check_pressure)

                if not pressure_ok:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info("Kompressor ausgeschaltet (Druckschalter offen).")
                    state.ausschluss_grund = "Druckschalter offen"
                    await asyncio.sleep(2)
                    continue

                if state.pressure_error_sent and (
                        state.last_pressure_error_time is None or (now - state.last_pressure_error_time) >= PRESSURE_ERROR_DELAY):
                    await send_telegram_message(session, config["Telegram"]["CHAT_ID"],
                                              "✅ Druckschalter wieder normal.", state.bot_token)
                    state.pressure_error_sent = False
                    state.last_pressure_error_time = None

                fehler, is_overtemp = check_boiler_sensors(t_boiler_oben, t_boiler_hinten, config)
                if fehler:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info(f"Kompressor ausgeschaltet (Sensorfehler: {fehler}).")
                    state.ausschluss_grund = fehler
                    await asyncio.sleep(2)
                    continue

                if state.last_pressure_error_time and (now - state.last_pressure_error_time) < PRESSURE_ERROR_DELAY:
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info("Kompressor ausgeschaltet (Druckfehler-Sperre).")
                    remaining_time = (PRESSURE_ERROR_DELAY - (now - state.last_pressure_error_time)).total_seconds()
                    state.ausschluss_grund = f"Druckfehler-Sperre ({remaining_time:.0f}s verbleibend)"
                    await asyncio.sleep(2)
                    continue

                if t_verd is not None and t_verd < int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"]):
                    if state.kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)
                        state.kompressor_ein = False
                        last_compressor_off_time = now
                        logging.info(f"Kompressor ausgeschaltet (Verdampfer zu kalt: {t_verd:.1f}°C).")
                    state.ausschluss_grund = f"Verdampfer zu kalt ({t_verd:.1f}°C)"
                    await asyncio.sleep(2)
                    continue

                if t_boiler_oben is not None and t_boiler_hinten is not None and t_boiler_mittig is not None:
                    if state.solar_ueberschuss_aktiv:
                        logging.debug(
                            f"Solarüberschuss aktiv, prüfe Einschaltbedingungen: "
                            f"T_Oben={t_boiler_oben:.1f}, T_Hinten={t_boiler_hinten:.1f}, T_Mittig={t_boiler_mittig:.1f}, "
                            f"Einschaltpunkt={state.aktueller_einschaltpunkt}, Ausschaltpunkt={state.aktueller_ausschaltpunkt}"
                        )
                        if (t_boiler_oben < state.aktueller_einschaltpunkt or
                            t_boiler_hinten < state.aktueller_einschaltpunkt or
                            t_boiler_mittig < state.aktueller_einschaltpunkt):
                            if not state.kompressor_ein:
                                min_pause_seconds = int(config["Heizungssteuerung"].get("MIN_PAUSE", 5)) * 60
                                if last_compressor_off_time and (now - last_compressor_off_time).total_seconds() < min_pause_seconds:
                                    pause_remaining = min_pause_seconds - (now - last_compressor_off_time).total_seconds()
                                    state.ausschluss_grund = f"Zu kurze Pause ({pause_remaining:.1f}s verbleibend)"
                                    logging.info(
                                        f"Kompressor bleibt aus (zu kurze Pause: {(now - last_compressor_off_time)}, benötigt: {timedelta(seconds=min_pause_seconds)})"
                                    )
                                else:
                                    logging.info(
                                        f"Versuche, Kompressor einzuschalten (ein Fühler < {state.aktueller_einschaltpunkt} °C)."
                                    )
                                    result = await asyncio.to_thread(set_kompressor_status, True)
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
                              t_boiler_hinten >= state.aktueller_ausschaltpunkt or
                              t_boiler_mittig >= state.aktueller_ausschaltpunkt):
                            if state.kompressor_ein:
                                await asyncio.to_thread(set_kompressor_status, False)
                                state.kompressor_ein = False
                                last_compressor_off_time = now
                                last_runtime = now - state.last_compressor_on_time
                                state.total_runtime_today += last_runtime
                                state.ausschluss_grund = None
                                logging.info(
                                    f"Kompressor ausgeschaltet (ein Fühler ≥ {state.aktueller_ausschaltpunkt} °C). Laufzeit: {last_runtime}"
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
                                min_pause_seconds = int(config["Heizungssteuerung"].get("MIN_PAUSE", 5)) * 60
                                if last_compressor_off_time and (now - last_compressor_off_time).total_seconds() < min_pause_seconds:
                                    pause_remaining = min_pause_seconds - (now - last_compressor_off_time).total_seconds()
                                    state.ausschluss_grund = f"Zu kurze Pause ({pause_remaining:.1f}s verbleibend)"
                                    logging.info(
                                        f"Kompressor bleibt aus (zu kurze Pause: {(now - last_compressor_off_time)}, benötigt: {timedelta(seconds=min_pause_seconds)})"
                                    )
                                else:
                                    logging.info(
                                        f"Versuche, Kompressor einzuschalten (ein Fühler < {state.aktueller_einschaltpunkt} °C)."
                                    )
                                    result = await asyncio.to_thread(set_kompressor_status, True)
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
                                await asyncio.to_thread(set_kompressor_status, False)
                                state.kompressor_ein = False
                                last_compressor_off_time = now
                                last_runtime = now - state.last_compressor_on_time
                                state.total_runtime_today += last_runtime
                                state.ausschluss_grund = None
                                logging.info(
                                    f"Kompressor ausgeschaltet (ein Fühler ≥ {state.aktueller_ausschaltpunkt} °C). Laufzeit: {last_runtime}"
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
                            csv_line = (
                                f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                                f"{t_boiler_oben if t_boiler_oben is not None else 'N/A'},"
                                f"{t_boiler_hinten if t_boiler_hinten is not None else 'N/A'},"
                                f"{t_boiler_mittig if t_boiler_mittig is not None else 'N/A'},"
                                f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                                f"{t_verd if t_verd is not None else 'N/A'},"
                                f"{'EIN' if state.kompressor_ein else 'AUS'},"
                                f"{acpower},{feedinpower},{batPower},{soc},{powerdc1},{powerdc2},{consumeenergy},"
                                f"{state.aktueller_einschaltpunkt},{state.aktueller_ausschaltpunkt},"
                                f"{int(state.solar_ueberschuss_aktiv)},{nacht_reduction}\n"
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
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
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