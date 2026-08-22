"""Tests: Stale-Schutz fuer Solar-Daten (Bugfix #1).

Vorher: Bei API-Ausfall blieb last_api_data stehen und steuerte die
PV-Regeln stundenlang mit alten Werten (Kompressor lief auf Netzstrom
im Glauben, es sei PV-Überschuss).
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import update_system_data  # noqa: E402
from constants import SOLAR_DATA_STALE_THRESHOLD_MIN  # noqa: E402


def baue_state(letzter_call_vor_min=None):
    """State mit Solar-Daten; letzter_call_vor_min=None -> nie abgerufen."""
    tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(tz)
    solar = SimpleNamespace(
        feedinpower=1234.0,
        batpower=567.0,
        soc=88.0,
        last_api_data={"feedinpower": 2500.0, "batPower": 1500.0, "soc": 66},
        last_api_call=(
            now - timedelta(minutes=letzter_call_vor_min)
            if letzter_call_vor_min is not None else None
        ),
    )
    state = SimpleNamespace(solar=solar, local_tz=tz, sensors=SimpleNamespace())
    sensor_manager = SimpleNamespace(
        get_all_temperatures=AsyncMock(return_value={})
    )
    return state, sensor_manager


@pytest.mark.asyncio
async def test_veraltete_daten_werden_genullt(caplog):
    """API tot seit > Threshold: Stale-Werte duerfen PV-Regeln NICHT steuern."""
    state, sensor_manager = baue_state(letzter_call_vor_min=SOLAR_DATA_STALE_THRESHOLD_MIN + 30)

    with patch('main.sensor_manager', sensor_manager), \
         patch('main.get_solax_data', new_callable=AsyncMock, return_value=None), \
         caplog.at_level("WARNING"):
        await update_system_data(None, state)

    assert state.solar.feedinpower == 0.0
    assert state.solar.batpower == 0.0
    assert state.solar.soc == 0.0
    assert any("veraltet" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_frische_cached_daten_werden_verwendet():
    """API kurz vorher erfolgreich: Cache-Werte sind gueltig und werden genutzt."""
    state, sensor_manager = baue_state(letzter_call_vor_min=10)

    with patch('main.sensor_manager', sensor_manager), \
         patch('main.get_solax_data', new_callable=AsyncMock, return_value=None):
        await update_system_data(None, state)

    assert state.solar.feedinpower == 2500.0
    assert state.solar.batpower == 1500.0
    assert state.solar.soc == 66


@pytest.mark.asyncio
async def test_ohne_api_historie_kein_crash():
    """Noch nie Daten geholt (None/None): Werte bleiben unangetastet, kein Fehler."""
    state, sensor_manager = baue_state(letzter_call_vor_min=None)
    state.solar.last_api_data = None

    with patch('main.sensor_manager', sensor_manager), \
         patch('main.get_solax_data', new_callable=AsyncMock, return_value=None):
        await update_system_data(None, state)

    assert state.solar.feedinpower == 1234.0  # Startwerte unberuehrt
    assert state.solar.last_api_call is None


@pytest.mark.asyncio
async def test_erneuter_stale_zyklus_wird_gedrosselt(caplog):
    """Warnung ist gethrottelt - nicht alle 10 Sekunden Log-Spam."""
    state, sensor_manager = baue_state(letzter_call_vor_min=SOLAR_DATA_STALE_THRESHOLD_MIN + 60)

    with patch('main.sensor_manager', sensor_manager), \
         patch('main.get_solax_data', new_callable=AsyncMock, return_value=None), \
         caplog.at_level("WARNING"):
        await update_system_data(None, state)
        erste_anzahl = sum(1 for r in caplog.records if "veraltet" in r.message)
        await update_system_data(None, state)  # direkt danach
        zweite_anzahl = sum(1 for r in caplog.records if "veraltet" in r.message)

    assert erste_anzahl == 1
    assert zweite_anzahl == 1  # gedrosselt
    assert state.solar.feedinpower == 0.0  # Nullung passiert trotzdem weiter
