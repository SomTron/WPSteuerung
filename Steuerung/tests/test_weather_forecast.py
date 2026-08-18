# -*- coding: utf-8 -*-
"""
Tests fuer weather_forecast.py.

Stellt sicher, dass get_solar_forecast() immer 7 Werte zurueckgibt,
damit main.py kein 'not enough values to unpack (expected 7, got 6)' wirft.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGetSolarForecastReturnValueCount:
    """Hauptgrund fuer diesen Test: Bug wo get_solar_forecast() 6 statt 7 Werte
    zurueckgab und main.py mit 'expected 7, got 6' crashte."""

    def _make_session(self, status=200, json_data=None):
        """Helper: erzeugt Session-Mock mit korrektem async context manager."""
        session = MagicMock()
        session.get = MagicMock()
        session.get.return_value = AsyncMock()  # for __aenter__
        mock_response = AsyncMock()
        mock_response.status = status
        mock_response.json = AsyncMock()
        mock_response.json.return_value = json_data or {}
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        session.get.return_value.__aenter__.return_value = mock_response
        session.get.return_value.__aexit__.return_value = None
        return session

    @pytest.mark.asyncio
    async def test_return_value_count_success(self):
        """Erfolgsfall: get_solar_forecast() gibt 7 Werte zurueck."""
        session = self._make_session(200, {
            "hourly": {
                "time": ["2026-08-17T00:00", "2026-08-17T01:00",
                         "2026-08-18T00:00", "2026-08-18T01:00",
                         "2026-08-19T00:00", "2026-08-19T01:00"],
                "direct_radiation": [100, 200, 300, 400, 500, 600],
                "diffuse_radiation": [10, 20, 30, 40, 50, 60],
            },
            "daily": {
                "time": ["2026-08-17", "2026-08-18", "2026-08-19"],
                "sunrise": ["2026-08-17T06:15", "2026-08-18T06:16", "2026-08-19T06:17"],
                "sunset": ["2026-08-17T20:15", "2026-08-18T20:14", "2026-08-19T20:13"],
            }
        })

        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(session, None)

        # KERN-TEST: MUSS 7 Werte sein (Bug: waren vorher 6!)
        assert len(result) == 7, (
            f"get_solar_forecast() gab {len(result)} Werte zurueck, "
            f"erwartet waren 7! main.py erwartet:\n"
            f"  rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tomorrow, ss_tomorrow\n"
            f"Das war der Grund fuer den Produktions-Crash am 2026-08-17."
        )

    @pytest.mark.asyncio
    async def test_return_value_count_api_error(self):
        """API-Fehlerfall: get_solar_forecast() gibt 7x None zurueck."""
        session = self._make_session(500, {})
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(session, None)

        assert len(result) == 7, (
            f"Bei API-Fehler: {len(result)} Werte, erwartet 7!"
        )
        # Alle muessen None sein
        assert all(v is None for v in result), (
            f"Bei API-Fehler muessen alle Werte None sein, bekam: {result}"
        )

    @pytest.mark.asyncio
    async def test_return_value_count_empty_hourly(self):
        """Leere API-Daten: get_solar_forecast() gibt 7x None zurueck."""
        session = self._make_session(200, {
            "hourly": {"time": [], "direct_radiation": [], "diffuse_radiation": []},
            "daily": {"time": [], "sunrise": [], "sunset": []},
        })
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(session, None)
        assert len(result) == 7

    @pytest.mark.asyncio
    async def test_return_value_count_network_error(self):
        """Netzwerkfehler: get_solar_forecast() gibt 7x None zurueck (Exception im async with)."""
        session = MagicMock()
        session.get = MagicMock()
        session.get.return_value = AsyncMock()
        session.get.return_value.__aenter__.side_effect = Exception("Connection refused")
        session.get.return_value.__aexit__.return_value = None

        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(session, None)

        assert len(result) == 7
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_return_value_count_timeout(self):
        """Timeout: get_solar_forecast() gibt 7x None zurueck."""
        session = MagicMock()
        session.get = MagicMock()
        session.get.return_value = AsyncMock()
        session.get.return_value.__aenter__.side_effect = TimeoutError("API timeout")
        session.get.return_value.__aexit__.return_value = None

        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(session, None)
        assert len(result) == 7

    @pytest.mark.asyncio
    async def test_return_value_names_match_main_expectation(self):
        """Stellt sicher, dass die Reihenfolge der 7 Werte mit main.py uebereinstimmt.
        main.py Zeile ~264 erwartet:
        rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tomorrow, ss_tomorrow
        """
        session = self._make_session(200, {
            "hourly": {
                "time": ["2026-08-17T00:00", "2026-08-17T01:00",
                         "2026-08-18T00:00", "2026-08-18T01:00",
                         "2026-08-19T00:00", "2026-08-19T01:00"],
                "direct_radiation": [100, 0, 200, 0, 300, 0],
                "diffuse_radiation": [10, 0, 20, 0, 30, 0],
            },
            "daily": {
                "time": ["2026-08-17", "2026-08-18", "2026-08-19"],
                "sunrise": ["2026-08-17T06:15", "2026-08-18T06:16", "2026-08-19T06:17"],
                "sunset": ["2026-08-17T20:15", "2026-08-18T20:14", "2026-08-19T20:13"],
            }
        })

        from weather_forecast import get_solar_forecast
        rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tomorrow, ss_tomorrow = await get_solar_forecast(session, None)

        # Werte sollten korrekt extrahiert sein
        assert rad_today is not None
        assert rad_tomorrow is not None
        assert rad_day2 is not None
        assert sr_today is not None
        assert ss_today is not None
        assert sr_tomorrow is not None
        assert ss_tomorrow is not None
        assert isinstance(sr_today, str)
        assert ":" in sr_today


class TestLogForecastToCsv:
    """Test fuer log_forecast_to_csv - auch hier 7 Parameter."""

    @pytest.mark.asyncio
    async def test_csv_header_has_8_columns(self):
        """CSV-Header muss 8 Spalten haben (mit Day2_kWh)."""
        import tempfile
        from weather_forecast import log_forecast_to_csv

        with patch("weather_forecast.os.path.dirname") as mock_dirname:
            with patch("weather_forecast.os.path.exists") as mock_exists:
                with patch("weather_forecast.aiofiles.open") as mock_open:
                    mock_dirname.return_value = tempfile.gettempdir()
                    mock_exists.return_value = False
                    mock_file = AsyncMock()
                    mock_open.return_value.__aenter__.return_value = mock_file

                    await log_forecast_to_csv(2.5, 3.0, 1.5, "06:15", "20:15", "06:16", "20:14")

                    # Pruefe: Header wurde geschrieben
                    write_calls = mock_file.write.call_args_list
                    assert len(write_calls) >= 1
                    header = write_calls[0][0][0]
                    assert "Day2_kWh" in header, (
                        f"Day2_kWh fehlt im CSV-Header: {header}"
                    )
                    columns = header.strip().split(",")
                    assert len(columns) == 8, (
                        f"CSV-Header hat {len(columns)} Spalten, erwartet 8: {columns}"
                    )


class TestMainCheckPeriodicTasks:
    """Test ob main.py check_periodic_tasks() mit 7 Werten umgehen kann."""

    @pytest.mark.asyncio
    async def test_check_periodic_tasks_handles_7_values(self):
        """main.py: check_periodic_tasks() verarbeitet 7-Werte-Return korrekt."""
        import datetime
        import pytz
        from types import SimpleNamespace
        state = SimpleNamespace()
        state.local_tz = pytz.timezone("Europe/Berlin")
        state.last_forecast_update = None
        state.config = None
        state.sommer_modus_aktiv = False
        state.sommer_modus_zaehler = 0
        state.solar = MagicMock()
        state.solar.forecast_today = None
        state.solar.forecast_tomorrow = None
        state.solar.forecast_day2 = None
        from json_config import SommerModusConfig
        state.priority_config = SimpleNamespace(
            sommer_modus=SommerModusConfig(
                aktiv=True, mindest_prognose_wh=2000.0,
                benoetigte_tage=3, temperatur_offset_c=-3.0,
            )
        )
        session = AsyncMock()

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (2.5, 3.0, 1.5, "06:15", "20:15", "06:16", "20:14")
            with patch("main.check_vpn_status", new_callable=AsyncMock):
                from main import check_periodic_tasks
                last_check = datetime.datetime.now() - datetime.timedelta(hours=2)
                result = await check_periodic_tasks(session, state, last_check)

                assert result is not None
                assert state.solar.forecast_today == 2.5
                assert state.solar.forecast_tomorrow == 3.0
                assert state.solar.forecast_day2 == 1.5  # <-- Day2!
                assert state.solar.sunrise_today == "06:15"
                assert state.solar.sunset_today == "20:15"

    @pytest.mark.asyncio
    async def test_check_periodic_tasks_handles_none_values(self):
        """main.py: check_periodic_tasks() uebersteht 7x None ohne Crash."""
        import datetime
        import pytz
        state = MagicMock()
        state.local_tz = pytz.timezone("Europe/Berlin")
        state.last_forecast_update = None
        state.config = MagicMock()
        state.solar = MagicMock()
        state.solar.forecast_today = None
        state.solar.forecast_tomorrow = None
        state.solar.forecast_day2 = None
        session = MagicMock()

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (None, None, None, None, None, None, None)
            with patch("main.check_vpn_status", new_callable=AsyncMock):
                from main import check_periodic_tasks
                last_check = datetime.datetime.now() - datetime.timedelta(hours=2)
                result = await check_periodic_tasks(session, state, last_check)

                assert result is not None
                # Da rad_today = None, wird state.solar.forecast_today NICHT gesetzt
                assert state.solar.forecast_today is None