import os
import smbus2
import datetime
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
import configparser
import aiohttp
import hashlib
from telegram import ReplyKeyboardMarkup
import asyncio
import aiofiles

# Basisverzeichnis für Temperatursensoren und Sensor-IDs
BASE_DIR = "/sys/bus/w1/devices/"
SENSOR_IDS = {
    "oben": "28-0bd6d4461d84",
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
TEMP_OFFSET = int(config["Heizungssteuerung"]["TEMP_OFFSET"])
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
last_shutdown_time = datetime.datetime.now()
last_config_hash = None
last_log_time = datetime.datetime.now() - datetime.timedelta(minutes=1)
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
PRESSURE_ERROR_DELAY = datetime.timedelta(minutes=5)  # 5 Minuten Verzögerung
last_pressure_state = None


# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

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
            return True
    except aiohttp.ClientError as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {e}, Nachricht={message}")
        return False


# Asynchrone Funktion zum Abrufen von Telegram-Updates
async def get_telegram_updates(session, offset=None):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": offset, "timeout": 20} if offset else {"timeout": 20}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as response:
            response.raise_for_status()
            updates = await response.json()
            logging.debug(f"Telegram-Updates empfangen: {updates}")
            return updates.get('result', [])
    except aiohttp.ClientError as e:
        logging.error(f"Fehler bei der Telegram-API-Abfrage: {e}")
        return None


async def get_boiler_temperature_history(session):
    """Liest die letzten 20 Temperaturen der Boilerfühler (oben und hinten) im 5-Minuten-Abstand aus der CSV-Datei."""
    try:
        temp_oben = []
        temp_hinten = []
        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            lines = await csvfile.readlines()
            # Überspringen des Headers und Umkehren der Reihenfolge (neueste zuerst)
            lines = lines[1:][::-1]

            # Zeitstempel und Temperaturen extrahieren
            for line in lines:
                parts = line.strip().split(',')
                if len(parts) >= 5:  # Mindestens Zeitstempel, T_Oben, T_Hinten, T_Boiler, T_Verd
                    timestamp_str, t_oben, t_hinten = parts[0], parts[1], parts[2]
                    if t_oben != "N/A" and t_oben != "Fehler" and t_hinten != "N/A" and t_hinten != "Fehler":
                        timestamp = datetime.datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        temp_oben.append((timestamp, float(t_oben)))
                        temp_hinten.append((timestamp, float(t_hinten)))

            # Filtern auf 5-Minuten-Abstand (wir verwenden eine Liste für beide Fühler)
            filtered_temps = []
            last_time = None
            for i in range(min(len(temp_oben), len(temp_hinten))):
                timestamp, t_oben = temp_oben[i]
                _, t_hinten = temp_hinten[i]
                if last_time is None or (last_time - timestamp).total_seconds() >= 300:  # 300 Sekunden = 5 Minuten
                    filtered_temps.append((timestamp, t_oben, t_hinten))
                    last_time = timestamp
                if len(filtered_temps) >= 20:
                    break

        if not filtered_temps:
            return "Keine gültigen Temperaturdaten verfügbar."

        # Nachricht formatieren
        message = "📈 Boiler-Temperaturverlauf (letzte 20 Werte, ~5-Min-Abstand):\n\n"
        message += "Zeitstempel | T_Oben | T_Hinten\n"
        message += "-" * 40 + "\n"
        for i, (timestamp, t_oben, t_hinten) in enumerate(filtered_temps[::-1], 1):  # Älteste zuerst
            message += f"{timestamp.strftime('%d.%m.%Y %H:%M:%S')} | {t_oben:.2f} °C | {t_hinten:.2f} °C\n"
        return message

    except Exception as e:
        logging.error(f"Fehler beim Auslesen des Temperaturverlaufs: {e}")
        return f"Fehler beim Abrufen des Verlaufs: {str(e)}"
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
        ["🌡️ Temperaturen", "📊 Status"],
        ["📈 Verlauf"],  # Neuer Button
        ["🌴 Urlaub", "🏠 Urlaub aus"],
        ["🆘 Hilfe"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# Asynchrone Hilfsfunktionen für Telegram
async def send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = f"🌡️ Aktuelle Temperaturen:\nKessel oben: {t_boiler_oben:.2f} °C\nKessel hinten: {t_boiler_hinten:.2f} °C\nVerdampfer: {t_verd:.2f} °C"
    return await send_telegram_message(session, CHAT_ID, message)


async def send_status_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd, kompressor_status, aktuelle_laufzeit,
                               gesamtlaufzeit, einschaltpunkt, ausschaltpunkt):
    """Sendet den aktuellen Status über Telegram."""
    global ausschluss_grund, t_boiler, urlaubsmodus_aktiv, solar_ueberschuss_aktiv, config, last_runtime

    # Basisnachricht
    message = (
        f"🌡️ Aktuelle Temperaturen:\n"
        f"Boiler oben: {t_boiler_oben:.2f} °C\n"
        f"Boiler hinten: {t_boiler_hinten:.2f} °C\n"
        f"Verdampfer: {t_verd:.2f} °C\n\n"
        f"🔧 Kompressorstatus: {'EIN' if kompressor_status else 'AUS'}\n"
    )

    # Laufzeit je nach Kompressorstatus anzeigen
    if kompressor_status:
        message += f"⏱️ Aktuelle Laufzeit: {aktuelle_laufzeit}\n"
    else:
        message += f"⏱️ Letzte Laufzeit: {str(last_runtime).split('.')[0]}\n"

    message += (
        f"⏳ Gesamtlaufzeit heute: {gesamtlaufzeit}\n\n"
        f"🎯 Sollwerte:\n"
        f"Einschaltpunkt: {einschaltpunkt} °C\n"
        f"Ausschaltpunkt: {ausschaltpunkt} °C\n"
    )

    # Aktive Modi hinzufügen
    active_modes = []
    if is_nighttime(config):
        nacht_reduction = int(config["Heizungssteuerung"]["NACHTABSENKUNG"])
        active_modes.append(f"Nachtabsenkung ({nacht_reduction} °C)")
    if urlaubsmodus_aktiv:
        urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 6))
        active_modes.append(f"Urlaubsmodus (-{urlaubsabsenkung} °C)")
    if solar_ueberschuss_aktiv:
        erhöhung = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]) - int(
            config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
        active_modes.append(f"PV-Überschuss (+{erhöhung} °C)")

    if active_modes:
        message += "\n🔄 Aktive Modi:\n- " + "\n- ".join(active_modes)
    else:
        message += "\n🔄 Aktive Modi: Keine"

    # Ausschlussgrund, falls vorhanden
    if not kompressor_status and ausschluss_grund:
        message += f"\n\n⚠️ Kompressor ausgeschaltet wegen: {ausschluss_grund}"

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
        "📈 *Verlauf* – Zeigt die letzten 20 Temperaturen (oben und hinten) im 5-Minuten-Abstand.\n"
        "🌴 *Urlaub* – Aktiviert den Urlaubsmodus.\n"
        "🏠 *Urlaub aus* – Deaktiviert den Urlaubsmodus.\n"
        "🆘 *Hilfe* – Zeigt diese Nachricht an."
    )
    return await send_telegram_message(session, CHAT_ID, message, parse_mode="Markdown")

async def shutdown(session):
    """Sendet eine Telegram-Nachricht beim Programmende und bereinigt Ressourcen."""
    now = datetime.datetime.now()
    message = f"🛑 Programm beendet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
    await send_telegram_message(session, CHAT_ID, message)
    GPIO.output(GIO21_PIN, GPIO.LOW)  # Kompressor ausschalten
    GPIO.cleanup()  # GPIO-Pins bereinigen
    lcd.close()  # LCD schließen
    logging.info("Heizungssteuerung sicher beendet, Hardware in sicherem Zustand.")

# Hauptprogrammstart
async def run_program():
    async with aiohttp.ClientSession() as session:
        # Logging mit Telegram-Handler einrichten
        await setup_logging(session)

        # CSV-Header schreiben, falls die Datei noch nicht existiert
        if not os.path.exists("heizungsdaten.csv"):
            async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                header = (
                    "Zeitstempel,T_Oben,T_Hinten,T_Boiler,T_Verd,Kompressor,"
                    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy\n"
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
        logging.warning(f"Fühlerdifferenz erkannt: oben={t_oben}, hinten={t_hinten}, Differenz={abs(t_oben - t_hinten)}")
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
    now = datetime.datetime.now()
    if ein:
        if not kompressor_ein:
            pause_time = now - last_shutdown_time
            if pause_time < MIN_PAUSE and not force_off:
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {MIN_PAUSE})")
                ausschluss_grund = f"Zu kurze Pause ({pause_time.total_seconds():.1f}s < {MIN_PAUSE.total_seconds():.1f}s)"
                return False
            kompressor_ein = True
            start_time = now
            current_runtime = datetime.timedelta()
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
            logging.info(f"Kompressor AUS geschaltet. Laufzeit: {elapsed_time}, Gesamtlaufzeit heute: {total_runtime_today}")
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
async def reload_config(session):
    """Lädt die Konfigurationsdatei asynchron neu und aktualisiert globale Variablen."""
    global AUSSCHALTPUNKT, AUSSCHALTPUNKT_ERHOEHT, TEMP_OFFSET, MIN_LAUFZEIT, MIN_PAUSE, TOKEN_ID, SN, VERDAMPFERTEMPERATUR, BOT_TOKEN, CHAT_ID, last_config_hash, urlaubsmodus_aktiv, aktueller_einschaltpunkt, aktueller_ausschaltpunkt

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
            TEMP_OFFSET = check_value(
                int(config["Heizungssteuerung"]["TEMP_OFFSET"]),
                min_value=5, max_value=20, default_value=10,
                parameter_name="TEMP_OFFSET"
            )
            VERDAMPFERTEMPERATUR = check_value(
                int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"]),
                min_value=4, max_value=40, default_value=6,
                parameter_name="VERDAMPFERTEMPERATUR"
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

        BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]
        CHAT_ID = config["Telegram"]["CHAT_ID"]
        MIN_LAUFZEIT = datetime.timedelta(minutes=MIN_LAUFZEIT_MINUTEN)
        MIN_PAUSE = datetime.timedelta(minutes=MIN_PAUSE_MINUTEN)
        TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
        SN = config["SolaxCloud"]["SN"]

        # Alte Sollwerte speichern
        old_einschaltpunkt = aktueller_einschaltpunkt
        old_ausschaltpunkt = aktueller_ausschaltpunkt

        # Berechne Sollwerte neu nach Konfigurationsänderung
        solax_data = await get_solax_data(session) or {"acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                                                       "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                                                       "api_fehler": True}
        aktueller_ausschaltpunkt = calculate_shutdown_point(config, is_nighttime(config), solax_data)
        aktueller_einschaltpunkt = aktueller_ausschaltpunkt - TEMP_OFFSET

        # Logging bei Änderung der Sollwerte
        if old_einschaltpunkt != aktueller_einschaltpunkt or old_ausschaltpunkt != aktueller_ausschaltpunkt:
            logging.info(
                f"Sollwerte durch Konfigurationsänderung angepasst: "
                f"Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
                f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}"
            )

        logging.info(
            f"Konfiguration neu geladen: AUSSCHALTPUNKT={AUSSCHALTPUNKT}, TEMP_OFFSET={TEMP_OFFSET}, "
            f"VERDAMPFERTEMPERATUR={VERDAMPFERTEMPERATUR}, Einschaltpunkt={aktueller_einschaltpunkt}, "
            f"Ausschaltpunkt={aktueller_ausschaltpunkt}"
        )
        last_config_hash = current_hash

    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}")


# Funktion zum Anpassen der Sollwerte (synchron, wird in Thread ausgeführt)
def adjust_shutdown_and_start_points(solax_data, config):
    global aktueller_ausschaltpunkt, aktueller_einschaltpunkt, AUSSCHALTPUNKT, TEMP_OFFSET
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

    # Alte Werte speichern, um Änderungen zu erkennen
    old_ausschaltpunkt = aktueller_ausschaltpunkt
    old_einschaltpunkt = aktueller_einschaltpunkt

    # Immer calculate_shutdown_point verwenden, auch im Urlaubsmodus
    aktueller_ausschaltpunkt = calculate_shutdown_point(config, is_night, solax_data)
    aktueller_einschaltpunkt = aktueller_ausschaltpunkt - TEMP_OFFSET

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
                        min_val = 0 if key not in ["AUSSCHALTPUNKT", "AUSSCHALTPUNKT_ERHOEHT"] else 20
                        max_val = 100 if key not in ["MIN_LAUFZEIT", "MIN_PAUSE"] else 60
                        if not (min_val <= value <= max_val):
                            logging.warning(f"Ungültiger Wert für {key} in {section}: {value}. Verwende Standardwert: {default}")
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
    now = datetime.datetime.now()
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

        logging.debug(f"Nachtzeitprüfung: Jetzt={now_time}, Start={start_time_minutes}, Ende={end_time_minutes}, Ist Nacht={is_night}")
        return is_night
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False


def calculate_shutdown_point(config, is_night, solax_data):
    global solar_ueberschuss_aktiv
    try:
        nacht_reduction = int(config["Heizungssteuerung"]["NACHTABSENKUNG"]) if is_night else 0
        bat_power = solax_data.get("batPower", 0)
        feedin_power = solax_data.get("feedinpower", 0)
        soc = solax_data.get("soc", 0)

        # Solarüberschuss aktivieren
        if bat_power > 600 or (soc > 95 and feedin_power > 600):
            solar_ueberschuss_aktiv = True
            logging.info(f"Solarüberschuss aktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")
        # Solarüberschuss deaktivieren mit Hysterese
        elif bat_power < 50 and feedin_power < 50:
            solar_ueberschuss_aktiv = False
            logging.info(f"Solarüberschuss deaktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")

        # Ausschaltpunkt basierend auf dem Zustand setzen
        if solar_ueberschuss_aktiv:
            shutdown_point = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]) - nacht_reduction
        else:
            shutdown_point = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nacht_reduction

        logging.debug(f"Ausschaltpunkt berechnet: Solarüberschuss_aktiv={solar_ueberschuss_aktiv}, Nachtreduktion={nacht_reduction}, Ergebnis={shutdown_point}")
        return shutdown_point
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Berechnen des Ausschaltpunkts: {e}, Solax-Daten={solax_data}")
        return int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])  # Fallback-Wert


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
    global last_update_id
    max_retries = 3
    while True:
        for attempt in range(max_retries):
            try:
                updates = await get_telegram_updates(session, last_update_id)
                if updates is not None:
                    last_update_id = await process_telegram_messages_async(
                        session,
                        await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"]),
                        await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"]),
                        await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"]),
                        updates,
                        last_update_id,
                        kompressor_ein,
                        str(current_runtime).split('.')[0],
                        str(total_runtime_today).split('.')[0]
                    )
                    break  # Erfolgreich, Schleife verlassen
                else:
                    logging.warning(f"Telegram-Updates waren None, Versuch {attempt + 1}/{max_retries}")
            except Exception as e:
                logging.error(f"Fehler in telegram_task (Versuch {attempt + 1}/{max_retries}): {e}", exc_info=True)
                if attempt < max_retries - 1:
                    await asyncio.sleep(10)  # 10 Sekunden warten vor erneutem Versuch
                else:
                    logging.error("Maximale Wiederholungen erreicht, warte 5 Minuten")
                    await asyncio.sleep(300)  # 5 Minuten warten nach Fehlschlag
        await asyncio.sleep(0.1)  # Schnelles Polling bei Erfolg

# Asynchrone Task für Display-Updates
async def display_task():
    """Separate Task für Display-Updates, entkoppelt von der Hauptschleife."""
    global lcd
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
            pressure_ok = await asyncio.to_thread(check_pressure)  # Druckschalter hier prüfen

            lcd.clear()
            if not pressure_ok:
                lcd.write_string("FEHLER: Druck zu niedrig")
                logging.error(f"Display zeigt Druckfehler: Druckschalter={pressure_ok}")
            else:
                lcd.write_string(f"T-Oben: {t_boiler_oben if t_boiler_oben is not None else 'Fehler':.2f} C")
                lcd.cursor_pos = (1, 0)
                lcd.write_string(f"T-Hinten: {t_boiler_hinten if t_boiler_hinten is not None else 'Fehler':.2f} C")
                lcd.cursor_pos = (2, 0)
                lcd.write_string(f"T-Boiler: {t_boiler if t_boiler != 'Fehler' else 'Fehler':.2f} C")
                lcd.cursor_pos = (3, 0)
                lcd.write_string(f"T-Verd: {t_verd if t_verd is not None else 'Fehler':.2f} C")
                logging.debug(f"Display-Seite 1 aktualisiert: oben={t_boiler_oben}, hinten={t_boiler_hinten}, boiler={t_boiler}, verd={t_verd}")
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
            logging.debug(f"Display-Seite 2 aktualisiert: Status={'EIN' if kompressor_ein else 'AUS'}, Laufzeit={current_runtime if kompressor_ein else last_runtime}")
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
                logging.debug(f"Display-Seite 3 aktualisiert: Solar={solar}, Netz={feedinpower}, Verbrauch={consumeenergy}, Batterie={batPower}, SOC={soc}")
            else:
                lcd.write_string("Fehler bei Solax-Daten")
                logging.warning("Keine Solax-Daten für Display verfügbar")
            await asyncio.sleep(5)

        except Exception as e:
            error_msg = f"Fehler beim Display-Update: {e}"
            logging.error(error_msg)
            await send_telegram_message(session, CHAT_ID, error_msg)
            lcd = None  # Setze lcd auf None bei Fehler während der Nutzung
            await asyncio.sleep(5)  # Warte, bevor es weitergeht


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
async def main_loop(session):
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
    global last_update_id, kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time, last_config_hash, last_log_time, last_kompressor_status, urlaubsmodus_aktiv, AUSSCHALTPUNKT, TEMP_OFFSET, original_einschaltpunkt, original_ausschaltpunkt, pressure_error_sent, aktueller_einschaltpunkt, aktueller_ausschaltpunkt, ausschluss_grund, t_boiler, last_pressure_error_time

    # GPIO-Pins initialisieren
    if not await initialize_gpio():
        logging.critical("Programm wird aufgrund fehlender GPIO-Initialisierung beendet.")
        exit(1)

    async with aiohttp.ClientSession() as session:
        # LCD initialisieren
        await initialize_lcd(session)

        # Startnachricht
        now = datetime.datetime.now()
        message = f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
        await send_telegram_message(session, CHAT_ID, message)
        await send_welcome_message(session, CHAT_ID)

        # Asynchrone Tasks starten
        telegram_task_handle = asyncio.create_task(telegram_task(session))
        display_task_handle = asyncio.create_task(display_task())

        # Watchdog-Variablen
        last_cycle_time = datetime.datetime.now()
        watchdog_warning_count = 0
        WATCHDOG_MAX_WARNINGS = 3

        try:
            while True:
                # Konfiguration laden und validieren
                config = validate_config(load_config())

                # Prüfen, ob sich die Konfigurationsdatei geändert hat
                current_hash = calculate_file_hash("config.ini")
                if last_config_hash != current_hash:
                    await reload_config(session)
                    last_config_hash = current_hash

                # Solax-Daten abrufen
                solax_data = await get_solax_data(session)
                if solax_data is None:
                    solax_data = {"acpower": 0, "feedinpower": 0, "consumeenergy": 0, "batPower": 0, "soc": 0,
                                  "powerdc1": 0, "powerdc2": 0, "api_fehler": True}

                # Sollwerte anpassen
                await asyncio.to_thread(adjust_shutdown_and_start_points, solax_data, config)

                # Temperaturen auslesen
                t_boiler_oben = await asyncio.to_thread(read_temperature, SENSOR_IDS["oben"])  # vorher "vorne"
                t_boiler_hinten = await asyncio.to_thread(read_temperature, SENSOR_IDS["hinten"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                t_boiler = (
                                   t_boiler_oben + t_boiler_hinten) / 2 if t_boiler_oben is not None and t_boiler_hinten is not None else "Fehler"


                # Druckschalter prüfen
                pressure_ok = await asyncio.to_thread(check_pressure)
                now = datetime.datetime.now()

                if not pressure_ok:
                    # Kompressor ausschalten, falls er läuft
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        logging.info("Kompressor wegen Druckfehler ausgeschaltet")

                    # Zeitpunkt des Druckfehlers speichern
                    last_pressure_error_time = now

                    # Telegram-Nachricht senden, wenn der Fehler noch nicht gemeldet wurde
                    if not pressure_error_sent:
                        error_msg = "❌ Druckfehler: Kompressor läuft nicht aufgrund eines Problems mit dem Druckschalter! 5-Minuten-Sperre aktiviert."
                        await send_telegram_message(session, CHAT_ID, error_msg)
                        pressure_error_sent = True
                        logging.error("Druckfehler erkannt: Telegram-Nachricht gesendet, 5-Minuten-Sperre gestartet")
                    ausschluss_grund = "Druckschalter offen"
                    continue  # Schleife fortsetzen, keine weiteren Steueraktionen ausführen
                else:
                    if pressure_error_sent and (last_pressure_error_time is None or (
                            now - last_pressure_error_time) >= PRESSURE_ERROR_DELAY):
                        # Wenn der Druck wieder normal ist und die 5-Minuten-Sperre abgelaufen ist
                        info_msg = "✅ Druckschalter wieder normal. Kompressor kann wieder laufen."
                        await send_telegram_message(session, CHAT_ID, info_msg)
                        logging.info(
                            "Druckschalter wieder normal, 5-Minuten-Sperre abgelaufen, Fehlermeldungsstatus zurückgesetzt")
                        pressure_error_sent = False
                        last_pressure_error_time = None  # Zurücksetzen, wenn Sperre abgelaufen

                # Boiler-Sensoren auf Fehler prüfen
                fehler, is_overtemp = check_boiler_sensors(t_boiler_oben, t_boiler_hinten, config)
                if fehler:
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    logging.info(f"Kompressor wegen Fehler ausgeschaltet: {fehler}")
                    continue

                # Kompressorsteuerung
                if last_pressure_error_time and (now - last_pressure_error_time) < PRESSURE_ERROR_DELAY:
                    # Kompressor bleibt aus, wenn die 5-Minuten-Sperre aktiv ist
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    remaining_time = (PRESSURE_ERROR_DELAY - (now - last_pressure_error_time)).total_seconds()
                    ausschluss_grund = f"Druckfehler-Sperre ({remaining_time:.0f}s verbleibend)"
                    logging.debug(f"Kompressor gesperrt wegen Druckfehler, verbleibende Zeit: {remaining_time:.0f}s")
                elif t_verd is not None and t_verd < VERDAMPFERTEMPERATUR:
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)
                    ausschluss_grund = f"Verdampfer zu kalt ({t_verd:.1f}°C < {VERDAMPFERTEMPERATUR}°C)"
                elif t_boiler != "Fehler":
                    if t_boiler < aktueller_einschaltpunkt and not kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, True)
                    elif t_boiler >= aktueller_ausschaltpunkt and kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)

                if kompressor_ein and start_time:
                    current_runtime = datetime.datetime.now() - start_time

                # Datenlogging
                now = datetime.datetime.now()
                should_log = False

                # Prüfe, ob eine Minute vergangen ist oder der Kompressorstatus sich geändert hat
                if last_log_time is None or (now - last_log_time) >= datetime.timedelta(minutes=1):
                    should_log = True
                elif kompressor_ein != last_kompressor_status:
                    should_log = True

                if should_log:
                    async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                        # PV-Daten aus solax_data extrahieren, mit Fallbacks für fehlende Werte
                        acpower = solax_data.get("acpower", "N/A")
                        feedinpower = solax_data.get("feedinpower", "N/A")
                        batPower = solax_data.get("batPower", "N/A")
                        soc = solax_data.get("soc", "N/A")
                        powerdc1 = solax_data.get("powerdc1", "N/A")
                        powerdc2 = solax_data.get("powerdc2", "N/A")
                        consumeenergy = solax_data.get("consumeenergy", "N/A")

                        csv_line = (
                            f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                            f"{t_boiler_oben if t_boiler_oben is not None else 'N/A'},"
                            f"{t_boiler_hinten if t_boiler_hinten is not None else 'N/A'},"
                            f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                            f"{t_verd if t_verd is not None else 'N/A'},"
                            f"{'EIN' if kompressor_ein else 'AUS'},"
                            f"{acpower},"
                            f"{feedinpower},"
                            f"{batPower},"
                            f"{soc},"
                            f"{powerdc1},"
                            f"{powerdc2},"
                            f"{consumeenergy}\n"
                        )
                        await csvfile.write(csv_line)
                        logging.info(f"CSV-Eintrag geschrieben: {csv_line.strip()}")
                    last_log_time = now
                    last_kompressor_status = kompressor_ein  # Aktualisiere den letzten Status

                # Watchdog
                cycle_duration = (datetime.datetime.now() - last_cycle_time).total_seconds()
                if cycle_duration > 15:
                    watchdog_warning_count += 1
                    logging.error(
                        f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {watchdog_warning_count}/{WATCHDOG_MAX_WARNINGS}")
                    if watchdog_warning_count >= WATCHDOG_MAX_WARNINGS:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        logging.critical("Maximale Watchdog-Warnungen erreicht, Hardware wird heruntergefahren.")
                        break

                last_cycle_time = datetime.datetime.now()
                await asyncio.sleep(2)

        except asyncio.CancelledError:
            logging.info("Hauptschleife abgebrochen, Tasks werden beendet.")
            telegram_task_handle.cancel()
            display_task_handle.cancel()
            await asyncio.gather(telegram_task_handle, display_task_handle, return_exceptions=True)
            raise

async def shutdown(session):
    """Sendet eine Telegram-Nachricht beim Programmende und bereinigt Ressourcen."""
    now = datetime.datetime.now()
    message = f"🛑 Programm beendet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
    await send_telegram_message(session, CHAT_ID, message)
    GPIO.output(GIO21_PIN, GPIO.LOW)
    GPIO.cleanup()
    if lcd is not None:
        lcd.close()
    logging.info("Heizungssteuerung sicher beendet.")


async def run_program():
    """Hauptfunktion zum Starten des Programms."""
    async with aiohttp.ClientSession() as session:
        # CSV-Header schreiben, falls die Datei noch nicht existiert
        if not os.path.exists("heizungsdaten.csv"):
            async with aiofiles.open("heizungsdaten.csv", 'w', newline='') as csvfile:
                header = (
                    "Zeitstempel,T_Oben,T_Hinten,T_Boiler,T_Verd,Kompressor,"
                    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy\n"
                )
                await csvfile.write(header)
                logging.info("CSV-Header geschrieben: " + header.strip())

        try:
            await main_loop(session)
        except KeyboardInterrupt:
            logging.info("Programm durch Benutzer beendet.")
        finally:
            await shutdown(session)

# Asynchrone Verarbeitung von Telegram-Nachrichten
async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_hinten, t_verd, updates, last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit):
    global AUSSCHALTPUNKT, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    global AUSSCHALTPUNKT, aktueller_einschaltpunkt
    if updates:
        for update in updates:
            message_text = update.get('message', {}).get('text')
            chat_id = update.get('message', {}).get('chat', {}).get('id')
            if message_text and chat_id:
                message_text = message_text.strip().lower()
                logging.debug(f"Telegram-Nachricht empfangen: Text={message_text}, Chat-ID={chat_id}")
                if message_text == "🌡️ temperaturen" or message_text == "temperaturen":
                    if t_boiler_oben != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        await send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd)
                    else:
                        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen der Temperaturen.")
                elif message_text == "📊 status" or message_text == "status":
                    if t_boiler_oben != "Fehler" and t_boiler_hinten != "Fehler" and t_verd != "Fehler":
                        await send_status_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd, kompressor_status,
                                                   aktuelle_laufzeit, gesamtlaufzeit, aktueller_einschaltpunkt,
                                                   aktueller_ausschaltpunkt)
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
                elif message_text == "📈 verlauf" or message_text == "verlauf":
                    history_message = await get_boiler_temperature_history(session)
                    await send_telegram_message(session, CHAT_ID, history_message)
                else:
                    await send_unknown_command_message(session, chat_id)
            last_update_id = update['update_id'] + 1
            logging.debug(f"last_update_id aktualisiert: {last_update_id}")
    return last_update_id


# Asynchrone Urlaubsmodus-Funktionen
async def aktivere_urlaubsmodus(session):
    """Aktiviert den Urlaubsmodus und passt Sollwerte an."""
    global urlaubsmodus_aktiv, AUSSCHALTPUNKT, TEMP_OFFSET, original_einschaltpunkt, original_ausschaltpunkt, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
    if not urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = True
        # Speichere die aktuellen Sollwerte vor der Änderung
        original_einschaltpunkt = aktueller_einschaltpunkt
        original_ausschaltpunkt = aktueller_ausschaltpunkt
        urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 6))
        # Alte Werte speichern
        old_einschaltpunkt = aktueller_einschaltpunkt
        old_ausschaltpunkt = aktueller_ausschaltpunkt
        # Passe die Sollwerte an
        aktueller_ausschaltpunkt = AUSSCHALTPUNKT - urlaubsabsenkung
        aktueller_einschaltpunkt = aktueller_ausschaltpunkt - TEMP_OFFSET
        logging.info(
            f"Urlaubsmodus aktiviert. Sollwerte geändert: "
            f"Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}"
        )
        await send_telegram_message(session, CHAT_ID,
                                    f"🌴 Urlaubsmodus aktiviert. Neue Werte:\nEinschaltpunkt: {aktueller_einschaltpunkt} °C\nAusschaltpunkt: {aktueller_ausschaltpunkt} °C")

async def deaktivere_urlaubsmodus(session):
    """Deaktiviert den Urlaubsmodus und stellt ursprüngliche Werte wieder her."""
    global urlaubsmodus_aktiv, AUSSCHALTPUNKT, TEMP_OFFSET, original_einschaltpunkt, original_ausschaltpunkt, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
    if urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = False
        # Alte Werte speichern
        old_einschaltpunkt = aktueller_einschaltpunkt
        old_ausschaltpunkt = aktueller_ausschaltpunkt
        # Stelle die ursprünglichen Sollwerte wieder her
        aktueller_einschaltpunkt = original_einschaltpunkt
        aktueller_ausschaltpunkt = original_ausschaltpunkt
        logging.info(
            f"Urlaubsmodus deaktiviert. Sollwerte wiederhergestellt: "
            f"Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}"
        )
        await send_telegram_message(session, CHAT_ID,
                                    f"🏠 Urlaubsmodus deaktiviert. Ursprüngliche Werte:\nEinschaltpunkt: {aktueller_einschaltpunkt} °C\nAusschaltpunkt: {aktueller_ausschaltpunkt} °C")

# Programmstart
if __name__ == "__main__":
    asyncio.run(run_program())