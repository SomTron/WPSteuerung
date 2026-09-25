"""Tests fuer die Sperrklassifikation `boiler_bereits_warm`.

Hintergrund: Nach einer Legionellenfahrt ist der Boiler heiss. Die
Startnaehe-Sperre (`boiler_max_ein_abstand_k`) verhindert dann fuer Stunden
einen Start - korrekt, weil ein Start am Temperaturlimit in einen Kurzlauf
laeuft. Vorher stand das in der Statuszeile als `Blocking: Boiler-Max-Naehe`
und loeste einen Telegram-Alarm aus, was wie eine Stoerung aussah.

Diese Tests sichern ab:

- die Klassifikation trennt Normalzustand von echter Stoerung,
- die harte Boiler-Maximum-Abschaltung bleibt Alarm,
- `INFO_BLOCKING_CODES` erzeugt keinen Telegram-Versand,
- der Code ist stabil gegen Umlaut-Schreibweisen,
- API liefert Code und Einstufung fuer die WebApp.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytz

STEUERUNG = Path(__file__).resolve().parents[1]
PROJEKT = STEUERUNG.parent
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from blocking_codes import (  # noqa: E402
    INFO_BLOCKING_CODES,
    blocking_code,
    normalisiere_sperrtext,
)

TZ = pytz.timezone("Europe/Berlin")


def _payload(state):
    import api

    for name in dir(api):
        obj = getattr(api, name)
        if callable(obj) and getattr(obj, "__name__", "") == "build_mode_payload":
            return obj(state)
    raise AssertionError("build_mode_payload nicht gefunden")


# ------------------------------------------------------------- Klassifikation


@pytest.mark.parametrize(
    "text",
    [
        "Boiler-Max-Naehe (unten 56.0C, mittig 58.0C, Einschalten erst < 45.0C)",
        "Boiler-Max-Nähe (unten 56.0C)",
        "Zieltemp erreicht",
        "Schichtungs-Obergrenze (48.8C >= 48.8C (Start 47.8C))",
    ],
)
def test_normalzustand_wird_als_info_klassifiziert(text):
    assert blocking_code(text) == "boiler_bereits_warm"
    assert blocking_code(text) in INFO_BLOCKING_CODES


@pytest.mark.parametrize(
    "text",
    [
        "Ueberhitzungsschutz (62.0C >= 62.0C)",
        "Min. Pause (noch 5m 3s)",
        "Warte auf Mindestlaufzeit (noch 12m)",
        "Regel 'Notfallschutz' sagt AUS, warte auf Mindestlaufzeit (noch 59m)",
        "Sensorfehler: T_Oben invalid",
        "Druckschalter-Fehler",
        "Verdampfer zu kalt (5.0C < 6.0C)",
        "Solar-Daten veraltet | STALE",
        "Nachtsperre (kein Einschalten, unten=41.0C)",
        "Legionellen wartet auf PV/Batterie",
        "Kompressor-Ausschalten fehlgeschlagen",
        "Kompressor-Einschalten fehlgeschlagen",
        "Regelungsfehler",
    ],
)
def test_echte_stoerungen_bleiben_alarm(text):
    code = blocking_code(text)
    assert code not in INFO_BLOCKING_CODES, f"{text!r} darf keine Info sein"
    assert code != "boiler_bereits_warm"


def test_umlautschreibweisen_ergeben_denselben_code():
    """Regression: 'Naehe' vs 'Nähe' darf nicht unterschiedlich klassifizieren."""
    assert blocking_code("Boiler-Max-Naehe (unten 56.0C)") == blocking_code(
        "Boiler-Max-Nähe (unten 56.0C)"
    )


def test_leere_und_none_ergeben_leeren_code():
    assert blocking_code(None) == ""
    assert blocking_code("") == ""
    assert blocking_code("   ") == ""


def test_normalisierung_ersetzt_alle_umlaute():
    assert normalisiere_sperrtext("ÄÖÜß") == "aeoeuess"


def test_hartes_boiler_maximum_ist_kein_normalzustand():
    """Die Abschaltung am Limit bleibt ein Alarm - nur die Naehe ist Info."""
    assert blocking_code("Ueberhitzungsschutz (62.0C >= 62.0C)") == "uebertemperatur"
    assert blocking_code("Boiler-Max (unten 48.5C >= 48.0C)") == "boiler_max"


# ------------------------------------------------------------------- Telegram


def _alert_state(blocking_reason):
    control = SimpleNamespace(
        blocking_reason=blocking_reason,
        blocking_code=None,
        last_alert_type="",
        last_blocking_reason=None,
        _last_alert_attempt_at=None,
        _last_alert_failed_type=None,
        _alert_retry_count=0,
    )
    state = SimpleNamespace(control=control, local_tz=TZ)
    state.config = SimpleNamespace(
        Telegram=SimpleNamespace(CHAT_ID="123", BOT_TOKEN="token")
    )
    for name in (
        "log_boiler_bereits_warm",
        "log_boiler_naehe_alert",
        "last_sensor_error_log",
        "last_forecast_log",
        "last_vpn_log",
    ):
        setattr(state, name, None)
    return state


def _patch_telegram(monkeypatch):
    """Klemmt den Telegram-Versand ab und liefert die Liste der Nachrichten."""
    import control_logic
    import main as M

    gesendet: list[str] = []

    async def fake_send(session, chat_id, msg, token, parse_mode=None):
        gesendet.append(msg)
        return True

    monkeypatch.setattr(M, "escape_markdown", lambda s: s, raising=False)
    monkeypatch.setattr(control_logic, "send_telegram_message", fake_send)
    monkeypatch.setattr(M.control_logic, "send_telegram_message", fake_send)
    return gesendet


@pytest.mark.asyncio
async def test_boiler_bereits_warm_sendet_keinen_telegram_alarm(monkeypatch):
    import main as M

    gesendet = _patch_telegram(monkeypatch)
    state = _alert_state("Boiler-Max-Naehe (unten 56.0C, mittig 58.0C)")

    await M.check_and_send_alerts(None, state)

    assert gesendet == [], "Normalzustand darf keinen Alarm senden"
    assert state.control.blocking_code == "boiler_bereits_warm"
    assert state.control.last_blocking_reason


@pytest.mark.asyncio
async def test_echte_sperre_sendet_weiterhin_telegram(monkeypatch):
    import main as M

    gesendet = _patch_telegram(monkeypatch)
    state = _alert_state("Sensorfehler: T_Oben invalid")

    await M.check_and_send_alerts(None, state)

    assert len(gesendet) == 1
    assert "blockiert" in gesendet[0]
    assert state.control.blocking_code == "sensorfehler"


@pytest.mark.asyncio
async def test_info_phase_hinterlaesst_keinen_alarmmarker(monkeypatch):
    """Nach der Info-Phase muss eine echte Sperre sofort alarmieren."""
    import main as M

    gesendet = _patch_telegram(monkeypatch)
    state = _alert_state("Boiler-Max-Naehe (unten 56.0C)")

    await M.check_and_send_alerts(None, state)
    assert gesendet == []

    state.control.blocking_reason = "Druckschalter-Fehler"
    await M.check_and_send_alerts(None, state)
    assert len(gesendet) == 1, "echte Sperre nach Info-Phase muss alarmieren"


# ------------------------------------------------------------------ API/Status


def test_api_status_meldet_code_und_einstufung():
    control = SimpleNamespace(
        blocking_reason="Boiler-Max-Naehe (unten 56.0C)",
        requested_rule_name="Abweichung",
        active_rule_sensor=None,
        source_current=None,
    )
    payload = _payload(SimpleNamespace(control=control))
    assert payload["blocking_code"] == "boiler_bereits_warm"
    assert payload["blocking_kind"] == "info"


def test_api_status_meldet_echte_sperre_als_alarm():
    control = SimpleNamespace(
        blocking_reason="Sensorfehler: T_Oben invalid",
        requested_rule_name=None,
        active_rule_sensor=None,
        source_current=None,
    )
    payload = _payload(SimpleNamespace(control=control))
    assert payload["blocking_code"] == "sensorfehler"
    assert payload["blocking_kind"] == "alarm"


def test_statuszeile_sagt_info_statt_blocking():
    """Regression: die Stunden nach der Legionellenfahrt sahen wie ein
    Fehler aus, weil die Statuszeile 'Blocking:' schrieb."""
    main_text = (STEUERUNG / "main.py").read_text(encoding="utf-8")
    assert 'log_line += f" | Info: {state.control.blocking_reason}"' in main_text
    assert 'log_line += f" | Blocking: {state.control.blocking_reason}"' in main_text


def test_webapp_stellt_normalzustand_als_info_dar():
    html = (PROJEKT / "webapp" / "index.html").read_text(encoding="utf-8")
    assert "boiler_bereits_warm" in html
    assert "mode.blocking_code" in html


def test_analyse_kennt_den_neuen_code():
    text = (PROJEKT / "Analyse" / "analysis_core.py").read_text(encoding="utf-8")
    assert '"boiler_bereits_warm"' in text
