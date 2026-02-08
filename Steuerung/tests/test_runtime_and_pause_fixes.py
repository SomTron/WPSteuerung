import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timedelta
import pytz
import sys
import os

# Ensure we can import from the current directory (Steuerung)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
# Add parent of tests (which is Steuerung) to path if running from Steuerung
sys.path.append(os.getcwd())

from main import set_kompressor_status, handle_day_transition
from control_logic import handle_compressor_off

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.control = MagicMock()
    state.stats = MagicMock()
    state.sensors = MagicMock()
    
    state.control.kompressor_ein = False
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.stats.total_runtime_today = timedelta()
    state.stats.last_day = datetime.now(state.local_tz).date()
    
    return state

@pytest.mark.asyncio
async def test_pause_time_logic_no_reset_if_already_off(mock_state):
    """Verifiziert, dass die Pause-Zeit nicht zurückgesetzt wird, wenn der Kompressor bereits AUS ist."""
    # Setup: Kompressor ist seit 10 Minuten aus
    off_time = datetime.now(mock_state.local_tz) - timedelta(minutes=10)
    mock_state.stats.last_compressor_off_time = off_time
    mock_state.control.kompressor_ein = False
    
    # Simuliere Abschaltwunsch (obwohl bereits aus)
    with patch('main.hardware_manager') as mock_hw:
        await set_kompressor_status(mock_state, False)
        
        # Zeit sollte sich NICHT geändert haben
        assert mock_state.stats.last_compressor_off_time == off_time
        mock_hw.set_compressor_state.assert_not_called()

@pytest.mark.asyncio
async def test_runtime_calculation_only_once_on_off(mock_state):
    """Verifiziert, dass die Laufzeit nur beim tatsächlichen Ausschalten berechnet wird."""
    # Setup: Kompressor läuft seit 15 Minuten
    on_time = datetime.now(mock_state.local_tz) - timedelta(minutes=15)
    mock_state.stats.last_compressor_on_time = on_time
    mock_state.control.kompressor_ein = True
    
    # 1. Ausschalten
    with patch('main.hardware_manager'):
        await set_kompressor_status(mock_state, False)
        
        expected_runtime = timedelta(minutes=15)
        # Erlaube kleine Abweichung durch Test-Laufzeit
        assert abs((mock_state.stats.total_runtime_today - expected_runtime).total_seconds()) < 1.0
        
        current_total = mock_state.stats.total_runtime_today
        
        # 2. Erneuter Abschaltruf (sollte nichts ändern)
        await set_kompressor_status(mock_state, False)
        assert mock_state.stats.total_runtime_today == current_total

def test_midnight_transition_running_compressor(mock_state):
    """Verifiziert den Midnight-Split bei laufendem Kompressor."""
    # Setup: Läuft seit 23:45 des Vortages
    yesterday = datetime.now(mock_state.local_tz) - timedelta(days=1)
    on_time = yesterday.replace(hour=23, minute=45, second=0, microsecond=0)
    mock_state.stats.last_compressor_on_time = on_time
    mock_state.stats.last_day = yesterday.date()
    mock_state.control.kompressor_ein = True
    mock_state.stats.total_runtime_today = timedelta() # Laufzeit des alten Tages
    
    # Jetzt ist 00:05 Uhr am neuen Tag
    now = datetime.now(mock_state.local_tz).replace(hour=0, minute=5, second=0, microsecond=0)
    
    handle_day_transition(mock_state, now)
    
    # 15 Minuten sollten dem alten Tag gutgeschrieben worden sein (bevor der Reset auf 0 erfolgte)
    # Da handle_day_transition total_runtime_today am Ende auf 0 setzt, prüfen wir den Zwischenstand 
    # eigentlich im Log, aber wir können hier prüfen, ob last_compressor_on_time auf Mitternacht gesetzt wurde.
    
    expected_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    assert mock_state.stats.last_compressor_on_time == expected_midnight
    assert mock_state.stats.last_day == now.date()
