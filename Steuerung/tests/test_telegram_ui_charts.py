"""Tests fuer die befehlsgetriebenen Telegram-Module.

Warum wichtig: `telegram_ui` und `telegram_charts` reagieren nur auf
Befehle. Ihre Fehler fallen deshalb ausschliesslich auf dem Pi auf - im
Steuerungslog und in CI tauchen sie nicht auf. Ein Tastaturfehler oder ein
falsches Tickerformat wuerde beim Nutzer schlicht "nichts passiert" zeigen.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

import telegram_charts  # noqa: E402
import telegram_ui  # noqa: E402


def state_mit(urlaub=False, bade=False):
    return SimpleNamespace(
        urlaubsmodus_aktiv=urlaub,
        bademodus_aktiv=bade,
        chat_id="42",
        bot_token="token",
        local_tz=None,
    )


def tastatur_knoepfe(state):
    tasten = telegram_ui.get_keyboard(state)["keyboard"]
    return [taste for zeile in tasten for taste in zeile]


# --------------------------------------------------------------- Tastatur


def test_tastatur_zeigt_grundzustand():
    knoepfe = tastatur_knoepfe(state_mit())
    assert "🌴 Urlaub" in knoepfe
    assert "🛁 Bademodus" in knoepfe


def test_tastatur_zeigt_urlaub_ende_wenn_aktiv():
    knoepfe = tastatur_knoepfe(state_mit(urlaub=True))
    assert "🌴 Urlaub Ende" in knoepfe
    assert "🌴 Urlaub" not in knoepfe


def test_tastatur_zeigt_bademodus_aus_wenn_aktiv():
    knoepfe = tastatur_knoepfe(state_mit(bade=True))
    assert "🛁 Bademodus aus" in knoepfe
    assert "🛁 Bademodus" not in knoepfe


def test_tastatur_bleibt_vollstaendig():
    """Auch in jedem Modus muessen alle Aktionen erreichbar bleiben."""
    for kwargs in ({}, {"urlaub": True}, {"bade": True}, {"urlaub": True, "bade": True}):
        knoepfe = tastatur_knoepfe(state_mit(**kwargs))
        assert len(knoepfe) == 8, f"unvollstaendige Tastatur bei {kwargs}"
        assert "🆘 Hilfe" in knoepfe
        assert "⏱️ Laufzeiten" in knoepfe


def test_tastatur_optionen_sind_korrekt():
    tastatur = telegram_ui.get_keyboard(state_mit())
    assert tastatur["resize_keyboard"] is True
    assert tastatur["one_time_keyboard"] is False


# ---------------------------------------------------------- Formathelfer


def test_escape_markdown_maskiert_alle_zeichen():
    """MarkdownV1 bricht bei _, *, ` und [ ab - unescaped kommen als
    Telegram-API-Fehler zurueck."""
    maskiert = telegram_ui.escape_markdown("a_b*c`d[e")
    assert maskiert == "a\\_b\\*c\\`d\\[e"


def test_escape_markdown_behaelt_umlaute():
    maskiert = telegram_ui.escape_markdown("Größe Öffnung Über")
    assert "Größe" in maskiert
    assert "\\" not in maskiert


def test_escape_markdown_ohne_sonderzeichen():
    assert telegram_ui.escape_markdown("Normaltext 123") == "Normaltext 123"


def test_escape_markdown_wandelt_nicht_string():
    assert telegram_ui.escape_markdown(42) == "42"
    assert telegram_ui.escape_markdown(None) == "None"


@pytest.mark.parametrize(
    "wert,erwartet",
    [
        (0, "0h 0m"),
        (59, "0h 0m"),
        (60, "0h 1m"),
        (3600, "1h 0m"),
        (3900, "1h 5m"),
        (7325, "2h 2m"),
        ("7200", "2h 0m"),
        (timedelta(hours=1, minutes=30), "1h 30m"),
    ],
)
def test_format_time(wert, erwartet):
    assert telegram_ui.format_time(wert) == erwartet


@pytest.mark.parametrize("wert", ["keine Zahl", None, [], {}])
def test_format_time_ueberlebt_muell(wert):
    """Ein unerwarteter Wert darf keine Exception in der Befehlskette
    erzeugen - der Nutzer bekaeme sonst gar keine Antwort."""
    assert telegram_ui.format_time(wert) == "0h 0m"


def test_fmt_temp_unterscheidet_none():
    assert telegram_ui.fmt_temp(None) == "N/A"
    assert telegram_ui.fmt_temp(0.0) == "0.0°C"
    assert telegram_ui.fmt_temp(45.67) == "45.7°C"


# ------------------------------------------------------------- Charts


@pytest.mark.asyncio
async def test_chart_meldet_fehlende_csv_statt_absturz(monkeypatch, tmp_path):
    """Ohne CSV muss eine Nachricht kommen, kein Traceback."""
    gesendet = []

    async def fake_send(session, chat_id, msg, token, **kwargs):
        gesendet.append(msg)
        return True

    monkeypatch.setattr(telegram_charts, "send_telegram_message", fake_send)
    monkeypatch.setattr(
        telegram_charts, "HEIZUNGSDATEN_CSV", str(tmp_path / "weg.csv")
    )

    state = state_mit()
    await telegram_charts.get_boiler_temperature_history(None, 6, state, None)

    assert len(gesendet) == 1
    assert "nicht gefunden" in gesendet[0]


def test_pandas_wird_nicht_beim_import_geladen():
    """RAM-Schutz: pandas/matplotlib duerfen erst im Aufruf geladen werden.

    Am 15.09. hat ein pandas-Import beim Start (~70 MB RSS) mit zum
    OOM-Kill beigetragen.
    """
    quelle = (STEUERUNG / "telegram_charts.py").read_text(encoding="utf-8")
    zeilen = quelle.splitlines()
    top_level = [
        z for z in zeilen
        if z.startswith("import pandas")
        or z.startswith("import matplotlib")
        or z.startswith("from matplotlib")
    ]
    assert not top_level, f"pandas/matplotlib am Modul-Anfang: {top_level}"
    assert "def _pandas(" in quelle or "def _matplotlib(" in quelle
