# -*- coding: utf-8 -*-
"""Tests: Forecast-Vorheizen nur mit akzeptierter Energie-Quelle.

Beobachtet (Log 25.08., 18:53): Die Forecast-Regel (Prio 57) war quellenblind
- sie heizte bei schlechter Prognose notfalls mit Batterie-/Netzstrom, ohne
auf PV zu warten. Neu gilt: EIN nur noch bei echter PV-Einspeisung oder
voller Hausbatterie ohne Netzkauf; sonst wird gewartet (stumm, kein AUS).
"""
import sys
from datetime import datetime
from types import SimpleNamespace


sys.stdout.reconfigure(encoding="utf-8")

from json_config import WPSteuerungConfig  # noqa: E402
from priority_control import evaluate_forecast, bewerte_alle_regeln  # noqa: E402


def _cfg(**overrides):
    cfg = SimpleNamespace(
        aktiv=True,
        prioritaet=57,
        temperaturfuehler="mitte",
        fc_schwelle_hoch_wh=3000.0,
        fc_schwelle_niedrig_wh=800.0,
        t_vorheiz_ab_c=44.0,
        tmax_c=48.0,
        vorheiz_start_uhr=8,
        vorheiz_ende_uhr=19,
        sparen_start_uhr=11,
        sparen_ende_uhr=15,
        pv_einspeisung_min_watt=50.0,
        soc_min_prozent=90.0,
        vorheiz_max_netzbezug_watt=-50.0,
        vorheiz_netz_erlaubt=False,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _temps(mittig=42.0):
    return {"oben": 45.0, "mittig": mittig, "unten": 41.0}


SCHLECHT = 6.0   # Wh/qm morgen - wie im beobachteten Log
GUT = 3500.0


class TestForecastQuellenGate:
    STUNDE = 18  # im Vorheiz-Fenster (8-19)

    def _rufe(self, cfg=None, feedin=0.0, soc=85.0, stunde=STUNDE):
        return evaluate_forecast(
            cfg or _cfg(), _temps(), SCHLECHT, stunde,
            feedin_watt=feedin, soc=soc,
        )

    def test_ohne_pv_und_leerer_batterie_wartet_die_regel(self):
        """Der gemeldete Fall: PV 0W, SOC unter Grenze -> KEIN Netzstrom."""
        erg = self._rufe(feedin=0.0, soc=85.0)
        assert erg.einschalten is None
        assert "wartet" in erg.grund

    def test_pv_einspeisung_schaltet_freigeben(self):
        erg = self._rufe(feedin=120.0, soc=10.0)
        assert erg.einschalten is True
        assert "PV" in erg.grund

    def test_volle_batterie_ohne_netzkauf_schaltet_frei(self):
        erg = self._rufe(feedin=-20.0, soc=90.0)  # -20 >= -50: kein Netzkauf
        assert erg.einschalten is True
        assert "Batterie" in erg.grund

    def test_batterie_mit_netzkauf_gilt_nicht(self):
        """SOC reicht, aber Haus kauft Netz (-300W < -50W) -> warten."""
        erg = self._rufe(feedin=-300.0, soc=92.0)
        assert erg.einschalten is None
        assert "wartet" in erg.grund

    def test_soc_unter_grenze_reicht_nicht(self):
        erg = self._rufe(feedin=0.0, soc=89.9)
        assert erg.einschalten is None

    def test_soc_ohne_daten_wartet(self):
        erg = self._rufe(feedin=0.0, soc=None)
        assert erg.einschalten is None
        assert "keine Daten" in erg.grund

    def test_legacy_modus_mit_netz_erlaubt(self):
        """vorheiz_netz_erlaubt=True stellt das alte Verhalten her."""
        erg = self._rufe(cfg=_cfg(vorheiz_netz_erlaubt=True), feedin=0.0, soc=20.0)
        assert erg.einschalten is True

    def test_default_felder_wenn_cfg_alt(self):
        """Alte Configs ohne neue Felder: Defaults greifen (wartet)."""
        cfg = _cfg()
        for feld in ("pv_einspeisung_min_watt", "soc_min_prozent",
                     "vorheiz_max_netzbezug_watt", "vorheiz_netz_erlaubt"):
            delattr(cfg, feld)
        erg = self._rufe(cfg=cfg, feedin=0.0, soc=50.0)
        assert erg.einschalten is None

    def test_sparen_bleibt_quellenunabhaengig(self):
        """Die Sparen-Entscheidung (AUS) braucht keine Quelle."""
        erg = evaluate_forecast(
            _cfg(), _temps(mittig=45.0), GUT, 12,
            feedin_watt=0.0, soc=10.0,
        )
        assert erg.einschalten is False
        assert "Sparen" in erg.grund

    def test_ausserhalb_vorheizfenster_keine_aktion(self):
        erg = self._rufe(stunde=20)
        assert erg.einschalten is None


class TestVerdrahtungInBewerteAlleRegeln:
    """Stellt sicher, dass pv_leistung/soc wirklich durchgereicht werden."""

    def _bewerte(self, feedin, soc):
        _gewinner, ergebnisse = bewerte_alle_regeln(
            config=WPSteuerungConfig(),
            temp_dict={"oben": 45.0, "mittig": 43.5, "unten": 41.0},
            pv_leistung=feedin,
            kompressor_ein=False,
            now=datetime(2026, 8, 26, 18, 30),
            forecast_wh_qm=SCHLECHT,
            forecast_today_wh_qm=SCHLECHT,
            soc=soc,
        )
        return ergebnisse

    @staticmethod
    def _forecast(ergebnisse):
        return next(e for e in ergebnisse if e.name == "Forecast")

    def test_soc_durchgereicht_batteriequelle_feuert(self):
        f = self._forecast(self._bewerte(feedin=-10.0, soc=95.0))
        assert f.einschalten is True

    def test_ohne_quelle_wartet_die_regel(self):
        f = self._forecast(self._bewerte(feedin=0.0, soc=80.0))
        assert f.einschalten is None
        assert "wartet" in f.grund
