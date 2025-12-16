import configparser
import pytest
import aiohttp
import asyncio
from telegram_handler import create_robust_aiohttp_session, send_telegram_message

import os

def get_telegram_config():
    config = configparser.ConfigParser()
    config.read(os.path.join(os.path.dirname(__file__), '../richtige_config.ini'))
    bot_token = config.get('Telegram', 'BOT_TOKEN', fallback=None)
    chat_id = config.get('Telegram', 'CHAT_ID', fallback=None)
    return bot_token, chat_id

@pytest.mark.asyncio
async def test_send_telegram_message_success():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    session = create_robust_aiohttp_session()
    try:
        result = await send_telegram_message(session, chat_id, "Testnachricht vom automatisierten Telegram-Test", bot_token)
        assert result is True
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_send_telegram_message_network_failure():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    # Simuliere Netzwerkausfall durch ungültigen Proxy
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9999'  # Port, auf dem kein Proxy läuft
    try:
        result = await send_telegram_message(session, chat_id, "Testnachricht Netzwerkausfall", bot_token, retries=2, retry_delay=1)
        assert result is False
    finally:
        await session.close()
        del os.environ['HTTPS_PROXY']

@pytest.mark.asyncio
async def test_send_telegram_message_markdown():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    session = create_robust_aiohttp_session()
    try:
        msg = "*Fett* _Kursiv_ [Link](https://example.com) `Code`"
        result = await send_telegram_message(session, chat_id, msg, bot_token, parse_mode="Markdown")
        assert result is True
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_send_telegram_message_long():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    session = create_robust_aiohttp_session()
    try:
        msg = "A" * 5000  # Über 4096 Zeichen
        result = await send_telegram_message(session, chat_id, msg, bot_token)
        assert result is True
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_send_telegram_message_invalid_token():
    bot_token, chat_id = "invalid", "invalid"
    session = create_robust_aiohttp_session()
    try:
        result = await send_telegram_message(session, chat_id, "Test mit ungültigem Token", bot_token)
        assert result is False
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_send_telegram_message_reply_keyboard():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    session = create_robust_aiohttp_session()
    try:
        reply_markup = {"keyboard": [["Test1", "Test2"]], "resize_keyboard": True}
        result = await send_telegram_message(session, chat_id, "Test mit Keyboard", bot_token, reply_markup=reply_markup)
        assert result is True
    finally:
        await session.close()

@pytest.mark.asyncio
async def test_send_telegram_message_timeout():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    # Simuliere Timeout durch sehr kurzen Timeout und ungültigen Proxy
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9999'
    try:
        result = await send_telegram_message(session, chat_id, "Test Timeout", bot_token, retries=1, retry_delay=1)
        assert result is False
    finally:
        await session.close()
        del os.environ['HTTPS_PROXY']

@pytest.mark.asyncio
async def test_send_telegram_message_parallel():
    bot_token, chat_id = get_telegram_config()
    if not bot_token or not chat_id:
        pytest.skip("BOT_TOKEN und CHAT_ID müssen in richtige_config.ini gesetzt sein.")
    session = create_robust_aiohttp_session()
    try:
        tasks = [send_telegram_message(session, chat_id, f"Parallel Test {i}", bot_token) for i in range(3)]
        results = await asyncio.gather(*tasks)
        assert all(results)
    finally:
        await session.close()

@pytest.mark.asyncio
def test_get_telegram_updates():
    """
    Dieser Test kann nicht automatisiert laufen, solange der Bot-Skript aktiv ist (Telegram API erlaubt nur eine getUpdates-Session).
    Führe diesen Test nur aus, wenn der Bot nicht läuft.
    """
    pytest.skip("Test für get_telegram_updates wird übersprungen, solange der Bot-Skript läuft (API-Limit 409 Conflict). Nur manuell testen!")
