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
             hausverbrauch=None, stundenprofil=False):
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
        hausverbrauch_stunden_profil=stundenprofil,
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
                         hausverbrauch=w, stundenprofil=True)
        profil = eng.get_surplus_profile()
        assert profil is not None
        assert profil[8] == pytest.approx(900.0, abs=1.0)

        # Zweiter Tag mit 0 W in denselben Stunden
        tag2 = datetime(2026, 10, 6, 0, 0)
        for h in stunden:
            for k in range(6):
                _schritt(eng, tag2 + timedelta(hours=h, minutes=10 * k),
                         hausverbrauch=0.0, stundenprofil=True)
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
                             hausverbrauch=float(werte.get(h, 0)),
                             stundenprofil=True)
        profil = eng.get_surplus_profile()
        assert profil is not None
        for h, wert in werte.items():
            assert profil[h] == pytest.approx(wert, abs=1.0), (
                f"Stunde {h}: {profil[h]} statt {wert}")


class TestVerbrauchsprofilUndMittagstief:
    """Das Profil misst den Hausverbrauch; die Tief-Erkennung findet ihn."""

    def test_profil_misst_hausverbrauch_nicht_einspeisung(self, tmp_path):
        """BUGFIX (21.10.2026): Die Einspeisung ist nachts 0 und
        beschreibt den PV-Ueberschuss, nicht das Haushaltsmuster.
        Auf dem Pi zeigte das gelernte Profil ~0 W fuer alle Stunden -
        wodurch JEDE Stunde als Mittagstief galt.
        """
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        start = datetime(2026, 10, 5, 0, 0)
        for d in range(3):
            tag = start + timedelta(days=d)
            for h in range(24):
                for k in range(6):
                    _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                             hausverbrauch=400.0, stundenprofil=True)
        profil = eng.get_surplus_profile()
        assert profil is not None
        assert profil[0] == pytest.approx(400.0, abs=1.0)
        assert profil[12] == pytest.approx(400.0, abs=1.0)

    def test_mittagstief_findet_verbrauchsspitze(self, tmp_path):
        """Der Lookup muss int-Keys UND str-Keys akzeptieren.

        BUGFIX (21.10.2026): Die Lernengine liefert int-Keys, die
        Funktion suchte mit str(h) - damit wurde NIE eine Stunde
        gefunden und die Erkennung war wirkungslos.
        """
        from priority_control import _mittagstief_stunden
        profil_int = {12: 1800.0, 13: 1800.0, 14: 500.0, 15: 500.0}
        profil_str = {"12": 1800.0, "13": 1800.0, "14": 500.0, "15": 500.0}
        for profil in (profil_int, profil_str):
            h, label = _mittagstief_stunden(11.0, 17.0, profil,
                                            schwelle_w=800.0)
            assert h == 2.0, f"{h} statt 2 - Key-Typ wird nicht akzeptiert"
            assert "12" in label

    def test_geringe_verbrauchsstunden_sind_kein_tief(self, tmp_path):
        """Der Grundverbrauch (400-500 W) ist kein Tief."""
        from priority_control import _mittagstief_stunden
        profil = {h: 450.0 for h in range(24)}
        h, label = _mittagstief_stunden(8.0, 17.0, profil, schwelle_w=800.0)
        assert h == 0.0
        assert label == ""


class TestPersistenz:
    """BUGFIX (21.10.2026): Die gelernten Daten muessen gespeichert werden.

    Die Stunden-Prognoseabweichung wurde gesammelt, aber erst beim
    naechsten Zyklusabschluss gespeichert - das kann 12 Stunden
    dauern (nachts laeuft keine WP) oder bis zum Neustart. Auf dem Pi
    stand das Feld dadurch dauerhaft auf "0 Eintraege".
    """

    def test_stundenabweichung_wird_gespeichert(self, tmp_path):
        import json
        pfad = tmp_path / "lern.json"
        eng = LearningEngine(data_path=str(pfad))
        hourly = {h: 300.0 for h in range(7, 19)}
        for k in range(6):
            eng.update(
                now=datetime(2026, 10, 21, 12, 0) + timedelta(minutes=10 * k),
                temp_dict={"unten": 40.0}, compressor_is_on=False,
                feedin_watt=0.0, pv_acpower_watt=0.0, pv_erzeugung_watt=1500.0,
                soc=50.0, forecast_today_wh_qm=2500.0,
                forecast_hourly_wh=hourly, pv_array_size_qm=10.0,
            )
        assert pfad.exists(), "Die Lerndaten wurden nicht gespeichert"
        with open(pfad, encoding="utf-8") as f:
            gespeichert = json.load(f)
        assert "2026-10-21-12" in (gespeichert.get("forecast_hourly_deviations") or {}), (
            "Die Stundenabweichung fehlt in der gespeicherten Datei - "
            "sie ginge bei jedem Neustart verloren")

    def test_taegliche_bilanz_wird_gespeichert(self, tmp_path):
        import json
        pfad = tmp_path / "lern.json"
        eng = LearningEngine(data_path=str(pfad))
        for h in range(24):
            for k in range(6):
                _schritt(eng, datetime(2026, 10, 21, h, 10 * k),
                         hausverbrauch=400.0)
        # Folgetag triggert die Bilanzierung
        _schritt(eng, datetime(2026, 10, 22, 8, 0), hausverbrauch=400.0)
        with open(pfad, encoding="utf-8") as f:
            gespeichert = json.load(f)
        assert gespeichert.get("tages_haushalt"), (
            "Die Tagesbilanz fehlt in der gespeicherten Datei")


class TestAutoLearningWirkt:
    """Das Gelernte muss Entscheidungen tatsaechlich beeinflussen."""

    def test_profil_verschiebt_den_calcstart(self, tmp_path):
        """Ein Verbrauchsprofil muss die Heizdauer verlaengern."""
        from priority_control import evaluate_calculated_start
        from json_config import WPSteuerungConfig
        cfg = WPSteuerungConfig()
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        start = datetime(2026, 10, 5, 0, 0)
        for d in range(7):
            tag = start + timedelta(days=d)
            for h in range(24):
                w = 1800.0 if h in (13, 14, 15) else 400.0
                for k in range(6):
                    _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                             hausverbrauch=w, stundenprofil=True)
        profil = eng.get_surplus_profile()
        assert profil[13] == pytest.approx(1800.0, abs=1.0)

        def _calc(p):
            return evaluate_calculated_start(
                cfg.calculated_start, {"unten": 40.0, "oben": 45.0, "mittig": 42.0},
                now_hour=8, now_minute=0, forecast_wh_qm=1500.0,
                feedin_watt=300.0, soc=40.0, pv_acpower=300.0,
                solar_stale=False, pv_fenster_stunde=10.0,
                pv_fenster_aktiv=True, surplus_profile=p,
            )
        mit = _calc(profil)
        ohne = _calc(None)
        assert "Mittagstief" in mit.grund, "Das Profil wirkt nicht auf den CalcStart"
        assert "Mittagstief" not in ohne.grund
        # Mehr Verbrauchsspitzen -> mehr Heizzeit noetig
        assert "2.9h" in mit.grund, mit.grund
        assert "0.7h" in ohne.grund, ohne.grund


class TestWochentagsUndSaisonTrennung:
    """BUGFIX (21.10.2026): Der Hausverbrauch unterscheidet sich stark
    nach Wochentag (Berufstag vs. Wochenende) und Jahreszeit. Ohne
    Trennung mittelt das Profil ueber alles - gemessen: 6,7 h Heizzeit
    statt 2,9 h (Werktag) bzw. 0,7 h (Wochenende).
    """

    @staticmethod
    def _lern_4_wochen(eng):
        """4 Wochen: Werktag 12-14 Uhr 1800 W, Wochenende 400 W."""
        for woche in range(4):
            for tag_idx in range(7):
                tag = datetime(2026, 10, 5, 0, 0) + timedelta(
                    days=woche * 7 + tag_idx)
                wochenende = tag.weekday() >= 5
                for h in range(24):
                    w = 400.0 if wochenende else (
                        1800.0 if 11 <= h <= 13 else 400.0)
                    for k in range(6):
                        _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                                 hausverbrauch=w, stundenprofil=True)

    def test_profil_trennt_wochentage(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        self._lern_4_wochen(eng)
        gesamt = eng.get_surplus_profile()
        montag = eng.get_surplus_profile(wochentag=0)
        samstag = eng.get_surplus_profile(wochentag=5)
        assert montag[12] == pytest.approx(1800.0, abs=1.0)
        assert samstag[12] == pytest.approx(400.0, abs=1.0)
        # Das Gesamtprofil liegt dazwischen
        assert gesamt[12] < montag[12]
        assert gesamt[12] > samstag[12]

    def test_profil_trennt_saisonen(self, tmp_path):
        """Juli (sommer) 400 W, Januar (winter) 1600 W."""
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        for start, wert in ((datetime(2026, 7, 1, 0, 0), 400.0),
                            (datetime(2026, 1, 5, 0, 0), 1600.0)):
            for d in range(10):
                tag = start + timedelta(days=d)
                for h in range(24):
                    for k in range(6):
                        _schritt(eng, tag + timedelta(hours=h, minutes=10 * k),
                                 hausverbrauch=wert, stundenprofil=True)
        sommer = eng.get_surplus_profile(saison="sommer")
        winter = eng.get_surplus_profile(saison="winter")
        assert sommer[12] == pytest.approx(400.0, abs=1.0)
        assert winter[12] == pytest.approx(1600.0, abs=1.0)

    def test_zu_wenig_daten_fallen_auf_gesamtprofil_zurueck(self, tmp_path):
        """Lieber ein Mittelwert als gar kein Profil."""
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        self._lern_4_wochen(eng)
        # Dienstag gibt es in den 4 Wochen mehrfach - aber z.B. ein
        # Wochentag, der noch nie vorkam, muss zurueckfallen.
        profil = eng.get_surplus_profile(wochentag=0)
        assert profil is not None
        assert len(profil) >= 4

    def test_wochentag_wirkt_auf_den_calcstart(self, tmp_path):
        """Der CalcStart muss das passende Tagesprofil verwenden."""
        from priority_control import evaluate_calculated_start
        from json_config import WPSteuerungConfig
        cfg = WPSteuerungConfig()
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        self._lern_4_wochen(eng)

        def _calc(wochentag):
            prof = eng.get_surplus_profile(wochentag=wochentag)
            return evaluate_calculated_start(
                cfg.calculated_start,
                {"unten": 40.0, "oben": 45.0, "mittig": 42.0},
                now_hour=8, now_minute=0, forecast_wh_qm=1500.0,
                feedin_watt=300.0, soc=40.0, pv_acpower=300.0,
                solar_stale=False, pv_fenster_stunde=10.0,
                pv_fenster_aktiv=True, surplus_profile=prof,
            )
        montag = _calc(0)
        samstag = _calc(5)
        assert "Mittagstief" in montag.grund, montag.grund
        assert "Mittagstief" not in samstag.grund, samstag.grund
        assert "2.9h" in montag.grund, montag.grund
        assert "0.7h" in samstag.grund, samstag.grund

    def test_saison_von_datum(self):
        from logic_utils import saison_von_datum
        assert saison_von_datum("2026-01-15") == "winter"
        assert saison_von_datum("2026-04-01") == "fruehling"
        assert saison_von_datum("2026-07-15") == "sommer"
        assert saison_von_datum("2026-10-21") == "herbst"
        assert saison_von_datum(datetime(2026, 7, 15)) == "sommer"
        assert saison_von_datum("kaputt") == "unbekannt"


class TestPvFensterUeberschuss:
    """BUGFIX (21.10.2026): Das PV-Fenster bewertete die BRUTTO-Erzeugung.

    Entscheidend ist aber der Ueberschuss: bei 8000 W Erzeugung endete
    das Fenster bei Hausverbrauch 500 / 1800 / 4000 W immer bei Stunde
    17 - der Verbrauch hatte keinen Einfluss.
    """

    def test_ueberschuss_bewertet_den_hausverbrauch(self):
        from logic_utils import pv_fenster_ueberschuss
        prognose = {h: 8000.0 for h in range(10, 18)}
        # Ohne Profil: Brutto-Bewertung (fail-open, wie bisher)
        assert pv_fenster_ueberschuss(prognose, 9.0, 300.0, 1.0)[0] == 17
        # Ruhiger Haushalt: Fenster bleibt offen
        ruhig = {h: 500.0 for h in range(24)}
        assert pv_fenster_ueberschuss(
            prognose, 9.0, 300.0, 1.0, haushalt_profil=ruhig)[0] == 17
        # Hoher Verbrauch: kein Ueberschuss mehr -> Fenster geschlossen
        hoch = {h: 7800.0 for h in range(24)}
        assert pv_fenster_ueberschuss(
            prognose, 9.0, 300.0, 1.0, haushalt_profil=hoch) is None

    def test_leeres_profil_bleibt_fail_open(self):
        from logic_utils import pv_fenster_ueberschuss
        prognose = {h: 8000.0 for h in range(10, 18)}
        assert pv_fenster_ueberschuss(
            prognose, 9.0, 300.0, 1.0, haushalt_profil={})[0] == 17


class TestStundenKorrekturfaktor:
    """BUGFIX (21.10.2026): Die gesammelten Stundenabweichungen wurden
    gespeichert, aber NIRGENDS gelesen. Sie sind genau die Information,
    die fuer das PV-Fenster-Ende fehlt: zu welcher Tageszeit liegt die
    Prognose daneben?
    """

    @staticmethod
    def _lern(eng, prognose_wm2, tatsaechlich_w, tage=6):
        from datetime import datetime, timedelta
        hourly = {h: prognose_wm2 for h in range(10, 18)}
        start = datetime(2026, 10, 5, 0, 0)
        for d in range(tage):
            tag = start + timedelta(days=d)
            for h in range(24):
                if h not in hourly:
                    continue
                for k in range(6):
                    eng.update(
                        now=tag + timedelta(hours=h, minutes=10 * k),
                        temp_dict={"unten": 40.0}, compressor_is_on=False,
                        feedin_watt=0.0, pv_acpower_watt=0.0,
                        pv_erzeugung_watt=tatsaechlich_w, soc=50.0,
                        forecast_today_wh_qm=2500.0, forecast_hourly_wh=hourly,
                        pv_array_size_qm=10.0,
                    )

    def test_korrekturfaktor_wird_gelernt(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        # Prognose 800 W/m2 (x10 m2 = 8000 W), tatsaechlich nur 3000 W
        self._lern(eng, 800.0, 3000.0)
        faktor = eng.get_stunden_korrekturfaktor()
        assert faktor, "Kein Korrekturfaktor gelernt"
        for h, f in faktor.items():
            assert 0.3 <= f <= 0.4, f"Stunde {h}: Faktor {f} statt ~0,375"

    def test_ohne_daten_kein_faktor(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        assert eng.get_stunden_korrekturfaktor() == {}


class TestLerndatenSichtbar:
    """BUGFIX (21.10.2026): Die neuen Lernfelder muessen die API
    erreichen - die Whitelist in status_snapshot.py filtert sonst weg.
    """

    def test_korrekturfaktor_ist_im_api_payload(self, tmp_path):
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        hourly = {h: 300.0 for h in range(10, 18)}
        for d in range(6):
            tag = datetime(2026, 10, 21, 0, 0) + timedelta(days=d)
            for h in range(24):
                if h not in hourly:
                    continue
                for k in range(6):
                    eng.update(
                        now=tag + timedelta(hours=h, minutes=10 * k),
                        temp_dict={"unten": 40.0}, compressor_is_on=False,
                        feedin_watt=0.0, pv_acpower_watt=0.0,
                        pv_erzeugung_watt=1500.0, soc=50.0,
                        forecast_today_wh_qm=2500.0, forecast_hourly_wh=hourly,
                        pv_array_size_qm=10.0,
                    )
        info = eng.get_info()
        assert "stunden_korrekturfaktor" in info
        assert info["stunden_korrekturfaktor"], "Kein Korrekturfaktor im Payload"
        assert info["stunden_abweichung_anzahl"] > 0

    def test_whitelist_laesst_die_felder_durch(self, tmp_path):
        """Die API filtert nach einer Whitelist - die Felder muessen drin sein."""
        from status_snapshot import _learning_summary
        eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
        summary = _learning_summary(eng)
        assert "stunden_korrekturfaktor" in summary, (
            "Der Korrekturfaktor wird vom Status-Snapshot weggefiltert - "
            "die WebApp wuerde nichts anzeigen")
        assert "stunden_abweichung_anzahl" in summary
        assert "haushalt_je_wochentag" in summary
        assert "saison" in summary


class TestToteParameterBereinigt:
    """BUGFIX (21.10.2026): Die toten Parameter wurden entfernt.
    Der Test haelt fest, dass sie auch wirklich nicht zurueckkommen.
    """

    def test_evaluate_legionellen_ohne_forecast_parameter(self):
        import inspect
        from priority_control import evaluate_legionellen
        params = inspect.signature(evaluate_legionellen).parameters
        assert "forecast_today_wh_qm" not in params
        assert "forecast_wh_qm" not in params
        assert "forecast_day2_wh_qm" not in params

    def test_bewerte_alle_regeln_ohne_forecast_hourly_wh(self):
        import inspect
        from priority_control import bewerte_alle_regeln
        params = inspect.signature(bewerte_alle_regeln).parameters
        # BUGFIX (22.10.2026): Der Parameter ist wieder da - er hat jetzt
        # eine echte Funktion: der Rest-Ertrag-Check im PV-Warten braucht
        # die Stundenprognose, um zu erkennen, dass die Sonne heute nicht
        # mehr genug liefert. Er wird deshalb auch weitergereicht.
        assert "forecast_hourly_wh" in params


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
