# -*- coding: utf-8 -*-
"""
Unit-Tests fuer telegram_api.py - hier wird NICHTS echt gesendet.

Hintergrund (Befund 13.10.2026): Der Healthcheck meldete einen Fehler, obwohl
der Fernzugriff auf den Pi lief. Im Log stand nur

    Healthcheck-Ping Fehler:  -> https://hc-ping.com/...

Die Ursache (Timeout / DNS / Netz) war aus der leeren Exception-Message nicht
erkennbar. Diese Tests sichern ab, dass der Exception-Typ immer im Log steht -
eine leere Meldung darf nie wieder zu einer leeren Logzeile fuehren.

Die Live-API-Tests liegen bewusst getrennt in test_telegram_handler.py
(@pytest.mark.integration).
"""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

import telegram_api
from telegram_api import (
    _send_healthcheck_ping,
    fehler_text,
    get_telegram_updates,
    send_telegram_message,
)


def _session_mit_ausnahme(exc: BaseException, methode: str = "get"):
    """Session-Mock, bei dem der Request schon beim Betreten scheitert."""
    session = MagicMock()
    antwort = AsyncMock()
    antwort.__aenter__ = AsyncMock(side_effect=exc)
    getattr(session, methode).return_value = antwort
    return session


class FakeResponse:
    """Minimal-Response, die als async context manager nutzbar ist."""

    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self._text


# ── fehler_text ───────────────────────────────────────────────────────────

def test_fehler_text_zeigt_typ_bei_leerer_meldung():
    """Der wichtigste Fall: leere Message darf nicht zu leerem Log fuehren."""
    text = fehler_text(asyncio.TimeoutError())

    assert type(asyncio.TimeoutError()).__name__ in text
    assert "keine Meldung" in text


def test_fehler_text_nutzt_typ_und_meldung():
    assert fehler_text(OSError("Network is unreachable")) == "OSError: Network is unreachable"


def test_fehler_text_behandelt_reine_leerzeichen_wie_leer():
    assert "keine Meldung" in fehler_text(ValueError("   "))


# ── Healthcheck-Ping ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_healthcheck_ping_timeout_loggt_exception_typ(caplog):
    """Regression Nutzer-Log 13.10.2026: die Ping-Fehlerzeile war leer."""
    session = _session_mit_ausnahme(asyncio.TimeoutError())
    url = "https://hc-ping.com/a0bb32d9-6bf1-4e1f-859a-4ee95bf48d20"

    with caplog.at_level(logging.ERROR):
        ergebnis = await _send_healthcheck_ping(session, url)

    assert ergebnis is False
    assert "TimeoutError" in caplog.text
    assert url in caplog.text


@pytest.mark.asyncio
async def test_healthcheck_ping_erfolg_ist_kein_fehler(caplog):
    session = MagicMock()
    session.get.return_value = FakeResponse(status=200)

    with caplog.at_level(logging.DEBUG):
        ergebnis = await _send_healthcheck_ping(session, "https://hc-ping.com/abc")

    assert ergebnis is True
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_healthcheck_ping_http_fehlerstatus_wird_gemeldet(caplog):
    """Ein HTTP-Fehlstatus (z.B. 503) laeuft nicht in den Exception-Pfad."""
    session = MagicMock()
    session.get.return_value = FakeResponse(status=503, text="down")

    with caplog.at_level(logging.WARNING):
        ergebnis = await _send_healthcheck_ping(session, "https://hc-ping.com/abc")

    assert ergebnis is False
    assert "Status 503" in caplog.text


# ── Telegram-Updates ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_updates_netzwerkfehler_nennt_typ(caplog):
    """Der "Netzwerkfehler beim Abrufen" trug bisher eine leere Message."""
    session = _session_mit_ausnahme(aiohttp.ClientConnectionError())
    # retries=1 -> kein Warte-Sleep, haelt das CI-Laufzeitbudget klein.
    with caplog.at_level(logging.DEBUG):
        ergebnis = await get_telegram_updates(session, "token", retries=1)

    assert ergebnis is None
    assert "ClientConnectionError" in caplog.text
    assert "keine Meldung" in caplog.text


@pytest.mark.asyncio
async def test_telegram_updates_timeout_ist_im_log_erkennbar(caplog):
    """Ab Python 3.11 (Debian 12 auf dem Pi) ist TimeoutError ein OSError.

    Er landet deshalb im Netzwerk-Zweig - genau dort entstand im Nutzer-Log
    13.10.2026 die leere Zeile
    "Netzwerkfehler beim Abrufen von Telegram-Updates (Versuch 1/3): ".
    """
    session = _session_mit_ausnahme(asyncio.TimeoutError())

    with caplog.at_level(logging.DEBUG):
        await get_telegram_updates(session, "token", retries=1)

    zeilen = [r.getMessage() for r in caplog.records if "Telegram-Updates" in r.getMessage()]
    assert zeilen, "Der Fehlversuch muss ueberhaupt geloggt werden"

    if isinstance(asyncio.TimeoutError(), OSError):
        assert "TimeoutError" in zeilen[0]
    else:
        assert "Timeout" in zeilen[0]


@pytest.mark.asyncio
async def test_telegram_updates_unerwarteter_fehler_nennt_typ(caplog):
    session = _session_mit_ausnahme(ValueError("kaputt"))

    with caplog.at_level(logging.ERROR):
        ergebnis = await get_telegram_updates(session, "token", retries=1)

    assert ergebnis is None
    assert "ValueError" in caplog.text
    assert "kaputt" in caplog.text


# ── Telegram senden ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_telegram_message_netzwerkfehler_nennt_typ(caplog):
    session = _session_mit_ausnahme(aiohttp.ClientConnectionError(), methode="post")

    with caplog.at_level(logging.ERROR):
        ergebnis = await send_telegram_message(
            session, "chat-id", "text", "token", retries=1, retry_delay=0
        )

    assert ergebnis is False
    assert "ClientConnectionError" in caplog.text


# ── DNS-Resolver ──────────────────────────────────────────────────────────

def _config_mit_dns(server: str):
    return SimpleNamespace(Netzwerk=SimpleNamespace(DNS_SERVER=server))


def test_dns_nameserver_liest_komma_getrennte_liste():
    assert telegram_api.dns_nameserver(_config_mit_dns("192.168.178.1, 1.1.1.1")) == [
        "192.168.178.1", "1.1.1.1",
    ]


def test_dns_nameserver_ist_ohne_angabe_leer():
    """Ohne Angabe darf es KEINEN Override geben (System-Resolver bleibt)."""
    assert telegram_api.dns_nameserver(None) == []
    assert telegram_api.dns_nameserver(_config_mit_dns("")) == []
    assert telegram_api.dns_nameserver(_config_mit_dns("  ,  ")) == []


@pytest.mark.asyncio
async def test_session_ohne_dns_server_nutzt_system_resolver(caplog):
    """Default muss /etc/resolv.conf bleiben - keine festen Public-DNS."""
    with caplog.at_level(logging.INFO):
        session = telegram_api.create_robust_aiohttp_session()
    try:
        assert "resolv.conf" in caplog.text
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_reicht_konfigurierte_nameserver_an_den_resolver(monkeypatch):
    gesehen = {}

    class FakeResolver:
        def __init__(self, nameservers=None):
            gesehen["nameservers"] = nameservers

    monkeypatch.setattr(telegram_api, "AsyncResolver", FakeResolver)

    session = telegram_api.create_robust_aiohttp_session(_config_mit_dns("192.168.178.1, 1.1.1.1"))
    try:
        assert gesehen["nameservers"] == ["192.168.178.1", "1.1.1.1"]
        assert not session.closed
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_meldet_unbrauchbaren_nameserver_und_laeuft_weiter(monkeypatch, caplog):
    """Fehlt aiodns, darf der Start nicht scheitern - nur eine Warnung."""
    def kein_aiodns(*args, **kwargs):
        raise RuntimeError("aiodns not installed")

    monkeypatch.setattr(telegram_api, "AsyncResolver", kein_aiodns)

    with caplog.at_level(logging.WARNING):
        session = telegram_api.create_robust_aiohttp_session(_config_mit_dns("8.8.8.8"))
    try:
        assert not session.closed
        assert "nicht nutzbar" in caplog.text
        assert "8.8.8.8" in caplog.text
    finally:
        await session.close()

