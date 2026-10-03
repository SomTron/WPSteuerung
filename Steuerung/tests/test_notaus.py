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


# ---------- Anleitung nach dem Ausloesen ----------

NEUSTART_HINWEIS = "systemctl restart wpsteuerung"


def _lade_quelle(dateiname):
    pfad = os.path.join(os.path.dirname(__file__), '..', '..', dateiname)
    with open(pfad, encoding='utf-8') as fh:
        return fh.read()


def test_telegram_meldung_erklaert_den_neustart():
    """Nach dem Not-Aus sind Telegram und WebApp offline.

    Der Bot laeuft im selben Prozess wie die WebApp; der Not-Aus
    beendet diesen Prozess. Eine Meldung, die bloss `notaus aus`
    verspricht, fuehrt in eine Sackgasse: der Befehl kann nicht
    abgesetzt werden, weil der Bot gar nicht mehr laeuft.
    """
    quelle = _lade_quelle('Steuerung/telegram_handler.py')
    idx = quelle.find('*Not-Aus ausgelöst.*')
    assert idx != -1, "Bestaetigungstext des Not-Aus nicht gefunden"
    abschnitt = quelle[idx:idx + 1200]
    # Der Aufheben-Hinweis muss den Neustart vorwegnehmen.
    assert NEUSTART_HINWEIS in abschnitt, (
        "Not-Aus-Meldung nennt keinen Dienstneustart - nach dem Ausloesen "
        "ist `notaus aus` aber gar nicht sendbar"
    )
    assert abschnitt.find(NEUSTART_HINWEIS) < abschnitt.find('danach'), (
        "Der Neustart muss vor dem Aufheben-Befehl stehen"
    )


def test_notaus_beenden_meldung_erklaert_den_neustart():
    """Gilt fuer die Meldung, die beim Beenden des Prozesses rausgeht."""
    quelle = _lade_quelle('Steuerung/main.py')
    idx = quelle.find('*NOT-AUS aktiv*')
    assert idx != -1, "Not-Aus-Meldung in main.py nicht gefunden"
    abschnitt = quelle[idx:idx + 1200]
    assert NEUSTART_HINWEIS in abschnitt, (
        "Beenden-Meldung nennt keinen Dienstneustart"
    )


def test_webapp_erklaert_dass_der_dienst_stirbt():
    """Die WebApp wird vom selben Prozess geliefert wie der Dienst.

    Nach `notaus` kann sie sich nicht mehr aktualisieren; der Knopf
    muss das sagen, sonst wartet der Nutzer vergeblich auf eine
    gruene Anzeige.
    """
    quelle = _lade_quelle('webapp/index.html')
    idx = quelle.find('async function sendControl')
    assert idx != -1
    abschnitt = quelle[idx:idx + 2500]
    assert 'notaus' in abschnitt
    assert NEUSTART_HINWEIS in abschnitt, (
        "WebApp meldet den Not-Aus ohne Hinweis auf den Dienstneustart"
    )
    # Und sie darf den Statusabruf nicht erzwingen, der ohnehin
    # fehlschlagen wuerde und die Fehlermeldung ueberschreiben wuerde.
    assert "if (command === 'notaus') return;" in abschnitt, (
        "Nach dem Not-Aus darf kein fetchStatus mehr die Fehlermeldung ueberholen"
    )


# ---------- Der Not-Aus muss das laufende main erreichen ----------

def test_laufendes_main_ist_nicht_das_zweite_modul(monkeypatch):
    """Regression, im echten Betrieb auf dem Pi aufgetreten.

    main.py startet ueber `python main.py` und laeuft damit als
    `__main__`. Ein blasses `import main` laedt die Datei ein zweites Mal
    und liefert ein zweites Modulobjekt - mit eigener
    ``control_command_queue``. Der Not-Aus landet in dieser_queue und
    wird von niemandem abgeholt.

    Sichtbar wurde das so: Telegram meldete "Not-Aus ausgeloest",
    `NOT-AUS ausgeloest` fehlte im Log, die Sperrdatei entstand nicht,
    der blieb aktiv.
    """
    import sys
    import types

    import telegram_handler

    empfaenger = []
    laufend = types.ModuleType("__main__")
    laufend.enqueue_control_command = lambda c, p=None: empfaenger.append(c)

    doppelte = types.ModuleType("main")
    doppelte.enqueue_control_command = lambda c, p=None: empfaenger.append("FALSCH")

    monkeypatch.setitem(sys.modules, "__main__", laufend)
    monkeypatch.setitem(sys.modules, "main", doppelte)

    modul = telegram_handler._laufendes_main()
    assert modul is laufend, (
        "es wurde das zweite main-Modul gewaehlt - der Befehl landet in "
        "einer Queue, die niemand abholt"
    )
    modul.enqueue_control_command("notaus", {"grund": "Telegram"})
    assert empfaenger == ["notaus"], empfaenger


def test_laufendes_main_faellt_zurueck_ohne_main_funktion(monkeypatch):
    """Ohne __main__ mit der Funktion greift der regulaere Import."""
    import sys
    import types

    import telegram_handler

    laufend = types.ModuleType("__main__")  # ohne enqueue_control_command
    modul = types.ModuleType("main")
    modul.enqueue_control_command = lambda c, p=None: None

    monkeypatch.setitem(sys.modules, "__main__", laufend)
    monkeypatch.setitem(sys.modules, "main", modul)
    assert telegram_handler._laufendes_main() is modul


def test_telegram_benutzt_kein_blasses_import_main():
    """Vertragstest: kein `import main as` mehr im Handler."""
    pfad = os.path.join(os.path.dirname(__file__), '..', 'telegram_handler.py')
    with open(pfad, encoding='utf-8') as fh:
        text = fh.read()
    assert 'import main as ' not in text, (
        "Ein blasses 'import main' laedt ein zweites Modul mit eigener Queue"
    )
    assert '_laufendes_main()' in text


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

def test_ausloeser_braucht_absicht_nicht_nur_erwaehnung():
    """Regression: eine Frage nach dem Befehl darf ihn nicht ausloesen.

    Der Not-Aus haelt die Sperre bis zu einem von Hand gesetzten Reset und
    haelt dabei auch Legionelle und Notfallschutz an. Ein zufaellig
    ausgeloester Not-Aus wuerde die Heizung stilllegen, ohne dass der
    Betreiber es bemerkt. Ein absichtlich gesendeter Befehl muss dagegen
    unkompliziert durchgehen.
    """
    from telegram_handler import (
        _NOTAUS_AUFHEBEN_WOERTER,
        _NOTAUS_AUSLOESER,
        _ist_notaus_ausloeser,
    )

    for befehl in ("notaus", "not aus", "not-aus", "🛑", "stopp"):
        assert _ist_notaus_ausloeser(befehl) is True, befehl

    for text in ("was bedeutet notaus",
                 "ok danke, nicht notaus",
                 "ich habe den notaus benutzt",
                 "notaus funktioniert nicht",
                 "notaus bitte"):
        assert _ist_notaus_ausloeser(text) is False, text

    # "notaus" enthaelt selbst "aus" - genau die Falle, die den Ausloeser
    # zur Aufhebung gemacht haette.
    assert any("aus" in w for w in _NOTAUS_AUFHEBEN_WOERTER)
    assert "notaus" in _NOTAUS_AUSLOESER
def test_webapp_knoepfe_senden_erlaubte_befehle():
    """Regression: der Not-Aus-Knopf in der WebApp tat nichts.

    `ControlCommand.command_must_be_allowed` prueft gegen `ALLOWED_COMMANDS`.
    `notaus` stand dort nicht drin, also lehnte die API den Aufruf mit HTTP
    422 ab - der Knopf blieb ohne Wirkung, waehrend Telegram (das die API
    umgeht) funktionierte. Der Test koppelt beide Seiten: was die WebApp
    sendet, muss auch erlaubt sein.
    """
    import re

    from api import ALLOWED_COMMANDS, ControlCommand

    html = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "webapp", "index.html",
    )
    text = open(html, encoding="utf-8").read()
    gesendet = set(re.findall(r"sendControl\('([a-z_]+)'", text))

    assert gesendet, "keine sendControl-Aufrufe in der WebApp gefunden"
    for befehl in gesendet:
        assert befehl in ALLOWED_COMMANDS, (
            f"WebApp sendet '{befehl}', das erlaubt ist nicht: "
            f"{sorted(ALLOWED_COMMANDS)}"
        )
        params = None
        if befehl == "notaus":
            params = {"grund": "WebApp"}
        elif befehl == "set_mode":
            params = {"mode": "bademodus", "active": True}
        ControlCommand(command=befehl, params=params)

    assert {"notaus", "notaus_aus"} <= gesendet, (
        "WebApp bietet den Not-Aus nicht an"
    )


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