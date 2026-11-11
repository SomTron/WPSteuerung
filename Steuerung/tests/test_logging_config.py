"""Tests fuer die begrenzte Telegram-Logging-Queue."""
import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from logging_config import TelegramHandler


# --- send_message: beide Wege muessen sich gleich verhalten -----------------

class _Antwort:
    """Antwort-Stummsetzer fuer den aiohttp-Kontextmanager."""

    def __init__(self, status, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Post:
    """Ersetzt session.post(...) - liefert eine Antwort oder loest aus."""

    def __init__(self, antwort=None, fehler=None):
        self.antwort = antwort
        self.fehler = fehler
        self.gezogene_urls = []

    def __call__(self, url, **kwargs):
        self.gezogene_urls.append(url)
        if self.fehler is not None:
            raise self.fehler
        return self.antwort


class _Session:
    """Minimale aiohttp-Session: post() ist aufrufbar, closed ist False."""

    def __init__(self, post):
        self.post = post
        self.closed = False


class _KurzlebigeSession(_Session):
    """Session, die beim Verlassen des Kontexts als geschlossen markiert wird."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


@pytest.mark.asyncio
async def test_send_message_erfolg_mit_session():
    """Der Session-Zweig meldet Erfolg bei HTTP 200."""
    post = _Post(_Antwort(200, "ok"))
    handler = TelegramHandler("token", "chat", session=_Session(post))
    assert await handler.send_message("hallo") is True
    assert post.gezogene_urls == ["https://api.telegram.org/bottoken/sendMessage"]


@pytest.mark.asyncio
async def test_send_message_meldet_http_fehler(caplog):
    """HTTP-Fehler darf nicht still verschwinden (war der alte Defekt)."""
    post = _Post(_Antwort(400, "Bad Request: chat not found"))
    handler = TelegramHandler("token", "chat", session=_Session(post))
    with caplog.at_level(logging.ERROR):
        assert await handler.send_message("hallo") is False
    assert any("Bad Request" in m for m in caplog.messages), (
        "Der Telegram-Fehlertext fehlt im Log")
    # Gegenprobe: der Handler darf sich nicht selbst wieder einreihen.
    treffer = [r for r in caplog.records if getattr(r, "kein_telegram", False)]
    assert treffer, "Meldung ohne kein_telegram wuerde die Queue erneut fuellen"


@pytest.mark.asyncio
async def test_send_message_meldet_ausnahme_mit_traceback(caplog):
    """Ein Timeout muss als ERROR mit exc_info sichtbar sein."""
    post = _Post(fehler=asyncio.TimeoutError("timeout"))
    handler = TelegramHandler("token", "chat", session=_Session(post))
    with caplog.at_level(logging.ERROR):
        assert await handler.send_message("hallo") is False
    assert any("Telegram Senden Fehler" in m for m in caplog.messages)
    treffer = [r for r in caplog.records if getattr(r, "kein_telegram", False)]
    assert treffer and treffer[0].exc_info, "ohne exc_info ist der Ort verloren"


@pytest.mark.asyncio
async def test_send_message_ohne_session_nutzt_kurzlebige(monkeypatch):
    """Produktivpfad: main.setup_application ruft setup_logging ohne session auf.

    Vorher war das der Zweig mit "# ... error handling ..." und
    `except Exception: return False` - ein Fehler blieb also unsichtbar.
    """
    post = _Post(_Antwort(200, "ok"))
    erzeugt = []

    def _session_fabrik():
        s = _KurzlebigeSession(post)
        erzeugt.append(s)
        return s

    monkeypatch.setattr(
        "logging_config.aiohttp.ClientSession", _session_fabrik)
    handler = TelegramHandler("token", "chat")          # keine Session
    assert await handler.send_message("hallo") is True
    assert len(erzeugt) == 1, "es wird eine eigene Session erzeugt"
    assert erzeugt[0].closed is True, "die kurzlebige Session wird geschlossen"


@pytest.mark.asyncio
async def test_send_message_ohne_session_meldet_fehler(caplog, monkeypatch):
    """Der Produktivpfad muss Fehler genauso melden wie der Session-Zweig."""
    post = _Post(_Antwort(500, "Internal Server Error"))
    monkeypatch.setattr(
        "logging_config.aiohttp.ClientSession",
        lambda: _KurzlebigeSession(post))
    handler = TelegramHandler("token", "chat")
    with caplog.at_level(logging.ERROR):
        assert await handler.send_message("hallo") is False
    assert any("Internal Server Error" in m for m in caplog.messages), (
        "der Fallback verschluckt den Fehler weiterhin")


@pytest.mark.asyncio
async def test_send_message_geschlossene_session_nutzt_neue(monkeypatch):
    """Eine geschlossene Session darf nicht wiederverwendet werden.

    Sonst endet jeder Versand mit "Session is closed", ohne dass es auffaellt -
    der Handler wuerde nur noch Fehler loggen.
    """
    alt_post = _Post(_Antwort(200, "alt"))
    alt = _Session(alt_post)
    alt.closed = True
    neu_post = _Post(_Antwort(200, "neu"))
    neu = _KurzlebigeSession(neu_post)
    monkeypatch.setattr("logging_config.aiohttp.ClientSession", lambda: neu)

    handler = TelegramHandler("token", "chat", session=alt)
    assert await handler.send_message("hallo") is True
    assert alt_post.gezogene_urls == [], (
        "die geschlossene Session wurde weiterverwendet")
    assert neu_post.gezogene_urls == ["https://api.telegram.org/bottoken/sendMessage"]


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
