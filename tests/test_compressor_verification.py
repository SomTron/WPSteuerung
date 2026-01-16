
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
async def test_verification_delayed_default_10_min(mock_state):
    """
    Test that verification is skipped (returns True) if time < 10 minutes (new default).
    """
    now = datetime(2023, 1, 1, 12, 10, 0, tzinfo=mock_state.local_tz)
    # Start time 9 minutes ago
    start_time = now - timedelta(minutes=9)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 10.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
        class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        m.setattr("control_logic.datetime", MockDateTime)
        
        # Should return True because elapsed time (9m) < default delay (10m)
        is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=8.0, current_t_unten=30.0
        )
        
        assert is_running is True
        assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_cold_start(mock_state):
    """
    Test "Cold Start / Restart" scenario.
    Start T_Verd is low (< 15), Current T_Verd is low (< 12), and did not rise significantly.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    # Start time 15 minutes ago
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 5.0  # Cold start
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
         m.setattr("control_logic.datetime", MockDateTime)
         
         # Scenario:
         # Start Verd: 5.0
         # Current Verd: 5.2 (slightly warmer, delta = -0.2)
         # Start Unten: 30.0
         # Current Unten: 30.5 (delta = 0.5 > 0.2 threshold)
         
         # Normal verd_ok check (delta >= 1.5) fails because -0.2 < 1.5
         # But Cold Start check should pass because:
         # Start < 15 (5.0) -> True
         # Delta >= -0.5 (-0.2) -> True
         # Current < 12 (5.2) -> True
         
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=5.2, current_t_unten=30.5
        )
         
         assert is_running is True
         assert error_msg is None

@pytest.mark.asyncio
async def test_verification_success_normal_drop(mock_state):
    """
    Test standard success case: significant temperature drop.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 20.0
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
         m.setattr("control_logic.datetime", MockDateTime)
         
         # Drop 2.0 deg (20 -> 18) > 1.5 threshold
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=18.0, current_t_unten=30.3
        )
         
         assert is_running is True
         assert error_msg is None

@pytest.mark.asyncio
async def test_verification_failure(mock_state):
    """
    Test failure: not cold enough, no drop.
    """
    now = datetime(2023, 1, 1, 12, 15, 0, tzinfo=mock_state.local_tz)
    start_time = now - timedelta(minutes=15)
    
    mock_state.kompressor_verification_start_time = start_time
    mock_state.kompressor_verification_start_t_verd = 20.0 # Warm start
    mock_state.kompressor_verification_start_t_unten = 30.0
    
    with pytest.MonkeyPatch.context() as m:
         class MockDateTime(datetime):
             @classmethod
             def now(cls, tz=None):
                 return now
         m.setattr("control_logic.datetime", MockDateTime)

         # No drop (20 -> 20), not cold start (< 15)
         is_running, error_msg = await verify_compressor_running(
            mock_state, None, current_t_verd=20.0, current_t_unten=30.3
        )
         
         assert is_running is False
         assert "Verdampfer: nur 0.0°C Abfall" in error_msg
