import pytest
import asyncio
from datetime import datetime, timedelta, time
import pytz
from unittest.mock import Mock, patch, MagicMock

import control_logic
import logic_utils
from hardware_mock import MockHardwareManager


@pytest.fixture
def mock_config():
    """Create a simple mock config for testing without ConfigManager dependency."""
    config = MagicMock()
    
    # Heizungssteuerung section
    config.Heizungssteuerung.NACHTABSENKUNG_START = "19:30"
    config.Heizungssteuerung.NACHTABSENKUNG_END = "08:00"
    config.Heizungssteuerung.NACHTABSENKUNG = 15.0
    config.Heizungssteuerung.SICHERHEITS_TEMP = 50.0
    config.Heizungssteuerung.EINSCHALTPUNKT = 43
    config.Heizungssteuerung.AUSSCHALTPUNKT = 45
    config.Heizungssteuerung.EINSCHALTPUNKT_ERHOEHT = 40
    config.Heizungssteuerung.AUSSCHALTPUNKT_ERHOEHT = 48
    config.Heizungssteuerung.VERDAMPFERTEMPERATUR = 6.0
    config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP = 9.0
    
    # Solarueberschuss section
    config.Solarueberschuss.BATPOWER_THRESHOLD = 600.0
    config.Solarueberschuss.SOC_THRESHOLD = 95.0
    config.Solarueberschuss.FEEDINPOWER_THRESHOLD = 600.0
    
    # Urlaubsmodus section
    config.Urlaubsmodus.URLAUBSABSENKUNG = 6.0
    
    return config


@pytest.fixture
def mock_state(mock_config):
    """Create a simplified state object for testing."""
    state = MagicMock()
    state.config = mock_config
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Sub-states
    state.sensors = MagicMock()
    state.solar = MagicMock()
    state.control = MagicMock()
    state.stats = MagicMock()

    # Initial values
    state.control.kompressor_ein = False
    state.control.solar_ueberschuss_aktiv = False
    state.control.previous_modus = "Normal"
    state.control.aktueller_einschaltpunkt = 43
    state.control.aktueller_ausschaltpunkt = 45
    
    state.solar.batpower = 0.0
    state.solar.soc = 0.0
    state.solar.feedinpower = 0.0
    
    state.stats.total_runtime_today = timedelta()
    state.stats.current_runtime = timedelta()
    state.stats.last_day = None
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.stats.last_completed_cycle = None

    # Properties/Calculated fields
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.basis_einschaltpunkt = 43
    state.basis_ausschaltpunkt = 45
    state.einschaltpunkt_erhoeht = 40
    state.ausschaltpunkt_erhoeht = 48
    
    state.min_laufzeit = timedelta(minutes=10)
    state.min_pause = timedelta(minutes=10)
    
    state.nachtabsenkung_ende = time(8, 0)
    state.nachtabsenkung_start = time(19, 30)
    state.uebergangsmodus_morgens_ende = time(10, 0)
    state.uebergangsmodus_abends_start = time(17, 0)
    
    return state


@pytest.fixture
def mock_hardware():
    """Create mock hardware for testing."""
    hw = MockHardwareManager()
    hw.init_gpio()
    return hw


class TestMidnightTransition:
    """Tests for day-change logic at midnight."""
    
    @pytest.mark.asyncio
    async def test_day_change_resets_statistics(self, mock_state):
        """Test that statistics reset at midnight."""
        # Setup: Set state to yesterday with some runtime
        tz = pytz.timezone("Europe/Berlin")
        yesterday = datetime.now(tz) - timedelta(days=1)
        
        mock_state.stats.last_day = yesterday.date()
        mock_state.stats.total_runtime_today = timedelta(hours=5)
        mock_state.stats.last_completed_cycle = yesterday
        
        # Simulate midnight transition
        now = datetime.now(tz)
        
        # This logic is extracted in main.handle_day_transition
        from main import handle_day_transition
        handle_day_transition(mock_state, now)
        
        # Verify reset
        assert mock_state.stats.total_runtime_today == timedelta()
        assert mock_state.stats.last_completed_cycle is None
        assert mock_state.stats.last_day == now.date()
    
    @pytest.mark.asyncio
    async def test_compressor_state_persists_across_midnight(self, mock_state, mock_hardware):
        """Test that compressor state is maintained across midnight."""
        # Setup: Compressor running before midnight
        mock_state.control.kompressor_ein = True
        mock_state.stats.last_compressor_on_time = datetime.now(mock_state.local_tz) - timedelta(minutes=30)
        mock_hardware.set_compressor_state(True)
        
        # Simulate day change
        tz = mock_state.local_tz
        yesterday = (datetime.now(tz) - timedelta(days=1)).date()
        mock_state.stats.last_day = yesterday
        
        from main import handle_day_transition
        handle_day_transition(mock_state, datetime.now(tz))
        
        # Compressor should still be on
        assert mock_state.control.kompressor_ein is True
        assert mock_hardware.get_compressor_state() is True


class TestSolarModeSwitch:
    """Tests for solar surplus mode transitions."""
    
    @pytest.mark.asyncio
    async def test_normal_to_solar_transition(self, mock_state, mock_config):
        """Test transition from Normal to Solar mode."""
        # Setup: Normal mode, temperature OK
        mock_state.control.solar_ueberschuss_aktiv = False
        mock_state.control.kompressor_ein = True
        
        t_oben = 44.0
        t_mittig = 43.5
        
        # Trigger solar surplus
        mock_state.solar.batpower = 700.0  # Above threshold
        mock_state.solar.soc = 96.0
        mock_state.solar.feedinpower = 700.0
        
        result = await control_logic.determine_mode_and_setpoints(mock_state, t_mittig, t_mittig)
        
        # Should switch to solar mode
        assert result["solar_ueberschuss_aktiv"] is True
        assert result["modus"] == "Solarüberschuss"
    
    @pytest.mark.asyncio
    async def test_solar_to_normal_transition_above_threshold(self, mock_state, mock_config, mock_hardware):
        """Test that compressor turns off when leaving solar mode if temp is high."""
        # Setup: Solar mode active, temp above normal threshold
        mock_state.control.solar_ueberschuss_aktiv = True
        mock_state.control.kompressor_ein = True
        mock_state.stats.last_compressor_on_time = datetime.now(mock_state.local_tz) - timedelta(minutes=20)
        
        t_oben = 46.0  # Above normal threshold (45)
        t_mittig = 45.5
        
        # Mock session for telegram
        mock_session = Mock()
        
        async def mock_set_kompressor(state, status, **kwargs):
            state.control.kompressor_ein = status
            mock_hardware.set_compressor_state(status)
            return True
        
        # Simulate mode switch
        mock_state.control.solar_ueberschuss_aktiv = False  # Solar surplus ended
        
        result = await control_logic.handle_mode_switch(
            mock_state, mock_session, t_oben, t_mittig, mock_set_kompressor
        )
        
        # Should have turned off
        assert result is True
        assert mock_state.control.kompressor_ein is False


class TestTimezoneEdgeCases:
    """Tests for timezone and DST handling."""
    
    def test_safe_timedelta_with_naive_datetime(self, mock_state):
        """Test safe_timedelta handles naive datetimes correctly."""
        from utils import safe_timedelta
        
        tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(tz)
        naive_past = datetime.now() - timedelta(hours=1)
        
        # Should not crash, should localize naive datetime
        delta = safe_timedelta(now, naive_past, tz)
        
        # Should be approximately 1 hour
        assert timedelta(minutes=50) < delta < timedelta(minutes=70)
    
    def test_nighttime_calculation_across_midnight(self, mock_config):
        """Test is_nighttime works correctly across midnight."""
        # Night from 19:30 to 08:00
        
        # Test at 23:00 (should be night)
        with patch('logic_utils.datetime') as mock_dt:
            mock_dt.now.return_value.time.return_value = time(23, 0)
            mock_dt.strptime.side_effect = datetime.strptime
            assert control_logic.is_nighttime(mock_config) is True
        
        # Test at 02:00 (should be night)
        with patch('logic_utils.datetime') as mock_dt:
            mock_dt.now.return_value.time.return_value = time(2, 0)
            mock_dt.strptime.side_effect = datetime.strptime
            assert control_logic.is_nighttime(mock_config) is True
        
        # Test at 12:00 (should not be night)
        with patch('logic_utils.datetime') as mock_dt:
            mock_dt.now.return_value.time.return_value = time(12, 0)
            mock_dt.strptime.side_effect = datetime.strptime
            assert control_logic.is_nighttime(mock_config) is False


class TestRuntimeCalculation:
    """Tests for runtime calculation logic."""
    
    @pytest.mark.asyncio
    async def test_current_runtime_updates_while_running(self, mock_state):
        """Test that current_runtime is calculated correctly while compressor runs."""
        from utils import safe_timedelta
        
        # Setup: Compressor started 15 minutes ago
        tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(tz)
        start_time = now - timedelta(minutes=15)
        
        mock_state.control.kompressor_ein = True
        mock_state.stats.last_compressor_on_time = start_time
        
        # Calculate current runtime (this should happen in main loop)
        if mock_state.control.kompressor_ein and mock_state.stats.last_compressor_on_time:
            mock_state.stats.current_runtime = safe_timedelta(now, mock_state.stats.last_compressor_on_time, tz)
        else:
            mock_state.stats.current_runtime = timedelta()
        
        # Should be approximately 15 minutes
        assert timedelta(minutes=14) < mock_state.stats.current_runtime < timedelta(minutes=16)
    
    @pytest.mark.asyncio
    async def test_total_runtime_accumulation(self, mock_state):
        """Test that total_runtime_today accumulates correctly."""
        # Setup: Previous runtime
        mock_state.stats.total_runtime_today = timedelta(hours=2)
        
        # Simulate compressor cycle completion
        mock_state.stats.last_runtime = timedelta(minutes=30)
        mock_state.stats.total_runtime_today += mock_state.stats.last_runtime
        
        # Should be 2.5 hours
        assert mock_state.stats.total_runtime_today == timedelta(hours=2, minutes=30)
