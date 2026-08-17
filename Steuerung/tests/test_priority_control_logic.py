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
    state.control._soll_einschalten = False  # Default: keine Regel aktiv
    
    # Stats
    state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
    state.stats.last_compressor_off_time = datetime.now(state.local_tz) - timedelta(minutes=5)
    
    # Log-Throttle-Attribute auf None setzen (wie ein frischer State)
    # Sonst liefert MagicMock ein anderes MagicMock statt None -> TypeError in check_log_throttle
    for attr in ['log_max_temp_warn', 'log_min_laufzeit_off', 'log_min_laufzeit_keine_regel']:
        try:
            delattr(state, attr)
        except AttributeError:
            pass
    
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

    # --- Neue Tests: Keine Regel aktiv (z.B. Nachtsperre) ---

    @pytest.mark.asyncio
    async def test_handle_compressor_off_no_rule_active_shuts_down(self, mock_state_with_config):
        """Testet: Keine Regel aktiv (_soll_einschalten=False), Kompressor laeuft,
           regelfuehler (t_unten=26.5°C) weit unter ausschaltpunkt (48°C)
           -> MUSS trotzdem abschalten! (Bug-Fix fuer Nachtsperre-Szenario)"""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        state.control._soll_einschalten = False  # <-- Keine Regel will einschalten
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # Genau wie in Ihrem Log: t_unten=26.5°C, ausschaltpunkt=48°C
        result = await pcl.handle_compressor_off(
            state=state, session=None,
            regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=49.5,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is True, "Muss ausschalten obwohl regelfuehler < ausschaltpunkt!"
        mock_set_kompressor.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_compressor_off_no_rule_active_min_runtime(self, mock_state_with_config):
        """Testet: Keine Regel aktiv, aber Mindestlaufzeit noch nicht erreicht -> warten."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        state.control._soll_einschalten = False
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=10)
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        result = await pcl.handle_compressor_off(
            state=state, session=None,
            regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=49.5,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is False  # Mindestlaufzeit (15 Min) noch nicht erreicht
        mock_set_kompressor.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_compressor_off_regel_aktiv_vorrang(self, mock_state_with_config):
        """Testet: Regel ist aktiv (_soll_einschalten=True) -> normale Abschaltlogik gilt weiter."""
        import priority_control_logic as pcl
        
        state = mock_state_with_config
        state.control.kompressor_ein = True
        state.control._soll_einschalten = True  # <-- Regel WILL einschalten!
        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
        
        mock_set_kompressor = AsyncMock(return_value=True)
        
        # regelfuehler UNTER ausschaltpunkt -> soll NICHT abschalten
        result = await pcl.handle_compressor_off(
            state=state, session=None,
            regelfuehler=40.0, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=49.5,
            set_kompressor_status_func=mock_set_kompressor
        )
        
        assert result is False  # Regel aktiv, Temp noch nicht erreicht -> weiterlaufen
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


class TestSommerModus:
    """Tests fuer den Sommer-Modus (mehrtagige gute PV-Prognose senkt Temperaturen)."""
    
    @pytest.fixture
    def mock_state_sommer(self):
        """Erstellt einen Mock-State mit voller Priority-Config inkl. Sommer-Modus."""
        state = MagicMock()
        state.local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(state.local_tz)
        
        state.solar.feedinpower = 500.0
        state.solar.forecast_today = 2500.0
        state.solar.forecast_tomorrow = 3000.0
        state.solar.forecast_day2 = 2800.0
        
        state.sensors.t_oben = 45.0
        state.sensors.t_verd = 5.0
        state.bademodus_aktiv = False
        state.urlaubsmodus_aktiv = False
        
        state.control.kompressor_ein = False
        state.control.blocking_reason = None
        state.control.previous_modus = "Normalmodus"
        state.control._soll_einschalten = False
        state.control.alle_ergebnisse = []
        
        state.stats.last_compressor_on_time = now - timedelta(minutes=30)
        state.stats.last_compressor_off_time = now - timedelta(minutes=5)
        
        from json_config import WPConfig, ZyklusConfig, SicherheitConfig
        from json_config import KomfortConfig, ZeitfensterConfig, AbweichungConfig
        from json_config import ForecastConfig, AdaptivePVConfig, CalculatedStartConfig
        from json_config import SommerModusConfig, WPSteuerungConfig
        
        state.priority_config = WPSteuerungConfig(
            beschreibung="Test Config",
            wp=WPConfig(),
            zyklus=ZyklusConfig(),
            sicherheit=SicherheitConfig(
                nachtsperre_start=19, nachtsperre_ende=8,
                max_temp_c=48.0, ueberhitzung_c=58.0, notfall_c=36.0,
            ),
            komfort=KomfortConfig(
                prioritaet=60, notfall_einschalten_bei_c=36.0,
                komfort_einschalten_bei_c=38.0, ausschalten_bei_c=42.0,
                min_pv_fuer_komfort_watt=50.0,
            ),
            zeitfenster=ZeitfensterConfig(),
            abweichung=AbweichungConfig(
                prioritaet=47, solltemperatur_c=40.0,
                temperaturfuehler="unten", einschalten_bei_abweichung_k=3.0,
                ausschalten_bei_abweichung_k=0.5,
            ),
            forecast=ForecastConfig(),
            adaptive_pv=AdaptivePVConfig(),
            calculated_start=CalculatedStartConfig(),
            sommer_modus=SommerModusConfig(
                aktiv=True, mindest_prognose_wh=2000.0,
                benoetigte_tage=3, temperatur_offset_c=-3.0,
            ),
            pv_regeln=[],
        )
        
        for attr in ['_last_priority_log', '_last_temp_log']:
            try:
                delattr(state, attr)
            except AttributeError:
                pass
        
        return state

    @pytest.mark.asyncio
    async def test_sommer_modus_aktiv_lowered_temperatures(self, mock_state_sommer):
        """Sommer-Modus: Alle 3 Tage ueber Schwelle -> Temperaturen werden gesenkt."""
        import priority_control_logic as pcl
        
        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(mock_state_sommer, 39.0, 42.0)
            
            args, kwargs = mock_bewerte.call_args
            config = kwargs.get('config')
            assert config is not None, "Config wurde nicht an bewerte_alle_regeln uebergeben"
            
            # Offset von -3.0C muss angewendet sein: 40.0 - 3.0 = 37.0
            assert config.abweichung.solltemperatur_c == 37.0,                 f"Solltemperatur sollte 37.0C sein, ist {config.abweichung.solltemperatur_c}C"
            # Komfort ausschalten: 42.0 - 3.0 = 39.0
            assert config.komfort.ausschalten_bei_c == 39.0,                 f"Komfort ausschalten sollte 39.0C sein, ist {config.komfort.ausschalten_bei_c}C"
    
    @pytest.mark.asyncio
    async def test_sommer_modus_inactive_low_forecast(self, mock_state_sommer):
        """Sommer-Modus: Nur 2 von 3 Tagen ueber Schwelle -> kein Offset."""
        import priority_control_logic as pcl
        
        mock_state_sommer.solar.forecast_day2 = 500.0  # zu niedrig
        
        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(mock_state_sommer, 39.0, 42.0)
            
            args, kwargs = mock_bewerte.call_args
            config = kwargs.get('config')
            assert config.abweichung.solltemperatur_c == 40.0,                 f"Solltemperatur sollte 40.0C bleiben, ist {config.abweichung.solltemperatur_c}C"
    
    @pytest.mark.asyncio
    async def test_sommer_modus_inactive_deactivated(self, mock_state_sommer):
        """Sommer-Modus: Config sagt deaktiviert -> kein Offset."""
        import priority_control_logic as pcl
        
        mock_state_sommer.priority_config.sommer_modus.aktiv = False
        
        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(mock_state_sommer, 39.0, 42.0)
            
            args, kwargs = mock_bewerte.call_args
            config = kwargs.get('config')
            assert config.abweichung.solltemperatur_c == 40.0,                 f"Solltemperatur sollte 40.0C bleiben, ist {config.abweichung.solltemperatur_c}C"

    @pytest.mark.asyncio
    async def test_sommer_modus_inactive_no_day2_forecast(self, mock_state_sommer):
        """Sommer-Modus: Keine Day2-Prognose -> kein Offset."""
        import priority_control_logic as pcl
        
        mock_state_sommer.solar.forecast_day2 = None
        
        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(mock_state_sommer, 39.0, 42.0)
            
            args, kwargs = mock_bewerte.call_args
            config = kwargs.get('config')
            assert config.abweichung.solltemperatur_c == 40.0

    @pytest.mark.asyncio
    async def test_sommer_modus_pv_regeln_werden_angepasst(self, mock_state_sommer):
        """Sommer-Modus: PV-Regeln bekommen ebenfalls den Offset."""
        import priority_control_logic as pcl
        from json_config import PVRegel
        
        mock_state_sommer.priority_config.pv_regeln = [
            PVRegel(name="PV_1", einschalten_bei_c=40.0, ausschalten_bei_c=45.0),
            PVRegel(name="PV_2", einschalten_bei_c=38.0, ausschalten_bei_c=43.0),
        ]
        
        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(mock_state_sommer, 39.0, 42.0)
            
            args, kwargs = mock_bewerte.call_args
            config = kwargs.get('config')
            assert config.pv_regeln[0].einschalten_bei_c == 37.0  # 40.0 - 3.0
            assert config.pv_regeln[0].ausschalten_bei_c == 42.0  # 45.0 - 3.0
            assert config.pv_regeln[1].einschalten_bei_c == 35.0  # 38.0 - 3.0
            assert config.pv_regeln[1].ausschalten_bei_c == 40.0  # 43.0 - 3.0
