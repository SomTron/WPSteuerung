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
