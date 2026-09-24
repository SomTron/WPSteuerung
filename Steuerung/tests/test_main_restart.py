"""Regressionen fuer Zustandsuebernahme und shutdown-sichere Zyklen."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import pytz

import main


def _state():
    return SimpleNamespace(
        local_tz=pytz.timezone("Europe/Berlin"),
        stats=SimpleNamespace(last_compressor_off_time=None),
    )


def test_restore_persisted_compressor_pause_aus_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "last_state.txt"
    snapshot.write_text(
        "2026-09-24 07:00:00 | Komp=AUS | Regel=Keine Regel aktiv | "
        "Blocking=- | AUS_seit=2026-09-24 06:55:00 | T_oben=48.0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "LAST_STATE_FILE", str(snapshot))
    state = _state()

    assert main.restore_persisted_compressor_pause(state) is True
    assert state.stats.last_compressor_off_time == pytz.timezone(
        "Europe/Berlin"
    ).localize(datetime(2026, 9, 24, 6, 55))


def test_restore_persisted_compressor_pause_ignoriert_alten_snapshot(tmp_path, monkeypatch):
    snapshot = tmp_path / "last_state.txt"
    snapshot.write_text(
        "2026-09-01 07:00:00 | Komp=AUS | Regel=Keine Regel aktiv | "
        "Blocking=- | AUS_seit=2026-09-01 06:55:00\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "LAST_STATE_FILE", str(snapshot))
    state = _state()

    assert main.restore_persisted_compressor_pause(state) is False
    assert state.stats.last_compressor_off_time is None


@pytest.mark.asyncio
async def test_periodic_tasks_akzeptiert_naiven_vpn_timestamp(monkeypatch):
    """Ein alter naiver Snapshot darf den Main-Loop nicht abbrechen."""
    from main import check_periodic_tasks

    state = _state()
    state.config = SimpleNamespace()
    state.last_forecast_update = None
    state.last_forecast_attempt = None
    state.priority_config = SimpleNamespace(
        sommer_modus=SimpleNamespace(aktiv=False),
        legionellen=SimpleNamespace(aktiv=False),
    )
    state.solar = SimpleNamespace()
    calls = []
    monkeypatch.setattr(main, "check_vpn_status", AsyncMock(side_effect=lambda s: calls.append(s)))
    monkeypatch.setattr(
        main,
        "get_solar_forecast",
        AsyncMock(return_value=(None, None, None, None, None, None, None, None)),
    )
    monkeypatch.setattr(main, "FORECAST_RETRY_INTERVAL_MIN", 15)
    monkeypatch.setattr(main, "VPN_CHECK_INTERVAL_SEC", 60)

    result = await check_periodic_tasks(
        object(), state, datetime.now() - timedelta(minutes=1)
    )
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert len(calls) == 1