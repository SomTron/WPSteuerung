"""Tests: der API-Fehlerdichte-Alarm loest tatsaechlich aus.

`check_api_health`, `track_api_error` und `state.api_errors` waren
vollstaendig vorhanden - der einzige Schreiber wurde aber nie
aufgerufen. Die Schleife lief alle 10 Sekunden ueber eine Struktur,
die dauerhaft leer blieb. Ihr Docstring versprach:

    "Bei mehr als 10 Fehlern pro API wird eine Warnung via Telegram
     gesendet"

Das war nicht wahr. Der Alarm konnte nicht ausloesen.

Hier wird der Weg end-to-end geprueft: Fehler sammeln -> Schwelle
erreichen -> Telegram-Meldung. Nur so ist belegt, dass die Verdrahtung
etwas tut.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import re

import pytest

import main as m
from constants import API_FEHLER_SCHWELLE, API_FEHLER_SCHWELLE_DEFAULT


def _state():
    return SimpleNamespace(api_errors={}, bot_token="token", chat_id=4711,
                           local_tz=None)


def _fuettere(anzahl, api_name="open-meteo", fehlertyp="keine_daten"):
    state = _state()
    for _ in range(anzahl):
        m.track_api_error(state, api_name, fehlertyp)
    return state


def _haenge_senden(monkeypatch, uhr):
    """Kleiner Telegram-Fake; gibt die gesendeten Texte zurueck."""
    gesendet = []

    async def fake_send(session, chat_id, text, token, **kw):
        gesendet.append((chat_id, text))
        return True

    monkeypatch.setattr("telegram_api.send_telegram_message", fake_send)
    monkeypatch.setattr(m, "_state_now", lambda s: uhr["jetzt"])
    return gesendet


# ---------- Sammeln ----------

def test_track_api_error_sammelt_je_api():
    state = _state()
    m.track_api_error(state, "solax", "timeout")
    m.track_api_error(state, "solax", "timeout")
    m.track_api_error(state, "open-meteo", "keine_daten")

    assert len(state.api_errors["solax"]["errors"]) == 2
    assert len(state.api_errors["open-meteo"]["errors"]) == 1
    assert state.api_errors["solax"]["errors"][0][1] == "timeout"


def test_track_api_error_erhaelt_sich_ohne_api_errors():
    """Der Aufrufer darf nie an der Fehlererfassung scheitern.

    Ohne das bricht ein Fehler beim Solax-Refresh oder in der Prognose
    ab - aus einem aufgezeichneten Fehler wird ein geworfener.
    """
    state = SimpleNamespace(local_tz=None)   # kein api_errors
    m.track_api_error(state, "solax", "timeout")
    assert len(state.api_errors["solax"]["errors"]) == 1


def test_track_api_error_begrenzt_den_speicher():
    state = _state()
    for _ in range(150):
        m.track_api_error(state, "solax", "timeout")
    assert len(state.api_errors["solax"]["errors"]) <= 100


# ---------- Schwellen ----------

def test_schwellen_sind_je_api_unterschiedlich():
    """Der Abruftakt liegt eine Groessenordnung auseinander.

    Solax fragt jede Minute ab, open-meteo hoechstens alle 15 Minuten.
    Eine gemeinsame Zahl koennte open-meteo nie erreichen.
    """
    assert API_FEHLER_SCHWELLE["solax"] != API_FEHLER_SCHWELLE["open-meteo"]
    # open-meteo: hoechstens 2 Versuche in 30 Minuten. Alles darueber waere
    # unerreichbar, der Alarm koennte nie feuern.
    assert API_FEHLER_SCHWELLE["open-meteo"] <= 2
    # Solax: hoechstens 30 Versuche in 30 Minuten; die Schwelle muss
    # darunter bleiben und zugleich ueber den 15 Minuten liegen, ab denen
    # _melde_solar_ausfall ohnehin alarmiert.
    assert 15 < API_FEHLER_SCHWELLE["solax"] <= 30


def test_solax_schwelle_kommt_nach_dem_stale_alarm():
    """Sonst reden zwei Alarme ueber dieselbe Stoerung.

    _melde_solar_ausfall alarmiert ab 15 Minuten Unfrische. Bei 25
    Fehlern je Minute faellt der Dichte-Alarm danach und ergaenzt um
    die Fehlerarten.
    """
    assert API_FEHLER_SCHWELLE["solax"] > 15


# ---------- Der Alarm loest wirklich aus ----------

@pytest.mark.asyncio
async def test_alarm_bei_ueberschrittener_schwelle(monkeypatch):
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _fuettere(API_FEHLER_SCHWELLE["open-meteo"])
    await m.check_api_health(None, state)

    assert gesendet, "der Alarm hat nicht ausgeloest"
    assert "open-meteo" in gesendet[0][1]
    assert "keine_daten: 2x" in gesendet[0][1]


@pytest.mark.asyncio
async def test_kein_alarm_unterhalb_der_schwelle(monkeypatch):
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _fuettere(API_FEHLER_SCHWELLE["open-meteo"] - 1)
    await m.check_api_health(None, state)

    assert not gesendet, f"Alarm trotz Unterschreitung: {gesendet}"


@pytest.mark.asyncio
async def test_alarm_wird_je_api_stund_once_gesendet(monkeypatch):
    """Ohne Drosselung fluten 25 Solax-Fehler 25 Telegram-Nachrichten."""
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _fuettere(API_FEHLER_SCHWELLE["solax"])
    await m.check_api_health(None, state)
    await m.check_api_health(None, state)
    await m.check_api_health(None, state)

    assert len(gesendet) == 1, f"{len(gesendet)} Nachrichten in 60 s"


@pytest.mark.asyncio
async def test_alarm_nach_60_minuten_wieder(monkeypatch):
    """Ein langer Ausfall muss sichtbar bleiben.

    Die Wiederholung setzt voraus, dass weiter Fehler eintreffen -
    das ist der reale Fall. Ohne neue Fehler waeren die alten nach 30
    Minuten aus dem Fenster heraus und der Alarm zu Recht still.
    """
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _fuettere(API_FEHLER_SCHWELLE["open-meteo"])
    await m.check_api_health(None, state)
    assert len(gesendet) == 1

    uhr["jetzt"] = uhr["jetzt"] + timedelta(minutes=61)
    for _ in range(API_FEHLER_SCHWELLE["open-meteo"]):
        m.track_api_error(state, "open-meteo", "keine_daten")
    await m.check_api_health(None, state)
    assert len(gesendet) == 2, "keine Wiederholung nach 60 min"


@pytest.mark.asyncio
async def test_keine_wiederholung_ohne_neue_fehler(monkeypatch):
    """Nach 61 Minuten ohne neue Fehler: die alten sind aus dem Fenster."""
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _fuettere(API_FEHLER_SCHWELLE["open-meteo"])
    await m.check_api_health(None, state)

    uhr["jetzt"] = uhr["jetzt"] + timedelta(minutes=61)
    await m.check_api_health(None, state)
    assert len(gesendet) == 1, "veraltete Fehler haben erneut ausgeloest"


@pytest.mark.asyncio
async def test_fehler_aelter_als_30_min_zaehlen_nicht(monkeypatch):
    """Ein alter Aussetzer darf keinen Alarm ausloesen."""
    uhr = {"jetzt": datetime(2026, 10, 4, 12, 30)}
    gesendet = _haenge_senden(monkeypatch, uhr)

    state = _state()
    alt = uhr["jetzt"] - timedelta(minutes=45)
    state.api_errors["solax"] = {
        "errors": [(alt, "timeout")] * API_FEHLER_SCHWELLE["solax"],
        "last_alert": None,
    }
    await m.check_api_health(None, state)
    assert not gesendet, "Fehler von vor 45 min haben ausgeloest"


@pytest.mark.asyncio
async def test_fehlende_sammlung_bricht_den_check_nicht():
    """check_api_health laeuft im 10-s-Takt und darf das nie unterbrechen."""
    state = SimpleNamespace(local_tz=None)   # kein api_errors
    await m.check_api_health(None, state)     # darf keine Ausnahme werfen


def test_unbekannte_api_bekommt_den_fallback():
    """Eine neue API darf nicht still stumm bleiben."""
    assert isinstance(API_FEHLER_SCHWELLE_DEFAULT, int)
    assert API_FEHLER_SCHWELLE_DEFAULT > 0


# ---------- Die Verdrahtung selbst ----------
#
# Die Tests oben pruefen den Alarm-Mechanismus. Sie merken aber nicht,
# wenn jemand die Verdrahtung entfernt: per Mutation bestaetigt - das
# Herausnehmen des track_api_error-Aufrufs liess alle Tests gruen.
# Genau so sah der Zustand vor diesem Commit aus, monatelang.

def _main_quelle():
    import os
    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "main.py")
    with open(pfad, encoding="utf-8") as fh:
        return fh.read()


def test_solax_refresh_meldet_seine_fehler():
    """Ohne diesen Aufruf bleibt die Sammlung dauerhaft leer.

    Genau das war der Zustand: track_api_error war vollstaendig da und
    wurde nie gerufen. Die Pruefung liest den Quelltext, weil sich der
    Fehler sonst nur durch einen echten Netzausfall zeigen wuerde.
    """
    quelle = _main_quelle()
    start = quelle.find("async def solar_refresh_loop(")
    assert start != -1, "solar_refresh_loop nicht gefunden"
    rumpf = quelle[start:quelle.find("\nasync def ", start + 10)]
    assert 'track_api_error(state, "solax", "timeout")' in rumpf, (
        "der Timeout-Pfad des Solax-Refresh meldet keinen Fehler - die "
        "Fehlersammlung bleibt leer und der Alarm kann nicht ausloesen"
    )
    assert 'track_api_error(state, "solax", "exception")' in rumpf, (
        "der Exception-Pfad des Solax-Refresh meldet keinen Fehler"
    )


def test_prognose_meldet_ihre_fehler():
    """open-meteo hatte ueberhaupt keinen Fehlerpfad.

    get_solar_forecast gibt bei Fehler None zurueck; der Aufrufer sah
    nur "keine Daten" und konnte einen Ausfall nicht von einem echten
    Wetterstand unterscheiden. Genau an dieser Verzweigung wird jetzt
    gemeldet.
    """
    quelle = _main_quelle()
    start = quelle.find("rad_today, rad_tomorrow, rad_day2")
    assert start != -1, "Prognose-Aufrufstelle nicht gefunden"
    rumpf = quelle[start:quelle.find("\nasync def ", start + 10)]
    assert 'track_api_error(state, "open-meteo"' in rumpf, (
        "der Fehlerpfad der Prognose meldet keinen Fehler - open-meteo "
        "faellt aus jeder Ueberwachung heraus"
    )


def test_wired_apis_stehen_auch_in_der_schwelle():
    """Jede verdrahtete API braucht eine eigene Schwelle.

    Sonst faellt eine neue Verdrahtung auf den Fallback zurueck, und
    bei einem 15-Minuten-Takt waere die Schwelle unerreichbar.
    """
    quelle = _main_quelle()
    verdrahtet = set(re.findall(r'track_api_error\(state, "([^"]+)"', quelle))
    assert verdrahtet, "keine API verdrahtet"
    fehlend = verdrahtet - set(API_FEHLER_SCHWELLE)
    assert not fehlend, (
        f"verdrahtet ohne eigene Schwelle, laeuft also auf den Fallback "
        f"({API_FEHLER_SCHWELLE_DEFAULT}) und koennte unerreichbar sein: "
        f"{sorted(fehlend)}"
    )