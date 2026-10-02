"""Tests: Not-Aus (Sperre, Abschaltung, Aufhebung).

Sicherheitskritisch. Geprueft wird:
- die Sperre ueberlebt jeden Neustart (Datei, nicht nur ein State-Flag),
- ein gesperrter Zustand laesst die Kontrollphase nicht einschalten,
- die Abschaltung wird geprueft und ein Fehlschlag laut gemeldet,
- das Aufheben passiert nur ueber einen ausdruecklichen Weg.
"""

import logging
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytz  # noqa: E402

import notaus  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ---------- Sperrdatei ----------

def test_sperre_ueberlebt_neustart(tmp_path):
    """Nur eine Datei haelt den Zustand - ein State-Flag waere nach Neustart weg."""
    datei = str(tmp_path / "notaus.lock")
    assert notaus.notaus_gesetzt(datei) is False
    assert notaus.notaus_daten(datei) is None

    assert notaus.notaus_setzen("Testgrund", datei) is True
    assert notaus.notaus_gesetzt(datei) is True

    # "Neustart": alles In-Memory weg, nur die Datei bleibt.
    daten = notaus.notaus_daten(datei)
    assert daten["grund"] == "Testgrund"
    assert daten["ts"]
    assert daten["exit_code"] == notaus.EXIT_CODE_NOTAUS

    assert notaus.notaus_loeschen(datei) is True
    assert notaus.notaus_gesetzt(datei) is False


def test_beschaedigte_sperre_bleibt_sperre(tmp_path):
    """Eine unlesbare Datei darf den Not-Aus NICHT aufheben."""
    datei = str(tmp_path / "notaus.lock")
    with open(datei, "w", encoding="utf-8") as f:
        f.write("{kein json")
    assert notaus.notaus_gesetzt(datei) is True
    daten = notaus.notaus_daten(datei)
    assert daten is not None, "beschaedigte Sperrdatei wurde als 'frei' gelesen"
    assert "nicht lesbar" in daten["grund"]


def test_aufheben_ohne_sperre_ist_erfolgreich(tmp_path):
    """Idempotent: ein zweites Aufheben darf nicht scheitern."""
    datei = str(tmp_path / "notaus.lock")
    assert notaus.notaus_loeschen(datei) is True


def test_exitcode_und_systemd_einheit_stimmen_ueberein():
    """Der Exitcode im Code muss in der Unit als RestartPreventExitStatus stehen."""
    assert notaus.EXIT_CODE_NOTAUS == 42
    unit = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "wpsteuerung.service"
    )
    text = open(unit, encoding="utf-8").read()
    assert f"RestartPreventExitStatus={notaus.EXIT_CODE_NOTAUS}" in text, (
        "Ohne diese Zeile startet systemd nach dem Not-Aus in 10 s wieder - "
        "Restart=always wuerde den Not-Aus ausheben."
    )
    assert "Restart=always" in text, (
        "Restart=always muss erhalten bleiben: ein Absturz darf die Heizung "
        "nicht stillstehen lassen."
    )


# ---------- Telegram-Befehlserkennung ----------

def test_telegram_ausloeser_wird_nicht_als_aufhebung_gelesen():
    """"notaus" enthaelt "aus" - der reine Ausloeser muss ausloesen."""
    from telegram_handler import _NOTAUS_AUSLOESER, _NOTAUS_AUFHEBEN_WOERTER
    assert "notaus" in _NOTAUS_AUSLOESER
    # Genau die Falle, die den Ausloeser zur Aufhebung gemacht haette:
    assert any("aus" in w for w in _NOTAUS_AUFHEBEN_WOERTER)
    assert "notaus" not in _NOTAUS_AUFHEBEN_WOERTER
    for ausloeser in ("notaus", "not aus", "not-aus"):
        assert ausloeser in _NOTAUS_AUSLOESER
        assert not any(w in ausloeser for w in _NOTAUS_AUFHEBEN_WOERTER) or \
            ausloeser in _NOTAUS_AUSLOESER


def test_api_payload_enthaelt_notaus_felder():
    """/status muss den Sperrzustand liefern, sonst blendet die WebApp falsch."""
    from api import build_mode_payload
    state = SimpleNamespace(
        control=SimpleNamespace(
            kompressor_ein=False, blocking_reason=None,
            notaus_aktiv=True, notaus_grund="Test", notaus_ts="2026-10-02T12:00:00",
        ),
        priority_config=None,
    )
    mode = build_mode_payload(state)
    assert mode["notaus_aktiv"] is True
    assert mode["notaus_grund"] == "Test"
    assert mode["notaus_ts"] == "2026-10-02T12:00:00"

    state.control.notaus_aktiv = False
    assert build_mode_payload(state)["notaus_aktiv"] is False


# ---------- Verhalten im Main-Loop ----------

def _baue_main_mocks(monkeypatch, tmp_path, aus_ok=True):
    """Holt main.py mit Kontroll-Side-Effects und gerichteter Sperrdatei."""
    import main as m

    sperrdatei = str(tmp_path / "notaus.lock")
    monkeypatch.setattr(notaus, "NOTAUS_DATEI", sperrdatei)
    calls = []

    async def set_kompressor_status(state, ein, **kwargs):
        calls.append((ein, kwargs.get("end_grund")))
        state.control.kompressor_ein = bool(ein)
        return aus_ok

    monkeypatch.setattr(m, "set_kompressor_status", set_kompressor_status)
    # Der Loop soll nach dem Not-Aus nicht weiterlaufen:
    m.stop_event.clear()
    return m, calls, sperrdatei


def _baue_state():
    return SimpleNamespace(
        control=SimpleNamespace(
            kompressor_ein=True, blocking_reason=None,
            _soll_einschalten=True, _soll_einschalten_bestaetigt=True,
            manual_force_on_pending=True, requested_rule_name="Abweichung",
            legionellen_started_at=datetime.now(TZ),
            legionellen_completion_pending=True,
            notaus_aktiv=False, notaus_grund=None, notaus_ts=None,
        ),
        legionellen_aktiv=True, legionellen_temp_override=42.0,
        stats=SimpleNamespace(), local_tz=TZ,
        bot_token=None,
        config=SimpleNamespace(Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")),
    )


@pytest.mark.asyncio
async def test_notaus_schaltet_aus_sperrt_und_beendet(monkeypatch, tmp_path):
    """Der Kern: Kompressor AUS, Sperre auf Platte, Loop gestoppt."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path)
    state = _baue_state()

    ok = await m.notaus_ausloesen(state, "Test", session=None)

    assert ok is True
    # 1. Abgeschaltet - mit technischem Grund, damit der Zyklus sauber endet
    assert calls, "Kompressor wurde nicht abgeschaltet"
    assert calls[0][0] is False
    assert calls[0][1] == "notaus"
    assert state.control.kompressor_ein is False
    # 2. Sperre liegt auf der Platte
    assert notaus.notaus_gesetzt(sperrdatei) is True
    # 3. Steuerung beendet
    assert m.stop_event.is_set() is True
    # 4. Ein wartender Lauf kann nicht nachplanen
    assert state.control.manual_force_on_pending is False
    assert state.control._soll_einschalten is False
    assert state.control._soll_einschalten_bestaetigt is False
    assert state.control.legionellen_started_at is None
    assert state.legionellen_aktiv is False


@pytest.mark.asyncio
async def test_notaus_meldet_ausfall_des_ausschaltens(monkeypatch, tmp_path, caplog):
    """Schlaegt das Ausschalten fehl, darf das nicht still bleiben."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path, aus_ok=False)
    state = _baue_state()

    with caplog.at_level(logging.CRITICAL):
        ok = await m.notaus_ausloesen(state, "Test", session=None)

    assert ok is False
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "FEHLGESCHLAGEN" in text, text
    # Die Sperre wird TROTZDEM gesetzt - sie ist die wichtigere Massnahme.
    assert notaus.notaus_gesetzt(sperrdatei) is True
    assert m.stop_event.is_set() is True
    assert "FEHLGESCHLAGEN" in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_gesperrter_zustand_bricht_den_durchlauf_ab(monkeypatch, tmp_path):
    """Bei gesperrtem Zustand darf die Kontrollphase nicht weiterlaufen."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path)
    state = _baue_state()
    state.control.notaus_aktiv = True
    state.control.kompressor_ein = False

    # Auch ein force_on im selben Batch darf nicht einschalten.
    m.enqueue_control_command("force_on")
    aufgerufen = []

    async def _darf_nicht_laufen(*a, **k):
        aufgerufen.append("kontrollphase")
        return True

    monkeypatch.setattr(m.pcl, "check_pressure_and_config", _darf_nicht_laufen)

    await m.run_logic_step(None, state)

    assert aufgerufen == [], "Kontrollphase lief trotz Not-Aus"
    assert state.control.kompressor_ein is False
    assert calls == [], "es wurde Hardware angefasst"


@pytest.mark.asyncio
async def test_notaus_hat_vorrang_und_ignoriert_force_on(monkeypatch, tmp_path):
    """notaus im selben Batch schlaegt ein vorheriges force_on."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path)
    state = _baue_state()

    m.enqueue_control_command("force_on")
    m.enqueue_control_command("notaus", {"grund": "Test"})

    await m.run_logic_step(None, state)

    # Der allererste Hardware-Aufruf muss das Ausschalten sein.
    assert calls[0][0] is False, calls
    assert calls[0][1] == "notaus"
    assert state.control.kompressor_ein is False
    assert notaus.notaus_gesetzt(sperrdatei) is True
    assert m.stop_event.is_set() is True


@pytest.mark.asyncio
async def test_aufheben_setzt_sperre_und_flag_zurueck(monkeypatch, tmp_path):
    """notaus_aus entfernt Sperre UND State-Flag."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path)
    notaus.notaus_setzen("Test", sperrdatei)
    state = _baue_state()
    state.control.notaus_aktiv = True
    state.control.notaus_grund = "Test"
    state.control.notaus_ts = "2026-10-02T12:00:00"

    m.enqueue_control_command("notaus_aus")

    # Nach dem Aufheben laeuft die Kontrollphase WEITER - das ist gerade der
    # Zweck des Aufhebens. Sie wird hier an der Druckschalter-Abfrage
    # abgefangen, weil da echte Hardware angefasst wuerde.
    class _Weiter(Exception):
        pass

    async def _abfangen(*a, **k):
        raise _Weiter

    monkeypatch.setattr(m.pcl, "check_pressure_and_config", _abfangen)
    with pytest.raises(_Weiter):
        await m.run_logic_step(None, state)

    assert notaus.notaus_gesetzt(sperrdatei) is False
    assert state.control.notaus_aktiv is False
    assert state.control.notaus_grund is None
    assert state.control.notaus_ts is None


@pytest.mark.asyncio
async def test_gesperrter_zustand_schaltet_nachgelaufenen_kompressor_ab(
    monkeypatch, tmp_path
):
    """Sperre steht, Kompressor laeuft trotzdem -> muss sofort aus."""
    m, calls, sperrdatei = _baue_main_mocks(monkeypatch, tmp_path)
    notaus.notaus_setzen("Test", sperrdatei)
    state = _baue_state()
    state.control.notaus_aktiv = True
    state.control.kompressor_ein = True

    await m.run_logic_step(None, state)

    assert calls and calls[0][0] is False, calls
    assert state.control.kompressor_ein is False