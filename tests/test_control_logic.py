import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, time, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from control_logic import determine_mode_and_setpoints

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.config = {
        "Heizungssteuerung": {
            "NACHT_START": "22:00",
            "NACHT_ENDE": "06:00",
            "NACHTABSENKUNG": "5",
            "NACHTABSENKUNG_END": "06:00"
        },
        "Urlaubsmodus": {
            "URLAUBSABSENKUNG": "10"
        }
    }
    state.nachtabsenkung_ende = time(6, 0)
    state.uebergangsmodus_morgens_ende = time(10, 0)
    state.uebergangsmodus_abends_start = time(17, 0)
    state.nachtabsenkung_start = time(22, 0)
    
    state.aktueller_ausschaltpunkt = 50
    state.aktueller_einschaltpunkt = 40
    state.ausschaltpunkt_erhoeht = 55
    state.einschaltpunkt_erhoeht = 45
    
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.solar_ueberschuss_aktiv = False
    state.previous_modus = "Normalmodus"
    
    state.batpower = 0
    state.soc = 50
    state.feedinpower = 0
    
    state.last_solar_window_check = None
    state.last_solar_window_status = False
    
    return state

@pytest.mark.asyncio
async def test_determine_mode_normal(mock_state):
    # Mock dependencies
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
            
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Normalmodus"
        assert result['ausschaltpunkt'] == 50
        assert result['einschaltpunkt'] == 40
        assert result['regelfuehler'] == 35 # t_mittig

@pytest.mark.asyncio
async def test_determine_mode_solar(mock_state):
    mock_state.batpower = 1000 # > 600 triggers solar excess
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=True):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Solarüberschuss"
        assert result['ausschaltpunkt'] == 55 # erhoeht
        assert result['regelfuehler'] == 30 # t_unten

@pytest.mark.asyncio
async def test_determine_mode_night(mock_state):
    # Test Night Mode (Nachtabsenkung)
    
    with patch('control_logic.is_nighttime', return_value=True), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False), \
         patch('control_logic.is_solar_window', return_value=False):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Nachtmodus"
        # 50 - 5 (Nachtabsenkung) = 45
        assert result['ausschaltpunkt'] == 45 
        # 40 - 5 = 35
        assert result['einschaltpunkt'] == 35
        assert result['regelfuehler'] == 35 # t_mittig

@pytest.mark.asyncio
async def test_determine_mode_bademodus(mock_state):
    mock_state.bademodus_aktiv = True
    
    with patch('control_logic.is_nighttime', return_value=False), \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=False):
        
        result = await determine_mode_and_setpoints(mock_state, t_unten=30, t_mittig=35)
        
        assert result['modus'] == "Bademodus"
        assert result['ausschaltpunkt'] == 55 # erhoeht
        assert result['einschaltpunkt'] == 51 # erhoeht - 4
        assert result['regelfuehler'] == 30 # t_unten
