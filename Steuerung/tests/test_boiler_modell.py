# -*- coding: utf-8 -*-
"""Tests fuer das Boiler-Fuellstandsmodell (Punkt A)."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from boiler_modell import schaetze_warmwasser  # noqa: E402


class TestSchaetzeWarmwasser:
    def test_alles_kalt_liefert_null(self):
        liter, anteil = schaetze_warmwasser(
            {"unten": 15.0, "mittig": 18.0, "oben": 22.0},
            volumen_l=150.0, nutztemp_c=40.0,
        )
        assert liter == 0.0
        assert anteil == 0.0

    def test_alles_warm_liefert_volles_volumen(self):
        liter, anteil = schaetze_warmwasser(
            {"unten": 45.0, "mittig": 48.0, "oben": 50.0},
            volumen_l=150.0, nutztemp_c=40.0,
        )
        assert liter == 150.0
        assert anteil == 100.0

    def test_geschichteter_speicher_teilvoll(self):
        # unten kalt, oben warm -> etwa die halbe Menge
        liter, anteil = schaetze_warmwasser(
            {"unten": 20.0, "mittig": 30.0, "oben": 45.0},
            volumen_l=100.0, nutztemp_c=40.0,
        )
        assert 10.0 <= liter <= 60.0   # nur der obere Teil ist warm
        assert anteil == liter  # konsistent (bei Volumen 100)

    def test_nur_ein_fuehler(self):
        # Nur unten lesbar und kalt -> nichts warm
        liter, _ = schaetze_warmwasser({"unten": 25.0}, volumen_l=80.0)
        assert liter == 0.0
        # Nur oben lesbar und warm -> alles warm
        liter, _ = schaetze_warmwasser({"oben": 45.0}, volumen_l=80.0, nutztemp_c=40.0)
        assert liter == 80.0

    def test_alle_fuehler_none_liefert_null(self):
        liter, anteil = schaetze_warmwasser(
            {"unten": None, "mittig": None, "oben": None}, volumen_l=150.0
        )
        assert liter == 0.0 and anteil == 0.0

    def test_leeres_dict_liefert_null(self):
        liter, anteil = schaetze_warmwasser({}, volumen_l=150.0)
        assert liter == 0.0 and anteil == 0.0

    def test_monotonie_mehr_oben_heiss_mehr_volumen(self):
        liter1, _ = schaetze_warmwasser(
            {"unten": 20.0, "mittig": 30.0, "oben": 42.0},
            volumen_l=120.0, nutztemp_c=40.0)
        liter2, _ = schaetze_warmwasser(
            {"unten": 35.0, "mittig": 42.0, "oben": 46.0},
            volumen_l=120.0, nutztemp_c=40.0)
        assert liter2 > liter1
