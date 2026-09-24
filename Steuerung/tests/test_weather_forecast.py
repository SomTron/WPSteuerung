# -*- coding: utf-8 -*-
"""
Tests fuer weather_forecast.py.

Abgedeckt:
1. Vertrag: get_solar_forecast() liefert IMMER genau 8 Werte - auch an den
   Fehlerpfaden (Historischer Bug 1: 6 statt 7; Regression 2: Erfolgsfall 8,
   Fehlerfall nur 7 -> main.py-Unpack 'expected 8, got 7' crashte im Pi-Log).
2. Test-Isolation: CSV-Writes gehen NUR in injizierte temporaere Pfade.
   (Regression: Vorher schrieben Tests in die Produktions-sonnen_prognose.csv!)
3. Header-Migration: alte 7-Spalten-Dateien werden einmalig auf 8 Spalten
   (inkl. Day2_kWh) migriert, damit Header und Datenzeilen zusammenpassen.
"""
import os
import sys
from datetime import datetime, timedelta
from constants import FORECAST_MAX_AGE_HOURS
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

PRODUKTION_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'sonnen_prognose.csv',
)


# --- Mock-Helfer -------------------------------------------------------------

def baue_mock_response(payload, status=200):
    """Response, die als Kontext fuer 'async with session.get(...)' dient."""
    response = MagicMock()
    response.status = status
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    response.json = AsyncMock(return_value=payload if payload is not None else {})
    response.text = AsyncMock(return_value="mock error details")
    return response


def baue_mock_session(payload=None, status=200):
    """session MUSS MagicMock sein: session.get() darf kein nacktes
    Coroutine-Objekt liefern (verursachte RuntimeWarnings + vacuous Tests)."""
    session = MagicMock()
    session.get.return_value = baue_mock_response(payload, status)
    return session


def baue_api_payload():
    """Konsistentes Open-Meteo-Payload mit heute/morgen/uebermorgen."""
    tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(tz)
    d = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(3)]
    return {
        "hourly": {
            "time": [f"{d[0]}T00:00", f"{d[0]}T01:00",
                     f"{d[1]}T00:00", f"{d[1]}T01:00",
                     f"{d[2]}T00:00", f"{d[2]}T01:00"],
            "direct_radiation": [100, 0, 200, 0, 300, 0],
            "diffuse_radiation": [10, 0, 20, 0, 30, 0],
        },
        "daily": {
            "time": d,
            "sunrise": [f"{d[0]}T06:15", f"{d[1]}T06:16", f"{d[2]}T06:17"],
            "sunset": [f"{d[0]}T20:15", f"{d[1]}T20:14", f"{d[2]}T20:13"],
        },
    }


# --- 1) Rueckgabewert-Vertrag -------------------------------------------------

class TestGetSolarForecastReturnValueCount:
    """Vertrag: IMMER genau 8 Werte (Erfolg UND Fehlerpfade)."""

    @pytest.mark.asyncio
    async def test_success_liefert_8_werte(self, tmp_path):
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(
            baue_mock_session(baue_api_payload()), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 8
        rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tom, ss_tom, hourly = result
        assert rad_today == pytest.approx(0.11)      # (100+10)/1000 kWh/m²
        assert rad_tomorrow == pytest.approx(0.22)
        assert rad_day2 == pytest.approx(0.33)
        assert sr_today == "06:15" and ss_today == "20:15"
        assert sr_tom == "06:16" and ss_tom == "20:14"
        assert isinstance(sr_today, str) and ":" in sr_today

    @pytest.mark.asyncio
    async def test_api_error_liefert_8x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(
            baue_mock_session(None, status=500), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 8
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_leere_stunden_daten_liefert_8x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        payload = {"hourly": {"time": [], "direct_radiation": [], "diffuse_radiation": []},
                   "daily": {"time": [], "sunrise": [], "sunset": []}}
        result = await get_solar_forecast(
            baue_mock_session(payload), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 8
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_netzwerkfehler_liefert_8x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        session = MagicMock()
        session.get.side_effect = Exception("Connection refused")
        result = await get_solar_forecast(session, None,
                                          csv_path=str(tmp_path / "forecast.csv"))
        assert len(result) == 8
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_timeout_liefert_8x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        session = MagicMock()
        session.get.side_effect = TimeoutError("API timeout")
        result = await get_solar_forecast(session, None,
                                          csv_path=str(tmp_path / "forecast.csv"))
        assert len(result) == 8
        assert all(v is None for v in result)


# --- 2) Schreib-Isolation ------------------------------------------------------

class TestCsvSchreibisolation:
    """Regression: Tests duerfen die Produktions-CSV niemals veraendern."""

    @pytest.mark.asyncio
    async def test_get_solar_forecast_schreibt_nur_in_injizierten_pfad(self, tmp_path):
        from weather_forecast import get_solar_forecast

        vorher = (open(PRODUKTION_CSV, 'rb').read()
                  if os.path.exists(PRODUKTION_CSV) else None)

        ziel = tmp_path / "forecast.csv"
        await get_solar_forecast(baue_mock_session(baue_api_payload()), None,
                                 csv_path=str(ziel))

        # Injizierter Pfad wurde beschrieben ...
        assert ziel.exists(), "Injizierter CSV-Pfad wurde nicht beschrieben"
        # ... und die Produktionsdatei blieb unberuehrt.
        if vorher is not None:
            nachher = open(PRODUKTION_CSV, 'rb').read()
            assert nachher == vorher, "Produktions-sonnen_prognose.csv wurde veraendert!"

    @pytest.mark.asyncio
    async def test_log_forecast_to_csv_default_pfad_ist_produktionspfad(self, tmp_path, monkeypatch):
        """Ohne csv_path gilt weiterhin das Skriptverzeichnis (Produktionsverhalten)."""
        import weather_forecast as wf
        erfasst = {}

        async def fake_ensure(csv_file):
            erfasst['pfad'] = csv_file

        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr(wf, '_ensure_forecast_csv_header', fake_ensure)
        monkeypatch.setattr(wf.aiofiles, 'open', MagicMock(return_value=mock_cm))

        await wf.log_forecast_to_csv(1.0, 2.0, 3.0, "06:00", "20:00", "06:01", "20:01")

        assert erfasst['pfad'].endswith('sonnen_prognose.csv')


# --- 3) Header-Migration (Fix: 7 -> 8 Spalten) ---------------------------------

class TestForecastCsvMigration:
    ALT_HEADER = ("Zeitstempel,Today_kWh,Tomorrow_kWh,"
                  "Sunrise_Today,Sunset_Today,Sunrise_Tomorrow,Sunset_Tomorrow")
    NEU_HEADER = ("Zeitstempel,Today_kWh,Tomorrow_kWh,Day2_kWh,"
                  "Sunrise_Today,Sunset_Today,Sunrise_Tomorrow,Sunset_Tomorrow")

    @pytest.mark.asyncio
    async def test_altes_7_spalten_format_wird_migriert(self, tmp_path):
        """Der gemeldete Bug: Code schrieb 8 Spalten in Datei mit 7-Spalten-Header."""
        from weather_forecast import log_forecast_to_csv

        p = tmp_path / "fc.csv"
        p.write_text(
            self.ALT_HEADER + "\n"
            "2024-06-01 12:00:00,1.50,2.00,05:10,21:00,05:11,20:59\n",
            encoding="utf-8",
        )

        await log_forecast_to_csv(1.0, 2.0, 3.0, "06:00", "20:00", "06:01", "20:01",
                                  csv_path=str(p))

        zeilen = p.read_text(encoding="utf-8").strip().splitlines()
        # Header ist jetzt 8-spaltig
        assert zeilen[0] == self.NEU_HEADER
        # Alte Zeile: Day2-Feld leer eingefuegt, sonst unveraendert
        alte = zeilen[1].split(",")
        assert len(alte) == 8, f"Alte Zeile hat {len(alte)} Felder, erwartet 8: {alte}"
        assert alte[3] == "", "Day2-Feld der Altzeile muss leer sein"
        assert alte[1] == "1.50" and alte[4] == "05:10" and alte[-1] == "20:59"
        # Neue Zeile: vollstaendig inkl. Day2
        neue = zeilen[2].split(",")
        assert len(neue) == 8 and neue[3] == "3.00"

    @pytest.mark.asyncio
    async def test_neue_datei_bekommt_8_spalten_header(self, tmp_path):
        from weather_forecast import log_forecast_to_csv

        p = tmp_path / "fc.csv"
        await log_forecast_to_csv(1.0, 2.0, 3.0, "06:00", "20:00", "06:01", "20:01",
                                  csv_path=str(p))
        zeilen = p.read_text(encoding="utf-8").strip().splitlines()
        assert zeilen[0] == self.NEU_HEADER
        assert len(zeilen[1].split(",")) == 8

    @pytest.mark.asyncio
    async def test_korrekter_header_wird_nicht_neu_geschrieben(self, tmp_path):
        """Idempotenz: Bei aktuellem Header nur anhaengen, nichts migrieren."""
        from weather_forecast import log_forecast_to_csv

        p = tmp_path / "fc.csv"
        await log_forecast_to_csv(1.0, 2.0, 3.0, "06:00", "20:00", "06:01", "20:01",
                                  csv_path=str(p))
        stand_nach_erstem_aufruf = p.read_text(encoding="utf-8")

        await log_forecast_to_csv(4.0, 5.0, 6.0, "07:00", "19:00", "07:01", "19:01",
                                  csv_path=str(p))

        inhalt = p.read_text(encoding="utf-8")
        zeilen = inhalt.strip().splitlines()
        assert len(zeilen) == 3                      # Header + 2 Datenzeilen
        assert inhalt.startswith(stand_nach_erstem_aufruf)  # nur angehaengt


# --- 4) Integration main.check_periodic_tasks ----------------------------------

@pytest.mark.asyncio
async def test_forecast_ohne_aktualisierungszeitpunkt_wird_stale():
    from types import SimpleNamespace
    import priority_control_logic as pcl

    state = SimpleNamespace(
        local_tz=__import__('pytz').timezone('Europe/Berlin'),
        solar=SimpleNamespace(
            forecast_today=3.1,
            forecast_tomorrow=2.5,
            forecast_day2=2.0,
        ),
        last_forecast_update=None,
        forecast_stale=False,
    )
    assert pcl._forecast_daten_veraltet(state) is True
    pcl._set_stale_forecast(state)
    assert state.forecast_stale is True
    assert state.solar.forecast_today is None


def test_forecast_stale_und_altes_datenalter_setzen_flags():
    from datetime import timedelta
    from types import SimpleNamespace
    import pytz
    import priority_control_logic as pcl

    tz = pytz.timezone("Europe/Berlin")
    state = SimpleNamespace(
        local_tz=tz,
        last_forecast_update=datetime.now(tz) - timedelta(hours=13),
        solar=SimpleNamespace(forecast_today=3000.0),
    )
    assert pcl._forecast_daten_veraltet(state) is True
    assert state.forecast_stale is True
    assert state.forecast_age_s > FORECAST_MAX_AGE_HOURS * 3600 - 2


def test_solar_negative_zeitstempel_ist_stale():
    from datetime import timedelta
    from types import SimpleNamespace
    import pytz
    import priority_control_logic as pcl

    tz = pytz.timezone("Europe/Berlin")
    state = SimpleNamespace(
        solar=SimpleNamespace(last_api_call=datetime.now(tz) + timedelta(minutes=2))
    )
    assert pcl._solar_daten_veraltet(state) is True


    from logic_utils import normalize_forecast_wh_qm

    assert normalize_forecast_wh_qm(3.1, value_unit="kwh_m2") == pytest.approx(3100.0)
    assert normalize_forecast_wh_qm(3100.0, value_unit="wh_m2") == pytest.approx(3100.0)
    assert normalize_forecast_wh_qm(0, value_unit="kwh_m2") == 0
    assert normalize_forecast_wh_qm(float("nan"), value_unit="kwh_m2") is None
    assert normalize_forecast_wh_qm(float("inf"), value_unit="kwh_m2") is None
    assert normalize_forecast_wh_qm(-1, value_unit="kwh_m2") is None
    with pytest.raises(ValueError):
        normalize_forecast_wh_qm(1.0, value_unit="unbekannt")


    """Test ob main.py check_periodic_tasks() mit 7 Werten umgehen kann."""

    def _baue_state(self):
        from types import SimpleNamespace
        from json_config import SommerModusConfig, LegionellenConfig
        state = SimpleNamespace()
        state.local_tz = pytz.timezone("Europe/Berlin")
        state.last_forecast_update = None
        state.config = None
        state.sommer_modus_aktiv = False
        state.sommer_modus_zaehler = 0
        state.legionellen_aktiv = False
        state.legionellen_last_done = None
        state.legionellen_started_at = None
        state.legionellen_planned_day = None
        state.legionellen_planned_time = None
        state.legionellen_planned_reason = None
        state.legionellen_telegram_start_sent = False
        state.legionellen_telegram_done_sent = False
        state.legionellen_temp_override = None
        state.legionellen_target_reached_at = None
        state.legionellen_wochennummer = None
        state.solar = SimpleNamespace(forecast_today=None, forecast_tomorrow=None,
                                      forecast_day2=None)
        state.priority_config = SimpleNamespace(
            sommer_modus=SommerModusConfig(
                aktiv=True, mindest_prognose_wh=2000.0,
                benoetigte_tage=3, temperatur_offset_c=-3.0,
            ),
            legionellen=LegionellenConfig(aktiv=False),
        )
        return state

    @pytest.mark.asyncio
    async def test_check_periodic_tasks_handles_7_values(self):
        from main import check_periodic_tasks
        state = self._baue_state()

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (2.5, 3.0, 1.5, "06:15", "20:15", "06:16", "20:14", {})
            with patch("main.check_vpn_status", new_callable=AsyncMock):
                last_check = datetime.now() - timedelta(hours=2)  # naiv, wie in main.py
                result = await check_periodic_tasks(AsyncMock(), state, last_check)

                assert result is not None
                assert state.solar.forecast_today == 2.5
                assert state.solar.forecast_tomorrow == 3.0
                assert state.solar.forecast_day2 == 1.5
                assert state.solar.sunrise_today == "06:15"
                assert state.solar.sunset_today == "20:15"

    @pytest.mark.asyncio
    async def test_check_periodic_tasks_reicht_sommer_modus_wh_werte(self):
        from main import check_periodic_tasks
        state = self._baue_state()
        state.priority_config.sommer_modus.benoetigte_tage = 1

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (2.5, 3.0, 2.0, "06:15", "20:15", "06:16", "20:14", {})
            with patch("main.check_vpn_status", new_callable=AsyncMock), patch(
                "main.evaluate_sommer_modus",
                return_value=(1, True, datetime.now().date(), "aktiviert"),
            ) as mock_sommer:
                await check_periodic_tasks(
                    AsyncMock(), state, datetime.now() - timedelta(hours=2)
                )

        kwargs = mock_sommer.call_args.kwargs
        assert kwargs["rad_today"] == pytest.approx(2500.0)
        assert kwargs["rad_tomorrow"] == pytest.approx(3000.0)
        assert kwargs["rad_day2"] == pytest.approx(2000.0)
        # Der State bleibt als kWh/m²-Versorgung erhalten.
        assert state.solar.forecast_today == 2.5
        assert state.solar.forecast_tomorrow == 3.0

    @pytest.mark.asyncio
    async def test_check_periodic_tasks_handles_none_values(self):
        from main import check_periodic_tasks
        state = self._baue_state()

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (None,) * 8
            with patch("main.check_vpn_status", new_callable=AsyncMock):
                last_check = datetime.now() - timedelta(hours=2)  # naiv, wie in main.py
                result = await check_periodic_tasks(AsyncMock(), state, last_check)

                assert result is not None
                assert state.solar.forecast_today is None

    @pytest.mark.asyncio
    async def test_fehlversuch_wird_erst_nach_retry_intervall_wiederholt(self):
        """Regression: Bei Netzausfall (Open-Meteo nicht erreichbar) wurde die
        API im 10-s-Loop-Takt endlos angefragt -> API-/Log-Spam im Pi-Log.
        Jetzt gilt ein Retry-Throttle (FORECAST_RETRY_INTERVAL_MIN)."""
        from main import check_periodic_tasks
        from constants import FORECAST_RETRY_INTERVAL_MIN
        state = self._baue_state()

        with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
            mock_forecast.return_value = (None,) * 8
            with patch("main.check_vpn_status", new_callable=AsyncMock):
                last_check = datetime.now() - timedelta(hours=2)

                # 1) Erster Fehlversuch -> anfragen, aber NICHT als Erfolg verbuchen
                await check_periodic_tasks(AsyncMock(), state, last_check)
                assert mock_forecast.await_count == 1
                assert state.last_forecast_update is None
                assert state.last_forecast_attempt is not None

                # 2) Direkt danach (naechster 10-s-Loop) -> kein zweiter Abruf
                await check_periodic_tasks(AsyncMock(), state, last_check)
                assert mock_forecast.await_count == 1

                # 3) Nach Ablauf des Retry-Intervalls -> wieder erlaubt
                state.last_forecast_attempt = (
                    datetime.now(state.local_tz)
                    - timedelta(minutes=FORECAST_RETRY_INTERVAL_MIN + 1)
                )
                await check_periodic_tasks(AsyncMock(), state, last_check)
                assert mock_forecast.await_count == 2
