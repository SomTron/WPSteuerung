"""
Tests fuer den Legionellen-Modus (evaluate_legionellen, _wochentag_name,
_extract_einschaltpunkt/-ausschaltpunkt, _boiler_max_info).

Praemt:
  - priority_control.py: evaluate_legionellen + _wochentag_name + neue Parameter
  - priority_control_logic.py: Legionellen-Zweige in extract-Funktionen + _boiler_max_info
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, date
import pytz
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import LegionellenConfig


@pytest.fixture
def legionellen_config():
    """Typische Legionellen-Konfiguration."""
    return LegionellenConfig(
        aktiv=True,
        prioritaet=90,
        target_temp_c=60.0,
        legionellen_max_temp_c=65.0,
        probezeit_minuten=15,
        bevorzugter_tag=4,  # Freitag
        letzter_tag=6,      # Sonntag
        start_uhr=10.0,
        pv_prognose_schwelle_gut=2000.0,
        erforderliche_wh_qm=500.0,
    )


@pytest.fixture
def temp_dict():
    return {"oben": 45.0, "unten": 35.0, "mittig": 40.0, "verd": 5.0}


# ============================================================
# Tests fuer _wochentag_name
# ============================================================
class TestWochentagName:
    """Tests fuer die _wochentag_name Hilfsfunktion."""

    def test_montag(self):
        from priority_control import _wochentag_name
        assert _wochentag_name(0) == "Montag"

    def test_dienstag(self):
        from priority_control import _wochentag_name
        assert _wochentag_name(1) == "Dienstag"

    def test_sonntag(self):
        from priority_control import _wochentag_name
        assert _wochentag_name(6) == "Sonntag"

    def test_ungueltig_negativ(self):
        from priority_control import _wochentag_name
        assert "Unbekannt" in _wochentag_name(-1)

    def test_ungueltig_groesser(self):
        from priority_control import _wochentag_name
        assert "Unbekannt" in _wochentag_name(7)


# ============================================================
# Tests fuer evaluate_legionellen
# ============================================================
class TestEvaluateLegionellen:
    """Tests fuer die evaluate_legionellen Funktion."""

    def test_deaktiviert(self, legionellen_config, temp_dict):
        """Regel muss inaktiv sein, wenn Legionellen deaktiviert sind."""
        from priority_control import evaluate_legionellen
        cfg = legionellen_config
        cfg.aktiv = False
        result = evaluate_legionellen(cfg, temp_dict, datetime.now())
        assert result.aktiv is False
        assert "deaktiviert" in result.grund
        assert result.einschalten is None

    def test_kein_unten_sensor(self, legionellen_config):
        """Regel muss inaktiv sein, wenn t_unten fehlt."""
        from priority_control import evaluate_legionellen
        td = {"oben": 45.0, "mittig": 40.0}  # kein "unten"
        result = evaluate_legionellen(legionellen_config, td, datetime.now())
        assert result.aktiv is False
        assert "Sensordaten" in result.grund

    def test_bereits_in_dieser_kw_erledigt(self, legionellen_config, temp_dict):
        """Regel muss inaktiv sein, wenn bereits in dieser KW durchgefuehrt."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 10, 0, 0)  # Freitag, KW 12
        last_done = date(2025, 3, 17)  # Montag, KW 12 (gleiche Woche)
        result = evaluate_legionellen(
            legionellen_config, temp_dict, now,
            legionellen_last_done=last_done,
        )
        assert result.aktiv is False
        assert "bereits" in result.grund.lower()

    def test_andere_kw_nicht_erledigt(self, legionellen_config, temp_dict):
        """Wenn letzte Durchfuehrung in anderer KW, darf die Regel feuern."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 10, 0, 0)  # Freitag, KW 12
        last_done = date(2025, 3, 10)  # Montag, KW 11 (andere Woche)
        result = evaluate_legionellen(
            legionellen_config, temp_dict, now,
            legionellen_last_done=last_done,
        )
        assert result.aktiv is True
        assert result.einschalten is True

    def test_falsche_uhrzeit_blockiert(self, legionellen_config, temp_dict):
        """Nur zur Startzeit (10:00) darf eingeschaltet werden."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 14, 0, 0)  # 14:00, nicht 10:00
        result = evaluate_legionellen(
            legionellen_config, temp_dict, now,
        )
        assert result.aktiv is False
        assert "Startzeit" in result.grund

    def test_korrekte_startzeit_erlaubt(self, legionellen_config, temp_dict):
        """Um 10:00 Uhr muss die Regel einschalten (sofern nicht aktiv)."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 10, 0, 0)
        result = evaluate_legionellen(
            legionellen_config, temp_dict, now,
        )
        assert result.aktiv is True
        assert result.einschalten is True
        assert "faellig" in result.grund

    def test_kompressor_laeuft_blockiert(self, legionellen_config, temp_dict):
        """Wenn Kompressor bereits laeuft, darf nicht eingeschaltet werden."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 10, 0, 0)
        result = evaluate_legionellen(
            legionellen_config, temp_dict, now,
            kompressor_ein=True,
        )
        assert result.einschalten is None  # keine Aktion
        assert "Kompressor" in result.grund

    def test_aktiv_heize_weiter(self, legionellen_config, temp_dict):
        """Wenn bereits aktiv und Ziel noch nicht erreicht -> weiterheizen."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 11, 0, 0)
        td = {"oben": 50.0, "unten": 45.0, "mittig": 48.0}  # unter target=60
        result = evaluate_legionellen(
            legionellen_config, td, now,
            legionellen_aktiv=True,
            legionellen_started_at=datetime(2025, 3, 21, 10, 0, 0),
        )
        assert result.einschalten is True
        assert "heize weiter" in result.grund

    def test_aktiv_ziel_erreicht_schaltet_aus(self, legionellen_config, temp_dict):
        """Ziel erreicht (z.B. >= 60 Grad unten) -> sofort AUS."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 3, 21, 11, 0, 0)
        td = {"oben": 62.0, "unten": 60.5, "mittig": 61.0}  # >= target=60
        result = evaluate_legionellen(
            legionellen_config, td, now,
            legionellen_aktiv=True,
            legionellen_started_at=datetime(2025, 3, 21, 10, 0, 0),
        )
        assert result.einschalten is False
        assert "AUS" in result.grund


# ============================================================
# Tests fuer die Integration mit _extract_einschaltpunkt
# ============================================================
class TestExtractEinschaltpunktLegionellen:
    """_extract_einschaltpunkt muss Legionellen korrekt extrahieren."""

    def test_legionellen_einschaltpunkt(self):
        """Legionellen-Einschaltpunkt = target_temp_c - 5."""
        import priority_control_logic as pcl
        from priority_control import RegelErgebnis

        config = MagicMock()
        config.legionellen.target_temp_c = 60.0
        ergebnis = RegelErgebnis(name="Legionellen", prioritaet=90, aktiv=True, einschalten=True)

        eps = pcl._extract_einschaltpunkt(ergebnis, config)
        assert eps == 55.0  # 60 - 5

    def test_andere_regel_unveraendert(self):
        """Andere Regeln muessen weiterhin funktionieren."""
        import priority_control_logic as pcl
        from priority_control import RegelErgebnis

        config = MagicMock()
        config.sicherheit.max_temp_c = 48.0

        ergebnis = RegelErgebnis(name="UnbekannteRegel", prioritaet=10, aktiv=True)
        eps = pcl._extract_einschaltpunkt(ergebnis, config)
        assert eps == 48.0  # Fallback


class TestExtractAusschaltpunktLegionellen:
    """_extract_ausschaltpunkt muss Legionellen korrekt extrahieren."""

    def test_legionellen_ausschaltpunkt(self):
        """Legionellen-Ausschaltpunkt = target_temp_c."""
        import priority_control_logic as pcl
        from priority_control import RegelErgebnis

        config = MagicMock()
        config.legionellen.target_temp_c = 60.0
        ergebnis = RegelErgebnis(name="Legionellen", prioritaet=90, aktiv=True, einschalten=True)

        ausp = pcl._extract_ausschaltpunkt(ergebnis, config)
        assert ausp == 60.0

    def test_andere_regel_unveraendert(self):
        import priority_control_logic as pcl
        from priority_control import RegelErgebnis

        config = MagicMock()
        config.sicherheit.max_temp_c = 48.0

        ergebnis = RegelErgebnis(name="UnbekannteRegel", prioritaet=10, aktiv=True)
        ausp = pcl._extract_ausschaltpunkt(ergebnis, config)
        assert ausp == 48.0


# ============================================================
# Tests fuer _boiler_max_info mit Legionellen temp_override
# ============================================================
class TestBoilerMaxInfoLegionellen:
    """_boiler_max_info muss den legionellen_temp_override beruecksichtigen."""

    def test_ohne_override(self):
        """Ohne legionellen_temp_override bleibt das Standard-Limit."""
        import priority_control_logic as pcl

        state = MagicMock()
        state.priority_config.sicherheit.max_temp_c = 48.0
        state.priority_config.sicherheit.boiler_max_hysterese_k = 2.0
        state.priority_config.sicherheit.boiler_max_fuehler = "unten"
        state.sensors.t_unten = 46.0
        state.legionellen_temp_override = None

        temp, limit, wiederein, fuehler = pcl._boiler_max_info(state)
        assert limit == 48.0
        assert wiederein == 46.0  # 48 - 2

    def test_mit_override_erhoeht_limit(self):
        """legionellen_temp_override muss das Limit anheben."""
        import priority_control_logic as pcl

        state = MagicMock()
        state.priority_config.sicherheit.max_temp_c = 48.0
        state.priority_config.sicherheit.boiler_max_hysterese_k = 2.0
        state.priority_config.sicherheit.boiler_max_fuehler = "unten"
        state.sensors.t_unten = 55.0
        state.legionellen_temp_override = 65.0  # Legionellen aktiv

        temp, limit, wiederein, fuehler = pcl._boiler_max_info(state)
        assert limit == 65.0  # override hoeher -> nimmt override
        assert wiederein == 63.0  # 65 - 2

    def test_override_kleiner_als_max_wird_ignoriert(self):
        """Override unter Standard-Limit wird ignoriert."""
        import priority_control_logic as pcl

        state = MagicMock()
        state.priority_config.sicherheit.max_temp_c = 48.0
        state.priority_config.sicherheit.boiler_max_hysterese_k = 2.0
        state.priority_config.sicherheit.boiler_max_fuehler = "unten"
        state.sensors.t_unten = 46.0
        state.legionellen_temp_override = 45.0  # kleiner als 48

        temp, limit, wiederein, fuehler = pcl._boiler_max_info(state)
        assert limit == 48.0  # 45 < 48, also bleibt Standard


# ============================================================
# Integrationstest: bewerte_alle_regeln mit Legionellen-Parametern
# ============================================================
class TestBewerteAlleRegelnLegionellenIntegration:
    """Testet, dass bewerte_alle_regeln die Legionellen-Parameter korrekt
    annimmt und durchreicht."""

    def test_legionellen_parameter_werden_akzeptiert(self):
        """Die neuen Parameter muessen von bewerte_alle_regeln akzeptiert werden."""
        from priority_control import bewerte_alle_regeln
        from json_config import WPSteuerungConfig

        config = WPSteuerungConfig(
            beschreibung="Test",
            legionellen=LegionellenConfig(aktiv=False),
        )
        temp_dict = {"oben": 45.0, "unten": 35.0, "mittig": 40.0, "verd": 5.0}

        # Aufruf mit allen neuen Parametern (muss ohne TypeError funktionieren)
        now = datetime.now()
        gewinner, alle = bewerte_alle_regeln(
            config=config,
            temp_dict=temp_dict,
            pv_leistung=0.0,
            kompressor_ein=False,
            now=now,
            legionellen_aktiv=False,
            legionellen_last_done=None,
            legionellen_started_at=None,
            forecast_day2_wh_qm=None,
        )
        # Mindestens ein Ergebnis (Legionellen) muss vorhanden sein (inaktiv)
        assert len(alle) > 0
        legionellen_ergebnis = [e for e in alle if e.name == "Legionellen"]
        assert len(legionellen_ergebnis) == 1
        assert legionellen_ergebnis[0].aktiv is False


# ============================================================
# Tests fuer die Parameteruebergabe in determine_mode_and_setpoints
# ============================================================
class TestDetermineModeAndSetpointsLegionellen:
    """Testet, dass determine_mode_and_setpoints die Legionellen-Parameter
    korrekt an bewerte_alle_regeln uebergibt."""

    @pytest.mark.asyncio
    async def test_legionellen_params_werden_durchgereicht(self):
        """Die State-Felder muessen als Parameter ankommen."""
        import priority_control_logic as pcl

        state = MagicMock()
        state.local_tz = pytz.timezone("Europe/Berlin")
        state.solar.feedinpower = 0.0
        state.solar.forecast_tomorrow = None
        state.solar.forecast_today = None
        state.solar.forecast_day2 = None
        state.solar.soc = None
        state.solar.batpower = None
        state.sensors.t_oben = 45.0
        state.sensors.t_verd = 5.0
        state.bademodus_aktiv = False
        state.urlaubsmodus_aktiv = False
        state.sommer_modus_aktiv = False
        state.control.kompressor_ein = False
        state.control.blocking_reason = None
        state.control.previous_modus = None
        state.control._soll_einschalten = False
        state.control.alle_ergebnisse = []

        # Legionellen-State-Felder
        state.legionellen_aktiv = True
        state.legionellen_last_done = date(2025, 3, 17)
        state.legionellen_started_at = datetime(2025, 3, 21, 10, 0, 0, tzinfo=state.local_tz)

        state.stats.last_compressor_on_time = datetime.now(state.local_tz) - timedelta(minutes=30)
        state.stats.last_compressor_off_time = datetime.now(state.local_tz) - timedelta(minutes=5)

        from json_config import WPSteuerungConfig, WPConfig, ZyklusConfig, SicherheitConfig
        state.priority_config = WPSteuerungConfig(
            beschreibung="Test",
            wp=WPConfig(),
            zyklus=ZyklusConfig(),
            sicherheit=SicherheitConfig(
                nachtsperre_start=19, nachtsperre_ende=8,
                max_temp_c=48.0, ueberhitzung_c=58.0, notfall_c=36.0,
            ),
            legionellen=LegionellenConfig(aktiv=False),
        )

        for attr in ['_last_priority_log', '_last_temp_log']:
            try:
                delattr(state, attr)
            except AttributeError:
                pass

        with patch('priority_control_logic.bewerte_alle_regeln') as mock_bewerte:
            mock_bewerte.return_value = (None, [])
            await pcl.determine_mode_and_setpoints(state, 35.0, 40.0)

            args, kwargs = mock_bewerte.call_args
            # Pruefe, dass die Legionellen-Parameter uebergeben wurden
            assert kwargs.get('legionellen_aktiv') is True
            assert kwargs.get('legionellen_last_done') == date(2025, 3, 17)
            assert kwargs.get('legionellen_started_at') == datetime(2025, 3, 21, 10, 0, 0, tzinfo=state.local_tz)
            assert kwargs.get('forecast_day2_wh_qm') is None
            # Neu: Wochenende-Config wird zur Wochenende-Nachholung gereicht
            assert kwargs.get('wochenende_cfg') is state.priority_config.wochenende


# ============================================================
# Wochenende-Sperre & flexible Nachholung (Prio 90 < 100)
# ============================================================
class TestLegionellenWochenendeNachholung:
    """Am Wochenende blockt die Prio-100-Sperre Starts vor fruehestens_uhr.
    Die Regel haelt das Startfenster offen und holt die Fahrt direkt nach
    der Sperre (z.B. 09:00) nach."""

    def _weekend_cfg(self):
        from types import SimpleNamespace
        return SimpleNamespace(fruehestens_uhr=9)

    def _samstag(self, hour, minute=0):
        # 2025-06-14 ist ein Samstag
        return datetime(2025, 6, 14, hour, minute)

    def _cfg_start8(self, legionellen_config):
        cfg = legionellen_config
        cfg.start_uhr = 8.0
        cfg.spaeteste_start_uhr = 16
        return cfg

    def test_wochenende_vor_sperre_fenster_offen(self, legionellen_config, temp_dict):
        """Sa 08:30: Start faellig (8:00), wird aber von der Sperre gehalten.
        Die Regel meldet trotzdem EIN-Wunsch -> gewinnt um 9:00 sofort."""
        from priority_control import evaluate_legionellen
        erg = evaluate_legionellen(
            self._cfg_start8(legionellen_config), temp_dict,
            self._samstag(8, 30),
            wochenende_cfg=self._weekend_cfg(),
        )
        assert erg.einschalten is True
        assert "Wochenende-Sperre" in erg.grund

    def test_wochenende_nachholung_nach_sperre(self, legionellen_config, temp_dict):
        """Sa 09:05: Nach der Sperre wird sofort nachgeholt (Fenster bis 16U)."""
        from priority_control import evaluate_legionellen
        erg = evaluate_legionellen(
            self._cfg_start8(legionellen_config), temp_dict,
            self._samstag(9, 5),
            wochenende_cfg=self._weekend_cfg(),
        )
        assert erg.aktiv is True
        assert erg.einschalten is True
        assert "Nachholung" in erg.grund

    def test_wochenende_startfenster_abgelaufen(self, legionellen_config, temp_dict):
        """Sa 17:00: Nachhol-Fenster (bis spaeteste_start_uhr=16) abgelaufen."""
        from priority_control import evaluate_legionellen
        erg = evaluate_legionellen(
            self._cfg_start8(legionellen_config), temp_dict,
            self._samstag(17, 0),
            wochenende_cfg=self._weekend_cfg(),
        )
        assert erg.aktiv is False
        assert "Startfenster abgelaufen" in erg.grund

    def test_donnerstag_vor_erlaubtem_fenster_geblockt(self, legionellen_config, temp_dict):
        """Do 09:30 (start=8): Donnerstag liegt VOR dem erlaubten Fenster
        (Freitag-Sonntag) -> kein Start (Wochentags-Gate)."""
        from priority_control import evaluate_legionellen
        # 2025-06-12 = Donnerstag
        now = datetime(2025, 6, 12, 9, 30)
        erg = evaluate_legionellen(
            self._cfg_start8(legionellen_config), temp_dict, now,
            wochenende_cfg=self._weekend_cfg(),
        )
        assert erg.einschalten is None
        assert "Nur Start an" in erg.grund

    def test_freitag_startstunde_feuerung(self, legionellen_config, temp_dict):
        """Fr 08:00 (start=8): Freitag liegt im erlaubten Fenster -> Start wie
        geplant (Wochentags-Gate aktiv)."""
        from priority_control import evaluate_legionellen
        now = datetime(2025, 6, 13, 8, 0)  # Freitag
        erg = evaluate_legionellen(
            self._cfg_start8(legionellen_config), temp_dict, now,
            wochenende_cfg=self._weekend_cfg(),
        )
        assert erg.einschalten is True
        assert "Starte Erhitzung" in erg.grund

    def test_integration_samstag_9_gewinner_legionellen(self, legionellen_config):
        """End-to-End: Sa 08:30 blockt die Wochenende-Sperre (100); Sa 09:00
        gewinnt die Legionellen-Regel (90) und die Fahrt startet."""
        import priority_control as pc
        from json_config import WPSteuerungConfig

        config = WPSteuerungConfig(beschreibung="Test", legionellen=legionellen_config)
        config.wochenende.fruehestens_uhr = 9
        config.calculated_start.aktiv = False
        config.forecast.aktiv = False
        config.adaptive_pv.aktiv = False
        config.einspeisung.aktiv = False
        config.batterie.aktiv = False
        temp = {"oben": 45.0, "unten": 35.0, "mittig": 40.0, "verd": 30.0}

        vor = pc.bewerte_alle_regeln(
            config=config, temp_dict=temp, pv_leistung=0.0, kompressor_ein=False,
            now=self._samstag(8, 30),
        )
        assert vor[0].name == "Wochenende"          # Sperre blockt trotz EIN-Wunsch

        nach = pc.bewerte_alle_regeln(
            config=config, temp_dict=temp, pv_leistung=0.0, kompressor_ein=False,
            now=self._samstag(9, 0),
        )
        assert nach[0].name == "Legionellen"        # Nachholung um 09:00
        assert nach[0].einschalten is True