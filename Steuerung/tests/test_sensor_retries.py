import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
from sensors import SensorManager

@pytest.mark.asyncio
async def test_read_temperature_retry_on_none():
    """Testet, ob read_temperature retryer, wenn read_temperature_raw None zurückgibt."""
    sensor_manager = SensorManager(base_dir="/tmp/fake_w1")
    sensor_manager.sensor_ids = {"test_sensor": "28-123"}
    
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
    
    with patch('asyncio.wait_for', new_callable=AsyncMock) as mock_wait_for:
        mock_wait_for.side_effect = [asyncio.TimeoutError, 26.0]
        
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            temp = await sensor_manager.read_temperature("test_sensor", retries=2)
            
            assert temp == 26.0
            assert mock_wait_for.call_count == 2
            assert mock_sleep.call_count == 1
