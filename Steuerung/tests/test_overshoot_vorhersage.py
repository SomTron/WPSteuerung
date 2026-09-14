# -*- coding: utf-8 -*-
"""Tests: Vorausschauende Overshoot-Vermeidung (Empfehlung 3.1).

- Rate-Helper _rate_fuer_entscheidung (Messung + Fallbacks)
- AUS-Vorhersage: Abschaltung BEVOR die Mindestlaufzeit den Fuehler ueber
  die Obergrenze treibt (nur wenn die Regel den Abschaltpunkt erreicht hat)
- EIN-Antizipation (in test_boiler_max.py) ergaenzt das Bild
"""
import sys
import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def _state(t_unten=48.0, lauf_min=4, mz_abstand_min=6, rate_diff_k=None):
    state = SimpleNamespace(
        local_tz=TZ,
        priority_config=WPSteuerungConfig(),
        control=SimpleNamespace(
            kompressor_ein=True, blocking_reason=None,
            _soll_einschalten=False, zyklus_id=7,
            _lauf_start_regel="AdaptivePV",
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=datetime.now(TZ) - timedelta(minutes=lauf_min)),
        sensors=SimpleNamespace(t_unten=t_unten, t_oben=50.0,
                                t_mittig=48.0, t_verd=12.0),
        solar=SimpleNamespace(feedinpower=5000.0, soc=80.0),
    )
    if rate_diff_k is not None:
        state.control._rate_messung = {
            "ts": datetime.now(TZ) - timedelta(minutes=mz_abstand_min),
            "unten": t_unten - rate_diff_k,
        }
    return state


class TestRateHelper:
    def test_faellt_auf_rate_fallback_ohne_messung(self):
        state = _state()
        assert pcl._rate_fuer_entscheidung(state, None) == \
            state.priority_config.sicherheit.rate_fallback_c_h

    def test_misst_live_rate_aus_messpunkt(self):
        state = _state(t_unten=45.9, rate_diff_k=2.4)  # 2.4K / 0.1h = 24 C/h
        rate = pcl._rate_fuer_entscheidung(state, 45.9)
        assert abs(rate - 24.0) < 1.0

    def test_aktualisiert_messpunkt(self):
        state = _state()
        pcl._rate_fuer_entscheidung(state, 46.0)
        assert state.control._rate_messung["unten"] == 46.0


class TestAusVorhersage:
    @pytest.mark.asyncio
    async def test_schaltet_vor_der_obergrenze_ab(self):
        """Regel-AUS ab 47.5 (ausschaltpunkt < hartes Limit 48), Restlaufzeit
        6 min, Rate 24 C/h: prognose 47.5 + 2.4 = 49.9 >= 48 - 0.8 -> sofort AUS."""
        state = _state(t_unten=47.5, rate_diff_k=2.4)
        calls = []

        async def set_status(s, ein, **kw):
            calls.append(ein)
            return True

        erg = await pcl.handle_compressor_off(
            state, None, regelfuehler=47.5, ausschaltpunkt=47.5,
            min_laufzeit=timedelta(minutes=10), t_oben=50.0,
            set_kompressor_status_func=set_status, regel_name=None,
        )
        assert erg is True
        assert calls == [False]
        assert "Overshoot-Vorhersage" in (state.control.blocking_reason or "")

    @pytest.mark.asyncio
    async def test_kein_aus_unterhalb_abschaltpunkt(self):
        """t_max 47.0 < ausschaltpunkt 48 -> normale Heizung laeuft weiter."""
        state = _state(t_unten=47.0, rate_diff_k=2.4)
        calls = []

        async def set_status(s, ein, **kw):
            calls.append(ein)
            return True

        erg = await pcl.handle_compressor_off(
            state, None, regelfuehler=47.0, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=10), t_oben=50.0,
            set_kompressor_status_func=set_status, regel_name=None,
        )
        assert erg is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_deaktivierbar_ueber_config(self):
        state = _state(t_unten=48.0, rate_diff_k=2.4)
        state.priority_config.sicherheit.overshoot_vorhersage_aktiv = False
        calls = []

        async def set_status(s, ein, **kw):
            calls.append(ein)
            return True

        erg = await pcl.handle_compressor_off(
            state, None, regelfuehler=48.0, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=10), t_oben=50.0,
            set_kompressor_status_func=set_status, regel_name=None,
        )
        # Deaktiviert: der reguläre Regel-AUS (Mindestlaufzeit erreicht)
        # sei hier nicht gefragt - nur wichtig: KEINE Vorhersage-Abschaltung.
        assert not any("Overshoot-Vorhersage" in (r or "")
                       for r in [state.control.blocking_reason])