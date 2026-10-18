"""Tests fuer die LearningEngine: Persistenz, Atomaritaet, Lernlogik.

Hintergrund: Die Lerndaten (gelernte Heizraten + Zielzeit) sind ueber Monate
akkumuliertes Wissen. Ein Stromausfall mitten im Schreiben darf sie nicht
korruptieren -- daher speichert _save() atomar (.tmp + os.replace) und
_load() sichert unlesbare Dateien als *.korrupt-* bevor Defaults greifen.
"""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from learning_engine import LearningEngine


@pytest.fixture
def pfad(tmp_path):
    return str(tmp_path / "learning_data.json")


@pytest.fixture
def engine(pfad):
    return LearningEngine(data_path=pfad)


# ── Defaults ──────────────────────────────────────────────────────────

class TestDefaults:
    def test_fehlende_datei_liefert_defaults(self, engine):
        assert engine.get_learned_heating_rate(6) == 3.0      # unten
        assert engine.get_learned_heating_rate(6, "gesamt") == 2.0
        assert engine.get_learned_target_hour() == 17.0
        assert engine.get_info()["total_cycles"] == 0

    def test_weniger_als_3_samples_liefert_defaults(self, engine):
        engine.data.heat_rates["winter"] = {"avg": 5.5, "count": 2}
        assert engine.get_learned_heating_rate(1) == 3.0      # count < 3 -> Default

    def test_gelernte_rate_ab_3_samples(self, engine):
        engine.data.heat_rates["winter"] = {"avg": 4.0, "count": 7}
        assert engine.get_learned_heating_rate(1) == pytest.approx(4.0)
        assert engine.get_learned_heating_rate(1, "gesamt") == pytest.approx(round(4.0 * 0.67, 2))


# ── Heizzyklen ────────────────────────────────────────────────────────

class TestHeizzyklen:
    def test_zyklus_wird_erkannt_und_gespeichert(self, engine):
        start = datetime(2026, 1, 15, 8, 0)
        temps_kalt = {"unten": 40.0, "mittig": 41.0, "oben": 45.0}
        temps_warm = {"unten": 43.0, "mittig": 44.5, "oben": 47.0}

        engine.update(start, temps_kalt, compressor_is_on=True)
        engine.update(start + timedelta(minutes=60), temps_warm, compressor_is_on=False)

        info = engine.get_info()
        assert info["total_cycles"] == 1
        # Heizrate unten: 3K / 1h = 3.0 C/h (Winter)
        assert engine.data.heat_rates["winter"]["count"] == 1
        assert engine.data.heat_rates["winter"]["avg"] == pytest.approx(3.0)
        assert os.path.exists(engine.data_path), "_finalize_cycle muss persistieren"

    def test_zu_kurzer_zyklus_wird_ignoriert(self, engine):
        start = datetime(2026, 1, 15, 8, 0)
        temps = {"unten": 40.0, "mittig": 41.0, "oben": 45.0}
        engine.update(start, temps, compressor_is_on=True)
        engine.update(start + timedelta(minutes=4), temps, compressor_is_on=False)
        assert engine.get_info()["total_cycles"] == 0

    # ── Rate nur aus repräsentativen Zyklen (Log 18.09.-01.10.2026) ──

    def test_kurzzyklus_zuehlt_nicht_zur_planungsrate(self, engine):
        """Ein 15-min-Zyklus ist eine Lokalmessung, keine Planungsgroesse.

        Log-Beleg: ueber die 68 Zyklen gemittelt ergab sich 13,86 K/h, die
        nachhaltige Rate (Kaltstart, 92 % Laufzeit) aber 4,73 K/h. Mit der
        zu schnellen Rate fiel der Startvorlauf des CalcStart um rund zwei
        Drittel zu kurz aus (brauchte 1.1h statt physikalisch 3,96 h).
        """
        start = datetime(2026, 1, 15, 8, 0)
        kalt = {"unten": 40.0, "mittig": 41.0, "oben": 45.0}
        warm = {"unten": 43.75, "mittig": 44.75, "oben": 48.0}   # 15 K/h lokal
        engine.update(start, kalt, compressor_is_on=True)
        engine.update(start + timedelta(minutes=15), warm, compressor_is_on=False)

        assert engine.data.heat_rates["winter"]["count"] == 0, (
            "Kurzzyklus darf die Planungsrate nicht veraendern"
        )
        # Der Zyklus selbst wird weiterhin protokolliert.
        assert engine.get_info()["total_cycles"] == 1
        # Ohne verwertbare Samples bleibt der konservative Default.
        assert engine.get_learned_heating_rate(1) == 3.0

    def test_langer_zyklus_zuehlt_zur_planungsrate(self, engine):
        start = datetime(2026, 1, 15, 8, 0)
        kalt = {"unten": 23.7, "mittig": 30.0, "oben": 45.0}
        warm = {"unten": 42.0, "mittig": 46.0, "oben": 57.0}    # ~4,7 K/h
        engine.update(start, kalt, compressor_is_on=True)
        engine.update(start + timedelta(minutes=240), warm, compressor_is_on=False)

        assert engine.data.heat_rates["winter"]["count"] == 1
        assert engine.data.heat_rates["winter"]["avg"] < 6.0

    def test_lokale_rate_wird_auf_die_physikalische_grenze_gekappt(self, engine):
        """Ein haengender Sensor darf die Startplanung nicht aufblaehen.

        300-L-Speicher, 600 W elektrisch, COP >= 3: der GANZE Speicher
        kann hoechstens ~5,2 K/h annehmen.
        """
        start = datetime(2026, 1, 15, 8, 0)
        kalt = {"unten": 30.0, "mittig": 40.0, "oben": 45.0}
        warm = {"unten": 48.0, "mittig": 52.0, "oben": 55.0}   # 36 K/h in 30 min
        engine.update(start, kalt, compressor_is_on=True)
        engine.update(start + timedelta(minutes=30), warm, compressor_is_on=False)

        assert engine.data.heat_rates["winter"]["avg"] <= 6.0, (
            "Rate wurde nicht auf die Speichergrenze begrenzt"
        )

    def test_rate_grenze_ist_konfigurierbar(self):
        from learning_engine import LearningConfig
        cfg = LearningConfig()
        assert cfg.heating_rate_lern_min_minuten == 30.0
        assert cfg.heating_rate_max_c_h == 6.0

    def test_altbestand_wird_beim_lesen_gekappt(self, engine):
        """Regression Betriebslog 02.10.2026.

        Die Kappung beim Lernen greift fuer bereits persistierte Werte erst
        mit der EWMA-Zeitkonstante (alpha=0.10), also nach Wochen. Im Log
        rechnete der Heizplan weiterhin mit 14.2 C/h ("noch 13.8K bei
        14.2 C/h -> erreicht ca. 09:34"). Der Wert wird deshalb auch beim
        Lesen begrenzt.
        """
        engine.data.heat_rates["transition"] = {"avg": 14.2, "count": 40}
        assert engine.get_learned_heating_rate(10) == pytest.approx(6.0)

    def test_werte_unter_der_grenze_bleiben_unveraendert(self, engine):
        engine.data.heat_rates["transition"] = {"avg": 4.7, "count": 12}
        assert engine.get_learned_heating_rate(10) == pytest.approx(4.7)

    def test_grenze_laesst_sich_abschalten(self, tmp_path):
        from learning_engine import LearningConfig, LearningEngine
        cfg = LearningConfig(heating_rate_max_c_h=0.0)
        engine = LearningEngine(data_path=str(tmp_path / "l.json"))
        engine.data.config = cfg
        engine.data.heat_rates["transition"] = {"avg": 14.2, "count": 40}
        assert engine.get_learned_heating_rate(10) == pytest.approx(14.2)

    def test_saison_bestimmt_die_rate(self, engine):
        for monat, saison in [(1, "winter"), (4, "transition"), (7, "summer")]:
            e = LearningEngine(data_path=engine.data_path)
            start = datetime(2026, monat, 10, 8, 0)
            e.update(start, {"unten": 40.0, "mittig": 41.0, "oben": 45.0}, True)
            e.update(start + timedelta(minutes=30),
                     {"unten": 42.0, "mittig": 43.0, "oben": 46.0}, False)
            assert list(e.data.heat_rates[saison].values()) != [3.0, 0], \
                f"Saison {saison} sollte aktualisiert worden sein"


# ── Zapfungs-Erkennung ────────────────────────────────────────────────

class TestZapfung:
    """Zapfungs-Erkennung nach der Neufassung vom 18.10.2026.

    Ein Vorgang wird ueber ein Fenster verfolgt (Ruhe 5 min, max. 20 min) und
    mit dem MAXIMALEN Abfall gespeichert. Gelernt wird er nur, wenn er
    plausibel ist: staerkster Abfall >= 2 K UND Flanke >= 1 K/min. Vorher wurde
    der Ein-Sample-Abfall gespeichert - dadurch hatten 26 von 27 Ereignissen
    < 1 K, und der 10-min-Cooldown verschluckte danach die echte Zapfung
    (18.10.2026: "gew. 0,56K" gelernt, waehrend der Speicher von 41,8 auf
    22,2 C fiel).
    """

    def _zapfe(self, engine, start, unten_vor=43.0, unten_nach=40.0,
               oben_vor=46.0, oben_nach=44.0, dauer_min=1):
        """Steiler Abfall + Ruhe-Sample: erzeugt genau EIN Ereignis."""
        engine.update(start, {"unten": unten_vor, "mittig": 44.0, "oben": oben_vor}, False)
        engine.update(start + timedelta(minutes=dauer_min),
                      {"unten": unten_nach, "mittig": 43.0, "oben": oben_nach}, False)
        engine.update(start + timedelta(minutes=dauer_min + 6),
                      {"unten": unten_nach, "mittig": 43.0, "oben": oben_nach}, False)

    def test_zapfung_am_abend_aktualisiert_zielzeit(self, engine):
        """Zapfungen sammeln sich als Zielzeit-Samples.

        API-Vertrag: get_learned_target_hour() liefert den Default 17.0,
        bis mind. 3 Samples gesammelt sind -- erst dann gilt der gelernte Wert.
        """
        for stunde, minute in ((18, 0), (18, 20), (18, 40)):
            self._zapfe(engine, datetime(2026, 1, 15, stunde, minute))

        # Alle drei Vorgaenge landen in der Historie ...
        assert len(engine.data.usage_events) == 3
        # ... aber nur die ERSTEN 2 pro Abend fliessen in die Zielzeit ein
        # (Design im Code: "Nur erste Zapfung(en) heute Abend beruecksichtigen")
        assert engine.data.target_hour_samples == 2
        # Der Zeitstempel ist der BEGINN des Abfalls (erster Sample ueber der
        # Schwelle), nicht das Ende des Verfolgungsfensters.
        assert engine.data.usage_events[0]["timestamp"].startswith("2026-01-15T18:01")
        # EWMA (alpha=0.15) aus 18:01 und 18:21: z1=18.017 exakt,
        # z2 = 18.017*0.85 + 18.35*0.15 = 18.067.
        assert engine.data.learned_target_hour == pytest.approx(18.07, abs=0.01)
        assert engine.get_learned_target_hour() == 17.0

    def test_zapfung_ausserhalb_des_zeitfensters_wird_ignoriert(self, engine):
        # 04:30 liegt vor dem Fenster (05-23), 23:30 dahinter
        for stunde, minute in ((4, 30), (23, 30)):
            start = datetime(2026, 1, 15, stunde, minute)
            engine.update(start, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
            engine.update(start + timedelta(minutes=1),
                          {"unten": 40.0, "mittig": 43.0, "oben": 44.0}, False)
            engine.update(start + timedelta(minutes=7),
                          {"unten": 40.0, "mittig": 43.0, "oben": 44.0}, False)
        assert engine.get_info()["total_usage_events"] == 0

    def test_rauschen_und_echte_zapfung_sind_ein_vorgang(self, engine):
        """Kernfall 18.10.2026, 18:28: Der erste kleine Schritt (0,6 K) loeste
        die Erkennung aus, der Cooldown verschluckte die eigentliche Zapfung -
        gelernt wurden "gew. 0,56K" statt der 19,6 K, die der Speicher verlor.
        Jetzt verfolgt derselbe Vorgang den ganzen Abfall."""
        t = datetime(2026, 1, 15, 18, 28)
        engine.update(t, {"unten": 41.8, "mittig": 61.3, "oben": 61.6}, False)
        engine.update(t + timedelta(minutes=1),
                      {"unten": 41.2, "mittig": 61.3, "oben": 61.6}, False)
        engine.update(t + timedelta(minutes=2),
                      {"unten": 37.0, "mittig": 58.0, "oben": 61.4}, False)
        engine.update(t + timedelta(minutes=4),
                      {"unten": 27.0, "mittig": 52.0, "oben": 61.0}, False)
        engine.update(t + timedelta(minutes=10),
                      {"unten": 22.2, "mittig": 48.0, "oben": 60.5}, False)
        engine.update(t + timedelta(minutes=16),
                      {"unten": 22.2, "mittig": 48.0, "oben": 60.5}, False)

        assert len(engine.data.usage_events) == 1, "Rauschen und Zapfung = EIN Vorgang"
        eintrag = engine.data.usage_events[0]
        assert eintrag["plausibel"] is True
        assert eintrag["drop_unten_k"] == pytest.approx(19.6, abs=0.2)
        assert eintrag["timestamp"].startswith("2026-01-15T18:29")
        assert engine.data.target_hour_samples == 1

    def test_kleinabfall_verschiebt_die_zielzeit_nicht(self, engine):
        """Ein 0,5-K-Rauschen wird protokolliert, aber NICHT gelernt."""
        t = datetime(2026, 1, 15, 18, 0)
        engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(t + timedelta(minutes=1),
                      {"unten": 42.5, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(t + timedelta(minutes=7),
                      {"unten": 42.5, "mittig": 44.0, "oben": 46.0}, False)

        assert len(engine.data.usage_events) == 1
        assert engine.data.usage_events[0]["plausibel"] is False
        assert engine.data.usage_events[0]["drop_unten_k"] == pytest.approx(0.5)
        assert engine.data.target_hour_samples == 0
        assert engine.data.learned_target_hour == 17.0

    def test_nur_die_steile_phase_zaehlt(self, engine):
        """Rauschen plus anschliessendes Abkuehlen ergibt keine Zapfung.

        Der Speicher kuehlt nach einem Lauf mit bis zu 0,5 K/min ab. Wuerde der
        Vorgang nur nach der Gesamt-Summe beurteilt, entstuende daraus ueber das
        20-min-Fenster eine scheinbare "Zapfung"; deshalb zaehlt nur die steile
        Phase (>= 1 K/min in den Defaults).
        """
        t = datetime(2026, 1, 15, 18, 0)
        engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        # steiler Sensor-Sprung loest die Erkennung aus (0,6 K in 1 min) ...
        engine.update(t + timedelta(minutes=1),
                      {"unten": 42.4, "mittig": 44.0, "oben": 46.0}, False)
        # ... danach nur noch langsames Abkuehlen (0,1 K/min, 2 K gesamt)
        for minute in range(2, 22):
            engine.update(t + timedelta(minutes=minute),
                          {"unten": 42.4 - 0.1 * (minute - 1), "mittig": 44.0,
                           "oben": 46.0}, False)

        assert len(engine.data.usage_events) == 1
        eintrag = engine.data.usage_events[0]
        assert eintrag["plausibel"] is False
        # Der Vorgang endet 5 min nach der steilen Phase (Ruhe-Fenster), das
        # anschliessende Abkuehlen wird also nur angeschnitten erfasst -
        # GELERNT wird nichts, weil die steile Phase nur 0,6 K bewegte.
        assert eintrag["drop_unten_k"] == pytest.approx(1.1, abs=0.1)
        assert engine.data.target_hour_samples == 0

    def test_morgens_zapfung_zaehlt_zur_morgen_zielzeit(self, engine):
        """Zapfungen vor 12 Uhr lernen die MORGEN-Zielzeit, nicht die Abend-Zeit."""
        for i in range(4):
            self._zapfe(engine, datetime(2026, 1, 9 + i, 7, 0))
        info = engine.get_info()
        assert info["morning_target_hour_samples"] == 4
        assert engine.get_learned_morning_target_hour() == pytest.approx(7.0, abs=0.1)
        # Abend-Zielzeit unbeeinflusst
        assert engine.data.target_hour_samples == 0
        assert engine.get_learned_target_hour() == 17.0
        # Gelerntes Morgenfenster vorhanden (ab 4 Samples in 14 Tagen)
        fenster = engine.get_learned_morning_window(now=datetime(2026, 1, 20, 12, 0))
        assert fenster is not None
        assert fenster[0] <= 6.2   # Zapfungen ~07:01 minus 1h Vorlauf
        assert fenster[1] >= 7.5   # plus 45min Nachlauf

    def test_zeitlich_entprellte_zapfung(self, engine):
        """Ein dauerhafter Abfall darf nur einmal als Zapfung gelten."""
        t = datetime(2026, 1, 15, 18, 0)
        engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        for minute in range(1, 8):
            engine.update(
                t + timedelta(minutes=minute),
                {"unten": 42.0, "mittig": 44.0, "oben": 45.0},
                False,
            )
        assert len(engine.data.usage_events) == 1

        # Nach dem Cooldown (gemessen ab dem ENDE des Vorgangs) ist ein neuer,
        # eigenstaendiger Abfall wieder ein gueltiges Lernereignis.
        self._zapfe(engine, t + timedelta(minutes=17))
        assert len(engine.data.usage_events) == 2

    def test_komfort_verletzung_wird_entprellt(self, engine, caplog):
        """Regression Nutzer-Log 28.09.2026, 18:43:

            18:43:18 WARNING KOMFORT-VERLETZUNG: t_oben 24.4C < 40.0C
            18:43:28 WARNING KOMFORT-VERLETZUNG: t_oben 24.4C < 40.0C
            18:43:37 WARNING KOMFORT-VERLETZUNG: t_oben 24.4C < 40.0C

        Eine anhaltende Kuehlphase ist EIN Ereignis. Sie darf weder dreimal
        gemeldet noch dreimal gezaehlt werden - sonst verzerrt sie
        `verletzungen_7d` in der WebApp um den Faktor 3.
        """
        import logging as _logging

        t = datetime(2026, 9, 28, 18, 43, 18)
        temps = {"unten": 23.2, "mittig": 23.4, "oben": 24.4, "verd": 9.6}

        with caplog.at_level(_logging.WARNING, logger="root"):
            for k in range(12):          # 12 Samples im 10-s-Takt = 2 min
                engine.update(
                    t + timedelta(seconds=10 * k), temps, False,
                )

        warnungen = [
            r for r in caplog.records if "KOMFORT-VERLETZUNG" in r.getMessage()
        ]
        assert len(warnungen) == 1, (
            f"{len(warnungen)} Warnungen statt 1 fuer eine einzige Kuehlphase"
        )
        assert len(engine.data.komfort_verletzungen) == 1, (
            f"{len(engine.data.komfort_verletzungen)} Eintraege statt 1 - "
            "die Statistik in der WebApp waere dreifach verzerrt"
        )

    def test_komfort_verletzung_zaehlt_spätere_kuehlphase_getrennt(self, engine):
        """Nach dem Cooldown ist eine NEUE Kuehlphase wieder ein Ereignis."""
        t = datetime(2026, 9, 28, 9, 0, 0)
        engine.update(t, {"unten": 23.0, "mittig": 23.0, "oben": 24.0, "verd": 9.0},
                      False)
        engine.update(
            t + timedelta(minutes=engine.data.config.comfort_cooldown_min + 5),
            {"unten": 23.0, "mittig": 23.0, "oben": 24.0, "verd": 9.0},
            False,
        )
        assert len(engine.data.komfort_verletzungen) == 2

    def test_komfort_verletzung_ignoriert_nachtsperre_und_warmen_speicher(self, engine):
        """Nachtsperre und ausreichende Temperatur bleiben unveraendert."""
        engine.update(datetime(2026, 9, 28, 23, 0),
                      {"unten": 23.0, "mittig": 23.0, "oben": 24.0, "verd": 9.0},
                      False)
        assert engine.data.komfort_verletzungen == []
        engine.update(datetime(2026, 9, 28, 10, 0),
                      {"unten": 44.0, "mittig": 44.0, "oben": 45.0, "verd": 9.0},
                      False)
        assert engine.data.komfort_verletzungen == []


# ── Persistenz ────────────────────────────────────────────────────────

class TestPersistenz:
    def test_roundtrip_ueber_neue_instanz(self, engine):
        start = datetime(2026, 1, 15, 8, 0)
        engine.update(start, {"unten": 40.0, "mittig": 41.0, "oben": 45.0}, True)
        engine.update(start + timedelta(minutes=60),
                      {"unten": 43.5, "mittig": 44.0, "oben": 47.0}, False)

        zweiter = LearningEngine(data_path=engine.data_path)
        assert zweiter.get_info()["total_cycles"] == 1
        assert zweiter.data.heat_rates["winter"] == engine.data.heat_rates["winter"]
        assert zweiter.data.version == 4

    def test_speichern_ist_atomar_alte_datei_ueberlebt_fehler(self, engine, pfad):
        """Wenn os.replace fehlschlaegt, bleibt die VORHERIGE Datei unbeschaedigt."""
        # Ersten gueltigen Stand erzeugen
        start = datetime(2026, 1, 15, 8, 0)
        engine.update(start, {"unten": 40.0, "mittig": 41.0, "oben": 45.0}, True)
        engine.update(start + timedelta(minutes=60),
                      {"unten": 43.0, "mittig": 44.0, "oben": 47.0}, False)
        with open(pfad, encoding="utf-8") as f:
            alter_inhalt = f.read()
        alte_daten = json.loads(alter_inhalt)

        # Zweiten Zyklus fahren, aber das atomare Ersetzen erzwingen einen Fehler
        engine.update(start + timedelta(hours=2),
                      {"unten": 40.0, "mittig": 41.0, "oben": 45.0}, True)
        with patch("learning_engine.os.replace", side_effect=OSError("Platte voll")):
            engine.update(start + timedelta(hours=3),
                          {"unten": 43.0, "mittig": 44.0, "oben": 47.0}, False)

        # Datei muss weiterhin den ALTEN vollstaendigen Stand enthalten
        with open(pfad, encoding="utf-8") as f:
            aktueller = json.load(f)
        assert aktueller == alte_daten
        # Kein .tmp-Rest
        assert not os.path.exists(pfad + ".tmp")

    def test_korrupte_datei_liefert_defaults_und_wird_gesichert(self, pfad):
        with open(pfad, "w", encoding="utf-8") as f:
            f.write('{"cycles": [HALB')

        engine = LearningEngine(data_path=pfad)
        assert engine.get_info()["total_cycles"] == 0          # Defaults
        assert engine.get_learned_target_hour() == 17.0

        backups = [n for n in os.listdir(os.path.dirname(pfad))
                   if n.startswith("learning_data.json.korrupt-")]
        assert len(backups) == 1, "Korrupte Datei muss als *.korrupt-* gesichert werden"

    def test_caps_begrenzen_die_datengroesse(self, engine):
        """51 Zyklen und 101 Zapfungen -> Listen werden auf 50/100 gekappt."""
        basis = datetime(2026, 1, 15, 8, 0)
        temp = 38.0
        for i in range(52):
            an = basis + timedelta(hours=i)
            engine.update(an, {"unten": temp, "mittig": temp + 1, "oben": temp + 4}, True)
            temp += 0.5
            engine.update(an + timedelta(minutes=6),
                          {"unten": temp, "mittig": temp + 1, "oben": temp + 4}, False)
        assert len(engine.data.cycles) == 50

        # Zapfungen: Der Listen-Deckel wird unabhaengig von der Erkennungs-
        # dynamik geprueft (die Ereignisse entstehen jetzt zeitgesteuert ueber
        # das Verfolgungsfenster): 120 Alt-Eintraege + 1 neues Ereignis.
        z = datetime(2026, 1, 20, 17, 0)
        engine.data.usage_events = [
            {"timestamp": f"2026-01-0{1 + i % 9}T18:00:00", "drop_k": 3.0}
            for i in range(120)
        ]
        engine.update(z, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(z + timedelta(minutes=1),
                      {"unten": 40.0, "mittig": 43.0, "oben": 44.0}, False)
        engine.update(z + timedelta(minutes=7),
                      {"unten": 40.0, "mittig": 43.0, "oben": 44.0}, False)
        assert len(engine.data.usage_events) == 100


# ── Method Signatures ────────────────────────────────────────────────────

class TestUpdateSignature:
    """Prüft, dass learning_engine.update() alle in production verwendeten
    Parameter akzeptiert. Dies verhindert TypeError bei zukünftigen Änderungen
    an der Funktionssignatur (Integrationstest zwischen priority_control und
    learning_engine)."""

    def test_update_with_all_production_parameters(self, engine):
        """Vollständige Liste der in priority_control_logic.py:208-216
        verwendeten Parameter, damit Signatur-Änderungen sofort auffallen."""
        start = datetime(2026, 1, 15, 8, 0)
        now = datetime(2026, 1, 15, 10, 30)
        temp_dict = {"unten": 40.0, "mittig": 41.0, "oben": 45.0, "verd": 55.0}
        forecast_hourly_wh = {h: float(h * 100) for h in range(24)}

        # Dies wird in production aufgerufen (priority_control_logic.py:208-216).
        # Falls forecast_hourly_wh oder neue Parameter hinzukommen, MUSS dieser
        # Test fehlschlagen, um die Signatur anzupassen.
        engine.update(
            now=now,
            temp_dict=temp_dict,
            compressor_is_on=True,
            feedin_watt=200.0,
            soc=50.0,
            forecast_today_wh_qm=3000.0,
            forecast_hourly_wh=forecast_hourly_wh,
        )

        # Validierung: Die Methode muss erfolgreich ohne TypeError ausgeführt werden
        info = engine.get_info()
        assert info["total_cycles"] >= 0  # Sollte mindestens initialisiert sein
