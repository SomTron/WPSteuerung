# -*- coding: utf-8 -*-
"""Quellen-Gates fuer CalcStart + Abweichung (Deadline/Tiefenschutz-Muster).

Beobachtetes Problem: Beide Regeln waren quellenblind und heizten tagsueber
notfalls mit Netzstrom. Neu:

- CalcStart startet frueh nur mit PV/Batterie; OHNE Quelle wartet er bis zum
  ERRECHNETEN Spaetest-Start (Zielzeit - berechnete Heizzeit - Puffer) und
  rettet dann notfalls mit Netz die Zapf-Garantie.
- Abweichung wartet im Normalfall auf PV/Batterie; faellt der Fuehler unter
  (Soll - netz_notfall_offset_k), erlaubt der Tiefenschutz Netzstrom.
"""
import sys
from datetime import datetime
from types import SimpleNamespace


sys.stdout.reconfigure(encoding="utf-8")

from json_config import WPSteuerungConfig  # noqa: E402
from priority_control import (  # noqa: E402
    bewerte_alle_regeln,
    evaluate_abweichung,
    evaluate_calculated_start,
)


# ─────────────────────────── CalcStart ───────────────────────────

def _calc_cfg(**overrides):
    cfg = SimpleNamespace(
        aktiv=True,
        prioritaet=82,
        solltemperatur_c=44.0,
        target_uhr=17,
        heizrate_unten_c_h=3.0,
        heizrate_gesamt_c_h=2.0,
        tmax_c=48.0,
        pv_einspeisung_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzbezug_watt=-50.0,
        spaetstart_puffer_h=0.5,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _temps(unten=41.0, mitte=43.0):
    """unten 41 -> diff 3K / 3 K/h = 1.0h; mitte 43 -> diff 1K / 2 K/h."""
    return {"oben": 45.0, "mittig": mitte, "unten": unten}


class TestCalcStartQuellenGate:
    def test_fruehstart_erfordert_quelle(self):
        """Knapper effektiver Puffer heizt nur noch MIT PV/Batterie frueh."""
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 16, 0, feedin_watt=100.0, soc=10.0,
        )
        assert erg.einschalten is True
        assert "[PV" in erg.grund

    def test_ohne_quelle_wartet_trotz_knappem_effektivpuffer(self):
        """Bewoelkt (Faktor 0.5) liess die alte Logik sofort mit Netz starten -
        jetzt wird gewartet, solange der REAL errechnete Puffer reicht."""
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 15, 12, forecast_wh_qm=400.0,
            feedin_watt=0.0, soc=50.0,
        )  # buffer 0.8h, effektiv 0.4h (< 0.5 haette alt EIN gegeben)
        assert erg.einschalten is None
        assert "warte auf PV/Batterie" in erg.grund

    def test_mit_gleicher_lage_pv_startet_frueher(self):
        """Gegenstueck zum Wartefall: Mit Quelle feuert der Fruehstart."""
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 15, 12, forecast_wh_qm=400.0,
            feedin_watt=150.0, soc=None,
        )
        assert erg.einschalten is True

    def test_spaetest_start_wird_aus_heizzeit_berechnet(self):
        """Ohne Quelle erst am errechneten Spatest-Start (buffer <= Puffer)."""
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 16, 0, feedin_watt=0.0, soc=None,
        )  # brauche 1.0h bis 17:00, Restpuffer 0.0h <= 0.5h
        assert erg.einschalten is True
        assert "SPAETEST" in erg.grund
        assert "Zapf-Garantie" in erg.grund

    def test_vor_dem_spaetest_start_wird_gewartet(self):
        """Noch 1.0h Restpuffer bei 1.0h Bedarf + Sicherheitspuffer: warten."""
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 15, 0, feedin_watt=0.0, soc=None,
        )
        assert erg.einschalten is None

    def test_zu_spaet_notfall_bleibt(self):
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 16, 59, feedin_watt=0.0, soc=None,
        )
        assert erg.einschalten is True
        assert "ZU SPAET" in erg.grund

    def test_batterie_als_fruehquelle(self):
        erg = evaluate_calculated_start(
            _calc_cfg(), _temps(), 16, 0, feedin_watt=-20.0, soc=92.0,
        )
        assert erg.einschalten is True
        assert "[Batterie" in erg.grund

    def test_spaetstart_puffer_konfigurierbar(self):
        erg = evaluate_calculated_start(
            _calc_cfg(spaetstart_puffer_h=2.0), _temps(), 14, 0,
            feedin_watt=0.0, soc=None,
        )  # Restpuffer 2.0h <= 2.0h -> Spatest-Start greift frueher
        assert erg.einschalten is True
        assert "SPAETEST" in erg.grund


class TestCalcStartVerdrahtung:
    """bewerte_alle_regeln muss pv_leistung/soc an CalcStart durchreichen."""

    @staticmethod
    def _ergebnisse(feedin, soc):
        _gewinner, alle = bewerte_alle_regeln(
            config=WPSteuerungConfig(),
            temp_dict={"oben": 45.0, "mittig": 43.0, "unten": 41.0},
            pv_leistung=feedin,
            kompressor_ein=False,
            now=datetime(2026, 8, 26, 16, 0),
            forecast_today_wh_qm=None,
            soc=soc,
        )
        return [e for e in alle if e.name == "CalcStart"][0]

    def test_soc_durchgereicht_spatest_start(self):
        cs = self._ergebnisse(feedin=0.0, soc=None)
        assert cs.einschalten is True
        assert "SPAETEST" in cs.grund

    def test_feedin_durchgereicht_fruehstart_mit_quelle(self):
        cs = self._ergebnisse(feedin=100.0, soc=95.0)
        assert cs.einschalten is True
        assert "[PV 100W" in cs.grund


# ─────────────────────────── Abweichung ───────────────────────────

def _abw_cfg(**overrides):
    cfg = SimpleNamespace(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=42.0,
        quelle_warten=True,
        pv_einspeisung_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzbezug_watt=-50.0,
        netz_notfall_offset_k=8.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _abw_rufe(unten, feedin=0.0, soc=None, cfg=None, stunde=12):
    return evaluate_abweichung(
        cfg or _abw_cfg(),
        {"oben": 35.0, "mittig": 34.0, "unten": unten},
        kompressor_ein=False, now_hour=stunde,
        nachtsperre_start=19, nachtsperre_ende=8,
        feedin_watt=feedin, soc=soc,
    )


class TestAbweichungQuellenGate:
    def test_wartet_ohne_quelle(self):
        """Zu kalt fuer Soll (+7K), aber weder PV noch Batterie -> warten."""
        erg = _abw_rufe(33.0)
        assert erg.einschalten is None
        assert "wartet auf PV/Batterie" in erg.grund
        assert "Netz erst unter 32.0C" in erg.grund

    def test_tiefenschutz_erlaubt_netz(self):
        """Unter Soll-Offset (32C) darf auch Netzstrom heizen."""
        erg = _abw_rufe(31.5)
        assert erg.einschalten is True

    def test_tiefenschutz_grenze_inklusive(self):
        """Genau auf der Tiefenschutz-Grenze gilt: Netz erlaubt."""
        erg = _abw_rufe(32.0)
        assert erg.einschalten is True

    def test_pv_als_quelle(self):
        erg = _abw_rufe(33.0, feedin=100.0)
        assert erg.einschalten is True
        assert erg.grund.endswith("-> EIN")

    def test_batterie_als_quelle(self):
        erg = _abw_rufe(33.0, feedin=-20.0, soc=90.0)
        assert erg.einschalten is True

    def test_batterie_mit_netzkauf_zaehlt_nicht(self):
        erg = _abw_rufe(33.0, feedin=-300.0, soc=95.0)
        assert erg.einschalten is None
        assert "wartet" in erg.grund

    def test_legacy_quelle_warten_deaktiviert(self):
        erg = _abw_rufe(33.0, feedin=0.0, soc=None, cfg=_abw_cfg(quelle_warten=False))
        assert erg.einschalten is True

    def test_schichtung_hat_vorrang_vor_gate(self):
        """Oben noch warm -> Schichtung blockiert zuerst (kein Quellen-Thema)."""
        erg = evaluate_abweichung(
            _abw_cfg(),
            {"oben": 45.0, "mittig": 40.0, "unten": 33.0},
            kompressor_ein=False, now_hour=12,
            nachtsperre_start=19, nachtsperre_ende=8,
            feedin_watt=0.0, soc=None,
        )
        assert erg.einschalten is None
        assert "Schichtung" in erg.grund

    def test_default_felder_bei_alter_config(self):
        cfg = _abw_cfg()
        for feld in ("quelle_warten", "pv_einspeisung_min_watt", "soc_min_prozent",
                     "max_netzbezug_watt", "netz_notfall_offset_k"):
            delattr(cfg, feld)
        erg = _abw_rufe(33.0, cfg=cfg)
        assert erg.einschalten is None  # Defaults: warten

    def test_aus_zweig_bleift_quellenunabhaengig(self):
        """Ausschalten benoetigt keine Energie - Gate darf nicht eingreifen."""
        erg = _abw_rufe(39.8, feedin=0.0, soc=None)
        assert erg.einschalten is False
