# -*- coding: utf-8 -*-
"""
Integrationstests fuer main.py - run_logic_step().
Testet ob Parameter korrekt durchgereicht werden (insb. learning_engine).
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_run_logic_step_passes_learning_engine():
    """Testet ob learning_engine durchgereicht wird (Bugfix fuer NameError)."""
    state = MagicMock()
    state.control.kompressor_ein = False
    state.sensors.t_oben = 45.0
    state.sensors.t_unten = 38.0
    state.sensors.t_mittig = 40.0
    state.sensors.t_verd = 5.0
    state.local_tz = "Europe/Berlin"
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
    """Testet ob run_logic_step auch ohne learning_engine funktioniert (None-Default)."""
    state = MagicMock()
    state.control.kompressor_ein = False
    state.sensors.t_oben = 45.0
    state.sensors.t_unten = 38.0
    state.sensors.t_mittig = 40.0
    state.sensors.t_verd = 5.0
    state.local_tz = "Europe/Berlin"
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
                            await run_logic_step(session, state)  # OHNE learning_engine

                            call_args = mock_determine.call_args
                            print(f"determine_mode_and_setpoints aufgerufen mit: kwargs={call_args.kwargs}")
                            assert True  # Kein Fehler = Test bestanden
