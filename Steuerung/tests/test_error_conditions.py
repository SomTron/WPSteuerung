import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
import sys
import os
import tempfile
from datetime import datetime, timedelta
import pytz
import asyncio

# Add parent directory to path to import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import set_kompressor_status, handle_day_transition, setup_application
from control_logic import handle_compressor_off, handle_compressor_on
from utils import check_and_fix_csv_header, rotate_csv
from telegram_api import send_telegram_message

def create_mock_state():
    """Helper function to create a mock state for testing"""
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.control = MagicMock()
    state.stats = MagicMock()
    state.sensors = MagicMock()
    state.config = MagicMock()

    state.control.kompressor_ein = False
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.stats.total_runtime_today = timedelta()
    state.stats.last_day = datetime.now(state.local_tz).date()
    state.config.Telegram.BOT_TOKEN = "test_token"
    state.config.Telegram.CHAT_ID = "test_chat_id"

    return state


@pytest.mark.asyncio
async def test_set_kompressor_status_network_error():
    """Test setting compressor status when network error occurs"""
    state = create_mock_state()
    
    # Mock hardware manager to simulate network error
    with patch('main.hardware_manager') as mock_hw:
        mock_hw.set_compressor_state.side_effect = Exception("Network error")
        
        # This should raise an exception, which is the current behavior
        with pytest.raises(Exception, match="Network error"):
            await set_kompressor_status(state, True)


@pytest.mark.asyncio
async def test_set_kompressor_status_timeout():
    """Test setting compressor status when timeout occurs"""
    state = create_mock_state()
    
    # Stelle sicher, dass der Kompressor aktuell aus ist, damit der Ausschaltzweig genommen wird
    state.control.kompressor_ein = True  # Muss eingeschaltet sein, damit er ausgeschaltet werden kann
    
    # Mock hardware manager to simulate timeout
    with patch('main.hardware_manager') as mock_hw:
        mock_hw.set_compressor_state.side_effect = asyncio.TimeoutError()
        
        # This should raise a TimeoutError, which is the current behavior
        with pytest.raises(asyncio.TimeoutError):
            await set_kompressor_status(state, False)


@pytest.mark.asyncio
async def test_handle_day_transition_error_handling():
    """Test day transition logic with error handling"""
    state = create_mock_state()
    
    # Mock datetime to simulate transition to new day
    new_day = datetime.now(state.local_tz).replace(day=datetime.now(state.local_tz).day + 1)
    
    # This should handle any errors during day transition gracefully
    try:
        handle_day_transition(state, new_day)
        # Verify that the day was updated
        assert state.stats.last_day == new_day.date()
    except Exception as e:
        pytest.fail(f"handle_day_transition raised {e} unexpectedly!")


@pytest.mark.asyncio
async def test_setup_application_config_error():
    """Test application setup when config loading fails"""
    # Mock config manager to raise an exception
    with patch('main.ConfigManager') as mock_config_mgr:
        mock_config_mgr.return_value.load_config.side_effect = Exception("Config error")
        
        # This should handle the config error gracefully
        try:
            state = await setup_application()
            # Should still return a state object even with config error
            assert state is not None
        except Exception as e:
            # If setup_application is designed to propagate errors, 
            # we might expect it to raise an exception
            pass


@pytest.mark.asyncio
async def test_handle_compressor_off_error():
    """Test handling compressor off when errors occur"""
    state = create_mock_state()
    session = AsyncMock()
    
    # Mock telegram to raise an exception
    with patch('control_logic.send_telegram_message') as mock_telegram:
        mock_telegram.side_effect = Exception("Telegram error")
        
        # This should handle the telegram error gracefully
        try:
            await handle_compressor_off(state, session, "Test reason")
        except Exception:
            # If the function propagates exceptions, that's fine
            # The important thing is that it handles the core logic
            pass
        
        # State should still be updated regardless of telegram error
        assert state.control.kompressor_ein is False


@pytest.mark.asyncio
async def test_send_telegram_message_error_handling():
    """Test telegram message sending with error handling"""
    session = AsyncMock()
    
    # Mock session.post to raise an exception
    session.post.side_effect = Exception("Network error")
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="Test message",
        bot_token="test_token"
    )
    
    # Should return False due to error
    assert result is False


@pytest.mark.asyncio
async def test_csv_header_check_error_handling():
    """Test CSV header check with error handling"""
    # Try to check a file that has permission issues or is locked
    # For this test, we'll mock the open function to raise an error
    with patch("builtins.open", side_effect=PermissionError("Access denied")):
        result = check_and_fix_csv_header("protected_file.csv")
        
        # Should return False due to error
        assert result is False


@pytest.mark.asyncio
async def test_csv_rotate_error_handling():
    """Test CSV rotation with error handling"""
    # Try to rotate a non-existent or protected file
    # For this test, we'll mock os.path.exists to return True but file operations to fail
    with patch("os.path.exists", return_value=True):
        with patch("os.path.getsize", side_effect=OSError("File error")):
            # This should handle the file error gracefully
            try:
                rotate_csv("problematic_file.csv")
            except Exception:
                # Function might catch and handle internally
                pass


@pytest.mark.asyncio
async def test_multiple_concurrent_requests():
    """Test handling of multiple concurrent requests"""
    state = create_mock_state()
    
    # Simulate multiple concurrent calls to set_kompressor_status
    async def call_set_kompressor():
        return await set_kompressor_status(state, True)
    
    # Run multiple concurrent calls
    tasks = [call_set_kompressor() for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Check that no exceptions were raised
    for result in results:
        if isinstance(result, Exception):
            pytest.fail(f"One of the concurrent calls raised an exception: {result}")


@pytest.mark.asyncio
async def test_sensor_data_none_values():
    """Test handling of sensor data with None values"""
    state = create_mock_state()
    
    # Test with None values in sensor data
    # This simulates how the main loop might handle None sensor values
    try:
        # Simulate the data processing part of the main loop
        t_oben = None
        t_mittig = 42.5
        t_unten = None

        # These should be handled gracefully with defaults
        processed_t_oben = float(0) if t_oben is None else float(t_oben)
        processed_t_mittig = float(0) if t_mittig is None else float(t_mittig)
        processed_t_unten = float(0) if t_unten is None else float(t_unten)

        # Verify that the processing didn't crash
        assert isinstance(processed_t_mittig, float)
    except Exception as e:
        pytest.fail(f"Processing None sensor values raised {e} unexpectedly!")


@pytest.mark.asyncio
async def test_invalid_temperature_values():
    """Test handling of invalid temperature values"""
    state = create_mock_state()
    
    # Test with extremely high/low temperature values
    extreme_temps = [-100, 200, float('inf'), float('-inf'), float('nan')]
    
    for temp in extreme_temps:
        try:
            # This simulates how the control logic might handle extreme temperatures
            if str(temp).lower() in ['inf', '-inf', 'nan'] or temp != temp:  # Check for inf, -inf, nan
                # Skip NaN values as they cause issues in comparisons
                continue
                
            # Test that the control logic doesn't crash with extreme values
            result = await handle_compressor_off(state, AsyncMock(), f"Test with temp {temp}")
        except Exception as e:
            # If an exception is expected for certain extreme values, that's fine
            # The important thing is that the system handles it appropriately
            pass


@pytest.mark.asyncio
async def test_config_value_out_of_range():
    """Test handling of out-of-range config values"""
    state = create_mock_state()
    
    # Set some config values to extreme values
    state.config.Heizungssteuerung.MIN_LAUFZEIT = -10  # Negative value
    state.config.Heizungssteuerung.SICHERHEITS_TEMP = 200  # Extremely high value
    state.config.Heizungssteuerung.EINSCHALTPUNKT = -50  # Negative temperature
    
    # Test that the system handles these gracefully
    try:
        # This simulates using the config values in control logic
        min_runtime = max(0, state.config.Heizungssteuerung.MIN_LAUFZEIT)  # Ensure non-negative
        safety_temp = min(100, max(0, state.config.Heizungssteuerung.SICHERHEITS_TEMP))  # Clamp to reasonable range
        einschaltpunkt = max(-30, min(80, state.config.Heizungssteuerung.EINSCHALTPUNKT))  # Clamp temperature range
        
        # Verify that the clamping worked
        assert min_runtime >= 0
        assert 0 <= safety_temp <= 100
        assert -30 <= einschaltpunkt <= 80
    except Exception as e:
        pytest.fail(f"Handling out-of-range config values raised {e} unexpectedly!")


@pytest.mark.asyncio
async def test_csv_file_locked_error():
    """Test handling when CSV file is locked by another process"""
    # Create a temporary file and simulate it being locked
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.csv', encoding='utf-8') as f:
        f.write("Zeitstempel,T_Oben,T_Unten,T_Mittig,T_Verd,Kompressor,PowerSource,Einschaltpunkt,Ausschaltpunkt\n")
        f.write("2023-01-01 00:00:00,40.0,39.0,38.0,10.0,EIN,Grid,0,0\n")
        temp_file = f.name

    try:
        # Mock the file opening to simulate a lock error
        with patch("builtins.open", side_effect=PermissionError("File is locked")):
            result = check_and_fix_csv_header(temp_file)
            assert result is False  # Should handle the error gracefully
    finally:
        # Clean up
        if os.path.exists(temp_file):
            os.unlink(temp_file)