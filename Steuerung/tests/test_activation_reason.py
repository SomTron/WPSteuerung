import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os
from datetime import datetime

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

@pytest.mark.asyncio
async def test_activation_reason_tracking():
    """Verify that activation_reason is correctly set and cleared in main.py."""
    from main import set_kompressor_status
    
    state = MagicMock()
    state.local_tz = None # Not needed for mock
    state.control.kompressor_ein = False
    state.control.previous_modus = "Solarüberschuss"
    state.control.activation_reason = None
    
    # Mock hardware
    with patch('main.hardware_manager') as mock_hw:
        # 1. Switch ON
        await set_kompressor_status(state, True)
        assert state.control.kompressor_ein is True
        assert state.control.activation_reason == "Solarüberschuss"
        
        # 2. Switch OFF
        await set_kompressor_status(state, False)
        assert state.control.kompressor_ein is False
        assert state.control.activation_reason is None

@pytest.mark.asyncio
async def test_status_message_includes_reason():
    """Verify that the status message in telegram_handler includes the activation reason."""
    from telegram_handler import compose_status_message
    
    state = MagicMock()
    state.control.active_rule_sensor = "Mittig"
    state.control.aktueller_einschaltpunkt = 40.0
    state.control.aktueller_ausschaltpunkt = 50.0
    state.control.blocking_reason = None
    state.control.activation_reason = "Solarüberschuss"
    state.battery_capacity = 0
    
    msg = compose_status_message(
        45.0, 40.0, 42.0, 5.0, 
        True, # kompressor_status
        None, None, # runtimes
        "Solarüberschuss", "127.0.0.1", "Forecast", 
        {"feedinpower": 100, "batPower": 0, "soc": 50, "acpower": 2000},
        state
    )
    
    assert "💡 Grund: Solarüberschuss" in msg
