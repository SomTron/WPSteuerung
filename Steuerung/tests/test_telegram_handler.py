# -*- coding: utf-8 -*-
"""
Integrationstests fuer telegram_api.py -- HINWEIS: Diese Tests sprechen die
ECHTE Telegram-API und senden echte Nachrichten an den konfigurierten Chat!

Standardverhalten: SKIP, solange keine Credentials in richtige_config.ini
liegen. Bewusst ausfuehren mit:

    pytest Steuerung/tests/test_telegram_handler.py -m integration

Die Tests sind mit @pytest.mark.integration markiert, damit sie sich auch in
CI-Pipelines zuverlaessig ausschliessen lassen (-m "not integration").
"""
import asyncio
import configparser
import os

import aiohttp
import pytest

from telegram_api import create_robust_aiohttp_session, send_telegram_message

RICHTIGE_CONFIG = os.path.join(os.path.dirname(__file__), '..', 'richtige_config.ini')


def _lese_telegram_config():
    config = configparser.ConfigParser()
    config.read(RICHTIGE_CONFIG)
    return (config.get('Telegram', 'BOT_TOKEN', fallback=None),
            config.get('Telegram', 'CHAT_ID', fallback=None))


BOT_TOKEN, CHAT_ID = _lese_telegram_config()
TELEGRAM_KONFIG_OK = bool(BOT_TOKEN and CHAT_ID)
SKIP_GRUND = ("BOT_TOKEN und CHAT_ID muessen in richtige_config.ini gesetzt sein "
              "(Live-API-Test; ohne Credentials bewusst geskippt).")

# Gemeinsame Dekoratoren fuer alle Live-API-Tests
integration = pytest.mark.integration
brauche_credentials = pytest.mark.skipif(not TELEGRAM_KONFIG_OK, reason=SKIP_GRUND)


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_success():
    session = create_robust_aiohttp_session()
    try:
        result = await send_telegram_message(session, CHAT_ID,
                                             "Testnachricht vom automatisierten Telegram-Test",
                                             BOT_TOKEN)
        assert result is True
    finally:
        await session.close()


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_network_failure():
    """Simulierter Netzwerkausfall ueber einen toten Proxy."""
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9999'  # Port, auf dem kein Proxy laeuft
    try:
        result = await send_telegram_message(session, CHAT_ID,
                                             "Testnachricht Netzwerkausfall", BOT_TOKEN,
                                             retries=2, retry_delay=1)
        assert result is False
    finally:
        await session.close()
        del os.environ['HTTPS_PROXY']


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_markdown():
    session = create_robust_aiohttp_session()
    try:
        msg = "*Fett* _Kursiv_ [Link](https://example.com) `Code`"
        result = await send_telegram_message(session, CHAT_ID, msg, BOT_TOKEN,
                                             parse_mode="Markdown")
        assert result is True
    finally:
        await session.close()


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_long():
    """Nachricht ueber dem 4096-Zeichen-Limit muss (aufgeteilt) ankommen."""
    session = create_robust_aiohttp_session()
    try:
        msg = "A" * 5000
        result = await send_telegram_message(session, CHAT_ID, msg, BOT_TOKEN)
        assert result is True
    finally:
        await session.close()


@integration
@pytest.mark.asyncio
async def test_send_telegram_message_invalid_token():
    """Braucht KEINE echten Credentials: ungültiger Token muss False liefern."""
    session = create_robust_aiohttp_session()
    try:
        result = await send_telegram_message(session, "invalid",
                                             "Test mit ungültigem Token", "invalid")
        assert result is False
    finally:
        await session.close()


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_reply_keyboard():
    session = create_robust_aiohttp_session()
    try:
        reply_markup = {"keyboard": [["Test1", "Test2"]], "resize_keyboard": True}
        result = await send_telegram_message(session, CHAT_ID, "Test mit Keyboard",
                                             BOT_TOKEN, reply_markup=reply_markup)
        assert result is True
    finally:
        await session.close()


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_timeout():
    """Simulierter Timeout ueber toten Proxy und kurzes Retry-Budget."""
    connector = aiohttp.TCPConnector()
    session = aiohttp.ClientSession(connector=connector, trust_env=True)
    os.environ['HTTPS_PROXY'] = 'http://127.0.0.1:9999'
    try:
        result = await send_telegram_message(session, CHAT_ID, "Test Timeout", BOT_TOKEN,
                                             retries=1, retry_delay=1)
        assert result is False
    finally:
        await session.close()
        del os.environ['HTTPS_PROXY']


@integration
@brauche_credentials
@pytest.mark.asyncio
async def test_send_telegram_message_parallel():
    session = create_robust_aiohttp_session()
    try:
        tasks = [send_telegram_message(session, CHAT_ID, f"Parallel Test {i}", BOT_TOKEN)
                 for i in range(3)]
        results = await asyncio.gather(*tasks)
        assert all(results)
    finally:
        await session.close()


@integration
def test_get_telegram_updates():
    """
    Kann nicht automatisiert laufen, solange der Bot aktiv ist (Telegram erlaubt
    nur eine getUpdates-Session -> 409 Conflict). Nur MANUELL ausfuehren, wenn
    der Bot-Prozess gestoppt ist:

        pytest Steuerung/tests/test_telegram_handler.py -m integration -k updates
    """
    pytest.skip("getUpdates kollidiert mit dem laufenden Bot (409 Conflict). "
                "Nur manuell testen, wenn der Bot gestoppt ist!")
