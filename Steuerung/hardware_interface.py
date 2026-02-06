from abc import ABC, abstractmethod
from typing import Optional


class HardwareInterface(ABC):
    """Abstract base class defining the hardware interface contract."""
    
    @abstractmethod
    def init_gpio(self) -> None:
        """Initialize GPIO pins."""
        pass
    
    @abstractmethod
    async def init_lcd(self) -> None:
        """Initialize LCD display."""
        pass
    
    @abstractmethod
    def set_compressor_state(self, state: bool) -> None:
        """
        Set compressor state.
        
        Args:
            state: True to turn on, False to turn off
        """
        pass
    
    @abstractmethod
    def read_pressure_sensor(self) -> bool:
        """
        Read pressure sensor state.
        
        Returns:
            True if pressure OK, False if error
        """
        pass
    
    @abstractmethod
    def write_lcd(self, line1: str = "", line2: str = "", line3: str = "", line4: str = "") -> None:
        """
        Write text to LCD display.
        
        Args:
            line1-4: Text for each line (max 20 chars)
        """
        pass
    
    @abstractmethod
    def cleanup(self) -> None:
        """Clean up hardware resources."""
        pass
