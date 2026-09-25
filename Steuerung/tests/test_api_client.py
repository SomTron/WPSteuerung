"""Tests fuer den zentralen robusten API-Client (`api_client`).

Warum wichtig: `weather_forecast` ruft ueber diesen Client jede
Open-Meteo-Prognose ab. Die Prognose steuert PV-Warten, CalcStart und die
Legionellen-Tageswahl. Ein Fehler in der 4xx/5xx-Unterscheidung bedeutet:

- 4xx mit Retry  -> dauerhaft fehlschlagende Aufrufe dreimal wiederholt
                   (unnötige Last auf Pi und Remote-API)
- 5xx ohne Retry -> transiente Fehler brechen die Prognose ab, die
                   Steuerung faellt auf "keine Prognose" zurueck
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from api_client import ApiResult, robust_api_call, robust_get, robust_post  # noqa: E402


class FakeResponse:
    """Minimaler aiohttp-Response-Ersatz."""

    def __init__(self, status=200, json_daten=None, text="", read_daten=b""):
        self.status = status
        self._json = json_daten
        self._text = text
        self._read = read_daten

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def read(self):
        return self._read


def session_mit(antworten, methode="get"):
    """Session-Ersatz, der der Reihe nach die Antworten ausgibt."""
    # Einzelnes Objekt als Ein-Ergebnis-Liste behandeln, damit Aufrufer
    # nicht jede Liste selbst umschreiben muessen.
    if isinstance(antworten, (list, tuple)):
        antworten = list(antworten)
    else:
        antworten = [antworten]

    session = MagicMock()
    aufrufe = []

    def _get(*args, **kwargs):
        aufrufe.append(kwargs)
        zustand = antworten[min(len(aufrufe) - 1, len(antworten) - 1)]
        if isinstance(zustand, Exception):
            raise zustand
        if callable(zustand):
            return zustand()
        return zustand

    setattr(session, methode, _get)
    session.aufrufe = aufrufe
    return session


async def hole_ohne_warten(session, **kwargs):
    """robust_api_call mit Wartezeit 0."""
    basis = dict(timeout_sec=1, max_retries=1, retry_delay_sec=0, logger=None)
    basis.update(kwargs)
    return await robust_api_call(session, "GET", "https://x/y", **basis)


# ------------------------------------------------------------- Erfolgsfall


@pytest.mark.asyncio
async def test_erfolgreicher_get_liefert_daten():
    session = session_mit(FakeResponse(200, {"wetter": "gut"}))
    r = await hole_ohne_warten(session)
    assert isinstance(r, ApiResult)
    assert r.success is True
    assert r.data == {"wetter": "gut"}
    assert r.status_code == 200
    assert r.error_type is None


@pytest.mark.asyncio
async def test_extract_json_false_liefert_rohdaten():
    session = session_mit(FakeResponse(200, read_daten=b"rohtext"))
    r = await hole_ohne_warten(session, extract_json=False)
    assert r.success is True
    assert r.data == b"rohtext"


@pytest.mark.asyncio
async def test_response_validator_entscheidet_ueber_erfolg():
    """Ein 200 mit ungueltigem Inhalt soll als Fehler gelten."""
    session = session_mit(FakeResponse(200, {"ok": False}))
    r = await hole_ohne_warten(
        session, response_validator=lambda resp: resp.status == 200 and True
    )
    assert r.success is True

    session2 = session_mit(FakeResponse(500, {"ok": False}))
    r2 = await hole_ohne_warten(
        session2, response_validator=lambda resp: False, max_retries=1
    )
    assert r2.success is False


# ------------------------------------------------- 4xx: kein Retry (wichtig)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
@pytest.mark.asyncio
async def test_4xx_wird_nicht_wiederholt(status):
    """Regression: 4xx sind Client-Fehler. Ein Retry waere verschwendete Last."""
    session = session_mit(FakeResponse(status, text="kaputt"))
    r = await hole_ohne_warten(session, max_retries=3)
    assert r.success is False
    assert r.status_code == status
    assert r.error_type == f"HTTP_{status}"
    assert len(session.aufrufe) == 1, "4xx darf genau einmal aufgerufen werden"


# --------------------------------------------------- 5xx: mit Retry (wichtig)


@pytest.mark.asyncio
async def test_5xx_wird_wiederholt_und_meldet_kein_http_plus_500():
    session = session_mit([FakeResponse(503, text="busy")] * 3)
    r = await hole_ohne_warten(session, max_retries=3)
    assert r.success is False
    assert len(session.aufrufe) == 3
    # Am Ende: MaxRetriesExceeded, nicht HTTP_503 - sonst wuerde ein
    # transientes Problem wie ein dauerhafter Client-Fehler aussehen.
    assert r.error_type == "MaxRetriesExceeded"


@pytest.mark.asyncio
async def test_5xx_erholt_sich_nach_retry():
    session = session_mit([FakeResponse(500, text="teuer"), FakeResponse(200, {"ok": 1})])
    r = await hole_ohne_warten(session, max_retries=3)
    assert r.success is True
    assert r.data == {"ok": 1}
    assert len(session.aufrufe) == 2


# --------------------------------------------------------- Netzwerkfehler


@pytest.mark.asyncio
async def test_timeout_wird_wiederholt():
    session = session_mit([asyncio.TimeoutError("timeout")] * 3)
    r = await hole_ohne_warten(session, max_retries=3)
    assert r.success is False
    assert r.error_type == "MaxRetriesExceeded"
    assert len(session.aufrufe) == 3


@pytest.mark.asyncio
async def test_clienterror_wird_wiederholt():
    session = session_mit([aiohttp.ClientError("netz")] * 2)
    r = await hole_ohne_warten(session, max_retries=2)
    assert r.success is False
    assert len(session.aufrufe) == 2


@pytest.mark.asyncio
async def test_oserror_wird_wiederholt():
    session = session_mit([OSError("kein Netz")] * 2)
    r = await hole_ohne_warten(session, max_retries=2)
    assert r.success is False
    assert len(session.aufrufe) == 2


@pytest.mark.asyncio
async def test_unerwarteter_fehler_bricht_sofort_ab():
    """Ein Programmierfehler soll nicht 3x wiederholt werden."""
    session = session_mit([ValueError("Programmierfehler")] * 3)
    r = await hole_ohne_warten(session, max_retries=3)
    assert r.success is False
    assert r.error_type == "ValueError"
    assert len(session.aufrufe) == 1, "Unerwartete Fehler nicht wiederholen"


# ------------------------------------------------------------- Wrapper


@pytest.mark.asyncio
async def test_robust_get_delegiert_an_robust_api_call():
    session = session_mit(FakeResponse(200, {"a": 1}))
    r = await robust_get(session, "https://x", max_retries=1, retry_delay_sec=0)
    assert r.success is True
    assert r.data == {"a": 1}


@pytest.mark.asyncio
async def test_robust_post_delegiert_an_robust_api_call():
    session = session_mit(FakeResponse(200, {"b": 2}), methode="post")
    r = await robust_post(
        session, "https://x", json={"q": 1}, max_retries=1, retry_delay_sec=0
    )
    assert r.success is True
    assert r.data == {"b": 2}


@pytest.mark.asyncio
async def test_fehlende_http_methode_wird_nicht_als_netzfehler_behandelt():
    """Ein Tippfehler in der Methode darf nicht in Endlosschleifen enden."""
    session = MagicMock()
    session.get = None
    r = await robust_get(session, "https://x", max_retries=1, retry_delay_sec=0)
    assert r.success is False
    assert r.error_type is not None
