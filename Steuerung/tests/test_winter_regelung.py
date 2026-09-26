"""Tests fuer die aus der Mehrtages-Simulation abgeleiteten Verbesserungen.

Hintergrund (Analyse/sim, 14 bzw. 35 Tage):
  * "Auf PV warten" lohnt nur, wenn PV kommt. Ohne Ausnahme wartete die Regel
    bei trueber Wetterlage den ganzen Tag auf eine Sonne, die ausblieb.
  * Die Legionellen-Probezeit konnte nie abgeschlossen werden, weil der
    generische Ausschaltzweig den Lauf genau beim Erreichen des Ziels stoppte.
  * Ohne PV-Frist faellt die Prophylaxe in PV-armen Wochen komplett aus.

Bewusst NICHT geaendert (Nutzerentscheidung):
  * Der Notfallschutz bleibt ein reiner Schutzleiter fuer den oberen Fuehler
    mit Abschaltung bei 38 C. Er steuert die Komforttemperatur bewusst nicht
    mit - die Abweichungs-Regel ist dafuer zustaendig.
  * Die Batterie-Regel bleibt stillgelegt: sie verliert gegen den
    Notfallschutz (Prio 110) und MinTemp (Prio 65) und wuerde nur mit
    geaenderter Prioritaet oder Schwelle wieder wirksam.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control as pc  # noqa: E402
import priority_control_logic as pcl  # noqa: E402
from json_config import (  # noqa: E402
    AbweichungConfig,
    LegionellenConfig,
    WPSteuerungConfig,
    WPSteuerungConfigManager,
)

TZ = pytz.timezone("Europe/Berlin")
HEUTE = TZ.localize(datetime(2026, 12, 11, 9, 0))   # Freitag


# ==============================================================
# 1. Abweichung: bei trueber Wetterlage nicht auf PV warten
# ==============================================================
class TestQuelleWartenWetterlage:
    """`quelle_warten_min_forecast_wh_qm` hebt das Quellen-Gate auf."""

    @staticmethod
    def _aufrufen(abw, **kwargs):
        params = dict(
            kompressor_ein=False, now_hour=15,
            nachtsperre_start=19, nachtsperre_ende=8,
            feedin_watt=0.0, soc=50.0,
        )
        params.update(kwargs)
        return pc.evaluate_abweichung(
            abw, {"unten": 35.0, "mitte": 36.0, "oben": 37.0}, **params
        )

    def test_ohne_prognose_weiter_warten(self):
        """Ohne Prognose gilt das alte Verhalten: auf PV warten."""
        abw = AbweichungConfig(quelle_warten=True,
                               quelle_warten_min_forecast_wh_qm=1500.0)
        erg = self._aufrufen(abw, forecast_today_wh_qm=None)
        assert erg.einschalten is None
        assert "wartet auf PV" in erg.grund

    def test_gute_prognose_weiter_warten(self):
        """Bei guter Tagesprognose wird weiter auf PV gewartet."""
        abw = AbweichungConfig(quelle_warten=True,
                               quelle_warten_min_forecast_wh_qm=1500.0)
        erg = self._aufrufen(abw, forecast_today_wh_qm=4000.0)
        assert erg.einschalten is None
        assert "wartet auf PV" in erg.grund

    def test_truwe_weatherlage_heizt_mit_netz(self):
        """Unter der Schwelle wird nicht mehr gewartet, sondern geheizt."""
        abw = AbweichungConfig(quelle_warten=True,
                               quelle_warten_min_forecast_wh_qm=1500.0)
        erg = self._aufrufen(abw, forecast_today_wh_qm=600.0)
        assert erg.einschalten is True
        assert "kein PV zu erwarten" in erg.grund

    def test_frist_0_disabled_behaelt_altes_verhalten(self):
        """0 schaltet die Ausnahme ab - Rueckwaertskompatibilitaet."""
        abw = AbweichungConfig(quelle_warten=True,
                               quelle_warten_min_forecast_wh_qm=0.0)
        erg = self._aufrufen(abw, forecast_today_wh_qm=100.0)
        assert erg.einschalten is None
        assert "wartet auf PV" in erg.grund

    def test_solar_stale_keine_entwarnung(self):
        """Veraltete Solardaten sind keine 'truebe Wetterlage'."""
        abw = AbweichungConfig(quelle_warten=True,
                               quelle_warten_min_forecast_wh_qm=1500.0)
        erg = self._aufrufen(abw, forecast_today_wh_qm=100.0, solar_stale=True)
        assert erg.einschalten is None

    def test_quelle_warten_false_unveraendert(self):
        """Ohne quelle_warten gibt es das Gate gar nicht."""
        abw = AbweichungConfig(quelle_warten=False)
        erg = self._aufrufen(abw, forecast_today_wh_qm=300.0)
        assert erg.einschalten is True

# ==============================================================
# 2. Legionellen-Hygienefrist
# ==============================================================
class TestLegionellenFrist:
    """max_tage_ohne_lauf verhindert, dass die Prophylaxe ausfaellt."""

    @staticmethod
    def _cfg(max_tage=14):
        return LegionellenConfig(
            aktiv=True, prioritaet=90, target_temp_c=60.0,
            legionellen_max_temp_c=65.0, probezeit_minuten=15,
            bevorzugter_tag=4, letzter_tag=6, start_uhr=8,
            max_tage_ohne_lauf=max_tage,
        )

    @staticmethod
    def _aufrufen(cfg, last_done, **kwargs):
        params = dict(
            temp_dict={"unten": 45.0, "mitte": 46.0, "oben": 47.0, "verd": 20.0},
            now=HEUTE, forecast_today_wh_qm=100.0, forecast_wh_qm=100.0,
            forecast_day2_wh_qm=100.0, legionellen_last_done=last_done,
            kompressor_ein=False, pv_leistung=0.0, soc=50.0,
            solar_stale=False,
        )
        params.update(kwargs)
        return pc.evaluate_legionellen(cfg, **params)

    def test_frist_noch_nicht_erreicht_wartet_auf_pv(self):
        erg = self._aufrufen(self._cfg(14), HEUTE.date() - timedelta(days=10))
        assert erg.einschalten is None
        assert "wartet auf PV" in erg.grund

    def test_frist_ueberschritten_heizt_mit_netz(self):
        erg = self._aufrufen(self._cfg(14), HEUTE.date() - timedelta(days=20))
        assert erg.einschalten is True
        assert "Hygiene-Notfall" in erg.grund

    def test_frist_0_unbegrenzt_warten(self):
        erg = self._aufrufen(self._cfg(0), HEUTE.date() - timedelta(days=400))
        assert erg.einschalten is None
        assert "wartet auf PV" in erg.grund

    def test_ohne_vorlauf_kein_notfall(self):
        """Frischbetrieb ohne bekannten letzten Lauf: kein Netzbetrieb."""
        erg = self._aufrufen(self._cfg(14), None)
        assert erg.einschalten is None

    def test_woechentagsgate_ohne_notfall_unveraendert(self):
        """Der normale Wochentags-Gate bleibt unveraendert aktiv."""
        montag = TZ.localize(datetime(2026, 12, 7, 9, 0))
        erg = self._aufrufen(self._cfg(14), HEUTE.date() - timedelta(days=10),
                             now=montag)
        assert erg.einschalten is None
        assert "Nur Start an" in erg.grund

    def test_notfall_ignoriert_wochentags_gate(self):
        """Am Mittwoch ist normalerweise kein Start - im Notfall doch."""
        mittwoch = TZ.localize(datetime(2026, 12, 9, 9, 0))
        cfg = self._cfg(14)
        normal = self._aufrufen(cfg, HEUTE.date() - timedelta(days=10), now=mittwoch)
        assert normal.einschalten is None
        notfall = self._aufrufen(cfg, HEUTE.date() - timedelta(days=20), now=mittwoch)
        assert notfall.einschalten is True
        assert "Hygiene-Notfall" in notfall.grund
# ==============================================================
# 3. Legionellen-Probezeit wird nicht abgebrochen
# ==============================================================
class TestLegionellenProbe:
    """Der generische Ausschaltzweig darf die Probezeit nicht kappen."""

    @staticmethod
    def _state(t_unten=60.2, aktiv=True, erreicht=None):
        cfg = WPSteuerungConfig()
        return SimpleNamespace(
            local_tz=TZ,
            priority_config=cfg,
            control=SimpleNamespace(
                kompressor_ein=True,
                _soll_einschalten=True,
                _lauf_start_regel="Legionellen",
                blocking_reason=None,
                restart_lockout_until=None,
            ),
            sensors=SimpleNamespace(t_unten=t_unten, t_mittig=t_unten + 0.5,
                                    t_oben=t_unten + 1.0, t_verd=20.0),
            stats=SimpleNamespace(
                last_compressor_on_time=HEUTE - timedelta(minutes=120),
                last_compressor_off_time=HEUTE - timedelta(hours=3),
            ),
            legionellen_aktiv=aktiv,
            legionellen_target_reached_at=erreicht,
            legionellen_started_at=HEUTE - timedelta(minutes=90),
            legionellen_temp_override=65.0 if aktiv else None,
        )

    @pytest.mark.asyncio
    async def test_probe_wird_nicht_abgebrochen(self):
        """Ziel erreicht, Probe laeuft -> Kompressor bleibt an."""
        state = self._state(aktiv=True, erreicht=HEUTE - timedelta(minutes=5))
        with patch.object(pcl, "_now_for_state", return_value=HEUTE):
            erg = await pcl.handle_compressor_off(
                state, None, regelfuehler=60.2, ausschaltpunkt=60.0,
                min_laufzeit=timedelta(minutes=60), t_oben=61.2,
                set_kompressor_status_func=AsyncMock(return_value=True),
                regel_name="Legionellen",
            )
        assert erg is False

    @pytest.mark.asyncio
    async def test_nach_probe_wird_abgeschaltet(self):
        """Nach abgelaufener Probe darf abgeschaltet werden."""
        state = self._state(aktiv=True, erreicht=HEUTE - timedelta(minutes=60))
        with patch.object(pcl, "_now_for_state", return_value=HEUTE):
            erg = await pcl.handle_compressor_off(
                state, None, regelfuehler=60.2, ausschaltpunkt=60.0,
                min_laufzeit=timedelta(minutes=60), t_oben=61.2,
                set_kompressor_status_func=AsyncMock(return_value=True),
                regel_name="Legionellen",
            )
        assert erg is True

    @pytest.mark.asyncio
    async def test_ohne_probezeit_normales_abschalten(self):
        """Ohne laufende Probe greift der normale Ausschaltpfad."""
        state = self._state(aktiv=False, erreicht=None)
        with patch.object(pcl, "_now_for_state", return_value=HEUTE):
            erg = await pcl.handle_compressor_off(
                state, None, regelfuehler=48.5, ausschaltpunkt=48.0,
                min_laufzeit=timedelta(minutes=60), t_oben=48.5,
                set_kompressor_status_func=AsyncMock(return_value=True),
                regel_name="PV_unten",
            )
        assert erg is True


# ==============================================================
# 4. Projektkonfiguration
# ==============================================================
class TestProjektkonfiguration:
    """Die aus der Simulation abgeleiteten Parameter sind gesetzt."""

    @staticmethod
    def _cfg():
        mgr = WPSteuerungConfigManager()
        mgr.load_config()
        return mgr.config

    def test_notfallschutzt_nur_den_oberen_fuehler(self):
        """Nutzerwunsch: Der Notfallschutz ist ein reiner Schutzleiter fuer den
        oberen Fühler - er darf die Komforttemperatur nicht mitsteuern."""
        assert self._cfg().notfallschutz.temperaturfuehler == "oben"

    def test_notfallschutz_schaltet_bei_38c_ab(self):
        """Der Notfallschutz endet bei 38 C und bleibt damit ein Schutzleiter."""
        nf = self._cfg().notfallschutz
        assert nf.ausschalten_bei_c == 38.0
        assert nf.einschalten_bei_c == 36.0
        assert nf.ausschalten_bei_c < self._cfg().abweichung.solltemperatur_c

    def test_batterie_regel_ist_stillgelegt(self):
        """Die Batterie-Regel bleibt deaktiviert (Prioritätskonflikt)."""
        assert self._cfg().batterie.aktiv is False

    def test_hygienefrist_gesetzt(self):
        assert self._cfg().legionellen.max_tage_ohne_lauf > 0

    def test_unwetter_schwelle_gesetzt(self):
        assert self._cfg().abweichung.quelle_warten_min_forecast_wh_qm > 0