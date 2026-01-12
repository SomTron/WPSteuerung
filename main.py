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

# Modules
from config_manager import ConfigManager
from state import State
from sensors import SensorManager
from hardware import HardwareManager
from logging_config import setup_logging
from solax import get_solax_data
import control_logic
from telegram_handler import (
    telegram_task, 
    start_healthcheck_task, 
    send_telegram_message,
    create_robust_aiohttp_session,
    get_boiler_temperature_history, # Needed for Telegram task
    get_runtime_bar_chart # Needed for Telegram task
)
from api import app, init_api

# Global objects
config_manager = ConfigManager()
state = None
sensor_manager = None
hardware_manager = None
stop_event = threading.Event()

def handle_exit(signum, frame):
    logging.info(f"Signal {signum} empfangen. Beende Programm...")
    stop_event.set()
    sys.exit(0)

async def set_kompressor_status(state, status, force=False, t_boiler_oben=None):
    """
    Schaltet den Kompressor und aktualisiert den State.
    Logik weitgehend übernommen aus original main.py, aber nutzt HardwareManager.
    """
    if status:
        # Einschalten
        if state.kompressor_ein and not force:
            return True
        
        # Hardware schalten
        hardware_manager.set_compressor_state(True)
        state.kompressor_ein = True
        return True
    else:
        # Ausschalten
        if not state.kompressor_ein and not force:
            return True

        hardware_manager.set_compressor_state(False)
        state.kompressor_ein = False
        return True

async def handle_pressure_check(session, state):
    """Liest den Druckschalter über HardwareManager."""
    pressure_ok = hardware_manager.read_pressure_sensor()
    
    if not pressure_ok and state.last_pressure_state:
         # Notify if changed to Error
         pass # Logic handled in control_logic mostly
         
    return pressure_ok

def run_api():
    """Startet den FastAPI-Server."""
    try:
        # Host/Port aus Config
        host = state.config.Heizungssteuerung.API_HOST
        port = state.config.Heizungssteuerung.API_PORT
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except Exception as e:
        logging.error(f"Fehler beim Starten der API: {e}")

async def main_loop():
    global state, sensor_manager, hardware_manager

    # 1. Config laden
    config_manager.load_config()
    config = config_manager.get()
    
    # 2. State init
    state = State(config_manager)
    
    # 3. Logging setup
    # Create a temporary session for logging if needed, or pass None and let Handler create one
    setup_logging(enable_full_log=True, telegram_config=state.config.Telegram)
    
    logging.info("Starten der Wärmepumpensteuerung (Refactored)...")

    # 4. Hardware & Sensors init
    hardware_manager = HardwareManager()
    hardware_manager.init_gpio()
    await hardware_manager.init_lcd()
    
    sensor_manager = SensorManager() # IDs hardcoded in class for now as per original
    
    # 5. API init
    control_funcs = {
        "set_kompressor": set_kompressor_status
    }
    init_api(state, control_funcs)
    
    # Start API Thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # 6. Session & Tasks
    session = create_robust_aiohttp_session()
    state.session = session # Optional, for access elsewhere if needed
    
    # Start Telegram Task
    # telegram_task(read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func, is_solar_window_func)
    # Adapting arguments to match expected signature
    tg_task = asyncio.create_task(telegram_task(
        read_temperature_func=sensor_manager.read_temperature,
        sensor_ids=sensor_manager.sensor_ids,
        kompressor_status_func=lambda: state.kompressor_ein,
        current_runtime_func=lambda: state.current_runtime,
        total_runtime_func=lambda: state.total_runtime_today,
        config=state.config, # Passes AppConfig object, updated telegram_handler expects this now mostly
        get_solax_data_func=get_solax_data,
        state=state,
        get_temperature_history_func=get_boiler_temperature_history,
        get_runtime_bar_chart_func=get_runtime_bar_chart,
        is_nighttime_func=control_logic.is_nighttime,
        is_solar_window_func=control_logic.is_solar_window
    ))
    
    # Start Healthcheck Task
    hc_task = asyncio.create_task(start_healthcheck_task(session, state))
    
    # 7. Main Loop
    try:
        while not stop_event.is_set():
            loop_start = datetime.now()
            
            # --- Sensoren lesen ---
            temps = await sensor_manager.get_all_temperatures()
            t_oben = temps.get("oben")
            t_mittig = temps.get("mittig")
            t_unten = temps.get("unten")
            t_verd = temps.get("verd")
            
            # Update API data (Solax) periodically - logic inside get_solax_data caches result
            await get_solax_data(session, state)
            
            # Energie-Daten aktualisieren (für Logic)
            if state.last_api_data:
                state.feedinpower = state.last_api_data.get("feedinpower", 0)
                state.batpower = state.last_api_data.get("batPower", 0)
                state.soc = state.last_api_data.get("soc", 0)
            
            # --- Steuerungslogik ---
            
            # 1. Druckschalter & Config
            if not await control_logic.check_pressure_and_config(
                session, state, 
                handle_pressure_check, 
                set_kompressor_status, 
                state.update_config, 
                lambda: "hash" # Mock hash func, config reload handled internally
            ):
                 # Wenn Check False liefert (Fehler), Loop continue? 
                 # Original logic continues but kompressor might be off.
                 # check_pressure_and_config handles turning off.
                 pass

            # 2. Sensoren & Safety
            sensors_safe = await control_logic.check_sensors_and_safety(
                session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status
            )
            
            if sensors_safe:
                # 3. Modus & Setpoints
                result = await control_logic.determine_mode_and_setpoints(state, t_unten, t_mittig)
                state.aktueller_einschaltpunkt = result["einschaltpunkt"]
                state.aktueller_ausschaltpunkt = result["ausschaltpunkt"]
                regelfuehler = result["regelfuehler"]
                state.solar_ueberschuss_aktiv = result["solar_ueberschuss_aktiv"] # Update state
                
                # 4. Schalten
                await control_logic.handle_compressor_off(
                    state, session, regelfuehler, state.aktueller_ausschaltpunkt, 
                    state.min_laufzeit, t_oben, set_kompressor_status
                )
                
                await control_logic.handle_compressor_on(
                    state, session, regelfuehler, state.aktueller_einschaltpunkt, 
                    state.min_laufzeit, state.min_pause, 
                    state.last_solar_window_status, t_oben, set_kompressor_status
                )
                
                # 5. Modus Wechsel Check
                await control_logic.handle_mode_switch(state, session, t_oben, t_mittig, set_kompressor_status)

            # --- LCD Update ---
            hardware_manager.write_lcd(
                f"Oben:{t_oben if t_oben else 'Err':.1f} Unt:{t_unten if t_unten else 'Err':.1f}",
                f"Mit :{t_mittig if t_mittig else 'Err':.1f} Verd:{t_verd if t_verd else 'Err':.0f}",
                f"Ziel:{state.aktueller_einschaltpunkt:.0f}/{state.aktueller_ausschaltpunkt:.0f} {'ON' if state.kompressor_ein else 'OFF'}",
                f"{state.previous_modus[:10]} {state.soc}%"
            )

            # --- CSV Logging (Restored) ---
            try:
                csv_file = "heizungsdaten.csv"
                # Header Check (synchron)
                from utils import check_and_fix_csv_header
                if not os.path.exists(csv_file):
                    # Erstellen mit Header
                    async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                        from utils import EXPECTED_CSV_HEADER
                        await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
                else:
                    # Header prüfen (blocking IO, aber selten)
                    check_and_fix_csv_header(csv_file)

                # Daten vorbereiten
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # Power Source Logic
                power_source = "Netz"
                if state.feedinpower > 0:
                    power_source = "Solar"
                elif state.batpower > 0:
                    power_source = "Batterie"

                # Werte aufbereiten (None -> N/A oder 0)
                def fmt_csv(val): return str(val) if val is not None else "N/A"
                
                # Spalten Mapping gemäß EMPFEHLUNG in utils.py
                # "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Boiler", "T_Verd", "Kompressor",
                # "ACPower", "FeedinPower", "BatPower", "SOC", "PowerDC1", "PowerDC2", "ConsumeEnergy",
                # "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "Nachtabsenkung", "PowerSource"
                
                solax = state.last_api_data or {}
                
                csv_line = [
                    timestamp,
                    fmt_csv(t_oben),
                    fmt_csv(t_unten),
                    fmt_csv(t_mittig),
                    fmt_csv(state.t_boiler),
                    fmt_csv(t_verd),
                    "1" if state.kompressor_ein else "0",
                    fmt_csv(solax.get("acpower", 0)),
                    fmt_csv(state.feedinpower),
                    fmt_csv(state.batpower),
                    fmt_csv(state.soc),
                    fmt_csv(solax.get("powerdc1", 0)),
                    fmt_csv(solax.get("powerdc2", 0)),
                    fmt_csv(solax.get("consumeenergy", 0)),
                    fmt_csv(state.aktueller_einschaltpunkt),
                    fmt_csv(state.aktueller_ausschaltpunkt),
                    "1" if state.solar_ueberschuss_aktiv else "0",
                    "1" if control_logic.is_nighttime(state.config) else "0", # Simple bool for Nachtabsenkung col
                    power_source
                ]
                
                async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
                    await f.write(",".join(csv_line) + "\n")

            except Exception as e:
                logging.error(f"Fehler beim Schreiben der CSV: {e}")

            # --- Sleep ---
            # Berechne Restzeit für 10s Loop (aus Config?)
            # Original war time.sleep(10) fix oder ähnlich.
            # Hier asyncio sleep.
            await asyncio.sleep(10)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.critical(f"Unbehandelter Fehler in Main Loop: {e}", exc_info=True)
    finally:
        logging.info("Shutting down...")
        hardware_manager.cleanup()
        await session.close()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
