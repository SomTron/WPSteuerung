# -*- coding: utf-8 -*-
"""Etappenrechnung in CalcStart (Nutzerentscheidung 05.11.2026).

Der Speicher hat mehrere Bedarfszeitpunkte (gemessen an 170 Zapfungen:
Schwerpunkt 18-21 Uhr, zweiter Schwerpunkt 13 Uhr). Statt einer
zeitgesteuerten Mindesttemperatur rechnet CalcStart fuer jede Etappe per
Vorwaertssimulation des kalibrierten Speichermodells die Heizzeit und
entscheidet nach der dringendsten (kleinster Restpuffer).

Warum Simulation statt Rate: Die Rate des mittleren Fuehlers haengt vom
Schichtungsabstand ab (bei delta <= 4 K +3,75 K/h, bei delta > 8 K
-1,25 K/h). Eine Konstante verschiebt den Start um Stunden.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_config import CalcEtappeConfig, CalculatedStartConfig  # noqa: E402
from priority_control import evaluate_calculated_start  # noqa: E402


def _config(etappen=None, soll=42.0, ziel=17):
    return CalculatedStartConfig(
        aktiv=True,
        prioritaet=82,
        solltemperatur_c=soll,
        target_uhr=ziel,
        heizrate_unten_c_h=3.0,
        heizrate_gesamt_c_h=2.0,
        tmax_c=48.0,
        netz_solltemperatur_c=0.0,
        etappen=etappen or [],
    )


MITTAG = CalcEtappeConfig(name="Mittag", ziel_uhr=13.0, solltemperatur_c=40.0,
                          temperaturfuehler="mitte", puffer_h=0.5)
ABEND = CalcEtappeConfig(name="Abend", ziel_uhr=18.0, solltemperatur_c=42.0,
                         temperaturfuehler="unten", puffer_h=0.5)


def _ruf(cfg, temps, stunde, minute=0, feedin=0.0, soc=None, profil=None):
    return evaluate_calculated_start(
        cfg, temps, stunde, minute,
        feedin_watt=feedin, soc=soc,
        learned_heating_rate_unten=3.0, learned_heating_rate_gesamt=2.0,
        surplus_profile=profil,
    )


# Hausverbrauch: Grundlast 500 W, Kochen 9-11 Uhr mit 2000 W.
# Median der Tagesstunden liegt bei 500 W -> Schwelle max(800, 500*1.4) = 800 W.
# Nur die Kochstunden gelten damit als Verbrauchs-Tief.
KOCH_PROFIL = {str(h): (2000.0 if 9 <= h <= 11 else 500.0) for h in range(6, 23)}


class TestEtappenplan:
    def test_plan_enthaelt_alle_etappen_mit_puffer(self):
        erg = _ruf(_config([MITTAG, ABEND]), {"unten": 30.0, "mittig": 38.0,
                                             "oben": 45.0}, 9)
        plan = erg.regel_dict["etappen"]
        assert [e["name"] for e in plan] == ["Mittag", "Abend"]
        assert all("puffer_h" in e and "hours_needed" in e for e in plan)

    def test_etappe_nennt_spaetesten_start(self):
        """09.11.2026: Jede Etappe nennt auch den letzten moeglichen Start.

        Befund: In der WebApp stand "geplant: 08:46" (globale Ein-Ziel-Rechnung
        inkl. Mittagstief-Aufschlag) neben "Start 10:30" (Etappenrechnung) -
        das las sich wie ein Widerspruch. Jetzt gibt es den spaetesten Start
        je Etappe: Ziel - Heizzeit - Spaetstart-Puffer.
        """
        erg = _ruf(_config([MITTAG, ABEND]), {"unten": 30.0, "mittig": 38.0,
                                              "oben": 45.0}, 9)
        for e in erg.regel_dict["etappen"]:
            assert "spaetester_start_uhr" in e, e
            assert e["spaetester_start_uhr"] < e["ziel_uhr"], e
            assert e["spaetester_start_uhr"] == round(
                e["ziel_uhr"] - e["hours_needed"] - 0.5, 2), e

        """Gleiche Solltemperatur, unterschiedlicher Abstand -> andere Zeit."""
        cfg = _config([MITTAG])
        nah = _ruf(cfg, {"unten": 37.0, "mittig": 39.0, "oben": 45.0}, 9)
        fern = _ruf(cfg, {"unten": 22.0, "mittig": 39.0, "oben": 45.0}, 9)
        h_nah = nah.regel_dict["etappen"][0]["hours_needed"]
        h_fern = fern.regel_dict["etappen"][0]["hours_needed"]
        assert h_fern > h_nah, (
            "bei kaltem unteren Fuehler muss die Heizzeit laenger sein "
            "(%.2f h vs. %.2f h)" % (h_fern, h_nah))

    def test_etappe_wartet_wenn_der_puffer_reicht(self):
        """09:00 bis 13:00 sind 4 h - die Heizzeit liegt deutlich darunter."""
        erg = _ruf(_config([MITTAG]), {"unten": 37.0, "mittig": 39.0,
                                       "oben": 45.0}, 9)
        assert erg.einschalten is not True
        assert "etappen" in (erg.regel_dict or {})

    def test_etappe_startet_wenn_es_knapp_wird(self):
        """Kurz vor der Zielzeit muss sie einschalten - mit Quelle."""
        erg = _ruf(_config([MITTAG]), {"unten": 36.0, "mittig": 38.5,
                                       "oben": 45.0}, 12, 50, feedin=150.0, soc=60.0)
        assert erg.einschalten is True

    def test_alle_etappen_erfuellt_ist_stumm(self):
        """Sind beide Ziele erreicht, darf nichts heizen."""
        erg = _ruf(_config([MITTAG, ABEND]), {"unten": 45.0, "mittig": 46.0,
                                             "oben": 48.0}, 10)
        assert erg.einschalten is not True
        assert "erreicht" in erg.grund

    def test_dringendste_etappe_bestimmt_ziel(self):
        """Kurz vor 13:00 ist Mittag dringend, nicht der Abend."""
        erg = _ruf(_config([MITTAG, ABEND]), {"unten": 30.0, "mittig": 38.0,
                                             "oben": 45.0}, 12, 40)
        plan = {e["name"]: e for e in erg.regel_dict["etappen"]}
        assert plan["Mittag"]["puffer_h"] < plan["Abend"]["puffer_h"]

    def test_ohne_etappen_bleibt_das_alte_verhalten(self):
        """Leere Etappenliste -> Ratenrechnung wie vorher (kein Etappen-Key)."""
        erg = _ruf(_config(), {"unten": 34.0, "mittig": 36.0, "oben": 45.0},
                   16, 0)
        assert "etappen" not in (erg.regel_dict or {})

    def test_plan_liefert_startzeit_und_markiert_die_dringendste(self):
        """WebApp-Anforderung: 'wann ist ein Start geplant' (05.11.2026)."""
        erg = _ruf(_config([MITTAG, ABEND]), {"unten": 30.0, "mittig": 38.0,
                                             "oben": 45.0}, 12, 40)
        plan = erg.regel_dict["etappen"]
        for e in plan:
            assert "start_uhr" in e, "Startzeit je Etappe fehlt (WebApp)"
            assert abs(e["start_uhr"] - (e["ziel_uhr"] - e["hours_needed"])) <= 0.02
        assert sum(1 for e in plan if e.get("dringendste")) == 1

    def test_puffer_enthaelt_den_mittagstief_aufschlag(self):
        """BUGFIX 11.11.2026: Anzeige und Entscheidung muessen dasselbe sagen.

        Produktionsfall (Log 12.11.2026, 08:00): Ziel Mittag 12:00/40 C am
        Fuehler "mitte", Speicher 32.1 C, Kochen 9-11 Uhr. Die Anzeige zeigte
        "Puffer 1.4 h", die Regel entschied "ZU SPAET" - weil der
        Mittagstief-Aufschlag nur in der Entscheidung, nicht in der Anzeige
        steckte.
        """
        mittag12 = CalcEtappeConfig(
            name="Mittag", ziel_uhr=12.0, solltemperatur_c=40.0,
            temperaturfuehler="mitte", puffer_h=0.5,
        )
        temps = {"unten": 16.9, "mittig": 32.1, "oben": 41.5}

        ohne = _ruf(_config([mittag12]), temps, 8)
        e_ohne = ohne.regel_dict["etappen"][0]
        assert e_ohne["dip_h"] == 0.0
        assert e_ohne["puffer_h"] > 0, e_ohne

        mit = _ruf(_config([mittag12]), temps, 8, profil=KOCH_PROFIL)
        e_mit = mit.regel_dict["etappen"][0]
        # 3 Kochstunden (9,10,11) x 0,75 = 2,25 h Aufschlag.
        assert e_mit["dip_h"] == 3.0
        assert e_mit["dip_aufschlag_h"] == 2.25
        assert e_mit["dip_label"], "das Tief muss benannt sein (WebApp)"
        # Der Puffer sinkt um genau diesen Aufschlag ...
        assert abs(
            (e_ohne["puffer_h"] - e_mit["puffer_h"]) - 2.25
        ) < 0.01, (e_ohne["puffer_h"], e_mit["puffer_h"])
        # ... und ist jetzt negativ -> die WebApp zeigt "ZU SPAET".
        assert e_mit["puffer_h"] < 0, e_mit

    def test_puffer_und_entscheidung_stimmen_ueberein(self):
        """Negativer Puffer <=> Regel meldet Zeitablauf (gleiche Rechnung)."""
        mittag12 = CalcEtappeConfig(
            name="Mittag", ziel_uhr=12.0, solltemperatur_c=40.0,
            temperaturfuehler="mitte", puffer_h=0.5,
        )
        temps = {"unten": 16.9, "mittig": 32.1, "oben": 41.5}
        erg = _ruf(_config([mittag12]), temps, 8, profil=KOCH_PROFIL)
        etappe = erg.regel_dict["etappen"][0]
        assert etappe["puffer_h"] < 0
        assert erg.einschalten is True
        assert "ZU SPAET" in erg.grund, erg.grund

    def test_webapp_zeigt_den_etappenplan(self):
        """Quelltext-Guard: die WebApp rendert Startzeit, Soll und Puffer."""
        pfad = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "webapp", "index.html")
        with open(pfad, encoding="utf-8") as fh:
            quelle = fh.read()
        assert "calc.etappen" in quelle
        assert "formatCalcHour(e.start_uhr)" in quelle
        assert "etappenHtml" in quelle
        assert "dringendste" in quelle

    def test_webapp_weist_den_tief_aufschlag_aus(self):
        """BUGFIX 11.11.2026: Die Anzeige muss den Aufschlag erklaeren.

        Ohne diesen Hinweis steht in der App "ZU SPAET 1.0 h", ohne dass
        erkennbar ist, dass 2,25 h davon der gelernte Mittagstief sind.
        """
        pfad = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "webapp", "index.html")
        with open(pfad, encoding="utf-8") as fh:
            quelle = fh.read()
        assert "dip_aufschlag_h" in quelle, (
            "die WebApp liest den Tief-Aufschlag nicht")
        assert "dip_label" in quelle, (
            "das Tief wird nicht benannt (z. B. '9-12 Uhr')")

    def test_etappe_mit_falschem_ziel_wird_abgelehnt(self):
        with pytest.raises(ValueError):
            CalcEtappeConfig(name="X", ziel_uhr=25.0)
        with pytest.raises(ValueError):
            CalcEtappeConfig(name="X", temperaturfuehler="seite")
