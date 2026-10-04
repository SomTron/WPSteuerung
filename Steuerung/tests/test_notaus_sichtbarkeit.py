"""Sichtbarkeit des Not-Aus-Zustands in /health und in der Statuszeile.

Anlass (Befund 03./04.10.2026): Die Anlage stand 12,5 Stunden unter einer
Not-Aus-Sperre (notaus.lock vom 03.10. 23:09:23). Waehrend dieser Zeit:

- `/health` meldete "ok" - keine Fehler, frischer Heartbeat, keine Persistenz-
  stoerung. Die Sperre wurde gar nicht geprueft.
- Die Statuszeile sah normal aus: kein `Blocking:`, kein `Regel:`.
- `consecutive_control_errors` blieb 0.

Das Regelwerk lief in dieser Zeit nicht. Ein gesperrtes System ist kein
Fehlerfall, sondern ein Zustand - und wurde als Zustand nirgends erfasst.
"""
import logging
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

TZ = datetime.now().astimezone().tzinfo


def _state_fuer_health(notaus=False):
    """Minimaler State, wie ihn `test_stability_boundaries` auch baut."""
    jetzt = datetime.now(TZ)
    return SimpleNamespace(
        local_tz=TZ,
        loop_heartbeat=jetzt,
        last_control_success=jetzt,
        last_sensor_success=jetzt,
        last_status_snapshot_at=jetzt,
        last_data_update_ok=True,
        last_state_write_ok=True,
        last_state_write_error=None,
        control=SimpleNamespace(
            consecutive_control_errors=0,
            notaus_aktiv=notaus,
            notaus_grund="Telegram" if notaus else None,
            notaus_ts="2026-10-03T23:09:23" if notaus else None,
        ),
    )


def _health(state):
    import api
    alt = (api.shared_state, api.control_state, api.control_funcs)
    try:
        api.init_api(state, {})
        return api.health_status()
    finally:
        api.shared_state, api.control_state, api.control_funcs = alt


def test_health_meldet_sperre_als_degraded():
    """Kern-Regression: Aktive Sperre darf nicht als 'ok' durchgehen."""
    result = _health(_state_fuer_health(notaus=True))
    assert result["status"] == "degraded"
    assert result["notaus_aktiv"] is True


def test_health_ohne_sperre_bleibt_ok():
    result = _health(_state_fuer_health(notaus=False))
    assert result["status"] == "ok"
    assert result["notaus_aktiv"] is False


def test_health_liefert_grund_und_zeitpunkt():
    """Ohne Grund und Zeitpunkt laesst sich eine Sperre nicht zuordnen."""
    result = _health(_state_fuer_health(notaus=True))
    assert result["notaus_grund"] == "Telegram"
    assert result["notaus_ts"] == "2026-10-03T23:09:23"


def test_health_meldet_sperre_auch_bei_fehlern():
    """Beide Signale gleichzeitig - die Sperre darf nicht untergehen."""
    state = _state_fuer_health(notaus=True)
    state.control.consecutive_control_errors = 3
    result = _health(state)
    assert result["status"] == "degraded"
    assert result["notaus_aktiv"] is True
    assert result["consecutive_control_errors"] == 3


def _state_fuer_statuszeile(notaus=False):
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(t_oben=48.0, t_mittig=43.0, t_unten=46.0,
                                t_verd=12.0),
        control=SimpleNamespace(
            kompressor_ein=False,
            aktueller_einschaltpunkt=42.0, aktueller_ausschaltpunkt=48.0,
            blocking_reason=None, active_rule_name=None,
            previous_modus="Keine Regel aktiv",
            notaus_aktiv=notaus,
            notaus_grund="Telegram" if notaus else None),
        solar=SimpleNamespace(
            acpower=None, feedinpower=None, soc=None,
            last_api_call=datetime.now(TZ) - timedelta(seconds=30)),
    )


async def _statuszeile(state, monkeypatch, caplog, tmp_path):
    import main
    monkeypatch.setattr(main, "check_log_throttle", lambda *a, **k: True)
    monkeypatch.setattr(main, "hardware_manager",
                        SimpleNamespace(write_lcd=lambda *a, **k: None))
    monkeypatch.setattr(main, "HEIZUNGSDATEN_CSV", str(tmp_path / "heiz.csv"))
    monkeypatch.setattr(main, "build_heizungsdaten_zeile", lambda s: ["1", "2"])
    monkeypatch.setattr(main, "write_last_state_snapshot", lambda s: None)
    with caplog.at_level(logging.INFO):
        await main.log_system_state(state)
    zeilen = [r.message for r in caplog.records if r.message.startswith("Status:")]
    assert zeilen
    return zeilen[0]


@pytest.mark.asyncio
async def test_statuszeile_zeigt_notaus_marker(monkeypatch, caplog, tmp_path):
    """Die Sperre muss in der Journal-Zeile stehen - sie setzt keinen
    blocking_reason und war dadurch 12,5 Stunden lang unsichtbar."""
    zeile = await _statuszeile(_state_fuer_statuszeile(notaus=True),
                               monkeypatch, caplog, tmp_path)
    assert "NOT-AUS: Telegram" in zeile


@pytest.mark.asyncio
async def test_statuszeile_ohne_sperre_unveraendert(monkeypatch, caplog, tmp_path):
    """Ohne Sperre darf kein NOT-AUS-Marker erscheinen."""
    zeile = await _statuszeile(_state_fuer_statuszeile(notaus=False),
                               monkeypatch, caplog, tmp_path)
    assert "NOT-AUS" not in zeile


if __name__ == "__main__":
    pytest.main([__file__, "-v"])