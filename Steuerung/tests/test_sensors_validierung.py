"""Validierung grenzwertiger und nicht-endlicher DS18B20-Messwerte."""
from types import SimpleNamespace
import pytz
import pytest

from sensors import SensorManager
from safety_logic import check_for_sensor_errors


def _sensor_datei(mgr, tmp_path, inhalt):
    sensor_id = mgr.sensor_ids["oben"]
    datei = tmp_path / sensor_id / "w1_slave"
    datei.parent.mkdir()
    datei.write_text(inhalt, encoding="utf-8")
    return datei


def test_nan_wird_abgewiesen(tmp_path):
    mgr = SensorManager(base_dir=str(tmp_path))
    sensor_id = mgr.sensor_ids["oben"]
    _sensor_datei(mgr, tmp_path, "YES\nt=nan\n")

    assert mgr.read_temperature_raw(sensor_id) is None
    assert mgr.sensor_error_counts[sensor_id] == 1


def test_unendliche_aussreiter_werden_abgewiesen(tmp_path):
    mgr = SensorManager(base_dir=str(tmp_path))
    sensor_id = mgr.sensor_ids["oben"]
    _sensor_datei(mgr, tmp_path, "YES\nt=inf\n")

    assert mgr.read_temperature_raw(sensor_id) is None
    assert mgr.sensor_error_counts[sensor_id] == 1


@pytest.mark.asyncio
async def test_mittelfuehler_wird_von_der_sicherheitspruefung_abgewiesen():
    state = SimpleNamespace(
        local_tz=pytz.timezone("Europe/Berlin"),
        control=SimpleNamespace(blocking_reason=None),
        last_sensor_error_time=None,
    )
    assert await check_for_sensor_errors(None, state, 45.0, 41.0, float("nan")) is False
    assert "T_Mittig" in state.control.blocking_reason


@pytest.mark.asyncio
async def test_sensorfehler_werden_nur_einmal_pro_intervall_geloggt(caplog):
    import logging

    state = SimpleNamespace(
        local_tz=pytz.timezone("Europe/Berlin"),
        control=SimpleNamespace(blocking_reason=None),
        last_sensor_error_time=None,
    )
    with caplog.at_level(logging.ERROR):
        assert await check_for_sensor_errors(None, state, 45.0, 41.0, float("nan")) is False
        assert await check_for_sensor_errors(None, state, 45.0, 41.0, float("nan")) is False
    sensor_logs = [record for record in caplog.records if "Sensorfehler:" in record.getMessage()]
    assert len(sensor_logs) == 1
