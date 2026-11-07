"""Tests fuer die begrenzte Telegram-Logging-Queue."""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from logging_config import TelegramHandler


@pytest.mark.asyncio
async def test_telegram_queue_ist_begrenzt_und_verwirft_alte_eintraege():
    handler = TelegramHandler("token", "chat", session=object())
    handler.process_queue = AsyncMock()
    try:
        assert handler.queue.maxsize == 50
        for index in range(60):
            handler.emit(logging.LogRecord(
                "test", logging.WARNING, __file__, 1,
                "message-%s", (index,), None,
            ))
        assert handler.queue.qsize() <= 50
        assert handler.dropped_messages >= 10
    finally:
        handler.close()
        current = handler.task
        if current is not None:
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)


@pytest.mark.asyncio
async def test_telegram_ueberspringt_reine_diagnose_meldungen():
    """`kein_telegram`-Records gehoeren ins Log, nicht aufs Handy.

    Anlass (07.11.2026): WebApp-Fehler (api.py `/debug/clientfehler`) werden
    als WARNING geschrieben. Der TelegramHandler haengt ab WARNING am Root -
    ohne diese Ausnahme wuerde ein Handy im Funkloch im 5-Minuten-Takt
    Nachrichten erzeugen.
    """
    handler = TelegramHandler("token", "chat", session=object())
    handler.process_queue = AsyncMock()
    try:
        record = logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "WebApp-Fehler", (), None,
        )
        record.kein_telegram = True
        handler.emit(record)
        assert handler.queue.empty(), "Diagnose-Meldung landet in der Telegram-Queue"
        # Gegenprobe: ohne die Markierung geht dieselbe Meldung raus.
        handler.emit(logging.LogRecord(
            "test", logging.WARNING, __file__, 1, "echter Alarm", (), None,
        ))
        assert handler.queue.qsize() == 1
    finally:
        handler.close()
        current = handler.task
        if current is not None:
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)
