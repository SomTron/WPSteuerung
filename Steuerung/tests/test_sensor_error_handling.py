import pytest
import asyncio
import logging
from unittest.mock import MagicMock, patch, AsyncMock, mock_open
import os
from sensors import SensorManager

@pytest.fixture
def sensor_manager():
    sm = SensorManager(base_dir="/tmp/fake_w1")
    sm.sensor_ids = {"test_sensor": "28-123"}
    sm.consecutive_failures = {"test_sensor": 0}
    return sm

def test_read_temperature_raw_file_not_found(sensor_manager, caplog):
    """Testet das Verhalten, wenn die Sensordatei nicht existiert."""
    with patch("os.path.exists", return_value=False):
        with caplog.at_level(logging.WARNING):
            result = sensor_manager.read_temperature_raw("28-123")
            assert result is None
            assert "Sensor-Datei nicht gefunden" in caplog.text

def test_read_temperature_raw_invalid_data(sensor_manager, caplog):
    """Testet das Verhalten mit ungültigen Daten (ValueError)."""
    # Simulate valid file existence but malformed content (cannot be parsed to float)
    with patch("os.path.exists", return_value=True):
        mock_file_content = ["YES\n", "t=invalid_data\n"]
        with patch("builtins.open", mock_open(read_data="".join(mock_file_content))):
            with caplog.at_level(logging.ERROR):
                result = sensor_manager.read_temperature_raw("28-123")
                assert result is None
                assert "Fehler beim Parsen der Temperatur" in caplog.text

@pytest.mark.asyncio
async def test_read_temperature_timeout_handling(sensor_manager, caplog):
    """Testet, dass Timeouts erst bei spaeteren Versuchen als Warning erzeugt werden."""
    with patch("asyncio.wait_for", new_callable=AsyncMock) as mock_wait_for:
        # 3 Versuche: 2x Timeout, 1x Erfolg
        mock_wait_for.side_effect = [asyncio.TimeoutError(), asyncio.TimeoutError(), 25.0]
        
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Check DEBUG level for Retry 1
            with caplog.at_level(logging.DEBUG):
                temp = await sensor_manager.read_temperature("test_sensor", retries=3)
                
                assert temp == 25.0
                assert mock_wait_for.call_count == 3
                assert mock_sleep.call_count == 2
                
                # Retry 1/3 sollte DEBUG sein
                assert any(record.levelname == 'DEBUG' and "Retry 1/3" in record.message for record in caplog.records)
                # Retry 2/3 sollte WARNING sein (da es der vorletzte Versuch ist, bevor es fehlschlägt, 
                # oder allgemeiner: wir haben es so implementiert, dass nur der LETZTE moegliche Retry vor dem finalen Fail ein Warning ist)
                # In der Implementierung: logs_level = DEBUG if attempt < retries - 2 else WARNING
                # Fuer retries=3:
                # attempt 0 (Retry 1): 0 < 1 -> DEBUG
                # attempt 1 (Retry 2): 1 < 1 -> False -> WARNING
                assert any(record.levelname == 'WARNING' and "Retry 2/3" in record.message for record in caplog.records)

@pytest.mark.asyncio
async def test_logging_levels(sensor_manager, caplog):
    """Testet, dass erfolgreiche Reads, CRC-Fehler und kritische Fehler richtig geloggt werden."""
    # Test erfolgreicher Read (kein Debug-Log mehr, um Log-Datei klein zu halten)
    caplog.clear()
    with patch("os.path.exists", return_value=True):
        mock_file_content_success = ["YES\n", "t=25000\n"]
        with patch("builtins.open", mock_open(read_data="".join(mock_file_content_success))):
            with caplog.at_level(logging.DEBUG):
                result = sensor_manager.read_temperature_raw("28-123")
                assert result == 25.0
                assert "Sensor 28-123 gelesen" not in caplog.text

    # Test CRC-Fehler (Warning)
    caplog.clear()
    with patch("os.path.exists", return_value=True):
        mock_file_content_crc = ["NO\n", "t=25000\n"]
        with patch("builtins.open", mock_open(read_data="".join(mock_file_content_crc))):
            with caplog.at_level(logging.WARNING):
                result = sensor_manager.read_temperature_raw("28-123")
                assert result is None
                assert "CRC-Fehler" in caplog.text

    # Test Critical Failure (Critical)
    caplog.clear()
    mock_raw = MagicMock(return_value=None)
    sensor_manager.max_consecutive_failures = 3
    with patch.object(SensorManager, "read_temperature_raw", mock_raw):
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with caplog.at_level(logging.CRITICAL):
                # 3 fehlschlagende Aufrufe
                for _ in range(3):
                    sensor_manager.last_sensor_readings.clear()
                    await sensor_manager.read_temperature("test_sensor", retries=1)
                
                assert sensor_manager.critical_failure is True
                assert "KRITISCH: Sensor test_sensor hat 3x hintereinander versagt!" in caplog.text
