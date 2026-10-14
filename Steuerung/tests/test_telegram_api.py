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
import socket
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
    # Die Startpruefung der Nameserver wird hier gestubbt: sonst haengt der Test
    # von der Erreichbarkeit echter Resolver ab (CI!) und kostet echte Timeouts.
    monkeypatch.setattr(telegram_api, "erreichbare_nameserver", lambda ns, **kw: list(ns))

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
    monkeypatch.setattr(telegram_api, "erreichbare_nameserver", lambda ns, **kw: list(ns))

    with caplog.at_level(logging.WARNING):
        session = telegram_api.create_robust_aiohttp_session(_config_mit_dns("8.8.8.8"))
    try:
        assert not session.closed
        assert "nicht nutzbar" in caplog.text
        assert "8.8.8.8" in caplog.text
    finally:
        await session.close()


# ── Startpruefung der Nameserver ──────────────────────────────────────────

def test_dns_probe_paket_ist_eine_gueltige_a_anfrage():
    paket = telegram_api.dns_probe_paket("hc-ping.com")

    assert paket[:2] == b"\x12\x34"          # Query-ID
    assert paket[2:4] == b"\x01\x00"         # Standardabfrage, RD=1
    assert paket[4:6] == b"\x00\x01"         # genau eine Frage
    assert b"\x07hc-ping\x03com\x00" in paket
    assert paket[-4:] == b"\x00\x01\x00\x01"  # QTYPE A, QCLASS IN


def test_dns_server_erreichbar_lehnt_nicht_literale_ab():
    """Ein Hostname statt IP darf keine DNS-Abfrage ausloesen (blockierender Pfad)."""
    assert telegram_api.dns_server_erreichbar("fritz.box", timeout=0.01) is False
    assert telegram_api.dns_server_erreichbar("192.168.178.", timeout=0.01) is False
    assert telegram_api.dns_server_erreichbar("", timeout=0.01) is False


def _fake_socket_modul(fabrik):
    """socket-Modul-Attrappe fuer telegram_api - laesst das echte Modul unberuehrt."""
    return SimpleNamespace(
        AF_INET=socket.AF_INET,
        AF_INET6=socket.AF_INET6,
        SOCK_DGRAM=socket.SOCK_DGRAM,
        timeout=socket.timeout,
        inet_pton=socket.inet_pton,
        socket=fabrik,
    )


def test_dns_server_erreichbar_ohne_antwort_ist_false(monkeypatch):
    """Keine Antwort -> False.

    Bewusst OHNE echten Socket: ein Test gegen eine unroutbare Adresse
    (z.B. 192.0.2.1) ist nicht tragfaehig - Router koennen UDP/53 abfangen und
    trotzdem antworten (auf diesem Netz beobachtet), dann waere der Test
    zufaellig gruen oder rot.
    """
    class StummerSocket:
        def settimeout(self, _timeout):
            pass

        def sendto(self, *_args):
            pass

        def recvfrom(self, _n):
            raise socket.timeout

        def close(self):
            pass

    monkeypatch.setattr(telegram_api, "socket", _fake_socket_modul(lambda *a, **kw: StummerSocket()))

    assert telegram_api.dns_server_erreichbar("192.168.178.1", timeout=0.2) is False


def test_dns_server_erreichbar_meldet_socketfehler_nicht_als_erreichbar(monkeypatch):
    """Netz weg (OSError) darf nicht als erreichbar durchgehen."""
    def kaputt(*_args, **_kwargs):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(telegram_api, "socket", _fake_socket_modul(kaputt))

    assert telegram_api.dns_server_erreichbar("192.168.178.1", timeout=0.01) is False


@pytest.mark.asyncio
async def test_session_ueberspringt_nicht_erreichbaren_nameserver(monkeypatch, caplog):
    """Regression: ein toter erster Eintrag darf nicht in den Resolver kommen."""
    gesehen = {}

    class FakeResolver:
        def __init__(self, nameservers=None):
            gesehen["nameservers"] = nameservers

    monkeypatch.setattr(telegram_api, "AsyncResolver", FakeResolver)
    monkeypatch.setattr(
        telegram_api, "erreichbare_nameserver",
        lambda ns, **kw: [s for s in ns if s != "192.168.178.1"],
    )

    with caplog.at_level(logging.WARNING):
        session = telegram_api.create_robust_aiohttp_session(
            _config_mit_dns("192.168.178.1, 100.100.100.100")
        )
    try:
        assert gesehen["nameservers"] == ["100.100.100.100"]
        assert "192.168.178.1" in caplog.text
        assert "wird uebersprungen" in caplog.text
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_session_ohne_erreichbaren_nameserver_nutzt_system_resolver(monkeypatch, caplog):
    """Antwortet keiner: Warnung + System-Resolver, niemals ein Absturz."""
    aufgerufen = {"resolver": False}

    def kein_resolver(*args, **kwargs):
        aufgerufen["resolver"] = True

    monkeypatch.setattr(telegram_api, "AsyncResolver", kein_resolver)
    monkeypatch.setattr(telegram_api, "erreichbare_nameserver", lambda ns, **kw: [])

    with caplog.at_level(logging.WARNING):
        session = telegram_api.create_robust_aiohttp_session(
            _config_mit_dns("10.0.0.99, 192.168.178.1")
        )
    try:
        assert aufgerufen["resolver"] is False
        assert "Kein Name aus [Netzwerk] DNS_SERVER erreichbar" in caplog.text
        assert not session.closed
    finally:
        await session.close()


def test_erreichbare_nameserver_cached_das_ergebnis(monkeypatch):
    """Zwei Sessions (Hauptprogramm + Telegram-Task) duerfen nur einmal proben."""
    telegram_api._DNS_PROBE_CACHE.clear()
    aufrufe = []

    def zaehlend(server, timeout=None):
        aufrufe.append(server)
        return True

    monkeypatch.setattr(telegram_api, "dns_server_erreichbar", zaehlend)

    erste = telegram_api.erreichbare_nameserver(["10.0.0.1", "10.0.0.2"])
    zweite = telegram_api.erreichbare_nameserver(["10.0.0.1", "10.0.0.2"])

    assert erste == ["10.0.0.1", "10.0.0.2"] == zweite
    assert aufrufe == ["10.0.0.1", "10.0.0.2"]
    telegram_api._DNS_PROBE_CACHE.clear()

