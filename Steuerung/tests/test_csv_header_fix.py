"""Regression: Der CSV-Header wird repariert, ohne Daten zu verlieren.

Befund 24.10.2026: Das manuelle Skript `test_header_fix.py` im Repo-Root
hat den Fix nur mit `print` geprueft (keine Assertions) und dabei `backup/`
im Arbeitsverzeichnis angelegt - von dort stammten die committeten
Backup-CSVs. Hier ist derselbe Fix ein echter pytest-Test und laeuft
vollstaendig in tmp_path.
"""
from utils import EXPECTED_CSV_HEADER, check_and_fix_csv_header


def _schreibe_csv(pfad, header, zeile="2023-01-01 00:00:00,1,2,3"):
    pfad.write_text(",".join(header) + "\n" + zeile + "\n", encoding="utf-8")


def test_falscher_header_wird_repariert(tmp_path, monkeypatch):
    """Ein Header mit fehlender Spalte wird ersetzt, die Daten bleiben."""
    monkeypatch.chdir(tmp_path)
    pfad = tmp_path / "heizungsdaten.csv"
    _schreibe_csv(pfad, EXPECTED_CSV_HEADER[:-1])

    assert check_and_fix_csv_header(str(pfad)) is True

    zeilen = pfad.read_text(encoding="utf-8").splitlines()
    assert zeilen[0] == ",".join(EXPECTED_CSV_HEADER)
    assert zeilen[1] == "2023-01-01 00:00:00,1,2,3", (
        "Die Datenzeile muss erhalten bleiben"
    )


def test_zweiter_lauf_ist_idempotent(tmp_path, monkeypatch):
    """Ein bereits korrigierter Header wird nicht erneut "repariert"."""
    monkeypatch.chdir(tmp_path)
    pfad = tmp_path / "heizungsdaten.csv"
    _schreibe_csv(pfad, EXPECTED_CSV_HEADER[:-1])

    assert check_and_fix_csv_header(str(pfad)) is True
    assert check_and_fix_csv_header(str(pfad)) is False


def test_backup_landet_im_arbeitsverzeichnis(tmp_path, monkeypatch):
    """Das Sicherheitsbackup darf den Projektordner nicht zumuellen."""
    monkeypatch.chdir(tmp_path)
    pfad = tmp_path / "heizungsdaten.csv"
    _schreibe_csv(pfad, EXPECTED_CSV_HEADER[:-1])

    check_and_fix_csv_header(str(pfad))

    assert len(list((tmp_path / "backup").glob("*.bak"))) == 1


def test_fehlende_datei_ist_kein_fehler(tmp_path):
    assert check_and_fix_csv_header(str(tmp_path / "fehlt.csv")) is False


def test_korrekter_header_bleibt_unveraendert(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    pfad = tmp_path / "heizungsdaten.csv"
    _schreibe_csv(pfad, EXPECTED_CSV_HEADER)
    vorher = pfad.read_text(encoding="utf-8")

    assert check_and_fix_csv_header(str(pfad)) is False
    assert pfad.read_text(encoding="utf-8") == vorher
