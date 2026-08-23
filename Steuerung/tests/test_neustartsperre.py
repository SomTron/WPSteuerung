"""Tests: Explizite Neustartsperre (Design-Fix #8).

Vorher wurde bei fehlgeschlagener Kompressor-Verifizierung ein
Zukunftszeitstempel in stats.last_compressor_off_time geschrieben,
um einen Neustart zu blockieren - die Mindestpausen-Anzeige loggte
dann eine erfundene Restpause. Jetzt gibt es ein ehrliches Feld
control.restart_lockout_until mit eigenem Blocking-Reason.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control_logic as pcl  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def baue_state(lockout_until=None):
    control = SimpleNamespace(
        kompressor_ein=False,
        _soll_einschalten=True,
        blocking_reason=None,
    )
    if lockout_until is not None:
        control.restart_lockout_until = lockout_until
    return SimpleNamespace(
        local_tz=TZ,
        control=control,
        stats=SimpleNamespace(last_compressor_off_time=None),
    )


def test_setze_neustartsperre_setzt_zeitpunkt():
    state = baue_state()
    vor = datetime.now(TZ)

    pcl.setze_neustartsperre(state, minuten=10)

    erwartet_von = vor + timedelta(minutes=9, seconds=55)
    erwartet_bis = vor + timedelta(minutes=10, seconds=5)
    assert erwartet_von <= state.control.restart_lockout_until <= erwartet_bis


@pytest.mark.asyncio
async def test_aktive_sperre_blockiert_mit_lesbarem_grund():
    """Waehrend der Sperre: kein Start, Blocking-Reason nennt die Sperre."""
    state = baue_state(lockout_until=datetime.now(TZ) + timedelta(minutes=5))
    mock_set = AsyncMock(return_value=True)

    result = await pcl.handle_compressor_on(
        state, None, regelfuehler=36.0, einschaltpunkt=38.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), min_pause=timedelta(minutes=30),
        t_oben=40.0, set_kompressor_status_func=mock_set,
    )

    assert result is False
    mock_set.assert_not_called()
    assert state.control.blocking_reason.startswith("Neustartsperre (noch 4m ")


@pytest.mark.asyncio
async def test_abgelaufene_sperre_blockiert_nicht():
    """Nach Ablauf der Sperre startet der Kompressor normal."""
    state = baue_state(lockout_until=datetime.now(TZ) - timedelta(minutes=1))
    mock_set = AsyncMock(return_value=True)

    result = await pcl.handle_compressor_on(
        state, None, regelfuehler=36.0, einschaltpunkt=38.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), min_pause=timedelta(minutes=30),
        t_oben=40.0, set_kompressor_status_func=mock_set,
    )

    assert result is True
    mock_set.assert_called_once()


@pytest.mark.asyncio
async def test_erfolgreicher_start_räumt_sperre_weg():
    """Sobald gestartet wurde, ist die Sperre konsumiert (Feld auf None)."""
    state = baue_state(lockout_until=datetime.now(TZ) - timedelta(minutes=1))
    mock_set = AsyncMock(return_value=True)

    await pcl.handle_compressor_on(
        state, None, regelfuehler=36.0, einschaltpunkt=38.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), min_pause=timedelta(minutes=30),
        t_oben=40.0, set_kompressor_status_func=mock_set,
    )

    assert state.control.restart_lockout_until is None


@pytest.mark.asyncio
async def test_fehlendes_feld_blockiert_nicht():
    """Alte State-Objekte ohne das Feld (getattr-Default) duerfen starten."""
    state = baue_state()  # kein restart_lockout_until gesetzt
    assert not hasattr(state.control, 'restart_lockout_until')
    mock_set = AsyncMock(return_value=True)

    result = await pcl.handle_compressor_on(
        state, None, regelfuehler=36.0, einschaltpunkt=38.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=60), min_pause=timedelta(minutes=30),
        t_oben=40.0, set_kompressor_status_func=mock_set,
    )

    assert result is True
    mock_set.assert_called_once()
