"""Alarm-Normalisierung: nur echte Stoerungen erzeugen Telegram-Nachrichten.

Grundregel: Ein Sperrgrund wird erst dann gemeldet, wenn er sich *aendert*.
Dynamische Teile (Restzeiten, Temperaturen) duerfen keine neue Nachricht
ausloesen. Taktschutz-Sperren (Mindestpause, Mindestlaufzeit,
Start-Antizipation) sind gewollter Normalbetrieb und alarmieren gar nicht -
sie stehen nach jedem Kompressorlauf an und wechseln dabei schnell, was sonst
je eine Nachricht pro Wechsel erzeugt.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

from main import ALARM_MINUTEN_MINIMUM, check_and_send_alerts

TZ = pytz.timezone("Europe/Berlin")
HEUTE = TZ.localize(datetime(2026, 9, 26, 16, 0, 0))


def _state(blocking, last_type=""):
    """Minimaler State fuer ``check_and_send_alerts``."""
    state = MagicMock()
    state.local_tz = TZ
    # Echte Uhr: _state_now(state) nutzt state.clock.now(), und
    # check_log_throttle() nutzt datetime.now() - beides muss ein datetime
    # liefern, sonst greifen die Drossel-/Retry-Pfade nicht.
    state.clock = MagicMock()
    state.clock.now.return_value = datetime.now(TZ)
    state.control.blocking_reason = blocking
    state.control.last_alert_type = last_type
    state.control.last_blocking_reason = None
    state.control.blocking_code = None
    state.control._last_alert_attempt_at = None
    state.control._last_alert_failed_type = None
    state.control._alert_retry_count = 0
    # Auch diese Felder muessen echte Typen sein, sonst greifen
    # die isinstance-Pruefungen in check_and_send_alerts nicht.
    state.config.Telegram.CHAT_ID = "123"
    state.config.Telegram.BOT_TOKEN = "abc"
    return state


@pytest.mark.asyncio
async def test_dynamische_teile_loesen_keine_zweite_nachricht_aus():
    """Gleicher Grund mit anderer Restzeit -> genau eine Nachricht."""
    state = _state("Verdampfer zu kalt (5.5°C < 6°C)")
    session = AsyncMock()
    with patch("control_logic.send_telegram_message", new_callable=AsyncMock) as send:
        send.return_value = True
        await check_and_send_alerts(session, state)
        assert send.call_count == 1
        assert "Verdampfer zu kalt" in send.call_args[0][2]

        state.control.blocking_reason = "Verdampfer zu kalt (5.2°C < 6°C)"
        await check_and_send_alerts(session, state)
        assert send.call_count == 1

        # Aufhebung sendet nichts und setzt den Alarmgemuetz zurueck
        state.control.blocking_reason = None
        await check_and_send_alerts(session, state)
        assert send.call_count == 1
        assert state.control.last_alert_type == ""


@pytest.mark.asyncio
async def test_echter_grundwechsel_meldet():
    state = _state("Verdampfer zu kalt (5.5°C < 6°C)")
    session = AsyncMock()
    with patch("control_logic.send_telegram_message", new_callable=AsyncMock) as send:
        send.return_value = True
        await check_and_send_alerts(session, state)
        assert send.call_count == 1

        # Neuer Stoerungsgrund, aber die globale Drosselung ist gerade erst
        # gelaufen -> bewusst keine zweite Nachricht.
        state.control.blocking_reason = "Druckschalter-Fehler"
        await check_and_send_alerts(session, state)
        assert send.call_count == 1
        assert state.control.blocking_code == "druckfehler"

        # Nach Ablauf der Drosselung wird der neue Grund gemeldet. Der neue
        # Code ist kein Retry des vorigen Alarms, daher wird der
        # Retry-Marker des vorigen Grundes zurueckgesetzt.
        # check_log_throttle() speichert auf state, nicht auf state.control.
        state.log_alarm_global_drossel = (
            datetime.now(TZ) - timedelta(minutes=ALARM_MINUTEN_MINIMUM + 1)
        )
        state.control._last_alert_attempt_at = None
        state.control._last_alert_failed_type = None
        await check_and_send_alerts(session, state)
        assert send.call_count == 2
        assert "Druckschalter-Fehler" in send.call_args[0][2]


@pytest.mark.parametrize("grund", [
    "Min. Pause (noch 29m 40s)",
    "Warte auf Mindestlaufzeit (noch 12m)",
    "Start-Antizipation: hub zur Obergrenze nur 0.1K (Rate 5C/h)",
])
@pytest.mark.asyncio
async def test_taktschutz_sendet_keine_nachricht(grund):
    """Taktschutz ist Normalbetrieb: keine Telegram-Nachricht."""
    state = _state(grund)
    with patch("control_logic.send_telegram_message", new_callable=AsyncMock) as send:
        send.return_value = True
        await check_and_send_alerts(AsyncMock(), state)
        assert send.call_count == 0
    # Der Grund bleibt trotzdem sichtbar (Log, WebApp, blocking_reason)
    assert state.control.blocking_reason == grund
    assert state.control.blocking_code


@pytest.mark.asyncio
async def test_grundwechsel_zwischen_taktschutz_gruenden_bleibt_still():
    """Der Spam-Fall aus dem Log: Min.Pause <-> Start-Antizipation."""
    reihenfolge = [
        "Min. Pause (noch 29m 40s)",
        "Start-Antizipation: hub zur Obergrenze nur 0.1K (Rate 5C/h)",
        "Min. Pause (noch 29m 10s)",
        "Start-Antizipation: hub zur Obergrenze nur 0.4K (Rate 5C/h)",
    ]
    state = _state(reihenfolge[0])
    with patch("control_logic.send_telegram_message", new_callable=AsyncMock) as send:
        send.return_value = True
        for grund in reihenfolge:
            state.control.blocking_reason = grund
            await check_and_send_alerts(AsyncMock(), state)
        assert send.call_count == 0


@pytest.mark.asyncio
async def test_telegram_fehlschlag_wird_nicht_als_erfolgreich_markiert():
    state = _state("Verdampfer zu kalt (5.5°C < 6°C)")
    with patch("control_logic.send_telegram_message", new_callable=AsyncMock) as send:
        send.return_value = False
        await check_and_send_alerts(AsyncMock(), state)
        assert state.control.last_alert_type == ""
        assert state.control.blocking_code == "verdampfer"
        assert state.control._last_alert_failed_type == "verdampfer"

        # Backoff-Fenster umgehen und erfolgreichen Retry simulieren.
        state.control._last_alert_attempt_at = None
        # check_log_throttle() speichert auf state, nicht auf state.control.
        state.log_alarm_global_drossel = (
            datetime.now(TZ) - timedelta(minutes=ALARM_MINUTEN_MINIMUM + 1)
        )
        send.return_value = True
        await check_and_send_alerts(AsyncMock(), state)
        assert state.control.last_alert_type == "verdampfer"
        assert state.control._last_alert_failed_type is None
