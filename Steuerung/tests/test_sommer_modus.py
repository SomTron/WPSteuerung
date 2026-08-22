"""
Tests fuer die Sommer-Modus-Bewertung: logic_utils.evaluate_sommer_modus()

Getestete Semantik:
- Pro Kalendertag zaehlt maximal EINE gute Bewertung (Bugfix: frueher wurde pro
  Forecast-Update hochgezaehlt -> 'benoetigte_tage' war nach wenigen Stunden erreicht)
- Aktivierung erst nach N AUFENANDERFOLGENDEN guten Tagen
- Schlechte/unvollstaendige Prognose -> sofortiger Reset + Deaktivierung
- Luecken von mehr als einem Tag brechen die Serie
"""
import os
import sys
from datetime import date

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from logic_utils import (
    evaluate_sommer_modus,
    SOMMER_AKTIVIERT,
    SOMMER_DEAKTIVIERT_PROGNOSE,
    SOMMER_DEAKTIVIERT_DATEN,
    SOMMER_KEIN_EREIGNIS,
)

# Prognosen (Wh/qm), Schwelle im Test: 2000
GUT = (2500.0, 3000.0, 2800.0)            # alle drei Tage >= 2000
SCHLECHT_MORGEN = (2500.0, 500.0, 2800.0) # morgen zu wenig

D1 = date(2024, 6, 1)
D2 = date(2024, 6, 2)
D3 = date(2024, 6, 3)
D4 = date(2024, 6, 4)
D10 = date(2024, 6, 10)


def bewerte(prognose, heute, zaehler=0, aktiv=False, letzter_tag=None):
    """Kurzhelfer: ein evaluate_sommer_modus-Aufruf mit Test-Defaults."""
    return evaluate_sommer_modus(
        benoetigte_tage=3,
        mindest_prognose_wh=2000.0,
        rad_today=prognose[0],
        rad_tomorrow=prognose[1],
        rad_day2=prognose[2],
        heute=heute,
        aktueller_zaehler=zaehler,
        ist_aktiv=aktiv,
        letzter_bewertungstag=letzter_tag,
    )


class TestZaehlungProKalendertag:
    """Kern-Bugfix: Zaehlung erfolgt pro Kalendertag, nicht pro Forecast-Update."""

    def test_mehrere_updates_am_selben_tag_zaehlen_einmal(self):
        """FORECAST_UPDATE_INTERVAL_HOURS < 24h: 3 Updates am selben Tag = 1 guter Tag."""
        zaehler, aktiv, tag, ereignis = 0, False, None, SOMMER_KEIN_EREIGNIS
        for _ in range(3):  # z.B. 06:00, 12:00, 18:00
            zaehler, aktiv, tag, ereignis = bewerte(GUT, D1, zaehler, aktiv, tag)

        assert zaehler == 1, "Mehrfache Updates am selben Tag duerfen nur 1x zaehlen"
        assert aktiv is False
        assert tag == D1
        assert ereignis == SOMMER_KEIN_EREIGNIS

    def test_aktiver_tag_zaehlt_nicht_doppelt_nach_reset(self):
        """Gut -> schlecht -> gut am SELBEN Tag: der zweite gute Wert zaehlt nicht erneut."""
        z, a, t, _ = bewerte(GUT, D1)                    # +1
        z, a, t, _ = bewerte(SCHLECHT_MORGEN, D1, z, a, t)  # Reset auf 0
        z, a, t, _ = bewerte(GUT, D1, z, a, t)           # gleiches Datum: kein +1

        assert z == 0, "Nach Reset am selben Tag darf nicht erneut gezaehlt werden"
        assert a is False


class TestAktivierung:
    """Aktivierung erst nach N aufeinanderfolgenden guten Tagen."""

    def _serie_guter_tage(self, tage):
        zaehler, aktiv, tag, ereignis = 0, False, None, SOMMER_KEIN_EREIGNIS
        verlauf = []
        for d in tage:
            zaehler, aktiv, tag, ereignis = bewerte(GUT, d, zaehler, aktiv, tag)
            verlauf.append(ereignis)
        return zaehler, aktiv, tag, verlauf

    def test_zwei_tage_reichen_nicht(self):
        zaehler, aktiv, _, _ = self._serie_guter_tage([D1, D2])
        assert zaehler == 2
        assert aktiv is False

    def test_drei_tage_aktivieren(self):
        zaehler, aktiv, tag, verlauf = self._serie_guter_tage([D1, D2, D3])
        assert zaehler == 3
        assert aktiv is True
        assert tag == D3
        assert verlauf[-1] == SOMMER_AKTIVIERT, "AKTIVIERT-Event genau beim Schwellwert-Tag"

    def test_aktiviert_event_nur_einmal(self):
        """Bleibt die Prognose gut, gibt es kein wiederholtes AKTIVIERT-Event."""
        _, aktiv, tag, _ = self._serie_guter_tage([D1, D2, D3])
        _, _, _, ereignis = bewerte(GUT, D4, 3, aktiv, tag)
        assert ereignis == SOMMER_KEIN_EREIGNIS

    def test_bleibt_aktiv_solange_prognose_gut(self):
        zaehler, aktiv, tag, _ = self._serie_guter_tage([D1, D2, D3, D4])
        assert aktiv is True
        assert zaehler == 4


class TestDeaktivierung:
    """Konservatives Verhalten bei schlechter/unvollstaendiger Prognose."""

    def test_schlechte_prognose_resettet_und_deaktiviert(self):
        z, a, t, _ = self._bis_aktiv()
        z, a, t, e = bewerte(SCHLECHT_MORGEN, D4, z, a, t)

        assert z == 0
        assert a is False
        assert e == SOMMER_DEAKTIVIERT_PROGNOSE

    def test_unvollstaendige_daten_deaktivieren(self):
        z, a, t, _ = self._bis_aktiv()
        z, a, t, e = bewerte((None, None, None), D4, z, a, t)

        assert z == 0
        assert a is False
        assert e == SOMMER_DEAKTIVIERT_DATEN

    def test_teilweise_daten_gelten_als_unvollstaendig(self):
        z, a, t, _ = self._bis_aktiv()
        z, a, t, e = bewerte((2500.0, 3000.0, None), D4, z, a, t)

        assert a is False
        assert e == SOMMER_DEAKTIVIERT_DATEN

    def test_schlecht_ohne_vorgeschichte_kein_ereignis(self):
        """Kein Log-Spam: schlechte Prognose ohne Serie/aktiv Modus = kein Ereignis."""
        z, a, t, e = bewerte(SCHLECHT_MORGEN, D1)
        assert z == 0 and a is False
        assert e == SOMMER_KEIN_EREIGNIS

    def _bis_aktiv(self):
        """Hilfsfunktion: Serie bis zur Aktivierung (3 gute Tage)."""
        z, a, t, _ = bewerte(GUT, D1)
        z, a, t, _ = bewerte(GUT, D2, z, a, t)
        z, a, t, e = bewerte(GUT, D3, z, a, t)
        assert a is True
        return z, a, t, e


class TestSerienschutz:
    """Die Serie darf nicht ueber Luecken hinweg weitergefuehrt werden."""

    def test_luecke_von_zwei_tagen_bricht_serie(self):
        """Tag 1 gut, dann Ausfall, Tag 3 wieder gut: Zaehler darf nicht bei 2 stehen."""
        z, a, t, _ = bewerte(GUT, D1)                     # +1
        z, a, t, e = bewerte(GUT, D3, z, a, t)            # D3 - D1 = 2 Tage Luecke

        assert z == 1, "Luecke muss die Serie zuruecksetzen, bevor neu gezaehlt wird"
        assert a is False
        assert e == SOMMER_KEIN_EREIGNIS

    def test_aufeinanderfolgende_tage_ohne_luecke(self):
        """Normalfall: direkt aufeinanderfolgende Tage zaehlen weiter."""
        z, a, t, _ = bewerte(GUT, D1)
        z, a, t, _ = bewerte(GUT, D2, z, a, t)
        assert z == 2

    def test_frischer_start_zaehlt_ersten_guten_tag(self):
        """Frischer Start (keine Historie): erster guter Tag -> Zaehler = 1."""
        z, a, t, e = bewerte(GUT, D1)
        assert z == 1
        assert a is False
        assert t == D1
        assert e == SOMMER_KEIN_EREIGNIS
