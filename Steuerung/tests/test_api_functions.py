import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock
import sys
import os

# Add parent directory to path to import api_server
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api_server import app, MockState

client = TestClient(app)

def test_root_endpoint():
    """Test the root endpoint"""
    response = client.get("/")
    assert response.status_code == 200
    
    data = response.json()
    assert data["name"] == "WPSteuerung API"
    assert data["version"] == "1.0.0"
    assert data["mode"] == "development"


def test_get_status():
    """Test the status endpoint"""
    response = client.get("/status")
    assert response.status_code == 200
    
    data = response.json()
    
    # Check temperature structure
    assert "temperatures" in data
    assert "oben" in data["temperatures"]
    assert "mittig" in data["temperatures"]
    assert "unten" in data["temperatures"]
    assert "verdampfer" in data["temperatures"]
    assert "boiler" in data["temperatures"]
    
    # Check compressor structure
    assert "compressor" in data
    assert "status" in data["compressor"]
    assert "runtime_current" in data["compressor"]
    assert "runtime_today" in data["compressor"]
    
    # Check setpoints structure
    assert "setpoints" in data
    assert "einschaltpunkt" in data["setpoints"]
    assert "ausschaltpunkt" in data["setpoints"]
    assert "sicherheits_temp" in data["setpoints"]
    assert "verdampfertemperatur" in data["setpoints"]
    
    # Check mode structure
    assert "mode" in data
    assert "current" in data["mode"]
    assert "solar_active" in data["mode"]
    assert "holiday_active" in data["mode"]
    assert "bath_active" in data["mode"]
    
    # Check energy structure
    assert "energy" in data
    assert "battery_power" in data["energy"]
    assert "soc" in data["energy"]
    assert "feed_in" in data["energy"]
    
    # Check system structure
    assert "system" in data
    assert "exclusion_reason" in data["system"]
    assert "last_update" in data["system"]
    assert "mode" in data["system"]


@patch('api_server.logging')
def test_update_config(mock_logging):
    """Test the config update endpoint"""
    from api_server import ConfigUpdate
    
    # Test valid config update
    config_data = {
        "section": "Heizungssteuerung",
        "key": "MIN_LAUFZEIT",
        "value": "20"
    }
    
    response = client.post("/config", json=config_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Updated Heizungssteuerung.MIN_LAUFZEIT to 20" in data["message"]
    assert "mock" in data["message"]


def test_control_system_force_on():
    """Test forcing compressor on"""
    from api_server import ControlCommand
    
    command_data = {
        "command": "force_on"
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Kompressor forced ON" in data["message"]
    assert "mock" in data["message"]


def test_control_system_force_off():
    """Test forcing compressor off"""
    command_data = {
        "command": "force_off"
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Kompressor forced OFF" in data["message"]
    assert "mock" in data["message"]


def test_control_system_set_bath_mode():
    """Test setting bath mode"""
    command_data = {
        "command": "set_mode",
        "params": {
            "mode": "bademodus",
            "active": True
        }
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Bademodus set to True" in data["message"]
    assert "mock" in data["message"]


def test_control_system_set_holiday_mode():
    """Test setting holiday mode"""
    command_data = {
        "command": "set_mode",
        "params": {
            "mode": "urlaubsmodus",
            "active": True
        }
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Urlaubsmodus set to True" in data["message"]
    assert "mock" in data["message"]


def test_control_system_unknown_mode():
    """Test setting an unknown mode"""
    command_data = {
        "command": "set_mode",
        "params": {
            "mode": "unknown_mode",
            "active": True
        }
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 400
    
    data = response.json()
    assert "detail" in data
    assert "Unknown mode: unknown_mode" in data["detail"]


def test_control_system_unknown_command():
    """Test sending an unknown command"""
    command_data = {
        "command": "unknown_command"
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 400
    
    data = response.json()
    assert "detail" in data
    assert "Unknown command: unknown_command" in data["detail"]


def test_get_history_default_hours():
    """Test getting history with default hours"""
    response = client.get("/history")
    assert response.status_code == 200
    
    data = response.json()
    assert "data" in data
    assert "count" in data
    assert isinstance(data["data"], list)
    assert isinstance(data["count"], int)
    assert data["count"] == len(data["data"])


def test_get_history_custom_hours():
    """Test getting history with custom hours"""
    response = client.get("/history?hours=12")
    assert response.status_code == 200
    
    data = response.json()
    assert "data" in data
    assert "count" in data
    assert isinstance(data["data"], list)
    assert isinstance(data["count"], int)
    assert data["count"] == len(data["data"])