import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timedelta
import pytz
from main import handle_day_transition

@pytest.fixture
def state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.stats.last_day = datetime.now(state.local_tz).date()
    state.stats.total_runtime_today = timedelta(hours=2)
    return state

def test_handle_day_transition_same_day(state):
    """Verify that stats are NOT reset on the same day."""
    now = datetime.now(state.local_tz)
    initial_runtime = state.stats.total_runtime_today
    
    handle_day_transition(state, now)
    
    assert state.stats.total_runtime_today == initial_runtime
    state.stats.total_runtime_today = initial_runtime # Ensure no accidental changes

def test_handle_day_transition_new_day(state):
    """Verify that stats ARE reset on a new day."""
    # Set last_day to yesterday
    state.stats.last_day = (datetime.now(state.local_tz) - timedelta(days=1)).date()
    now = datetime.now(state.local_tz)
    
    handle_day_transition(state, now)
    
    assert state.stats.total_runtime_today == timedelta()
    assert state.stats.last_day == now.date()

@pytest.mark.asyncio
async def test_update_system_data_mapping():
    """Verify that update_system_data correctly maps sensor values to the state."""
    from main import update_system_data
    
    state = MagicMock()
    session = MagicMock()
    
    # Mocking sensor_manager
    mock_sensor_manager = MagicMock()
    mock_sensor_manager.get_all_temperatures = AsyncMock(return_value={
        "oben": 50.5, "mittig": 45.0, "unten": 40.0, "verd": -5.0
    })
    
    with patch('main.sensor_manager', mock_sensor_manager), \
         patch('main.get_solax_data', return_value=None):
        
        await update_system_data(session, state)
        
        assert state.sensors.t_oben == 50.5
        assert state.sensors.t_mittig == 45.0
        assert state.sensors.t_unten == 40.0
        assert state.sensors.t_verd == -5.0
