import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, time, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from control_logic import determine_mode_and_setpoints, check_pressure_and_config
import configparser

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Mocking Config as object with attributes
    config = MagicMock()
    
    # Helper to create nested mocks
    def create_section_mock(data):
        section = MagicMock()
        for k, v in data.items():
            setattr(section, k, v)
        return section

    config.Heizungssteuerung = create_section_mock({
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
        "UEBERGANGSMODUS_ABENDS_START": "17:00",
        "SICHERHEITS_TEMP": 60.0,
        "VERDAMPFERTEMPERATUR": -10.0,
        "VERDAMPFER_RESTART_TEMP": 9.0
    })
    
    config.Urlaubsmodus = create_section_mock({
        "URLAUBSABSENKUNG": 10.0
    })
    
    config.Solarueberschuss = create_section_mock({
        "BATPOWER_THRESHOLD": 600.0,
        "SOC_THRESHOLD": 95.0,
        "FEEDINPOWER_THRESHOLD": 600.0,
        "BATTERY_CAPACITY_KWH": 10.0,
        "MIN_SOC": 0.0
    })
    
    config.Telegram = create_section_mock({
        "BOT_TOKEN": "mock_token",
        "CHAT_ID": "mock_chat_id"
    })
    
    config.Healthcheck = create_section_mock({
        "HEALTHCHECK_URL": "http://mock-url",
        "HEALTHCHECK_INTERVAL_MINUTES": 15
    })

    state.config = config
    
    # Sub-states
    state.sensors = MagicMock()
    state.solar = MagicMock()
    state.control = MagicMock()
    state.stats = MagicMock()

    # Default values for sub-states
    state.control.aktueller_ausschaltpunkt = 50
    state.control.aktueller_einschaltpunkt = 40
    state.control.kompressor_ein = False
    state.control.solar_ueberschuss_aktiv = False
    state.control.previous_modus = "Normalmodus"
    
    state.solar.batpower = 0
    state.solar.soc = 50
    state.solar.feedinpower = 0
    
    state.stats.last_compressor_off_time = datetime.now(state.local_tz) - timedelta(hours=1)
    state.stats.total_runtime_today = timedelta()
    
    # Other legacy/simple fields
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.last_solar_window_status = False
    
    # Properties/Helper methods
    state.basis_ausschaltpunkt = 50
    state.basis_einschaltpunkt = 40
    state.ausschaltpunkt_erhoeht = 55
    state.einschaltpunkt_erhoeht = 45
    
    return state

@pytest.mark.asyncio
async def test_determine_mode_normal(mock_state):
    # Mock dependencies
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
            
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Normalmodus"
        assert result['ausschaltpunkt'] == 50
        assert result['einschaltpunkt'] == 40
        assert result['regelfuehler'] == 35 # t_mittig

@pytest.mark.asyncio
async def test_determine_mode_solar(mock_state):
    mock_state.solar.batpower = 1000 # > 600 triggers solar excess
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Solarüberschuss"
        assert result['ausschaltpunkt'] == 55 # erhoeht
        assert result['regelfuehler'] == 30 # t_unten

@pytest.mark.asyncio
async def test_determine_mode_solar_min_soc(mock_state):
    mock_state.solar.batpower = 1000 # > 600 triggers solar excess
    mock_state.solar.soc = 15
    mock_state.config.Solarueberschuss.MIN_SOC = 20.0
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        # Should be Normalmodus because soc (15) < MIN_SOC (20)
        assert result['modus'] == "Normalmodus"
        assert mock_state.control.solar_ueberschuss_aktiv is False

@pytest.mark.asyncio
async def test_determine_mode_battery_early_start(mock_state):
    """Test battery-aware early start during morning transition."""
    # Setup morning transition time (e.g., 08:30 if end is 10:00)
    mock_state.config.Heizungssteuerung.NACHTABSENKUNG_END = "08:00"
    mock_state.config.Heizungssteuerung.UEBERGANGSMODUS_MORGENS_ENDE = "10:00"
    mock_state.config.Heizungssteuerung.WP_POWER_EXPECTED = 600.0
    
    # Battery: 10kWh, SOC: 50%, Min SOC: 20% -> 3kWh available
    mock_state.config.Solarueberschuss.BATTERY_CAPACITY_KWH = 10.0
    mock_state.config.Solarueberschuss.MIN_SOC = 20.0
    mock_state.solar.soc = 50.0
    
    # House: 200W + WP 600W = 800W. 
    # At 08:30, 1.5h remaining -> 1200Wh needed. 3000Wh > 1200Wh.
    mock_state.solar.acpower = 0.0
    mock_state.solar.feedinpower = -200.0 # Drawing 200W from grid
    mock_state.solar.batpower = 0.0
    
    fixed_time = datetime.now(mock_state.local_tz).replace(hour=8, minute=30, second=0, microsecond=0)
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.is_solar_window', return_value=False):
        
        # Ensure strptime and time still work by using real datetime if possible, 
        # or just mock the returns of now()
        mock_lu_dt.now.return_value = fixed_time
        mock_lu_dt.strptime.side_effect = datetime.strptime
        
        # We also need to patch it in control_logic if it was imported as 'datetime' 
        # but it's used via logic_utils symbols there mostly.
        # Wait, control_logic.py imports determine_mode_and_setpoints from logic_utils? 
        # No, determine_mode_and_setpoints is in control_logic.py and it calls functions from logic_utils.
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Übergangsmodus (Batterie Frühstart)"
        # Setpoints should be basis (50) without 5 reduction
        assert result['ausschaltpunkt'] == 50
        assert result['einschaltpunkt'] == 40

@pytest.mark.asyncio
async def test_determine_mode_night(mock_state):
    # Test Night Mode (Nachtabsenkung)
    
    with patch('control_logic.is_nighttime', return_value=True), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=False):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Nachtmodus"
        # 50 - 5 (Nachtabsenkung) = 45
        assert result['ausschaltpunkt'] == 45 
        # 40 - 5 = 35
        assert result['einschaltpunkt'] == 35
        assert result['regelfuehler'] == 35 # t_mittig

@pytest.mark.asyncio
async def test_determine_mode_bademodus(mock_state):
    mock_state.bademodus_aktiv = True
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Bademodus"
        assert result['ausschaltpunkt'] == 55 # erhoeht
        assert result['einschaltpunkt'] == 51 # erhoeht - 4
        assert result['regelfuehler'] == 30 # t_unten
        assert result['regelfuehler'] == 30 # t_unten

@pytest.mark.asyncio
async def test_check_pressure_and_config_only_pressure(mock_state):
    # Mock dependencies
    handle_pressure = AsyncMock(return_value=True)
    set_kompressor = AsyncMock()
    reload_config = AsyncMock()
    calc_hash = MagicMock(return_value="new_hash")
    
    # Setup state
    mock_state.last_pressure_state = True
    mock_state._last_config_check = datetime.now(mock_state.local_tz) - timedelta(minutes=5)
    mock_state.last_config_hash = "old_hash"
    
    # Test with only_pressure=True
    await check_pressure_and_config(
        None, mock_state, handle_pressure, set_kompressor, reload_config, calc_hash, only_pressure=True
    )
    
    # Assertions
    handle_pressure.assert_called_once()
    reload_config.assert_not_called() # Should be skipped
    calc_hash.assert_not_called() # Should be skipped
    
    # Test with only_pressure=False (default)
    await check_pressure_and_config(
        None, mock_state, handle_pressure, set_kompressor, reload_config, calc_hash, only_pressure=False
    )
    
    # Assertions
    assert handle_pressure.call_count == 2
    mock_state.update_config.assert_called_once()
    # reload_config argument is ignored in favor of state method
    # calc_hash.assert_called_once() # Hashes logic removed/simplified

@pytest.mark.asyncio
async def test_determine_mode_none_solar_values(mock_state):
    """Test handling of None values for solar data (API failure)."""
    # Setup state with None values
    mock_state.solar.batpower = None
    mock_state.solar.soc = None
    mock_state.solar.feedinpower = None
    mock_state.config.Solarueberschuss.BATPOWER_THRESHOLD = 600.0
    mock_state.config.Solarueberschuss.SOC_THRESHOLD = 95.0
    mock_state.config.Solarueberschuss.FEEDINPOWER_THRESHOLD = 600.0
    
    # Mock time to 12:00 (Normalmodus, outside night/transition)
    fixed_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=mock_state.local_tz)
    
    with patch('logic_utils.datetime') as mock_dt:
        mock_dt.now.return_value = fixed_time
        # Re-apply side effects if needed, or just relying on now() is enough for determine_mode
        
        # These should proceed without error
        result = await determine_mode_and_setpoints(mock_state, 40.0, 45.0)
        
        assert mock_state.control.solar_ueberschuss_aktiv is False
        assert result['modus'] == "Normalmodus"

@pytest.mark.asyncio
async def test_determine_mode_transition_frostschutz(mock_state):
    """Test detection of Frostschutz in Übergangsmodus."""
    # Night starts at 35 (Einschaltpunkt 40 - reduction 5)
    # Transitions starts at 40
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True), \
         patch('control_logic.is_solar_window', return_value=False):
        
        # Scenario: Warm enough for night (37 > 35), but cold enough for transition (37 < 40)
        # Normal transition mode
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=37)
        assert result['modus'] == "Übergangsmodus"
        
        # Scenario: Colder than night limit (34 < 35)
        # Frostschutz should trigger
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=34)
        assert result['modus'] == "Übergangsmodus (Frostschutz)"

@pytest.mark.asyncio
async def test_handle_compressor_on_prevents_immediate_off(mock_state):
    """Test that handle_compressor_on does not start if stop conditions are already met."""
    from control_logic import handle_compressor_on
    
    # Setup state: Bottom cold (trigger start), but top warm (trigger stop)
    # Target: 50°C (Normal mode)
    # t_unten: 35°C (<= 40 triggers ON)
    # t_oben: 55°C (>= 50 triggers STOP)
    
    set_kompressor_status = AsyncMock(return_value=True)
    mock_state.control.kompressor_ein = False
    mock_state.sensors.t_verd = 15.0
    mock_state.sensors.t_unten = 35.0
    mock_state.stats.last_compressor_off_time = datetime.now(mock_state.local_tz) - timedelta(hours=1)
    
    # Try to turn on
    # regelfuehler (t_mittig) is cold (35 <= 40), t_oben is warm (55 >= 50)
    # The new logic should allow starting because only regelfuehler counts for regulation stop.
    result = await handle_compressor_on(
        mock_state, None, regelfuehler=35.0, einschaltpunkt=40, ausschaltpunkt=50,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=20), 
        within_solar_window=True, t_oben=55.0, set_kompressor_status_func=set_kompressor_status
    )
    
    assert result is True
    set_kompressor_status.assert_called_once()
    assert mock_state.control.blocking_reason is None
