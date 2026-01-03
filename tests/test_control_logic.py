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
        "FEEDINPOWER_THRESHOLD": 600.0
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
    
    # Update properties dependent on config
    state.nachtabsenkung_ende = time(6, 0)
    state.uebergangsmodus_morgens_ende = time(10, 0)
    state.uebergangsmodus_abends_start = time(17, 0)
    state.nachtabsenkung_start = time(22, 0)
    
    state.aktueller_ausschaltpunkt = 50
    state.aktueller_einschaltpunkt = 40
    state.basis_ausschaltpunkt = 50
    state.basis_einschaltpunkt = 40
    state.ausschaltpunkt_erhoeht = 55
    state.einschaltpunkt_erhoeht = 45
    
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.solar_ueberschuss_aktiv = False
    state.previous_modus = "Normalmodus"
    
    state.batpower = 0
    state.soc = 50
    state.feedinpower = 0
    
    state.last_solar_window_check = None
    state.last_solar_window_status = False
    
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
    mock_state.batpower = 1000 # > 600 triggers solar excess
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Solarüberschuss"
        assert result['ausschaltpunkt'] == 55 # erhoeht
        assert result['regelfuehler'] == 30 # t_unten

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
    mock_state.batpower = None
    mock_state.soc = None
    mock_state.feedinpower = None
    mock_state.config.Solarueberschuss.BATPOWER_THRESHOLD = 600.0
    mock_state.config.Solarueberschuss.SOC_THRESHOLD = 95.0
    mock_state.config.Solarueberschuss.FEEDINPOWER_THRESHOLD = 600.0
    
    # Mock time to 12:00 (Normalmodus, outside night/transition)
    fixed_time = datetime(2023, 1, 1, 12, 0, 0, tzinfo=mock_state.local_tz)
    
    with patch('control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = fixed_time
        # Re-apply side effects if needed, or just relying on now() is enough for determine_mode
        
        # These should proceed without error
        result = await determine_mode_and_setpoints(mock_state, 40.0, 45.0)
        
        assert mock_state.solar_ueberschuss_aktiv is False
        assert result['modus'] == "Normalmodus"
