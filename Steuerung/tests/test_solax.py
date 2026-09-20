"""
Tests: Solax-API-Abruf mit Timeout- und Fehlerbehandlung.

Stellt sicher, dass get_solax_data() bei Timeout/ClientError korrekt retried
und None zurueckgibt statt die Exception ungehindert propagieren zu lassen.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import asyncio
import aiohttp
import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from solax import get_solax_data  # noqa: E402
from constants import DEFAULT_TIMEZONE  # noqa: E402


def baue_state():
    """Hilfskonstruktor fuer einen Minimal-State mit Solax-Config."""
    tz = pytz.timezone(DEFAULT_TIMEZONE)
    solar = SimpleNamespace(
        feedinpower=0.0,
        batpower=0.0,
        soc=0.0,
        last_api_data=None,
        last_api_call=None,
    )
    config = SimpleNamespace(
        SolaxCloud=SimpleNamespace(
            TOKEN_ID="test-token",
            SN="test-sn",
        )
    )
    state = SimpleNamespace(
        solar=solar,
        local_tz=tz,
        config=config,
    )
    return state


class MockResponse:
    """Simuliert eine aiohttp-Response fuer Erfolgsfaelle."""
    def __init__(self, json_data, status=200):
        self._json_data = json_data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                status=self.status,
                request_info=MagicMock(),
                history=(),
            )

    async def json(self):
        return self._json_data

    @property
    def ok(self):
        return self.status < 400


@pytest.mark.asyncio
async def test_timeout_wird_abgefangen_und_gibt_none(caplog):
    """
    TimeoutError (wie im gemeldeten Bug) wird vom Retry-Block abgefangen.
    Nach max_retries Fehlschlaege gibt die Funktion None zurueck.
    """
    state = baue_state()
    session = MagicMock()
    # session.get() gibt einen AsyncMock zurueck, dessen __aenter__ den Fehler wirft
    mock_resp = AsyncMock()
    mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError("Connection timed out"))
    session.get.return_value = mock_resp

    with caplog.at_level("ERROR"):
        result = await get_solax_data(session, state)

    assert result is None, "Nach 3 Retry-Versuchen sollte None zurueckkommen"
    assert session.get.call_count == 3, "Es muessen genau 3 Versuche stattfinden"
    assert state.solar.last_api_call is None, "Bei Fehler darf last_api_call NICHT aktualisiert werden"
@pytest.mark.asyncio
async def test_timeout_wird_im_retry_log_erfasst(caplog):
    """Der Timeout-Fehler wird korrekt im Log mit Typ-Information erfasst."""
    state = baue_state()
    session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError("Connection timed out"))
    session.get.return_value = mock_resp

    with caplog.at_level("ERROR"):
        await get_solax_data(session, state)

    timeout_logs = [
        r for r in caplog.records
        if "TimeoutError" in r.message or "timeout" in r.message.lower()
    ]
    assert len(timeout_logs) >= 1, (
        f"Mindestens ein Log-Eintrag muss 'TimeoutError' enthalten. "
        f"Gefundene Logs: {[r.message for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_clienterror_wird_abgefangen_und_gibt_none(caplog):
    """
    aiohttp.ClientError (z.B. ConnectionError) wird ebenfalls vom Retry-Block
    abgefangen. Nach max_retries Fehlschlaege gibt die Funktion None zurueck.
    """
    state = baue_state()
    session = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("Connection refused"))
    session.get.return_value = mock_resp

    with caplog.at_level("ERROR"):
        result = await get_solax_data(session, state)

    assert result is None, "Nach 3 Retry-Versuchen sollte None zurueckkommen"
    assert session.get.call_count == 3


@pytest.mark.asyncio
async def test_erfolgreicher_erstversuch():
    """Erfolgreicher API-Call im ersten Versuch liefert Daten und logged keinen Fehler."""
    state = baue_state()

    erfolgsdaten = {
        "success": True,
        "result": {"acpower": 1234, "feedinpower": 500, "soc": 75}
    }

    session = MagicMock()
    session.get.return_value = MockResponse(json_data=erfolgsdaten)

    result = await get_solax_data(session, state)

    assert result == erfolgsdaten["result"], "Result-Daten muessen korrekt sein"
    assert state.solar.last_api_data == erfolgsdaten["result"]
    assert state.solar.last_api_call is not None, "last_api_call muss gesetzt sein"
    assert session.get.call_count == 1, "Nur 1 Versuch bei Erfolg"


@pytest.mark.asyncio
async def test_retry_faehrt_bei_spaetem_erfolg():
    """Erste 2 Versuche schlagen fehl, dritter Erfolg -> Daten werden geliefert."""
    state = baue_state()
    session = MagicMock()

    erfolgsdaten = {
        "success": True,
        "result": {"acpower": 999, "feedinpower": 300, "soc": 50}
    }

    # Simuliere: 1. und 2. Aufruf werfen Timeout, 3. Aufruf erfolgreich
    fehler_resp = AsyncMock()
    fehler_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError("Timeout"))
    session.get.side_effect = [
        fehler_resp,           # 1. Versuch -> Timeout
        fehler_resp,           # 2. Versuch -> Timeout
        MockResponse(json_data=erfolgsdaten),  # 3. Versuch -> Erfolg
    ]

    result = await get_solax_data(session, state)

    assert result == erfolgsdaten["result"], "Result-Daten muessen korrekt sein"
    assert session.get.call_count == 3, "Genau 3 Versuche (2 Fehler + 1 Erfolg)"


@pytest.mark.asyncio
async def test_kein_token_oder_sn_gibt_none():
    """Fehlende Config (Token/SN) soll direkt None zurueckgeben, ohne API-Call."""
    state = baue_state()
    state.config.SolaxCloud.TOKEN_ID = ""
    session = MagicMock()

    result = await get_solax_data(session, state)

    assert result is None
    session.get.assert_not_called(), "Kein API-Call bei fehlender Config"


@pytest.mark.asyncio
async def test_cache_wird_genutzt():
    """Innerhalb des CACHE_TTL werden gecachte Daten verwendet, kein API-Call."""
    tz = pytz.timezone(DEFAULT_TIMEZONE)
    now = datetime.now(tz)
    state = baue_state()
    state.solar.last_api_data = {"acpower": 100, "feedinpower": 50, "soc": 80}
    state.solar.last_api_call = now - timedelta(minutes=2)  # innerhalb 5 min TTL

    session = MagicMock()
    session.get = MagicMock()  # einfacher Mock, session.get() wird nie aufgerufen

    result = await get_solax_data(session, state)

    assert result == state.solar.last_api_data
    session.get.assert_not_called(), "Cache muss API-Call verhindern"