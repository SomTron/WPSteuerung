"""Tests fuer den stabilen WP-Manager-Launcher mit Fehlerausgabe und Downgrade.

Der Launcher (`Updater/wp-manager.sh`) ist bewusst klein und startet das
eigentliche Menue (`Updater/wp-manager-menu.sh`). Damit kann ein fehlerhaftes
Menue-Update nicht mehr den einzigen Startweg unbrauchbar machen:

- Menue nicht startbar  -> deutliche Fehlermeldung + letzte gute Kopie
- Menue beendet mit !=0 -> Fehlermeldung, kein Stand-Wechsel, dann Ersatz
- `--downgrade`/`--fallback` startet die Ersatzkopie direkt
- nach erfolgreichem Lauf wird der aktuelle Stand als 'gut' gesichert

Die Shell-Funktionstests laufen nur, wenn eine echte POSIX-Shell (`sh`)
vorhanden ist - in CI (ubuntu) ist das der Fall.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "Updater"
LAUNCHER = UPDATER_DIR / "wp-manager.sh"
MENU = UPDATER_DIR / "wp-manager-menu.sh"

POSIX_SH = shutil.which("sh")

GUTES_MENUE = """#!/bin/sh
echo "MENUE_LIEF"
exit 0
"""


def test_launcher_und_menue_sind_getrennte_dateien():
    assert LAUNCHER.is_file(), "Stabiler Launcher fehlt"
    assert MENU.is_file(), "Menue-Datei fehlt"
    assert LAUNCHER.resolve() != MENU.resolve()
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "wp-manager-menu.sh" in text
    # Der Launcher darf nicht selbst das grosse Menue enthalten.
    assert len(text.splitlines()) < 160


def test_launcher_prueft_das_menue_vor_dem_start():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "preflight_menu" in text
    assert 'sh -n "$candidate"' in text


def test_launcher_meldet_fehler_mit_klarer_ausgabe():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "WP-Manager FEHLER" in text
    assert "print_error" in text
    assert "nicht startbar" in text
    assert "keine funktionierende Ersatzkopie" in text


def test_launcher_bietet_expliziten_downgrade():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert '--downgrade' in text
    assert '--fallback' in text
    assert "Downgrade nicht moeglich" in text


def test_launcher_sichert_erst_nach_erfolgreichem_lauf():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "promote_last_good" in text
    # Die Sicherung darf nur im Erfolgszweig liegen.
    erfolg = text.index('if [ "$menu_rc" -eq 0 ]')
    sicherung = text.index('promote_last_good "$MENU_SCRIPT"')
    assert erfolg < sicherung


def test_launcher_verwendet_kein_git_reset_und_kein_hartes_downgrade():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "git reset" not in text
    assert "git checkout" not in text
    assert "git revert" not in text


def test_menue_startet_selfupdate_ueber_den_launcher():
    text = MENU.read_text(encoding="utf-8")
    assert 'exec sh "$SCRIPT_DIR/wp-manager.sh"' in text


# ---------------------------------------------------------------- Funktionell


def _write_layout(tmp_path: Path, menu_inhalt: str) -> Path:
    updater = tmp_path / "Updater"
    updater.mkdir(parents=True, exist_ok=True)
    (updater / "wp-manager.sh").write_text(
        LAUNCHER.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    (updater / "wp-manager-menu.sh").write_text(
        menu_inhalt, encoding="utf-8", newline="\n"
    )
    return updater


def _launch(updater: Path, state_dir: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WPS_MANAGER_STATE_DIR"] = str(state_dir)
    env["WPS_MANAGER_PROJECT_ROOT"] = str(updater.parent)
    return subprocess.run(
        [POSIX_SH, str(updater / "wp-manager.sh"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(updater.parent),
    )


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_gutes_menue_startet_und_wird_als_letzter_stand_gesichert(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, GUTES_MENUE)

    result = _launch(updater, state)

    assert result.returncode == 0, result.stderr
    assert "MENUE_LIEF" in result.stdout
    assert (state / "wp-manager-menu.last-good.sh").is_file()


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_defektes_menue_meldet_fehler_und_nutzt_letzten_stand(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, GUTES_MENUE)
    assert _launch(updater, state).returncode == 0

    # absichtlich unvollstaendig -> sh -n schlaegt fehl
    (updater / "wp-manager-menu.sh").write_text(
        "if [ 1 -eq 1 ]; then\n", encoding="utf-8", newline="\n"
    )

    result = _launch(updater, state)

    assert result.returncode == 0, result.stderr
    assert "WP-Manager FEHLER" in result.stderr
    assert "nicht startbar" in result.stderr
    assert "MENUE_LIEF" in result.stdout, "Ersatzstand wurde nicht ausgefuehrt"


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_defektes_menue_ohne_ersatzstand_bricht_mit_meldung_ab(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, "if [ 1 -eq 1 ]; then\n")

    result = _launch(updater, state)

    assert result.returncode == 1
    assert "WP-Manager FEHLER" in result.stderr
    assert "keine funktionierende Ersatzkopie" in result.stderr


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_downgrade_startet_ersatzstand_auch_bei_defektem_menue(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, GUTES_MENUE)
    assert _launch(updater, state).returncode == 0

    (updater / "wp-manager-menu.sh").write_text(
        "if [ 1 -eq 1 ]; then\n", encoding="utf-8", newline="\n"
    )

    result = _launch(updater, state, "--downgrade")

    assert result.returncode == 0, result.stderr
    assert "MENUE_LIEF" in result.stdout
    assert "verwendet" in result.stderr


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_downgrade_ohne_ersatzstand_meldet_klaren_fehler(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, GUTES_MENUE)

    result = _launch(updater, state, "--downgrade")

    assert result.returncode == 1
    assert "Downgrade nicht moeglich" in result.stderr


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_fehlgeschlagenes_menue_wird_nicht_als_guter_stand_gesichert(tmp_path):
    state = tmp_path / "state"
    updater = _write_layout(tmp_path, GUTES_MENUE)
    assert _launch(updater, state).returncode == 0

    last_good = state / "wp-manager-menu.last-good.sh"
    last_good.write_text(
        GUTES_MENUE.replace("exit 0", "exit 9"), encoding="utf-8", newline="\n"
    )

    # Menue liefert die richtige Ausgabe, bricht aber mit Exitcode 3 ab.
    (updater / "wp-manager-menu.sh").write_text(
        GUTES_MENUE.replace("exit 0", "exit 3"), encoding="utf-8", newline="\n"
    )

    result = _launch(updater, state)

    assert result.returncode == 9, result.stderr
    assert "WP-Manager FEHLER" in result.stderr
    assert "Exitcode 3" in result.stderr
    assert "exit 9" in last_good.read_text(encoding="utf-8"), (
        "Ersatzstand wurde durch den fehlgeschlagenen Lauf ueberschrieben"
    )
