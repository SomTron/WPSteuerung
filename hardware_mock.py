import logging
from typing import List, Dict
from hardware_interface import HardwareInterface


class MockHardwareManager(HardwareInterface):
    """Mock hardware implementation for testing on non-Raspberry Pi platforms."""
    
    def __init__(self, i2c_addr=0x27, i2c_bus=1):
        self.GIO21_PIN = 21
        self.PRESSURE_SENSOR_PIN = 17
        self.i2c_addr = i2c_addr
        self.i2c_bus = i2c_bus
        
        # Mock state tracking
        self.gpio_initialized = False
        self.lcd_initialized = False
        self.compressor_state = False
        self.pressure_sensor_value = True  # Default: OK
        self.lcd_content: List[str] = ["", "", "", ""]
        self.gpio_history: List[Dict] = []  # Track all GPIO changes
        self.lcd_history: List[List[str]] = []  # Track all LCD writes
    
    def init_gpio(self) -> None:
        """Mock GPIO initialization."""
        self.gpio_initialized = True
        logging.info("Mock GPIO initialized")
    
    async def init_lcd(self) -> None:
        """Mock LCD initialization."""
        self.lcd_initialized = True
        self.lcd_content = ["", "", "", ""]
        logging.info("Mock LCD initialized")
    
    def set_compressor_state(self, state: bool) -> None:
        """Mock compressor control with state tracking."""
        if self.gpio_initialized:
            old_state = self.compressor_state
            self.compressor_state = state
            self.gpio_history.append({
                "pin": self.GIO21_PIN,
                "state": state,
                "previous": old_state
            })
            logging.debug(f"Mock compressor: {old_state} -> {state}")
    
    def read_pressure_sensor(self) -> bool:
        """Mock pressure sensor reading."""
        if self.gpio_initialized:
            return self.pressure_sensor_value
        return True  # Default: OK
    
    def write_lcd(self, line1: str = "", line2: str = "", line3: str = "", line4: str = "") -> None:
        """Mock LCD write with content tracking."""
        if self.lcd_initialized:
            self.lcd_content = [
                line1.ljust(20)[:20],
                line2.ljust(20)[:20],
                line3.ljust(20)[:20],
                line4.ljust(20)[:20]
            ]
            self.lcd_history.append(self.lcd_content.copy())
            logging.debug(f"Mock LCD: {self.lcd_content}")
    
    def cleanup(self) -> None:
        """Mock cleanup."""
        if self.gpio_initialized:
            self.compressor_state = False
            self.gpio_initialized = False
            logging.info("Mock GPIO cleanup")
        
        if self.lcd_initialized:
            self.lcd_content = ["System aus", "", "", ""]
            self.lcd_initialized = False
            logging.info("Mock LCD cleanup")
    
    # Test helper methods
    def set_pressure_sensor_value(self, value: bool) -> None:
        """Test helper: Set mock pressure sensor value."""
        self.pressure_sensor_value = value
    
    def get_compressor_state(self) -> bool:
        """Test helper: Get current compressor state."""
        return self.compressor_state
    
    def get_lcd_content(self) -> List[str]:
        """Test helper: Get current LCD content."""
        return self.lcd_content.copy()
    
    def clear_history(self) -> None:
        """Test helper: Clear all tracked history."""
        self.gpio_history.clear()
        self.lcd_history.clear()
