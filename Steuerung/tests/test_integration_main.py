import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os
import asyncio
from datetime import datetime, timedelta
import pytz

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import main_loop

@pytest.mark.asyncio
async def test_main_loop_integration():
    """
    Test one iteration of the main loop to ensure components work together.
    """
    with patch('main.setup_application', new_callable=AsyncMock) as mock_setup, \
         patch('main.update_system_data', new_callable=AsyncMock) as mock_update, \
         patch('main.check_periodic_tasks', new_callable=AsyncMock) as mock_periodic, \
         patch('main.log_system_state', new_callable=AsyncMock) as mock_logging, \
         patch('main.stop_event') as mock_stop_event, \
         patch('main.asyncio.sleep', new_callable=AsyncMock) as mock_sleep, \
         patch('control_logic.check_sensors_and_safety', new_callable=AsyncMock) as mock_safety, \
         patch('control_logic.determine_mode_and_setpoints', new_callable=AsyncMock) as mock_dims, \
         patch('control_logic.handle_compressor_off', new_callable=AsyncMock) as mock_off, \
         patch('control_logic.handle_compressor_on', new_callable=AsyncMock) as mock_on, \
         patch('control_logic.handle_mode_switch', new_callable=AsyncMock) as mock_switch, \
         patch('main.check_and_send_alerts', new_callable=AsyncMock):
        
        # Setup mock state
        mock_state = MagicMock()
        mock_state.local_tz = pytz.timezone("Europe/Berlin")
        mock_state.bot_token = "mock_token"
        mock_state.chat_id = "mock_chat_id"
        mock_state.control.kompressor_ein = False
        mock_state.stats.last_compressor_on_time = None
        mock_state.min_laufzeit = 10
        mock_state.min_pause = 10
        mock_state.sensors.t_oben = 40.0
        mock_state.sensors.t_unten = 30.0
        mock_state.sensors.t_mittig = 35.0
        mock_state.sensors.t_verd = 5.0
        mock_state.sensors.t_vorlauf = 35.0
        mock_state.set_kompressor_status = AsyncMock()
        mock_state._last_config_check = datetime.now(mock_state.local_tz) - timedelta(minutes=10)
        mock_state.last_forecast_update = datetime.now(mock_state.local_tz) - timedelta(hours=10)
        mock_state.update_config = MagicMock()
        mock_state.kompressor_verification_error_count = 0
        mock_state.control.last_pressure_state = True
        
        mock_setup.return_value = AsyncMock() # mock_session
        mock_dims.return_value = {
            "einschaltpunkt": 25.0, 
            "ausschaltpunkt": 45.0, 
            "solar_ueberschuss_aktiv": False,
            "regelfuehler": 30.0
        }
        mock_safety.return_value = True
        
        # Mock handle_pressure_check_func in main
        with patch('main.handle_pressure_check', new_callable=AsyncMock) as mock_p:
            mock_p.return_value = True
            
            # Define loop behavior: run once then stop
            # First call to is_set returns False, second returns True
            mock_stop_event.is_set.side_effect = [False, True]
            
            # Mock check_periodic_tasks to return a datetime
            mock_periodic.return_value = datetime.now()
            
            # Use a global-like state mock because main.py uses a 'state' variable
            import main
            main.state = mock_state
            
            # Run the loop
            await main_loop()
            
        # Verify the flow
        mock_setup.assert_called_once()
        mock_update.assert_called_once()
        mock_periodic.assert_called_once()
        # mock_logic NO LONGER CALLED as we test the real run_logic_step
        mock_logging.assert_called_once()
        mock_sleep.assert_called_once_with(10)
        
        # Verify sub-calls of real run_logic_step
        mock_safety.assert_called_once()
        mock_off.assert_called_once()
        mock_on.assert_called_once()
        mock_switch.assert_called_once()

@pytest.mark.asyncio
async def test_main_loop_error_handling_integration():
    """
    Test that main loop handles errors gracefully and pings healthcheck/telegram.
    """
    with patch('main.setup_application', side_effect=Exception("Critical Failure")), \
         patch('main.logging.critical') as mock_log_critical, \
         patch('main.control_logic.send_telegram_message', new_callable=AsyncMock) as mock_tg:
        
        # Setup mock state
        mock_state = MagicMock()
        mock_state.bot_token = "token"
        mock_state.chat_id = "id"
        mock_state.healthcheck_url = None # Avoid healthcheck call to simplify
        
        with patch('main.state', mock_state):
            await main_loop()
            
            # Verify critical error was logged
            mock_log_critical.assert_called()
            # Verify telegram message was attempted
            mock_tg.assert_called()
