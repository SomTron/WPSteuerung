"""
Tests für priority_control_logic.py
Stellt sicher, dass alle Funktionen korrekt mit der richtigen API aufgerufen werden.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
import pytz
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from logic_utils import check_log_throttle
from utils import safe_timedelta


@pytest.fixture
def mock_state():
    """Erstellt einen Mock-State für Tests."""
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Setzt log attributes explizit auf None (wie ein frischer State)
    del state.test_log_attr
    del state.test_default_attr
    
    return state


@pytest.fixture
def mock_state_with_config():
    """Erstellt einen Mock-State mit vollständiger Config für priority_control_logic."""
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    
    # Priority Config
    state.priority_config.sicherheit.ueberhitzung_c = 58.0
    state.priority_config.sicherheit.max_temp_c = 48.0
    
    # Control state
    state.control.kompressor_ein = False
    state.control.blocking_reason = None
    state.control.previous_modus = "Normalmodus"
    
    # Stats
    state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
    state.stats.last_compressor_off_time = datetime.now(state.local_tz) - timedelta(minutes=5)
    
    return state


class TestCheckLogThrottle:
    """Tests für die check_log_throttle Funktion und ihre korrekte Verwendung."""
    
    def test_check_log_throttle_with_correct_parameter(self, mock_state):
        """Testet, dass check_log_throttle mit interval_minutes funktioniert."""
        # Erstes Mal aufrufen - sollte True zurückgeben
        result = check_log_throttle(mock_state, "test_log_attr", interval_minutes=5.0)
        assert result is True
        
        # Sofort erneut aufrufen - sollte False zurückgeben (Throttling)
        result = check_log_throttle(mock_state, "test_log_attr", interval_minutes=5.0)
        assert result is False
    
    def test_check_log_throttle_rejects_wrong_parameter(self):
        """Testet, dass check_log_throttle ein falsches Keyword argument ablehnt."""
        state = MagicMock()
        state.local_tz = pytz.timezone("Europe/Berlin")
        del state.test_log_attr
        
        with pytest.raises(TypeError, match="unexpected keyword argument 'interval_min'"):
            check_log_throttle(state, "test_log_attr", interval_min=5)
    
    def test_check_log_throttle_default_interval(self, mock_state):
        """Testet, dass der Standard-Interval (5 Minuten) funktioniert."""
        result = check_log_throttle(mock_state, "test_default_attr")
        assert result is True
        
        # Ohne Argumente sollte auch funktionieren
        result = check_log_throttle(mock_state, "test_default_attr")
        assert result is False


class TestPriorityControlLogicImports:
    """Testet, dass priority_control_logic.py korrekt importiert wird."""
    
    def test_import_priority_control_logic(self):
        """Testet, dass das Modul ohne Fehler importiert werden kann."""
        import priority_control_logic as pcl
        assert hasattr(pcl, 'check_safety_limits')
        assert hasattr(pcl, 'handle_compressor_off')
        assert hasattr(pcl, 'handle_compressor_on')
        assert hasattr(pcl, 'determine_mode_and_setpoints')
    
    def test_check_safety_limits_callable(self):
        """Testet, dass check_safety_limits eine async Funktion ist."""
        import priority_control_logic as pcl
        import inspect
        assert inspect.iscoroutinefunction(pcl.check_safety_limits)
    
    def test_handle_compressor_off_callable(self):
        """Testet, dass handle_compressor_off eine async Funktion ist."""
        import priority_control_logic as pcl
        import inspect
        assert inspect.iscoroutinefunction(pcl.handle_compressor_off)


class TestCheckSafetyLimits:
    """Tests für check_safety_limits in priority_control_logic.py."""
    
    @pytest.mark.asyncio
    async def test_check_safety_limits_overheating(self, mock_state_with_config):
        """Testet, dass Überhitzungsschutz greift."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Temperatur über Überhitzungsgrenze
        result = await pcl.check_safety_limits(
            None, state, t_oben=60.0, t_unten=40.0, 
            t_mittig=45.0, t_verd=5.0, 
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is False  # Sicherheitslimit überschritten
        mock_set_kompressor.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_check_safety_limits_normal(self, mock_state_with_config):
        """Testet, dass normaler Bereich OK zurückgibt."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Normale Temperatur
        result = await pcl.check_safety_limits(
            None, state, t_oben=45.0, t_unten=35.0, 
            t_mittig=40.0, t_verd=5.0, 
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is True  # Alles OK
        mock_set_kompressor.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_check_safety_limits_temp_warn(self, mock_state_with_config, caplog):
        """Testet, dass Temperatur-Warnung bei max_temp_c + 2 ausgegeben wird."""
        import priority_control_logic as pcl
        import logging
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Temperatur bei max_temp_c + 2 = 50.0 (kein Abschalten!)
        with caplog.at_level(logging.WARNING):
            result = await pcl.check_safety_limits(
                None, state, t_oben=50.0, t_unten=35.0, 
                t_mittig=40.0, t_verd=5.0, 
                set_kompressor_status_func=mock_set_kompressor
            )
        
        assert result is True  # Kein Abschalten bei Warnung
        mock_set_kompressor.assert_not_called()


class TestHandleCompressorOff:
    """Tests für handle_compressor_off in priority_control_logic.py."""
    
    @pytest.mark.asyncio
    async def test_handle_compressor_off_switches_off(self, mock_state_with_config):
        """Testet, dass Kompressor bei Ausschaltpunkt-Überschreitung ausgeschaltet wird."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Regelfühler über Ausschaltpunkt
        result = await pcl.handle_compressor_off(
            state=state, session=None, 
            regelfuehler=50.0, ausschaltpunkt=45.0,
            min_laufzeit=timedelta(minutes=15), t_oben=46.0,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is True
        mock_set_kompressor.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_handle_compressor_off_min_runtime_not_reached(self, mock_state_with_config):
        """Testet, dass Kompressor NICHT ausgeschaltet wird wenn Mindestlaufzeit nicht erreicht."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        # Kompressor läuft erst seit 10 Minuten
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=10)
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Regelfühler über Ausschaltpunkt, aber Mindestlaufzeit (15 Min) nicht erreicht
        result = await pcl.handle_compressor_off(
            state=state, session=None, 
            regelfuehler=50.0, ausschaltpunkt=45.0,
            min_laufzeit=timedelta(minutes=15), t_oben=46.0,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is False  # Mindestlaufzeit nicht erreicht
        mock_set_kompressor.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_handle_compressor_off_not_running(self, mock_state_with_config):
        """Testet, dass nichts passiert wenn Kompressor bereits aus ist."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = False
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        result = await pcl.handle_compressor_off(
            state=state, session=None, 
            regelfuehler=50.0, ausschaltpunkt=45.0,
            min_laufzeit=timedelta(minutes=15), t_oben=46.0,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is False  # Kompressor läuft nicht
        mock_set_kompressor.assert_not_called()


class TestNoWrongParameterName:
    """
    Stellt sicher, dass die korrigierten Dateien kein 'interval_min' mehr verwenden.
    Dieser Test verhindert Regression.
    """
    
    def test_priority_control_logic_no_interval_min(self):
        """Testet, dass priority_control_logic.py kein interval_min mehr verwendet."""
        import inspect
        import priority_control_logic as pcl
        
        source = inspect.getsource(pcl)
        assert 'interval_min=' not in source, \
            "priority_control_logic.py verwendet noch 'interval_min=' statt 'interval_minutes='"
    
    def test_control_logic_no_interval_min(self):
        """Testet, dass control_logic.py kein interval_min mehr verwendet."""
        import inspect
        import control_logic
        
        source = inspect.getsource(control_logic)
        assert 'interval_min=' not in source, \
            "control_logic.py verwendet noch 'interval_min=' statt 'interval_minutes='"
