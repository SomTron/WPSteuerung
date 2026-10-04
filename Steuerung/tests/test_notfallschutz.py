"""
Tests fuer den ausgekoppelten Notfallschutz (Prio 110).

Nutzeranforderung:
- Reiner Schutzleiter (<=36C): greift ohne Workaround vor allen Sperren
  (Wochenende, Nachtsperre).
- Der fruehere Komfort-Notfall ist damit obsolet - Komfort regelt nur noch
  den PV-abhaengigen Komfort (38C mit PV, AUS 42C).
Zusaetzlich werden die Prioritaeten-Kaskade (110/100/90/85/78/75) geprueft.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import (  # noqa: E402
    WPSteuerungConfig,
    NotfallschutzConfig,
    WochenendeConfig,
    LegionellenConfig,
    EinspeisungConfig,
    AdaptivePVConfig,
    PVRegel,
    BatterieConfig,
)
import priority_control as pc  # noqa: E402
import priority_control_logic as pcl  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ============================================================
# Startfreigabe gegen Nachtabschaltung (28.09.2026)
# ============================================================
class TestNachtabschaltungStart:
    """Vor jedem Einschalten: passt die Mindestlaufzeit vor bis_uhr?

    Nutzerwunsch: "vor jedem Einschalten pruefen, ob nicht vor Ende
    der Mindestlaufzeit es zur Nachtabschaltung kommt."
    """

    @staticmethod
    def _state(cutoff=22, legionellen=False, regel_name=None, pv_min=10):
        from types import SimpleNamespace as NS

        cfg = NS(
            notfallschutz=NS(bis_uhr=cutoff),
            zyklus=NS(mindestlaufzeit_minuten=60, pv_min_laufzeit_minuten=pv_min),
            legionellen=NS(legionellen_max_temp_c=65, max_duration_hours=8),
        )
        return NS(
            priority_config=cfg,
            legionellen_aktiv=legionellen,
            control=NS(requested_rule_name=regel_name),
        )

    def test_start_passt_genau_auf_cutoff(self):
        """21:00 + 60 min = 22:00 -> erlaubt (Grenzfall inklusive)."""
        st = self._state()
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 0), timedelta(minutes=60), "Regel"
        )
        assert ok is True

    def test_start_ueberschreitet_cutoff_gesperrt(self):
        """21:01 + 60 min = 22:01 -> gesperrt."""
        st = self._state()
        ok, grund = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 1), timedelta(minutes=60), "Regel"
        )
        assert ok is False
        assert "22:01" in grund and "22:00" in grund

    def test_start_deutlich_vor_cutoff_erlaubt(self):
        st = self._state()
        for h, m in ((18, 0), (19, 30), (20, 30)):
            ok, _ = pcl._pruefe_nachtabschaltung(
                st, datetime(2026, 9, 28, h, m), timedelta(minutes=60), "Regel"
            )
            assert ok is True, f"{h}:{m:02d} sollte erlaubt sein"

    def test_kein_cutoff_konfiguriert_alte_logik(self):
        st = self._state(cutoff=None)
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(minutes=60), "Regel"
        )
        assert ok is True

    def test_legionelle_startet_trotz_cutoff(self):
        """Prophylaxe darf nie an der Startzeit scheitern."""
        st = self._state(legionellen=True, regel_name="Legionellen")
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(minutes=60), "Legionellen"
        )
        assert ok is True

    def test_pv_lauf_mit_kurzer_mindestlaufzeit_erlaubt(self):
        """PV hat 10 min Mindestlaufzeit -> 21:30 passt vor 22:00."""
        st = self._state()
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(minutes=60), "Einspeisung"
        )
        assert ok is True

    def test_pv_lauf_ueber_cutoff_gesperrt(self):
        st = self._state()
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 55), timedelta(minutes=60), "Einspeisung"
        )
        assert ok is False

    def test_start_nach_cutoff_nicht_zweimal_geprueft(self):
        """Nach 22:00 zaehlt der naechste Cutoff erst morgen."""
        st = self._state()
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 23, 0), timedelta(minutes=60), "Regel"
        )
        assert ok is True

    def test_ungueltiger_cutoff_ignoriert(self):
        st = self._state(cutoff=99)
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(minutes=60), "Regel"
        )
        assert ok is True

    def test_keine_mindestlaufzeit_nie_gesperrt(self):
        """Ohne konfigurierte Mindestlaufzeit gibt es nichts zu pruefen."""
        st = self._state()
        st.priority_config.zyklus.mindestlaufzeit_minuten = 0
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(0), "Regel"
        )
        assert ok is True

    def test_json_mindestlaufzeit_schlaegt_basiswert(self):
        """Der JSON-Wert ist fuehrend: 0 als Basis heisst nicht 'kein Schutz'."""
        st = self._state()
        ok, _ = pcl._pruefe_nachtabschaltung(
            st, datetime(2026, 9, 28, 21, 30), timedelta(0), "Regel"
        )
        assert ok is False


# ============================================================
# Prioritaeten-Kaskade (glatt): 110/100/90/85/78/75
# ============================================================
class TestPrioritaetenKaskade:
    def test_defaults_glatte_kaskade(self):
        assert NotfallschutzConfig().prioritaet == 110
        assert WochenendeConfig().prioritaet == 100
        assert LegionellenConfig().prioritaet == 90
        assert EinspeisungConfig().prioritaet == 85
        assert AdaptivePVConfig().prioritaet == 78
        assert PVRegel(name="PV_x").prioritaet == 78      # Backup-Position
        assert BatterieConfig().prioritaet == 75

    def test_json_kaskade(self):
        """wp_steuerung_parameter.json muss die gleiche Kaskade enthalten."""
        path = os.path.join(os.path.dirname(__file__), "..", "wp_steuerung_parameter.json")
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        assert d["notfallschutz"]["prioritaet"] == 110
        assert d["wochenende"]["prioritaet"] == 100
        assert d["legionellen"]["prioritaet"] == 90
        assert d["einspeisung"]["prioritaet"] == 85
        assert d["adaptive_pv"]["prioritaet"] == 78
        assert all(r["prioritaet"] == 78 for r in d["pv_regeln"])
        assert d["batterie"]["prioritaet"] == 75

    def test_kaskade_ohne_ueberlappung_mindesttemp_komfort(self):
        """MindestTemp(65)/Komfort(60)/Forecast(57)/Zeitfenster(53)/Abweichung(47)
        muessen weiterhin UNTER den glatten Stufen liegen."""
        c = WPSteuerungConfig()
        assert c.mindest_temp.prioritaet == 65
        assert c.komfort.prioritaet == 60
        assert c.forecast.prioritaet == 57
        assert c.zeitfenster.prioritaet == 53
        assert c.abweichung.prioritaet == 47
        assert c.calculated_start.prioritaet == 82  # bleibt ueber AdaptivePV (78)


# ============================================================
# evaluate_notfallschutz
# ============================================================
class TestTagesCutoff:
    """Nutzerwunsch 28.09.2026: der Notfall laeuft spaetestens bis 22:00."""

    def test_vor_cutoff_startet_der_notfall_wie_gewohnt(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 30.0, "mittig": 30.0, "unten": 30.0},
            now_hour=20,
        )
        assert erg.einschalten is True
        assert "NOTFALLSCHUTZ" in erg.grund

    def test_nach_cutoff_kein_notfallstart(self):
        """Nach 22:00 mit WARMEM Speicher: kein Start.

        Der Cutoff ist ein LAUFZEIT-Wunsch ("der Notfall laeuft spaetestens
        bis 22:00"). Er gilt weiterhin uneingeschraenkt fuer das BEENDEN
        eines laufenden Notfalls (siehe Nachbartests) und dafuer sorgen,
        dass die Hysterese nachts keinen Endlauf erhaelt. Bei bereits
        ausreichend warmem Speicher wird kein neuer Notfalllauf eroeffnet.
        """
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 37.5, "mittig": 37.5, "unten": 37.5},
            now_hour=22,
        )
        assert erg.einschalten is None, "nach 22:00 darf nicht gestartet werden"
        assert "22:00" in erg.grund

    def test_nach_cutoff_untertemperatur_startet_trotzdem(self):
        """KRITISCH (Befund Regelanalyse 04.10.2026): der Cutoff darf kein
        Startverbot fuer Untertemperatur sein.

        Vorher lieferte die Regel nach dem Cutoff bedingungslos
        `einschalten = None`. Damit fiel sie aus `aktive_regeln` heraus
        (`if e.aktiv and e.einschalten is not None`) und Wochenende (Prio
        100, einschalten=False) gewann die Wahl. Nachgerechnet: Sonntag
        07:00 bei 18 C Speichertemperatur -> "Start gesperrt -> AUS".
        Der Prio-Wert 110 nuetzte nichts, weil die Regel gar nicht zur Wahl
        stand.
        """
        for stunde in (22, 23, 0, 3, 7):
            erg = pc.evaluate_notfallschutz(
                NotfallschutzConfig(bis_uhr=22),
                {"oben": 18.0, "mittig": 19.0, "unten": 17.0},
                now_hour=stunde,
            )
            assert erg.einschalten is True, (
                f"{stunde:02d}:00 bei 18 C muss den Notfall starten, "
                f"sonst blockiert die Wochenende-Sperre"
            )
            assert "Untertemperatur hat Vorrang" in erg.grund

    def test_der_eigene_notfalllauf_wird_beim_cutoff_beendet(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 34.0, "mittig": 34.0, "unten": 34.0},
            kompressor_ein=True, notfall_aktiv=True, now_hour=22,
        )
        assert erg.einschalten is False
        assert "Cutoff" in erg.grund

    def test_fremder_lauf_wird_nicht_abgebrochen(self):
        """Nach 22:00 darf der Notfallschutz (Prio 110) nicht auch PV- oder
        Boiler-Laeufe abwuergen, die gar nicht von ihm stammen.

        Der Speicher ist hier warm (34 C > 36 C Schwelle nicht unterschritten,
        aber auch kein Notfallfall) - entscheidend ist `notfall_aktiv=False`:
        der Schutzleiter hat nichts zu beenden und bleibt stumm.
        """
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 34.0, "mittig": 34.0, "unten": 34.0},
            kompressor_ein=True, notfall_aktiv=False, now_hour=23,
        )
        assert erg.einschalten is not False, "fremder Lauf wurde abgebrochen"

    def test_legionellenfahrt_ist_vom_cutoff_ausgenommen(self):
        """KRITISCH: Die Prophylaxe zielt auf 60 C und dauert bis zu
        `max_duration_hours`. Ein Cutoff um 22:00 wuerde sie sonst
        abschneiden, bevor sie ihr Ziel erreicht."""
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 45.0, "mittig": 45.0, "unten": 45.0},
            kompressor_ein=True, legionellen_aktiv=True, now_hour=23,
        )
        assert erg.einschalten is not False, "Legionellenlauf wurde gekappt"
        assert "Cutoff" not in erg.grund

    def test_nacht_ist_gesperrt(self):
        """'Spaetestens 22:00' heisst: 22:00-07:59 gesperrt - fuer das
        ERHALTEN eines laufenden Notfalls und fuer Starts bei warmem
        Speicher.

        Regression: Ein naiver Vergleich `hour >= 22` liess 03:00 wieder
        zu - die Nacht liegt naechtlich UEBER Mitternacht. Geprueft wird
        daher mit 37,5 C: ueber der Einschaltschwelle, aber kein
        Untertemperaturfall.
        """
        for stunde in (22, 23, 0, 3, 7):
            erg = pc.evaluate_notfallschutz(
                NotfallschutzConfig(bis_uhr=22),
                {"oben": 37.5, "mittig": 37.5, "unten": 37.5},
                now_hour=stunde,
            )
            assert erg.einschalten is None, (
                f"{stunde:02d}:00 ist gesperrt, startete aber"
            )

    def test_erste_stunde_nach_der_nacht_ist_wieder_frei(self):
        for stunde in (8, 12, 21):
            erg = pc.evaluate_notfallschutz(
                NotfallschutzConfig(bis_uhr=22),
                {"oben": 30.0, "mittig": 30.0, "unten": 30.0},
                now_hour=stunde,
            )
            assert erg.einschalten is True, f"{stunde:02d}:00 sollte starten duerfen"

    def test_nachtlauf_wird_beendet(self):
        """Auch nachts muss ein eigener Notfalllauf enden (hier 03:00)."""
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(bis_uhr=22),
            {"oben": 34.0, "mittig": 34.0, "unten": 34.0},
            kompressor_ein=True, notfall_aktiv=True, now_hour=3,
        )
        assert erg.einschalten is False
        assert "Cutoff" in erg.grund

    def test_ohne_cutoff_unveraendert_ganze_nacht(self):
        """None/0 = alte Logik, der Notfall darf die ganze Nacht laufen."""
        for cfg in (NotfallschutzConfig(), NotfallschutzConfig(bis_uhr=0)):
            erg = pc.evaluate_notfallschutz(
                cfg, {"oben": 30.0, "mittig": 30.0, "unten": 30.0}, now_hour=3,
            )
            assert erg.einschalten is True, f"Cutoff wirkt unerwartet bei {cfg.bis_uhr}"


class TestEvaluateNotfallschutz:
    def test_unter_36_einschalten(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(), {"oben": 35.0, "mittig": 37.0, "unten": 38.0}
        )
        assert erg.aktiv is True
        assert erg.einschalten is True
        assert "NOTFALLSCHUTZ" in erg.grund

    def test_normalbetrieb_stumm(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(), {"oben": 45.0, "mittig": 44.0, "unten": 41.0}
        )
        assert erg.aktiv is True
        assert erg.einschalten is None  # kein Eingriff, blockt andere Regeln nie

    def test_fallback_mittig(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(), {"oben": None, "mittig": 35.5, "unten": 40.0}
        )
        assert erg.einschalten is True
        assert "mittig" in erg.grund

    def test_fallback_unten(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(), {"oben": None, "mittig": None, "unten": 34.0}
        )
        assert erg.einschalten is True
        assert "unten" in erg.grund

    def test_konfigurierter_fuehler_unten(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(temperaturfuehler="unten"),
            {"oben": 35.0, "mittig": 36.0, "unten": 40.0},
        )
        assert erg.einschalten is None
        assert "unten" in erg.grund

    def test_alle_verwendet_kuehlsten_fuehler(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(temperaturfuehler="alle"),
            {"oben": 40.0, "mittig": 35.0, "unten": 38.0},
        )
        assert erg.einschalten is True
        assert "mittig" in erg.grund

    def test_deaktiviert(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(aktiv=False), {"oben": 35.0}
        )
        assert erg.aktiv is False
        assert erg.einschalten is None

    def test_auto_erster_gueltiger_fuehler_gewinnt(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(), {"oben": 40.0, "mittig": 35.0, "unten": 34.0}
        )
        assert erg.einschalten is None
        assert "oben" in erg.grund

    def test_hysterese_bekommt_laufenden_notfall_zuerst_wieder_aus(self):
        """Der EIGENE Notfall-Lauf (notfall_aktiv=True) wird per Hysterese
        beendet, sobald der Fühler die Ausschaltschwelle erreicht."""
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(),
            {"oben": 39.0, "mittig": 40.0, "unten": 41.0},
            kompressor_ein=True,
            notfall_aktiv=True,
        )
        assert erg.einschalten is False
        assert "Hysterese" in erg.grund

    def test_aktive_legionellenfahrt_wird_nicht_ueber_stop_notfall_beendet(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(),
            {"oben": 45.7, "mittig": 45.2, "unten": 38.0},
            kompressor_ein=True,
            legionellen_aktiv=True,
        )
        assert erg.einschalten is None
        assert "Legionellenfahrt" in erg.grund

    def test_echter_kaltnotfall_gewinnt_auch_waehrend_legionellenfahrt(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(),
            {"oben": 35.0, "mittig": 36.0, "unten": 36.0},
            kompressor_ein=True,
            legionellen_aktiv=True,
        )
        assert erg.einschalten is True
        assert "NOTFALLSCHUTZ" in erg.grund

    def test_hysterese_between_grenzen_haelt_laufenden_lauf(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(),
            {"oben": 37.0, "mittig": 38.0, "unten": 39.0},
            kompressor_ein=True,
            notfall_aktiv=True,
        )
        assert erg.einschalten is True
        assert "laeuft weiter" in erg.grund

        erg = pc.evaluate_notfallschutz(NotfallschutzConfig(), {})
        assert erg.aktiv is False

    def test_fremder_lauf_wird_nicht_von_der_hysterese_abgebrochen(self):
        """Regression Log 28.09.2026, 08:02: `oben` 57.8C, Komfort startet
        (Prio 60), der Notfallschutz (Prio 110) lieferte trotzdem
        "Hysterese -> AUS" und wuergte den fremden Start im nächsten 10-s-Takt
        wieder ab. Ohne notfall_aktiv bleibt der Schutzleiter stumm."""
        for temp_oben, erwartet in ((57.8, None), (39.0, None)):
            erg = pc.evaluate_notfallschutz(
                NotfallschutzConfig(),
                {"oben": temp_oben, "mittig": 40.0, "unten": 23.7},
                kompressor_ein=True,
                notfall_aktiv=False,   # Lauf kam von Komfort, nicht vom Notfall
            )
            assert erg.einschalten is erwartet
            assert "fremder Lauf" in erg.grund

    def test_echter_notfall_startet_und_beendet_ueber_hysterese(self):
        """Vollstaendiger Zyklus: Notfall schaltet ein, laeuft, und beendet
        sich ueber die Hysterese selbst - ohne eine andere Regel zu stoeren."""
        cfg = NotfallschutzConfig()
        temps = {"oben": 34.0, "mittig": 36.0, "unten": 38.0}

        # 1) Kalt -> Notfall schaltet ein
        ein = pc.evaluate_notfallschutz(cfg, temps)
        assert ein.einschalten is True and "NOTFALLSCHUTZ" in ein.grund

        # 2) Lauf laeuft, Fühler noch im Band -> weiter
        lauf = pc.evaluate_notfallschutz(cfg, {**temps, "oben": 37.0},
                                         kompressor_ein=True, notfall_aktiv=True)
        assert lauf.einschalten is True and "laeuft weiter" in lauf.grund

        # 3) Fühler ueber Ausschaltschwelle -> Notfall beendet selbst
        aus = pc.evaluate_notfallschutz(cfg, {**temps, "oben": 38.5},
                                        kompressor_ein=True, notfall_aktiv=True)
        assert aus.einschalten is False and "Hysterese" in aus.grund


# ============================================================
# Integration: greift ohnes Workaround vor allen Sperren
# ============================================================
class TestNotfallschutzPrioritaet:
    def _bewerte(self, now, temp):
        config = WPSteuerungConfig()
        config.calculated_start.aktiv = False
        return pc.bewerte_alle_regeln(
            config=config, temp_dict=temp, pv_leistung=0.0, kompressor_ein=False,
            now=now, forecast_wh_qm=None,
        )

    def test_schlaegt_wochenende_sperre(self):
        """Samstag 08:00, oben kalt -> Notfallschutz (110) gewinnt gegen die
        Wochenende-Sperre (100), obwohl beides EIN/AUS-Wuensche liefert."""
        gewinner, alle = self._bewerte(
            TZ.localize(datetime(2025, 6, 14, 8, 0)),  # Samstag
            {"oben": 35.0, "mittig": 37.0, "unten": 35.0},
        )
        assert gewinner is not None
        assert gewinner.name == "Notfallschutz"
        assert gewinner.einschalten is True

    def test_schlaegt_nachtsperre(self):
        """23:00 (Nachtsperre), oben kalt -> Notfallschutz feuert trotzdem."""
        gewinner, alle = self._bewerte(
            TZ.localize(datetime(2025, 6, 14, 23, 0)),  # Samstag Nacht
            {"oben": 34.0, "mittig": 36.0, "unten": 35.0},
        )
        assert gewinner is not None
        assert gewinner.name == "Notfallschutz"
        assert gewinner.einschalten is True

    def test_laufende_legionellen_regel_bleibt_wirksamer_gewinner(self):
        config = WPSteuerungConfig()
        config.legionellen.aktiv = True
        gewinner, alle = pc.bewerte_alle_regeln(
            config=config,
            temp_dict={"oben": 45.7, "mittig": 45.2, "unten": 35.8, "verd": 21.6},
            pv_leistung=7560.0,
            kompressor_ein=True,
            now=TZ.localize(datetime(2026, 9, 25, 13, 23)),
            legionellen_aktiv=True,
            legionellen_started_at=TZ.localize(datetime(2026, 9, 25, 13, 20)),
        )
        assert gewinner is not None
        assert gewinner.name == "Legionellen"
        assert gewinner.einschalten is True
        notfall = next(e for e in alle if e.name == "Notfallschutz")
        assert notfall.einschalten is None

    def test_normalbetrieb_blockt_nicht(self):
        """Warmes Wasser: Notfallschutz stumm - andere Regeln koennen gewinnen."""
        gewinner, alle = self._bewerte(
            TZ.localize(datetime(2026, 1, 15, 12, 0)),
            {"oben": 45.0, "mittig": 44.0, "unten": 42.0},
        )
        assert gewinner is None or gewinner.name != "Notfallschutz"
        nf = next(e for e in alle if e.name == "Notfallschutz")
        assert nf.einschalten is None

    def test_notfallschutz_beendet_fremden_lauf_nicht(self):
        """Kaskaden-Regression (Log 28.09.2026 08:02, oben 57.8C): Ein laufender
        Kompressor, der NICHT vom Notfallschutz gestartet wurde, wird von der
        Notfall-Hysterese nicht mehr mit Prio 110 abgeschaltet - der Gewinner
        muss dann eine echte Heizregel sein und nicht 'Notfallschutz'."""
        config = WPSteuerungConfig()
        config.calculated_start.aktiv = False
        config.forecast.aktiv = False
        config.adaptive_pv.aktiv = False
        gewinner, alle = pc.bewerte_alle_regeln(
            config=config,
            temp_dict={"oben": 57.8, "mittig": 40.0, "unten": 23.7},
            pv_leistung=200.0,
            kompressor_ein=True,      # Kompressor laeuft bereits
            now=TZ.localize(datetime(2026, 9, 28, 8, 2)),
            notfall_aktiv=False,      # ... gestartet von einer anderen Regel
        )
        nf = next(e for e in alle if e.name == "Notfallschutz")
        assert nf.einschalten is None
        assert "fremder Lauf" in nf.grund
        # Vor dem Fix war hier "Notfallschutz" mit einschalten=False der Gewinner.
        assert gewinner is None or gewinner.name != "Notfallschutz"

    def test_notfallschutz_darf_eigenen_lauf_beenden(self):
        """Gegenprobe: Ein echter Notfall-Lauf wird weiterhin per Hysterese
        beendet - der Schutzleiter verliert seine Abschaltfunktion nicht."""
        config = WPSteuerungConfig()
        config.calculated_start.aktiv = False
        config.forecast.aktiv = False
        config.adaptive_pv.aktiv = False
        gewinner, alle = pc.bewerte_alle_regeln(
            config=config,
            temp_dict={"oben": 40.0, "mittig": 41.0, "unten": 42.0},
            pv_leistung=0.0,
            kompressor_ein=True,
            now=TZ.localize(datetime(2026, 9, 28, 8, 2)),
            notfall_aktiv=True,
        )
        nf = next(e for e in alle if e.name == "Notfallschutz")
        assert nf.einschalten is False
        assert "Hysterese" in nf.grund
        assert gewinner is not None and gewinner.name == "Notfallschutz"


class TestNotfallschutzExtract:
    def test_extract_ein_aus(self):
        config = WPSteuerungConfig()
        ergebnis = pc.RegelErgebnis(
            name="Notfallschutz", prioritaet=110, aktiv=True, einschalten=True
        )
        assert pcl._extract_einschaltpunkt(ergebnis, config) == 36.0
        assert pcl._extract_ausschaltpunkt(ergebnis, config) == 38.0

    def test_validator_verwirft_ein_ueber_aus(self):
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            NotfallschutzConfig(einschalten_bei_c=40.0, ausschalten_bei_c=38.0)


# ===========================================================
# Legionellen-Ausnahme in der Notfallschutz-Hysterese
# ===========================================================

class TestNotfallschutzLegionelle:
    """Regression aus dem Laufzeitlog 18.09.-01.10.2026.

    Der Notfallschutz hat Prioritaet 110 und damit VOR der Legionelle (90).
    Am 25.09.2026 beendete seine 38-C-Hysterese zwei Anlaufversuche der
    Prophylaxe bei oben 45,7 C, lange vor dem 60-C-Hygieneziel:

        10:23  Legionellenprophylaxe faellig: Starte mit PV auf 60C
        10:25  Notfallschutz-Hysterese: oben 45.7C >= 38.0C -> AUS
    """

    def _cfg(self):
        return NotfallschutzConfig(aktiv=True, prioritaet=110,
                                   einschalten_bei_c=36.0, ausschalten_bei_c=38.0)

    def test_ohne_legionelle_unveraendert(self):
        """Normalbetrieb: die 38-C-Hysterese gilt unveraendert."""
        erg = pc.evaluate_notfallschutz(
            self._cfg(), {"oben": 45.7}, kompressor_ein=True,
            notfall_aktiv=True, now_hour=12,
        )
        assert erg.einschalten is False
        assert "38.0C" in erg.grund
        assert "Legionellenfahrt" not in erg.grund

    def test_legionelle_hebt_die_hysterese_an(self):
        """Bei 45,7 C darf der Notfall die Fahrt nicht mehr beenden."""
        erg = pc.evaluate_notfallschutz(
            self._cfg(), {"oben": 45.7}, kompressor_ein=True,
            legionellen_aktiv=True, legionellen_max_c=65.0,
            notfall_aktiv=True, now_hour=12,
        )
        assert erg.einschalten is not False, (
            f"Notfallschutz hat die Legionellenfahrt abgebrochen: {erg.grund}"
        )

    def test_legionelle_bleibt_bis_zum_limit_wirksam(self):
        """Der Notfallschutz beendet eine laufende Fahrt gar nicht erst.

        Die harte 65-C-Grenze setzt bewusst NICHT der Notfallschutz: solange
        `legionellen_aktiv` gilt, gibt er bei zu warmem `oben` bewusst
        `einschalten=None` ab (siehe Guard in evaluate_notfallschutz). Die
        obere Grenze wahrt stattdessen die Ueberhitzungssicherung in
        safety_logic.py. Dieser Test haelt fest, dass der Schutzleiter
        waehrend der Fahrt nicht eingreift.
        """
        for temp in (45.0, 55.0, 64.0, 65.5):
            erg = pc.evaluate_notfallschutz(
                self._cfg(), {"oben": temp}, kompressor_ein=True,
                legionellen_aktiv=True, legionellen_max_c=65.0,
                notfall_aktiv=True, now_hour=12,
            )
            assert erg.einschalten is not False, (
                f"Notfallschutz beendete die Fahrt bei {temp} C: {erg.grund}"
            )

    def test_angekuendigte_fahrt_wird_nicht_ausgewaergt(self):
        """Der reale 25.09.-Fall: Fahrt angekuendigt, aber noch nicht aktiv.

        `legionellen_aktiv` ist in diesem Fenster noch False - der Start
        wurde erst nach der Hardware-Bestaetigung gesetzt. Genau hier hat
        die 38-C-Hysterese (Prio 110 > 90) den Start abgebrochen.
        """
        erg = pc.evaluate_notfallschutz(
            self._cfg(), {"oben": 45.7}, kompressor_ein=True,
            legionellen_aktiv=False, legionellen_angekuendigt=True,
            notfall_aktiv=True, now_hour=12,
        )
        assert erg.einschalten is not False, (
            f"Prophylaxe-Start abgewaergt: {erg.grund}"
        )
        assert "angekuendigte" in erg.grund

    def test_ohne_legelle_gilt_38c_unveraendert(self):
        """Weder aktiv noch angekuendigt: das alte Verhalten gilt."""
        erg = pc.evaluate_notfallschutz(
            self._cfg(), {"oben": 45.0}, kompressor_ein=True,
            legionellen_aktiv=False, legionellen_angekuendigt=False,
            notfall_aktiv=True, now_hour=12,
        )
        assert erg.einschalten is False and "38.0C" in erg.grund