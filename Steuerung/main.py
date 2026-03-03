import asyncio
import logging
import threading
import signal
import sys
import uvicorn
import aiohttp
import aiofiles
import os
from datetime import datetime, timedelta
import pytz
import re

# Modules
from config_manager import ConfigManager
from state import State
from sensors import SensorManager
from hardware import HardwareManager
from hardware_mock import MockHardwareManager
from logging_config import setup_logging
from solax import get_solax_data
import control_logic
from telegram_handler import telegram_task
from telegram_ui import send_welcome_message
from telegram_api import start_healthcheck_task, send_telegram_message, create_robust_aiohttp_session
from telegram_charts import get_boiler_temperature_history, get_runtime_bar_chart
from vpn_manager import check_vpn_status
from api import app
from utils import safe_timedelta, HEIZUNGSDATEN_CSV, EXPECTED_CSV_HEADER
from weather_forecast import get_solar_forecast
from logic_utils import is_nighttime, is_solar_window

# Global objects
config_manager = ConfigManager()
state = None
stop_event = threading.Event()

def handle_exit(signum, frame):
    logging.info(f"Signal {signum} empfangen. Beende Programm...")
    stop_event.set()
    sys.exit(0)

async def handle_pressure_check(session, state):
    """Liest den Druckschalter über HardwareManager."""
    if not state.hardware_manager: return True
    return state.hardware_manager.read_pressure_sensor()

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
    global state
    
    # 1. Config laden
    config_manager = ConfigManager()
    config_manager.load_config()
    config = config_manager.get()
    
    # 2. State init
    state = State(config_manager)
    
    # 3. Logging setup
    setup_logging(enable_full_log=True, telegram_config=state.config.Telegram)
    logging.info("Starten der Wärmepumpensteuerung (Refactored)...")

    # 4. Hardware & Sensors init
    try:
        import RPi.GPIO
        hw_mgr = HardwareManager()
        logging.info("Using real hardware (Raspberry Pi detected)")
    except ImportError:
        hw_mgr = MockHardwareManager()
        logging.info("Using mock hardware (non-Raspberry Pi platform)")
    
    state.hardware_manager = hw_mgr
    hw_mgr.init_gpio()
    await hw_mgr.init_lcd()
    
    state.sensor_manager = SensorManager(config=config)
    
    # 5. API init
    app.state.shared_state = state
    app.state.control_funcs = {"set_kompressor": state.set_kompressor_status}
    
    # Start API Thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # 6. Session & Tasks
    session = create_robust_aiohttp_session()
    state.session = session
    
    
    # 7. CSV Header Check (Once at startup)
    try:
        from utils import check_and_fix_csv_header
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
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

    # Start Telegram Task
    asyncio.create_task(telegram_task(
        read_temperature_func=state.sensor_manager.read_temperature,
        sensor_ids=state.sensor_manager.sensor_ids,
        kompressor_status_func=lambda: state.control.kompressor_ein,
        current_runtime_func=lambda: state.stats.current_runtime,
        total_runtime_func=lambda: state.stats.total_runtime_today + state.stats.current_runtime,
        config=state.config,
        get_solax_data_func=get_solax_data,
        state=state,
        get_temperature_history_func=get_boiler_temperature_history,
        get_runtime_bar_chart_func=get_runtime_bar_chart,
        is_nighttime_func=control_logic.is_nighttime,
        is_solar_window_func=control_logic.is_solar_window
    ))
    
    # Start Healthcheck Task
    asyncio.create_task(start_healthcheck_task(session, state))
    
    return session

def handle_day_transition(state, now):
    """Führt Aktionen beim Tageswechsel durch."""
    current_date = now.date()
    if state.stats.last_day is None:
        state.stats.last_day = current_date
    elif state.stats.last_day != current_date:
        logging.info(f"Tageswechsel erkannt ({state.stats.last_day} -> {current_date}). Setze Statistiken zurück.")

        # Automatische Rollierung der Heizungslog-Datei (einmal pro Tag)
        try:
            from utils import rotate_csv
            rotate_csv(HEIZUNGSDATEN_CSV)
            logging.info("Heizungslog-Datei wurde rolliert (alte Einträge gesichert).")
        except Exception as e:
            logging.error(f"Fehler bei der Rollierung der Heizungslog-Datei: {e}")

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
        state.stats.last_completed_cycle = None
        state.stats.last_day = current_date

async def update_system_data(session, state):
    """Liest Sensoren und PV-Daten."""
    # 1. Sensoren lesen
    if not state.sensor_manager: return
    temps = await state.sensor_manager.get_all_temperatures()
    state.sensors.t_oben = temps.get("oben")
    state.sensors.t_mittig = temps.get("mittig")
    state.sensors.t_unten = temps.get("unten")
    state.sensors.t_verd = temps.get("verd")
    state.sensors.t_vorlauf = temps.get("vorlauf")
    
    # 1b. Kritischen Sensorfehler prüfen (z.B. 5x hintereinander fehlgeschlagen)
    if state.sensor_manager.critical_failure:
        sensor_name = state.sensor_manager.critical_failure_sensor or "unbekannt"
        fail_count = state.sensor_manager.consecutive_failures.get(sensor_name, 0)
        error_msg = (
            f"🚨 KRITISCHER SENSORFEHLER: Sensor '{sensor_name}' hat {fail_count}x "
            f"hintereinander versagt! Kompressor wird abgeschaltet, System startet neu."
        )
        logging.critical(error_msg)
        
        # Kompressor sicherheitshalber ausschalten
        if state.control.kompressor_ein:
            await state.set_kompressor_status(False, force=True)
            logging.critical("Kompressor wurde wegen Sensorfehler ausgeschaltet.")
        
        # Telegram-Alarm senden (best effort)
        if state.bot_token and state.chat_id:
            try:
                await send_telegram_message(session, state.chat_id, error_msg, state.bot_token)
            except Exception as e:
                logging.error(f"Telegram-Alarm konnte nicht gesendet werden: {e}")
        
        # Hardware aufräumen und Neustart durch systemd auslösen
        if state.hardware_manager:
            state.hardware_manager.cleanup()
        await session.close()
        logging.critical("System wird beendet (exit code 1) für systemd-Neustart.")
        sys.exit(1)
    
    # 2. PV-Daten aktualisieren
    await get_solax_data(session, state)
    if state.solar.last_api_data:
        state.solar.feedinpower = state.solar.last_api_data.get("feedinpower", 0)
        state.solar.batpower = state.solar.last_api_data.get("batPower", 0)
        state.solar.soc = state.solar.last_api_data.get("soc", 0)

async def check_periodic_tasks(session, state, last_vpn_check):
    """Führt zeitgesteuerte Hintergrundaufgaben aus."""
    now_dt = datetime.now()
    now_local = datetime.now(state.local_tz)
    
    # 1. VPN Check (alle 60s)
    if (now_dt - last_vpn_check).total_seconds() >= 60:
        await check_vpn_status(state)
        last_vpn_check = now_dt
    
    # 2. Solar Forecast (alle 6h)
    if state.last_forecast_update is None or (now_local - state.last_forecast_update).total_seconds() >= 6 * 3600:
        rad_today, rad_tomorrow, sr_today, ss_today, sr_tomorrow, ss_tomorrow = await get_solar_forecast(session, state.config)
        if rad_today is not None:
            state.solar.forecast_today = rad_today
            state.solar.forecast_tomorrow = rad_tomorrow
            state.solar.sunrise_today = sr_today
            state.solar.sunset_today = ss_today
            state.solar.sunrise_tomorrow = sr_tomorrow
            state.solar.sunset_tomorrow = ss_tomorrow
            state.last_forecast_update = now_local
            
    return last_vpn_check

async def check_and_send_alerts(session, state):
    """Prüft auf Änderungen im blocking_reason und sendet sofortige Telegram-Alarme (einmalig)."""
    current_blocking = state.control.blocking_reason
    
    # Normalisierung: Dynamische Teile (Zeiten, Temperaturen) entfernen
    def normalize(text):
        if not text: return ""
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
                msg = f"{emoji} *Kompressor blockiert:* {current_blocking}"
                logging.info(f"Sende Einmal-Alarm: {current_type} (Voll: {current_blocking})")
                await control_logic.send_telegram_message(
                    session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN, parse_mode="Markdown"
                )
        
        state.control.last_alert_type = current_type
    
    # Der technische Statuswechsel wird weiterhin für andere Zwecke geloggt/gespeichert
    state.control.last_blocking_reason = current_blocking

async def run_logic_step(session, state):
    """Führt einen Schritt der Steuerungslogik aus."""
    # 1. Druckschalter & Config
    if not await control_logic.check_pressure_and_config(
        session, state, handle_pressure_check, state.set_kompressor_status, state.update_config, lambda: "hash"
    ):
        return

    # 2. Kompressor-Verifizierung
    if state.control.kompressor_ein:
        is_running, error_msg = await control_logic.verify_compressor_running(state, session, state.sensors.t_vorlauf, state.sensors.t_unten, verification_delay_minutes=20)
        if not is_running and state.kompressor_verification_error_count >= 2:
            logging.error(f"Kompressor-Verifizierung fehlgeschlagen (2x): {error_msg} - Schalte aus!")
            await state.set_kompressor_status(False, force=True)
            state.control.ausschluss_grund = "Kompressor läuft nicht (Verifizierung fehlgeschlagen)"
            state.stats.last_compressor_off_time = datetime.now(state.local_tz) + timedelta(minutes=10)

    # 3. Sensoren & Safety
    if await control_logic.check_sensors_and_safety(session, state, state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd, state.set_kompressor_status):
        result = await control_logic.determine_mode_and_setpoints(state, state.sensors.t_unten, state.sensors.t_mittig)
        state.control.aktueller_einschaltpunkt = result["einschaltpunkt"]
        state.control.aktueller_ausschaltpunkt = result["ausschaltpunkt"]
        state.control.solar_ueberschuss_aktiv = result["solar_ueberschuss_aktiv"]
        state.last_solar_window_status = control_logic.is_solar_window(state.config, state)
        
        regelfuehler = result["regelfuehler"]
        
        # Save active sensor name for status message
        if regelfuehler is state.sensors.t_mittig:
            state.control.active_rule_sensor = "Mittig"
        elif regelfuehler is state.sensors.t_unten:
            state.control.active_rule_sensor = "Unten"
        else:
            state.control.active_rule_sensor = "Unknown"

        await control_logic.handle_compressor_off(state, session, regelfuehler, state.control.aktueller_ausschaltpunkt, state.min_laufzeit, state.sensors.t_oben, state.set_kompressor_status)
        await control_logic.handle_compressor_on(state, session, regelfuehler, state.control.aktueller_einschaltpunkt, state.control.aktueller_ausschaltpunkt, state.min_laufzeit, state.min_pause, state.last_solar_window_status, state.sensors.t_oben, state.set_kompressor_status)
        await control_logic.handle_mode_switch(state, session, state.sensors.t_oben, state.sensors.t_mittig, state.set_kompressor_status)
        
    # 4. Sofort-Alarme prüfen (Moved outside safety check to ensure it runs even if sensors fail)
    await check_and_send_alerts(session, state)

async def log_system_state(state):
    """Schreibt CSV-Log und aktualisiert LCD."""
    # 1. LCD Update
    def f_temp(prefix, val, fmt=".1f"):
        if val is None:
            return f"{prefix}:Err"
        try:
            return f"{prefix}:{val:{fmt}}"
        except Exception:
            return f"{prefix}:Err"

    if not state.hardware_manager: return
    state.hardware_manager.write_lcd(
        f"{f_temp('Oben', state.sensors.t_oben)} {f_temp('Unt', state.sensors.t_unten)}",
        f"Mit {f_temp('Verd', state.sensors.t_verd, '.0f')}/{f_temp('V', state.sensors.t_vorlauf, '.0f')}",
        f"Ziel:{state.control.aktueller_einschaltpunkt:.0f}/{state.control.aktueller_ausschaltpunkt:.0f} {'ON' if state.control.kompressor_ein else 'OFF'}",
        f"{state.control.previous_modus[:10] if state.control.previous_modus else ''} {state.solar.soc if state.solar.soc else 0}%"
    )

    # 2. CSV Logging
    try:
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
            async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        # Optimization: Header check removed from loop (done at startup)

        # Power Source
        power_source = "Netz"
        if state.solar.feedinpower and state.solar.feedinpower > 0: power_source = "Solar"
        elif state.solar.batpower and state.solar.batpower > 0: power_source = "Batterie"

        def fmt_csv(val): return str(val) if val is not None else "N/A"
        solax = state.solar.last_api_data or {}
        
        csv_line = [
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            fmt_csv(state.sensors.t_oben), fmt_csv(state.sensors.t_unten), fmt_csv(state.sensors.t_mittig),
            fmt_csv(state.sensors.t_boiler), fmt_csv(state.sensors.t_verd), fmt_csv(state.sensors.t_vorlauf),
            "1" if state.control.kompressor_ein else "0",
            fmt_csv(solax.get("acpower", 0)), fmt_csv(state.solar.feedinpower),
            fmt_csv(state.solar.batpower), fmt_csv(state.solar.soc),
            fmt_csv(solax.get("powerdc1", 0)), fmt_csv(solax.get("powerdc2", 0)),
            fmt_csv(solax.get("consumeenergy", 0)),
            fmt_csv(state.control.aktueller_einschaltpunkt), fmt_csv(state.control.aktueller_ausschaltpunkt),
            "1" if state.control.solar_ueberschuss_aktiv else "0",
            "1" if control_logic.is_nighttime(state.config, tz=state.local_tz) else "0",
            power_source, fmt_csv(state.solar.forecast_tomorrow),
            fmt_csv(state.control.activation_reason)
        ]
        
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            await f.write(",".join(csv_line) + "\n")
    except Exception as e:
        logging.error(f"Fehler beim Schreiben der CSV: {e}")

async def main_loop():
    session = None
    try:
        session = await setup_application()
        
        # Send Startup Message
    # Send Startup Message (Non-blocking)
        if state.bot_token and state.chat_id:
            asyncio.create_task(
                send_welcome_message(session, state.chat_id, state.bot_token, state)
            )
            logging.info("Startup message task created.")

        last_vpn_check = datetime.now() - timedelta(minutes=1)
        
        while not stop_event.is_set():
            try:
                now = datetime.now(state.local_tz)
                
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
                await run_logic_step(session, state)
                await log_system_state(state)
            except Exception as loop_e:
                logging.error(f"Fehler im Main-Loop-Durchlauf: {loop_e}", exc_info=True)
                
            await asyncio.sleep(10)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        error_msg = f"🚨 CRITICAL: Unbehandelter Fehler in Main Loop: {e}"
        logging.critical(error_msg, exc_info=True)
        
        # Explicitly signal FAILURE to healthcheck service
        if state and state.healthcheck_url:
            try:
                from telegram_api import send_healthcheck_ping
                fail_url = state.healthcheck_url + "/fail"
                # Use a fresh task or direct call to ensure it's sent
                await send_healthcheck_ping(session, fail_url)
                logging.info("Explicit /fail ping sent to healthcheck service.")
            except Exception as ping_err:
                logging.error(f"Fehler beim Senden des Healthcheck-Fail-Pings: {ping_err}")

        if state.bot_token and state.chat_id:
            try:
                # Explicitly await send_telegram_message to ensure it's sent before exit
                await control_logic.send_telegram_message(session, state.chat_id, error_msg, state.bot_token)
            except Exception as tg_err:
                logging.error(f"Fehler beim Senden der Telegram-Kritik-Nachricht: {tg_err}")
    finally:
        logging.info("Shutting down...")
        if state and hasattr(state, 'hardware_manager') and state.hardware_manager:
            state.hardware_manager.cleanup()
        if session:
            await session.close()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass


