
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from control_logic import verify_compressor_running

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.kompressor_ein = True
    state.bot_token = "mock_token"
    state.chat_id = "mock_chat_id"
    state.kompressor_verification_error_count = 0
    state.kompressor_verification_failed = False
    state.kompressor_verification_last_check = None
    return state

@pytest.mark.asyncio
async def test_verification_delayed(mock_state):
    """
    Test that verification is skipped (returns True) if time < 5 minutes.
    """
    now = datetime(2023, 1, 1, 12, 10, 0, tzinfo=mock_state.local_tz)
    # Start time 4 minutes ago
    start_time = now - timedelta(minutes=4)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 10.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("control_logic.datetime", MockDateTime)
        
        # Should return True because elapsed time (4m) < delay (5m)
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=8.0, current_t_unten=30.0
        )
        
        assert is_running is True
        assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_relaxed(mock_state):
    """
    Test that verification PASSES with small temp rise (0.3 deg) after 6 minutes.
    """
    now = datetime(2023, 1, 1, 12, 10, 0, tzinfo=mock_state.local_tz)
    # Start time 6 minutes ago
    start_time = now - timedelta(minutes=6)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 10.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
         m.setattr("control_logic.datetime", MockDateTime)
         
         # Temp rise 0.3 deg. Threshold is now 0.2. Should pass.
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=8.0, current_t_unten=30.3
        )
         
         assert is_running is True
         assert error_msg is None

@pytest.mark.asyncio
async def test_verification_failure_still_works(mock_state):
    """
    Verify that it STILL fails if change is very small (0.0 deg).
    """
    now = datetime(2023, 1, 1, 12, 10, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=6)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 10.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
             @classmethod
             def now(cls, tz=None):
                 return now
         m.setattr("control_logic.datetime", MockDateTime)

         # No change in T_Unten (30.0 -> 30.0)
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=9.0, current_t_unten=30.0
        )
         
         assert is_running is False
         assert "Unterer Fühler: nur 0.0°C Änderung" in error_msg
