# -*- coding: utf-8 -*-
"""Tests: Abweichungs-PV-Warten (Empfehlung 3.3).

Bei sehr guter Tagesprognose soll die Abweichungs-Regel morgens NICHT mit
Netzstrom heizen, sondern auf die erwartete PV-Sonne warten. Schuetzt vor
den gemessenen "ZU FRUEH"-Events (Netz-Heizen, kurz darauf PV-Ueberschuss).
"""
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from priority_control import evaluate_abweichung  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402


def _abw_r(unten, feedin=0.0, soc=None, stunde=8, forecast=None,
           fc_ratio=1.0, oben=30.0, config=None):
    """Ruft die Abweichungs-Regel mit den neuen PV-Warten-Parametern auf."""
    cfg = (config or WPSteuerungConfig()).abweichung
    return evaluate_abweichung(
        cfg,
        {"oben": oben, "mittig": 40.0, "unten": unten},
        False, stunde, 19, 5,
        feedin_watt=feedin, soc=soc,
        forecast_today_wh_qm=forecast, fc_ratio=fc_ratio,
    )


# Kurz-Alias fuer die Tests
_abw = _abw_r


class TestPvWarten:
    def test_gute_prognose_morgens_wartet_statt_netz(self):
        """08.09-Fall: unten 24.9, Prognose 5.34 kWh/m2 -> warten, kein Netz."""
        erg = _abw(oben=30.0, unten=24.9, feedin=0.0, stunde=8, forecast=5340.0)
        assert erg.einschalten is None
        assert "gute PV-Prognose" in erg.grund
        assert "warte auf PV" in erg.grund

    def test_regression_ohne_prognose_heizt_normal_vom_netz(self):
        """Ohne Forecast-Daten bleibt das bisherige Tiefenschutz-Verhalten."""
        erg = _abw(oben=30.0, unten=24.9, feedin=0.0, stunde=8, forecast=None)
        assert erg.einschalten is True  # unter Soll-8K (Tiefenschutz)

    def test_schlechte_prognose_heizt_normal(self):
        """10.09-Fall (0.59 kWh/m2): keine PV erwartet -> gewohntes Netz-Heizen."""
        erg = _abw(oben=30.0, unten=30.0, feedin=0.0, stunde=8, forecast=590.0)
        assert erg.einschalten is True

    def test_kwh_eingabe_wird_normalisiert(self):
        """state.solar.forecast_today = kWh/m2 (5.34) -> Wh/m2 (5340) -> warten."""
        erg = _abw(oben=30.0, unten=25.0, feedin=0.0, stunde=8, forecast=5.34)
        assert erg.einschalten is None
        assert "gute PV-Prognose" in erg.grund

    def test_kalibrierte_prognose_unter_schwelle_wartet_nicht(self):
        """fc_ratio senkt die effektive Prognose unter die Schwelle."""
        erg = _abw(oben=30.0, unten=30.0, feedin=0.0, stunde=8,
                   forecast=5340.0, fc_ratio=0.2)  # 1068 < 2500
        assert erg.einschalten is True

    def test_echt_kalt_unten_heizt_sofort(self):
        """unten < pv_warten_unten_min_c (20) -> sofort heizen (Notfall)."""
        erg = _abw(oben=30.0, unten=18.0, feedin=0.0, stunde=8, forecast=5340.0)
        assert erg.einschalten is True

    def test_nach_warteranster_nachmittags_wieder_normal(self):
        """Ab pv_warten_bis_uhr (12) spielt die Regel wieder normal."""
        erg = _abw(oben=30.0, unten=25.0, feedin=0.0, stunde=14, forecast=5340.0)
        assert erg.einschalten is True

    def test_pv_aktiv_beim_warten_heizt_normal(self):
        """Ist die PV schon aktiv, wird normal entschieden (kein ueberflue)."""
        erg = _abw(oben=30.0, unten=25.0, feedin=600.0, soc=95.0,
                   stunde=8, forecast=5340.0)
        assert erg.einschalten is True

    def test_deaktivierbar_ueber_config(self):
        cfg = WPSteuerungConfig()
        cfg.abweichung.pv_warten_aktiv = False
        erg = _abw(oben=30.0, unten=30.0, feedin=0.0, stunde=8,
                   forecast=5340.0, config=cfg)
        assert erg.einschalten is True