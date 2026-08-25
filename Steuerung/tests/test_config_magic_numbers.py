"""Tests: Magic Numbers wanderten in die JSON-Config (Design-Fix #6).

- Bademodus-Erhoehung (+3K) -> bademodus.solltemperatur_erhoehung_c
- AdaptivePV-Prognose-Schwellen (4000/1000 Wh/qm) -> adaptive_pv.fc_schwelle_*
- Wochenende-Prioritaet (100) -> wochenende.prioritaet
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig, AdaptivePVConfig, WochenendeConfig  # noqa: E402
from priority_control import evaluate_adaptive_pv, evaluate_wochenende  # noqa: E402
import priority_control_logic as pcl  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ── Wochenende-Prioritaet ──

def test_wochenende_prio_default_100():
    config = WPSteuerungConfig()
    assert config.wochenende.prioritaet == 100

    samstag_frueh = TZ.localize(datetime(2025, 6, 14, 8, 0))
    ergebnis = evaluate_wochenende(config.wochenende, samstag_frueh)
    assert ergebnis.prioritaet == 100
    assert ergebnis.einschalten is False  # vor 9 Uhr blockiert


def test_wochenende_prio_aus_config():
    config = SimpleNamespace(wochenende=WochenendeConfig(
        aktiv=True, fruehestens_uhr=9, prioritaet=77,
    ))
    samstag_frueh = TZ.localize(datetime(2025, 6, 14, 8, 0))

    block = evaluate_wochenende(config.wochenende, samstag_frueh)
    assert block.prioritaet == 77
    assert block.einschalten is False

    erlaubt = evaluate_wochenende(config.wochenende, TZ.localize(datetime(2025, 6, 14, 10, 0)))
    assert erlaubt.prioritaet == 77

    inaktiv = evaluate_wochenende(
        WochenendeConfig(aktiv=False, fruehestens_uhr=9, prioritaet=77),
        samstag_frueh,
    )
    assert inaktiv.prioritaet == 77

    kein_wochenende = evaluate_wochenende(config.wochenende, TZ.localize(datetime(2025, 6, 11, 8, 0)))
    assert kein_wochenende.prioritaet == 77


# ── AdaptivePV-Prognose-Schwellen ──

def _adaptive_grund(forecast, adaptive_cfg=None):
    cfg = adaptive_cfg or AdaptivePVConfig()
    ergebnis = evaluate_adaptive_pv(
        cfg,
        # 40C: ueber beiden Kalt-Schwellen (35/38) -> kein Temperatur-Faktor,
        # die Grund-Schwelle 300W bleibt direkt sichtbar
        temp_dict={"unten": 40.0, "mitte": 42.0, "oben": 44.0},
        pv_leistung=0.0,  # unter jeder Schwelle -> Grund enthaelt die Schwelle
        forecast_wh_qm=forecast,
        kompressor_ein=False,
        now_hour=12,
    )
    return ergebnis.grund


def test_adaptive_pv_schwellen_default():
    assert AdaptivePVConfig().fc_schwelle_gut_wh == 4000.0
    assert AdaptivePVConfig().fc_schwelle_schlecht_wh == 1000.0

    # 4001 Wh/qm = "guter Tag" -> Schwelle x1.5 (300 -> 450 W)
    assert "< 450W" in _adaptive_grund(4001)
    # 3999 Wh/qm = neutral -> Basis-Schwelle 300 W
    assert "< 300W" in _adaptive_grund(3999)
    # 999 Wh/qm = "schlechter Tag" -> Schwelle x0.5 (300 -> 150 W)
    assert "< 150W" in _adaptive_grund(999)
    # 1001 Wh/qm = neutral -> Basis-Schwelle 300 W
    assert "< 300W" in _adaptive_grund(1001)


def test_adaptive_pv_schwellen_konfigurierbar():
    cfg = AdaptivePVConfig(fc_schwelle_gut_wh=2000.0, fc_schwelle_schlecht_wh=500.0)
    # 2500 liegt jetzt im "guten" Bereich -> x1.5
    assert "< 450W" in _adaptive_grund(2500, cfg)
    # 400 waere jetzt "schlecht" -> x0.5
    assert "< 150W" in _adaptive_grund(400, cfg)


# ── Bademodus-Erhoehung ──

def _baue_state(bademodus, erhoehung):
    config = WPSteuerungConfig()
    config.bademodus.solltemperatur_erhoehung_c = erhoehung
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=config,
        bademodus_aktiv=bademodus,
        urlaubsmodus_aktiv=False,
        sensors=SimpleNamespace(t_oben=40.0, t_unten=41.0, t_mittig=42.0, t_verd=30.0),
        solar=SimpleNamespace(
            # PV als Quelle fuer das Abweichungs-Gate, aber SOC niedrig, damit
            # die Batterie-Regel (Prio 75) nicht dazwischen gewinnt
            feedinpower=100, batpower=0, soc=0, forecast_today=None, forecast_tomorrow=None,
        ),
        control=SimpleNamespace(
            kompressor_ein=False,
            previous_modus="Normalmodus",
            aktueller_einschaltpunkt=None,
            aktueller_ausschaltpunkt=None,
            active_rule_name=None,
            active_rule_sensor=None,
            komfort_aktiv=False,
            alle_ergebnisse=[],
        ),
    )


@pytest.mark.asyncio
async def test_bademodus_erhoehung_aus_config():
    """Mit Erhoehung +5K gewinnt Abweichung bei t_unten=41 (Soll 40+5=45, -3K Hysterese)."""
    state = _baue_state(bademodus=True, erhoehung=5.0)

    with patch('priority_control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(datetime(2025, 6, 11, 18, 0))  # Mi, ausserhalb Fenster
        result = await pcl.determine_mode_and_setpoints(state, t_unten=41.0, t_mittig=42.0)

    gewinner = result["gewinner_ergebnis"]
    assert gewinner is not None and gewinner.name == "Abweichung"
    # einschaltpunkt = (40 + 5) - 3.0 = 42.0 statt 37.0 ohne Bademodus
    assert result["einschaltpunkt"] == 42.0


@pytest.mark.asyncio
async def test_ohne_bademodus_keine_erhoehung():
    """Ohne Bademodus bleibt der Sollwert bei 40C -> 41C loest kein EIN aus."""
    state = _baue_state(bademodus=False, erhoehung=5.0)

    with patch('priority_control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(datetime(2025, 6, 11, 18, 0))
        result = await pcl.determine_mode_and_setpoints(state, t_unten=41.0, t_mittig=42.0)

    gewinner = result["gewinner_ergebnis"]
    assert gewinner is not None and gewinner.name == "Abweichung"
    # Soll 40.0 - unten 41.0 = -1.0K: innerhalb der Hysterese, keine Heizanforderung
    assert gewinner.einschalten is False
    assert "Soll 40.0" in gewinner.grund
    # Gemeldete Setpoints folgen dem unveraenderten Soll (40C):
    # AUS-Zweig nimmt max(Ein-Punkt 37, Aus-Punkt 39.5) = 39.5 (kein Neueinschalten)
    assert result["einschaltpunkt"] == 39.5
