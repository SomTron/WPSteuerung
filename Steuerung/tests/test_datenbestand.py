"""Tests fuer den vollstaendigen Datenbestand (Anzeige + Upload).

Hintergrund: Der WP-Manager lud bisher genau EINE Datei hoch - die aktive.
Bei rotierenden Daten ging die Historie verloren:

  * heizungssteuerung.log     - Generationen .2026-10-19, .1 daneben
  * heizungsdaten.csv         - Monatsarchive heizungsdaten_2026-09.csv
  * entscheidungs_log.jsonl   - Generationen .old.1 ... .old.7
  * learning_data.json        - Sicherungskopien .korrupt-...

Diese Tests halten fest, dass der Bestand ALLES findet - und dass das
Archiv keine gleichnamigen Dateien kollidieren laesst.
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from datenbestand import (  # noqa: E402
    als_archiv,
    bestand,
    bestand_csv,
    bestand_jsonl,
    bestand_lerndaten,
    bestand_log,
    bestand_zyklen,
)


# ------------------------------------------------------------------- Log


def test_bestand_log_umfasst_alle_generationen(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("aktuell\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.2026-10-19").write_text("gestern\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.1").write_text("altbestand\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_log(str(log))]

    assert len(namen) == 3, "eine Generation fehlt im Bestand"
    assert "heizungssteuerung.log" in namen
    assert "heizungssteuerung.log.2026-10-19" in namen
    assert "heizungssteuerung.log.1" in namen
    # Die aktive Datei steht am Ende (sie ist die juengste).
    assert namen[-1] == "heizungssteuerung.log"


def test_bestand_log_ignoriert_fremde_dateien(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("x\n", encoding="utf-8")
    (tmp_path / "error.log").write_text("y\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_log(str(log))]
    assert namen == ["heizungssteuerung.log"]


# ------------------------------------------------------------------- CSV


def test_bestand_csv_umfasst_die_monatsarchive(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("aktuell\n", encoding="utf-8")
    (tmp_path / "heizungsdaten_2026-09.csv").write_text("september\n", encoding="utf-8")
    (tmp_path / "heizungsdaten_2026-08.csv").write_text("august\n", encoding="utf-8")
    # Darf nicht mitgenommen werden: eine andere CSV.
    (tmp_path / "zyklen.csv").write_text("zyklen\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_csv(str(csv))]

    assert "heizungsdaten_2026-09.csv" in namen
    assert "heizungsdaten_2026-08.csv" in namen
    assert "heizungsdaten.csv" in namen
    assert "zyklen.csv" not in namen
    assert namen[-1] == "heizungsdaten.csv"


def test_bestand_csv_ueberlebt_leerzeichen_im_ordnernamen(tmp_path):
    """Der echte Pfad heisst "csv log" - ein Leerzeichen im Ordnernamen."""
    verz = tmp_path / "csv log"
    verz.mkdir()
    csv = verz / "heizungsdaten.csv"
    csv.write_text("aktuell\n", encoding="utf-8")
    (verz / "heizungsdaten_2026-09.csv").write_text("archiv\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_csv(str(csv))]
    assert len(namen) == 2


# ------------------------------------------------------- Entscheidungslog


def test_bestand_jsonl_umfasst_die_old_generationen(tmp_path):
    jl = tmp_path / "entscheidungs_log.jsonl"
    jl.write_text("{}\n", encoding="utf-8")
    for nr in (1, 2, 3):
        (tmp_path / f"entscheidungs_log.jsonl.old.{nr}").write_text("{}\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_jsonl(str(jl))]

    assert len(namen) == 4
    # .old.1 ist die juengste Generation -> steht am weitesten hinten.
    assert namen[-1] == "entscheidungs_log.jsonl"
    assert namen[-2] == "entscheidungs_log.jsonl.old.1"


# ------------------------------------------------------------- Lerndaten


def test_bestand_lerndaten_umfasst_die_sicherungskopien(tmp_path):
    lz = tmp_path / "learning_data.json"
    lz.write_text("{}", encoding="utf-8")
    (tmp_path / "learning_data.json.korrupt-20261020_101500").write_text("{}", encoding="utf-8")

    namen = [os.path.basename(p) for p in bestand_lerndaten(str(lz))]
    assert len(namen) == 2
    assert namen[-1] == "learning_data.json"


# ---------------------------------------------------------------- Zyklen


def test_bestand_zyklen_sucht_die_analysen(tmp_path):
    laufend = tmp_path / "zyklen.csv"
    laufend.write_text("a\n", encoding="utf-8")
    logs = tmp_path / "logs"
    (logs / "analyse_2026-10-20").mkdir(parents=True)
    (logs / "analyse_2026-10-20" / "zyklen.csv").write_text("b\n", encoding="utf-8")
    (logs / "analyse_2026-10-19").mkdir(parents=True)
    (logs / "analyse_2026-10-19" / "zyklen.csv").write_text("c\n", encoding="utf-8")

    dateien = bestand_zyklen(str(laufend), [str(logs)])

    assert len(dateien) == 3
    assert str(laufend) in dateien


# --------------------------------------------------------------- Archiv


def test_archiv_enthaelt_den_gesamten_bestand(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("aktuell\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.2026-10-19").write_text("gestern\n", encoding="utf-8")

    ziel = tmp_path / "out.zip"
    dateien = als_archiv("log", str(log), str(ziel))

    assert len(dateien) == 2
    assert ziel.exists()
    with zipfile.ZipFile(ziel) as zf:
        namen = zf.namelist()
    assert len(namen) == 2
    assert all(n.startswith("log/") for n in namen)
    # Der Inhalt muss unbeschaedigt sein - sonst waere der Upload nutzlos.
    with zipfile.ZipFile(ziel) as zf:
        inhalte = [zf.read(n).decode("utf-8").strip() for n in namen]
    assert sorted(inhalte) == ["aktuell", "gestern"]


def test_archiv_kollidiert_nicht_bei_gleichnamigen_dateien(tmp_path):
    """Mehrere zyklen.csv aus verschiedenen analyse-Laeufen: im Archiv
    muessen sie unterscheidbar bleiben."""
    logs = tmp_path / "logs"
    (logs / "analyse_a").mkdir(parents=True)
    (logs / "analyse_b").mkdir(parents=True)
    (logs / "analyse_a" / "zyklen.csv").write_text("a\n", encoding="utf-8")
    (logs / "analyse_b" / "zyklen.csv").write_text("b\n", encoding="utf-8")

    ziel = tmp_path / "out.zip"
    als_archiv("zyklen", "", str(ziel), [str(logs)])

    with zipfile.ZipFile(ziel) as zf:
        namen = zf.namelist()
    assert len(namen) == 2
    assert len(set(namen)) == 2, "zwei Eintraege mit gleichem Namen"


def test_archiv_meldet_fehler_bei_leerem_bestand(tmp_path):
    ziel = tmp_path / "out.zip"
    with pytest.raises(FileNotFoundError):
        als_archiv("log", str(tmp_path / "gibt_es_nicht.log"), str(ziel))


# ----------------------------------------------------------------- Dispatch


def test_bestand_dispatch_steuert_die_arte(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("x\n", encoding="utf-8")
    assert len(bestand("log", str(log))) == 1

    with pytest.raises(ValueError):
        bestand("gibtesnicht", str(log))
