# -*- coding: utf-8 -*-
"""Code-Stand (Git-Revision) in allen Logs (Nutzerwunsch 18.10.2026).

Anlass: Entscheidungslog und Zyklus-CSV werden monatlich rotiert und mischen
damit mehrere Code-Staende in EINER Datei. Ohne Revisionsangabe ist spaeter
nicht entscheidbar, welcher Fix schon gelaufen ist - genau das erschwerte die
Auswertung des Journals vom 18.10.2026.

Abgedeckt:
  * `code_revision` liefert Hash bzw. "unbekannt" (Robust-Fallback),
  * Umgebungsvariable `WPS_GIT_REVISION` hat Vorrang,
  * jede Entscheidungslog-Zeile traegt `rev`,
  * jede Zyklus-CSV-Zeile traegt `rev` (und Bestandszeilen bleiben leer).
"""
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import code_revision  # noqa: E402
import cycle_logging  # noqa: E402
import entscheidungs_log as el  # noqa: E402


@pytest.fixture(autouse=True)
def _frischer_cache():
    """Cache je Test leeren: er wird pro Prozess nur einmal gefuellt."""
    code_revision.reset_cache()
    yield
    code_revision.reset_cache()
    os.environ.pop("WPS_GIT_REVISION", None)


# ── Ermittlung ──────────────────────────────────────────────────────────

def test_kennung_ist_hash_oder_unbekannt():
    kennung = code_revision.kennung()
    assert isinstance(kennung, str) and kennung
    if kennung != code_revision.UNBEKANNT:
        # kurz-hash, optional mit "+" fuer lokale python-Aenderungen
        assert re.match(r"^[0-9a-f]{7,40}\+?$", kennung), kennung


def test_startup_text_haelt_das_journalformat():
    text = code_revision.startup_text()
    assert text
    if text != code_revision.UNBEKANNT:
        assert re.match(r"^[0-9a-f]{7,40} \[.+\]", text), text


def test_umgebungsvariable_hat_vorrang(monkeypatch):
    monkeypatch.setenv("WPS_GIT_REVISION", "deadbee123")
    code_revision.reset_cache()
    assert code_revision.kurz_hash() == "deadbee123"
    assert code_revision.kennung().startswith("deadbee123")


def test_ohne_git_liefert_unbekannt(monkeypatch):
    """Fallback ohne git/Repo: die Logzeile bleibt schreibbar."""
    monkeypatch.delenv("WPS_GIT_REVISION", raising=False)
    monkeypatch.setattr(code_revision, "_git", lambda *args: "")
    code_revision.reset_cache()
    assert code_revision.kennung() == code_revision.UNBEKANNT
    assert code_revision.startup_text() == code_revision.UNBEKANNT


# ── Entscheidungslog ────────────────────────────────────────────────────

def test_entscheidungslog_zeile_traegt_die_revision(tmp_path):
    from unittest.mock import patch

    log_datei = str(tmp_path / "entscheidungs_log.jsonl")
    with patch.multiple(el, LOG_DATEI=log_datei, MAX_BYTES=1_000_000):
        el.schreibe_eintrag(gewinner_name="PV_unten", gewinner_grund="g",
                            soll_einschalten=True, kompressor_laeuft=True)
        with open(log_datei, encoding="utf-8") as f:
            eintrag = json.loads(f.readline())
    assert "rev" in eintrag, eintrag.keys()
    assert eintrag["rev"] == code_revision.kennung()


# ── Zyklus-CSV ──────────────────────────────────────────────────────────

def _zustand():
    return SimpleNamespace(
        sensors=SimpleNamespace(t_unten=30.0, t_mittig=40.0, t_oben=50.0, t_verd=5.0),
        control=SimpleNamespace(
            kompressor_ein=False, active_rule_name="Einspeisung", blocking_reason=None
        ),
    )


def test_zyklus_zeile_traegt_die_revision(tmp_path):
    state = _zustand()
    start = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    pfad = str(tmp_path / "csv log" / "zyklen.csv")

    cycle_logging.begin_cycle(state, start, 7)
    state.control.kompressor_ein = True
    assert cycle_logging.finish_cycle(
        state, start + timedelta(minutes=20), end_grund="regel_aus", csv_path=pfad
    ) is True

    with open(pfad, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["rev"] == code_revision.kennung()


def test_migration_laesst_bestandszeilen_leer(tmp_path):
    """Alte Zeilen bekommen die neue Spalte leer - kein Datenverlust."""
    pfad = tmp_path / "zyklen.csv"
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write("start;ende;dauer_min;quelle;start_regel;end_grund;start_unten;"
                "max_unten;ueberschreitung_k;start_verd\n")
        f.write("2026-09-01 10:00:00;2026-09-01 10:10:00;10.0;pv;AdaptivePV;"
                "regel_aus;40.0;44.0;0.0;18.0\n")

    assert cycle_logging.ensure_cycle_csv(str(pfad), jetzt=datetime(2026, 9, 24)) is True
    with open(pfad, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["max_unten"] == "44.0"
    assert rows[0]["rev"] == ""
