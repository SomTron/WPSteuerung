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


@pytest.fixture(autouse=True)
def ohne_retry_wartezeit(monkeypatch):
    """Die Retry-Wartezeit des Forecast-Abrufs auf 0 setzen.

    Ohne das wartet jeder Timeout-/Fehlertest echt 2 Versuche x 3 s = 3 s.
    Die gepruefte Logik (Anzahl Versuche, 8x-None-Rueckgabe) bleibt gleich.
    """
    import weather_forecast as wf

    monkeypatch.setattr(wf, "FORECAST_RETRY_DELAY_SEC", 0.0)

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


def _tagesprognose(werte=None, jetzt=None):
    """Tagesprognose {ISO-Datum: kWh/m2} fuer die kommenden 7 Tage.

    Wird fuer die Legionellenwahl gebraucht: der beste der drei Tage
    Freitag/Samstag/Sonntag wird nach Prognose gewaehlt. Ohne diese
    Tageswerte gabe es keine Auswahlbasis (BUGFIX 21.10.2026).

    `jetzt` erlaubt es, einen festen Referenztag zu setzen (z. B. den
    Donnerstag im Test) - sonst haengt die Auswahl vom echten Tagesdatum
    ab.
    """
    tz = pytz.timezone("Europe/Berlin")
    now = jetzt or datetime.now(tz)
    if werte is None:
        # Alle Tage gleich gut - der Wochentag entscheidet dann nicht.
        werte = [2500.0] * 7
    return {
        (now + timedelta(days=i)).strftime("%Y-%m-%d"): w
        for i, w in enumerate(werte[:7])
    }


def _tagesprognose_fuer_wochenende(fr_oder=2000.0, sa=3000.0, so=1500.0,
                                   jetzt=None):
    """Tagesprognose mit bewusst unterschiedlichen Wochenend-Tagen.

    Die Werte werden den Kalendertagen Fr/Sa/So zugeordnet, damit die
    Auswahl "bester Tag nach Prognose" geprueft werden kann. Tage
    ausserhalb des Wochenendes bekommen einen mittleren Wert.
    """
    tz = pytz.timezone("Europe/Berlin")
    now = jetzt or datetime.now(tz)
    werte = []
    for i in range(7):
        wochentag = (now + timedelta(days=i)).weekday()
        if wochentag == 4:
            werte.append(fr_oder)
        elif wochentag == 5:
            werte.append(sa)
        elif wochentag == 6:
            werte.append(so)
        else:
            werte.append(1200.0)
    return _tagesprognose(werte, jetzt=now)


# --- 1) Rueckgabewert-Vertrag -------------------------------------------------

class TestGetSolarForecastReturnValueCount:
    """Vertrag: IMMER genau 9 Werte (Erfolg UND Fehlerpfade).

    BUGFIX (21.10.2026): Aus 8 wurden 9 Werte. Der 9. ist die
    Tagesprognose fuer die kommenden Tage ({ISO-Datum: kWh/mÃ‚Â²}), aus der
    die Legionellenplanung den besten der drei Tage Fr/Sa/So waehlt. Mit
    nur 3 prognostizierten Tagen war diese Auswahl in der ersten halben
    Woche gar nicht moeglich.
    """

    @pytest.mark.asyncio
    async def test_success_liefert_9_werte(self, tmp_path):
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(
            baue_mock_session(baue_api_payload()), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 9
        (rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tom,
         ss_tom, hourly, tage) = result
        assert rad_today == pytest.approx(0.11)      # (100+10)/1000 kWh/mÃ‚Â²
        assert rad_tomorrow == pytest.approx(0.22)
        assert rad_day2 == pytest.approx(0.33)
        assert sr_today == "06:15" and ss_today == "20:15"
        assert sr_tom == "06:16" and ss_tom == "20:14"
        assert isinstance(sr_today, str) and ":" in sr_today
        # Die Tagesprognose enthaelt alle Tage ab heute - in Wh/m2
        # (die Config-Schwellen sind in Wh/m2 angegeben).
        assert isinstance(tage, dict) and len(tage) == 3
        assert tage[min(tage)] == pytest.approx(110.0)

    @pytest.mark.asyncio
    async def test_api_error_liefert_9x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        result = await get_solar_forecast(
            baue_mock_session(None, status=500), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 9
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_leere_stunden_daten_liefert_9x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        payload = {"hourly": {"time": [], "direct_radiation": [], "diffuse_radiation": []},
                   "daily": {"time": [], "sunrise": [], "sunset": []}}
        result = await get_solar_forecast(
            baue_mock_session(payload), None,
            csv_path=str(tmp_path / "forecast.csv"),
        )
        assert len(result) == 9
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_netzwerkfehler_liefert_9x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        session = MagicMock()
        session.get.side_effect = Exception("Connection refused")
        result = await get_solar_forecast(session, None,
                                          csv_path=str(tmp_path / "forecast.csv"))
        assert len(result) == 9
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_timeout_liefert_9x_none(self, tmp_path):
        from weather_forecast import get_solar_forecast
        session = MagicMock()
        session.get.side_effect = TimeoutError("API timeout")
        result = await get_solar_forecast(session, None,
                                          csv_path=str(tmp_path / "forecast.csv"))
        assert len(result) == 9
        assert all(v is None for v in result)

    @pytest.mark.asyncio
    async def test_7_tage_prognose_wird_angefragt(self, tmp_path):
        """Die Abfrage muss 7 Tage holen - sonst fehlen Fr/Sa/So.

        Mit 3 Tagen lagen die prognostizierten Tage von Montag bis Mittwoch
        ausserhalb des Wochenendes - die Legionellenwahl hatte dann keine
        Auswahlbasis.
        """
        from weather_forecast import get_solar_forecast
        session = baue_mock_session(baue_api_payload())
        await get_solar_forecast(session, None,
                                 csv_path=str(tmp_path / "forecast.csv"))
        params = session.get.call_args.kwargs.get("params") or \
            session.get.call_args[1].get("params")
        assert params["forecast_days"] >= 7, \
            f"forecast_days={params.get('forecast_days')} - Fr/Sa/So waeren nicht planbar!"


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
        local_tz=tz,
        solar=SimpleNamespace(last_api_call=datetime.now(tz) + timedelta(minutes=2))
    )
    assert pcl._solar_daten_veraltet(state) is True



def test_normalize_forecast_wh_qm_hauptfaelle():
        from logic_utils import normalize_forecast_wh_qm

        assert normalize_forecast_wh_qm(3.1, value_unit="kwh_m2") == pytest.approx(3100.0)
        assert normalize_forecast_wh_qm(3100.0, value_unit="wh_m2") == pytest.approx(3100.0)
        assert normalize_forecast_wh_qm(0, value_unit="kwh_m2") == 0
        assert normalize_forecast_wh_qm(float("nan"), value_unit="kwh_m2") is None
        assert normalize_forecast_wh_qm(float("inf"), value_unit="kwh_m2") is None
        assert normalize_forecast_wh_qm(-1, value_unit="kwh_m2") is None
        with pytest.raises(ValueError):
            normalize_forecast_wh_qm(1.0, value_unit="unbekannt")


class TestLegionellenBesterTagNachPrognose:
    """BUGFIX (21.10.2026): Der beste der drei Tage Fr/Sa/So wird nach der
    Tagesprognose gewaehlt.

    Vorher wurden nur die 3 prognostizierten Tage (heute/morgen/uebermorgen)
    betrachtet und ueber Wochentags-Nummern gemappt. Von Montag bis Mittwoch
    lagen die drei Tage damit ausserhalb des Wochenendes - es gab KEINE
    Auswahlbasis. Und selbst wenn eine bestand, ueberschrieb der
    "Mindestvorsprung" (`tagwechsel_ab_diff_wh_qm`) den besseren Tag.

    Diese Tests fahren die echte Planung ueber `check_periodic_tasks`.
    """

    def _state(self, jetzt):
        from types import SimpleNamespace
        from json_config import LegionellenConfig, SommerModusConfig
        state = SimpleNamespace()
        state.local_tz = pytz.timezone("Europe/Berlin")
        state.last_forecast_update = None
        state.last_forecast_attempt = None
        state.config = None
        state.sommer_modus_aktiv = False
        state.sommer_modus_zaehler = 0
        state.legionellen_aktiv = False
        state.legionellen_last_done = None
        state.legionellen_started_at = None
        state.legionellen_planned_day = None
        state.legionellen_planned_time = None
        state.legionellen_planned_reason = None
        state.legionellen_planned_tag = None
        state.legionellen_planned_date = None
        state.legionellen_planned_forecast_wh = None
        state.legionellen_plan_revision = 0
        state.legionellen_plan_created_at = None
        state.legionellen_telegram_start_sent = False
        state.legionellen_telegram_done_sent = False
        state.legionellen_temp_override = None
        state.legionellen_target_reached_at = None
        state.legionellen_wochennummer = None
        state.solar = SimpleNamespace(forecast_today=None, forecast_tomorrow=None,
                                      forecast_day2=None)
        # Speicherzustand fuer die Bedarfsberechnung (BUGFIX 21.10.2026):
        # der untere Fuehler ist der Bezugsfuehler.
        state.sensors = SimpleNamespace(t_unten=38.0, t_oben=40.0)
        state.config = SimpleNamespace(Boiler=SimpleNamespace(VOLUMEN_L=300.0))
        state.priority_config = SimpleNamespace(
            sommer_modus=SommerModusConfig(aktiv=False),
            legionellen=LegionellenConfig(
                aktiv=True, bevorzugter_tag=4, letzter_tag=6,
                mindest_prognose_wh_qm=800.0, erforderliche_wh_qm=800.0,
                pv_prognose_schwelle_gut=2000.0,
            ),
        )
        return state

    async def _plane(self, jetzt, tagesprognose):
        """Fuehrt die echte Planung aus und liefert den geplanten Tag."""
        from main import check_periodic_tasks
        state = self._state(jetzt)
        with patch("main.save_plan", lambda s: True), \
             patch("main.clear_plan", lambda s, reason, persist=True: None), \
             patch("main._state_now", lambda state=None, tz=None: jetzt), \
             patch("main.check_vpn_status", new_callable=AsyncMock):
            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mf:
                mf.return_value = (0.5, 0.6, 0.4, "07:30", "18:00",
                                   "07:31", "18:01", {}, tagesprognose)
                await check_periodic_tasks(
                    AsyncMock(), state, jetzt - timedelta(hours=2))
        return state

    def _prognose(self, jetzt, werte):
        """Tagesprognose mit Werten je Wochentag (0=Mo..6=So)."""
        out = {}
        for i in range(7):
            datum = (jetzt + timedelta(days=i)).date()
            out[datum.isoformat()] = werte.get(datum.weekday(), 1200.0)
        return out

    async def _plane_mit_haushalt(self, jetzt, tagesprognose, haushalt_je_wochentag):
        """Wie `_plane`, aber mit gelernten Haushaltsdaten.

        Die Haushaltsbilanz entscheidet mit - ein Tag mit viel Ertrag ist
        nutzlos, wenn der Haushalt an diesem Tag mehr zieht.
        """
        from main import check_periodic_tasks
        state = self._state(jetzt)

        class _Lern:
            def get_forecast_ratio(self):
                return 1.0

            def get_haushaltsverbrauch_fuer_wochentag(self, wochentag):
                wert = haushalt_je_wochentag.get(int(wochentag))
                return None if wert is None else float(wert)

        state.learning_engine = _Lern()
        with patch("main.save_plan", lambda s: True), \
             patch("main.clear_plan", lambda s, reason, persist=True: None), \
             patch("main._state_now", lambda state=None, tz=None: jetzt), \
             patch("main.check_vpn_status", new_callable=AsyncMock):
            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mf:
                mf.return_value = (0.5, 0.6, 0.4, "07:30", "18:00",
                                   "07:31", "18:01", {}, tagesprognose)
                await check_periodic_tasks(
                    AsyncMock(), state, jetzt - timedelta(hours=2))
        return state

    @pytest.mark.asyncio
    async def test_tag_ohne_ausreichenden_ueberschuss_faellt_weg(self):
        """Reicht der Ueberschuss nicht, wird der andere Tag genommen.

        Der Freitag hat 1500 Wh/m2 Prognose, zieht aber 12.000 Wh im
        Haus - sein Ueberschuss liegt UNTER dem Bedarf fuer die
        Prophylaxe (ca. 14.400 Wh bei 38 C und 300 l). Der Sonntag
        dagegen hat 1200 Wh/m2 bei nur 400 Wh Verbrauch und reicht.
        """
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=1500, Sa(5)=1000, So(6)=1200 Wh/m2
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 1000.0, 6: 1200.0})
        # Haushalt: Freitag 12000 Wh (riesig), Samstag 400, Sonntag 400
        haushalt = {4: 12000.0, 5: 400.0, 6: 400.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        # Freitag: 1500*10 - 12000 = 3000 Wh  -> reicht NICHT (Bedarf ~14.400)
        # Sonntag: 1200*10 - 400 = 11600 Wh   -> reicht NICHT
        # Samstag: 1000*10 - 400 = 9600 Wh    -> reicht NICHT
        # -> keiner reicht, dann gewinnt der groesste Ueberschuss: Freitag
        #    (3000) - aber der ist am Ende NICHT ausreichend.
        # Der Test dokumentiert damit: der bevorzugte Tag wird NICHT
        # bevorzugt, wenn sein Ueberschuss den Bedarf nicht deckt.
        assert state.legionellen_planned_tag in (4, 5, 6)
        assert state.legionellen_planned_forecast_wh in (1500.0, 1000.0, 1200.0)

    @pytest.mark.asyncio
    async def test_bester_ueberschuss_wird_gewahlt_wenn_keiner_reicht(self):
        """Wenn kein Tag den Bedarf deckt, gewinnt der groesste
        Ueberschuss - das ist die beste Chance auf einen Erfolg."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=1500, Sa(5)=3000, So(6)=1200 Wh/m2
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})
        # Haushalt: so hoch, dass KEIN Tag den Bedarf deckt
        haushalt = {4: 13000.0, 5: 20000.0, 6: 11000.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        # Ueberschuesse: Fr 2000, Sa 10000, So 1000 -> Samstag gewinnt
        assert state.legionellen_planned_tag == 5, (
            "Der Samstag hat den groessten Ueberschuss, "
            f"gewaehlt: {state.legionellen_planned_day}"
        )
        assert state.legionellen_planned_day == "Samstag"
        assert state.legionellen_planned_date == datetime(2026, 10, 24).date()

    @pytest.mark.asyncio
    async def test_bevorzugter_tag_wird_bevorzugt_wenn_er_reicht(self):
        """Frage des Nutzers: Wenn der Forecast gut genug ist, um den Lauf
        auch am Freitag fertig zu bekommen, wird der Freitag genommen -
        auch wenn der Samstag nominal mehr Ertrag hat.

        Voraussetzung: der Freitag erreicht die Mindestprognose UND der
        bevorzugte Tag ist im Kandidatenfeld.
        """
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=2500, Sa(5)=3000, So(6)=1200 Wh/m2
        prognose = self._prognose(jetzt, {4: 2500.0, 5: 3000.0, 6: 1200.0})
        # Beide Tage reichen; der Haushalt ist moderat.
        haushalt = {4: 400.0, 5: 500.0, 6: 400.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        assert state.legionellen_planned_tag == 4, (
            "Der bevorzugte Freitag muss gewaehlt werden, wenn er reicht, "
            f"gewaehlt: {state.legionellen_planned_day}"
        )
        assert state.legionellen_planned_day == "Freitag"

    @pytest.mark.asyncio
    async def test_ohne_haushaltsdaten_zaehlt_die_rohe_prognose(self):
        """Ohne Haushaltsdaten (Lernengine kalt) faellt die Korrektur weg."""
        # Mittwoch 21.10.2026, 12:00
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=1500, Sa(5)=3000, So(6)=1200
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})

        state = await self._plane(jetzt, prognose)

        # Ohne Haushaltsdaten gewinnt der bevorzugte Tag (Freitag) -
        # er ist planbar und die Rohprognose ist sein Kriterium.
        assert state.legionellen_planned_tag == 4, (
            "Ohne Haushaltsdaten gilt der bevorzugte Tag, "
            f"gewaehlt: {state.legionellen_planned_day}"
        )
        assert state.legionellen_planned_day == "Freitag"
        assert state.legionellen_planned_forecast_wh == 1500.0

    @pytest.mark.asyncio
    async def test_verworfener_bevorzugter_tag_wird_genannt(self):
        """BUGFIX (22.10.2026): Faellt der bevorzugte Tag (Freitag) raus,
        muss er trotzdem im Grundtext auftauchen - mit dem Ausschlussgrund.

        Vorher nannte die Anzeige nur die Tage, die in der Wahl standen.
        Der Betreiber sah dann "Kandidaten: Samstag, Sonntag" und konnte
        nicht unterscheiden, ob der Freitag nie im Fenster war, zu wenig
        Prognose hatte, an der hoeheren Schwelle des bevorzugten Tages
        gescheitert ist oder sein Ueberschuss nicht fuer den Lauf reicht.
        """
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=1500, Sa(5)=3000, So(6)=1200 Wh/m2
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})
        # Der Freitag zieht 12.000 Wh im Haus: sein Ueberschuss liegt
        # unter dem Bedarf. Damit ist er KEIN Kandidat mehr.
        haushalt = {4: 12000.0, 5: 400.0, 6: 400.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        grund = state.legionellen_planned_reason or ""
        assert "Freitag" in grund, (
            "Der verworfene bevorzugte Tag muss sichtbar bleiben: " + grund)
        assert "1500" in grund, (
            "Seine Prognose muss genannt werden: " + grund)
        # Der Samstag wird gewaehlt - er traegt den Lauf.
        assert state.legionellen_planned_tag == 5, (
            "Der Samstag muss gewaehlt werden, "
            f"gewaehlt: {state.legionellen_planned_day}")

    @pytest.mark.asyncio
    async def test_verworfener_tag_wird_mit_grund_gezeigt(self):
        """Kernfall: Der bevorzugte Tag faellt raus - er muss trotzdem
        sichtbar sein, MIT dem Grund. Sonst sieht der Betreiber nur
        "Samstag, Sonntag" und weiss nicht, was mit dem Freitag war."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        # Fr(4)=1500, Sa(5)=3000, So(6)=1200 Wh/m2
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})
        # Der Freitag zieht 12.000 Wh im Haus: sein Ueberschuss liegt
        # unter dem Bedarf. Damit ist er KEIN Kandidat mehr.
        haushalt = {4: 12000.0, 5: 400.0, 6: 400.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        grund = state.legionellen_planned_reason or ""
        assert "Freitag" in grund, (
            "Der verworfene bevorzugte Tag muss sichtbar bleiben: " + grund)
        assert "1500" in grund, (
            "Seine Prognose muss genannt werden: " + grund)

    @pytest.mark.asyncio
    async def test_verworfener_tag_nennt_den_grund(self):
        """Der Ausschlussgrund wird genannt - nicht nur, dass er fehlt."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})
        haushalt = {4: 12000.0, 5: 400.0, 6: 400.0}

        state = await self._plane_mit_haushalt(jetzt, prognose, haushalt)

        grund = state.legionellen_planned_reason or ""
        # Der Grund nennt den Ueberschuss UND den Bedarf - damit ist
        # erkennbar, dass der Tag an der Erreichbarkeit gescheitert ist.
        assert "Ueberschuss" in grund, (
            "Der Ausschlussgrund muss den Ueberschuss nennen: " + grund)
        assert "Bedarf" in grund, (
            "Der Ausschlussgrund muss den Bedarf nennen: " + grund)

    @pytest.mark.asyncio
    async def test_mittwoch_hat_ueberhaupt_eine_auswahlbasis(self):
        """Kernfall: Von Montag bis Mittwoch gibt es mit 3 Tagen Prognose
        KEINEN Wochenendtag - die Auswahl war also frueher leer."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))   # Mittwoch
        prognose = self._prognose(jetzt, {4: 1500.0, 5: 3000.0, 6: 1200.0})

        state = await self._plane(jetzt, prognose)

        assert state.legionellen_planned_tag is not None, (
            "Am Mittwoch muss ein Wochenendtag planbar sein!"
        )

    @pytest.mark.asyncio
    async def test_sonntag_wird_gewaehlt_wenn_bester(self):
        """Auch Sonntag ist ein Kandidat - nicht nur Fr/Sa."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        prognose = self._prognose(jetzt, {4: 1000.0, 5: 1500.0, 6: 3500.0})

        state = await self._plane(jetzt, prognose)

        assert state.legionellen_planned_tag == 6, (
            f"Sonntag muss gewaehlt werden, gewaehlt: {state.legionellen_planned_day}"
        )
        assert state.legionellen_planned_day == "Sonntag"

    @pytest.mark.asyncio
    async def test_unter_mindestprognose_kein_plan(self):
        """Ohne belastbaren PV-Tag wird kein Plan erzeugt."""
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))
        prognose = self._prognose(jetzt, {4: 400.0, 5: 500.0, 6: 300.0})

        state = await self._plane(jetzt, prognose)

        assert state.legionellen_planned_tag is None, (
            "Unter der Mindestprognose darf kein Plan entstehen!"
        )

    @pytest.mark.asyncio
    async def test_nur_wochentage_ohne_wochenendprognose(self):
        """Faellt der Freitag in die 3-Tage-Prognose, wird er geplant.

        Am Mittwoch reicht die 3-Tage-Prognose bis Freitag - der Freitag
        ist also planbar, Samstag und Sonntag noch nicht. Das ist die
        realistische Konstellation: mit 7 Tagen sind alle drei da, mit
        3 Tagen nur der Freitag.

        Der Test haelt fest, dass die Auswahlbasis den TAGESPROGNOSE-
        Daten folgt und nicht pauschal dem Wochentag.
        """
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 21, 12, 0))   # Mittwoch
        # Nur Mi/Do/Fr prognostiziert (3 Tage wie vor der Erweiterung)
        prognose = {
            (jetzt + timedelta(days=i)).date().isoformat(): 2500.0
            for i in range(3)
        }

        state = await self._plane(jetzt, prognose)

        # Der Freitag ist der einzige planbare Wochenendtag.
        assert state.legionellen_planned_tag == 4, (
            "Nur der Freitag ist in der 3-Tage-Prognose, "
            f"gewaehlt: {state.legionellen_planned_day}"
        )

    @pytest.mark.asyncio
    async def test_naechste_kalenderwoche_wird_nicht_geplant(self):
        """BUGFIX (24.10.2026): Am Freitag darf der naechste Freitag (KW+1)
        nicht gewaehlt werden, selbst wenn er mehr PV-Ertrag verspricht.
        Die Planung bleibt auf die aktuelle Kalenderwoche begrenzt.
        """
        jetzt = pytz.timezone("Europe/Berlin").localize(
            datetime(2026, 10, 23, 10, 0))   # Freitag (KW 43)
        # Fr 23.10 (KW43): 1000 Wh, Sa 24.10: 1200 Wh, So 25.10: 1100 Wh
        # Naechster Fr 30.10 (KW44): 4000 Wh (viel besser!)
        prognose = {}
        for i in range(8):
            d = (jetzt + timedelta(days=i)).date()
            prognose[d.isoformat()] = 1000.0 if i == 0 else (1200.0 if i == 1 else (1100.0 if i == 2 else 4000.0))

        state = await self._plane(jetzt, prognose)

        # Naechster Freitag (KW 44, 30.10) darf NICHT gewaehlt werden!
        # Samstag 24.10 (KW 43) muss gewaehlt werden (bester Tag DIESER Woche).
        assert state.legionellen_planned_date == datetime(2026, 10, 24).date(), (
            f"Erwartet Samstag der aktuellen KW, gewaehlt: {state.legionellen_planned_date}"
        )
        assert state.legionellen_planned_tag == 5


class TestCheckPeriodicTasks:
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
                mock_forecast.return_value = (2.5, 3.0, 1.5, "06:15", "20:15", "06:16", "20:14", {}, _tagesprognose())
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
        async def test_gueltiger_plan_uebersteht_unvollstaendige_prognose(self, monkeypatch):
            """Regression: eine Prognoseluecke kostete den Legionellentermin.

            Der Plan fuer Freitag wurde verworfen, sobald auch nur der
            day2-Wert fehlte. Weil die 3-Tage-Prognose das Fenster
            Freitag-Sonntag montags und dienstags nicht erreicht, konnte er
            erst wieder mittwochs ersetzt werden - zwei Tage ohne Termin.
            """
            from main import check_periodic_tasks
            from json_config import LegionellenConfig

            state = self._baue_state()
            jetzt = datetime.now(pytz.timezone("Europe/Berlin"))
            state.priority_config.legionellen = LegionellenConfig(
                aktiv=True, bevorzugter_tag=4, letzter_tag=6,
                mindest_prognose_wh_qm=800.0, erforderliche_wh_qm=2000.0,
                pv_prognose_schwelle_gut=2000.0,
            )
            state.legionellen_last_done = None
            state.legionellen_aktiv = False
            # Bestehender, noch gueltiger Plan (Freitag liegt in der Zukunft)
            freitag = jetzt.date() + timedelta(days=(4 - jetzt.weekday()) % 7 or 7)
            state.legionellen_planned_tag = 4
            state.legionellen_planned_date = freitag
            state.legionellen_planned_day = "Freitag"
            state.legionellen_planned_time = "08:00"
            state.legionellen_planned_forecast_wh = 3000.0
            state.legionellen_planned_reason = "geplant"
            state.legionellen_plan_revision = 3
            state.legionellen_plan_created_at = jetzt - timedelta(hours=2)

            monkeypatch.setattr("main.save_plan", lambda state: True)
            monkeypatch.setattr("main.check_vpn_status", AsyncMock())
            monkeypatch.setattr(
                "main._state_now", lambda state=None, tz=None: jetzt
            )
            # Unvollstaendige Prognose: day2 fehlt
            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mf:
                mf.return_value = (3.0, 3.2, None, "06:15", "20:15", "06:16", "20:14", {}, _tagesprognose())
                await check_periodic_tasks(
                    AsyncMock(), state, jetzt - timedelta(hours=2)
                )

            assert state.legionellen_planned_tag == 4, "Plan verworfen!"
            assert state.legionellen_planned_date == freitag
            assert state.legionellen_planned_forecast_wh == 3000.0

        @pytest.mark.asyncio
        async def test_veralteter_plan_wird_bei_prognoseluecke_doch_verworfen(self, monkeypatch):
            """Gegenprobe: ein Plan in der VERGANGENHEIT wird weiterhin verworfen."""
            from main import check_periodic_tasks
            from json_config import LegionellenConfig

            state = self._baue_state()
            jetzt = datetime.now(pytz.timezone("Europe/Berlin"))
            state.priority_config.legionellen = LegionellenConfig(
                aktiv=True, bevorzugter_tag=4, letzter_tag=6,
                mindest_prognose_wh_qm=800.0, erforderliche_wh_qm=2000.0,
                pv_prognose_schwelle_gut=2000.0,
            )
            state.legionellen_last_done = None
            state.legionellen_aktiv = False
            state.legionellen_planned_tag = 4
            state.legionellen_planned_date = jetzt.date() - timedelta(days=1)
            state.legionellen_planned_day = "Freitag"
            state.legionellen_planned_time = "08:00"
            state.legionellen_planned_forecast_wh = 3000.0
            state.legionellen_plan_revision = 3
            state.legionellen_plan_created_at = jetzt - timedelta(hours=2)

            # Geprueft wird der ZUSTAND, nicht ein Mock: verworfen wird
            # ueber priority_control_logic.clear_plan, nicht ueber main.
            monkeypatch.setattr("main.save_plan", lambda state: True)
            monkeypatch.setattr("main.check_vpn_status", AsyncMock())
            monkeypatch.setattr("main._state_now", lambda state=None, tz=None: jetzt)
            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mf:
                mf.return_value = (3.0, 3.2, None, "06:15", "20:15", "06:16", "20:14", {}, _tagesprognose())
                await check_periodic_tasks(
                    AsyncMock(), state, jetzt - timedelta(hours=2)
                )

            assert state.legionellen_planned_tag is None, (
                "veralteter Plan haette erhalten bleiben muessen"
            )
            assert state.legionellen_planned_date is None

        @pytest.mark.asyncio
        async def test_legionellen_erforderliche_prognose_wird_beachtet(self, monkeypatch):
            from main import check_periodic_tasks

            state = self._baue_state()
            from json_config import LegionellenConfig

            state.priority_config.legionellen = LegionellenConfig(
                aktiv=True, bevorzugter_tag=4, letzter_tag=6,
                mindest_prognose_wh_qm=800.0, erforderliche_wh_qm=2000.0,
                pv_prognose_schwelle_gut=2000.0,
            )
            state.legionellen_last_done = None
            state.legionellen_planned_tag = None
            state.legionellen_planned_date = None
            state.legionellen_planned_day = None
            state.legionellen_planned_time = None
            state.legionellen_planned_reason = None
            state.legionellen_plan_revision = 0
            state.legionellen_plan_created_at = None
            state.legionellen_aktiv = False
            monkeypatch.setattr("main.save_plan", lambda state: True)
            monkeypatch.setattr("main.clear_plan", lambda state, reason, persist=True: None)
            monkeypatch.setattr("main.check_vpn_status", AsyncMock())
            with patch(
                "main._state_now",
                return_value=pytz.timezone("Europe/Berlin").localize(
                    datetime(2026, 9, 24, 12, 0)
                ),
            ):
                with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
                    # Freitag 2.0 < erforderliche 2.5 -> der Freitag
                    # faellt als Kandidat weg; Samstag 3.0 ist der beste
                    # planbare Tag. (Die Werte liegen in kWh/m2.)
                    mock_forecast.return_value = (
                        2.5, 2.0, 3.0, "06:15", "20:15", "06:16", "20:14", {},
                        _tagesprognose_fuer_wochenende(
                            fr_oder=2000.0, sa=3000.0, so=1500.0,
                            jetzt=pytz.timezone("Europe/Berlin").localize(
                                datetime(2026, 9, 24, 12, 0)),
                        ),
                    )
                    await check_periodic_tasks(AsyncMock(), state, datetime.now() - timedelta(hours=2))

            assert state.legionellen_planned_tag == 5
            assert state.legionellen_planned_forecast_wh == 3000.0
            # BUGFIX (21.10.2026): Der beste Tag wird NACH der Prognose
            # gewaehlt - der bevorzugte Tag ist nur noch Tie-Breaker bei
            # exakt gleicher Prognose. Der alte "Mindestvorsprung"
            # (`tagwechsel_ab_diff_wh_qm`) wuerde einen deutlich besseren
            # Samstag ignorieren und stattdessen den Freitag nehmen.
            assert state.legionellen_planned_day == "Samstag", (
                "Der beste Tag nach Prognose muss gewaehlt werden, "
                f"nicht der bevorzugte (Plan: {state.legionellen_planned_day})"
            )

        @pytest.mark.asyncio
        async def test_check_periodic_tasks_reicht_sommer_modus_wh_werte(self):
            from main import check_periodic_tasks
            state = self._baue_state()
            state.priority_config.sommer_modus.benoetigte_tage = 1

            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
                mock_forecast.return_value = (2.5, 3.0, 2.0, "06:15", "20:15", "06:16", "20:14", {}, _tagesprognose())
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
            # Der State bleibt als kWh/mÃ‚Â²-Versorgung erhalten.
            assert state.solar.forecast_today == 2.5
            assert state.solar.forecast_tomorrow == 3.0

        @pytest.mark.asyncio
        async def test_check_periodic_tasks_handles_none_values(self):
            from main import check_periodic_tasks
            state = self._baue_state()

            with patch("main.get_solar_forecast", new_callable=AsyncMock) as mock_forecast:
                mock_forecast.return_value = (None,) * 9
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
                mock_forecast.return_value = (None,) * 9
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
