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
    def test_zapfung_am_abend_aktualisiert_zielzeit(self, engine):
        """Zapfungen sammeln sich als Zielzeit-Samples.

        API-Vertrag: get_learned_target_hour() liefert den Default 17.0,
        bis mind. 3 Samples gesammelt sind -- erst dann gilt der gelernte Wert.
        """
        for i in range(3):
            t = datetime(2026, 1, 15, 18, i * 10)
            oben_vor = 46.0  # vor jeder Zapfung wieder "aufgefuellt"
            engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": oben_vor}, False)
            engine.update(t + timedelta(minutes=5),
                          {"unten": 43.0, "mittig": 44.0, "oben": oben_vor - 2.0}, False)

            if i == 0:
                # Unterhalb der Schwelle gilt weiterhin der Default ...
                assert engine.get_learned_target_hour() == 17.0
                # ... auch wenn intern schon gelernt wurde
                # Erste Zapfung: hour_f wird exakt uebernommen (Drop um 18:05)
                assert engine.data.learned_target_hour == pytest.approx(18.083, abs=0.01)

        # Alle 3 Zapfungen landen in der Historie ...
        assert len(engine.data.usage_events) == 3
        # ... aber nur die ERSTEN 2 pro Abend fliessen in die Zielzeit ein
        # (Design im Code: "Nur erste Zapfung(en) heute Abend beruecksichtigen")
        assert engine.data.target_hour_samples == 2
        # EWMA (alpha=0.15) aus den ersten beiden Ereignissen (~18:05/~18:15):
        # z1=18.083 exakt, z2 = 18.083*0.85 + 18.25*0.15 = 18.108.
        # Da weiterhin < 3 Samples: API liefert weiterhin den Default 17.0.
        assert engine.data.learned_target_hour == pytest.approx(18.11, abs=0.005)
        assert engine.get_learned_target_hour() == 17.0

    def test_zapfung_ausserhalb_des_zeitfensters_wird_ignoriert(self, engine):
        t = datetime(2026, 1, 15, 4, 30)   # vor 05:00 (neues Fenster 05-23)
        engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(t + timedelta(minutes=10),
                      {"unten": 43.0, "mittig": 44.0, "oben": 44.0}, False)
        t2 = datetime(2026, 1, 15, 23, 30)  # ab 23:00
        engine.update(t2, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(t2 + timedelta(minutes=10),
                      {"unten": 43.0, "mittig": 44.0, "oben": 44.0}, False)
        assert engine.get_info()["total_usage_events"] == 0

    def test_morgens_zapfung_zaehlt_zur_morgen_zielzeit(self, engine):
        """Zapfungen vor 12 Uhr lernen die MORGEN-Zielzeit, nicht die Abend-Zeit."""
        for i in range(4):
            t = datetime(2026, 1, 9 + i, 7, 0)
            engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
            engine.update(t + timedelta(minutes=5),
                          {"unten": 43.0, "mittig": 44.0, "oben": 44.0}, False)
        info = engine.get_info()
        assert info["morning_target_hour_samples"] == 4
        assert engine.get_learned_morning_target_hour() == pytest.approx(7.0, abs=0.1)
        # Abend-Zielzeit unbeeinflusst
        assert engine.data.target_hour_samples == 0
        assert engine.get_learned_target_hour() == 17.0
        # Gelerntes Morgenfenster vorhanden (ab 4 Samples in 14 Tagen)
        fenster = engine.get_learned_morning_window(now=datetime(2026, 1, 20, 12, 0))
        assert fenster is not None
        assert fenster[0] <= 6.2   # Zapfungen ~07:05 minus 1h Vorlauf
        assert fenster[1] >= 7.5   # plus 45min Nachlauf

    def test_kleiner_abfall_ist_keine_zapfung(self, engine):
        t = datetime(2026, 1, 15, 18, 0)
        engine.update(t, {"unten": 43.0, "mittig": 44.0, "oben": 46.0}, False)
        engine.update(t + timedelta(minutes=10),
                      {"unten": 43.0, "mittig": 44.0, "oben": 45.5}, False)  # nur 0.5K
        assert engine.get_info()["total_usage_events"] == 0


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
        assert zweiter.data.version == 3

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

        # Zapfungen: 101 Ereignisse im Fenster 16-23h
        z = datetime(2026, 1, 20, 17, 0)
        oben = 46.0
        for i in range(102):
            engine.update(z + timedelta(minutes=i),
                          {"unten": 43.0, "mittig": 44.0, "oben": oben}, False)
            oben -= 2.0
        assert len(engine.data.usage_events) == 100
