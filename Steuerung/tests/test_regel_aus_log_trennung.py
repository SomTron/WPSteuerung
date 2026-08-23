"""Tests: Trennung 'Regel sagt AUS' vs. 'Keine Regel aktiv' (Design-Fix #5).

Vorher liefen beide Faelle unter demselben Log "Keine Regel aktiv",
sodass man im Nachhinein nicht sehen konnte, WER ausgeschaltet hat -
eine explizit entscheidende Regel oder schlicht Regel-Funkenschutz.
"""
import os
import sys
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from priority_control_logic import handle_compressor_off  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def baue_state(lauf_minuten):
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=SimpleNamespace(sicherheit=SimpleNamespace(ueberhitzung_c=58.0)),
        control=SimpleNamespace(
            kompressor_ein=True,
            blocking_reason=None,
            _soll_einschalten=False,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=datetime.now(TZ) - timedelta(minutes=lauf_minuten),
        ),
    )


@pytest.mark.asyncio
async def test_regel_sagt_aus_erscheint_im_log(caplog):
    """Gewinner-Regel entschied AUS + Mindestlaufzeit erreicht -> eindeutiger Log."""
    state = baue_state(lauf_minuten=30)

    with caplog.at_level(logging.INFO):
        result = await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
            regel_name="Komfort",
        )

    assert result is True
    assert state.control.blocking_reason is None
    meldungen = [r.message for r in caplog.records]
    assert any("Regel 'Komfort' sagt AUS" in m for m in meldungen)
    assert not any("Keine Regel aktiv" in m for m in meldungen)


@pytest.mark.asyncio
async def test_regel_sagt_aus_wartet_auf_mindestlaufzeit(caplog):
    """Regel will AUS, Mindestlaufzeit laeuft noch -> Blocking-Reason nennt die Regel."""
    state = baue_state(lauf_minuten=10)

    with caplog.at_level(logging.INFO):
        result = await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
            regel_name="Abweichung",
        )

    assert result is False
    assert state.control.blocking_reason == (
        "Regel 'Abweichung' sagt AUS, warte auf Mindestlaufzeit (noch 4m)"
    )
    assert any(
        "Regel 'Abweichung' sagt AUS, aber Mindestlaufzeit" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_keine_regel_alttext_bleibt_erhalten(caplog):
    """Regression: ohne regel_name bleibt der gewohnte Text unveraendert."""
    state = baue_state(lauf_minuten=30)

    with caplog.at_level(logging.INFO):
        result = await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
        )

    assert result is True
    assert any("Keine Regel aktiv: Kompressor AUS" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_keine_regel_blocking_reason_unveraendert(caplog):
    """Regression: Blocking-Reason ohne Regel wie gehabt 'Keine Regel aktiv, ...'."""
    state = baue_state(lauf_minuten=10)

    result = await handle_compressor_off(
        state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), t_oben=40.0,
        set_kompressor_status_func=AsyncMock(return_value=True),
    )

    assert result is False
    assert state.control.blocking_reason == (
        "Keine Regel aktiv, warte auf Mindestlaufzeit (noch 4m)"
    )


@pytest.mark.asyncio
async def test_throttle_keys_getrennt_pro_fall(caplog):
    """Regel-AUS und keine-Regel drosseln UNABHAENGIG voneinander."""
    state = baue_state(lauf_minuten=10)

    with caplog.at_level(logging.INFO):
        await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
            regel_name="Komfort",
        )
        await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
            regel_name=None,
        )

    meldungen = [r.message for r in caplog.records]
    assert sum(1 for m in meldungen if "Regel 'Komfort' sagt AUS" in m) == 1
    assert sum(1 for m in meldungen if "Keine Regel aktiv, aber Mindestlaufzeit" in m) == 1


@pytest.mark.asyncio
async def test_ueberhitzung_hat_vorrang_vor_beiden_texten(caplog):
    """Sicherheits-Abschaltung (Ueberhitzung) bleibt von der Kontext-Logik unberuehrt."""
    state = baue_state(lauf_minuten=30)

    with caplog.at_level(logging.WARNING):
        result = await handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=60.0,  # >= ueberhitzung_c
            set_kompressor_status_func=AsyncMock(return_value=True),
            regel_name="PV_X",
        )

    assert result is True
    assert "Ueberhitzungsschutz" in state.control.blocking_reason
    meldungen = [r.message for r in caplog.records]
    assert any("SICHERHEIT AUS" in m for m in meldungen)
    assert not any("Regel 'PV_X' sagt AUS" in m for m in meldungen)
