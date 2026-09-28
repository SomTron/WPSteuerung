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
from datetime import datetime

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