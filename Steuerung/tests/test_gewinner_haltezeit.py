# -*- coding: utf-8 -*-
"""Tests: Mindesthaltezeit des Gewinners (D(c), 5 min).

Anlass: Im Bestandslog flatterte der Notfallschutz am 03.09.2026 40-mal in
30 min, weil ``oben`` zwischen 36.0 und 36.06C pendelte - der 2-Loop-Debounce
(~20 s) ist gegen dieses 0.06-K-Rauschen zu kurz. Ein einmal bestaetigter
Gewinner bleibt jetzt GEWINNER_MINDESTHALTE_MIN lang stehen, bevor ein ANDERER
Gewinner uebernehmen darf. Sicherheits-Abschaltungen (Boiler-Max, Ueberhitzung,
Not-Aus, Druck) liegen ausserhalb dieser Wahl und greifen unveraendert sofort.
"""
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


class _FakeClock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t


def _state(previous="Keine Regel aktiv", jetzt=None):
    cfg = WPSteuerungConfig()
    jetzt = jetzt or TZ.localize(datetime(2026, 1, 15, 12, 0))
    return SimpleNamespace(
        local_tz=TZ,
        clock=_FakeClock(jetzt),
        priority_config=cfg,
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=45.0, t_unten=42.0, t_mittig=43.0, t_verd=30.0),
        solar=SimpleNamespace(
            feedinpower=0.0, batpower=0.0, soc=80.0,
            forecast_today=None, forecast_tomorrow=None, forecast_day2=None,
            last_api_call=jetzt - timedelta(minutes=5),
        ),
        control=SimpleNamespace(
            kompressor_ein=False,
            previous_modus=previous,
            aktueller_einschaltpunkt=None,
            aktueller_ausschaltpunkt=None,
            active_rule_name=None,
            active_rule_sensor=None,
            komfort_aktiv=False,
            alle_ergebnisse=[],
            _soll_einschalten=False,
            _soll_einschalten_bestaetigt=False,
            _lauf_start_regel=None,
            _pending_modus=None,
            _wechsel_historie=deque(),
            _gewinner_bestaetigt_seit=None,
            restart_lockout_until=None,
            boiler_max_blockiert=None,
            blocking_reason=None,
            schichtung_oben_max=None,
            schichtung_oben_start=None,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=jetzt - timedelta(minutes=5),
            last_compressor_off_time=jetzt - timedelta(minutes=10),
        ),
    )


# ─────────────────────────── Unit: Haltezeit-Pruefung ────────────────────

def test_haltezeit_blockiert_frischen_wechsel():
    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    state = _state(previous="Notfallschutz", jetzt=jetzt)
    state.control._gewinner_bestaetigt_seit = jetzt - timedelta(minutes=1)
    assert pcl._gewinner_haltezeit_blockiert(state, "Keine Regel aktiv") is True


def test_haltezeit_gibt_nach_5min_frei():
    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    state = _state(previous="Notfallschutz", jetzt=jetzt)
    state.control._gewinner_bestaetigt_seit = jetzt - timedelta(minutes=6)
    assert pcl._gewinner_haltezeit_blockiert(state, "Keine Regel aktiv") is False


def test_haltezeit_blockiert_gleichen_gewinner_nicht():
    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    state = _state(previous="Notfallschutz", jetzt=jetzt)
    state.control._gewinner_bestaetigt_seit = jetzt - timedelta(seconds=10)
    assert pcl._gewinner_haltezeit_blockiert(state, "Notfallschutz") is False


def test_haltezeit_ohne_zeitstempel_nicht_aktiv():
    """Erster Wechsel (kein bestaetigter Gewinner vorher) wird nie blockiert."""
    state = _state(previous="Notfallschutz")
    assert pcl._gewinner_haltezeit_blockiert(state, "Komfort") is False


# ─────────────────────────── Integration ─────────────────────────────────

@pytest.mark.asyncio
async def test_gewinner_wechsel_wird_gehalten():
    """Ein sofortiger Folgewechsel wird von der Haltezeit geschluckt."""
    import priority_control as pc

    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    state = _state(jetzt=jetzt)
    a = pc.RegelErgebnis(name="Abweichung", prioritaet=47, aktiv=True,
                         einschalten=True, grund="A -> EIN")
    for _ in range(2):
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(a, [a])):
            await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)
    assert state.control.previous_modus == "Abweichung"

    b = pc.RegelErgebnis(name="Komfort", prioritaet=60, aktiv=True,
                         einschalten=True, grund="B -> EIN")
    for _ in range(3):
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(b, [b])):
            res = await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

    # Gewinner und Empfehlung bleiben stehen (Haltezeit aktiv).
    assert state.control.previous_modus == "Abweichung"
    assert res["soll_einschalten"] is True


@pytest.mark.asyncio
async def test_gewinner_wechsel_nach_haltezeit_wieder_moeglich():
    """Nach Ablauf der 5 min darf der neue Gewinner uebernehmen."""
    import priority_control as pc

    clock = _FakeClock(TZ.localize(datetime(2026, 1, 15, 12, 0)))
    state = _state(jetzt=clock.t)
    state.clock = clock
    a = pc.RegelErgebnis(name="Abweichung", prioritaet=47, aktiv=True,
                         einschalten=True, grund="A -> EIN")
    for _ in range(2):
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(a, [a])):
            await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)
    assert state.control.previous_modus == "Abweichung"

    clock.t = clock.t + timedelta(minutes=6)
    state.solar.last_api_call = clock.t  # Daten frisch halten
    b = pc.RegelErgebnis(name="Komfort", prioritaet=60, aktiv=True,
                         einschalten=True, grund="B -> EIN")
    for _ in range(2):
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(b, [b])):
            await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

    assert state.control.previous_modus == "Komfort"


@pytest.mark.asyncio
async def test_wechselursache_wird_fuer_das_log_geliefert():
    """A2: Ein zurueckgehaltener Wechsel nennt die Ursache (Debounce/Haltezeit).

    Ohne diese Angabe blieb ``handlung_override`` im Entscheidungslog ohne
    Detail ("Handlung AUS trotz Regel-EIN").
    """
    import priority_control as pc

    jetzt = TZ.localize(datetime(2026, 1, 15, 12, 0))
    state = _state(jetzt=jetzt)
    a = pc.RegelErgebnis(name="Abweichung", prioritaet=47, aktiv=True,
                         einschalten=True, grund="A -> EIN")
    for _ in range(2):
        with patch("priority_control_logic.bewerte_alle_regeln",
                   return_value=(a, [a])):
            await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)

    b = pc.RegelErgebnis(name="Komfort", prioritaet=60, aktiv=True,
                         einschalten=True, grund="B -> EIN")
    with patch("priority_control_logic.bewerte_alle_regeln",
               return_value=(b, [b])), \
         patch("priority_control_logic.entscheidungs_log.schreibe_eintrag") as log:
        for _ in range(3):
            await pcl.determine_mode_and_setpoints(state, 40.0, 43.0)
        gruende = [
            c.kwargs["diagnostics"].get("regelwechsel_grund")
            for c in log.call_args_list
        ]

    assert any(g and "Debounce" in g for g in gruende)
    assert any(g and "Mindesthaltezeit" in g for g in gruende)

