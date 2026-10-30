# -*- coding: utf-8 -*-
"""Regression 30.10.2026: Der Schichtungsdeckel brach die Legionellenfahrt ab.

Betriebslog 30.10.2026, 10:38:09:

    SCHICHTUNG AUS (cycle=23) reason=schichtung:
    oben 49.0C >= 49.0C (Start 48.0C)
    Legionellenlauf vor dem Ziel abgebrochen (schichtung, Ziel 60C nicht
    erreicht)

Der Deckel (48,0 C beim Einsetzen des Warmstarts + `schichtung_max_steig_k`
1,0 K) stammt aus dem Schichtungs-Warmstart der Abweichungs-Regel. Er wurde
von `handle_compressor_off` ohne Legionellen-Ausnahme durchgesetzt, obwohl
Boiler-Maximum und Ueberhitzungsschutz waehrend der Prophylaxe ueber
`_erhoehtes_legionellen_limit` auf `legionellen_max_temp_c` (65 C) stehen.

Getestet werden die ECHTEN Funktionen `handle_compressor_off` und
`handle_compressor_on` - nur dort wird der Deckel wirksam bzw. als
Start-Sperre verwendet.
"""
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def _state(t_oben, t_unten, t_mittig=None):
    """Zustand wie im Betriebslog: oben 49,0 C, unten 48,4 C, Deckel 49,0 C."""
    now = datetime.now(TZ)
    control = SimpleNamespace(
        kompressor_ein=False,
        blocking_reason=None,
        _soll_einschalten=True,
        restart_lockout_until=None,
        requested_rule_name="Legionellen",
        schichtung_oben_max=None,
        schichtung_oben_start=None,
        boiler_max_blockiert=None,
        _lauf_start_regel="Legionellen",
        zyklus_id=23,
        # Frische Heizrate, damit die Start-Antizipation den Start nicht aus
        # einem anderen Grund verweigert (getestet wird der Deckel).
        _rate_messung={"ts": now - timedelta(minutes=5), "unten": t_unten - 0.8},
        _rate_messungen=deque(
            [
                (now - timedelta(minutes=15), 7.2),
                (now - timedelta(minutes=10), 8.1),
                (now - timedelta(minutes=5), 9.0),
            ],
            maxlen=5,
        ),
        _rate_confidence=0.9,
    )
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(
            t_unten=t_unten,
            t_oben=t_oben,
            t_mittig=t_unten if t_mittig is None else t_mittig,
            t_verd=10.0,
        ),
        priority_config=WPSteuerungConfig(),
        control=control,
        legionellen_aktiv=True,
        legionellen_temp_override=65.0,
        legionellen_started_at=now - timedelta(hours=2),
        legionellen_end_time=None,
        legionellen_target_reached_at=None,
        legionellen_unvollstaendig=False,
        stats=SimpleNamespace(
            last_compressor_on_time=now - timedelta(hours=2),
            # Kompressor stand deutlich laenger still als die Mindestpause.
            last_compressor_off_time=now - timedelta(hours=3),
        ),
    )


def _deckel_setzen(state, deckel=49.0, basis=48.0):
    state.control.schichtung_oben_max = deckel
    state.control.schichtung_oben_start = basis


async def _set_status(st, ein, **kw):
    st.control.kompressor_ein = ein
    return True


@pytest.mark.asyncio
async def test_legionellenfahrt_wird_nicht_vom_deckel_beendet():
    """Der Fall aus dem Log: oben erreicht den Deckel, die Fahrt laeuft weiter."""
    state = _state(t_oben=49.0, t_unten=48.4, t_mittig=48.3)
    _deckel_setzen(state)
    state.control.kompressor_ein = True

    gestoppt = await pcl.handle_compressor_off(
        state, None,
        regelfuehler=48.4, ausschaltpunkt=60.0,
        min_laufzeit=timedelta(seconds=0), t_oben=49.0,
        set_kompressor_status_func=_set_status, regel_name="Legionellen",
    )

    assert gestoppt is False, "Legionellenfahrt wurde am Schichtungsdeckel beendet"
    assert state.control.kompressor_ein is True
    assert "Schichtungs-Obergrenze" not in (state.control.blocking_reason or "")
    # Der Deckel ist ausser Kraft; die BASIS bleibt (kein Hochschrauben pro Lauf).
    assert state.control.schichtung_oben_max is None
    assert state.control.schichtung_oben_start == 48.0


@pytest.mark.asyncio
async def test_start_trotz_deckel_waehrend_legionellenfahrt():
    """Ein alter Deckel darf den (Nach-)Start der Prophylaxe nicht sperren."""
    state = _state(t_oben=49.0, t_unten=48.4, t_mittig=48.3)
    _deckel_setzen(state)
    state.control._lauf_start_regel = None

    gestartet = await pcl.handle_compressor_on(
        state, None,
        regelfuehler=48.4, einschaltpunkt=48.0, ausschaltpunkt=60.0,
        min_laufzeit=timedelta(minutes=60), min_pause=timedelta(minutes=30),
        t_oben=49.0, t_mittig=48.3,
        set_kompressor_status_func=_set_status, regel_name="Legionellen",
    )

    assert gestartet is True, (
        f"Start am Deckel verweigert: {state.control.blocking_reason}"
    )
    assert state.control.kompressor_ein is True
    assert state.control.schichtung_oben_max is None


@pytest.mark.asyncio
async def test_ohne_legionellen_bleibt_der_deckel_wirksam():
    """Kontrolle: ausserhalb der Prophylaxe stoppt der Deckel unveraendert."""
    state = _state(t_oben=49.0, t_unten=48.4, t_mittig=48.3)
    _deckel_setzen(state)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.control.kompressor_ein = True
    # Boiler-Maximum anheben, damit hier ausschliesslich der Deckel greift
    # (Standard 48 C wuerde bei unten 48,4 C vorher abschalten).
    state.priority_config.sicherheit.max_temp_c = 55.0

    gestoppt = await pcl.handle_compressor_off(
        state, None,
        regelfuehler=48.4, ausschaltpunkt=60.0,
        min_laufzeit=timedelta(seconds=0), t_oben=49.0,
        set_kompressor_status_func=_set_status, regel_name="Abweichung",
    )

    assert gestoppt is True
    assert state.control.kompressor_ein is False
    assert "Schichtungs-Obergrenze" in (state.control.blocking_reason or "")
    # Auch hier bleibt die Basis stehen, nur der Deckel wird freigegeben.
    assert state.control.schichtung_oben_max is None
    assert state.control.schichtung_oben_start == 48.0
