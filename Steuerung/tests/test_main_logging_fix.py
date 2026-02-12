import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os
import pytz
from datetime import datetime, timedelta

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import log_system_state

@pytest.mark.asyncio
async def test_log_system_state_handles_none_sensors():
    """Verify that log_system_state does not crash when sensors are None."""
    
    # Mock state
    state = MagicMock()
    state.sensors.t_oben = None
    state.sensors.t_unten = 25.5
    state.sensors.t_mittig = None
    state.sensors.t_verd = None
    state.sensors.t_boiler = 45.0
    
    state.control.aktueller_einschaltpunkt = 40.0
    state.control.aktueller_ausschaltpunkt = 50.0
    state.control.kompressor_ein = False
    state.control.previous_modus = "Normalmodus"
    state.control.solar_ueberschuss_aktiv = False
    
    state.solar.soc = 50.0
    state.solar.feedinpower = 0.0
    state.solar.batpower = 0.0
    state.solar.last_api_data = {}
    
    # Mock hardware_manager
    mock_hw = MagicMock()
    
    # Mock aiofiles
    mock_file = AsyncMock()
    
    with patch('main.hardware_manager', mock_hw), \
         patch('aiofiles.open', return_value=mock_file), \
         patch('main.HEIZUNGSDATEN_CSV', 'mock.csv'), \
         patch('os.path.exists', return_value=True):
        
        # This should NOT raise ValueError
        await log_system_state(state)
        
        # Verify LCD was written
        mock_hw.write_lcd.assert_called()
        args = mock_hw.write_lcd.call_args[0]
        assert "Oben:Err" in args[0]
        assert "Unt:25.5" in args[0]
        assert "Mit :Err" in args[1]
        assert "Verd:Err" in args[1]
