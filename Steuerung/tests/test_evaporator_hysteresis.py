import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timedelta
import pytz
from safety_logic import check_sensors_and_safety

class MockState:
    def __init__(self):
        self.local_tz = pytz.timezone("Europe/Berlin")
        
        # Sub-states
        self.sensors = MagicMock()
        self.control = MagicMock()
        
        # Initial values
        self.verdampfer_blocked = False
        self.control.kompressor_ein = False
        self.control.ausschluss_grund = None
        
        # Config
        self.config = MagicMock()
        self.config.Heizungssteuerung.VERDAMPFERTEMPERATUR = 6.0
        self.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP = 9.0
        self.config.Heizungssteuerung.SICHERHEITS_TEMP = 60.0
        self.config.Telegram.BOT_TOKEN = "mock"
        self.config.Telegram.CHAT_ID = "mock"
        
        # Mocking properties seen in safety_logic
        self.bot_token = self.config.Telegram.BOT_TOKEN
    
    @property
    def ausschluss_grund(self):
        return self.control.ausschluss_grund
    @ausschluss_grund.setter
    def ausschluss_grund(self, val):
        self.control.ausschluss_grund = val

@pytest.mark.asyncio
async def test_evaporator_hysteresis():
    # Setup mock state
    state = MockState()
    
    mock_set_status = AsyncMock(return_value=True)
    session = MagicMock()

    # Case 1: Normal temperature (10°C) -> Status OK
    t_verd = 10.0
    result = await check_sensors_and_safety(session, state, 40.0, 35.0, 38.0, t_verd, mock_set_status)
    assert result is True
    assert state.verdampfer_blocked is False

    # Case 2: Temperature drops below limit (5°C < 6°C) -> Blocked
    t_verd = 5.0
    result = await check_sensors_and_safety(session, state, 40.0, 35.0, 38.0, t_verd, mock_set_status)
    assert result is False
    assert state.verdampfer_blocked is True
    assert "niedrig" in state.ausschluss_grund

    # Case 3: Temperature recovers slightly (7°C), but still below restart threshold (9°C) -> Still blocked
    t_verd = 7.0
    result = await check_sensors_and_safety(session, state, 40.0, 35.0, 38.0, t_verd, mock_set_status)
    assert result is False
    assert state.verdampfer_blocked is True
    assert "Warten auf Erwärmung" in state.ausschluss_grund

    # Case 4: Temperature reaches restart threshold (9°C) -> Block removed
    t_verd = 9.0
    result = await check_sensors_and_safety(session, state, 40.0, 35.0, 38.0, t_verd, mock_set_status)
    assert result is True
    assert state.verdampfer_blocked is False
