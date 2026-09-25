"""Tests fuer robuste CSV-Auswertung und Sicherheitsregeln des WP-Managers."""
import importlib.util
from datetime import datetime
from pathlib import Path
import re

MODULE_PATH = Path(__file__).resolve().parents[2] / "Updater" / "manager_status.py"
UPDATER_DIR = MODULE_PATH.parent
SPEC = importlib.util.spec_from_file_location("manager_status", MODULE_PATH)
manager_status = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(manager_status)


HEATING_HEADER = (
    "Zeitstempel,T_Oben,T_Unten,T_Mittig,T_Boiler,T_Verd,Kompressor,"
    "ACPower,FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
    "Einschaltpunkt,Ausschaltpunkt,Solarüberschuss,Urlaubsmodus,PowerSource,"
    "Prognose_Morgen"
)


def test_format_age():
    assert manager_status.format_age(None) == "n/a"
    assert manager_status.format_age(-1) == "0 s"
    assert manager_status.format_age(45) == "45 s"
    assert manager_status.format_age(125) == "2 min"
    assert manager_status.format_age(7200) == "2 h"
    assert manager_status.format_age(172800) == "2 d"


def test_collect_status_liest_letzten_snapshot_und_bom_zyklen(tmp_path):
    heating = tmp_path / "heizungsdaten.csv"
    heating.write_text(
        HEATING_HEADER + "\n2026-09-23 10:00:00,40,38,39,45,30,0,0,0,0,0,0,0,0,0,0,0,0,Netz,0\n",
        encoding="utf-8",
    )
    cycles = tmp_path / "zyklen.csv"
    cycles.write_text(
        "﻿start;ende;dauer_min;quelle;start_regel;end_grund\n"
        "2026-09-23 09:00:00;2026-09-23 09:40:00;40;netz;Regel;boiler_max\n",
        encoding="utf-8",
    )
    state = tmp_path / "last_state.txt"
    state.write_text(
        "2026-09-23 10:01:00 | Komp=EIN | Regel=Normal | Blocking=-\n",
        encoding="utf-8",
    )

    status = manager_status.collect_status(
        str(heating), str(cycles), str(state), datetime(2026, 9, 23, 10, 2)
    )

    assert status["compressor"] == "EIN"
    assert status["compressor_source"] == "last_state.txt"
    assert status["compressor_age"] == "1 min"
    assert status["heating_age"] == "2 min"
    assert status["cycle_count"] == "1"
    assert status["cycle_last_end"] == "2026-09-23 09:40:00"
    assert status["cycle_age"] == "22 min"
    assert status["cycle_last_reason"] == "boiler_max"
    assert status["cycle_columns"] == "6"


def test_collect_status_fällt_auf_heizungsdaten_zurück(tmp_path):
    heating = tmp_path / "heizungsdaten.csv"
    heating.write_text(
        HEATING_HEADER + "\n2026-09-23 10:00:00,40,38,39,45,30,1,0,0,0,0,0,0,0,0,0,0,0,Netz,0",
        encoding="utf-8",
    )
    status = manager_status.collect_status(
        str(heating),
        str(tmp_path / "missing.csv"),
        str(tmp_path / "missing-state.txt"),
        datetime(2026, 9, 23, 10, 0, 30),
    )

    assert status["compressor"] == "EIN"
    assert status["compressor_source"] == "heizungsdaten.csv"
    assert status["compressor_age"] == "30 s"
    assert status["cycle_count"] == "n/a"


def test_kaputte_und_leere_dateien_erzeugen_defaults(tmp_path):
    heating = tmp_path / "empty.csv"
    heating.write_text("", encoding="utf-8")
    status = manager_status.collect_status(
        str(heating),
        str(tmp_path / "empty-cycles.csv"),
        str(tmp_path / "empty-state.txt"),
        datetime(2026, 9, 23),
    )
    assert status["compressor"] == "UNBEKANNT"
    assert status["heating_age"] == "n/a"
    assert status["cycle_count"] == "n/a"
    assert status["cycle_last_end"] == "n/a"


def test_letzte_zeile_wird_auch_mit_leerzeilen_am_dateiende_gefunden(tmp_path):
    path = tmp_path / "mehrzeilig.txt"
    path.write_text("erste\nzweite\n\n\n", encoding="utf-8")
    assert manager_status._tail_text(str(path)) == "zweite"


def test_zeilenzaehler_beruecksichtigt_optionalen_abschliessenden_newline(tmp_path):
    path = tmp_path / "daten.csv"
    path.write_text("kopf\na\nb\n", encoding="utf-8")
    assert manager_status._count_data_rows(str(path)) == 2
    path.write_text("kopf\na\nb", encoding="utf-8")
    assert manager_status._count_data_rows(str(path)) == 2


def test_main_gibt_shell_werte_aus(tmp_path, capsys):
    missing = tmp_path / "fehlt"
    result = manager_status.main([str(missing), str(missing), str(missing)])
    assert result == 0
    output = capsys.readouterr().out
    assert "compressor=UNBEKANNT\n" in output
    assert "heating_age=n/a\n" in output


def _updater_scripts():
    return (
        (UPDATER_DIR / "wp-manager-menu.sh").read_text(encoding="utf-8"),
        (UPDATER_DIR / "rpi-deploy.sh").read_text(encoding="utf-8"),
    )


def _launcher_text():
    return (UPDATER_DIR / "wp-manager.sh").read_text(encoding="utf-8")


def test_updater_verwirft_lokale_aenderungen_nicht():
    manager, deploy = _updater_scripts()
    assert "git reset --hard" not in manager
    assert "git reset --hard" not in deploy
    assert "wp-manager-backups" in manager
    assert "wp-manager-backups" in deploy
    assert "tracked-changes.patch" in manager
    assert "tracked-changes.patch" in deploy


def test_alle_updater_pulls_sind_fast_forward_only():
    manager, deploy = _updater_scripts()
    pull_lines = [
        line.strip()
        for script in (manager, deploy)
        for line in script.splitlines()
        if re.search(r"\bgit\b.*\bpull\b", line)
    ]
    assert pull_lines
    assert all("--ff-only" in line for line in pull_lines), pull_lines


def test_serviceaktionen_werden_verifiziert():
    manager, deploy = _updater_scripts()
    for action in ("start", "stop", "restart"):
        assert f"verify_service_action {action} wpsteuerung" in manager
    assert "MainPID" in manager
    assert "NRestarts" in manager
    assert "Neustart hat die MainPID nicht gewechselt" in manager
    assert "Neustart hat die MainPID nicht gewechselt" in deploy
    assert "verify_service_restart" in deploy
    assert "journalctl -u" in deploy


def test_branch_namen_koennen_nicht_als_git_option_falsch_interpretiert_werden():
    _, deploy = _updater_scripts()
    assert 'case "$target_branch" in' in deploy
    assert "-*|'')" in deploy
    assert 'git check-ref-format "refs/heads/$target_branch"' in deploy


def test_upload_ist_bestaetigungspflichtig_begrenzt_und_bereinigt():
    manager, _ = _updater_scripts()
    assert "Upload jetzt ausfuehren? (j/N)" in manager
    assert "WPS_UPLOAD_MAX_BYTES" in manager
    assert "--connect-timeout 15" in manager
    assert "--max-time 300" in manager
    assert "trap 'rm -rf \"$temp_dir\"' 0 1 2 3 15" in manager
    assert 'rm -rf "$temp_dir"' in manager
