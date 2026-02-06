import pytest
from hardware_mock import MockHardwareManager


class TestMockHardware:
    """Tests for the mock hardware implementation."""
    
    @pytest.fixture
    def mock_hw(self):
        """Create a fresh mock hardware instance."""
        return MockHardwareManager()
    
    def test_gpio_initialization(self, mock_hw):
        """Test GPIO initialization tracking."""
        assert mock_hw.gpio_initialized is False
        
        mock_hw.init_gpio()
        
        assert mock_hw.gpio_initialized is True
    
    @pytest.mark.asyncio
    async def test_lcd_initialization(self, mock_hw):
        """Test LCD initialization tracking."""
        assert mock_hw.lcd_initialized is False
        
        await mock_hw.init_lcd()
        
        assert mock_hw.lcd_initialized is True
        assert mock_hw.lcd_content == ["", "", "", ""]
    
    def test_compressor_state_tracking(self, mock_hw):
        """Test that compressor state changes are tracked."""
        mock_hw.init_gpio()
        
        # Initially off
        assert mock_hw.get_compressor_state() is False
        assert len(mock_hw.gpio_history) == 0
        
        # Turn on
        mock_hw.set_compressor_state(True)
        assert mock_hw.get_compressor_state() is True
        assert len(mock_hw.gpio_history) == 1
        assert mock_hw.gpio_history[0]["state"] is True
        assert mock_hw.gpio_history[0]["previous"] is False
        
        # Turn off
        mock_hw.set_compressor_state(False)
        assert mock_hw.get_compressor_state() is False
        assert len(mock_hw.gpio_history) == 2
        assert mock_hw.gpio_history[1]["state"] is False
        assert mock_hw.gpio_history[1]["previous"] is True
    
    def test_pressure_sensor_injection(self, mock_hw):
        """Test that pressure sensor values can be injected for testing."""
        mock_hw.init_gpio()
        
        # Default: OK
        assert mock_hw.read_pressure_sensor() is True
        
        # Inject failure
        mock_hw.set_pressure_sensor_value(False)
        assert mock_hw.read_pressure_sensor() is False
        
        # Restore
        mock_hw.set_pressure_sensor_value(True)
        assert mock_hw.read_pressure_sensor() is True
    
    @pytest.mark.asyncio
    async def test_lcd_content_tracking(self, mock_hw):
        """Test that LCD writes are tracked."""
        await mock_hw.init_lcd()
        
        # Write to LCD
        mock_hw.write_lcd("Line 1", "Line 2", "Line 3", "Line 4")
        
        content = mock_hw.get_lcd_content()
        assert content[0].strip() == "Line 1"
        assert content[1].strip() == "Line 2"
        assert content[2].strip() == "Line 3"
        assert content[3].strip() == "Line 4"
        
        # Check history
        assert len(mock_hw.lcd_history) == 1
    
    @pytest.mark.asyncio
    async def test_lcd_line_truncation(self, mock_hw):
        """Test that LCD lines are truncated to 20 chars."""
        await mock_hw.init_lcd()
        
        long_line = "This is a very long line that exceeds 20 characters"
        mock_hw.write_lcd(long_line)
        
        content = mock_hw.get_lcd_content()
        assert len(content[0]) == 20
        assert content[0] == long_line[:20]
    
    def test_cleanup_resets_state(self, mock_hw):
        """Test that cleanup resets hardware state."""
        mock_hw.init_gpio()
        mock_hw.set_compressor_state(True)
        
        assert mock_hw.get_compressor_state() is True
        assert mock_hw.gpio_initialized is True
        
        mock_hw.cleanup()
        
        assert mock_hw.get_compressor_state() is False
        assert mock_hw.gpio_initialized is False
    
    def test_history_clearing(self, mock_hw):
        """Test that history can be cleared for test isolation."""
        mock_hw.init_gpio()
        mock_hw.set_compressor_state(True)
        mock_hw.set_compressor_state(False)
        
        assert len(mock_hw.gpio_history) == 2
        
        mock_hw.clear_history()
        
        assert len(mock_hw.gpio_history) == 0
        assert len(mock_hw.lcd_history) == 0
