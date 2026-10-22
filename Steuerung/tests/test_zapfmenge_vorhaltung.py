"""Zapfungsmenge und Vorhaltung (BUGFIX 22.10.2026).

Aus der Live-Auswertung (Entscheidungslog 09.-15.10.2026): nach einer
Zapfung fiel der untere Fuehler von 43,2 auf 24,3 C, waehrend Mitte
(48 C) und Oben (48 C) unveraendert blieben. Das ist keine Abkuehlung,
sondern entnommenes Wasser - der Speicher war danach von unten kalt.

Die WP kann das nicht rueckgaengig machen: sie fuelt den Speicher von
oben, und oben ist bereits voll (Boiler-Max 48 C). Also muss die
Reserve VOR der Zapfung im Speicher liegen.

Diese Tests decken ab:
  1) Die Zapfungsmenge wird aus dem Temperaturabfall geschaetzt.
  2) Sie wird gelernt (Mittel + Peak).
  3) Die Vorhaltung erhoeht den SOLLwert (nicht die Zielzeit) und
     verschiebt damit den Start sinnvoll frueher.
  4) Die Verdrahtung von der Lernengine bis zum CalcStart.
"""
from datetime import datetime, timedelta

import pytest

from learning_engine import LearningEngine
from priority_control import evaluate_calculated_start
from json_config import CalculatedStartConfig


def _engine(tmp_path):
    return LearningEngine(data_path=str(tmp_path / "lern.json"))


def _zapfe(engine, start, unten_vor, unten_nach, mitte_vor=47.0, mitte_nach=46.5,
          kompressor=False):
    """Ein steiler Abfall + Ruhe-Sample: erzeugt genau EIN Ereignis."""
    engine.update(start, {"unten": unten_vor, "mittig": mitte_vor, "oben": 47.5},
                  kompressor)
    engine.update(start + timedelta(minutes=1),
                  {"unten": unten_nach, "mittig": mitte_nach, "oben": 46.5},
                  kompressor)
    engine.update(start + timedelta(minutes=8),
                  {"unten": unten_nach, "mittig": mitte_nach, "oben": 46.5},
                  kompressor)


class TestZapfmenge:
    """Die Menge wird aus dem Temperaturabfall geschaetzt."""

    def test_menge_wird_geschaetzt(self, tmp_path):
        """16 K Abfall bei 300 l -> eine plausible Menge."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        assert len(eng.data.usage_events) == 1
        menge = eng.data.usage_events[0]["zapfmenge_l"]
        assert menge > 0.0, "Die Zapfungsmenge wurde nicht berechnet"
        # 16 K Abfall, halber Speicher betroffen, 300 l:
        # Q = 16 * 300 * 1.163 * 0.5 / (1.163 * 45) ~ 53 l
        assert 20.0 < menge < 120.0, f"Unerwartete Menge: {menge} l"

    def test_menge_ist_ohne_volumen_null(self, tmp_path):
        """Ohne Speichervolumen ist keine Menge berechenbar."""
        eng = _engine(tmp_path)
        eng.data.config.speicher_volumen_l = 0.0
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        assert eng.data.usage_events[0]["zapfmenge_l"] == 0.0

    def test_menge_wird_gelernt(self, tmp_path):
        """Nach zwei Zapfungen ist die Menge verfuegbar."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        _zapfe(eng, datetime(2026, 10, 16, 18, 30), 43.0, 27.0)
        assert eng.data.zapfmenge_samples == 2
        assert eng.get_learned_zapfmenge_l() > 0.0
        assert eng.get_learned_zapfmenge_l(peak=True) > 0.0

    def test_peak_ist_der_groesste_wert(self, tmp_path):
        """Der Peak ist der schlimmste Fall - fuer die Vorhaltung relevant."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)   # gross
        _zapfe(eng, datetime(2026, 10, 16, 18, 30), 43.0, 35.0)   # klein
        _zapfe(eng, datetime(2026, 10, 17, 18, 30), 43.0, 35.0)
        assert eng.get_learned_zapfmenge_l(peak=True) >= \
            eng.get_learned_zapfmenge_l()

    def test_ohne_daten_keine_menge(self, tmp_path):
        """Ohne Zapfungen gibt es keine Menge - die Vorhaltung bleibt 0."""
        eng = _engine(tmp_path)
        assert eng.get_learned_zapfmenge_l() == 0.0
        assert eng.get_learned_zapfmenge_l(peak=True) == 0.0


class TestVorhaltungImCalcStart:
    """Die Vorhaltung erhoeht den SOLLwert und verschiebt den Start."""

    def _start(self, vorhaltung_k):
        cfg = CalculatedStartConfig()
        r = evaluate_calculated_start(
            calc_cfg=cfg,
            temp_dict={"unten": 30.0, "mittig": 35.0, "oben": 40.0},
            now_hour=10, now_minute=0,
            nachtsperre_start=19, nachtsperre_ende=8,
            forecast_wh_qm=2000.0,
            learned_heating_rate_unten=3.0,
            learned_heating_rate_gesamt=2.0,
            learned_target_hour=17.0,
            feedin_watt=0.0, soc=50.0, fc_ratio=1.0,
            surplus_profile=None,
            pv_acpower=0.0, battery_power=0.0, solar_stale=False,
            zapf_vorhaltung_k=vorhaltung_k,
        )
        return r.regel_dict or {}

    def test_ohne_vorhaltung_kein_effekt(self):
        d0 = self._start(0.0)
        assert d0["hours_needed"] == pytest.approx(4.0, abs=0.1)

    def test_vorhaltung_erhoeht_den_bedarf(self):
        """6 K Vorhaltung -> 2 K mehr Bedarf (bei 3 K/h Heizrate)."""
        d0 = self._start(0.0)
        d6 = self._start(6.0)
        assert d6["hours_needed"] > d0["hours_needed"] + 1.5, (
            f"Vorhaltung wirkt nicht auf den Bedarf: {d0} -> {d6}")

    def test_vorhaltung_verschiebt_den_start_frueher(self):
        """Der Start muss FRUEHER liegen, nicht nur der Puffer kleiner."""
        d0 = self._start(0.0)
        d6 = self._start(6.0)
        assert d6["planned_start_hour"] < d0["planned_start_hour"], (
            f"Start nicht frueher: {d0['planned_start_hour']} -> "
            f"{d6['planned_start_hour']}")

    def test_vorhaltung_ist_im_grundtext_nachvollziehbar(self):
        """Der Grund muss die Vorhaltung nennen."""
        cfg = CalculatedStartConfig()
        r = evaluate_calculated_start(
            calc_cfg=cfg,
            temp_dict={"unten": 30.0, "mittig": 35.0, "oben": 40.0},
            now_hour=10, now_minute=0,
            nachtsperre_start=19, nachtsperre_ende=8,
            forecast_wh_qm=2000.0,
            learned_heating_rate_unten=3.0,
            learned_heating_rate_gesamt=2.0,
            learned_target_hour=17.0,
            feedin_watt=0.0, soc=50.0, fc_ratio=1.0,
            surplus_profile=None,
            pv_acpower=0.0, battery_power=0.0, solar_stale=False,
            zapf_vorhaltung_k=4.0,
        )
        assert "Vorhaltung" in str(r.grund) or "4.0K" in str(r.grund), (
            "Die Vorhaltung muss im Grundtext stehen: " + str(r.grund))


class TestVerdrahtung:
    """Von der Lernengine bis zum CalcStart - die Leitung muss existieren."""

    def test_priority_control_logic_reicht_die_vorhaltung_weiter(self):
        import pathlib
        quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        assert "zapf_vorhaltung_k=zapf_vorhaltung_k" in text, (
            "Die Vorhaltung wird nicht an bewerte_alle_regeln gereicht")

    def test_bewerte_alle_regeln_hat_die_parameter(self):
        import inspect
        from priority_control import bewerte_alle_regeln
        params = inspect.signature(bewerte_alle_regeln).parameters
        assert "zapf_vorhaltung_k" in params
        assert "zapfmenge_l" in params

    def test_evaluate_calculated_start_hat_die_parameter(self):
        import inspect
        from priority_control import evaluate_calculated_start
        params = inspect.signature(evaluate_calculated_start).parameters
        assert "zapf_vorhaltung_k" in params

    def test_die_menge_wird_durchgereicht(self):
        """Die gelernte Menge muss bis zum CalcStart kommen - sie ist
        die Grundlage der Vorhaltung (und der Anzeige)."""
        import pathlib
        quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        assert "zapfmenge_l=gelernte_zapfmenge_l" in text, (
            "Die Zapfungsmenge wird nicht durchgereicht")

    def test_ohne_lernengine_keine_vorhaltung(self, tmp_path):
        """Ohne Lernengine (oder mit zu wenig Daten) bleibt alles neutral."""
        eng = _engine(tmp_path)
        assert eng.get_learned_zapfmenge_l() == 0.0
