# -*- coding: utf-8 -*-
"""Tests fuer das Korrektur-Werkzeug des Zyklus-Altbestands."""
import csv
import glob

import pytest

import zyklus_reason_fix as fix

HEADER = "start;ende;dauer_min;quelle;source_at_start;start_regel;end_grund;reason_code;max_unten;rev"


def _schreibe(pfad, zeilen):
    with open(pfad, "w", encoding="utf-8", newline="") as fh:
        schreiber = csv.writer(fh, delimiter=";", lineterminator="\r\n")
        schreiber.writerow(HEADER.split(";"))
        for zeile in zeilen:
            schreiber.writerow(zeile)


def _lies(pfad):
    with open(pfad, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


@pytest.fixture
def zyklen_csv(tmp_path):
    """Vier Zeilen wie im Betriebslog: drei falsche Codes, einer korrekt."""
    pfad = tmp_path / "zyklen.csv"
    _schreibe(pfad, [
        ["2026-10-01 08:00:11", "2026-10-01 09:00:16", "60.1", "pv", "PV", "Abweichung",
         "regel_aus", "mindestlaufzeit", "35.1", "e781756"],
        ["2026-10-02 12:52:47", "2026-10-02 13:16:30", "23.7", "pv", "PV", "AdaptivePV",
         "boiler_max", "mindestlaufzeit", "48.0", "e781756"],
        ["2026-10-05 10:35:47", "2026-10-05 11:16:20", "40.6", "pv", "PV", "AdaptivePV",
         "UEBERHITZUNG (58.0C >= 58.0C)", "unbekannt", "48.0", "e781756"],
        ["2026-10-06 09:00:00", "2026-10-06 09:40:00", "40.0", "pv", "PV", "Einspeisung",
         "regel_aus", "regel_aus", "46.0", "e781756"],
    ])
    return pfad


def test_korrigiert_nur_falsche_codes(zyklen_csv):
    zeilen, geaendert, details = fix.korrigiere_datei(str(zyklen_csv))

    assert zeilen == 4
    assert geaendert == 3
    assert details[("regel_aus", "mindestlaufzeit", "regel_aus")] == 1
    assert details[("boiler_max", "mindestlaufzeit", "boiler_max")] == 1

    daten = _lies(zyklen_csv)
    assert [z["reason_code"] for z in daten] == [
        "regel_aus", "boiler_max", "uebertemperatur", "regel_aus"
    ]
    # Spalten/Reihenfolge und CRLF bleiben unveraendert.
    with open(zyklen_csv, "rb") as fh:
        roh = fh.read()
    assert roh.startswith(HEADER.split(";")[0].encode())
    assert roh.count(b"\r\n") == 5
    assert not roh.startswith(b"\xef\xbb\xbf")

    sicherungen = glob.glob(str(zyklen_csv) + ".bak-*")
    assert len(sicherungen) == 1
    assert _lies(sicherungen[0])[0]["reason_code"] == "mindestlaufzeit"


def test_dry_run_schreibt_nichts(zyklen_csv):
    vorher = zyklen_csv.read_bytes()
    zeilen, geaendert, _details = fix.korrigiere_datei(str(zyklen_csv), dry_run=True)

    assert geaendert == 3
    assert zyklen_csv.read_bytes() == vorher
    assert glob.glob(str(zyklen_csv) + ".bak-*") == []


def test_zweiter_lauf_aendert_nichts(zyklen_csv):
    assert fix.korrigiere_datei(str(zyklen_csv))[1] == 3
    assert fix.korrigiere_datei(str(zyklen_csv))[1] == 0
    # Nur das Backup des ersten Laufs existiert.
    assert len(glob.glob(str(zyklen_csv) + ".bak-*")) == 1


def test_fehlende_spalte_wird_gemeldet(tmp_path):
    pfad = tmp_path / "kaputt.csv"
    pfad.write_text("start;ende;end_grund\n2026-10-01;2026-10-01 09:00;regel_aus\n",
                    encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        fix.korrigiere_datei(str(pfad))
    assert "reason_code" in str(exc.value)


def test_cli_korrigiert_verzeichnis(tmp_path, capsys):
    ziel = tmp_path / "csv log"
    ziel.mkdir()
    datei = ziel / "zyklen.csv"
    _schreibe(datei, [
        ["2026-10-01 08:00:11", "2026-10-01 09:00:16", "60.1", "pv", "PV", "Abweichung",
         "regel_aus", "mindestlaufzeit", "35.1", "e781756"],
    ])
    (ziel / "notizen.txt").write_text("kein csv", encoding="utf-8")

    rc = fix.main(["--pfad", str(ziel)])

    assert rc == 0
    assert "1 korrigiert" in capsys.readouterr().out
    assert _lies(datei)[0]["reason_code"] == "regel_aus"


def test_cli_ohne_datei_meldet_fehler(tmp_path, capsys):
    rc = fix.main(["--pfad", str(tmp_path / "gibtsnicht")])
    assert rc == 1
    assert "Keine zyklen" in capsys.readouterr().err
