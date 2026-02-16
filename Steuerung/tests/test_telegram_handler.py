import pytest
from unittest.mock import MagicMock, patch
from telegram_handler import compose_status_message
from datetime import datetime
import pytz

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Vienna")
    state.config.Heizungssteuerung.WP_POWER_EXPECTED = 600.0
    state.battery_capacity = 10.0
    state.control.blocking_reason = "Test Reason"
    state.control.aktueller_einschaltpunkt = 40.0
    state.control.aktueller_ausschaltpunkt = 45.0
    state.control.solar_ueberschuss_aktiv = False
    state.control.active_rule_sensor = "Mittig"
    return state

@pytest.mark.asyncio
async def test_compose_status_message(mock_state):
    """Testet die Generierung der Telegram-Statusnachricht."""
    solax_data = {
        "acpower": 2000,
        "feedinpower": 500,
        "batPower": -100,
        "soc": 80
    }
    
    # Mocking external values
    t_oben, t_mittig, t_unten, t_verd, t_vorlauf = 50.0, 45.0, 40.0, 10.0, 35.0
    kompressor_status = True
    current_runtime = MagicMock()
    total_runtime = MagicMock()
    mode_str = "Test Modus"
    vpn_ip = "10.0.0.1"
    forecast_text = "Sonnig"
    
    with patch('telegram_handler.datetime') as mock_dt:
        mock_dt.now.return_value = datetime(2026, 2, 11, 12, 0, 0)
        
        message = compose_status_message(
            t_oben, t_unten, t_mittig, t_verd, t_vorlauf,
            kompressor_status, current_runtime, total_runtime,
            mode_str, vpn_ip, forecast_text,
            solax_data, mock_state
        )
        
        # Basis-Checks
        assert "SYSTEMSTATUS" in message
        assert "Akku: 8.0 kWh" in message  # 80% von 10kWh
        assert "SOC: 80%" in message
        assert "Modus: Test Modus" in message
        assert "10.0.0.1" in message
        assert "Vorlauf: 35.0" in message
