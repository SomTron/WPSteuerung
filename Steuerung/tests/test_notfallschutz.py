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

    def test_deaktiviert(self):
        erg = pc.evaluate_notfallschutz(
            NotfallschutzConfig(aktiv=False), {"oben": 35.0}
        )
        assert erg.aktiv is False
        assert erg.einschalten is None

    def test_keine_sensordaten(self):
        erg = pc.evaluate_notfallschutz(NotfallschutzConfig(), {})
        assert erg.aktiv is False


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

    def test_normalbetrieb_blockt_nicht(self):
        """Warmes Wasser: Notfallschutz stumm - andere Regeln koennen gewinnen."""
        gewinner, alle = self._bewerte(
            TZ.localize(datetime(2026, 1, 15, 12, 0)),
            {"oben": 45.0, "mittig": 44.0, "unten": 42.0},
        )
        assert gewinner is None or gewinner.name != "Notfallschutz"
        nf = next(e for e in alle if e.name == "Notfallschutz")
        assert nf.einschalten is None


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