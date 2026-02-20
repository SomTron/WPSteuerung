import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime
import sys
import os

# Add parent directory to path to import api
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from api import app, ConfigUpdate, ControlCommand

# Setup mock state for testing
mock_state = MagicMock()
mock_state.sensors.t_oben = 42.5
mock_state.sensors.t_mittig = 41.0
mock_state.sensors.t_unten = 39.5
mock_state.sensors.t_verd = 10.2
mock_state.sensors.t_vorlauf = 35.0
mock_state.sensors.t_boiler = 41.0

mock_state.control.kompressor_ein = False
mock_state.control.aktueller_einschaltpunkt = 40.0
mock_state.control.aktueller_ausschaltpunkt = 50.0
mock_state.sicherheits_temp = 60.0
mock_state.verdampfertemperatur = -10.0
mock_state.control.previous_modus = "Normalmodus"
mock_state.control.solar_ueberschuss_aktiv = False
mock_state.urlaubsmodus_aktiv = False
mock_state.bademodus_aktiv = False
mock_state.control.ausschluss_grund = None

mock_state.solar.batpower = 250
mock_state.solar.soc = 75
mock_state.solar.feedinpower = 100

mock_state.stats.last_runtime = "0:15:00"
mock_state.stats.total_runtime_today = "2:30:00"

# Mock Control Config Section
mock_state.config.Heizungssteuerung.MIN_LAUFZEIT = 15

# Inject dependencies
app.state.shared_state = mock_state
mock_set_kompressor = AsyncMock()
app.state.control_funcs = {"set_kompressor": mock_set_kompressor}

client = TestClient(app)

def test_get_status():
    """Test the status endpoint"""
    response = client.get("/status")
    assert response.status_code == 200
    
    data = response.json()
    
    # Check temperature structure and values
    assert data["temperatures"]["oben"] == 42.5
    assert data["temperatures"]["mittig"] == 41.0
    
    # Check compressor structure
    assert data["compressor"]["status"] == "AUS"
    
    # Check setpoints structure
    assert data["setpoints"]["einschaltpunkt"] == 40.0
    
    # Check energy structure
    assert data["energy"]["battery_power"] == 250


def test_update_config():
    """Test the config update endpoint"""
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


def test_control_system_force_on():
    """Test forcing compressor on"""
    command_data = {
        "command": "force_on"
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Compressor forced ON" in data["message"]
    mock_set_kompressor.assert_called_once_with(mock_state, True, force=True)


def test_control_system_force_off():
    """Test forcing compressor off"""
    command_data = {
        "command": "force_off"
    }
    mock_set_kompressor.reset_mock()
    response = client.post("/control", json=command_data)
    assert response.status_code == 200
    
    data = response.json()
    assert data["status"] == "success"
    assert "Compressor forced OFF" in data["message"]
    mock_set_kompressor.assert_called_once_with(mock_state, False, force=True)


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
    assert mock_state.bademodus_aktiv == True


def test_control_system_unknown_command():
    """Test sending an unknown command"""
    command_data = {
        "command": "unknown_command"
    }
    
    response = client.post("/control", json=command_data)
    assert response.status_code == 400
    
    data = response.json()
    assert "detail" in data
    assert "Unknown command" in data["detail"]


@patch('api.read_history_data')
def test_get_history_custom_hours(mock_read_history):
    """Test getting history with custom hours using the new async utility"""
    # Mocking the to_thread read_history_data response
    mock_read_history.return_value = {
        "data": [
            {"timestamp": "2023-10-27 12:00:00", "t_oben": 45.0, "kompressor": "EIN"}
        ],
        "count": 1
    }
    
    # We must patch os.path.exists during the test if the endpoint still checks it or bypass it
    with patch('os.path.exists', return_value=True):
        response = client.get("/history?hours=12")
        assert response.status_code == 200
        
        data = response.json()
        assert "data" in data
        assert "count" in data
        assert data["count"] == 1
        assert data["data"][0]["kompressor"] == "EIN"