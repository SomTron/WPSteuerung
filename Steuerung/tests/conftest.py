import sys
import os
from unittest.mock import MagicMock
import pytest

# Add the project root to the python path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# --- MOCK HARDWARE MODULES BEFORE IMPORTING APP CODE ---

# Mock RPi.GPIO
mock_gpio = MagicMock()
mock_gpio.BCM = "BCM"
mock_gpio.OUT = "OUT"
mock_gpio.IN = "IN"
mock_gpio.HIGH = 1
mock_gpio.LOW = 0
mock_gpio.getmode.return_value = None # Default to None
sys.modules["RPi"] = MagicMock()
sys.modules["RPi.GPIO"] = mock_gpio

# Mock smbus2
sys.modules["smbus2"] = MagicMock()

# Mock RPLCD
mock_rplcd = MagicMock()
sys.modules["RPLCD"] = mock_rplcd
sys.modules["RPLCD.i2c"] = mock_rplcd

# Mock w1thermsensor (just in case)
sys.modules["w1thermsensor"] = MagicMock()

@pytest.fixture
def mock_aioresponse():
    """Fixture to mock aiohttp responses if needed."""
    with MagicMock() as mock:
        yield mock
