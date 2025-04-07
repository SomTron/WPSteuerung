import os
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


# Basisverzeichnis für Temperatursensoren und Sensor-IDs
BASE_DIR = "/sys/bus/w1/devices/"
SENSOR_IDS = {
    "oben": "28-0bd6d4461d84",
    "hinten": "28-445bd44686f4",
    "verd": "28-213bd4460d65",
    "mittig": "28-6977d446424a"
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
MIN_LAUFZEIT = timedelta(minutes=min(max(int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]), 1), 60))
MIN_PAUSE = timedelta(minutes=min(max(int(config["Heizungssteuerung"]["MIN_PAUSE"]), 1), 60))
VERDAMPFERTEMPERATUR = min(max(int(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"]), -20), 20)
SOLAR_AUS = min(max(int(config["Heizungssteuerung"]["SOLAR_AUS"]), 40), 60)
SOLAR_EIN = min(max(int(config["Heizungssteuerung"]["SOLAR_EIN"]), 35), 55)
NORMAL_AUS = min(max(int(config["Heizungssteuerung"]["NORMAL_AUS"]), 40), 60)
NORMAL_EIN = min(max(int(config["Heizungssteuerung"]["NORMAL_EIN"]), 35), 55)
NACHT_EIN = min(max(int(config["Heizungssteuerung"]["NACHT_EIN"]), 20), 40)
SICHERHEITS_TEMP = min(max(int(config["Heizungssteuerung"]["SICHERHEITS_TEMP"]), 45), 70)
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
last_update_id = 0
urlaubsmodus_aktiv = False
pressure_error_sent = False
aktueller_ausschaltpunkt = NORMAL_AUS  # Standardwert für Normalbetrieb
aktueller_einschaltpunkt = NORMAL_EIN  # Standardwert für Normalbetrieb
original_ausschaltpunkt = NORMAL_AUS   # Ursprünglicher Wert für Wiederherstellung
original_einschaltpunkt = NORMAL_EIN   # Ursprünglicher Wert für Wiederherstellung
ausschluss_grund = None  # Grund, warum der Kompressor nicht läuft (z.B. "Zu kurze Pause")
t_boiler = None
solar_ueberschuss_aktiv = False
lcd = None
last_pressure_error_time = None  # Zeitpunkt des letzten Druckfehlers
PRESSURE_ERROR_DELAY = timedelta(minutes=5)  # 5 Minuten Verzögerung
last_pressure_state = None
csv_lock = asyncio.Lock()
# Globale Variablen für Temperaturwerte
t_boiler_oben = 0
t_boiler_hinten = 0
t_boiler_mittig = 0
t_verd = 0
t_boiler = 0


# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",
    level=logging.DEBUG,
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
    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)
            lcd.clear()
            logging.info("LCD erfolgreich initialisiert")
            return
        except Exception as e:
            logging.error(f"Fehler bei der LCD-Initialisierung (Versuch {attempt + 1}/{max_attempts}): {e}")
            if attempt < max_attempts - 1:
                await asyncio.sleep(1)
    logging.warning("LCD-Initialisierung fehlgeschlagen, fahre ohne LCD fort.")
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

async def update_csv_header_if_needed():
    """Prüft und aktualisiert den CSV-Header, falls nötig."""
    if os.path.exists("heizungsdaten.csv"):
        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            header = await csvfile.readline()
            if "Urlaubsmodus" not in header:  # Prüfe neuen Header
                old_header = (
                    "Zeitstempel,T_Oben,T_Hinten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
                    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
                    "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Nachtabsenkung,PowerSource\n"
                )
                new_header = (
                    "Zeitstempel,T_Oben,T_Hinten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
                    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
                    "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Urlaubsmodus,PowerSource\n"
                )
                lines = await csvfile.readlines()
                async with aiofiles.open("heizungsdaten.csv", 'w', newline='') as csvfile_new:
                    await csvfile_new.write(new_header)
                    for line in lines:
                        parts = line.strip().split(',')
                        if len(parts) >= 18:  # Mindestens bis Nachtabsenkung
                            updated_line = ','.join(parts[:17] + [parts[17].replace("Nachtabsenkung", "0")] + parts[18:]) + '\n'
                            await csvfile_new.write(updated_line)
                logging.info("CSV-Header aktualisiert: Nachtabsenkung durch Urlaubsmodus ersetzt.")
    else:
        async with aiofiles.open("heizungsdaten.csv", 'w', newline='') as csvfile:
            header = (
                "Zeitstempel,T_Oben,T_Hinten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
                "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
                "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Urlaubsmodus,PowerSource\n"
            )
            await csvfile.write(header)
            logging.info("Neue CSV-Datei erstellt mit Header: " + header.strip())

async def get_boiler_temperature_history(session, hours):
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    global UNTERER_FUEHLER_MIN, UNTERER_FUEHLER_MAX
    try:
        temp_oben = []
        temp_hinten = []
        temp_mittig = []
        einschaltpunkte = []
        ausschaltpunkte = []
        kompressor_status = []
        solar_ueberschuss_periods = []

        # Lese CSV-Daten
        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            lines = await csvfile.readlines()
            lines = lines[1:][::-1]  # Header überspringen und umkehren (neueste zuerst)

            now = datetime.now()
            time_ago = now - timedelta(hours=hours)

            for line in lines:
                parts = line.strip().split(',')
                if len(parts) >= 13:  # Mindestens bis ConsumeEnergy (altes Format)
                    while len(parts) < 19:  # Fülle bis 19 Spalten
                        parts.append("N/A")

                    timestamp_str = parts[0].strip()
                    timestamp_str = ''.join(c for c in timestamp_str if c.isprintable())

                    try:
                        timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                        if timestamp < time_ago:
                            continue  # Überspringe Daten außerhalb des Zeitfensters

                        t_oben, t_hinten, t_mittig = parts[1], parts[2], parts[3]
                        kompressor = parts[6]
                        einschaltpunkt = parts[14] if parts[14].strip() and parts[14] not in ("N/A", "Fehler") else "42"
                        ausschaltpunkt = parts[15] if parts[15].strip() and parts[15] not in ("N/A", "Fehler") else "45"
                        solar_ueberschuss = parts[16] if parts[16].strip() and parts[16] not in ("N/A", "Fehler") else "0"
                        power_source = parts[18] if parts[18].strip() and parts[18] not in ("N/A", "Fehler") else "Unbekannt"

                        if not (t_oben.strip() and t_oben not in ("N/A", "Fehler")) or not (
                                t_hinten.strip() and t_hinten not in ("N/A", "Fehler")):
                            logging.warning(f"Übersprungene Zeile wegen fehlender Temperaturen: {line.strip()}")
                            continue

                        temp_oben.append((timestamp, float(t_oben)))
                        temp_hinten.append((timestamp, float(t_hinten)))
                        if t_mittig.strip() and t_mittig not in ("N/A", "Fehler"):
                            temp_mittig.append((timestamp, float(t_mittig)))
                        einschaltpunkte.append((timestamp, float(einschaltpunkt)))
                        ausschaltpunkte.append((timestamp, float(ausschaltpunkt)))
                        kompressor_status.append((timestamp, 1 if kompressor == "EIN" else 0, power_source))
                        if int(solar_ueberschuss) == 1:
                            solar_ueberschuss_periods.append(
                                (timestamp, SOLAR_EIN))  # Unterer Grenzwert (Einschaltpunkt)
                            solar_ueberschuss_periods.append(
                                (timestamp, SOLAR_AUS))  # Oberer Grenzwert (Ausschaltpunkt)
                    except ValueError as e:
                        logging.error(f"Fehler beim Parsen der Zeile: {line.strip()},"
                                      f" Zeitstempel: '{timestamp_str}', Fehler: {e}")
                        continue

        if not temp_oben or not temp_hinten:
            logging.error(f"Keine gültigen Daten für {hours}h gefunden!")
            await send_telegram_message(session, CHAT_ID, f"Keine Daten für den {hours}h-Verlauf verfügbar.")
            return

        # Sampling: 1 Punkt alle 5 Minuten (300 Sekunden)
        total_minutes = hours * 60
        target_points = total_minutes // 5  # Z.B. 24h = 1440 Minuten / 5 = 288 Punkte
        target_interval = 300  # 5 Minuten in Sekunden

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

        sampled_oben = sample_data(temp_oben, target_interval, target_points)
        sampled_hinten = sample_data(temp_hinten, target_interval, target_points)
        sampled_mittig = sample_data(temp_mittig, target_interval, target_points)
        sampled_einschalt = sample_data(einschaltpunkte, target_interval, target_points)
        sampled_ausschalt = sample_data(ausschaltpunkte, target_interval, target_points)
        sampled_kompressor = sample_data(kompressor_status, target_interval, target_points)
        sampled_solar_min = sample_data(
            [(ts, val) for ts, val in solar_ueberschuss_periods
             if val == SOLAR_EIN], target_interval, target_points)
        sampled_solar_max = sample_data(
            [(ts, val) for ts, val in solar_ueberschuss_periods
             if val == SOLAR_AUS], target_interval, target_points)

        if not sampled_oben or not sampled_hinten:
            logging.error(f"Sampling ergab keine Daten für {hours}h!")
            await send_telegram_message(session, CHAT_ID, f"Fehler: Keine sampled Daten für den {hours}h-Verlauf.")
            return

        # Diagramm erstellen
        plt.figure(figsize=(12, 6))
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "PV + Netzstrom": "orange",
            "Unbekannt": "gray"
        }

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
                    plt.fill_between(segment_timestamps, 0, max(SOLAR_AUS, NORMAL_AUS) + 5,
                                     where=[val == 1 for val in segment_vals], color=color, alpha=0.2,
                                     label=f"Kompressor EIN ({power_sources[current_start_idx]})"
                                     if current_start_idx == 0 else None)
                    current_start_idx = i

            handles, labels = plt.gca().get_legend_handles_labels()
            by_label = dict(zip(labels, handles))
            plt.legend(by_label.values(), by_label.keys(), loc="lower left")

        if sampled_oben:
            timestamps_oben, t_oben_vals = zip(*sampled_oben)
            plt.plot(timestamps_oben, t_oben_vals, label="T_Oben", marker="o", color="blue")
        if sampled_hinten:
            timestamps_hinten, t_hinten_vals = zip(*sampled_hinten)
            plt.plot(timestamps_hinten, t_hinten_vals, label="T_Hinten", marker="x", color="red")
        if sampled_mittig:
            timestamps_mittig, t_mittig_vals = zip(*sampled_mittig)
            plt.plot(timestamps_mittig, t_mittig_vals, label="T_Mittig", marker="s", color="purple")
        if sampled_einschalt:
            timestamps_einschalt, einschalt_vals = zip(*sampled_einschalt)
            plt.plot(timestamps_einschalt, einschalt_vals, label="Einschaltpunkt (historisch)",
                     linestyle='--', color="green")
        if sampled_ausschalt:
            timestamps_ausschalt, ausschalt_vals = zip(*sampled_ausschalt)
            plt.plot(timestamps_ausschalt, ausschalt_vals, label="Ausschaltpunkt (historisch)",
                     linestyle='--', color="orange")
        if sampled_solar_min:
            timestamps_min, min_vals = zip(*sampled_solar_min)
            plt.plot(timestamps_min, min_vals, color='purple', linestyle='-.',
                     label=f'Solar Einschaltpunkt ({SOLAR_EIN}°C)')
        if sampled_solar_max:
            timestamps_max, max_vals = zip(*sampled_solar_max)
            plt.plot(timestamps_max, max_vals, color='cyan', linestyle='-.',
                     label=f'Solar Ausschaltpunkt ({SOLAR_AUS}°C)')

        plt.xlim(time_ago, now)
        plt.ylim(0, max(SOLAR_AUS, NORMAL_AUS) + 5)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf (letzte {hours} Stunden)")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.tight_layout()

        # Speichere Diagramm
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        # Sende Diagramm
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", CHAT_ID)
        form.add_field("caption",
                       f"📈 Verlauf {hours}h (T_Oben = blau, T_Hinten = rot, T_Mittig = lila,"
                       f" Kompressor EIN: grün=PV, gelb=Batterie, rot=Netz)")
        form.add_field("photo", buf, filename="temperature_graph.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Temperaturdiagramm für {hours}h gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen oder Senden des Temperaturverlaufs ({hours}h): {e}", exc_info=True)
        await send_telegram_message(session, CHAT_ID, f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}")
async def get_runtime_bar_chart(session, days=7):
    logging.info(f"Funktion aufgerufen mit days={days}")
    try:
        runtime_per_period = {}
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "PV + Netzstrom": "orange",
            "Unbekannt": "gray"
        }
        valid_power_sources = set(color_map.keys())

        now = datetime.now()
        time_ago = now - timedelta(days=days)
        logging.debug(f"Zeitfenster: {time_ago} bis {now}, days={days}")

        if days <= 30:
            period_type = "day"
            max_periods = days
            all_periods = [time_ago.date() + timedelta(days=i) for i in range(days)]
        elif days <= 210:
            period_type = "week"
            max_periods = min(30, (days + 6) // 7)
            all_periods = [time_ago.date() + timedelta(days=i * 7 - time_ago.weekday())
                           for i in range(max_periods)]
        elif days <= 900:
            period_type = "month"
            max_periods = min(30, (days + 29) // 30)
            start_month = time_ago.replace(day=1)
            all_periods = [start_month + relativedelta(months=i) for i in range(max_periods)]
        else:
            period_type = "year"
            max_periods = min(30, (days + 364) // 365)
            start_year = time_ago.replace(month=1, day=1)
            all_periods = [start_year + relativedelta(years=i) for i in range(max_periods)]
        logging.info(f"Periodentyp: {period_type}, max_periods: {max_periods}")

        try:
            async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
                lines = await csvfile.readlines()
        except FileNotFoundError:
            logging.warning("heizungsdaten.csv nicht gefunden.")
            await send_telegram_message(session, CHAT_ID, "Keine Daten verfügbar: CSV-Datei fehlt.")
            return

        if len(lines) <= 1:
            logging.warning("Keine Daten in heizungsdaten.csv vorhanden.")
            await send_telegram_message(session, CHAT_ID, "Keine Daten in der CSV-Datei vorhanden.")
            return

        lines = lines[1:]  # Header überspringen
        logging.debug(f"Anzahl CSV-Zeilen (ohne Header): {len(lines)}")

        last_timestamp = None
        last_status = None
        last_power_source = None
        seen_invalid_sources = set()

        for line in lines:
            parts = line.strip().split(',')
            # Fülle die Zeile mit "N/A" auf 19 Spalten auf, bevor wir darauf zugreifen
            if len(parts) < 19:
                parts.extend(["N/A"] * (19 - len(parts)))
                logging.debug(f"Zeile aufgefüllt: {line.strip()} -> {','.join(parts)}")

            timestamp_str = parts[0].strip()
            kompressor = parts[6].strip()  # Kompressor-Status
            power_source = parts[18].strip()  # PowerSource

            if not power_source or power_source not in valid_power_sources:
                if power_source and power_source not in seen_invalid_sources:
                    logging.warning(f"Ungültige Stromquelle gefunden: '{power_source}', Zeile: {line.strip()}")
                    seen_invalid_sources.add(power_source)
                power_source = "Unbekannt"

            try:
                timestamp = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
                if timestamp < time_ago:
                    continue

                if period_type == "day":
                    period = timestamp.date()
                elif period_type == "week":
                    period = timestamp.date() - timedelta(days=timestamp.weekday())
                elif period_type == "month":
                    period = timestamp.date().replace(day=1)
                else:
                    period = timestamp.date().replace(month=1, day=1)

                if period not in runtime_per_period:
                    runtime_per_period[period] = {
                        "Direkter PV-Strom": 0,
                        "Strom aus der Batterie": 0,
                        "Strom vom Netz": 0,
                        "PV + Netzstrom": 0,
                        "Unbekannt": 0
                    }

                if last_timestamp and last_status == "EIN":
                    time_diff = (timestamp - last_timestamp).total_seconds() / 60
                    if time_diff > 0:
                        last_period = (last_timestamp.date() if period_type == "day" else
                                       last_timestamp.date() - timedelta(
                                           days=last_timestamp.weekday()) if period_type == "week" else
                                       last_timestamp.date().replace(day=1) if period_type == "month" else
                                       last_timestamp.date().replace(month=1, day=1))
                        runtime_per_period[last_period][last_power_source] += time_diff

                last_timestamp = timestamp
                last_status = kompressor
                last_power_source = power_source

            except ValueError as e:
                logging.error(f"Ungültiger Zeitstempel in Zeile: {line.strip()}, Fehler: {e}")
                continue
            except Exception as e:
                logging.error(
                    f"Unerwarteter Fehler bei Zeile: {line.strip()}, Fehler: {e}, last_power_source: {last_power_source}")
                continue

        # Daten für das Diagramm vorbereiten
        pv_times = [runtime_per_period.get(p, {"Direkter PV-Strom": 0})["Direkter PV-Strom"] for p in all_periods]
        battery_times = [runtime_per_period.get(p, {"Strom aus der Batterie": 0})["Strom aus der Batterie"] for p in
                         all_periods]
        mixed_times = [runtime_per_period.get(p, {"PV + Netzstrom": 0})["PV + Netzstrom"] for p in all_periods]
        grid_times = [runtime_per_period.get(p, {"Strom vom Netz": 0})["Strom vom Netz"] for p in all_periods]
        unknown_times = [runtime_per_period.get(p, {"Unbekannt": 0})["Unbekannt"] for p in all_periods]

        plt.figure(figsize=(12, 6))
        plt.bar(all_periods, pv_times, label="PV", color=color_map["Direkter PV-Strom"])
        plt.bar(all_periods, battery_times, bottom=pv_times, label="Batterie",
                color=color_map["Strom aus der Batterie"])
        plt.bar(all_periods, mixed_times, bottom=[pv + bat for pv, bat in zip(pv_times, battery_times)],
                label="PV + Netz", color=color_map["PV + Netzstrom"])
        plt.bar(all_periods, grid_times,
                bottom=[pv + bat + mix for pv, bat, mix in zip(pv_times, battery_times, mixed_times)],
                label="Netz", color=color_map["Strom vom Netz"])
        plt.bar(all_periods, unknown_times,
                bottom=[pv + bat + mix + grid for pv, bat, mix, grid in
                        zip(pv_times, battery_times, mixed_times, grid_times)],
                label="Unbekannt", color=color_map["Unbekannt"])

        plt.xlabel("Periode" if period_type == "day" else f"{period_type.capitalize()} (Startdatum)")
        plt.ylabel("Laufzeit (Minuten)")
        plt.title(f"Kompressor-Laufzeiten (letzte {days} Tage, {period_type})")
        plt.legend(loc="upper left")
        plt.xticks(all_periods, rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", CHAT_ID)
        form.add_field("caption", f"📊 Laufzeiten der letzten {days} Tage ({period_type})")
        form.add_field("photo", buf, filename="runtime_chart.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Laufzeit-Diagramm für {days} Tage ({period_type}) gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Kritischer Fehler in get_runtime_bar_chart: {e}", exc_info=True)
        await send_telegram_message(session, CHAT_ID, f"Kritischer Fehler beim Erstellen des Diagramms: {str(e)}")

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
    feedin_power = solax_data.get("feedinpower", 0)  # positiv = Einspeisung, negativ = Bezug
    consumption = solax_data.get("consumeenergy", 0)

    if feedin_power < 0:  # Wir beziehen Strom vom Netz
        if pv_production > 0:
            return "PV + Netzstrom"
        else:
            return "Strom vom Netz"
    elif feedin_power > 0:  # Wir speisen ein
        return "Direkter PV-Strom"
    elif bat_power < 0:  # Batterie entlädt
        return "Strom aus der Batterie"
    elif pv_production > 0 and bat_power >= 0 and feedin_power == 0:  # PV deckt Verbrauch
        return "Direkter PV-Strom"
    else:
        return "Unbekannt"

# Funktion für die benutzerdefinierte Telegram-Tastatur
def get_custom_keyboard():
    """Erstellt eine benutzerdefinierte Tastatur mit verfügbaren Befehlen."""
    keyboard = [
        ["🌡️ Temperaturen", "📊 Status"],
        ["📈 Verlauf 6h", "📉 Verlauf 24h"],
        ["🌴 Urlaub", "🏠 Urlaub aus"],  # Angepasst
        ["🆘 Hilfe", "⏱️ Laufzeiten"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


# Asynchrone Hilfsfunktionen für Telegram
async def send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = f"🌡️ Aktuelle Temperaturen:\nKessel oben: {t_boiler_oben:.2f} °C\nKessel hinten: {t_boiler_hinten:.2f} °C\nVerdampfer: {t_verd:.2f} °C"
    return await send_telegram_message(session, CHAT_ID, message)


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


async def send_status_telegram(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, aktueller_einschaltpunkt, aktueller_ausschaltpunkt):
    """Sendet eine schönere und informativere Statusmeldung per Telegram."""
    config = validate_config(load_config())
    is_night = is_nighttime(config)
    nachtabsenkung = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 20))
    t_boiler = (t_boiler_oben + t_boiler_hinten + t_boiler_mittig) / 3 if all(isinstance(t, (int, float)) for t in [t_boiler_oben, t_boiler_hinten, t_boiler_mittig]) else "N/A"

    # Basisnachricht
    message = (
        "🌡️ *Temperaturen*\n"
        f"  Boiler oben: {t_boiler_oben:.2f} °C\n"
        f"  Boiler mittig: {t_boiler_mittig:.2f} °C\n"
        f"  Boiler hinten: {t_boiler_hinten:.2f} °C\n"
        f"  Durchschnitt: {t_boiler:.2f} °C\n"
        f"  Verdampfer: {t_verd:.2f} °C\n\n"
        f"🔧 *Kompressor*: {'🟢 EIN' if kompressor_status else '🔴 AUS'}\n"
        f"⏱️ *Aktuelle Laufzeit*: {str(aktuelle_laufzeit).split('.')[0]}\n"
        f"⏳ *Heute gesamt*: {str(gesamtlaufzeit).split('.')[0]}\n\n"
    )

    # Sollwerte und Abstand
    temp_diff = aktueller_einschaltpunkt - t_boiler_oben if kompressor_status else t_boiler_oben - aktueller_ausschaltpunkt
    temp_status = (
        f"  Noch {abs(temp_diff):.2f} °C bis {'Einschalten' if temp_diff > 0 else 'Ausschalten'}\n"
        if isinstance(t_boiler_oben, (int, float)) else ""
    )

    if urlaubsmodus_aktiv:
        message += (
            "🎯 *Sollwerte (Urlaubsmodus)*\n"
            f"  Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
            f"  Ausschaltpunkt: {aktueller_ausschaltpunkt} °C\n"
            f"{temp_status}\n"
        )
    elif solar_ueberschuss_aktiv:
        message += (
            "🎯 *Sollwerte (Solarüberschuss)*\n"
            f"  Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
            f"  Ausschaltpunkt: {aktueller_ausschaltpunkt} °C\n"
            f"{temp_status}\n"
        )
    else:
        if is_night:
            message += (
                "🎯 *Sollwerte (Nachtbetrieb)*\n"
                f"  Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
                f"  Ausschaltpunkt: {aktueller_ausschaltpunkt} °C\n"
                f"  Nachtabsenkung: {nachtabsenkung} °C\n"
                f"{temp_status}\n"
            )
        else:
            message += (
                "🎯 *Sollwerte (Normalbetrieb)*\n"
                f"  Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
                f"  Ausschaltpunkt: {aktueller_ausschaltpunkt} °C\n"
                f"{temp_status}\n"
            )

    # Aktive Modi
    active_modes = []
    if urlaubsmodus_aktiv:
        active_modes.append("🏖️ Urlaubsmodus")
    if solar_ueberschuss_aktiv:
        active_modes.append("☀️ Solarüberschuss")
    if is_night and not urlaubsmodus_aktiv and not solar_ueberschuss_aktiv:
        active_modes.append(f"🌙 Nachtbetrieb ({config['Heizungssteuerung']['NACHTABSENKUNG_START']}–{config['Heizungssteuerung']['NACHTABSENKUNG_END']})")
    message += "🔄 *Aktive Modi*\n  " + (", ".join(active_modes) if active_modes else "Keiner") + "\n\n"

    # Grund für Kompressor AUS
    if not kompressor_status:
        reason = ""
        if urlaubsmodus_aktiv:
            reason = "Urlaubsmodus aktiv"
        elif t_boiler_oben >= float(config["Heizungssteuerung"]["SICHERHEITS_TEMP"]):
            reason = f"Sicherheitsgrenze überschritten ({t_boiler_oben:.2f} °C > {config['Heizungssteuerung']['SICHERHEITS_TEMP']} °C)"
        elif t_verd <= float(config["Heizungssteuerung"]["VERDAMPFERTEMPERATUR"]):
            reason = f"Verdampfer zu kalt ({t_verd:.2f} °C < {config['Heizungssteuerung']['VERDAMPFERTEMPERATUR']} °C)"
        elif t_boiler_oben >= aktueller_ausschaltpunkt or t_boiler_mittig >= aktueller_ausschaltpunkt:
            reason = f"Boiler warm genug ({t_boiler_oben:.2f} °C ≥ {aktueller_ausschaltpunkt} °C)"
        else:
            reason = "Unbekannter Grund (prüfe Sensoren)"
        message += f"⚠️ *Warum aus?*\n  {reason}"

    # Optional: Solarinformationen (falls gewünscht)
    solax_data = await get_solax_data(session) or {}
    if solax_data.get("batPower") is not None:
        message += (
            "\n\n🔋 *Solarstatus*\n"
            f"  Batterieleistung: {solax_data.get('batPower', 0)} W\n"
            f"  Einspeisung: {solax_data.get('feedinpower', 0)} W\n"
            f"  Ladestand: {solax_data.get('soc', 0)} %"
        )

    await send_telegram_message(session, CHAT_ID, message)
    logging.info(f"Telegram-Nachricht gesendet: {message}")

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
        "📈 *Verlauf 6h* – Zeigt den Temperaturverlauf der letzten 6 Stunden.\n"
        "📉 *Verlauf 24h* – Zeigt den Temperaturverlauf der letzten 24 Stunden.\n"
        "⏱️ *Laufzeiten* – Zeigt die Laufzeiten des Kompressors.\n"  # Neuer Befehl
        "🌴 *Urlaub* – Aktiviert den Urlaubsmodus.\n"
        "🏠 *Urlaub aus* – Deaktiviert den Urlaubsmodus.\n"
        "🆘 *Hilfe* – Zeigt diese Nachricht an."
    )
    return await send_telegram_message(session, CHAT_ID, message, parse_mode="Markdown")

async def shutdown(session):
    """Sendet eine Telegram-Nachricht beim Programmende und bereinigt Ressourcen."""
    now = datetime.now()
    message = f"🛑 Programm beendet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
    await send_telegram_message(session, CHAT_ID, message)
    GPIO.output(GIO21_PIN, GPIO.LOW)  # Kompressor ausschalten
    GPIO.cleanup()  # GPIO-Pins bereinigen
    if lcd is not None:  # Nur schließen, wenn lcd initialisiert wurde
        lcd.close()  # LCD schließen
    logging.info("Heizungssteuerung sicher beendet, Hardware in sicherem Zustand.")

# Hauptprogrammstart
async def run_program():
    async with aiohttp.ClientSession() as session:
        # Prüfe und aktualisiere den CSV-Header
        await update_csv_header_if_needed()
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
                    logging.error(f"Unrealistischer Temperaturwert von Sensor {sensor_id}: {temp} °C. Sensor als fehlerhaft betrachtet.")
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


def check_boiler_sensors(t_oben, t_hinten, t_mittig, config):
    """Prüft die Boiler-Sensoren (oben, hinten, mittig) auf Fehler."""
    try:
        # Verwende SICHERHEITS_TEMP statt eines dynamischen Ausschaltpunkts für Übertemperatur
        sicherheits_temp = int(config["Heizungssteuerung"]["SICHERHEITS_TEMP"])
    except (KeyError, ValueError):
        sicherheits_temp = 51
        logging.warning(f"SICHERHEITS_TEMP nicht gefunden, verwende Standard: {sicherheits_temp}")

    fehler = None
    is_overtemp = False

    # Prüfe, ob ein Sensorwert fehlt
    if t_oben is None or t_hinten is None or t_mittig is None:
        fehler = "Fühlerfehler!"
        logging.error(f"Fühlerfehler erkannt: oben={t_oben}, hinten={t_hinten}, mittig={t_mittig}")
    # Prüfe auf Übertemperatur für alle Sensoren
    elif t_oben >= sicherheits_temp or t_hinten >= sicherheits_temp or t_mittig >= sicherheits_temp:
        fehler = "Übertemperatur!"
        is_overtemp = True
        logging.error(
            f"Übertemperatur erkannt: oben={t_oben}, hinten={t_hinten}, mittig={t_mittig}, Grenze={sicherheits_temp}")
    # Prüfe auf unplausible Differenzen zwischen den Sensoren
    elif max(abs(t_oben - t_hinten), abs(t_oben - t_mittig), abs(t_hinten - t_mittig)) > 50:
        fehler = "Fühlerdifferenz!"
        logging.warning(f"Fühlerdifferenz erkannt: oben={t_oben}, hinten={t_hinten}, mittig={t_mittig}, "
                        f"Max. Differenz={max(abs(t_oben - t_hinten), abs(t_oben - t_mittig), abs(t_hinten - t_mittig))}")

    return fehler, is_overtemp


def set_gpio_state(pin, state):
    """Setze den GPIO-Pin auf den gewünschten Zustand und überprüfe ihn."""
    try:
        GPIO.output(pin, state)
        actual_state = GPIO.input(pin)
        if actual_state != state:
            logging.error(f"GPIO {pin} konnte nicht auf {'HIGH' if state else 'LOW'} gesetzt werden!")
            return False
        return True
    except Exception as e:
        logging.error(f"Fehler beim Setzen von GPIO {pin}: {e}", exc_info=True)
        return False

compressor_lock = asyncio.Lock()

async def set_kompressor_status(status, force_off=False):
    global kompressor_ein, start_time, last_shutdown_time, current_runtime, ausschluss_grund
    async with compressor_lock:
        try:
            now = datetime.now()
            if status and not kompressor_ein:
                if not force_off and last_shutdown_time:
                    pause_time = now - last_shutdown_time
                    if pause_time < MIN_PAUSE:
                        ausschluss_grund = f"Zu kurze Pause ({pause_time.total_seconds():.0f}s < {MIN_PAUSE.total_seconds()}s)"
                        logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                        return
                if not set_gpio_state(GIO21_PIN, GPIO.HIGH):
                    ausschluss_grund = "GPIO-Fehler beim Einschalten"
                    logging.error(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                    return
                kompressor_ein = True
                start_time = now
                ausschluss_grund = None
                logging.info("Kompressor EINGESCHALTET")
            elif (not status and kompressor_ein) or force_off:
                if not set_gpio_state(GIO21_PIN, GPIO.LOW):
                    ausschluss_grund = "GPIO-Fehler beim Ausschalten"
                    logging.error(f"Kompressor nicht ausgeschaltet: {ausschluss_grund}")
                    return
                kompressor_ein = False
                last_shutdown_time = now
                if start_time:
                    current_runtime = now - start_time
                    logging.info(f"Kompressor AUSGESCHALTET. Laufzeit: {current_runtime}")
                start_time = None
        except Exception as e:
            ausschluss_grund = f"Interner Fehler: {str(e)}"
            logging.error(f"KRITISCHER FEHLER in set_kompressor_status: {ausschluss_grund}", exc_info=True)
            set_gpio_state(GIO21_PIN, GPIO.LOW)
            kompressor_ein = False

# Asynchrone Funktion zum Neuladen der Konfiguration
async def reload_config(session):
    """Lädt die Konfiguration neu und aktualisiert globale Variablen."""
    global config, BOT_TOKEN, CHAT_ID, MIN_LAUFZEIT, MIN_PAUSE, VERDAMPFERTEMPERATUR, SOLAR_AUS, SOLAR_EIN, NORMAL_AUS, NORMAL_EIN, NACHT_EIN, SICHERHEITS_TEMP, TOKEN_ID, SN, aktueller_einschaltpunkt, aktueller_ausschaltpunkt

    try:
        config = load_config()
        logging.info("Konfiguration erfolgreich neu geladen.")

        # Telegram
        BOT_TOKEN = config["Telegram"]["BOT_TOKEN"]
        CHAT_ID = config["Telegram"]["CHAT_ID"]

        # Heizungssteuerung mit Min/Max-Grenzen
        MIN_LAUFZEIT = timedelta(minutes=min(max(int(config["Heizungssteuerung"].get("MIN_LAUFZEIT", 5)), 1), 60))
        MIN_PAUSE = timedelta(minutes=min(max(int(config["Heizungssteuerung"].get("MIN_PAUSE", 5)), 1), 60))
        VERDAMPFERTEMPERATUR = min(max(int(config["Heizungssteuerung"].get("VERDAMPFERTEMPERATUR", 6)), -20), 20)
        SOLAR_AUS = min(max(int(config["Heizungssteuerung"].get("SOLAR_AUS", 50)), 40), 60)
        SOLAR_EIN = min(max(int(config["Heizungssteuerung"].get("SOLAR_EIN", 45)), 35), 55)
        NORMAL_AUS = min(max(int(config["Heizungssteuerung"].get("NORMAL_AUS", 45)), 40), 60)
        NORMAL_EIN = min(max(int(config["Heizungssteuerung"].get("NORMAL_EIN", 42)), 35), 55)
        NACHT_EIN = min(max(int(config["Heizungssteuerung"].get("NACHT_EIN", 30)), 20), 40)
        SICHERHEITS_TEMP = min(max(int(config["Heizungssteuerung"].get("SICHERHEITS_TEMP", 51)), 45), 70)

        # SolaxCloud
        TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
        SN = config["SolaxCloud"]["SN"]

        # Aktualisiere aktuelle Sollwerte nur, wenn Urlaubsmodus nicht aktiv ist
        if not urlaubsmodus_aktiv:
            aktueller_ausschaltpunkt = NORMAL_AUS
            aktueller_einschaltpunkt = NORMAL_EIN
            logging.info(f"Aktuelle Sollwerte aktualisiert: Ausschaltpunkt={aktueller_ausschaltpunkt}, Einschaltpunkt={aktueller_einschaltpunkt}")

        await send_telegram_message(session, CHAT_ID, "🔄 Konfiguration erfolgreich neu geladen.")
    except Exception as e:
        logging.error(f"Fehler beim Neuladen der Konfiguration: {e}")
        await send_telegram_message(session, CHAT_ID, f"⚠️ Fehler beim Neuladen der Konfiguration: {e}")


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
            "MIN_LAUFZEIT": "5",
            "MIN_PAUSE": "5",
            "NACHTABSENKUNG_START": "19:30",
            "NACHTABSENKUNG_END": "8:00",
            "VERDAMPFERTEMPERATUR": "6",
            "SOLAR_AUS": "50",
            "SOLAR_EIN": "45",
            "NORMAL_AUS": "45",
            "NORMAL_EIN": "42",
            "NACHT_EIN": "30",
            "SICHERHEITS_TEMP": "51",
            "NACHTABSENKUNG": "20"  # Standardwert für Nachtabsenkung
        },
        "Telegram": {"BOT_TOKEN": "", "CHAT_ID": ""},
        "SolaxCloud": {"TOKEN_ID": "", "SN": ""},
        "Urlaubsmodus": {"URLAUBSABSENKUNG": "15"}
    }

    for section in defaults:
        if section not in config:
            config[section] = {}
            logging.warning(f"Abschnitt {section} fehlt in config.ini, wird mit Standardwerten erstellt.")

        for key, default in defaults[section].items():
            if key not in config[section]:
                config[section][key] = default
                logging.warning(f"Schlüssel {key} in {section} fehlt, verwende Standardwert: {default}")
            else:
                # Validierung numerischer Werte
                if key in ["MIN_LAUFZEIT", "MIN_PAUSE", "VERDAMPFERTEMPERATUR",
                           "SOLAR_AUS", "SOLAR_EIN", "NORMAL_AUS", "NORMAL_EIN",
                           "NACHT_EIN", "SICHERHEITS_TEMP", "NACHTABSENKUNG",
                           "URLAUBSABSENKUNG"]:
                    try:
                        val = int(config[section][key])
                        min_val = 0
                        max_val = 100
                        # Spezifische Grenzen für bestimmte Parameter
                        if key == "VERDAMPFERTEMPERATUR":
                            min_val, max_val = -20, 20
                        elif key in ["MIN_LAUFZEIT", "MIN_PAUSE"]:
                            min_val, max_val = 1, 60
                        elif key in ["SOLAR_AUS", "NORMAL_AUS"]:
                            min_val, max_val = 40, 60
                        elif key in ["SOLAR_EIN", "NORMAL_EIN"]:
                            min_val, max_val = 35, 55
                        elif key == "NACHT_EIN":
                            min_val, max_val = 20, 40
                        elif key == "SICHERHEITS_TEMP":
                            min_val, max_val = 45, 70

                        if not (min_val <= val <= max_val):
                            logging.warning(
                                f"Ungültiger Wert für {key} in {section}: {val}. Verwende Standardwert: {default}")
                            config[section][key] = default
                    except ValueError:
                        logging.warning(f"Ungültiger Wert für {key} in {section}. Verwende Standardwert: {default}")
                        config[section][key] = default

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

        logging.debug(f"Nachtzeitprüfung: Jetzt={now_time}, Start={start_time_minutes}, Ende={end_time_minutes}, Ist Nacht={is_night}")
        return is_night
    except Exception as e:
        logging.error(f"Fehler in is_nighttime: {e}")
        return False


def calculate_shutdown_point(config, is_night, solax_data):
    global solar_ueberschuss_aktiv, urlaubsmodus_aktiv
    try:
        bat_power = solax_data.get("batPower", 0)
        feedin_power = solax_data.get("feedinpower", 0)
        soc = solax_data.get("soc", 0)

        # Solarüberschuss-Logik
        if solax_data.get("api_fehler", False):
            solar_ueberschuss_aktiv = False
        else:
            if bat_power > 600 or (soc > 95 and feedin_power > 600):
                if not solar_ueberschuss_aktiv:
                    solar_ueberschuss_aktiv = True
                    logging.info(f"Solarüberschuss aktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")
            else:
                if solar_ueberschuss_aktiv:
                    solar_ueberschuss_aktiv = False
                    logging.info(f"Solarüberschuss deaktiviert: batPower={bat_power}, feedinpower={feedin_power}, soc={soc}")

        # Sollwerte berechnen
        if urlaubsmodus_aktiv:
            urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 15))
            ausschaltpunkt = int(config["Heizungssteuerung"]["NORMAL_AUS"]) - urlaubsabsenkung
            einschaltpunkt = int(config["Heizungssteuerung"]["NORMAL_EIN"]) - urlaubsabsenkung
        elif solar_ueberschuss_aktiv:
            ausschaltpunkt = int(config["Heizungssteuerung"]["SOLAR_AUS"])
            einschaltpunkt = int(config["Heizungssteuerung"]["SOLAR_EIN"])
        else:
            if is_night:
                ausschaltpunkt = int(config["Heizungssteuerung"]["NORMAL_AUS"])
                einschaltpunkt = int(config["Heizungssteuerung"]["NACHT_EIN"])
            else:
                ausschaltpunkt = int(config["Heizungssteuerung"]["NORMAL_AUS"])
                einschaltpunkt = int(config["Heizungssteuerung"]["NORMAL_EIN"])

        # Sicherstellen, dass Einschaltpunkt < Ausschaltpunkt
        if einschaltpunkt >= ausschaltpunkt:
            logging.error(f"Logikfehler: Einschaltpunkt ({einschaltpunkt}) >= Ausschaltpunkt ({ausschaltpunkt})")
            temp_offset = 3
            ausschaltpunkt = einschaltpunkt + temp_offset

        logging.debug(f"Sollwerte berechnet: Solarüberschuss_aktiv={solar_ueberschuss_aktiv}, "
                      f"Urlaubsmodus_aktiv={urlaubsmodus_aktiv}, Ausschaltpunkt={ausschaltpunkt}, "
                      f"Einschaltpunkt={einschaltpunkt}")
        return ausschaltpunkt, einschaltpunkt
    except (KeyError, ValueError) as e:
        logging.error(f"Fehler beim Berechnen der Sollwerte: {e}, Solax-Daten={solax_data}")
        return 45, 42  # Fallback-Werte

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


# Asynchrone Task für Telegram-Updates
async def telegram_task():
    """Task zum kontinuierlichen Abrufen und Verarbeiten von Telegram-Nachrichten."""
    global last_update_id, kompressor_ein, current_runtime, total_runtime_today, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, aktueller_einschaltpunkt, aktueller_ausschaltpunkt, last_runtime
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id}&timeout=30"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        updates = data.get("result", [])
                        if updates:
                            last_update_id = await process_telegram_messages_async(
                                session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates, last_update_id,
                                kompressor_ein, str(current_runtime).split('.')[0] if kompressor_ein else "0",
                                str(total_runtime_today).split('.')[0], str(last_runtime).split('.')[0]
                            )
                    else:
                        logging.error(f"Fehler beim Abrufen von Telegram-Updates: Status {response.status}")
            except Exception as e:
                logging.error(f"Fehler in telegram_task: {e}", exc_info=True)
            await asyncio.sleep(2)

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
                t_boiler_mittig = await asyncio.to_thread(read_temperature, SENSOR_IDS["mittig"])
                t_verd = await asyncio.to_thread(read_temperature, SENSOR_IDS["verd"])
                t_boiler = (
                    (t_boiler_oben + t_boiler_hinten + t_boiler_mittig) / 3
                    if t_boiler_oben is not None and t_boiler_hinten is not None and t_boiler_mittig is not None
                    else "Fehler"
                )
                pressure_ok = await asyncio.to_thread(check_pressure)

                lcd.clear()
                if not pressure_ok:
                    lcd.write_string("FEHLER: Druck zu niedrig")
                    logging.error(f"Display zeigt Druckfehler: Druckschalter={pressure_ok}")
                else:
                    # Prüfe Typ und formatiere entsprechend
                    oben_str = f"{t_boiler_oben:.1f}" if isinstance(t_boiler_oben, (int, float)) else "Fehler"
                    hinten_str = f"{t_boiler_hinten:.1f}" if isinstance(t_boiler_hinten, (int, float)) else "Fehler"
                    mittig_str = f"{t_boiler_mittig:.1f}" if isinstance(t_boiler_mittig, (int, float)) else "Fehler"
                    verd_str = f"{t_verd:.1f}" if isinstance(t_verd, (int, float)) else "Fehler"

                    lcd.write_string(f"T-Oben: {oben_str} C")
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string(f"T-Mittig: {mittig_str} C")
                    lcd.cursor_pos = (2, 0)
                    lcd.write_string(f"T-Hinten: {hinten_str} C")
                    lcd.cursor_pos = (3, 0)
                    lcd.write_string(f"T-Verd: {verd_str} C")
                    logging.debug(
                        f"Display-Seite 1 aktualisiert: oben={oben_str}, mittig={mittig_str}, hinten={hinten_str}, verd={verd_str}")
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


async def read_all_sensors():
    """Lese alle Temperatursensoren und gebe sie als Dictionary zurück."""
    sensors = {
        "oben": SENSOR_IDS["oben"],
        "mittig": SENSOR_IDS["mittig"],
        "hinten": SENSOR_IDS["hinten"],
        "verd": SENSOR_IDS["verd"]
    }
    temps = {}
    for name, sensor_id in sensors.items():
        temps[name] = await asyncio.to_thread(read_temperature, sensor_id)
    return temps

async def control_compressor(t_oben, t_mittig, t_hinten, t_verd, solar_active, urlaub, config):
    global kompressor_ein, ausschluss_grund, aktueller_einschaltpunkt, aktueller_ausschaltpunkt

    # Prüfe auf Sensorfehler
    if t_oben is None or t_mittig is None or t_hinten is None or t_verd is None:
        ausschluss_grund = "Sensorfehler"
        logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
        return

    # Sicherheitsprüfung: Kein Heizen, wenn ein Fühler über SICHERHEITS_TEMP liegt
    if t_oben > SICHERHEITS_TEMP or t_mittig > SICHERHEITS_TEMP or t_hinten > SICHERHEITS_TEMP:
        ausschluss_grund = f"Sicherheitstemperatur überschritten ({SICHERHEITS_TEMP}°C)"
        logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
        return

    # Urlaubsmodus
    if urlaub:
        ausschluss_grund = "Urlaubsmodus aktiv"
        logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
        return

    # PV-Strom vorhanden (Solarüberschuss)
    if solar_active:
        # Ausschalten, wenn ein Sensor SOLAR_AUS erreicht
        if t_oben >= SOLAR_AUS or t_mittig >= SOLAR_AUS or t_hinten >= SOLAR_AUS:
            if kompressor_ein:
                await asyncio.to_thread(set_kompressor_status, False)
                logging.info(f"Kompressor ausgeschaltet: PV-Überschuss, ein Fühler >= {SOLAR_AUS}°C")
                ausschluss_grund = f"Max. Temperatur erreicht ({SOLAR_AUS}°C)"
        # Einschalten, wenn der untere Wert (t_hinten) unter SOLAR_EIN fällt
        elif t_hinten < SOLAR_EIN:
            if not kompressor_ein:
                await asyncio.to_thread(set_kompressor_status, True)
                logging.info(f"Kompressor eingeschaltet: PV-Überschuss, t_hinten < {SOLAR_EIN}°C")
            elif not kompressor_ein and ausschluss_grund:
                logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")

    # Netzstrom oder Batterie
    else:
        is_night = is_nighttime(config)
        if is_night:
            # Nachtmodus: Oberer Fühler darf nicht unter NACHT_EIN fallen
            if t_oben < NACHT_EIN:
                if not kompressor_ein:
                    await asyncio.to_thread(set_kompressor_status, True)
                    logging.info(f"Kompressor eingeschaltet: Nachtmodus, t_oben < {NACHT_EIN}°C")
                elif not kompressor_ein and ausschluss_grund:
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
            elif t_oben >= NORMAL_AUS and t_mittig >= NORMAL_AUS:
                if kompressor_ein:
                    await asyncio.to_thread(set_kompressor_status, False)
                    logging.info(f"Kompressor ausgeschaltet: Nachtmodus, t_oben und t_mittig >= {NORMAL_AUS}°C")
        else:
            # Normalbetrieb: Kompressor ein, wenn t_oben oder t_mittig unter NORMAL_EIN fällt
            if t_oben < NORMAL_EIN or t_mittig < NORMAL_EIN:
                if not kompressor_ein:
                    await asyncio.to_thread(set_kompressor_status, True)
                    logging.info(f"Kompressor eingeschaltet: Normalbetrieb, t_oben oder t_mittig < {NORMAL_EIN}°C")
                elif not kompressor_ein and ausschluss_grund:
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
            # Ausschalten, wenn t_oben und t_mittig NORMAL_AUS erreichen
            elif t_oben >= NORMAL_AUS and t_mittig >= NORMAL_AUS:
                if kompressor_ein:
                    await asyncio.to_thread(set_kompressor_status, False)
                    logging.info(f"Kompressor ausgeschaltet: Normalbetrieb, t_oben und t_mittig >= {NORMAL_AUS}°C")
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
    global last_update_id, kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time, last_config_hash, last_log_time, last_kompressor_status, urlaubsmodus_aktiv, pressure_error_sent, aktueller_einschaltpunkt, aktueller_ausschaltpunkt, ausschluss_grund, t_boiler, last_pressure_error_time, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd

    if not await initialize_gpio():
        logging.critical("Programm wird aufgrund fehlender GPIO-Initialisierung beendet.")
        exit(1)

    await initialize_lcd(session)

    now = datetime.now()
    message = f"✅ Programm gestartet am {now.strftime('%d.%m.%Y um %H:%M:%S')}"
    await send_telegram_message(session, CHAT_ID, message)
    await send_welcome_message(session, CHAT_ID)

    telegram_task_handle = asyncio.create_task(telegram_task())
    display_task_handle = asyncio.create_task(display_task())

    last_cycle_time = datetime.now()
    watchdog_warning_count = 0
    WATCHDOG_MAX_WARNINGS = 3

    try:
        while True:
            pressure_ok = await asyncio.to_thread(check_pressure)
            logging.info(
                f"Regelungscheck: oben={t_boiler_oben} mittig={t_boiler_mittig} hinten={t_boiler_hinten} verd={t_verd}")
            logging.info(f"Solarüberschuss: {solar_ueberschuss_aktiv} | Drucksensor: {pressure_ok}")

            try:
                now = datetime.now()
                should_check_day = (last_log_time is None or (now - last_log_time) >= timedelta(minutes=1))
                if should_check_day:
                    current_day = now.date()
                    if current_day != last_day:
                        logging.info(f"Neuer Tag erkannt: {current_day}. Setze Gesamtlaufzeit zurück.")
                        total_runtime_today = timedelta()
                        last_day = current_day

                config = validate_config(load_config())
                current_hash = calculate_file_hash("config.ini")
                if last_config_hash != current_hash:
                    await reload_config(session)
                    last_config_hash = current_hash

                solax_data = await get_solax_data(session)
                if solax_data is None:
                    if last_api_data and not is_data_old(last_api_timestamp):
                        solax_data = last_api_data
                        logging.warning("API-Anfrage fehlgeschlagen, verwende zwischengespeicherte Daten.")
                    else:
                        solax_data = {"acpower": 0, "feedinpower": 0, "consumeenergy": 0,
                                      "batPower": 0, "soc": 0, "powerdc1": 0, "powerdc2": 0,
                                      "api_fehler": True}
                        logging.error(
                            "API-Anfrage fehlgeschlagen, keine gültigen zwischengespeicherten Daten verfügbar.")

                power_source = get_power_source(solax_data)

                acpower = solax_data.get("acpower", "N/A")
                feedinpower = solax_data.get("feedinpower", "N/A")
                batPower = solax_data.get("batPower", "N/A")
                soc = solax_data.get("soc", "N/A")
                powerdc1 = solax_data.get("powerdc1", "N/A")
                powerdc2 = solax_data.get("powerdc2", "N/A")
                consumeenergy = solax_data.get("consumeenergy", "N/A")

                is_night = is_nighttime(config)
                # Sollwerte nur aktualisieren, wenn Urlaubsmodus nicht aktiv ist
                if not urlaubsmodus_aktiv:
                    aktueller_ausschaltpunkt, aktueller_einschaltpunkt = calculate_shutdown_point(config, is_night,
                                                                                                  solax_data)

                temps = await read_all_sensors()
                t_boiler_oben = temps["oben"]
                t_boiler_mittig = temps["mittig"]
                t_boiler_hinten = temps["hinten"]
                t_verd = temps["verd"]
                t_boiler = (
                    (t_boiler_oben + t_boiler_hinten + t_boiler_mittig) / 3
                    if all(t is not None for t in [t_boiler_oben, t_boiler_hinten, t_boiler_mittig])
                    else "Fehler"
                )

                pressure_ok = await asyncio.to_thread(check_pressure)
                logging.info(
                    f"Regelungscheck: oben={t_boiler_oben} mittig={t_boiler_mittig} hinten={t_boiler_hinten} verd={t_verd}")
                logging.info(f"Solarüberschuss: {solar_ueberschuss_aktiv} | Drucksensor: {pressure_ok}")
                now = datetime.now()

                if not pressure_ok:
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    last_pressure_error_time = now
                    if not pressure_error_sent:
                        error_msg = "❌ Druckfehler: Kompressor läuft nicht aufgrund eines Problems mit dem Druckschalter! 5-Minuten-Sperre aktiviert."
                        await send_telegram_message(session, CHAT_ID, error_msg)
                        pressure_error_sent = True
                    ausschluss_grund = "Druckschalter offen"
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                    await asyncio.sleep(2)
                    continue

                if pressure_error_sent and (
                        last_pressure_error_time is None or (now - last_pressure_error_time) >= PRESSURE_ERROR_DELAY):
                    info_msg = "✅ Druckschalter wieder normal. Kompressor kann wieder laufen."
                    await send_telegram_message(session, CHAT_ID, info_msg)
                    pressure_error_sent = False
                    last_pressure_error_time = None

                fehler, is_overtemp = check_boiler_sensors(t_boiler_oben, t_boiler_hinten, t_boiler_mittig, config)
                if fehler:
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    ausschluss_grund = fehler
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                    await asyncio.sleep(2)
                    continue

                if last_pressure_error_time and (now - last_pressure_error_time) < PRESSURE_ERROR_DELAY:
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    remaining_time = (PRESSURE_ERROR_DELAY - (now - last_pressure_error_time)).total_seconds()
                    ausschluss_grund = f"Druckfehler-Sperre ({remaining_time:.0f}s verbleibend)"
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                    await asyncio.sleep(2)
                    continue

                if t_verd is not None and t_verd < VERDAMPFERTEMPERATUR:
                    if kompressor_ein:
                        await asyncio.to_thread(set_kompressor_status, False)
                    ausschluss_grund = f"Verdampfer zu kalt ({t_verd:.1f}°C < {VERDAMPFERTEMPERATUR}°C)"
                    logging.info(f"Kompressor nicht eingeschaltet: {ausschluss_grund}")
                    await asyncio.sleep(2)
                    continue

                # Steuerlogik ausgelagert
                await control_compressor(t_boiler_oben, t_boiler_mittig, t_boiler_hinten, t_verd,
                                         solar_ueberschuss_aktiv, urlaubsmodus_aktiv, config)

                # Laufzeit aktualisieren
                if kompressor_ein and start_time:
                    current_runtime = datetime.now() - start_time
                else:
                    current_runtime = timedelta(seconds=0)

                # Logging in CSV
                now = datetime.now()
                should_log = (last_log_time is None or (now - last_log_time) >= timedelta(minutes=1)) or (
                        kompressor_ein != last_kompressor_status)
                if should_log:
                    async with csv_lock:
                        async with aiofiles.open("heizungsdaten.csv", 'a', newline='') as csvfile:
                            einschaltpunkt_str = str(
                                aktueller_einschaltpunkt) if aktueller_einschaltpunkt is not None else "N/A"
                            ausschaltpunkt_str = str(
                                aktueller_ausschaltpunkt) if aktueller_ausschaltpunkt is not None else "N/A"
                            solar_ueberschuss_str = "1" if solar_ueberschuss_aktiv else "0"
                            urlaubsmodus_str = "1" if urlaubsmodus_aktiv else "0"  # Neuer Eintrag für Urlaubsmodus
                            power_source_str = power_source if power_source else "N/A"

                            csv_line = (
                                f"{now.strftime('%Y-%m-%d %H:%M:%S')},"
                                f"{t_boiler_oben if t_boiler_oben is not None else 'N/A'},"
                                f"{t_boiler_hinten if t_boiler_hinten is not None else 'N/A'},"
                                f"{t_boiler_mittig if t_boiler_mittig is not None else 'N/A'},"
                                f"{t_boiler if t_boiler != 'Fehler' else 'N/A'},"
                                f"{t_verd if t_verd is not None else 'N/A'},"
                                f"{'EIN' if kompressor_ein else 'AUS'},"
                                f"{acpower},{feedinpower},{batPower},{soc},{powerdc1},{powerdc2},{consumeenergy},"
                                f"{einschaltpunkt_str},{ausschaltpunkt_str},{solar_ueberschuss_str},{urlaubsmodus_str},"
                                f"{power_source_str}\n"
                            )
                            await csvfile.write(csv_line)
                            logging.debug(f"CSV-Eintrag geschrieben: {csv_line.strip()}")
                        last_log_time = now
                        last_kompressor_status = kompressor_ein

                cycle_duration = (datetime.now() - last_cycle_time).total_seconds()
                if cycle_duration > 30:
                    watchdog_warning_count += 1
                    logging.error(f"Zyklus dauert zu lange ({cycle_duration:.2f}s), Warnung {watchdog_warning_count}/{WATCHDOG_MAX_WARNINGS}")
                    if watchdog_warning_count >= WATCHDOG_MAX_WARNINGS:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        logging.critical("Maximale Watchdog-Warnungen erreicht, Hardware wird heruntergefahren.")
                        watchdog_message = (
                            "🚨 **Kritischer Fehler**: Software wird aufgrund des Watchdogs beendet.\n"
                            f"Grund: Maximale Warnungen ({WATCHDOG_MAX_WARNINGS}) erreicht, Zykluszeit > 30s.\n"
                            f"Letzte Zykluszeit: {cycle_duration:.2f}s"
                        )
                        await send_telegram_message(session, CHAT_ID, watchdog_message, parse_mode="Markdown")
                        await shutdown(session)
                        raise SystemExit("Watchdog-Exit: Programm wird beendet.")

                last_cycle_time = datetime.now()
                await asyncio.sleep(2)
            except Exception as e:
                logging.error(f"Fehler in der Hauptschleife: {e}", exc_info=True)
                await asyncio.sleep(30)

    except Exception as e:
        logging.error(f"Fehler in main_loop: {e}", exc_info=True)
        telegram_task_handle.cancel()
        display_task_handle.cancel()
        await asyncio.gather(telegram_task_handle, display_task_handle, return_exceptions=True)
        raise

# Asynchrone Verarbeitung von Telegram-Nachrichten
def format_timedelta(td):
    """Formatiert eine timedelta in HH:MM:SS."""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def safe_float(value):
    """Sichere Umwandlung in Float mit Fallback."""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0

def format_timedelta(td):
    """Formatiert timedelta als HH:MM:SS."""
    try:
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    except:
        return "00:00:00"

async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates, last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, letzte_laufzeit):
    """Verarbeitet eingehende Telegram-Nachrichten und gibt die aktualisierte last_update_id zurück."""
    global urlaubsmodus_aktiv  # Zugriff auf die globale Variable
    if updates:
        for update in updates:
            message_text = update.get('message', {}).get('text')
            chat_id = update.get('message', {}).get('chat', {}).get('id')
            if message_text and chat_id:
                message_text_lower = message_text.strip().lower()
                logging.debug(f"Telegram-Nachricht empfangen: Text={message_text}, Chat-ID={chat_id}")
                if message_text_lower == "🌡️ temperaturen" or message_text_lower == "temperaturen":
                    if t_boiler_oben != "Fehler" and t_boiler_hinten != "Fehler" and t_boiler_mittig != "Fehler" and t_verd != "Fehler":
                        await send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_verd)
                    else:
                        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen der Temperaturen.")
                elif message_text_lower == "📊 status" or message_text_lower == "status":
                    if t_boiler_oben != "Fehler" and t_boiler_hinten != "Fehler" and t_boiler_mittig != "Fehler" and t_verd != "Fehler":
                        await send_status_telegram(
                            session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, kompressor_status,
                            aktuelle_laufzeit, gesamtlaufzeit, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
                        )
                    else:
                        await send_telegram_message(session, CHAT_ID, "Fehler beim Abrufen des Status.")
                elif message_text_lower == "⏱️ laufzeiten" or message_text_lower == "laufzeiten":
                    await get_runtime_bar_chart(session, days=7)
                elif message_text_lower == "📉 verlauf 24h" or message_text_lower == "verlauf 24h":
                    await get_boiler_temperature_history(session, 24)
                elif message_text_lower == "📈 verlauf 12h" or message_text_lower == "verlauf 12h":
                    await get_boiler_temperature_history(session, 12)
                elif message_text_lower == "📈 verlauf 6h" or message_text_lower == "verlauf 6h":
                    await get_boiler_temperature_history(session, 6)
                elif message_text_lower == "📈 verlauf 1h" or message_text_lower == "verlauf 1h":
                    await get_boiler_temperature_history(session, 1)
                elif message_text_lower == "🔛 kompressor ein":
                    await asyncio.to_thread(set_kompressor_status, True)
                    await send_telegram_message(session, CHAT_ID, "Kompressor wird angeschaltet.")
                elif message_text_lower == "🔴 kompressor aus":
                    await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                    await send_telegram_message(session, CHAT_ID, "Kompressor wird ausgeschaltet.")
                elif "🌴 urlaub" in message_text_lower or message_text_lower == "urlaub":  # Neue Bedingung
                    await aktivere_urlaubsmodus(session)
                elif "🏠 urlaub aus" in message_text_lower or message_text_lower == "urlaub aus":  # Neue Bedingung
                    await deaktivere_urlaubsmodus(session)
                elif message_text_lower == "🏖️ urlaubsmodus ein":  # Alte Bedingung beibehalten
                    await aktivere_urlaubsmodus(session)
                elif message_text_lower == "🏠 urlaubsmodus aus":  # Alte Bedingung beibehalten
                    await deaktivere_urlaubsmodus(session)
                else:
                    await send_unknown_command_message(session, CHAT_ID)
                last_update_id = update['update_id'] + 1
                logging.debug(f"last_update_id aktualisiert: {last_update_id}")
        return last_update_id
    return last_update_id

# Asynchrone Urlaubsmodus-Funktionen
async def aktivere_urlaubsmodus(session):
    global urlaubsmodus_aktiv, NORMAL_AUS, NORMAL_EIN, original_einschaltpunkt, original_ausschaltpunkt, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
    if not urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = True
        # Speichere die aktuellen Sollwerte vor der Änderung
        original_einschaltpunkt = aktueller_einschaltpunkt
        original_ausschaltpunkt = aktueller_ausschaltpunkt
        urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 15))
        # Alte Werte speichern für Logging
        old_einschaltpunkt = aktueller_einschaltpunkt
        old_ausschaltpunkt = aktueller_ausschaltpunkt
        # Neue Sollwerte berechnen
        aktueller_ausschaltpunkt = NORMAL_AUS - urlaubsabsenkung
        aktueller_einschaltpunkt = NORMAL_EIN - urlaubsabsenkung
        logging.info(
            f"Urlaubsmodus aktiviert. Sollwerte geändert: "
            f"Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}"
        )
        # Telegram-Nachricht senden
        await send_telegram_message(session, CHAT_ID,
                                    f"🌴 Urlaubsmodus aktiviert. Neue Werte:\n"
                                    f"Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
                                    f"Ausschaltpunkt: {aktueller_ausschaltpunkt} °C")

async def deaktivere_urlaubsmodus(session):
    global urlaubsmodus_aktiv, NORMAL_AUS, NORMAL_EIN, original_einschaltpunkt, original_ausschaltpunkt, aktueller_einschaltpunkt, aktueller_ausschaltpunkt
    if urlaubsmodus_aktiv:
        urlaubsmodus_aktiv = False
        # Alte Werte speichern für Logging
        old_einschaltpunkt = aktueller_einschaltpunkt
        old_ausschaltpunkt = aktueller_ausschaltpunkt
        # Ursprüngliche Sollwerte wiederherstellen
        aktueller_einschaltpunkt = original_einschaltpunkt
        aktueller_ausschaltpunkt = original_ausschaltpunkt
        logging.info(
            f"Urlaubsmodus deaktiviert. Sollwerte wiederhergestellt: "
            f"Ausschaltpunkt={old_ausschaltpunkt} -> {aktueller_ausschaltpunkt}, "
            f"Einschaltpunkt={old_einschaltpunkt} -> {aktueller_einschaltpunkt}"
        )
        # Telegram-Nachricht senden
        await send_telegram_message(session, CHAT_ID,
                                    f"🏠 Urlaubsmodus deaktiviert. Ursprüngliche Werte:\n"
                                    f"Einschaltpunkt: {aktueller_einschaltpunkt} °C\n"
                                    f"Ausschaltpunkt: {aktueller_ausschaltpunkt} °C")

# Programmstart
if __name__ == "__main__":
    try:
        asyncio.run(run_program())
    except KeyboardInterrupt:
        logging.info("Programm durch Benutzer abgebrochen")
    except Exception as e:
        logging.critical(f"Kritischer Fehler beim Programmstart: {e}", exc_info=True)
    finally:
        try:
            GPIO.cleanup()
            if lcd is not None:
                lcd.close()
        except Exception as e:
            logging.error(f"Fehler bei der Bereinigung der Ressourcen: {e}")