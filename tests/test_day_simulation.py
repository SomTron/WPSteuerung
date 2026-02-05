import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, time, timedelta
import pytz
import sys
import os
import asyncio
import configparser

# Ensure we can import from parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from control_logic import (
    determine_mode_and_setpoints,
    handle_compressor_on,
    handle_compressor_off,
    check_sensors_and_safety
)

# --- HELPER FUNCTIONS ---

def load_simulation_config():
    """Loads the simulation configuration from config_simulation.ini."""
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(__file__), '..', 'config_simulation.ini')
    config.read(config_path)
    return config

def create_mock_state(config):
    """Creates a mock state object based on the configuration."""
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    # Mocking Config as object with attributes
    mock_config = MagicMock()
    
    # Helper to create nested mocks
    def create_section_mock(data):
        section = MagicMock()
        for k, v in data.items():
            setattr(section, k, v)
        return section

    mock_config.Heizungssteuerung = create_section_mock({
        "NACHT_START": "22:00",
        "NACHT_ENDE": "06:00",
        "NACHTABSENKUNG": 5.0,
        "NACHTABSENKUNG_START": "22:00",
        "NACHTABSENKUNG_END": "06:00",
        "EINSCHALTPUNKT": 40,
        "AUSSCHALTPUNKT": 50,
        "AUSSCHALTPUNKT_ERHOEHT": 55,
        "EINSCHALTPUNKT_ERHOEHT": 45,
        "MIN_LAUFZEIT": 15,
        "MIN_PAUSE": 20,
        "UEBERGANGSMODUS_MORGENS_ENDE": "10:00",
        "UEBERGANGSMODUS_ABENDS_START": "18:00",
        "SICHERHEITS_TEMP": 60.0,
        "VERDAMPFERTEMPERATUR": -10.0,
        "VERDAMPFER_RESTART_TEMP": 9.0
    })

    mock_config.Urlaubsmodus = create_section_mock({
        "URLAUBSABSENKUNG": 6.0
    })

    mock_config.Solarueberschuss = create_section_mock({
        "BATPOWER_THRESHOLD": 600.0,
        "SOC_THRESHOLD": 95.0,
        "FEEDINPOWER_THRESHOLD": 600.0
    })
    
    state.config = mock_config
    
    # Sub-states
    state.sensors = MagicMock()
    state.solar = MagicMock()
    state.control = MagicMock()
    state.stats = MagicMock()

    # Initial values
    state.control.aktueller_ausschaltpunkt = 50
    state.control.aktueller_einschaltpunkt = 40
    state.control.kompressor_ein = False
    state.control.solar_ueberschuss_aktiv = False
    state.control.previous_modus = None
    state.control.ausschluss_grund = None
    
    state.stats.last_compressor_on_time = datetime(2023, 1, 1, 0, 0, tzinfo=state.local_tz)
    state.stats.last_compressor_off_time = datetime(2023, 1, 1, 0, 0, tzinfo=state.local_tz)
    state.stats.total_runtime_today = timedelta()
    
    state.solar.batpower = 0
    state.solar.soc = 50
    state.solar.feedinpower = 0
    state.solar.forecast_tomorrow = 0
    
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    
    # Properties for calculations
    state.basis_ausschaltpunkt = float(config['Heizungssteuerung']['AUSSCHALTPUNKT'])
    state.basis_einschaltpunkt = float(config['Heizungssteuerung']['EINSCHALTPUNKT'])
    state.ausschaltpunkt_erhoeht = float(config['Heizungssteuerung']['AUSSCHALTPUNKT_ERHOEHT'])
    state.einschaltpunkt_erhoeht = float(config['Heizungssteuerung']['EINSCHALTPUNKT_ERHOEHT'])
    
    return state

async def run_simulation_scenario(scenario_name, steps, config):
    print(f"\n\n=== SZENARIO: {scenario_name} ===")
    print(f"{'UHRZEIT':<10} | {'MODUS':<20} | {'TEMP (O/M/U)':<12} | {'REGEL':<6} | {'EIN':<4} | {'AUS':<4} | {'VERD':<6} | {'SOLAR':<8} | {'KOMPRESSOR':<10} | {'INFO'}")
    print("-" * 140)

    mock_state = create_mock_state(config)
    start_date = datetime(2024, 6, 15, 0, 0, 0)
    local_tz = pytz.timezone("Europe/Berlin")
    
    async def mock_set_kompressor(state, status, force=False, t_boiler_oben=None):
        state.control.kompressor_ein = status
        if status:
            state.stats.last_compressor_on_time = current_sim_time
        else:
            state.stats.last_compressor_off_time = current_sim_time
        return True
        
    async def mock_send_telegram(session, chat_id, message, token, parse_mode=None):
        pass

    # CRITICAL: Patch datetime in ALL modules that use it for time logic
    with patch('control_logic.datetime') as mock_dt_ctrl, \
         patch('logic_utils.datetime') as mock_dt_utils, \
         patch('safety_logic.datetime') as mock_dt_safety, \
         patch('control_logic.is_nighttime') as mock_is_night, \
         patch('control_logic.is_solar_window') as mock_is_solar, \
         patch('telegram_api.send_telegram_message', side_effect=mock_send_telegram):
        
        # Setup mocks to return consistent simulation time
        def get_current_time(tz=None):
            return current_sim_time.replace(tzinfo=tz if tz else None)
            
        mock_dt_ctrl.now.side_effect = get_current_time
        mock_dt_utils.now.side_effect = get_current_time
        mock_dt_safety.now.side_effect = get_current_time
        
        # Ensure strptime works (needed by logic_utils)
        mock_dt_utils.strptime.side_effect = datetime.strptime
        mock_dt_ctrl.strptime.side_effect = datetime.strptime

        def update_mocks(sim_time):
            t = sim_time.time()
            night_start = datetime.strptime(config["Heizungssteuerung"].get("NACHTABSENKUNG_START", "22:00"), "%H:%M").time()
            night_end = datetime.strptime(config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00"), "%H:%M").time()
            
            if night_start <= night_end:
                mock_is_night.return_value = (night_start <= t <= night_end)
            else:
                mock_is_night.return_value = (night_start <= t or t <= night_end)
            
            mock_is_solar.return_value = (time(8,0) <= t < time(18,0))

        for step in steps:
            hour, minute, t_mittig, t_unten, bat_power, soc, desc = step[:7]
            t_oben = step[7] if len(step) > 7 else t_mittig 
            t_verd = step[8] if len(step) > 8 else 10.0
            
            current_sim_time = local_tz.localize(start_date.replace(hour=hour, minute=minute))
            update_mocks(current_sim_time)
            
            mock_state.solar.batpower = bat_power
            mock_state.solar.soc = soc
            mock_state.sensors.t_verd = t_verd # Optional but good
            
            setpoints = await determine_mode_and_setpoints(mock_state, t_unten, t_mittig)
            
            safety_ok = await check_sensors_and_safety(
                None, mock_state, t_oben=t_oben, t_unten=t_unten, t_mittig=t_mittig, t_verd=t_verd, 
                set_kompressor_status_func=mock_set_kompressor
            )
            
            if safety_ok:
                min_run = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"]))
                min_pause = timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"]))
                
                if mock_state.control.kompressor_ein:
                    await handle_compressor_off(
                        mock_state, None, setpoints['regelfuehler'], setpoints['ausschaltpunkt'], 
                        min_run, t_oben, mock_set_kompressor
                    )
                else:
                    await handle_compressor_on(
                        mock_state, None, setpoints['regelfuehler'], setpoints['einschaltpunkt'], 
                        setpoints['ausschaltpunkt'], min_run, min_pause, 
                        mock_is_solar.return_value, t_oben, mock_set_kompressor
                    )
            
            time_str = current_sim_time.strftime("%H:%M")
            comp_str = "AN" if mock_state.control.kompressor_ein else "AUS"
            solar_str = f"{bat_power}W"
            temp_str = f"{t_oben}/{t_mittig}/{t_unten}"
            regel_str = f"{setpoints['regelfuehler']}"
            ein_str = f"{setpoints['einschaltpunkt']}"
            aus_str = f"{setpoints['ausschaltpunkt']}"
            
            # Validation logic
            t = current_sim_time.time()
            is_transition = ((time(8,0) <= t <= time(10,0)) or (time(18,0) <= t <= time(19,30)))
            has_solar = bat_power > 600 or (soc >= 95 and False) # Simplified
            
            validation_suffix = ""
            if is_transition and not has_solar and t_mittig < setpoints['einschaltpunkt']:
                night_setpoint = float(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 43)) - float(config["Heizungssteuerung"].get("NACHTABSENKUNG", 5.0))
                if t_mittig <= night_setpoint:
                    if not mock_state.control.kompressor_ein:
                        error_msg = f"BUG: Kompressor AUS trotz kritischer Kälte ({t_mittig} <= {night_setpoint})"
                        print(error_msg)
                        raise AssertionError(error_msg)
                    validation_suffix = f" [KORREKT: AN wegen Kälte]"
                elif mock_state.control.kompressor_ein:
                    # Should be off unless was already running and min_run not met
                    # Simplified for this specific test case
                    pass

            print(f"{time_str:<10} | {setpoints['modus']:<20} | {temp_str:<12} | {regel_str:<6} | {ein_str:<4} | {aus_str:<4} | {t_verd:<6.1f} | {solar_str:<8} | {comp_str:<10} | {desc}{validation_suffix}")
    print("-" * 140)

@pytest.mark.asyncio
async def test_scenarios():
    config = load_simulation_config()
    
    # Run Scenario 6 specifically to verify fix
    steps_6 = [
        (17, 59, 35, 30, 0, 50, "Vor Abend-Übergangsmodus"),
        (18, 0, 25, 22, 0, 50, "Abend-Übergangsmodus START - Sehr Kalt (25 <= 35)"),
        (18, 15, 25, 22, 0, 50, "Sollte AN sein"),
    ]
    await run_simulation_scenario("6. Abend-Übergang Kaltstart", steps_6, config)
    
    # Run others...
    steps_1 = [(0,0,40,35,0,20,"Start")]
    await run_simulation_scenario("1. Test", steps_1, config)

if __name__ == "__main__":
    asyncio.run(test_scenarios())
