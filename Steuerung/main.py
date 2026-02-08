import asyncio
import logging
import os
import sys
import signal
from datetime import datetime, timedelta
import aiofiles
import pytz

# Import core modules
from config_manager import ConfigManager
from state import State
from hardware import HardwareManager
from sensors import SensorManager
from telegram_handler import telegram_task
from csv_rotator import CSVRotator
from solax import get_solax_data
from weather_forecast import update_solar_forecast, log_forecast_to_csv

# Import control logic
from control_logic import (
    check_pressure_and_config,
    determine_mode_and_setpoints,
    handle_compressor_off,
    handle_compressor_on,
    handle_mode_switch,
    set_last_compressor_off_time
)
from logic_utils import is_nighttime, is_solar_window, check_log_throttle
from utils import check_and_fix_csv_header, HEIZUNGSDATEN_CSV, EXPECTED_CSV_HEADER

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("heizungssteuerung.log"),
        logging.StreamHandler(sys.stdout)
    ]
)

# Global instances for shutdown handler
hardware_manager = None
telegram_future = None

async def set_kompressor_status(state, ein: bool, force: bool = False, t_boiler_oben=None) -> bool:
    """
    Schaltet den Kompressor ein oder aus und aktualisiert den State.
    Gibt True zurück, wenn geschaltet wurde.
    """
    global hardware_manager
    if not hardware_manager:
        logging.error("HardwareManager nicht initialisiert!")
        return False

    if state.control.kompressor_ein == ein and not force:
        return False

    # Hardware schalten
    hardware_manager.set_compressor_state(ein)
    
    # State update
    state.control.kompressor_ein = ein
    now = datetime.now(state.local_tz)

    if ein:
        state.stats.last_compressor_on_time = now
        state.stats.start_time = now # Track current cycle start
        logging.info(f"Kompressor EINGESCHALTET. T_Oben={t_boiler_oben}")
    else:
        # Runtime calculation
        if state.stats.last_compressor_on_time:
            runtime = now - state.stats.last_compressor_on_time
            state.stats.last_runtime = runtime
            state.stats.total_runtime_today += runtime
            state.stats.last_completed_cycle = now
        
        set_last_compressor_off_time(state, now)
        logging.info(f"Kompressor AUSGESCHALTET.")

    return True

async def log_to_csv(state, now, t_oben, t_unten, t_mittig, t_verd, solax_data=None):
    """Schreibt Systemdaten in die CSV Datei."""
    try:
        if not os.path.exists(HEIZUNGSDATEN_CSV):
            async with aiofiles.open(HEIZUNGSDATEN_CSV, 'w', newline='', encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")

        # Daten vorbereiten
        t_boiler = state.sensors.t_boiler if state.sensors.t_boiler else (
            (t_oben + t_mittig + t_unten) / 3 if (t_oben and t_mittig and t_unten) else 0.0
        )
        
        # Power & Solar values
        ac_power = solax_data.get('acpower', 0) if solax_data else 0
        feedin = solax_data.get('feedinpower', 0) if solax_data else 0
        bat_power = solax_data.get('batPower', 0) if solax_data else 0
        soc = solax_data.get('soc', 0) if solax_data else 0
        dc1 = solax_data.get('powerdc1', 0) if solax_data else 0
        dc2 = solax_data.get('powerdc2', 0) if solax_data else 0
        consume = solax_data.get('consumeenergy', 0) if solax_data else 0
        
        # Logic states
        komp_status = "EIN" if state.control.kompressor_ein else "AUS"
        solar_ueberschuss = "JA" if state.control.solar_ueberschuss_aktiv else "NEIN"
        urlaubs_modus = "JA" if state.urlaubsmodus_aktiv else "NEIN"
        prognose_morgen = state.solar.forecast_tomorrow if state.solar.forecast_tomorrow else 0.0
        power_source = "Netz" # Simplified default
        if bat_power > 100: power_source = "Batterie"
        elif ac_power > 100: power_source = "PV"

        row = [
            now.strftime("%Y-%m-%d %H:%M:%S"),
            f"{t_oben:.1f}" if t_oben else "",
            f"{t_unten:.1f}" if t_unten else "",
            f"{t_mittig:.1f}" if t_mittig else "",
            f"{t_boiler:.1f}",
            f"{t_verd:.1f}" if t_verd else "",
            komp_status,
            f"{ac_power}", f"{feedin}", f"{bat_power}", f"{soc}", f"{dc1}", f"{dc2}", f"{consume}",
            f"{state.control.aktueller_einschaltpunkt}",
            f"{state.control.aktueller_ausschaltpunkt}",
            solar_ueberschuss,
            urlaubs_modus,
            power_source,
            f"{prognose_morgen}"
        ]
        
        line = ",".join(row) + "\n"
        
        async with aiofiles.open(HEIZUNGSDATEN_CSV, 'a', newline='', encoding="utf-8") as f:
            await f.write(line)
            
    except Exception as e:
        logging.error(f"Fehler beim CSV-Logging: {e}")

async def main_loop():
    global hardware_manager, telegram_future
    
    # Init Components
    config_manager = ConfigManager()
    state = State(config_manager)
    
    hardware_manager = HardwareManager()
    hardware_manager.init_gpio()
    await hardware_manager.init_lcd()
    
    sensor_manager = SensorManager()
    csv_rotator = CSVRotator() # New CSV Rotator
    
    # Check/Fix CSV Header at startup
    check_and_fix_csv_header(HEIZUNGSDATEN_CSV)

    # Start Background Tasks
    # 1. Telegram Task
    telegram_future = asyncio.create_task(telegram_task(
        sensor_manager.read_temperature,
        sensor_manager.sensor_ids,
        lambda: state.control.kompressor_ein,
        lambda: state.stats.current_runtime,
        lambda: state.stats.total_runtime_today,
        state.config,
        get_solax_data,
        state,
        None, None, # Charts funcs (passed as None for now to avoid circular deps if needed)
        is_nighttime,
        is_solar_window
    ))
    
    # 2. CSV Rotator Task (Daily)
    asyncio.create_task(csv_rotator.run_daily())
    logging.info("CSV Rotator task started.")

    logging.info("WP Steuerung gestartet (Refactored Main Loop)")

    while True:
        try:
            state.local_tz = pytz.timezone("Europe/Berlin")
            now = datetime.now(state.local_tz)
            
            # 1. Update Config (if changed)
            state.update_config()
            
            # 2. Read Sensors
            temps = await sensor_manager.get_all_temperatures()
            state.sensors.t_oben = temps.get("oben")
            state.sensors.t_mittig = temps.get("mittig")
            state.sensors.t_unten = temps.get("unten")
            state.sensors.t_verd = temps.get("verd")
            
            # 3. Read Solar/Weather
            solax_data = await get_solax_data(state.session, state) # Note: session is None in state init? need to create one?
            # Telegram task creates its own session. For main loop, we might need one if get_solax_data expects it.
            # solax.py uses 'session' arg. We should create a shared session or use a one-off.
            # For simplicity here, passing None might fail if get_solax_data doesn't handle it. 
            # Looking at solax.py: async with session.get... -> Needs session.
            
            # Let's fix session handling:
            if state.session is None or state.session.closed:
                 import aiohttp
                 state.session = aiohttp.ClientSession()

            # 4. Check Safety/Pressure
            pressure_ok = await check_pressure_and_config(
                state.session, state, 
                hardware_manager.read_pressure_sensor, # Wrap generic check? 
                # check_pressure_and_config expects 'handle_pressure_check_func'
                # control_logic.py: pressure_ok = await handle_pressure_check_func(session, state)
                # We need an adapter since hardware_manager.read_pressure_sensor is sync/simple
                lambda s, st: asyncio.to_thread(hardware_manager.read_pressure_sensor),
                set_kompressor_status,
                state.update_config,
                lambda: state.last_config_hash
            )
            
            if not pressure_ok:
                await asyncio.sleep(5)
                continue

            # 5. Determine Mode & Setpoints
            mode_result = await determine_mode_and_setpoints(state, state.sensors.t_unten, state.sensors.t_mittig)
            state.control.aktueller_einschaltpunkt = mode_result["einschaltpunkt"]
            state.control.aktueller_ausschaltpunkt = mode_result["ausschaltpunkt"]
            regelfuehler_val = mode_result["regelfuehler"]

            # 6. Control Logic
            # Check Mode Switch OFF
            await handle_mode_switch(state, state.session, state.sensors.t_oben, state.sensors.t_mittig, set_kompressor_status)
            
            # Check ON
            await handle_compressor_on(
                state, state.session, regelfuehler_val, 
                state.control.aktueller_einschaltpunkt, 
                state.control.aktueller_ausschaltpunkt,
                state.min_laufzeit, state.min_pause,
                is_solar_window(state.config, state),
                state.sensors.t_oben,
                set_kompressor_status
            )
            
            # Check OFF
            await handle_compressor_off(
                state, state.session, regelfuehler_val,
                state.control.aktueller_ausschaltpunkt,
                state.min_laufzeit,
                state.sensors.t_oben,
                set_kompressor_status
            )

            # 7. Update Hardware Display
            if hardware_manager.lcd:
                # Simple status display
                line1 = f"T: {state.sensors.t_oben:.1f}/{state.sensors.t_mittig:.1f}" if state.sensors.t_oben else "Sensors Error"
                line2 = f"Z: {state.control.aktueller_einschaltpunkt}/{state.control.aktueller_ausschaltpunkt}"
                line3 = f"St: {'EIN' if state.control.kompressor_ein else 'AUS'} {state.control.previous_modus[:10]}"
                line4 = datetime.now().strftime("%H:%M:%S")
                await asyncio.to_thread(hardware_manager.write_lcd, line1, line2, line3, line4)

            # 8. Log to CSV
            await log_to_csv(state, now, state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd, state.solar.last_api_data)

            # Loop cycle sleep
            await asyncio.sleep(10)

        except Exception as e:
            logging.error(f"Error in main loop: {e}", exc_info=True)
            await asyncio.sleep(10)

def handle_exit(signum, frame):
    logging.info("Shutdown requested...")
    loop = asyncio.get_running_loop()
    loop.stop()

if __name__ == "__main__":
    # Note: signals work best in main thread, assume sync start
    # Windows has limits with signals, but basic SIGINT works usually
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.critical(f"Fatal error: {e}", exc_info=True)
