"""Tests: CalcStart x Nachtsperre-Konflikt (Bugfix #3).

Vorher: Eine Zielzeit innerhalb/hinter der Nachtsperre machte die Regel
STUMM - sie feuerte nie, ohne dass irgendwo eine Warnung erschien.
"""
import os
import sys
from types import SimpleNamespace
import logging

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from priority_control import (  # noqa: E402
    calcstart_nachtsperre_konflikt,
    evaluate_calculated_start,
)
from priority_control_logic import determine_mode_and_setpoints  # noqa: E402
from json_config import CalculatedStartConfig, WPSteuerungConfig  # noqa: E402


# ── Unit-Tests der reinen Prueffunktion ─────────────────────────────────

def test_zielzeit_innerhalb_nachtsperre_ist_tot():
    """target=6 Uhr, Sperre 19-8: Regel kann nie feuern -> tot."""
    tot, letzte, hinweis = calcstart_nachtsperre_konflikt(6.0, 19, 8)
    assert tot is True
    assert letzte is None
    assert "nie aktiv" in hinweis


def test_zielzeit_vor_nachtsperre_ist_gesund():
    """target=17 Uhr, Sperre 19-8 (Default): kein Konflikt, kein Hinweis."""
    tot, letzte, hinweis = calcstart_nachtsperre_konflikt(17.0, 19, 8)
    assert tot is False
    assert hinweis == ""
    assert letzte == 16  # letzte volle Stunde vor Ziel


def test_zielzeit_hinter_nachtsperre_wird_abgekappt():
    """target=20 Uhr, Sperre ab 19: Vorheizen wird um 19 Uhr abgeschnitten."""
    tot, letzte, hinweis = calcstart_nachtsperre_konflikt(20.0, 19, 8)
    assert tot is False
    assert letzte == 18
    assert "abgeschnitten" in hinweis


def test_mittagssperre_kappt_vormittags_heizen_ab():
    """Nicht-ueber-Mitternacht-Sperre (8-14): target=10 -> ab 8 Uhr kaputt."""
    tot, _, hinweis = calcstart_nachtsperre_konflikt(10.0, 8, 14)
    assert tot is False
    assert "abgeschnitten" in hinweis


def test_leere_sperre_macht_nie_tot():
    """start==ende -> Sperre wirkungslos: jede Zielzeit erreichbar."""
    tot, _, hinweis = calcstart_nachtsperre_konflikt(6.0, 8, 8)
    assert tot is False
    assert hinweis == ""


# ── Regel-Annotation: Grund-Text bei Nachtsperre ────────────────────────

def test_regel_grund_warnt_bei_toter_zielzeit():
    cfg = CalculatedStartConfig(target_uhr=6)  # Default-Sperre 19-8
    res = evaluate_calculated_start(cfg, {"unten": 30.0, "mitte": 35.0}, now_hour=22,
                                    now_minute=0, nachtsperre_start=19, nachtsperre_ende=8)
    assert res.aktiv is False
    assert "Nachtsperre aktiv" in res.grund
    assert "nie aktiv" in res.grund


def test_regel_grund_ohne_konflikt_bleibt_kurz():
    cfg = CalculatedStartConfig(target_uhr=17)  # gesunde Default-Kombi
    res = evaluate_calculated_start(cfg, {"unten": 30.0, "mitte": 35.0}, now_hour=22,
                                    now_minute=0, nachtsperre_start=19, nachtsperre_ende=8)
    assert res.grund == "Nachtsperre aktiv"


# ── Integration: pcl warnt einmalig pro Konfigurationsstand ─────────────

class FakeLearningEngine:
    """Lern-Engine mit steuerbarem Ziel-Stundenwert."""
    ziel = None

    def update(self, **kwargs):
        pass

    def get_learned_heating_rate(self, monat, sensor):
        return 3.0

    def get_learned_target_hour(self):
        return type(self).ziel


def baue_pcl_state():
    tz = pytz.timezone("Europe/Berlin")
    return SimpleNamespace(
        local_tz=tz,
        priority_config=WPSteuerungConfig(),
        sensors=SimpleNamespace(t_oben=45.0, t_unten=40.0, t_mittig=42.0, t_verd=25.0),
        control=SimpleNamespace(
            kompressor_ein=False, previous_modus=None,
            aktueller_einschaltpunkt=44.0, aktueller_ausschaltpunkt=48.0,
            active_rule_name=None, active_rule_sensor=None,
            komfort_aktiv=False, alle_ergebnisse=[], _soll_einschalten=False,
        ),
        solar=SimpleNamespace(feedinpower=0.0, forecast_today=None, forecast_tomorrow=None),
        config=SimpleNamespace(Urlaubsmodus=SimpleNamespace(URLAUBSABSENKUNG=5.0)),
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
    )


@pytest.mark.asyncio
async def test_pcl_warnt_bei_toter_calcstart_config(caplog):
    """Config-Zielzeit 6 Uhr + Sperre bis 8: ERROR-Warnung muss erscheinen."""
    state = baue_pcl_state()
    state.priority_config.calculated_start.target_uhr = 6

    with caplog.at_level(logging.ERROR):
        await determine_mode_and_setpoints(state, 40.0, 42.0)

    assert any("nie aktiv werden" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pcl_keine_warnung_bei_gesunder_config(caplog):
    """Default-Zielzeit 17 Uhr: keine CalcStart-Warnung."""
    state = baue_pcl_state()

    with caplog.at_level(logging.WARNING):
        await determine_mode_and_setpoints(state, 40.0, 42.0)

    assert not any("CalcStart" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_pcl_gelernte_zielzeit_loest_neue_warnung_aus(caplog):
    """Lern-Engine schiebt Ziel in die Sperre -> Warnung; stabil -> keine Wiederholung."""
    state = baue_pcl_state()
    FakeLearningEngine.ziel = 21.0  # hinter Nachtsperren-Beginn (19)

    with caplog.at_level(logging.WARNING):
        await determine_mode_and_setpoints(state, 40.0, 42.0, learning_engine=FakeLearningEngine())
        erste = sum(1 for r in caplog.records if "CalcStart" in r.message)
        assert erste >= 1

        await determine_mode_and_setpoints(state, 40.0, 42.0, learning_engine=FakeLearningEngine())
        zweite = sum(1 for r in caplog.records if "CalcStart" in r.message)
        assert zweite == erste  # gleiche Signatur -> gedrosselt

        FakeLearningEngine.ziel = 22.5  # Lernwert aendert sich -> neu warnen
        await determine_mode_and_setpoints(state, 40.0, 42.0, learning_engine=FakeLearningEngine())
        dritte = sum(1 for r in caplog.records if "CalcStart" in r.message)
        assert dritte > zweite
