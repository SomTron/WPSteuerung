import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from safety_logic import verify_compressor_running

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Sub-states
    state.control = MagicMock()
    state.control.kompressor_ein = True
    
    # Verification fields
    state.kompressor_verification_error_count = 0
    state.kompressor_verification_failed = False
    state.kompressor_verification_last_check = None
    state.kompressor_verification_start_time = None
    state.kompressor_verification_start_t_vorlauf = 0.0
    state.kompressor_verification_start_t_unten = 0.0
    
    # Config/Telegram
    state.config = MagicMock()
    state.config.Telegram.BOT_TOKEN = "mock_token"
    state.config.Telegram.CHAT_ID = "mock_chat_id"
    state.bot_token = state.config.Telegram.BOT_TOKEN
    
    return state

@pytest.mark.asyncio
async def test_verification_delayed_default_20_min(mock_state):
    """
    Test that verification is skipped (returns True) if time < 20 minutes.
    """
    now = datetime(2023, 1, 1, 12, 19, 0, tzinfo=mock_state.local_tz)
    # Start time 19 minutes ago
    start_time = now - timedelta(minutes=19)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_vorlauf = 30.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with patch_datetime(now):
        # Should return True because elapsed time (19m) < default delay (20m)
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_vorlauf=35.0, current_t_unten=30.0
        )
        
        assert is_running is True
        assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_rise(mock_state):
    """
    Test standard success case: significant flow temperature rise.
    """
    now = datetime(2023, 1, 1, 12, 25, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=25)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_vorlauf = 30.0
    mock_state.kompressor_verification_start_t_unten = 40.0
    
    with patch_datetime(now):
        # Rise 2.5 deg (30 -> 32.5) >= 2.0 threshold
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_vorlauf=32.5, current_t_unten=40.3
        )
        
        assert is_running is True
        assert error_msg is None

@pytest.mark.asyncio
async def test_verification_failure_no_rise(mock_state):
    """
    Test failure: no rise in flow temperature.
    """
    now = datetime(2023, 1, 1, 12, 25, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=25)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_vorlauf = 30.0
    mock_state.kompressor_verification_start_t_unten = 40.0
    
    with patch_datetime(now):
        # Only 1 degree rise (30 -> 31) < 2.0 threshold
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_vorlauf=31.0, current_t_unten=40.3
        )
        
        assert is_running is False
        assert "Vorlauf: nur 1.0°C Anstieg" in error_msg

@pytest.mark.asyncio
async def test_verification_failure_no_unten_change(mock_state):
    """
    Test failure: rise in flow ok, but no change in unten sensor.
    """
    now = datetime(2023, 1, 1, 12, 25, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=25)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_vorlauf = 30.0
    mock_state.kompressor_verification_start_t_unten = 40.0
    
    with patch_datetime(now):
        # Rise 3.0 deg ok, but unten change 0.1 < 0.2
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_vorlauf=33.0, current_t_unten=40.1
        )
        
        assert is_running is False
        assert "Unterer Fühler: nur 0.1°C Änderung" in error_msg

def patch_datetime(target_now):
    from unittest.mock import patch
    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return target_now
    return patch("safety_logic.datetime", MockDateTime)
