import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime, timedelta
import pytz
import sys
import os

# Ensure we can import from parent directory
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from control_logic import handle_compressor_off

class StatsContainer:
    def __init__(self, last_on):
        self.last_compressor_on_time = last_on
        self.last_compressor_off_time = None

@pytest.fixture
def mock_state():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Use a fixed base time for everything
    base_time = datetime(2026, 2, 12, 8, 0, 0, tzinfo=state.local_tz)
    
    # Mocking Config
    config = MagicMock()
    hs = MagicMock()
    hs.NACHTABSENKUNG = 5.0
    hs.NACHTABSENKUNG_START = "20:00"
    hs.NACHTABSENKUNG_END = "08:00"
    hs.MIN_LAUFZEIT = 15
    hs.UEBERGANGSMODUS_MORGENS_ENDE = "10:00"
    hs.UEBERGANGSMODUS_ABENDS_START = "17:00"
    hs.WP_POWER_EXPECTED = 600.0
    config.Heizungssteuerung = hs
    
    su = MagicMock()
    su.MIN_SOC = 20.0
    su.BATTERY_CAPACITY_KWH = 10.0
    config.Solarueberschuss = su
    
    state.config = config
    state.basis_einschaltpunkt = 40.0
    state.min_laufzeit = timedelta(minutes=15)
    
    # Sub-states
    state.control = MagicMock()
    state.control.kompressor_ein = True
    state.control.solar_ueberschuss_aktiv = False
    state.control.blocking_reason = None
    state.bademodus_aktiv = False
    
    # Real object for stats. Started 20m before base_time.
    state.stats = StatsContainer(base_time - timedelta(minutes=20))
    
    state.solar = MagicMock()
    state.solar.soc = 25.0
    state.solar.acpower = 0.0
    state.solar.feedinpower = 0.0
    state.solar.batpower = 0.0
    
    return state

@pytest.mark.asyncio
async def test_handle_compressor_off_transition_no_surplus_no_battery(mock_state):
    """Scenario: Transition morning, no surplus, battery insufficient -> Should turn OFF."""
    set_kompressor_status = AsyncMock(return_value=True)
    
    # Morning transition: 09:00
    fixed_now = datetime.now(mock_state.local_tz).replace(hour=9, minute=0, second=0, microsecond=0)
    
    # Ensure log throttling doesn't return mocks
    mock_state.log_min_laufzeit_off = None
    mock_state.log_min_laufzeit_off_uebergang = None
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.datetime') as mock_cl_dt, \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True), \
         patch('control_logic.is_battery_sufficient_for_transition') as mock_bat:
        
        mock_lu_dt.now.return_value = fixed_now
        mock_cl_dt.now.return_value = fixed_now
        mock_lu_dt.strptime.side_effect = datetime.strptime
        mock_bat.return_value = False # Battery insufficient
        
        # regelfuehler is warm enough (not frost), but no surplus and weak battery
        result = await handle_compressor_off(
            mock_state, None, regelfuehler=38.0, ausschaltpunkt=50.0, 
            min_laufzeit=timedelta(minutes=15), t_oben=45.0, 
            set_kompressor_status_func=set_kompressor_status
        )
        
        assert result is True
        set_kompressor_status.assert_called_with(mock_state, False, force=True, t_boiler_oben=45.0)

@pytest.mark.asyncio
async def test_handle_compressor_off_transition_has_battery(mock_state):
    """Scenario: Transition morning, no surplus, battery SUFFICIENT -> Should stay ON."""
    set_kompressor_status = AsyncMock(return_value=True)
    fixed_now = datetime.now(mock_state.local_tz).replace(hour=9, minute=0, second=0, microsecond=0)
    
    mock_state.log_min_laufzeit_off = None
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.datetime') as mock_cl_dt, \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True), \
         patch('control_logic.is_battery_sufficient_for_transition') as mock_bat:
        
        mock_lu_dt.now.return_value = fixed_now
        mock_cl_dt.now.return_value = fixed_now
        mock_lu_dt.strptime.side_effect = datetime.strptime
        mock_bat.return_value = True # Battery sufficient
        
        result = await handle_compressor_off(
            mock_state, None, regelfuehler=38.0, ausschaltpunkt=50.0, 
            min_laufzeit=timedelta(minutes=15), t_oben=45.0, 
            set_kompressor_status_func=set_kompressor_status
        )
        
        assert result is False
        set_kompressor_status.assert_not_called()

@pytest.mark.asyncio
async def test_handle_compressor_off_transition_min_runtime_not_met(mock_state):
    """Scenario: Transition morning, no surplus, battery insufficient, BUT min runtime not reached -> Should stay ON."""
    set_kompressor_status = AsyncMock(return_value=True)
    
    # Only 5 minutes running
    mock_state.stats.last_compressor_on_time = datetime.now(mock_state.local_tz) - timedelta(minutes=5)
    fixed_now = datetime.now(mock_state.local_tz).replace(hour=9, minute=0, second=0, microsecond=0)
    
    mock_state.log_min_laufzeit_off = None
    mock_state.log_min_laufzeit_off_uebergang = None
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.datetime') as mock_cl_dt, \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True), \
         patch('control_logic.is_battery_sufficient_for_transition') as mock_bat:
        
        mock_lu_dt.now.return_value = fixed_now
        mock_cl_dt.now.return_value = fixed_now
        mock_lu_dt.strptime.side_effect = datetime.strptime
        mock_bat.return_value = False # Battery insufficient
        
        result = await handle_compressor_off(
            mock_state, None, regelfuehler=38.0, ausschaltpunkt=50.0, 
            min_laufzeit=timedelta(minutes=15), t_oben=45.0, 
            set_kompressor_status_func=set_kompressor_status
        )
        
        assert result is False
        set_kompressor_status.assert_not_called()
        assert "Warte auf Mindestlaufzeit" in mock_state.control.blocking_reason

@pytest.mark.asyncio
async def test_handle_compressor_off_transition_frost_protection(mock_state):
    """Scenario: Transition morning, no surplus, battery insufficient, BUT it is too cold -> Should stay ON."""
    set_kompressor_status = AsyncMock(return_value=True)
    
    # Regelfuehler is cold: 34.0 (night einschaltpunkt is 35.0 = 40 - 5)
    regelfuehler = 34.0
    
    # Morning transition: 09:00
    fixed_now = datetime.now(mock_state.local_tz).replace(hour=9, minute=0, second=0, microsecond=0)
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.datetime') as mock_cl_dt, \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True):
        
        mock_lu_dt.now.return_value = fixed_now
        mock_cl_dt.now.return_value = fixed_now
        mock_lu_dt.strptime.side_effect = datetime.strptime
        
        result = await handle_compressor_off(
            mock_state, None, regelfuehler=regelfuehler, ausschaltpunkt=50.0, 
            min_laufzeit=timedelta(minutes=15), t_oben=45.0, 
            set_kompressor_status_func=set_kompressor_status
        )
        
        assert result is False
        set_kompressor_status.assert_not_called()

@pytest.mark.asyncio
async def test_handle_compressor_off_evening_transition(mock_state):
    """Scenario: Evening transition, no surplus, battery insufficient -> Should turn OFF."""
    set_kompressor_status = AsyncMock(return_value=True)
    
    # Evening transition: 18:00 (End is 20:00)
    fixed_now = datetime.now(mock_state.local_tz).replace(hour=18, minute=0, second=0, microsecond=0)
    
    with patch('logic_utils.datetime') as mock_lu_dt, \
         patch('control_logic.datetime') as mock_cl_dt, \
         patch('control_logic.ist_uebergangsmodus_aktiv', return_value=True):
        
        mock_lu_dt.now.return_value = fixed_now
        mock_cl_dt.now.return_value = fixed_now
        mock_lu_dt.strptime.side_effect = datetime.strptime
        
        result = await handle_compressor_off(
            mock_state, None, regelfuehler=38.0, ausschaltpunkt=50.0, 
            min_laufzeit=timedelta(minutes=15), t_oben=45.0, 
            set_kompressor_status_func=set_kompressor_status
        )
        
        assert result is True
        set_kompressor_status.assert_called_with(mock_state, False, force=True, t_boiler_oben=45.0)
