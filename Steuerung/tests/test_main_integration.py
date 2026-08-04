# -*- coding: utf-8 -*-
"""
Integrationstests fuer main.py - run_logic_step() und main_loop().
Testet ob Parameter korrekt durchgereicht werden (insb. learning_engine).
"""
import threading
from datetime import timedelta, datetime
import pytz
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_run_logic_step_passes_learning_engine():
    state = MagicMock()
    state.control.kompressor_ein = False
    state.sensors.t_oben = 45.0
    state.sensors.t_unten = 38.0
    state.sensors.t_mittig = 40.0
    state.sensors.t_verd = 5.0
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.kompressor_verification_error_count = 0
    state.min_laufzeit = 10
    state.min_pause = 5
    state.control.ausschluss_grund = None
    state.control.aktueller_ausschaltpunkt = 44.0
    state.control.aktueller_einschaltpunkt = 38.0
    state.stats = MagicMock()
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.control._soll_einschalten = False
    learning_engine = MagicMock()
    learning_engine.get_learned_heating_rate.return_value = 3.5
    learning_engine.get_learned_target_hour.return_value = 17.5
    session = AsyncMock()
    with patch("main.pcl.check_pressure_and_config", new_callable=AsyncMock) as mock_pressure:
        mock_pressure.return_value = True
        with patch("main.pcl.check_safety_limits", new_callable=AsyncMock) as mock_safety:
            mock_safety.return_value = True
            with patch("main.control_logic.check_sensors_and_safety", new_callable=AsyncMock) as mock_sensors:
                mock_sensors.return_value = True
                with patch("main.pcl.determine_mode_and_setpoints", new_callable=AsyncMock) as mock_determine:
                    mock_determine.return_value = {"soll_einschalten": False, "regelfuehler": "unten", "regel_ergebnisse": []}
                    with patch("main.pcl.handle_compressor_on", new_callable=AsyncMock):
                        with patch("main.check_and_send_alerts", new_callable=AsyncMock):
                            from main import run_logic_step
                            await run_logic_step(session, state, learning_engine=learning_engine)
                            call_kwargs = mock_determine.call_args.kwargs
                            assert "learning_engine" in call_kwargs, f"learning_engine nicht uebergeben! Kwargs: {call_kwargs}"
                            assert call_kwargs["learning_engine"] is learning_engine, "Falsches learning_engine-Objekt"


@pytest.mark.asyncio
async def test_run_logic_step_without_learning_engine_defaults_to_none():
    state = MagicMock()
    state.control.kompressor_ein = False
    state.sensors.t_oben = 45.0
    state.sensors.t_unten = 38.0
    state.sensors.t_mittig = 40.0
    state.sensors.t_verd = 5.0
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.kompressor_verification_error_count = 0
    state.min_laufzeit = 10
    state.min_pause = 5
    state.control.ausschluss_grund = None
    state.control.aktueller_ausschaltpunkt = 44.0
    state.control.aktueller_einschaltpunkt = 38.0
    state.stats = MagicMock()
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.control._soll_einschalten = False
    session = AsyncMock()
    with patch("main.pcl.check_pressure_and_config", new_callable=AsyncMock) as mock_pressure:
        mock_pressure.return_value = True
        with patch("main.pcl.check_safety_limits", new_callable=AsyncMock) as mock_safety:
            mock_safety.return_value = True
            with patch("main.control_logic.check_sensors_and_safety", new_callable=AsyncMock) as mock_sensors:
                mock_sensors.return_value = True
                with patch("main.pcl.determine_mode_and_setpoints", new_callable=AsyncMock) as mock_determine:
                    mock_determine.return_value = {"soll_einschalten": False, "regelfuehler": "unten", "regel_ergebnisse": []}
                    with patch("main.pcl.handle_compressor_on", new_callable=AsyncMock):
                        with patch("main.check_and_send_alerts", new_callable=AsyncMock):
                            from main import run_logic_step
                            await run_logic_step(session, state)
                            assert True


@pytest.mark.asyncio
async def test_main_loop_passes_learning_engine():
    import main as main_module
    state = MagicMock()
    state.learning_engine = MagicMock()
    state.bot_token = None
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.control.kompressor_ein = False
    state.control.aktueller_ausschaltpunkt = 44.0
    state.control.aktueller_einschaltpunkt = 38.0
    state.control.ausschluss_grund = None
    state.control._soll_einschalten = False
    state.stats = MagicMock()
    state.stats.last_compressor_on_time = None
    state.stats.last_compressor_off_time = None
    state.stats.current_runtime = timedelta()
    main_module.state = state
    main_module.hardware_manager = MagicMock()
    main_module.stop_event = threading.Event()
    def stop_after_one_iteration(*args, **kwargs):
        main_module.stop_event.set()
    with patch("main.setup_application", new_callable=AsyncMock) as mock_setup:
        mock_setup.return_value = AsyncMock()
        with patch("main.run_logic_step", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = stop_after_one_iteration
            with patch("main.update_system_data", new_callable=AsyncMock):
                with patch("main.check_periodic_tasks", new_callable=AsyncMock) as mock_periodic:
                    mock_periodic.return_value = datetime.now() - timedelta(minutes=1)
                    with patch("main.log_system_state", new_callable=AsyncMock):
                        with patch("main.handle_day_transition"):
                            with patch("main.safe_timedelta"):
                                await main_module.main_loop()
                                call_args = mock_run.call_args
                                assert call_args is not None, "run_logic_step wurde nie aufgerufen!"
                                args, kwargs = call_args
                                assert "learning_engine" in kwargs, f"learning_engine fehlt: {kwargs}"
                                assert kwargs["learning_engine"] is state.learning_engine, "Falsches learning_engine-Objekt"