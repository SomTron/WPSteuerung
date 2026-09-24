"""Regressionen fuer Zustandsuebernahme und shutdown-sichere Zyklen."""
from datetime import datetime
from types import SimpleNamespace

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
