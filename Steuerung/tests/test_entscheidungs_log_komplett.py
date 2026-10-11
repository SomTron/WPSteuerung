"""Tests fuer das Einmal-Auslesen des kompletten Entscheidungslogs.

Deckt `Updater/entscheidungs_log_komplett.py` ab: Einlesen ALLER Generationen
inkl. der Alt-Datei `.old` (die der laufende Dienst nicht kennt), Sortierung
ueber naive und mit Zeitzone versehene Zeitstempel, Entdopplung sowie die
Ausgabemodi (Datei und stdout).
"""
import importlib.util
import json
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[2] / "Updater" / "entscheidungs_log_komplett.py"
SPEC = importlib.util.spec_from_file_location("entscheidungs_log_komplett", MODULE_PATH)
komplett = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(komplett)


def _schreibe(pfad: Path, eintraege):
    pfad.write_text("".join(json.dumps(e) + "\n" for e in eintraege), encoding="utf-8")


def _bestand(tmp_path):
    """Bestand aus aktueller Datei, Rotation und Alt-Datei (Einzel-.old)."""
    _schreibe(tmp_path / "entscheidungs_log.jsonl", [
        {"ts": "2026-10-01T09:00:00+02:00", "gewinner": "Neu"},
    ])
    _schreibe(tmp_path / "entscheidungs_log.jsonl.old.1", [
        {"ts": "2026-09-20T08:00:00+02:00", "gewinner": "Rot1"},
    ])
    # Alt-Schema: EINE .old-Datei - die kennt der laufende Dienst nicht.
    _schreibe(tmp_path / "entscheidungs_log.jsonl.old", [
        {"ts": "2026-09-09T10:38:00+02:00", "gewinner": "Alt"},
    ])


def test_liest_auch_die_alt_datei_und_sortiert(tmp_path):
    _bestand(tmp_path)
    assert len(komplett.finde_logdateien(str(tmp_path))) == 3
    _, eintraege, je_datei, unlesbar = komplett.lies_alles(str(tmp_path))
    assert unlesbar == 0
    assert [e["gewinner"] for e in eintraege] == ["Alt", "Rot1", "Neu"]
    assert je_datei["entscheidungs_log.jsonl.old"] == 1


def test_entdoppelt_identische_zeilen(tmp_path):
    zeile = {"ts": "2026-10-01T09:00:00+02:00", "gewinner": "X"}
    _schreibe(tmp_path / "entscheidungs_log.jsonl", [zeile])
    _schreibe(tmp_path / "entscheidungs_log.jsonl.old.1", [zeile])
    _, eintraege, _, _ = komplett.lies_alles(str(tmp_path))
    assert len(eintraege) == 1


def test_naive_und_aware_zeitstempel_gemischt(tmp_path):
    _schreibe(tmp_path / "entscheidungs_log.jsonl", [
        {"ts": "2026-09-18T14:45:19", "gewinner": "Naiv"},
        {"ts": "2026-09-18T14:40:00+02:00", "gewinner": "Aware"},
    ])
    _, eintraege, _, _ = komplett.lies_alles(str(tmp_path))
    assert [e["gewinner"] for e in eintraege] == ["Aware", "Naiv"]


def test_unlesbare_zeile_wird_uebersprungen(tmp_path):
    pfad = tmp_path / "entscheidungs_log.jsonl"
    pfad.write_text('{"ts": "2026-10-01T09:00:00", "gewinner": "Ok"}\n'
                    "  abgebrochene zeile\n", encoding="utf-8")
    _, eintraege, _, unlesbar = komplett.lies_alles(str(tmp_path))
    assert unlesbar == 1
    assert [e["gewinner"] for e in eintraege] == ["Ok"]


def test_main_schreibt_zieldatei(tmp_path, capsys):
    _bestand(tmp_path)
    ziel = tmp_path / "raus.jsonl"
    assert komplett.main([str(tmp_path), "-o", str(ziel)]) == 0
    zeilen = [json.loads(z) for z in ziel.read_text(encoding="utf-8").splitlines() if z]
    assert [e["gewinner"] for e in zeilen] == ["Alt", "Rot1", "Neu"]
    assert "Eintraege gesamt: 3" in capsys.readouterr().out


def test_stdout_modus_trennt_protokoll(tmp_path, capsys):
    _bestand(tmp_path)
    assert komplett.main([str(tmp_path), "-o", "-"]) == 0
    ergebnis = capsys.readouterr()
    zeilen = [json.loads(z) for z in ergebnis.out.splitlines() if z]
    assert [e["gewinner"] for e in zeilen] == ["Alt", "Rot1", "Neu"]
    # Zusammenfassung auf stderr, damit stdout reine JSON-Lines bleibt.
    assert "Eintraege gesamt: 3" in ergebnis.err
    assert "Eintraege gesamt" not in ergebnis.out


def test_fehler_ohne_verzeichnis():
    assert komplett.main(["/gibt/es/nicht/hoechstwahrscheinlich"]) == 2
