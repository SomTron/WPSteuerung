import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, time, timedelta
import pytz
import sys
import os
import asyncio
import configparser

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

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
        "UEBERGANGSMODUS_ABENDS_START": "18:00", # Note: simulation uses 18:00 vs default 17:00
        "SICHERHEITS_TEMP": 60.0,
        "VERDAMPFERTEMPERATUR": -10.0,
        "VERDAMPFER_RESTART_TEMP": 9.0
    })

    mock_config.Urlaubsmodus = create_section_mock({
        "URLAUBSABSENKUNG": 6.0 # Default value from original main.py
    })

    mock_config.Solarueberschuss = create_section_mock({
        "BATPOWER_THRESHOLD": 600.0,
        "SOC_THRESHOLD": 95.0,
        "FEEDINPOWER_THRESHOLD": 600.0
    })
    
    state.config = mock_config
    
    # We need to wrap the real configparser values into the mock object structure
    section = "Heizungssteuerung"
    mock_config.Heizungssteuerung.AUSSCHALTPUNKT = int(config[section].get("AUSSCHALTPUNKT", 50))
    mock_config.Heizungssteuerung.EINSCHALTPUNKT = int(config[section].get("EINSCHALTPUNKT", 40))
    mock_config.Heizungssteuerung.AUSSCHALTPUNKT_ERHOEHT = int(config[section].get("AUSSCHALTPUNKT_ERHOEHT", 55))
    mock_config.Heizungssteuerung.EINSCHALTPUNKT_ERHOEHT = int(config[section].get("EINSCHALTPUNKT_ERHOEHT", 45))
    
    # Parse times from config for State properties
    def parse_time(section, key, default):
        try:
            return datetime.strptime(config[section].get(key, default), "%H:%M").time()
        except:
            return datetime.strptime(default, "%H:%M").time()
            
    # Access via dict (config) for setup, assign to properties
    state.nachtabsenkung_ende = parse_time("Heizungssteuerung", "NACHTABSENKUNG_END", "06:00")
    state.uebergangsmodus_morgens_ende = parse_time("Heizungssteuerung", "UEBERGANGSMODUS_MORGENS_ENDE", "10:00")
    state.uebergangsmodus_abends_start = parse_time("Heizungssteuerung", "UEBERGANGSMODUS_ABENDS_START", "18:00")
    state.nachtabsenkung_start = parse_time("Heizungssteuerung", "NACHTABSENKUNG_START", "22:00")
    
    state.aktueller_ausschaltpunkt = mock_config.Heizungssteuerung.AUSSCHALTPUNKT
    state.aktueller_einschaltpunkt = mock_config.Heizungssteuerung.EINSCHALTPUNKT
    state.basis_ausschaltpunkt = state.aktueller_ausschaltpunkt
    state.basis_einschaltpunkt = state.aktueller_einschaltpunkt
    state.ausschaltpunkt_erhoeht = mock_config.Heizungssteuerung.AUSSCHALTPUNKT_ERHOEHT
    state.einschaltpunkt_erhoeht = mock_config.Heizungssteuerung.EINSCHALTPUNKT_ERHOEHT
    state.sicherheits_temp = mock_config.Heizungssteuerung.SICHERHEITS_TEMP
    state.verdampfertemperatur = mock_config.Heizungssteuerung.VERDAMPFERTEMPERATUR
    state.verdampfer_restart_temp = mock_config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
    
    # Initial State
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.solar_ueberschuss_aktiv = False
    state.previous_modus = None
    state.kompressor_ein = False
    state.last_compressor_on_time = datetime(2023, 1, 1, 0, 0, tzinfo=state.local_tz)
    state.last_compressor_off_time = datetime(2023, 1, 1, 0, 0, tzinfo=state.local_tz)
    state.last_completed_cycle = datetime(2023, 1, 1, 0, 0, tzinfo=state.local_tz)
    state.last_solar_window_check = None
    state.last_solar_window_status = False
    state.previous_temp_conditions = False
    state.previous_abschalten = False
    state.last_no_start_log = None
    state.last_pause_log = None
    state.last_abschalt_log = None
    state.batpower = 0
    state.soc = 50
    state.feedinpower = 0
    state.ausschluss_grund = None
    state.last_sensor_error_time = None
    state.last_verdampfer_notification = None
    state.chat_id = "mock_chat_id"
    state.bot_token = "mock_token"
    
    return state

async def run_simulation_scenario(scenario_name, steps, config):
    """Runs a specific simulation scenario."""
    print(f"\n\n=== SZENARIO: {scenario_name} ===")
    print(f"{'UHRZEIT':<10} | {'MODUS':<20} | {'TEMP (O/M/U)':<12} | {'REGEL':<6} | {'EIN':<4} | {'AUS':<4} | {'VERD':<6} | {'SOLAR':<8} | {'KOMPRESSOR':<10} | {'INFO'}")
    print("-" * 140)

    mock_state = create_mock_state(config)
    start_date = datetime(2024, 6, 15, 0, 0, 0) # Base date
    local_tz = pytz.timezone("Europe/Berlin")
    
    # Mock functions
    async def mock_set_kompressor(state, status, force=False, t_boiler_oben=None):
        state.kompressor_ein = status
        if status:
            state.last_compressor_on_time = datetime.now(state.local_tz)
        else:
            state.last_compressor_off_time = datetime.now(state.local_tz)
        return True
        
    async def mock_send_telegram(session, chat_id, message, token, parse_mode=None):
        # print(f"  [TELEGRAM] {message}")
        pass

    with patch('control_logic.datetime') as mock_dt, \
         patch('control_logic.is_nighttime') as mock_is_night, \
         patch('control_logic.is_solar_window') as mock_is_solar, \
         patch('control_logic.send_telegram_message', side_effect=mock_send_telegram): # Mock telegram
        
        # Setup mocks
        mock_dt.now.side_effect = lambda tz=None: current_sim_time.replace(tzinfo=tz if tz else None)
        
        def update_mocks(sim_time):
            t = sim_time.time()
            # Use config values for night check (mit Fallback auf alte Schlüssel)
            night_start = datetime.strptime(
                config["Heizungssteuerung"].get("NACHTABSENKUNG_START", 
                config["Heizungssteuerung"].get("NACHT_START", "22:00")), "%H:%M").time()
            night_end = datetime.strptime(
                config["Heizungssteuerung"].get("NACHTABSENKUNG_END", 
                config["Heizungssteuerung"].get("NACHT_ENDE", "06:00")), "%H:%M").time()
            
            if night_start <= night_end:
                is_night = night_start <= t <= night_end
            else:
                is_night = night_start <= t or t <= night_end
            
            mock_is_night.return_value = is_night
            
            # Simplified solar window (8-18)
            is_solar = (t >= time(8,0) and t < time(18,0))
            mock_is_solar.return_value = is_solar

        for step in steps:
            hour, minute, t_mittig, t_unten, bat_power, soc, desc = step[:7]
            # Optional extra params for failures
            t_oben = step[7] if len(step) > 7 else t_mittig 
            t_verd = step[8] if len(step) > 8 else 10.0
            
            # 1. Update Time & State
            current_sim_time = start_date.replace(hour=hour, minute=minute)
            current_sim_time = local_tz.localize(current_sim_time)
            update_mocks(current_sim_time)
            
            mock_state.batpower = bat_power
            mock_state.soc = soc
            mock_state.feedinpower = 0 
            
            # 2. Determine Mode
            setpoints = await determine_mode_and_setpoints(mock_state, t_unten, t_mittig)
            
            # 3. Simulate Control Loop
            # Check Safety
            safety_ok = await check_sensors_and_safety(
                None, mock_state, t_oben=t_oben, t_unten=t_unten, t_mittig=t_mittig, t_verd=t_verd, 
                set_kompressor_status_func=mock_set_kompressor
            )
            
            if safety_ok:
                # Handle On/Off
                if mock_state.kompressor_ein:
                    await handle_compressor_off(
                        mock_state, None, setpoints['regelfuehler'], setpoints['ausschaltpunkt'], 
                        timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"])), t_oben, mock_set_kompressor
                    )
                else:
                    await handle_compressor_on(
                        mock_state, None, setpoints['regelfuehler'], setpoints['einschaltpunkt'], 
                        timedelta(minutes=int(config["Heizungssteuerung"]["MIN_LAUFZEIT"])), 
                        timedelta(minutes=int(config["Heizungssteuerung"]["MIN_PAUSE"])), 
                        mock_is_solar.return_value, t_oben, mock_set_kompressor
                    )
            
            # 4. Output Result
            time_str = current_sim_time.strftime("%H:%M")
            comp_str = "AN" if mock_state.kompressor_ein else "AUS"
            solar_str = f"{bat_power}W" if bat_power is not None else "N/A"
            temp_str = f"{t_oben}/{t_mittig}/{t_unten}" if t_mittig is not None and t_unten is not None and t_oben is not None else "ERR"
            verd_str = f"{t_verd}" if t_verd is not None else "ERR"
            regel_str = f"{setpoints['regelfuehler']}" if setpoints['regelfuehler'] is not None else "N/A"
            ein_str = f"{setpoints['einschaltpunkt']}"
            aus_str = f"{setpoints['ausschaltpunkt']}"
            
            # Add safety info to output if relevant
            info_suffix = ""
            if not safety_ok:
                info_suffix = f" [SAFETY: {mock_state.ausschluss_grund}]"
            
            # Validation logic
            t = current_sim_time.time()
            is_transition = ((time(8,0) <= t <= time(10,0)) or (time(18,0) <= t <= time(19,30)))
            has_solar = bat_power > 600 or (soc >= 95 and 0 > 600)
            
            time_since_on = (current_sim_time - mock_state.last_compressor_on_time).total_seconds() / 60
            min_runtime_minutes = int(config["Heizungssteuerung"]["MIN_LAUFZEIT"])
            
            if is_transition and t_mittig < setpoints['einschaltpunkt'] and not has_solar:
                if mock_state.kompressor_ein and time_since_on > min_runtime_minutes:
                    # Check for critical exception (Scenario 6)
                    basis_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 43))
                    nacht_reduction = float(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0.0))
                    night_setpoint = basis_einschaltpunkt - nacht_reduction
                    is_evening_transition = (time(18,0) <= t <= time(19,30))
                    
                    if is_evening_transition and t_mittig <= night_setpoint:
                        pass # Allowed
                    else:
                        error_msg = (f"\n\n*** BUG DETECTED! ***\n"
                                    f"Zeit: {t}\n"
                                    f"Übergangsmodus: Ja\n"
                                    f"Temperatur: {t_mittig} < {setpoints['einschaltpunkt']} (würde einschalten)\n"
                                    f"Solar: {bat_power}W (KEIN Überschuss!)\n"
                                    f"Kompressor: AN (SOLLTE AUS SEIN!)\n")
                        print(error_msg)
                        raise AssertionError(error_msg)
            
            # Extra validation info
            validation_suffix = ""
            if is_transition and not has_solar and t_mittig < setpoints['einschaltpunkt']:
                basis_einschaltpunkt = int(config["Heizungssteuerung"].get("EINSCHALTPUNKT", 43))
                nacht_reduction = float(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0.0))
                night_setpoint = basis_einschaltpunkt - nacht_reduction
                is_evening_transition = (time(18,0) <= t <= time(19,30))
                
                if is_evening_transition and t_mittig <= night_setpoint:
                     if mock_state.kompressor_ein:
                         validation_suffix = f" [KORREKT: AN wegen Kälte ({t_mittig} <= {night_setpoint})]"
                     else:
                         error_msg = f"BUG: Kompressor AUS trotz kritischer Kälte ({t_mittig} <= {night_setpoint})"
                         print(error_msg)
                         raise AssertionError(error_msg)
                elif not mock_state.kompressor_ein:
                    validation_suffix = " [KORREKT: AUS trotz Temp-Bed.]"
            
            print(f"{time_str:<10} | {setpoints['modus']:<20} | {temp_str:<12} | {regel_str:<6} | {ein_str:<4} | {aus_str:<4} | {verd_str:<6} | {solar_str:<8} | {comp_str:<10} | {desc}{validation_suffix}")

    print("-" * 140)

# --- SCENARIOS ---

@pytest.mark.asyncio
async def test_scenarios():
    config = load_simulation_config()
    
    # Scenario 1: Standard Day + Fluctuations
    steps_1 = [
        (0,  0, 40, 35, 0, 20, "Start Nacht"),
        (2,  0, 34, 30, 0, 15, "Temp < Einschalt (35) -> AN"),
        (3,  0, 46, 42, 0, 15, "Temp > Ausschalt (45) -> AUS"),
        (7,  0, 39, 36, 100, 25, "Morgen (Übergang)"),
        (12, 0, 42, 40, 1000, 90, "Solarüberschuss -> Sollwerte hoch"),
        (13, 0, 48, 46, 200, 90, "Wolke! BatPower < 600 -> Normalmodus"),
        (13, 15, 48, 46, 1200, 92, "Sonne da -> Solarüberschuss"),
        (16, 0, 56, 54, 800, 100, "Speicher voll -> AUS"),
        (23, 0, 45, 42, -200, 60, "Nacht"),
    ]
    await run_simulation_scenario("1. Standard Tag + Schwankungen", steps_1, config)

    # Scenario 2: Transitions & Mode Switching
    steps_2 = [
        (9, 55, 45, 42, 100, 50, "Morgen Übergang (Ende 10:00)"),
        (10, 5, 45, 42, 100, 50, "Normalmodus (nach 10:00)"),
        (11, 0, 45, 42, 800, 96, "Direkt zu Solarüberschuss (>600W, SOC>95)"),
        (11, 15, 45, 42, 800, 96, "Kompressor AN (Solar)"),
        (14, 0, 50, 48, -100, 40, "Verbrauch hoch, Bat leer -> Normalmodus"),
        (14, 15, 50, 48, -100, 40, "Kompressor bleibt AN (Hysterese)"),
        (18, 5, 50, 48, -100, 40, "Abend Übergang (Start 18:00)"),
    ]
    await run_simulation_scenario("2. Übergänge & Moduswechsel", steps_2, config)

    # Scenario 3: Failures & Edge Cases
    steps_3 = [
        (10, 0, 40, 38, 0, 50, "Normalbetrieb", 40, 10),
        (10, 15, 40, 38, 0, 50, "Kompressor AN", 40, 10),
        (10, 30, None, 38, 0, 50, "SENSORFEHLER: T_Mittig ausgefallen", 40, 10),
        (10, 45, 42, 40, 0, 50, "Sensor wieder da", 42, 10),
        (11, 0, 45, 42, 0, 50, "Normalbetrieb", 45, 10),
        (11, 15, 45, 42, 0, 50, "ÜBERTEMPERATUR T_Oben=65!", 65, 10),
        (11, 30, 45, 42, 0, 50, "Abkühlung T_Oben=55", 55, 10),
        (12, 0, 45, 42, 0, 50, "VERDAMPFER VEREIST (-15°C)", 45, -15),
    ]
    await run_simulation_scenario("3. Fehlerfälle & Edge Cases", steps_3, config)

    # Scenario 4: Minimum Runtime & Pause
    steps_4 = [
        (8, 0, 40, 35, 0, 50, "Start (Normalmodus)"),
        (8, 5, 34, 30, 0, 50, "Temp < Einschalt -> AN"),
        (8, 10, 46, 42, 0, 50, "Temp > Ausschalt -> Bleibt AN (Min Laufzeit 5/10 min)"),
        (8, 16, 46, 42, 0, 50, "Temp > Ausschalt -> AUS (Laufzeit > 10 min)"),
        (8, 20, 34, 30, 0, 50, "Temp < Einschalt -> Bleibt AUS (Min Pause 4/10 min)"),
        (8, 27, 34, 30, 0, 50, "Temp < Einschalt -> AN (Pause > 10 min)"),
    ]
    await run_simulation_scenario("4. Mindestlaufzeit & Pause", steps_4, config)
    
    # Scenario 5: Transition Mode Edge Cases
    steps_5 = [
        (7, 59, 35, 30, 0, 50, "Vor Übergangsmodus (Nacht)"),
        (8, 0, 35, 30, 0, 50, "Übergangsmodus START - KEIN Solar -> MUSS AUS bleiben!"),
        (8, 1, 35, 30, 0, 50, "Immer noch kein Solar -> MUSS AUS bleiben!"),
        (8, 5, 35, 30, 800, 96, "Solar aktiv -> DARF AN"),
        (8, 16, 35, 30, 200, 50, "Solar weg (nach Min-Laufzeit) -> MUSS AUS"),
        (9, 0, 35, 30, 1000, 97, "Solar wieder da -> DARF AN"),
        (10, 0, 50, 46, 1000, 97, "Nach Übergangsmodus (Normal) -> Temperatur erreicht -> AUS"),
        (10, 15, 35, 30, 0, 50, "Normalmodus, keine Temp-Bedingung -> AUS"),
        (17, 59, 35, 30, 0, 50, "Vor Abend-Übergangsmodus"),
        (18, 0, 35, 30, 0, 50, "Abend-Übergangsmodus START - KEIN Solar -> MUSS AUS!"),
        (18, 1, 35, 30, 700, 96, "Solar aktiv -> DARF AN"),
        (18, 30, 50, 46, 700, 96, "Temperatur erreicht -> AUS"),
        (19, 30, 35, 30, 0, 50, "Nachtmodus -> Temperatur < Einschalt -> DARF AN (Nachtmodus erlaubt ohne Solar)"),
    ]
    await run_simulation_scenario("5. Übergangsmodus Edge Cases", steps_5, config)
    
    # Scenario 6: Evening Transition Cold Start (User Request)
    steps_6 = [
        (17, 59, 35, 30, 0, 50, "Vor Abend-Übergangsmodus"),
        (18, 0, 25, 22, 0, 50, "Abend-Übergangsmodus START - Sehr Kalt (25°C < 28°C Night-Setpoint) - KEIN Solar"),
        (18, 15, 25, 22, 0, 50, "Sollte AN sein wegen kritischer Kälte"),
    ]
    await run_simulation_scenario("6. Abend-Übergang Kaltstart", steps_6, config)
    
    print("\n=== TEST BESTANDEN! Kein Fehler gefunden. ===")

if __name__ == "__main__":
    try:
        asyncio.run(test_scenarios())
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
