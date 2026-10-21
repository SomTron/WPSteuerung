"""Regressionstests: Surplus-Profil und Tagesbilanz (21.10.2026).

Befunde, die diese Tests absichern:

1. `surplus_by_hour` benutzte als Key NUR die Stunde ("8"). Beim
   Tageswechsel wurden die Buckets nicht geleert - der Wert war ein
   EWMA aus mehreren Tagen (gemessen: Stunde 8 nach zwei Tagen
   545,7 W statt 900 W). Die WebApp zeigte das als "gelerntes
   Ueberschuss-Profil" an, um Stunden verschoben.

2. Die Tagesbilanz berechnete den Hausverbrauch als
   "Erzeugung - Einspeisung". Das erfasst nur den Sonnentag; die
   naechtlichen ~4400 Wh fehlten komplett (gemessen: 5200 statt
   9600 Wh/Tag). Richtig ist die Wechselrichterbilanz
   `acpower - feedin` (energy_source.hausverbrauch_watt).

3. Der Hausverbrauch haengt nicht nur vom Wochentag ab, sondern auch
   von der Jahreszeit. Ohne Saisontrennung vermischte der Median
   Sommer und Winter zu einem unbrauchbaren Zwischenwert.
"""

from datetime import datetime, timedelta

import pytest

from learning_engine import LearningEngine


def _schritt(engine, now, compressor_is_on=False, feedin=0.0,
             hausverbrauch=None):
    engine.update(
        now=now,
        temp_dict={"unten": 40.0},
        compressor_is_on=compressor_is_on,
        feedin_watt=feedin,
        pv_acpower_watt=0.0,
        pv_erzeugung_watt=0.0,
        soc=50.0,
        forecast_today_wh_qm=2500.0,
        forecast_hourly_wh=None,
        pv_array_size_qm=10.0,
        hausverbrauch_watt=hausverbrauch,
    )


class TestSurplusProfilTagesTrennung:
    """Der Key muss den Tag enthalten, sonst vermischen sich die Tage."""

    def test_profil_nach_zwei_tagen_ist_der_tageswert(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        # Vier Stunden, damit das Profil als Ganzes freigegeben wird
        stunden = {6: 100.0, 8: 900.0, 12: 1400.0, 17: 800.0}
        tag1 = datetime(2026, 10, 5, 0, 0)
        for h, w in stunden.items():
            for k in range(6):
                _schritt(eng, tag1 + timedelta(hours=h, minutes=10 * k),
                         feedin=w)
        profil = eng.get_surplus_profile()
        assert profil is not None
        assert profil[8] == pytest.approx(900.0, abs=1.0)

        # Zweiter Tag mit 0 W in denselben Stunden
        tag2 = datetime(2026, 10, 6, 0, 0)
        for h in stunden:
            for k in range(6):
                _schritt(eng, tag2 + timedelta(hours=h, minutes=10 * k),
                         feedin=0.0)
        profil = eng.get_surplus_profile()
        assert profil is not None
        # Der Wert darf NICHT der Mischwert aus beiden Tagen sein
        # (vor dem Fix: 545,7 W statt 900 W)
        # Vor dem Fix war das ein EWMA-Mischwert aus beiden Tagen
        # (gemessen: 545,7 W) - der neuere Tag wog staerker, und der
        # Wert war damit kein Tageswert mehr. Jetzt ist es der
        # gleichgewichtete Mittelwert der vollstaendigen Tage:
        # (900 + 0) / 2 = 450 W.
        assert profil[8] == pytest.approx(450.0, abs=1.0), (
            f"Stunde 8: {profil[8]} statt 450 - die Tageswerte werden "
            f"falsch gewichtet")

    def test_profil_ist_je_stunde_korrekt(self, tmp_path):
        """Jede Stunde muss ihren eigenen Wert liefern (kein Versatz)."""
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        werte = {6: 100, 7: 400, 8: 900, 9: 1200, 10: 1500, 11: 1600,
                 12: 1400, 13: 1200, 14: 1500, 15: 1700, 16: 1300,
                 17: 800, 18: 200}
        for d in range(3):
            tag = datetime(2026, 10, 5, 0, 0) + timedelta(days=d)
            for h in range(24):
                for k in range(6):
                    _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                             feedin=float(werte.get(h, 0)))
        profil = eng.get_surplus_profile()
        assert profil is not None
        for h, wert in werte.items():
            assert profil[h] == pytest.approx(wert, abs=1.0), (
                f"Stunde {h}: {profil[h]} statt {wert}")


class TestTagesbilanzEchterHausverbrauch:
    """Die Bilanz muss den echten Hausverbrauch messen, nicht PV-Export."""

    def test_nachtverbrauch_ist_enthalten(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        tag = datetime(2026, 10, 19, 0, 0)
        for h in range(24):
            for k in range(6):
                _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                         hausverbrauch=400.0)
        # Folgetag triggert die Bilanzierung des Vortags
        _schritt(eng, datetime(2026, 10, 20, 8, 0), hausverbrauch=400.0)
        assert eng.data.tages_haushalt, "Keine Tagesbilanz erfasst"
        eintrag = eng.data.tages_haushalt[0]
        # 400 W ueber 24 h = 9600 Wh. Vor dem Fix waren es 5200 Wh
        # (nur der Sonnentag).
        assert eintrag["verbrauch_wh"] == pytest.approx(9600.0, rel=0.05)

    def test_bilanz_ohne_hausverbrauch_wird_verworfen(self, tmp_path):
        """Lieber keine Zahl als eine falsche."""
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        tag = datetime(2026, 10, 19, 0, 0)
        for h in range(24):
            for k in range(6):
                _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                         feedin=500.0)
        _schritt(eng, datetime(2026, 10, 20, 8, 0))
        assert not eng.data.tages_haushalt


class TestSaisontrennung:
    """Wochentag UND Jahreszeit muessen getrennt ausgewertet werden."""

    def test_saison_wird_erkannt(self):
        assert LearningEngine._saison_von_datum("2026-01-15") == "winter"
        assert LearningEngine._saison_von_datum("2026-04-01") == "fruehling"
        assert LearningEngine._saison_von_datum("2026-07-15") == "sommer"
        assert LearningEngine._saison_von_datum("2026-10-21") == "herbst"

    def test_sommer_und_winter_werden_getrennt(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        for start, wert in ((datetime(2026, 7, 6, 0, 0), 400.0),
                            (datetime(2026, 1, 5, 0, 0), 1500.0)):
            for d in range(14):
                tag = start + timedelta(days=d)
                for h in range(24):
                    for k in range(6):
                        _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                                 hausverbrauch=wert)
        profil = eng.get_haushaltsprofil_je_tag()
        # Es MUSS saisonbezogene Schluessel geben
        saison_werte = {k: v for k, v in profil.items() if "-" in k}
        assert saison_werte, "Keine saisonbezogenen Werte im Profil"
        # Sommer- und Winterwerte duerfen nicht vermischt sein:
        # der Winterwert muss groesser sein als der Sommerwert.
        sommer = [v for k, v in saison_werte.items() if k.endswith("-sommer")]
        winter = [v for k, v in saison_werte.items() if k.endswith("-winter")]
        assert sommer and winter
        assert min(winter) > max(sommer), (
            f"Winter nicht groesser als Sommer: {saison_werte}")

    def test_zugriffsfunktion_nutzt_die_saison(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        for start, wert in ((datetime(2026, 7, 6, 0, 0), 400.0),
                            (datetime(2026, 1, 5, 0, 0), 1500.0)):
            for d in range(14):
                tag = start + timedelta(days=d)
                for h in range(24):
                    for k in range(6):
                        _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                                 hausverbrauch=wert)
        sommer = eng.get_haushaltsverbrauch_fuer_wochentag(0, "2026-07-13")
        winter = eng.get_haushaltsverbrauch_fuer_wochentag(0, "2026-01-12")
        assert sommer is not None and winter is not None
        assert winter > sommer * 1.5, (
            f"Winter {winter} sollte deutlich ueber Sommer {sommer} liegen")


class TestAltDatenVerwerfung:
    """Alte Eintraege (fehlerhafte Formel) duerfen nicht verwendet werden."""

    def test_alte_eintraege_ohne_saison_werden_verworfen(self, tmp_path):
        pfad = tmp_path / "lern.json"
        pfad.write_text(
            '{"tages_haushalt": ['
            '{"ts": "2026-10-05", "verbrauch_wh": 5200.0},'
            '{"ts": "2026-10-12", "verbrauch_wh": 5200.0}'
            ']}', encoding="utf-8")
        eng = LearningEngine(data_path=str(pfad))
        assert eng.data.tages_haushalt == [], (
            "Alte Eintraege ohne 'saison' muessen verworfen werden")
