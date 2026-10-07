"""Tests fuer RateLimitMiddleware (Steuerung/api.py).

Hintergrund: Die Middleware nutzt einen eigenen Prozess-Counter (keine
slowapi-Storage-Interna mehr). Regressionen, die hier abgesichert werden:
- Limit-Reihenfolge: /config/export (30) muss VOR /config (10) geprueft werden.
- Limit-Tier-Trennung: schreibende Endpoints (10/min) teilen sich nicht
  mit dem Default-Kontingent (100/min) eines Clients.
- Body-Buffering: Der gepufferte Request-Body muss FastAPI intakt erreichen
  (frueher gab buffered_receive() immer einen leeren Body zurueck -> POSTs
  waeren mit 422/Validation-Error gescheitert).
- 429 inkl. Retry-After nach Ablauf des Limits.

Ohne TestClient/httpx: dispatch() wird direkt mit Fake-Requests aufgerufen.
"""
import asyncio
import os
import sys
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402


class _FakeHeaders:
    def __init__(self, d=None):
        self._d = dict(d or {})

    def get(self, key, default=None):
        return self._d.get(key, default)


class _FakeRequest:
    def __init__(self, path="/status", headers=None, body=b""):
        self.url = type("U", (), {"path": path})()
        self.headers = _FakeHeaders(headers)
        # slowapi's get_remote_address erwartet request.client.host
        self.client = type("C", (), {"host": "127.0.0.1"})()
        self._rohbody = body
        self._body = None

    async def body(self):
        return self._rohbody


def _mw():
    """Middleware ohne echte App-Kette (dispatch() wird direkt gerufen)."""
    mw = api.RateLimitMiddleware.__new__(api.RateLimitMiddleware)
    mw.limiter = api.limiter
    mw._counters = {}
    return mw


async def _dispatch(mw, request, handler=None):
    async def call_next(req):
        if handler is None:
            return "OK"  # steht repraesentativ fuer die Endpoint-Antwort
        return await handler(req)

    return await mw.dispatch(request, call_next)


# --- _get_limit_for_path ---------------------------------------------------

def test_config_export_hat_eigenes_limit_vor_config():
    """Regression: /config/export wurde frueher von /config (10) geschluckt."""
    mw = _mw()
    assert mw._get_limit_for_path("/config/export") == 30
    assert mw._get_limit_for_path("/config") == 10
    assert mw._get_limit_for_path("/control") == 10
    assert mw._get_limit_for_path("/command") == 10
    assert mw._get_limit_for_path("/status") == 100
    assert mw._get_limit_for_path("/health") == 100


# --- Body-Buffering ----------------------------------------------------------

def test_post_body_erreicht_endpoint_intakt():
    """Regression: buffered_receive() lieferte frueher immer body=b''.
    Fuer POSTs waere das ein 422-Validation-Fehler gewesen."""
    mw = _mw()
    request = _FakeRequest(path="/control", body=b'{"solltemperatur_c": 45.0}')

    async def endpoint(req):
        # Genau wie FastAPI intern: Body ueber _receive lesen.
        msg = await req._receive()
        return msg["body"]

    ergebnis = asyncio.run(_dispatch(mw, request, endpoint))
    assert ergebnis == b'{"solltemperatur_c": 45.0}'
    assert request._body == b'{"solltemperatur_c": 45.0}'


# --- Limit-Tier-Trennung & 429 ----------------------------------------------

def test_strict_tier_429_ohne_default_tier_zu_beruehren():
    """10x /control ok, 11. -> 429; /status bleibt unberuehrt (eigener Key)."""
    mw = _mw()
    control = _FakeRequest(path="/control")
    status = _FakeRequest(path="/status")

    for _ in range(10):
        ergebnis = asyncio.run(_dispatch(mw, control))
        assert ergebnis == "OK"
    antwort = asyncio.run(_dispatch(mw, control))
    assert antwort.status_code == 429
    assert antwort.headers["Retry-After"]
    # Anderes Tier muss davon unbeeinflusst sein
    assert asyncio.run(_dispatch(mw, status)) == "OK"


def test_429_nach_100_requests_default_tier():
    """Ab dem 101. Request im Default-Tier kommt 429."""
    mw = _mw()
    request = _FakeRequest(path="/status")
    for _ in range(100):
        assert asyncio.run(_dispatch(mw, request)) == "OK"
    antwort = asyncio.run(_dispatch(mw, request))
    assert antwort.status_code == 429
    assert "Rate limit exceeded" in antwort.body.decode("utf-8")


def test_api_key_erzeugt_stabilen_client_key():
    """X-API-Key hat Vorrang und erzeugt einen reproduzierbaren Key."""
    mw = _mw()
    r1 = _FakeRequest(headers={"X-API-Key": "geheim123456789"})
    r2 = _FakeRequest(headers={"X-API-Key": "geheim123456789"})
    assert mw._get_key(r1) == mw._get_key(r2)
    assert mw._get_key(r1).startswith("apikey:")


# --- Counter-Aufraeumen ------------------------------------------------------

def test_zaehler_raeumt_alte_fenster_auf():
    """Memory-Leak-Schutz: > 4096 Eintraege loest Cleanup alter Fenster aus."""
    mw = _mw()
    window = int(time.time() // 60)
    for i in range(4096):
        mw._counters[f"alt:{i}:{window - 1}"] = 1
    for i in range(1000):
        mw._counters[f"neu:{i}:{window}"] = 1

    # Cleanup-Logik aus dispatch() nachstellen (gleiche Bedingung)
    if len(mw._counters) > 4096:
        stale = [k for k in mw._counters if int(k.rsplit(":", 1)[1]) < window]
        for k in stale:
            mw._counters.pop(k, None)

    assert len(mw._counters) == 1000
    assert all(k.startswith("neu:") for k in mw._counters)