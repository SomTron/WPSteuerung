import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio
from contextlib import asynccontextmanager
import sys
import os

# Add parent directory to path to import telegram_api
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from telegram_api import send_telegram_message, get_telegram_updates, create_robust_aiohttp_session


def create_async_context_manager_mock(return_value):
    """Helper function to create a proper async context manager mock."""
    mock_cm = AsyncMock()
    mock_cm.__aenter__.return_value = return_value
    mock_cm.__aexit__.return_value = None
    return mock_cm

@pytest.mark.asyncio
async def test_create_robust_aiohttp_session():
    """Test creating a robust aiohttp session"""
    session = create_robust_aiohttp_session()
    assert session is not None
    await session.close()


@pytest.mark.asyncio
async def test_send_telegram_message_success():
    """Test successful sending of a Telegram message"""
    # For this test, we'll mock the actual post request to avoid complex async mocking
    with patch('telegram_api.create_robust_aiohttp_session') as mock_create_session:
        # Create mock objects
        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.text = AsyncMock(return_value="OK")
        
        # Set up the session to return our mock response when post is called
        session_post_mock = MagicMock()
        session_post_mock.return_value.__aenter__.return_value = mock_response
        session_post_mock.return_value.__aexit__.return_value = None
        mock_session.post = session_post_mock
        
        mock_create_session.return_value.__aenter__.return_value = mock_session
        mock_create_session.return_value.__aexit__.return_value = None
        
        result = await send_telegram_message(
            session=None,  # This will trigger creation of a new session
            chat_id="123456",
            message="Test message",
            bot_token="test_bot_token"
        )
        
        assert result is True


@pytest.mark.asyncio
async def test_send_telegram_message_failure():
    """Test failure when sending a Telegram message"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 400
    response.text.return_value = "Bad Request"
    response.text.return_value = "Bad Request"
    
    session_post_mock = MagicMock()
    session_post_mock.return_value.__aenter__.return_value = response
    session_post_mock.return_value.__aexit__.return_value = None
    session.post = session_post_mock
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="Test message",
        bot_token="test_bot_token"
    )
    
    assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_missing_token():
    """Test sending a Telegram message with missing token"""
    result = await send_telegram_message(
        session=None,
        chat_id="123456",
        message="Test message",
        bot_token=""
    )
    
    assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_missing_chat_id():
    """Test sending a Telegram message with missing chat ID"""
    result = await send_telegram_message(
        session=None,
        chat_id="",
        message="Test message",
        bot_token="test_bot_token"
    )
    
    assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_long_message():
    """Test sending a very long Telegram message (should be truncated)"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.status = 200
    
    session_post_mock = MagicMock()
    session_post_mock.return_value.__aenter__.return_value = response
    session_post_mock.return_value.__aexit__.return_value = None
    session.post = session_post_mock
    
    long_message = "A" * 5000  # Much longer than 4096 character limit
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message=long_message,
        bot_token="test_bot_token"
    )
    
    assert result is True
    # Verify the message was truncated
    args, kwargs = session.post.call_args
    payload = kwargs['json']
    assert len(payload['text']) <= 4096


@pytest.mark.asyncio
async def test_send_telegram_message_with_reply_markup():
    """Test sending a Telegram message with reply markup"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.status = 200
    
    session_post_mock = MagicMock()
    session_post_mock.return_value.__aenter__.return_value = response
    session_post_mock.return_value.__aexit__.return_value = None
    session.post = session_post_mock
    
    reply_markup = {"inline_keyboard": [[{"text": "Button", "callback_data": "btn1"}]]}
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="Test message with buttons",
        bot_token="test_bot_token",
        reply_markup=reply_markup
    )
    
    assert result is True
    args, kwargs = session.post.call_args
    payload = kwargs['json']
    assert 'reply_markup' in payload


@pytest.mark.asyncio
async def test_send_telegram_message_with_parse_mode():
    """Test sending a Telegram message with parse mode"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.status = 200
    
    session_post_mock = MagicMock()
    session_post_mock.return_value.__aenter__.return_value = response
    session_post_mock.return_value.__aexit__.return_value = None
    session.post = session_post_mock
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="*Bold text*",
        bot_token="test_bot_token",
        parse_mode="Markdown"
    )
    
    assert result is True
    args, kwargs = session.post.call_args
    payload = kwargs['json']
    assert payload['parse_mode'] == "Markdown"


@pytest.mark.asyncio
async def test_send_telegram_message_network_error():
    """Test handling network errors when sending a Telegram message"""
    session = AsyncMock()
    # Correctly mock the async context manager raising an exception
    session_post_mock = MagicMock()
    # If the side effect is on the post() call itself:
    session_post_mock.side_effect = Exception("Network error")
    session.post = session_post_mock
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="Test message",
        bot_token="test_bot_token"
    )
    
    assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_timeout():
    """Test handling timeout when sending a Telegram message"""
    session = AsyncMock()
    session_post_mock = MagicMock()
    session_post_mock.side_effect = asyncio.TimeoutError()
    session.post = session_post_mock
    
    result = await send_telegram_message(
        session=session,
        chat_id="123456",
        message="Test message",
        bot_token="test_bot_token"
    )
    
    assert result is False


@pytest.mark.asyncio
async def test_get_telegram_updates_success():
    """Test successful retrieval of Telegram updates"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"result": [{"update_id": 1, "message": {"text": "hi"}}]}
    response.json.return_value = {"result": [{"update_id": 1, "message": {"text": "hi"}}]}
    
    session_get_mock = MagicMock()
    session_get_mock.return_value.__aenter__.return_value = response
    session_get_mock.return_value.__aexit__.return_value = None
    session.get = session_get_mock
    
    updates = await get_telegram_updates(
        session=session,
        bot_token="test_bot_token"
    )
    
    assert updates is not None
    assert len(updates) == 1
    assert updates[0]["update_id"] == 1


@pytest.mark.asyncio
async def test_get_telegram_updates_failure():
    """Test failure when retrieving Telegram updates"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 400
    response.text.return_value = "Bad Request"
    response.text.return_value = "Bad Request"
    
    session_get_mock = MagicMock()
    session_get_mock.return_value.__aenter__.return_value = response
    session_get_mock.return_value.__aexit__.return_value = None
    session.get = session_get_mock
    
    updates = await get_telegram_updates(
        session=session,
        bot_token="test_bot_token"
    )
    
    assert updates is None


@pytest.mark.asyncio
async def test_get_telegram_updates_with_offset():
    """Test retrieval of Telegram updates with offset"""
    session = AsyncMock()
    response = AsyncMock()
    response.status = 200
    response.json.return_value = {"result": []}
    response.json.return_value = {"result": []}
    
    session_get_mock = MagicMock()
    session_get_mock.return_value.__aenter__.return_value = response
    session_get_mock.return_value.__aexit__.return_value = None
    session.get = session_get_mock
    
    updates = await get_telegram_updates(
        session=session,
        bot_token="test_bot_token",
        offset=100
    )
    
    assert updates == []
    # Verify that offset was passed in the request
    args, kwargs = session.get.call_args
    assert kwargs['params']['offset'] == 100


@pytest.mark.asyncio
async def test_get_telegram_updates_network_error():
    """Test handling network errors when retrieving Telegram updates"""
    session = AsyncMock()
    session_get_mock = MagicMock()
    session_get_mock.side_effect = Exception("Network error")
    session.get = session_get_mock
    
    updates = await get_telegram_updates(
        session=session,
        bot_token="test_bot_token"
    )
    
    assert updates is None


@pytest.mark.asyncio
async def test_send_telegram_message_without_session():
    """Test sending a Telegram message without a session (should create one)"""
    with patch('telegram_api.create_robust_aiohttp_session') as mock_create_session:
        mock_session = AsyncMock()
        mock_response = AsyncMock()
        mock_response.status = 200
        
        session_post_mock = MagicMock()
        session_post_mock.return_value.__aenter__.return_value = mock_response
        session_post_mock.return_value.__aexit__.return_value = None
        mock_session.post = session_post_mock
        
        mock_create_session.return_value.__aenter__ = AsyncMock(return_value=mock_session)
        mock_create_session.return_value.__aexit__ = AsyncMock(return_value=None)
        
        result = await send_telegram_message(
            session=None,
            chat_id="123456",
            message="Test message",
            bot_token="test_bot_token"
        )
        
        assert result is True
        mock_create_session.assert_called_once()