"""Regressionen fuer die getrennten Main-Loop-Phasen."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _state(*, compressor=False):
    return SimpleNamespace(
        local_tz=__import__("pytz").timezone("Europe/Berlin"),
        sensors=SimpleNamespace(
            t_oben=45.0, t_unten=40.0, t_mittig=42.0, t_verd=12.0, t_boiler=45.0
        ),
        control=SimpleNamespace(
            kompressor_ein=compressor,
            blocking_reason=None,
            consecutive_control_errors=0,
            manual_force_on_pending=False,
        ),
        learning_engine=object(),
    )


@pytest.mark.asyncio
async def test_periodic_phase_fehler_behaelt_letzten_vpn_timestamp():
    import main

    old = object()
    with patch.object(main, "check_periodic_tasks", AsyncMock(side_effect=RuntimeError("boom"))):
        result = await main._run_periodic_phase(object(), _state(), old)
    assert result is old


@pytest.mark.asyncio
async def test_data_phase_fehler_macht_sensoren_fail_safe():
    import main

    state = _state()
    with patch.object(main, "update_system_data", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await main._run_data_phase(object(), state) is False
    assert state.sensors.t_oben is None
    assert state.sensors.t_unten is None
    assert state.last_data_update_ok is False


@pytest.mark.asyncio
async def test_control_phase_ruft_regelung_trotz_optionalem_periodic_fehler():
    import main

    state = _state()
    with patch.object(main, "run_logic_step", AsyncMock()) as run_logic:
        with patch.object(main, "set_kompressor_status", AsyncMock()) as set_status:
            await main._run_control_phase(object(), state, True)
    run_logic.assert_awaited_once()
    set_status.assert_not_awaited()
    assert state.control.consecutive_control_errors == 0


@pytest.mark.asyncio
async def test_controlfehler_schaltet_laufenden_kompressor_fail_safe_aus():
    import main

    state = _state(compressor=True)
    with patch.object(main, "run_logic_step", AsyncMock(side_effect=RuntimeError("boom"))):
        with patch.object(main, "set_kompressor_status", AsyncMock(return_value=True)) as set_status:
            await main._run_control_phase(object(), state, True)
    set_status.assert_awaited_once_with(
        state, False, force=True, end_grund="regelungsfehler"
    )
    assert state.control.consecutive_control_errors == 1
    assert state.control.blocking_reason == "Regelungsfehler"


@pytest.mark.asyncio
async def test_sensor_update_fehler_ueberspringt_regelung_und_schaltet_aus():
    import main

    state = _state(compressor=True)
    with patch.object(main, "run_logic_step", AsyncMock()) as run_logic:
        with patch.object(main, "set_kompressor_status", AsyncMock(return_value=True)) as set_status:
            await main._run_control_phase(object(), state, False)
    run_logic.assert_not_awaited()
    set_status.assert_awaited_once_with(
        state, False, force=True, end_grund="sensor_update_fehler"
    )
    assert state.control.blocking_reason == "Sensor-Update fehlgeschlagen"
