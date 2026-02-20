import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from sensors import SensorManager

@pytest.mark.asyncio
async def test_read_temperature_retry_on_none():
    """Testet, ob read_temperature retryer, wenn read_temperature_raw None zurückgibt."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"test_sensor": "28-123"}
    sensor_manager.consecutive_failures = {"test_sensor": 0}
    
    # Mock read_temperature_raw: liefert zweimal None, dann einen Wert
    mock_raw = MagicMock(side_effect=[None, None, 25.5])
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        # AsyncMock für sleep
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            temp = await sensor_manager.read_temperature("test_sensor", retries=3)
            
            assert temp == 25.5
            assert mock_raw.call_count == 3
            assert mock_sleep.call_count == 2

@pytest.mark.asyncio
async def test_read_temperature_final_failure():
    """Testet, ob read_temperature nach allen Versuchen None zurückgibt."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"test_sensor": "28-123"}
    sensor_manager.consecutive_failures = {"test_sensor": 0}
    
    # Mock read_temperature_raw: liefert immer None
    mock_raw = MagicMock(return_value=None)
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            temp = await sensor_manager.read_temperature("test_sensor", retries=3)
            
            assert temp is None
            assert mock_raw.call_count == 3
            assert mock_sleep.call_count == 2

@pytest.mark.asyncio
async def test_read_temperature_timeout_retry():
    """Testet, ob read_temperature bei Timeout retryer."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"test_sensor": "28-123"}
    sensor_manager.consecutive_failures = {"test_sensor": 0}
    
    with patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for:
        mock_wait_for.side_effect = [asyncio.TimeoutError, 26.0]
        
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            temp = await sensor_manager.read_temperature("test_sensor", retries=2)
            
            assert temp == 26.0
            assert mock_wait_for.call_count == 2
            assert mock_sleep.call_count == 1

# --- Neue Tests für Consecutive-Failure-Tracking ---

@pytest.mark.asyncio
async def test_consecutive_failure_counter():
    """Testet, dass der Failure-Counter nach jedem Fehlschlag hochzählt."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"oben": "28-123"}
    sensor_manager.consecutive_failures = {"oben": 0}
    
    mock_raw = MagicMock(return_value=None)
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        with patch('asyncio.sleep', new_callable=AsyncMock):
            # Erster Fehlschlag
            await sensor_manager.read_temperature("oben", retries=1)
            assert sensor_manager.consecutive_failures["oben"] == 1
            
            # Zweiter Fehlschlag
            sensor_manager.last_sensor_readings.clear()  # Cache leeren
            await sensor_manager.read_temperature("oben", retries=1)
            assert sensor_manager.consecutive_failures["oben"] == 2
            
            # Dritter Fehlschlag
            sensor_manager.last_sensor_readings.clear()
            await sensor_manager.read_temperature("oben", retries=1)
            assert sensor_manager.consecutive_failures["oben"] == 3

@pytest.mark.asyncio
async def test_consecutive_failure_reset_on_success():
    """Testet, dass der Counter bei Erfolg auf 0 zurückgesetzt wird."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"oben": "28-123"}
    sensor_manager.consecutive_failures = {"oben": 3}  # Schon 3 Fehler
    
    mock_raw = MagicMock(return_value=42.5)
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        with patch('asyncio.sleep', new_callable=AsyncMock):
            temp = await sensor_manager.read_temperature("oben", retries=1)
            
            assert temp == 42.5
            assert sensor_manager.consecutive_failures["oben"] == 0

@pytest.mark.asyncio
async def test_critical_failure_flag():
    """Testet, dass critical_failure nach max_consecutive_failures gesetzt wird."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"oben": "28-123"}
    sensor_manager.consecutive_failures = {"oben": 0}
    sensor_manager.max_consecutive_failures = 3  # Niedriger für Test
    
    mock_raw = MagicMock(return_value=None)
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        with patch('asyncio.sleep', new_callable=AsyncMock):
            # 1. und 2. Fehlschlag: noch kein critical_failure
            for i in range(2):
                sensor_manager.last_sensor_readings.clear()
                await sensor_manager.read_temperature("oben", retries=1)
                assert sensor_manager.critical_failure is False
            
            # 3. Fehlschlag: critical_failure wird gesetzt
            sensor_manager.last_sensor_readings.clear()
            await sensor_manager.read_temperature("oben", retries=1)
            assert sensor_manager.critical_failure is True
            assert sensor_manager.critical_failure_sensor == "oben"

@pytest.mark.asyncio
async def test_cache_uses_sensor_key():
    """Testet, dass der Cache sensor_key statt sensor_id (Hardware-ID) verwendet."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"oben": "28-abc123"}
    sensor_manager.consecutive_failures = {"oben": 0}
    
    mock_raw = MagicMock(return_value=35.0)
    
    with patch.object(SensorManager, 'read_temperature_raw', mock_raw):
        with patch('asyncio.sleep', new_callable=AsyncMock):
            temp = await sensor_manager.read_temperature("oben", retries=1)
            
            assert temp == 35.0
            # Cache muss mit sensor_key "oben" gespeichert sein, nicht "28-abc123"
            assert "oben" in sensor_manager.last_sensor_readings
            assert "28-abc123" not in sensor_manager.last_sensor_readings
