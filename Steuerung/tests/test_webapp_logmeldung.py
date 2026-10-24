# -*- coding: utf-8 -*-
"""Langsame Antworten und WebApp-Fehler muessen im Steuerungs-Log stehen.

Befund 23.10.2026: `/status` brauchte 19,7 s, die WebApp bricht nach 8 s ab
(STATUS_TIMEOUT_MS) und zwar stumm (AbortError wird nicht in die Konsole
geschrieben). Im Log stand nichts - der Dienst sah gesund aus, waehrend die
Oberflaeche leer blieb.
"""
import logging
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402


@pytest.fixture(autouse=True)
def _drossel_zuruecksetzen():
    api._letzte_meldung.clear()
    yield
    api._letzte_meldung.clear()


# --- Langsame Antwort --------------------------------------------------------

def test_langsame_antwort_wird_gemeldet(caplog):
    with caplog.at_level(logging.WARNING):
        api._melde_langsame_antwort("/status", 19700.0)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "/status" in text
    assert "19.7" in text, f"die Dauer fehlt in der Meldung: {text}"
    # Die Grenze kommt aus der Konstante (24.10.2026: 8 -> 12 s). Ein fester
    # Wert im Test wuerde die Kopplung an die WebApp nicht absichern.
    grenze = f"{api.WEBAPP_STATUS_TIMEOUT_SEC:.0f} s"
    assert grenze in text, (
        f"der WebApp-Abbruch ({grenze}) muss in der Meldung stehen: {text}"
    )


def test_schnelle_antwort_wird_nicht_gemeldet(caplog):
    with caplog.at_level(logging.WARNING):
        api._melde_langsame_antwort("/status", 1200.0)
    assert not caplog.records, "eine schnelle Antwort darf das Log nicht fuellen"


def test_langsame_antwort_wird_gedrosselt(caplog):
    """Ein dauerhaft langsamer Poll (alle 5 s) darf das Log nicht fluten."""
    with caplog.at_level(logging.WARNING):
        api._melde_langsame_antwort("/status", 9000.0)
        api._melde_langsame_antwort("/status", 9000.0)
        api._melde_langsame_antwort("/status", 9000.0)
    assert len(caplog.records) == 1


# --- Fehlermeldung aus der WebApp -------------------------------------------

def test_clientfehler_wird_geloggt(caplog):
    with caplog.at_level(logging.WARNING):
        antwort = api.client_fehler(
            api.ClientFehler(meldung="Cannot read properties of null",
                             typ="JS-Fehler", seite="/", version="1.10.26")
        )
    assert antwort["ok"] is True and antwort["geloggt"] is True
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "WebApp-Fehler" in text
    assert "Cannot read properties of null" in text
    assert "1.10.26" in text


def test_clientfehler_wird_gedrosselt(caplog):
    fehler = api.ClientFehler(meldung="gleicher Fehler", typ="JS-Fehler")
    with caplog.at_level(logging.WARNING):
        erste = api.client_fehler(fehler)
        zweite = api.client_fehler(fehler)
    assert erste["geloggt"] is True
    assert zweite["geloggt"] is False and zweite["grund"] == "gedrosselt"
    assert len(caplog.records) == 1


def test_clientfehler_begrenzt_laenge():
    """Ein fehlerhafter Client darf das Log nicht mit Romanen fuellen."""
    with pytest.raises(Exception):
        # Pydantic weist zu lange Meldungen ab (max_length=500)
        api.ClientFehler(meldung="x" * 5000)


def test_clientfehler_ohne_inhalt_crasht_nicht(caplog):
    with caplog.at_level(logging.WARNING):
        antwort = api.client_fehler(api.ClientFehler())
    assert antwort["ok"] is True
    assert "ohne Meldung" in " ".join(r.getMessage() for r in caplog.records)


# --- Single-Flight: keine parallele Lawine ----------------------------------

def test_auswertung_laeuft_nur_einmal_gleichzeitig():
    """Nach einem Neustart sind die Caches kalt - die WebApp pollt alle 5 s.

    Ohne Single-Flight rechnen mehrere Anfragen dieselbe Auswertung
    gleichzeitig (gemessen: 10,7 s, WebApp bricht nach 8 s ab).
    """
    assert api._auswertung_starten() is True
    try:
        assert api._auswertung_starten() is False, "zweite Anfrage rechnet parallel"
        assert api._auswertung_starten() is False
    finally:
        api._auswertung_beenden()
    # Nach der Freigabe darf wieder genau einer rechnen.
    assert api._auswertung_starten() is True
    api._auswertung_beenden()


def test_vorwaermen_erzeugt_keine_falsche_langsam_warnung(caplog, monkeypatch):
    """Beim Vorwaermen ist eine lange Dauer normal - keine WARNING."""
    monkeypatch.setattr(api, "_WAERMT_VOR", True)
    with caplog.at_level(logging.WARNING):
        api._melde_langsame_antwort("/status", 9000.0)
    assert not caplog.records


# --- /status rechnet nie selbst ---------------------------------------------

def test_gecachtes_profil_liefert_none_wenn_kalt(tmp_path):
    """/status darf die CSV-Auswertung nicht ausloesen, sondern nur lesen."""
    import pv_profil

    p = tmp_path / "profil.csv"
    p.write_text("Zeitstempel,PowerDC1\n", encoding="utf-8")
    pv_profil._cache.clear()

    assert pv_profil.hole_gecachtes_profil(forecast_scaling=1.0, csv_path=str(p)) is None

    profil = pv_profil.berechne_profil(str(p), force_refresh=True)
    assert pv_profil.hole_gecachtes_profil(forecast_scaling=None, csv_path=str(p)) == profil


def test_gecachtes_wh_qm_liefert_none_wenn_kalt(tmp_path, monkeypatch):
    import pytz
    from datetime import datetime as dt
    from types import SimpleNamespace

    monkeypatch.setattr(
        api, "shared_state",
        SimpleNamespace(local_tz=pytz.timezone("Europe/Berlin"), clock=None),
    )
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None
    assert api._historisches_wh_qm_gecacht(str(tmp_path / "x.csv")) is None

    api._HIST_WH_QM_CACHE["zeit"] = api.to_naive(dt.now())
    api._HIST_WH_QM_CACHE["wert"] = 1234.0
    assert api._historisches_wh_qm_gecacht(str(tmp_path / "x.csv")) == 1234.0

    # Zu alt -> wieder None (dann rechnet der Hintergrund)
    assert api._historisches_wh_qm_gecacht(
        str(tmp_path / "x.csv"), max_alter_sek=-1
    ) is None
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None


# --- Cache warm halten -------------------------------------------------------

async def test_cache_loop_waermt_sofort_und_dann_zyklisch(monkeypatch):
    """Der Loop muss beim Start vorwaermen UND die 30-Minuten-TTL unterlaufen.

    Sonst zahlt der zufaellig naechste Request die Auswertung (gemessen:
    4,4 s statt 1,3 s - und nach einem Neustart bis 10,7 s).
    """
    import asyncio

    import main as m

    aufrufe = []
    monkeypatch.setattr(m, "erwaerme_auswertungen", lambda: aufrufe.append(1))
    monkeypatch.setattr(m, "AUSWERTUNG_CACHE_REFRESH_SEC", 0.01)

    task = asyncio.create_task(m.auswertung_cache_loop())
    try:
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(aufrufe) >= 2, f"Loop waermt nicht zyklisch vor: {aufrufe}"
