import asyncio

import logging
import threading
import signal
import uvicorn
import aiofiles
import os
from datetime import datetime, timedelta

# Modules
from config_manager import ConfigManager
from state import State
from sensors import SensorManager
from hardware import HardwareManager
from hardware_mock import MockHardwareManager
from logging_config import setup_logging
from solax import get_solax_data
import control_logic
import priority_control_logic as pcl
from telegram_handler import telegram_task
from telegram_ui import send_welcome_message, escape_markdown
from telegram_api import start_healthcheck_task, create_robust_aiohttp_session
from telegram_charts import get_boiler_temperature_history, get_runtime_bar_chart
from vpn_manager import check_vpn_status
from api import app, init_api
from utils import safe_timedelta, HEIZUNGSDATEN_CSV, EXPECTED_CSV_HEADER, check_and_fix_csv_header, rotiere_csv_monatlich
from learning_engine import LearningEngine
from weather_forecast import get_solar_forecast
from logic_utils import (
    check_log_throttle,
    evaluate_sommer_modus,
    SOMMER_AKTIVIERT, SOMMER_DEAKTIVIERT_PROGNOSE, SOMMER_DEAKTIVIERT_DATEN,
)
from constants import VPN_CHECK_INTERVAL_SEC, FORECAST_UPDATE_INTERVAL_HOURS, MAIN_LOOP_INTERVAL_SEC, COMPRESSOR_VERIFICATION_ERROR_THRESHOLD, SOLAR_DATA_STALE_THRESHOLD_MIN

# Global objects
config_manager = ConfigManager()
state = None
sensor_manager = None
hardware_manager = None
stop_event = threading.Event()

# Referenzen auf Hintergrund-Tasks halten (ohne Referenz können sie vom
# Garbage Collector eingesammelt werden, während sie noch laufen!)
background_tasks = []

def _log_task_exception(task):
    """Loggt unerwartete Fehler aus Hintergrund-Tasks (verhindert stillen Absturz)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logging.error(
            f"Hintergrund-Task '{task.get_name()}' wurde mit Fehler beendet: {exc}",
            exc_info=exc,
        )

def handle_exit(signum, frame):
    # Wichtig: Hier bewusst KEIN sys.exit()!
    # SystemExit würde den finally-Block in main_loop überspringen
    # (GPIO-Cleanup und session.close() würden nie laufen).
    # Wir setzen nur das Stop-Event; der Loop beendet sich kontrolliert selbst.
    logging.info(f"Signal {signum} empfangen. Beende Programm...")
    stop_event.set()

async def set_kompressor_status(state, status, force=False, t_boiler_oben=None):
    """
    Schaltet den Kompressor und aktualisiert den State sowie Statistiken.
    """
    now = datetime.now(state.local_tz)
    was_ein = state.control.kompressor_ein

    if status:
        # Einschalten
        if was_ein and not force:
            return True
        
        hardware_manager.set_compressor_state(True)
        state.control.kompressor_ein = True
        
        # Statistiken aktualisieren
        state.stats.last_compressor_on_time = now
        
        # Startwerte für Verifizierung speichern
        state.kompressor_verification_start_time = now
        state.kompressor_verification_start_t_verd = state.sensors.t_verd
        state.kompressor_verification_start_t_unten = state.sensors.t_unten
        state.kompressor_verification_last_check = None
        logging.info(f"Kompressor EIN - Verifizierung gestartet (t_verd={state.sensors.t_verd}, t_unten={state.sensors.t_unten})")
        
        return True
    else:
        # Ausschalten
        if not was_ein and not force:
            return True

        hardware_manager.set_compressor_state(False)
        state.control.kompressor_ein = False
        
        # Statistiken aktualisieren
        state.stats.last_compressor_off_time = now
        if was_ein and state.stats.last_compressor_on_time:
            elapsed = safe_timedelta(now, state.stats.last_compressor_on_time, state.local_tz)
            state.stats.total_runtime_today += elapsed
            state.stats.last_completed_cycle = now
            logging.info(f"Kompressor AUS. Laufzeit: {elapsed}")
        else:
            logging.info("Kompressor AUS")
            
        return True

async def handle_pressure_check(session, state):
    """Liest den Druckschalter ueber den HardwareManager.

    Reine Lese-Funktion: Die Erkennung von Zustandsaenderungen inkl. Logging
    und Setzen von ausschluss_grund passiert in pcl.check_pressure_and_config --
    dort wird auch state.control.last_pressure_state gepflegt."""
    return hardware_manager.read_pressure_sensor()

def run_api():
    """Startet den FastAPI-Server."""
    try:
        # Host/Port aus Config
        host = state.config.Heizungssteuerung.API_HOST
        port = state.config.Heizungssteuerung.API_PORT
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except Exception as e:
        logging.error(f"Fehler beim Starten der API: {e}")

async def setup_application():
    """Initialisiert Konfiguration, Hardware, Sensoren und API."""
    global state, sensor_manager, hardware_manager
    
    # 1. Config laden
    config_manager.load_config()
    
    # 2. State init
    state = State(config_manager)
    learning_engine = LearningEngine()
    state.learning_engine = learning_engine
    
    # 3. Logging setup
    setup_logging(enable_full_log=True, telegram_config=state.config.Telegram)
    logging.info("Starten der Wärmepumpensteuerung (Refactored)...")

    # 4. Hardware & Sensors init
    try:
        import RPi.GPIO  # noqa: F401 - Pi-Erkennung
        hardware_manager = HardwareManager()
        logging.info("Using real hardware (Raspberry Pi detected)")
    except ImportError:
        hardware_manager = MockHardwareManager()
        logging.info("Using mock hardware (non-Raspberry Pi platform)")
    
    hardware_manager.init_gpio()
    await hardware_manager.init_lcd()
    
    sensor_manager = SensorManager()
    
    # 5. API init
    control_funcs = {"set_kompressor": set_kompressor_status}
    init_api(state, control_funcs)
    
    # Start API Thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # 6. Session & Tasks
    session = create_robust_aiohttp_session()
    state.session = session
    
    
    # 7. CSV: Monatsrotation pruefen (holt ggf. den Rueckstand nach Ausfall),
    # dann Header-Check
    try:
        archiv = rotiere_csv_monatlich()
        if archiv:
            logging.info(f"CSV-Monatsrotation beim Start: {archiv}")
    except Exception as e:
        logging.error(f"CSV-Rotation beim Start fehlgeschlagen: {e}")

    # 7b. CSV Header Check (einmalig beim Start)
    try:
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
            # Neue Datei beim Start direkt anlegen
            async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
            logging.info(f"Created new CSV file: {csv_file}")
        else:
            if check_and_fix_csv_header(csv_file):
                logging.warning("CSV Header was redundant/fixed at startup.")
            else:
                logging.info("CSV Header check passed.")
    except Exception as e:
        logging.error(f"Startup CSV check failed: {e}")

    # 8. Start Telegram Task
    tg_task = asyncio.create_task(telegram_task(
        read_temperature_func=sensor_manager.read_temperature,
        sensor_ids=sensor_manager.sensor_ids,
        kompressor_status_func=lambda: state.control.kompressor_ein,
        current_runtime_func=lambda: state.stats.current_runtime,
        total_runtime_func=lambda: state.stats.total_runtime_today + state.stats.current_runtime,
        config=state.config,
        get_solax_data_func=get_solax_data,
        state=state,
        get_temperature_history_func=get_boiler_temperature_history,
        get_runtime_bar_chart_func=get_runtime_bar_chart,
        is_nighttime_func=lambda config: pcl._is_nachtsperre_aktiv(state.priority_config, datetime.now(state.local_tz)),
        is_solar_window_func=control_logic.is_solar_window
    ))

    # Start Healthcheck Task
    hc_task = asyncio.create_task(start_healthcheck_task(session, state))

    # Referenzen halten und Crash-Fruehwarnung aktivieren
    for task in (tg_task, hc_task):
        task.add_done_callback(_log_task_exception)
        background_tasks.append(task)
    
    return session

def handle_day_transition(state, now):
    """Führt Aktionen beim Tageswechsel durch."""
    current_date = now.date()
    if state.stats.last_day is None:
        state.stats.last_day = current_date
    elif state.stats.last_day != current_date:
        logging.info(f"Tageswechsel erkannt ({state.stats.last_day} -> {current_date}). Setze Statistiken zurück.")
        
        # Falls der Kompressor über Mitternacht läuft: Restzeit des alten Tages dazurechnen
        if state.control.kompressor_ein and state.stats.last_compressor_on_time:
            # Ende des alten Tages (23:59:59.999...)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elapsed_old_day = safe_timedelta(midnight, state.stats.last_compressor_on_time, state.local_tz)
            if elapsed_old_day.total_seconds() > 0:
                state.stats.total_runtime_today += elapsed_old_day
                logging.info(f"Laufzeitanteil alter Tag: {elapsed_old_day}")
            
            # Startzeit für neuen Tag auf Mitternacht setzen
            state.stats.last_compressor_on_time = midnight

        # Hier könnte man die total_runtime_today in eine DB oder Datei wegschreiben
        state.stats.total_runtime_today = timedelta()

        # CSV-Monatsrotation pruefen (billig, einmal pro Tag)
        try:
            archiv = rotiere_csv_monatlich(heute=now)
            if archiv:
                logging.info(f"CSV-Monatsrotation am Tageswechsel: {archiv}")
        except Exception as e:
            logging.error(f"CSV-Rotation am Tageswechsel fehlgeschlagen: {e}")
        state.stats.last_completed_cycle = None
        state.stats.last_day = current_date

async def update_system_data(session, state):
    """Liest Sensoren und PV-Daten."""
    # 1. Sensoren lesen
    temps = await sensor_manager.get_all_temperatures()
    state.sensors.t_oben = temps.get("oben")
    state.sensors.t_mittig = temps.get("mittig")
    state.sensors.t_unten = temps.get("unten")
    state.sensors.t_verd = temps.get("verd")
    state.sensors.t_boiler = temps.get("oben")
    
    # 2. PV-Daten aktualisieren
    solax_result = await get_solax_data(session, state)
    if state.solar.last_api_data:
        state.solar.feedinpower = state.solar.last_api_data.get("feedinpower", 0)
        state.solar.batpower = state.solar.last_api_data.get("batPower", 0)
        state.solar.soc = state.solar.last_api_data.get("soc", 0)
    elif solax_result is None:
        # API fehlgeschlagen: Prüfe wie alt die letzten Daten sind
        if state.solar.last_api_call:
            now = datetime.now(state.local_tz)
            age_min = (now - state.solar.last_api_call).total_seconds() / 60
            if age_min > SOLAR_DATA_STALE_THRESHOLD_MIN:
                logging.warning(f"Solar-Daten veraltet ({age_min:.0f} min), setze auf 0")
                state.solar.feedinpower = 0
                state.solar.batpower = 0
                state.solar.soc = 0

async def check_periodic_tasks(session, state, last_vpn_check):
    """Führt zeitgesteuerte Hintergrundaufgaben aus."""
    now_dt = datetime.now()
    now_local = datetime.now(state.local_tz)
    
    # 1. VPN Check
    if (now_dt - last_vpn_check).total_seconds() >= VPN_CHECK_INTERVAL_SEC:
        await check_vpn_status(state)
        last_vpn_check = now_dt
    
    # 2. Solar Forecast (alle FORECAST_UPDATE_INTERVAL_HOURS)
    if state.last_forecast_update is None or (now_local - state.last_forecast_update).total_seconds() >= FORECAST_UPDATE_INTERVAL_HOURS * 3600:
        rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tomorrow, ss_tomorrow = await get_solar_forecast(session, state.config)
        if rad_today is not None:
            state.solar.forecast_today = rad_today
            state.solar.forecast_tomorrow = rad_tomorrow
            state.solar.forecast_day2 = rad_day2
            state.solar.sunrise_today = sr_today
            state.solar.sunset_today = ss_today
            state.sunrise_tomorrow = sr_tomorrow
            state.sunset_tomorrow = ss_tomorrow
            state.last_forecast_update = now_local

            # --- Sommer-Modus: max. EINE Bewertung pro Kalendertag ---
            # Aktivierung erst nach 'benoetigte_tage' AUFENANDERFOLGENDEN Kalendertagen
            # mit durchgehend guter Prognose (heute+morgen+uebermorgen >= Schwelle).
            # Idee: Kommt sicher PV-Ueberschuss, ist Vorheizen/Buffern unnoetig ->
            # die Solltemperatur wird dann um temperatur_offset_c gesenkt (s. pcl).
            sommer_cfg = state.priority_config.sommer_modus
            if sommer_cfg.aktiv:
                neuer_zaehler, ist_aktiv, bewertungstag, ereignis = evaluate_sommer_modus(
                    benoetigte_tage=sommer_cfg.benoetigte_tage,
                    mindest_prognose_wh=sommer_cfg.mindest_prognose_wh,
                    rad_today=rad_today,
                    rad_tomorrow=rad_tomorrow,
                    rad_day2=rad_day2,
                    heute=now_local.date(),
                    aktueller_zaehler=getattr(state, 'sommer_modus_zaehler', 0),
                    ist_aktiv=getattr(state, 'sommer_modus_aktiv', False),
                    letzter_bewertungstag=getattr(state, 'sommer_letzter_bewertungstag', None),
                )
                state.sommer_modus_zaehler = neuer_zaehler
                state.sommer_modus_aktiv = ist_aktiv
                state.sommer_letzter_bewertungstag = bewertungstag

                if ereignis == SOMMER_AKTIVIERT:
                    logging.info(
                        f"Sommer-Modus AKTIV nach {neuer_zaehler} guten Prognosetag(en): "
                        f"Solltemperatur wird um {abs(sommer_cfg.temperatur_offset_c):.1f}C gesenkt "
                        f"(Offset {sommer_cfg.temperatur_offset_c:+.1f}C, "
                        f"Schwelle >={sommer_cfg.mindest_prognose_wh:.0f} Wh/qm)"
                    )
                elif ereignis == SOMMER_DEAKTIVIERT_PROGNOSE:
                    logging.info("Sommer-Modus INAKTIV: PV-Prognose nicht mehr durchgehend gut")
                elif ereignis == SOMMER_DEAKTIVIERT_DATEN:
                    logging.info("Sommer-Modus INAKTIV: Keine vollstaendigen Prognosedaten verfuegbar")

    return last_vpn_check

async def check_and_send_alerts(session, state):
    """Prüft auf Änderungen im blocking_reason und sendet sofortige Telegram-Alarme (einmalig)."""
    current_blocking = state.control.blocking_reason
    
    # Normalisierung: Dynamische Teile (Zeiten, Temperaturen) entfernen
    # Beispiel: "Min. Pause (noch 1m 10s)" -> "Min. Pause"
    # Beispiel: "Verdampfer zu kalt (5.0°C < 6°C)" -> "Verdampfer zu kalt"
    # Beispiel: "Sensorfehler: T_Oben invalid" -> "Sensorfehler"
    import re
    def normalize(text):
        if not text:
            return ""
        # 1. Alles in Klammern entfernen (Zeiten, Werte)
        res = re.sub(r'\(.*?\)', '', text)
        # 2. Alles nach Doppelpunkt entfernen (Details)
        res = res.split(':')[0]
        return res.strip()

    current_type = normalize(current_blocking)
    last_type = getattr(state.control, 'last_alert_type', "")
    
    if current_type != last_type:
        if current_type:
            # Filtere bekannte Infos, die keine Alarme sein sollen
            is_solar = "Solarfenster" in current_type
            is_zieltemp = "Zieltemp" in current_type
            
            if not is_solar and not is_zieltemp:
                emoji = "⚠️"
                if any(x in current_type for x in ["Fehler", "Sicherheit", "🚨"]):
                    emoji = "🚨"
                elif any(x in current_type for x in ["Pause", "Mindestlaufzeit"]):
                    emoji = "⏳"
                
                # Wir schicken die VOLLE Nachricht (inkl. Details/Zeit) beim ersten Mal
                msg = f"{emoji} *Kompressor blockiert:* {escape_markdown(current_blocking)}"
                logging.info(f"Sende Einmal-Alarm: {current_type} (Voll: {current_blocking})")
                await control_logic.send_telegram_message(
                    session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN, parse_mode="Markdown"
                )
        
        state.control.last_alert_type = current_type
    
    # Der technische Statuswechsel wird weiterhin für andere Zwecke geloggt/gespeichert
    state.control.last_blocking_reason = current_blocking

async def run_logic_step(session, state, learning_engine=None):
    """Fuehrt einen Schritt der Steuerungslogik aus (Pareto-Prioritaeten)."""
    # 1. Druckschalter & Config
    if not await pcl.check_pressure_and_config(
            session, state, handle_pressure_check, set_kompressor_status
        ):
        return  # Druckfehler: Restliche Logik ueberspringen

    # 2. Kompressor-Verifizierung
    if state.control.kompressor_ein:
        is_running, error_msg = await control_logic.verify_compressor_running(state, session, state.sensors.t_verd, state.sensors.t_unten)
        if not is_running and state.kompressor_verification_error_count >= COMPRESSOR_VERIFICATION_ERROR_THRESHOLD:
            logging.error(f"Kompressor-Verifizierung fehlgeschlagen (2x): {error_msg} - Schalte aus!")
            await set_kompressor_status(state, False, force=True)
            state.control.ausschluss_grund = "Kompressor laeuft nicht (Verifizierung fehlgeschlagen)"
            state.stats.last_compressor_off_time = datetime.now(state.local_tz) + timedelta(minutes=10)

    # 3. Sensoren & Safety (Sicherheits-Check)
    if await pcl.check_safety_limits(session, state, state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd, set_kompressor_status):
        # 3b. Sensoren-Check (behalten wir vom alten System)
        if await control_logic.check_sensors_and_safety(session, state, state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd, set_kompressor_status):
            # 4. Prioritaeten-Engine: Regel bewerten
            result = await pcl.determine_mode_and_setpoints(state, state.sensors.t_unten, state.sensors.t_mittig, learning_engine=learning_engine)
            
            # 5. Schaltentscheidung
            should_on = result.get("soll_einschalten", False)
            state.control._soll_einschalten = should_on
            
            regelfuehler = result["regelfuehler"]
            ausschaltpunkt = state.control.aktueller_ausschaltpunkt
            einschaltpunkt = state.control.aktueller_einschaltpunkt

            if state.control.kompressor_ein:
                # Kompressor laeuft: Ausschalten pruefen
                await pcl.handle_compressor_off(
                    state, session, regelfuehler, ausschaltpunkt,
                    state.min_laufzeit, state.sensors.t_oben, set_kompressor_status
                )
            else:
                # Kompressor aus: Einschalten pruefen
                await pcl.handle_compressor_on(
                                    state, session, regelfuehler, einschaltpunkt, ausschaltpunkt,
                                    state.min_laufzeit, state.min_pause, state.sensors.t_oben,
                                    set_kompressor_status
                                )
            
            # 6. Sofort-Alarme pruefen
            await check_and_send_alerts(session, state)

def build_heizungsdaten_zeile(state):
    """Baut die CSV-Datenzeile fuer heizungsdaten.csv (19 Spalten).

    Muss mit utils.EXPECTED_CSV_HEADER uebereinstimmen -- abgesichert
    durch tests/test_csv_konsistenz.py."""
    def fmt_csv(val):
        return str(val) if val is not None else "N/A"

    solax = state.solar.last_api_data or {}

    # Power Source
    power_source = "Netz"
    if state.solar.feedinpower and state.solar.feedinpower > 0:
        power_source = "Solar"
    elif state.solar.batpower and state.solar.batpower > 0:
        power_source = "Batterie"

    return [
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        fmt_csv(state.sensors.t_oben), fmt_csv(state.sensors.t_unten), fmt_csv(state.sensors.t_mittig),
        fmt_csv(state.sensors.t_boiler), fmt_csv(state.sensors.t_verd),
        "1" if state.control.kompressor_ein else "0",
        fmt_csv(solax.get("acpower", 0)), fmt_csv(state.solar.feedinpower),
        fmt_csv(state.solar.batpower), fmt_csv(state.solar.soc),
        fmt_csv(solax.get("powerdc1", 0)), fmt_csv(solax.get("powerdc2", 0)),
        fmt_csv(solax.get("consumeenergy", 0)),
        fmt_csv(state.control.aktueller_einschaltpunkt), fmt_csv(state.control.aktueller_ausschaltpunkt),
        "1" if state.control.solar_ueberschuss_aktiv else "0",
        "1" if getattr(state, "urlaubsmodus_aktiv", False) else "0",
        power_source, fmt_csv(state.solar.forecast_tomorrow)
    ]


async def log_system_state(state):
    """Schreibt CSV-Log, aktualisiert LCD und loggt Temperaturen + Entscheidungen."""
    # 1. Temperatur- und Entscheidungs-Logging (gethrottelt alle 5 Min)
    if check_log_throttle(state, '_last_temp_log', interval_minutes=5.0):
        logging.info(
            f"Sensoren: Oben={state.sensors.t_oben or 0:.1f}°C | Mittig={state.sensors.t_mittig or 0:.1f}°C | "
            f"Unten={state.sensors.t_unten or 0:.1f}°C | Verd={state.sensors.t_verd or 0:.1f}°C"
        )
        komp_status = "EIN" if state.control.kompressor_ein else "AUS"
        log_line = (
            f"Status: {komp_status} | "
            f"EP={state.control.aktueller_einschaltpunkt:.1f}°C | "
            f"AP={state.control.aktueller_ausschaltpunkt:.1f}°C"
        )
        if state.control.blocking_reason:
            log_line += f" | Blocking: {state.control.blocking_reason}"
        if state.control.active_rule_name:
            log_line += f" | Regel: {state.control.active_rule_name}"
        if state.control.previous_modus:
            log_line += f" | Modus: {state.control.previous_modus}"
        logging.info(log_line)

    # 3. LCD Update
    pv_w = state.solar.feedinpower if state.solar.feedinpower else 0
    rule_name = getattr(state.control, 'active_rule_name', '') or ''
    if rule_name and len(rule_name) > 12:
        rule_name = rule_name[:12]
    hardware_manager.write_lcd(
        f"E:{state.sensors.t_oben if state.sensors.t_oben else 0:.1f} U:{state.sensors.t_unten if state.sensors.t_unten else 0:.1f}",
        f"M:{state.sensors.t_mittig if state.sensors.t_mittig else 0:.1f} V:{state.sensors.t_verd if state.sensors.t_verd else 0:.0f}",
        f"{'ON' if state.control.kompressor_ein else 'OFF'} PV:{pv_w:.0f}W {rule_name[:7]}",
        f"{state.solar.soc if state.solar.soc else 0}% {state.control.previous_modus[:7] if state.control.previous_modus else ''}"
    )

    # 4. CSV Logging
    try:
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
            async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        # Optimization: Header check removed from loop (done at startup)

        csv_line = build_heizungsdaten_zeile(state)
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            await f.write(",".join(csv_line) + "\n")
    except Exception as e:
        logging.error(f"Fehler beim Schreiben der CSV: {e}")

async def main_loop():
    session = await setup_application()
    
    # Send Startup Message
    if state.bot_token and state.chat_id:
        try:
            await send_welcome_message(session, state.chat_id, state.bot_token, state)
            logging.info("Startup message sent.")
        except Exception as e:
            logging.error(f"Failed to send startup message: {e}")

    last_vpn_check = datetime.now() - timedelta(minutes=1)
    
    try:
        while not stop_event.is_set():
            now = datetime.now(state.local_tz)

            try:
                # Tageswechsel und Laufzeit
                handle_day_transition(state, now)
                if state.control.kompressor_ein and state.stats.last_compressor_on_time:
                    state.stats.current_runtime = safe_timedelta(now, state.stats.last_compressor_on_time, state.local_tz)
                else:
                    state.stats.current_runtime = timedelta()

                # Daten-Update & Periodische Tasks
                await update_system_data(session, state)
                last_vpn_check = await check_periodic_tasks(session, state, last_vpn_check)

                # Logik & Logging
                await run_logic_step(session, state, learning_engine=state.learning_engine)
                await log_system_state(state)
            except Exception:
                # Transienter Fehler (Sensor, API, ...) darf die Regelung nicht komplett beenden
                logging.exception("Fehler im Loop-Durchlauf - fahre mit naechstem Zyklus fort")

            # In kurzen Abschnitten schlafen, damit ein Stop-Signal zuegig reagiert
            remaining = MAIN_LOOP_INTERVAL_SEC
            while remaining > 0 and not stop_event.is_set():
                step = min(remaining, 0.5)
                await asyncio.sleep(step)
                remaining -= step

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.critical(f"Unbehandelter Fehler in Main Loop: {e}", exc_info=True)
    finally:
        logging.info("Shutting down...")
        # Hintergrund-Tasks kontrolliert beenden, BEVOR die Session geschlossen wird
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        if hardware_manager:
            hardware_manager.cleanup()
        await session.close()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
