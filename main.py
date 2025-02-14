import os
import glob
import time
import smbus2
import requests
from datetime import datetime, timedelta
from RPLCD.i2c import CharLCD
import RPi.GPIO as GPIO
import logging
import configparser
import csv
from datetime import timedelta

# Konfiguration (in eine separate Datei auslagern)
BASE_DIR = "/sys/bus/w1/devices/"
I2C_ADDR = 0x27
I2C_BUS = 1
API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"
GIO21_PIN = 21  # GPIO-Pin für GIO21

# Config einlesen
config = configparser.ConfigParser()
config.read("config.ini")

# Werte aus der Konfigurationsdatei holen
AUSSCHALTPUNKT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
AUSSCHALTPUNKT_ERHOEHT = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"])
EINSCHALTPUNKT = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"])

# MIN_LAUFZEIT und MIN_PAUSE in Minuten aus der Config lesen
MIN_LAUFZEIT_MINUTEN = int(config["Heizungssteuerung"]["MIN_LAUFZEIT"])  # Minuten
MIN_PAUSE_MINUTEN = int(config["Heizungssteuerung"]["MIN_PAUSE"])         # Minuten

# Beide Werte in timedelta-Objekte umwandeln
MIN_LAUFZEIT = timedelta(minutes=MIN_LAUFZEIT_MINUTEN)
MIN_PAUSE = timedelta(minutes=MIN_PAUSE_MINUTEN)


# SolaxCloud-Daten aus der Konfiguration lesen
TOKEN_ID = config["SolaxCloud"]["TOKEN_ID"]
SN = config["SolaxCloud"]["SN"]

# Logging-Konfiguration
log_file = "heizungssteuerung.log"  # Name der Logdatei
log_level = logging.DEBUG  # Detaillierteste Protokollierungsstufe

logging.basicConfig(filename=log_file, level=log_level,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# Initialisierung
logging.info("Heizungssteuerung gestartet.")

lcd = CharLCD('PCF8574', I2C_ADDR, port=I2C_BUS, cols=20, rows=4)
GPIO.setmode(GPIO.BCM)
GPIO.setup(GIO21_PIN, GPIO.OUT)
GPIO.output(GIO21_PIN, GPIO.LOW)  # Stelle sicher, dass der Kompressor aus ist

# Globale Variablen
last_api_call = None
last_api_data = None
last_api_timestamp = None
kompressor_ein = False
start_time = None
last_runtime = timedelta()
current_runtime = timedelta()
total_runtime_today = timedelta()
last_day = datetime.now().date()
aktueller_ausschaltpunkt = AUSSCHALTPUNKT
last_shutdown_time = datetime.now()  # Initialisiere last_shutdown_time mit der aktuellen Zeit

# Globale Variablen für Logging
last_log_time = datetime.now() - timedelta(minutes=1)  # Simuliert, dass die letzte Log-Zeit vor einer Minute war
last_kompressor_status = None

test_counter = 1  # Zähler für die Testeinträge

def limit_temperature(temp):
    """Begrenzt die Temperatur auf maximal 70 Grad."""
    return min(temp, 70)

# CSV-Datei initialisieren
csv_file = "heizungsdaten.csv"
fieldnames = ['Zeitstempel', 'T-Vorne', 'T-Hinten', 'T-Boiler', 'T-Verd',
              'Kompressorstatus', 'Soll-Temperatur', 'Ist-Temperatur',
              'Aktuelle Laufzeit', 'Letzte Laufzeit', 'Gesamtlaufzeit',
              'Solarleistung', 'Netzbezug/Einspeisung', 'Hausverbrauch',
              'Batterieleistung', 'SOC']

with open(csv_file, 'a', newline='') as csvfile:
    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
    # Header nur schreiben, wenn die Datei neu erstellt wird
    if os.stat(csv_file).st_size == 0:
        writer.writeheader()

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

def is_night_time(config):
    now = datetime.now()
    print(config.sections())  # Zeigt alle vorhandenen Abschnitte an
    print(config["Heizungssteuerung"])  # Zeigt alle Keys in Heizungssteuerung
    start_time_str = config["Heizungssteuerung"]["NACHTABSENKUNG_START"]  # Aus config.ini lesen
    end_time_str = config["Heizungssteuerung"]["NACHTABSENKUNG_END"]    # Aus config.ini lesen

    start_hour, start_minute = map(int, start_time_str.split(':'))
    end_hour, end_minute = map(int, end_time_str.split(':'))

    start_time = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end_time = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)

    if start_time > end_time:  # Fall, dass die Nacht über Mitternacht geht
        end_time = end_time.replace(day=now.day + 1)

    return start_time <= now <= end_time

def calculate_ausschaltpunkt(config, is_night, solax_data):
    nachtabsenkung = int(config["Heizungssteuerung"]["NACHTABSENKUNG"]) if is_night else 0
    if solax_data and (solax_data.get("batPower", 0) > 600 or (solax_data.get("soc", 0) > 95 and solax_data.get("feedinpower", 0) > 600)):
        return int(config["Heizungssteuerung"]["AUSSCHALTPUNKT_ERHOEHT"]) - nachtabsenkung
    else:
        return int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"]) - nachtabsenkung

def adjust_ausschaltpunkt(solax_data, config):
    global aktueller_ausschaltpunkt, aktueller_einschaltpunkt
    is_night = is_night_time(config)
    aktueller_ausschaltpunkt = calculate_ausschaltpunkt(config, is_night, solax_data)

    if solax_data and (solax_data.get("batPower", 0) > 600 or (solax_data.get("soc", 0) > 95 and solax_data.get("feedinpower", 0) > 600)):
        aktueller_einschaltpunkt = aktueller_ausschaltpunkt # Wenn Solarüberschuss, dann gleich dem Ausschalpunkt
    else:
        aktueller_einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"]) - (int(config["Heizungssteuerung"]["NACHTABSENKUNG"]) if is_night else 0) # Ansonsten normaler Einschaltpunkt mit Nachtabsenkung

    logging.info(f"Ausschaltpunkt: {aktueller_ausschaltpunkt}, Einschaltpunkt: {aktueller_einschaltpunkt}")


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
    ausschaltpunkt = int(config["Heizungssteuerung"]["AUSSCHALTPUNKT"])
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
    global kompressor_ein, start_time, current_runtime, total_runtime_today, last_day, last_runtime, last_shutdown_time

    now = datetime.now()
    logging.debug(f"set_kompressor_status aufgerufen: ein={ein}, force_off={force_off}")

    if ein:
        if not kompressor_ein:
            # Berechne die vergangene Pausenzeit seit dem letzten Ausschalten
            pause_time = now - last_shutdown_time

            # Überprüfe, ob die Mindestpause eingehalten wurde
            if pause_time < MIN_PAUSE:
                logging.info(f"Kompressor bleibt aus (zu kurze Pause: {pause_time}, benötigt: {MIN_PAUSE}).")
                return  # Wichtig: Hier aufhören, wenn die Pause zu kurz ist

            # Kompressor einschalten
            kompressor_ein = True
            start_time = now
            current_runtime = timedelta()
            logging.info("Kompressor EIN. Startzeit gesetzt.") # Korrekte Stelle für die
        else:
            # Kompressor läuft bereits
            elapsed_time = now - start_time
            current_runtime = elapsed_time
            logging.info(f"Kompressor läuft ({current_runtime}).")
    else:  # Ausschalten
        if kompressor_ein:
            elapsed_time = now - start_time
            if elapsed_time < MIN_LAUFZEIT and not force_off:
                logging.info(f"Kompressor bleibt an (zu kurze Laufzeit: {elapsed_time}, benötigt: {MIN_LAUFZEIT}).")
                return  # Hier aufhören, wenn die Laufzeit zu kurz ist

            # Kompressor tatsächlich ausschalten
            kompressor_ein = False
            current_runtime = elapsed_time
            total_runtime_today += current_runtime
            last_runtime = current_runtime
            last_shutdown_time = now
            logging.info(f"Kompressor AUS. Laufzeit: {elapsed_time}, Gesamtlaufzeit heute: {total_runtime_today}")  # Korrekte Stelle für die Logmeldung

            start_time = None

        else:
            logging.info("Kompressor ist bereits aus.")

    GPIO.output(GIO21_PIN, GPIO.HIGH if ein else GPIO.LOW)

def get_solax_data():
    global last_api_call, last_api_data, last_api_timestamp
    try:
        if last_api_call and (datetime.now() - last_api_call) < timedelta(minutes=5):
            logging.debug("Verwende zwischengespeicherte API-Daten.")
            return last_api_data

        params = {
            "tokenId": TOKEN_ID,
            "sn": SN
        }
        response = requests.get(API_URL, params=params)
        if response.status_code == 200:
            data = response.json()
            logging.debug(f"API-Antwort: {data}")
            if data.get("success", False):
                last_api_data = data.get("result", None)
                last_api_timestamp = datetime.now()
                last_api_call = last_api_timestamp
                return last_api_data
            else:
                error_message = data.get("exception", "Unbekannter Fehler")
                logging.error(f"API-Fehler: {error_message}")
                if "exceed the maximum call threshold limit" in error_message or "Request calls within the current minute > threshold" in error_message:
                    last_api_data = {
                        "acpower": 0,
                        "feedinpower": 0,
                        "consumeenergy": 0,
                        "batPower": 0,
                        "soc": 0,
                        "powerdc1": 0,
                        "powerdc2": 0,
                        "api_fehler": True
                    }
                    last_api_timestamp = datetime.now()
                    last_api_call = last_api_timestamp
                    return last_api_data
                return None
        else:
            logging.error(f"Fehler bei der API-Anfrage: {response.status_code}")
            return None
    except Exception as e:
        logging.error(f"Fehler beim Abrufen der API-Daten: {e}")
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

        adjust_ausschaltpunkt(solax_data, config)

        temperatures = ["Fehler"] * 3
        for i, sensor_id in enumerate(sensor_ids):
            temp = read_temperature(sensor_id)
            if temp is not None:
                temperatures[i] = limit_temperature(temp)
            logging.debug(f"Sensor {i + 1}: {temperatures[i]:.2f} °C")

        if temperatures[0] != "Fehler" and temperatures[1] != "Fehler":
            t_boiler = (temperatures[0] + temperatures[1]) / 2
        else:
            t_boiler = "Fehler"

        t_verd = temperatures[2] if temperatures[2] != "Fehler" else None
        logging.debug(f"T-Verd: {t_verd:.2f} °C")

        # Fehlerprüfung und Kompressorsteuerung
        fehler, is_overtemp = check_boiler_sensors(temperatures[0], temperatures[1], config)
        if fehler:
            lcd.clear()
            lcd.write_string(f"FEHLER: {fehler}")
            time.sleep(5)
            set_kompressor_status(False, force_off=True)
            continue

        # Kompressorsteuerung basierend auf Temperaturen
        einschaltpunkt = int(config["Heizungssteuerung"]["EINSCHALTPUNKT"])
        if t_verd is not None and t_verd < 25:
            if kompressor_ein:
                set_kompressor_status(False)
                logging.info("T-Verd unter 25 Grad. Kompressor wurde ausgeschaltet.")
            logging.info("T-Verd unter 25 Grad. Kompressor bleibt ausgeschaltet.")
        elif t_boiler != "Fehler":
            logging.debug(
                f"T-Boiler: {t_boiler:.2f}, EINSCHALTPUNKT: {EINSCHALTPUNKT}, aktueller_ausschaltpunkt: {aktueller_ausschaltpunkt}")
            if t_boiler < EINSCHALTPUNKT and not kompressor_ein:
                set_kompressor_status(True)
                logging.info(f"T-Boiler Temperatur unter {EINSCHALTPUNKT} Grad. Kompressor eingeschaltet.")
            elif t_boiler >= aktueller_ausschaltpunkt and kompressor_ein:
                # Hier wird der Kompressor ausgeschaltet, wenn die Temperatur den Ausschaltpunkt erreicht
                set_kompressor_status(False)
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

            # Daten in CSV-Datei schreiben
            try:
                with open(csv_file, 'a', newline='') as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writerow({'Zeitstempel': now_str, 'T-Vorne': t_vorne,
                                     'T-Hinten': t_hinten, 'T-Boiler': t_boiler_wert,
                                     'T-Verd': t_verd, 'Kompressorstatus': kompressor_status,
                                     'Soll-Temperatur': soll_temperatur, 'Ist-Temperatur': ist_temperatur,
                                     'Aktuelle Laufzeit': aktuelle_laufzeit,
                                     'Letzte Laufzeit': letzte_laufzeit,
                                     'Gesamtlaufzeit': gesamtlaufzeit,
                                     'Solarleistung': solar, 'Netzbezug/Einspeisung': netz,
                                     'Hausverbrauch': verbrauch, 'Batterieleistung': batterie,
                                     'SOC': soc})
                logging.info(f"Daten in CSV-Datei geschrieben: {now_str}")
            except Exception as e:
                logging.error(f"Fehler beim Schreiben in die CSV-Datei: {e}")

            # Logging-Zeit und Kompressorstatus aktualisieren
            last_log_time = now
            last_kompressor_status = kompressor_ein

        time.sleep(10)  # Kurze Pause, um die CPU-Last zu reduzieren

except KeyboardInterrupt:
    logging.info("Programm beendet.")
finally:
    GPIO.cleanup()
    lcd.close()
    logging.info("Heizungssteuerung beendet.")