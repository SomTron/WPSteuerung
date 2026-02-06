import pytest
from state import State
from unittest.mock import MagicMock
from datetime import datetime
import pytz

class MockConfigManager:
    def __init__(self, config):
        self.config = config
    def get(self):
        return self.config
    def load_config(self):
        pass

@pytest.fixture
def state():
    mock_config = MagicMock()
    # Setup some basic config values
    mock_config.Heizungssteuerung.AUSSCHALTPUNKT = 50
    mock_config.Heizungssteuerung.EINSCHALTPUNKT = 40
    mock_config.Heizungssteuerung.SICHERHEITS_TEMP = 60
    mock_config.Heizungssteuerung.VERDAMPFERTEMPERATUR = -10
    mock_config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP = 9
    mock_config.Heizungssteuerung.MIN_LAUFZEIT = 15
    mock_config.Heizungssteuerung.MIN_PAUSE = 20
    mock_config.Telegram.BOT_TOKEN = "token"
    mock_config.Telegram.CHAT_ID = "chat"
    
    manager = MockConfigManager(mock_config)
    return State(manager)

def test_state_initialization(state):
    """Verify that State initializes with sub-objects."""
    assert state.sensors is not None
    assert state.solar is not None
    assert state.control is not None
    assert state.stats is not None

def test_sensors_state(state):
    """Verify sensors sub-state."""
    state.sensors.t_oben = 45.5
    assert state.sensors.t_oben == 45.5
    assert state.sensors.t_unten is None

def test_solar_state(state):
    """Verify solar sub-state."""
    state.solar.soc = 85
    assert state.solar.soc == 85
    assert state.solar.acpower is None

def test_control_state(state):
    """Verify control sub-state."""
    assert state.control.aktueller_ausschaltpunkt == 50
    state.control.kompressor_ein = True
    assert state.control.kompressor_ein is True

def test_stats_state(state):
    """Verify stats sub-state."""
    assert state.stats.total_runtime_today.total_seconds() == 0
    assert isinstance(state.stats.last_day, datetime.now().date().__class__)

def test_config_properties(state):
    """Verify properties that pull from config."""
    assert state.sicherheits_temp == 60
    assert state.bot_token == "token"
