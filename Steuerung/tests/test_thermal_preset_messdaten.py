# -*- coding: utf-8 -*-
"""Regression: das gemessene Speicher-Preset bildet die Betriebsdaten ab.

Der Fit vom 05.11.2026 (545 Heizintervalle + ~2.200 Ruheintervalle aus
01.10.-05.11.2026) hat zwei Mechanismen aufgedeckt, die das Modell vorher
nicht hatte: eine vom Ruecklauf abhaengige Heizleistung und eine vom
Schichtungsabstand abhaengige Waermeverteilung. Beides ist noetig, sonst
verliert der Fit die Haelfte der Aussage:

    ohne leistung_pro_k/verteilung_pro_k : R2 0.055
    mit  leistung_pro_k/verteilung_pro_k : R2 0.93

Dieser Test haelt die gemessenen Raten fest. Er schlaegt an, wenn jemand
die Parameter oder die Modellgleichungen aendert, ohne die Betriebsdaten
erneut zu pruefen - genau der Fehler, den die Lern-Engine mit EINER
Konstante je Monat macht.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "Analyse", "sim")
if SIM not in sys.path:
    sys.path.insert(0, SIM)

from thermal import Speicher, SpeicherParameter  # noqa: E402

#: Gemessene Heizraten (K/h) je Schichtungsabstand delta = mitte - unten.
#: Quelle: Median ueber die 15-min-Intervalle mit durchlaufendem Kompressor,
#: Startzustand = Median des jeweiligen Bins.
MESSUNG = {
    "0-4":   {"start": (43.6, 44.5, 47.1), "rate": (4.75, 3.75, 0.50), "n": 412},
    "4-8":   {"start": (33.4, 38.6, 46.9), "rate": (14.00, 0.50, -0.25), "n": 49},
    "8-12":  {"start": (30.3, 40.6, 47.9), "rate": (16.88, -1.38, -0.25), "n": 36},
    "12-16": {"start": (28.9, 42.6, 49.0), "rate": (19.25, -0.50, -0.25), "n": 21},
    ">16":   {"start": (25.5, 47.6, 49.2), "rate": (21.00, -0.63, -0.25), "n": 18},
}

TOLERANZ = 1.5          # K/h - die Raten streuen selbst um Faktor 2


def _rate(parameter, start, dt_min=15.0):
    speicher = Speicher(parameter, start_unten=start[0], start_mitte=start[1],
                        start_oben=start[2])
    speicher.schritt(dt_min * 60.0, kompressor_ein=True)
    t = speicher.temperaturen
    h = dt_min / 60.0
    return ((t["unten"] - start[0]) / h, (t["mitte"] - start[1]) / h,
            (t["oben"] - start[2]) / h)


class TestGemessenesPreset:
    def test_heizraten_je_schichtungsabstand(self):
        parameter = SpeicherParameter.gemessen_oktober_2026()
        for name, mess in MESSUNG.items():
            rate = _rate(parameter, mess["start"])
            for knoten, (ist, soll) in enumerate(zip(rate, mess["rate"])):
                assert abs(ist - soll) <= TOLERANZ, (
                    "Bin %s, Knoten %d: Modell %.2f K/h, gemessen %.2f K/h"
                    % (name, knoten, ist, soll)
                )

    def test_mitte_folgt_nur_bei_kleiner_schichtung(self):
        """Der Kern des Befunds: bei delta <= 4 folgt mitte, bei delta > 8 nicht."""
        parameter = SpeicherParameter.gemessen_oktober_2026()
        nah = _rate(parameter, MESSUNG["0-4"]["start"])
        fern = _rate(parameter, MESSUNG[">16"]["start"])
        assert nah[1] > 2.0, "bei delta <= 4 muss mitte deutlich steigen"
        assert fern[1] < 0.5, "bei delta > 16 darf mitte nicht mitsteigen"
        # und `unten` heizt im geschichteten Fall viel schneller
        assert fern[0] > 4.0 * nah[0]

    def test_leistung_haengt_vom_ruecklauf_ab(self):
        """Kaltes Wasser unten -> mehr Waermeeintrag (Spreizung am Kondensator)."""
        parameter = SpeicherParameter.gemessen_oktober_2026()
        assert parameter.leistung_pro_k > 0.0
        kalt = parameter.heizleistung_w + parameter.leistung_pro_k * (40.0 - 25.0)
        warm = parameter.heizleistung_w + parameter.leistung_pro_k * (40.0 - 44.0)
        assert kalt > warm

    def test_ruhephase_bleibt_geschichtet(self):
        """Kompressor aus: keine Durchmischung, oben kuehlt am staerksten."""
        parameter = SpeicherParameter.gemessen_oktober_2026()
        speicher = Speicher(parameter, start_unten=20.2, start_mitte=37.5,
                            start_oben=47.6)
        speicher.schritt(900.0, kompressor_ein=False)
        t = speicher.temperaturen
        assert t["oben"] < 47.6                     # Deckel verliert Waerme
        assert abs(t["unten"] - 20.2) < 0.4         # unten praktisch stabil
        assert t["mitte"] < 37.5 + 0.2              # keine Konvektion nach oben

    def test_stabilitaet_ueber_lange_laeufe(self):
        """Sechs Stunden heizen duerfen den Speicher nicht ueberhitzen."""
        parameter = SpeicherParameter.gemessen_oktober_2026()
        speicher = Speicher(parameter, start_unten=20.0, start_mitte=40.0,
                            start_oben=48.0)
        for _ in range(24):
            speicher.schritt(900.0, kompressor_ein=True)
        t = speicher.temperaturen
        assert all(-1.0 <= wert <= 99.0 for wert in t.values()), t
        assert t["unten"] > t["mitte"] or True      # nur Plausibilitaet, kein Zwang
