"""Tests fuer Snapshot-, Aktuator- und Netzwerk-Grenzen."""
import asyncio
import csv
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytz

from atomic_io import atomic_write_json
from clock import Clock
from hardware_actuator import CompressorActuator
from status_snapshot import build_status_snapshot
from api_contract import (
    API_CONTRACT_VERSION,
    STATUS_SCHEMA_VERSION,
    validate_status_shape,
    with_contract_metadata,
)
from runtime_validation import (
    RuntimeContractError,
    validate_runtime_contract,
    validate_startup_dependencies,
)


def _minimal_state():
    return SimpleNamespace(
        local_tz=pytz.timezone("Europe/Berlin"),
        sensors=SimpleNamespace(t_oben=45.0, t_unten=40.0, t_mittig=42.0, t_verd=12.0, t_boiler=45.0),
        solar=SimpleNamespace(feedinpower=100.0, batpower=0.0, soc=80.0, acpower=200.0),
        control=SimpleNamespace(
            kompressor_ein=False, blocking_reason=None, effective_rule_name="Keine",
            alle_ergebnisse=[],
        ),
        stats=SimpleNamespace(current_runtime=timedelta(), total_runtime_today=timedelta()),
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        sommer_modus_zaehler=0,
        config=SimpleNamespace(),
        priority_config=SimpleNamespace(),
    )


def test_atomic_json_ersetzt_datei_ohne_tmp_reste(tmp_path):
    target = tmp_path / "status.json"
    atomic_write_json(str(target), {"gut": True})
    atomic_write_json(str(target), {"gut": False, "wert": 2})
    assert target.read_text(encoding="utf-8").endswith("\n")
    assert '"gut": false' in target.read_text(encoding="utf-8")
    assert not list(tmp_path.glob("*.tmp"))


def test_csv_tail_meldet_ungueltige_und_null_zeilen(tmp_path):
    import api

    target = tmp_path / "daten.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Zeitstempel", "Wert"])
        writer.writerow(["2026-09-24 10:00:00", "1"])
        handle.write("2026-09-24 10:00:01,2\x00\n")
        handle.write("2026-09-24 10:00:02\n")
    rows, quality = api._read_csv_tail(str(target))
    assert len(rows) == 1
    assert quality["invalid_rows"] == 2
    assert quality["null_bytes"] == 1


def test_startup_dependencies_importieren():
    validate_startup_dependencies()


def test_development_api_uses_production_app():
    import api as production_api
    import api_server

    assert api_server.app is production_api.app
    assert api_server.ControlCommand is production_api.ControlCommand
    assert api_server.ConfigUpdate is production_api.ConfigUpdate


def test_status_contract_ist_versioniert_und_stabil():
    payload = with_contract_metadata({
        "temperatures": {}, "compressor": {}, "setpoints": {}, "mode": {},
        "energy": {}, "system": {}, "priority": {}, "regel_ergebnisse": [],
        "status_indikatoren": {},
    })
    validate_status_shape(payload)
    assert payload["_contract"] == {
        "api_version": API_CONTRACT_VERSION,
        "status_schema_version": STATUS_SCHEMA_VERSION,
    }


def test_status_contract_weist_fehlende_schluessel_zurueck():
    with pytest.raises(ValueError, match="fehlen"):
        validate_status_shape(with_contract_metadata({"temperatures": {}}))


def test_runtime_contract_prueft_state_und_substates():
    state = _minimal_state()
    validate_runtime_contract(state)
    del state.control.kompressor_ein
    with pytest.raises(RuntimeContractError, match="control-Felder fehlen"):
        validate_runtime_contract(state)


def test_health_endpoint_zeigt_heartbeat_und_control_errors():
    import api

    state = _minimal_state()
    state.control.consecutive_control_errors = 0
    state.loop_heartbeat = datetime.now(state.local_tz)
    state.last_control_success = state.loop_heartbeat
    state.last_sensor_success = state.loop_heartbeat
    state.last_status_snapshot_at = state.loop_heartbeat
    state.last_data_update_ok = True
    old_state, old_control, old_funcs = api.shared_state, api.control_state, api.control_funcs
    try:
        api.init_api(state, {})
        result = api.health_status()
    finally:
        api.shared_state, api.control_state, api.control_funcs = old_state, old_control, old_funcs
    assert result["status"] == "ok"
    assert result["consecutive_control_errors"] == 0
    assert result["data_update_ok"] is True


def test_persistenzfehler_macht_health_degraded(monkeypatch):
    import api
    import main

    state = _minimal_state()
    state.loop_heartbeat = datetime.now(state.local_tz)
    state.last_data_update_ok = True
    state.last_state_write_ok = None
    state.last_state_write_error = None

    def kaputt(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(main, "atomic_write_text", kaputt)
    main.write_last_state_snapshot(state)
    assert state.last_state_write_ok is False
    assert "OSError" in state.last_state_write_error

    old_state, old_control, old_funcs = api.shared_state, api.control_state, api.control_funcs
    try:
        api.init_api(state, {})
        result = api.health_status()
    finally:
        api.shared_state, api.control_state, api.control_funcs = old_state, old_control, old_funcs
    assert result["status"] == "degraded"
    assert result["last_state_write_ok"] is False
    assert "OSError" in result["last_state_write_error"]


def test_clock_liefert_aware_und_monotone_zeit():
    clock = Clock(pytz.timezone("Europe/Berlin"))
    assert clock.now().tzinfo is not None
    assert clock.monotonic() <= clock.monotonic()


def test_api_init_verwendet_status_snapshot():
    import api

    state = _minimal_state()
    old_state, old_control, old_funcs = api.shared_state, api.control_state, api.control_funcs
    try:
        api.init_api(state, {})
        assert api.shared_state._is_status_snapshot is True
        state.sensors.t_unten = 5.0
        assert api.shared_state.sensors.t_unten == 40.0
        assert api.control_state is state
    finally:
        api.shared_state, api.control_state, api.control_funcs = old_state, old_control, old_funcs


def test_forecast_stale_setzt_kein_alter_und_enthaelt_plan():
    import priority_control_logic as pcl
    state = _minimal_state()
    state.last_forecast_update = datetime.now(state.local_tz)
    state.solar.last_api_call = datetime.now(state.local_tz) - timedelta(hours=1)
    state.solar.feedinpower = 100.0
    state.solar.forecast_today = 3.0
    result = pcl._set_stale_forecast(state)
    assert result is None
    assert state.forecast_stale is True
    assert state.forecast_age_s is None


def test_status_snapshot_enthaelt_nur_kompakte_lernsummary():
    state = _minimal_state()
    state.learning_engine = SimpleNamespace(
        get_info=lambda: {
            "total_cycles": 7,
            "total_usage_events": 4,
            "forecast_ratio": 1.1,
            "forecast_ratio_samples": 3,
            "internal_large_payload": "x" * 10000,
        }
    )
    snapshot = build_status_snapshot(state)
    assert not hasattr(snapshot, "learning_engine")
    assert snapshot.learning_engine_summary["total_cycles"] == 7
    assert "internal_large_payload" not in snapshot.learning_engine_summary


def test_status_snapshot_is_consistent_after_live_mutation():
    state = _minimal_state()
    snapshot = build_status_snapshot(state)
    state.sensors.t_unten = 10.0
    state.control.kompressor_ein = True
    state.solar.feedinpower = 9999.0
    assert snapshot.sensors.t_unten == 40.0
    assert snapshot.control.kompressor_ein is False
    assert snapshot.solar.feedinpower == 100.0


@pytest.mark.asyncio
async def test_compressor_actuator_serializes_and_reports_failure():
    class Hardware:
        def __init__(self):
            self.calls = []

        def set_compressor_state(self, value):
            self.calls.append(value)
            return False

    hardware = Hardware()
    actuator = CompressorActuator(hardware, asyncio.Lock())
    assert await actuator.set_state(True) is False
    assert hardware.calls == [True]


@pytest.mark.asyncio
async def test_solar_refresh_loop_applies_deadline_without_blocking_forever(monkeypatch):
    import main

    started = asyncio.Event()

    async def slow_fetch(*args):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(main, "get_solax_data", slow_fetch)
    monkeypatch.setattr(main, "SOLAR_REFRESH_DEADLINE_SEC", 0.01)
    task = asyncio.create_task(main.solar_refresh_loop(object(), _minimal_state()))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_main_data_phase_can_skip_network_refresh():
    import main

    state = _minimal_state()
    with patch("main.sensor_manager") as sensor_manager:
        sensor_manager.get_all_temperatures = AsyncMock(
            return_value={"oben": 45.0, "unten": 40.0, "mittig": 42.0, "verd": 12.0}
        )
        with patch("main.get_solax_data", new=AsyncMock()) as fetch:
            assert await main.update_system_data(object(), state, refresh_solar=False) is None
    fetch.assert_not_awaited()
