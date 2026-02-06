import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from main import check_and_send_alerts

@pytest.mark.asyncio
async def test_check_and_send_alerts_normalization():
    """Verifies that dynamic blocking reasons only trigger a single notification."""
    state = MagicMock()
    state.control.blocking_reason = "Min. Pause (noch 2m 00s)"
    state.control.last_alert_type = ""
    state.config.Telegram.CHAT_ID = "123"
    state.config.Telegram.BOT_TOKEN = "abc"
    
    session = AsyncMock()
    
    with patch('control_logic.send_telegram_message', new_callable=AsyncMock) as mock_send:
        # 1. Initial notification
        await check_and_send_alerts(session, state)
        assert mock_send.call_count == 1
        assert "Min. Pause (noch 2m 00s)" in mock_send.call_args[0][2]
        
        # 2. Same reason, different time -> Should NOT notify
        state.control.blocking_reason = "Min. Pause (noch 1m 45s)"
        await check_and_send_alerts(session, state)
        assert mock_send.call_count == 1
        
        # 3. Different reason -> Should notify
        state.control.blocking_reason = "Verdampfer zu kalt (5.5°C < 6°C)"
        await check_and_send_alerts(session, state)
        assert mock_send.call_count == 2
        assert "Verdampfer zu kalt" in mock_send.call_args[0][2]
        
        # 4. Same reason, different temp -> Should NOT notify
        state.control.blocking_reason = "Verdampfer zu kalt (5.2°C < 6°C)"
        await check_and_send_alerts(session, state)
        assert mock_send.call_count == 2
        
        # 5. Clearance -> No notification (we only notify for blocks)
        state.control.blocking_reason = None
        await check_and_send_alerts(session, state)
        assert mock_send.call_count == 2
        assert state.control.last_alert_type == ""
