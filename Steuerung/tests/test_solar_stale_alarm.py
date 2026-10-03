"""Tests: Alarm beim Ausfall der Solax-Live-Daten.

Der Verlust der Solardaten pausiert alle Quellenregeln (Einspeisung,
CalcStart, AdaptivePV, Batterie, Zeitfenster, Forecast). Es heizen dann
nur noch Abweichung, Mindesttemperatur, Notfallschutz und Legionellen -
und die ohne Quellenpruefung, also aus dem Netz. Bisher stand dazu nur
eine Logzeile im Journal; der Betreiber konnte stundenlang im Netzbetrieb
heizen, ohne es zu merken.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _true():
    return True


def _sammel_mock(monkeypatch):
    """Hängt einen asynchronen Telegram-Mock ein und liefert die Liste."""
    import main as m

    gesendet = []

    async def send(session, chat_id, msg, token, **kw):
        gesendet.append(msg)
        return True

    monkeypatch.setattr(m.control_logic, "send_telegram_message", send)
    return gesendet


@pytest.mark.asyncio
async def test_solar_ausfall_wird_gemeldet(monkeypatch):
    """Beim Stale-Zustand geht eine erklaerende Telegram-Nachricht raus."""
    import main as m

    gesendet = []

    async def send(session, chat_id, msg, token, **kw):
        gesendet.append(msg)
        return True

    monkeypatch.setattr(m.control_logic, "send_telegram_message", send)
    monkeypatch.setattr(m, "check_log_throttle", lambda *a, **k: True)

    state = SimpleNamespace(
        bot_token="t",
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )
    await m._melde_solar_ausfall(None, state, 47.0)

    assert len(gesendet) == 1
    text = gesendet[0]
    # Die FOLGE muss benannt sein, sonst bleibt "veraltet" eine Nebensache
    assert "aus dem Netz" in text or "Netz" in text
    assert "pausiert" in text
    assert "47" in text


@pytest.mark.asyncio
async def test_wiederholung_nur_nach_intervall(monkeypatch):
    """Ein langer Ausfall bleibt sichtbar, ohne Telegram zu fluten.

    Geprueft wird die Semantik, nicht eine Magic Number im Mock:
    die erste Meldung geht sofort raus, danach entscheidet die Drosselung.
    Hier ist das Intervall noch nicht abgelaufen -> genau eine Meldung.
    """
    import main as m

    gesendet = _sammel_mock(monkeypatch)

    throttle_aufrufe = []

    def _throttle(state, key, interval_minutes=0.0):
        throttle_aufrufe.append((key, interval_minutes))
        return False  # Intervall noch nicht abgelaufen

    monkeypatch.setattr(m, "check_log_throttle", _throttle)
    state = SimpleNamespace(
        bot_token="t",
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )
    for _ in range(5):
        await m._melde_solar_ausfall(None, state, 30.0)

    assert len(gesendet) == 1, f"zu viele Meldungen: {len(gesendet)}"
    # Der Zaehler zaehlt weiter - er ist die Grundlage fuer die Entwarnung.
    assert state.solar_stale_meldungen == 5
    # Jede Meldung nach der ersten wird gedrosselt, mit dem definierten Intervall.
    assert len(throttle_aufrufe) == 4, throttle_aufrufe
    assert all(
        key == "_alarm_solar_stale" and iv == m.STALE_ALARM_MINUTEN
        for key, iv in throttle_aufrufe
    ), throttle_aufrufe


@pytest.mark.asyncio
async def test_langer_ausfall_wiederholt_sich(monkeypatch):
    """Nach Ablauf des Intervalls meldet der Ausfall erneut.

    Genau das unterscheidet einen 20-Minuten-Ausfall von einem, der den
    ganzen Tag laeuft - beide duerfen nicht gleich behandelt werden.
    """
    import main as m

    gesendet = _sammel_mock(monkeypatch)

    # Ab dem zweiten Durchlauf ist das Intervall abgelaufen.
    durchlauf = []

    def _throttle(state, key, interval_minutes=0.0):
        durchlauf.append(1)
        return len(durchlauf) >= 2

    monkeypatch.setattr(m, "check_log_throttle", _throttle)
    state = SimpleNamespace(
        bot_token="t",
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )
    for _ in range(4):
        await m._melde_solar_ausfall(None, state, 45.0)

    # Erste sofort, danach je abgelaufenem Intervall eine weitere.
    assert len(gesendet) == 3, gesendet
    assert "aus" in gesendet[0] and "Netz" in gesendet[0]


@pytest.mark.asyncio
async def test_entwarnung_nur_einmal_und_nur_nach_ausfall(monkeypatch):
    """Ohne vorherigen Ausfall darf keine Entwarnung entstehen."""
    import main as m

    gesendet = _sammel_mock(monkeypatch)

    state = SimpleNamespace(
        bot_token="t", solar_stale=False, solar_stale_meldungen=0,
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )
    await m._solar_ausfall_zurueck_melden(None, state)
    assert gesendet == [], "Entwarnung ohne vorherigen Ausfall"

    state.solar_stale_meldungen = 3
    await m._solar_ausfall_zurueck_melden(None, state)
    assert len(gesendet) == 1
    assert "3 Alarme" in gesendet[0]
    assert state.solar_stale_meldungen == 0

    # Zweite Entwarnung unterbleibt (Zaehler ist zurueckgesetzt)
    await m._solar_ausfall_zurueck_melden(None, state)
    assert len(gesendet) == 1


@pytest.mark.asyncio
async def test_entwarnung_nicht_während_ausfall(monkeypatch):
    """Solange die Daten stale sind, darf keine Entwarnung laufen."""
    import main as m

    gesendet = _sammel_mock(monkeypatch)
    state = SimpleNamespace(
        bot_token="t", solar_stale=True, solar_stale_meldungen=2,
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )
    await m._solar_ausfall_zurueck_melden(None, state)
    assert gesendet == []
    assert state.solar_stale_meldungen == 2, "Zaehler wurde zurueckgesetzt"


@pytest.mark.asyncio
async def test_ohne_bot_token_kein_absturz(monkeypatch):
    """Fehlender Telegram-Token darf den Datenpfad nie reiessen."""
    import main as m

    monkeypatch.setattr(m, "check_log_throttle", lambda *a, **k: True)
    state = SimpleNamespace(bot_token=None, config=None)
    await m._melde_solar_ausfall(None, state, 20.0)  # darf nicht werfen
    assert state.solar_stale_meldungen == 1


def test_status_payload_nennt_die_folge():
    """Die WebApp soll die Netzheizungs-Folge mitliefern.

    Geprueft wird derselbe Weg wie in test_api_routen: der Fake-State wird
    an `api.shared_state` gehaengt und die echte /status-Route gerufen.
    """
    from types import SimpleNamespace as NS

    import pytz
    from json_config import WPSteuerungConfigManager
    import api

    fake = NS(
        priority_config=WPSteuerungConfigManager().config,
        local_tz=pytz.timezone("Europe/Berlin"),
        solar=NS(last_api_call=None, batpower=0.0, feedinpower=0.0,
                 soc=50.0, acpower=0.0),
        sensors=NS(t_oben=42.0, t_mittig=41.0, t_unten=40.0,
                   t_verd=-2.0, t_boiler=41.0),
        compressor=NS(status="AUS", laeuft=False, runtime_today="0m",
                      runtime_current="0m", start_time=None),
        mode=NS(current="Normal", active_rule="Keine", active_rule_sensor="",
                blocking_reason=None, soll_einschalten=False, bath_active=False,
                holiday_active=False, solar_active=False, nightsperre_active=False,
                sommer_modus_aktiv=False, sommer_modus_offset_c=0.0,
                sommer_modus_tage_ueber=0, sommer_modus_benoetigte=3),
        energy=NS(soc=50.0, battery_power=0.0, feed_in=0.0, ac_power=0.0,
                  forecast_today=None, forecast_tomorrow=None,
                  forecast_day2=None, sunrise=None, sunset=None),
        system=NS(last_update="-", exclusion_reason=None),
        setpoints=NS(einschaltpunkt=40.0, ausschaltpunkt=38.0),
        regel_ergebnisse=[],
        stats=NS(current_runtime="0m", total_runtime_today="0m"),
        control=NS(alle_ergebnisse=[], kompressor_ein=False,
                   aktueller_einschaltpunkt=40.0, aktueller_ausschaltpunkt=38.0,
                   ausschluss_grund=None),
        learning_engine=None,
        sicherheits_temp=55.0,
        verdampfertemperatur=-5.0,
        solar_stale=True,
        solar_stale_meldungen=4,
    )
    original = api.shared_state
    try:
        api.shared_state = fake
        d = api.get_status()
    finally:
        api.shared_state = original

    ind = d["status_indikatoren"]
    assert ind["solar_stale"] is True
    assert ind["solar_stale_alarme"] == 4
    assert "Netz" in ind["solar_stale_hinweis"]