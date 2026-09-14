# -*- coding: utf-8 -*-
"""Tests: Kurzzvkal-Reduktion (Empfehlung 3.2).

A) Hysterese-Sparen bei guter HEUTE-Prognose in AdaptivePV (tiefer starten,
   laengere Lauefe)
B) PV-Weiterlauf-Band: Ein kurzer PV-Einbruch (Wolke) beendet einen laufenden
   PV-Zyklus fruehestens nach pv_weiterlauf_abschalt_delay_min.
"""
import sys
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from priority_control import evaluate_adaptive_pv  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ---------------------------------- A) Hysterese-Sparen -------------------- #
def _adp_cfg(**overrides):
    cfg = SimpleNamespace(
        aktiv=True, prioritaet=78, temperaturfuehler="unten",
        base_threshold_watt=300.0, fc_schwelle_gut_wh=4000.0,
        fc_schwelle_schlecht_wh=1000.0, tmax_c=48.0,
        t_aggressiv_kalt_c=30.0, t_normal_kalt_c=35.0,
        einschalten_bis_c=45.0, sparen_hysterese_k=1.0,
        hysterese_forecast_schwelle_wh_qm=2500.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _adp(unten=44.5, forecast_today=None, pv=200.0, cfg=None):
    return evaluate_adaptive_pv(
        cfg or _adp_cfg(),
        {"oben": 45.0, "mittig": 44.0, "unten": unten},
        pv, None, False, 12, 19, 8,
        forecast_today_wh_qm=forecast_today,
    )


class TestHystereseSparen:
    def test_gute_prognose_senkt_einschaltgrenze(self):
        """5.34 kWh heute -> Grenze 45-1=44, unten 44.5 -> noch warten
        (sonst wuerde die WP oben-nahe starten und am Limit takten)."""
        erg = _adp(unten=44.5, forecast_today=5.34)
        assert erg.einschalten is None
        assert "Einschaltgrenze" in erg.grund

    def test_ohne_forecast_kein_sparen(self):
        """Ohne Prognose bleibt die gewohnte Grenze (kein Einschaltgrenzen-Block)."""
        erg = _adp(unten=44.5, forecast_today=None)
        assert "Einschaltgrenze" not in erg.grund
        assert "PV " in erg.grund or "C" in erg.grund  # normaler Schwelle-Weg

    def test_sparen_absaltbar(self):
        erg = _adp(unten=44.5, forecast_today=5.34,
                   cfg=_adp_cfg(sparen_hysterese_k=0.0))
        assert "Einschaltgrenze" not in erg.grund


# ---------------------------------- B) PV-Weiterlauf ---------------------- #
def _block_state(delay=3.0, kompressor=True):
    return SimpleNamespace(
        local_tz=TZ,
        control=SimpleNamespace(kompressor_ein=kompressor,
                                _pv_abschaltwunsch_seit=None),
        priority_config=SimpleNamespace(
            zyklus=SimpleNamespace(pv_weiterlauf_abschalt_delay_min=delay)),
    )


def _gewinner(name="AdaptivePV", grund="AdaptivePV: PV 0W < 105W"):
    return SimpleNamespace(name=name, grund=grund)


class TestPvWeiterlaufBand:
    def test_erkennt_pv_unterbrechung(self):
        assert pcl._ist_pv_unterbrechung("AdaptivePV: PV 0W < 105W") is True
        assert pcl._ist_pv_unterbrechung(
            "AdaptivePV: unten 48.0C >= 48.0C -> AUS") is False

    def test_erster_pv_einbruch_laeuft_weiter(self):
        state = _block_state()
        out = pcl._pv_weiterlauf_block(state, _gewinner(), False)
        assert out is True                  # AUS-Wunsch unterdrueckt
        assert state.control._pv_abschaltwunsch_seit is not None

    def test_kurzer_einbruch_halt_weiter(self):
        state = _block_state(delay=3.0)
        assert pcl._pv_weiterlauf_block(state, _gewinner(), False) is True
        assert pcl._pv_weiterlauf_block(state, _gewinner(), False) is True

    def test_nach_delay_wird_aus_freigegeben(self):
        state = _block_state(delay=3.0)
        assert pcl._pv_weiterlauf_block(state, _gewinner(), False) is True
        state.control._pv_abschaltwunsch_seit = \
            datetime.now(TZ) - timedelta(minutes=5)
        out = pcl._pv_weiterlauf_block(state, _gewinner(), False)
        assert out is False
        assert state.control._pv_abschaltwunsch_seit is None

    def test_nur_fuer_pv_regeln_und_pv_gruende(self):
        state = _block_state()
        # Nicht-PV-Regel mit PV-grund-artigem Text
        assert pcl._pv_weiterlauf_block(
            state, _gewinner(name="Abweichung"), False) is False
        # PV-Regel, aber Temperatur-AUS (Limit)
        assert pcl._pv_weiterlauf_block(
            state, _gewinner(grund="AdaptivePV: unten 48.0C >= 48.0C -> AUS"),
            False) is False

    def test_deaktivierbar_mit_delay_0(self):
        state = _block_state(delay=0.0)
        assert pcl._pv_weiterlauf_block(state, _gewinner(), False) is False
        assert state.control._pv_abschaltwunsch_seit is None

    def test_einbruch_ende_setzt_zustand_zurueck(self):
        state = _block_state()
        state.control._pv_abschaltwunsch_seit = datetime.now(TZ)
        # Kein AUS-Wunsch mehr (should_on=True) -> Zustand zurueck
        assert pcl._pv_weiterlauf_block(state, _gewinner(), True) is True
        assert state.control._pv_abschaltwunsch_seit is None