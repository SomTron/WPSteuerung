"""Tests fuer robuste CSV-Auswertung und Sicherheitsregeln des WP-Managers."""
import importlib.util
from datetime import datetime
from pathlib import Path
import re

import pytest

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


def _hauptmenue_block(manager: str, option: str) -> str:
    """Holt den Block einer Option des HAUPTmenues bis zur naechsten Option.

    Zwei Stolperfallen, die dieser Helfer vermeidet:
    * Das Analyse-Untermenue nutzt dieselben Nummern, aber mit 12 statt 8
      Leerzeichen eingerueckt - der Anker verlangt einen Zeilenumbruch
      davor, sonst wuerde das falsche Menue getroffen.
    * Ein Split auf ';;' schneidet zu frueh ab: Option 6 enthaelt ein
      inneres 'case ... [Jj]*) ;;'. Deshalb laeuft der Block bis zur
      naechsten Hauptmenue-Option.
    """
    rest = manager.split(f"\n        {option}\n", 1)[1]
    return re.split(r"\n        \d+\)\n", rest, maxsplit=1)[0]


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


def test_updater_resetet_nur_bei_identischem_dateistand():
    """BUGFIX (22.10.2026): Nach einem Force-Push (History-Aufraeumen)
    scheitert 'git pull --ff-only' auf dem Pi - der Deploy bricht ab,
    obwohl inhaltlich nichts geaendert wurde.

    Der Deploy darf das nur aufloesen, wenn der Dateistand IDENTISCH
    ist: dann aendert der Reset nichts am Code. Bei unterschiedlichem
    Dateistand waere es ein echter Codewechsel, der eine Entscheidung
    des Betreibers braucht.

    Und: 'git reset --soft' statt '--hard' - uncommittete Aenderungen
    an getrackten Dateien duerfen NIE vernichtet werden.
    """
    _manager, deploy = _updater_scripts()

    # Der Fallback existiert und vergleicht die Tree-Hashes
    assert "reset_bei_identischem_dateistand" in deploy
    assert "HEAD^{tree}" in deploy
    assert "origin/$branch^{tree}" in deploy

    # Und er nutzt bewusst NICHT --hard (das wuerde lokale Arbeit
    # vernichten - siehe test_updater_verwirft_lokale_aenderungen_nicht)
    assert "git reset --soft" in deploy
    assert "git reset --hard" not in deploy

    # Beide Pull-Stellen nutzen den Fallback
    assert deploy.count("elif reset_bei_identischem_dateistand") == 2


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


def test_upload_optionen_nehmen_den_vollstaendigen_datenbestand():
    """REGRESSION: die Uploads luden nur die AKTIVE Datei hoch.

    Bei rotierenden Daten ging damit die ganze Historie verloren:
      * heizungssteuerung.log    - Generationen .2026-10-19, .1 daneben
      * heizungsdaten.csv        - Monatsarchive heizungsdaten_2026-09.csv
      * entscheidungs_log.jsonl  - Generationen .old.1 ... .old.7
      * learning_data.json       - Sicherungskopien .korrupt-...

    Deshalb laufen die Optionen 9/14/16/18/21 jetzt ueber upload_bestand,
    das den Bestand als ZIP buendelt.
    """
    manager, _ = _updater_scripts()

    # Kein Upload darf mehr direkt eine einzelne Datei hochladen.
    for zeile in (
        'upload_file "$LOG_FILE"',
        'upload_file "$HEATING_CSV"',
        'upload_file "$ENT_LOG_FILE"',
        'upload_file "$ZYKLEN_CSV"',
        'upload_file "$LEARNING_JSON"',
    ):
        assert zeile not in manager, f"{zeile} laedt nur die aktive Datei hoch"

    # Stattdessen: der Bestand als Archiv.
    assert 'upload_bestand log "$LOG_FILE"' in manager
    assert 'upload_bestand csv "$HEATING_CSV"' in manager
    assert 'upload_bestand jsonl "$ENT_LOG_FILE"' in manager
    assert 'upload_bestand lern "$LEARNING_JSON"' in manager
    assert "datenbestand.py" in manager
    # ZIP statt tar.gz: auf Windows per Doppelklick entpackbar und genauso
    # klein (beide nutzen dieselbe deflate-Kompression).
    assert "--out-dir" in manager
    assert "--label" in manager
    assert '"${label}.tar.gz"' not in manager
    # Die Groessensteuerung muss durchgereicht werden - sonst waere die
    # Grenze nur eine Pruefung NACH dem Bau des Archivs.
    assert '--max-bytes "$max_bytes"' in manager
    assert '--budget "$budget"' in manager
    assert "WPS_UPLOAD_BUDGET_BYTES" in manager
    # Geteilte Uploads: mehrere Archive, jedes einzeln hochladen.
    assert 'while IFS= read -r archiv' in manager


def test_upload_bestaetigung_bleibt_auch_beim_archiv():
    """Auch der Archiv-Upload muss bestaetigungspflichtig bleiben -
    die Daten landen oeffentlich auf catbox.moe."""
    manager, _ = _updater_scripts()
    block = manager.split("upload_fertig() {", 1)[1].split("\n}", 1)[0]
    assert "Upload jetzt ausfuehren? (j/N)" in block
    assert "WPS_UPLOAD_MAX_BYTES" in block
    # Der Archiv-Weg nutzt dieselbe bestaetigte Funktion.
    assert 'upload_fertig "$archiv"' in manager


# ------------------- Korrektheits- und Härtungsverträge -------------------

def test_kpis_werden_mit_pflichtparametern_aufgerufen():
    """kpis() braucht Leistung und Strompreis - ohne Parameter: TypeError."""
    manager, _ = _updater_scripts()
    assert "kpis(leistung, preis)" in manager
    # Der fehlerhafte parameterlose Aufruf darf nicht mehr vorkommen.
    assert "kpis()" not in manager


def test_inline_python_bekommt_pfade_ueber_umgebung_statt_interpolation():
    manager, _ = _updater_scripts()
    # Kein Einbetten von Shell-Variablen in den Python-Code mehr.
    assert "sys.path.insert(0, '$TARGET_DIR')" not in manager
    assert "WPS_STEER_DIR" in manager
    assert "os.environ.get(\"WPS_STEER_DIR\"" in manager


def test_manager_update_prueft_detached_head_und_upstream():
    manager, _ = _updater_scripts()
    assert "symbolic-ref --quiet --short HEAD" in manager
    assert "Update blockiert: HEAD ist detached" in manager
    assert "@{u}" in manager


def test_backup_sichert_auch_untracked_dateien():
    manager, _ = _updater_scripts()
    assert "untracked-files.tar.gz" in manager
    assert "--others --exclude-standard" in manager


def test_preflight_meldet_fehlende_pflichtprogramme():
    manager, _ = _updater_scripts()
    assert "preflight()" in manager
    assert "preflight\n" in manager
    assert "Fehlende Pflichtprogramme" in manager


def test_ctrl_c_beendet_nur_die_ansicht_nicht_den_manager():
    """Strg+C darf den Manager NICHT beenden (Bug 28.09.2026).

    Vorher rief 'menu_abbruch' per 'exit 130' auf. Da SIGINT an die ganze
    Prozessgruppe geht, beendete ein Strg+C in 'tail -f' (Option 1) oder
    'more' den Menueprozess selbst - man landete auf der Shell und musste
    neu starten. INT behandelt jetzt nur die Ansicht; TERM bleibt fatal.
    """
    manager, _ = _updater_scripts()
    assert "menu_abbruch()" in manager
    # menu_abbruch darf den Prozess NICHT mehr beenden.
    abbruch_body = manager.split("menu_abbruch() {", 1)[1].split("\n}", 1)[0]
    assert "exit" not in abbruch_body, (
        "menu_abbruch beendet den Manager - Strg+C wuerde ihn aus dem "
        "Menue werfen"
    )
    # INT ist nicht mehr fatal, TERM schon (kill von aussen).
    assert "trap menu_abbruch INT\n" in manager
    assert "trap menu_abbruch INT TERM" not in manager
    assert "menu_beenden() {" in manager
    assert "trap menu_beenden TERM" in manager


def test_menue_zeichnet_sich_beim_zurueckkehren_neu():
    """Ohne Terminal-Leeren schob sich der ~50-zeilige Status-Header bei
    jedem Zurueckkehren weiter nach unten, bis nichts lesbar war."""
    manager, _ = _updater_scripts()
    # printf '\033[H\033[2J' im Shell-Skript
    assert "\\033[H\\033[2J" in manager
    # Ein unguarded 'clear' stand frueher an der Header-Ausgabe: fehlt das
    # Kommando auf dem Pi, schrieb es eine Fehlermeldung und blieb ohne
    # Wirkung. Das Leeren passiert jetzt nur noch ueber die ANSI-Sequenz.
    assert "\n    clear\n" not in manager


def test_live_logs_kehren_zum_menue_zurueck():
    """Option 1 (tail) braucht eine Rueckkehr-Pause - sonst scrollt die
    Ausgabe ohne Halt durch und der Benutzer verliert den Kontext.

    `tail -F` (follow by name) statt `-f`: das Log rotiert taeglich und wird
    dabei umbenannt. `-f` haelt an der alten Datei fest und zeigte nach der
    Rotation nichts mehr an.
    """
    manager, _ = _updater_scripts()
    # Anker: Zeilenumbruch + 8 Leerzeichen = Hauptmenue. Ohne den Umbruch
    # wuerde auch das Analyse-Untermenue (12 Leerzeichen) treffen.
    option1 = _hauptmenue_block(manager, "1)")
    assert "tail -n 20 -F" in option1
    assert "wait_for_key" in option1


def test_header_wird_gecacht_und_nach_zustandswechsel_verworfen():
    """Der Header startet 4x git und einmal python3. Beim Navigieren durch
    die Optionen zahlt man das sonst jedes Mal neu.

    Wichtig: Nach Aktionen, die den Zustand veraendern, muss der Cache
    aktiv verworfen werden - sonst zeigt der Header z.B. weiter 'Dienst
    INAKTIV', obwohl gerade neu gestartet wurde.
    """
    manager, _ = _updater_scripts()
    assert "header_cache_ist_alt() {" in manager
    assert "header_verwerfen() {" in manager
    assert "if header_cache_ist_alt; then" in manager
    # Cache wird NACH der Berechnung gefuellt, nicht davor.
    reihenfolge = manager.index("HEADER_CACHE_STAMP=$(date +%s")
    assert reihenfolge > manager.index("MANAGER_STATUS=$(python3")
    # Zustandsaendernde Optionen verwerfen den Cache. Anker mit fuehrendem
    # Zeilenumbruch + exakt 8 Leerzeichen: das Analyse-Untermenue nutzt
    # dieselben Nummern, aber mit 12 Leerzeichen eingerueckt.
    for option in ("5)", "6)", "7)"):
        block = _hauptmenue_block(manager, option)
        assert "header_verwerfen" in block, f"Option {option} cacht den Header nicht"


def test_remote_zeile_gibt_den_vergleich_als_lokal_kenntlich():
    """'auf aktuellem Stand' war eine Behauptung: der Abstand kommt aus dem
    lokalen Branch-Ref, es wird gar nicht gefetcht (Timeout/Passphrase).
    """
    manager, _ = _updater_scripts()
    assert "laut lokalem Ref" in manager
    assert "kein Live-Fetch" in manager


def test_deploy_kehrt_in_den_manager_zurueck():
    """Nach Option 4 (Update & Deploy) muss man im Manager landen, nicht in
    einer zweiten Runde des Deploy-Menues. rpi-deploy.sh restartet sich
    deshalb nicht selbst, wenn WPS_DEPLOY_NO_RELOAD gesetzt ist, und das
    Menue laedt sich ueber den Launcher neu (mit dessen Last-Good-Pruefung).
    """
    manager, deploy = _updater_scripts()
    # Anker: Zeilenumbruch + 8 Leerzeichen (Hauptmenue, nicht Analyse-Untermenue).
    option4 = _hauptmenue_block(manager, "4)")
    assert "WPS_DEPLOY_NO_RELOAD=1" in option4
    assert 'exec sh "$SCRIPT_DIR/wp-manager.sh"' in option4
    assert "header_verwerfen" in option4
    # rpi-deploy.sh: Selbst-Neustart nur noch ueber reload_oder_ende().
    assert "reload_oder_ende() {" in deploy
    assert "WPS_DEPLOY_NO_RELOAD" in deploy
    # Genau EIN exec auf SCRIPT_PATH - nämlich in reload_oder_ende().
    # Weitere direkte Aufrufe wuerden das Deploy-Menue erneut oeffnen.
    assert deploy.count('exec sh "$SCRIPT_PATH" "$@"') == 1
    assert deploy.count("reload_oder_ende ") == 4  # 4 Aufrufstellen


def test_service_stopp_wird_bestaetigt():
    manager, _ = _updater_scripts()
    assert "Trotzdem jetzt stoppen? (j/N):" in manager
    assert "Abbruch - Dienst laeuft weiter." in manager


def test_pager_hat_fallback_wenn_more_fehlt():
    manager, _ = _updater_scripts()
    assert "zeige()" in manager
    assert "command -v more" in manager
    # Kein direkter Pager-Aufruf mehr ausser ueber den Fallback.
    assert "| more" not in manager


def test_neue_optionen_health_und_qualitaet():
    manager, _ = _updater_scripts()
    assert "22) show_control_health ;;" in manager
    assert "23) show_analysis_quality ;;" in manager
    assert "manager_health.py" in manager
    assert "/health" in manager
    assert "quality_report.json" in manager


def test_menueintrraege_22_23_stehen_im_hauptmenue_nicht_im_untermenue():
    """Die Anzeigezeilen muessen neben der Exit-Zeile stehen.

    Regression: Beim Einfuegen wurde die erste passende `printf "0)`-Zeile
    getroffen - das ist die Exit-Zeile des Analyse-Untermenues. Die Optionen
    waren dadurch nur im Untermenue sichtbar, obwohl die Aufrufe korrekt
    im Hauptmenue case-Zweig lagen.
    """
    manager, _ = _updater_scripts()
    zeilen = [z.strip() for z in manager.splitlines()]

    exit_idx = [
        i for i, z in enumerate(zeilen) if z.startswith('printf "0)') and "Exit" in z
    ]
    assert len(exit_idx) == 1, f"genau eine Exit-Zeile erwartet, gefunden: {exit_idx}"
    fenster = zeilen[exit_idx[0] - 4: exit_idx[0] + 1]
    assert any(z.startswith('printf "22)') for z in fenster), (
        "Option 22 wird im Hauptmenue nicht angezeigt"
    )
    assert any(z.startswith('printf "23)') for z in fenster), (
        "Option 23 wird im Hauptmenue nicht angezeigt"
    )

    # In keinem Untermenue (endet mit "Zurueck zum Hauptmenue") darf 22/23
    # erscheinen.
    #
    # Angepasst ab Option 24: es gibt mit dem API-Schluessel-Untermenue
    # eine zweite Rueckkehrzeile. Vorher stand hier "genau eine", was
    # still voraussetzte, dass es nur ein Untermenue gibt - ein zweites
    # liess den Test an einer Stelle scheitern, die nichts mit 22/23 zu
    # tun hat. Geprueft wird jetzt jedes Untermenue einzeln.
    zurueck_idx = [
        i for i, z in enumerate(zeilen) if z.startswith('printf "0)') and "Zurueck" in z
    ]
    assert zurueck_idx, "keine Rueckkehrzeile im Untermenue gefunden"
    for idx in zurueck_idx:
        untermenue = zeilen[idx - 8: idx + 1]
        assert not any(z.startswith('printf "22)') for z in untermenue), (
            f"Option 22 erscheint im Untermenue vor Zeile {idx}"
        )
        assert not any(z.startswith('printf "23)') for z in untermenue), (
            f"Option 23 erscheint im Untermenue vor Zeile {idx}"
        )


# --- Banner-Version: abgeleitet statt hardcodiert ----------------------


def _menu_text():
    return (UPDATER_DIR / "wp-manager-menu.sh").read_text(encoding="utf-8")


def test_banner_version_ist_abgeleitet_nicht_hardcodiert():
    """REGRESSION: das Banner zeigte v1.16, der Code war schon v1.17.

    Genau dieser Drift ist zweimal passiert (siehe Changelog im Menue). Eine
    feste Version im Banner kann nicht mehr nachlaufen - sie wird aus dem
    Changelog-Kopf abgeleitet.
    """
    text = _menu_text()
    assert "Manager v1." not in text, (
        "die Version steht noch fest im Banner - sie kann nicht nachlaufen"
    )
    assert "MANAGER_VERSION" in text, "die Version muss abgeleitet werden"


def test_abgeleitete_version_ist_die_hoechste_changelog_version():
    """Die abgeleitete Version muss der hoechsten v1.x-Zeile im Changelog
    entsprechen - und natuerlich sortiert (v1.9 vor v1.10)."""
    import re
    import shutil
    import subprocess

    text = _menu_text()
    versionen = re.findall(r"^# (v1\.\d+)", text, re.M)
    assert versionen, "im Changelog steht keine v1.x-Zeile"
    erwartet = max(set(versionen), key=lambda v: tuple(int(t) for t in v[1:].split(".")))

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("keine POSIX-Shell verfuegbar")
    # Dieselbe Pipeline, die das Menue benutzt.
    res = subprocess.run(
        [
            sh, "-c",
            "grep -oE '^# v1\\.[0-9]+' \"$1\" | grep -oE 'v1\\.[0-9]+' "
            "| sort -V | tail -1",
            "sh", str(UPDATER_DIR / "wp-manager-menu.sh"),
        ],
        capture_output=True, text=True, timeout=30,
    )
    abgeleitet = res.stdout.strip()
    assert abgeleitet == erwartet, (
        f"die Shell-Logik liefert {abgeleitet!r}, erwartet {erwartet!r}"
    )


def test_nicht_interaktiver_deploy_wird_unterstuetzt():
    """`wps --deploy` soll den Deploy ohne Menue ausfuehren.

    Damit laesst sich die Anlage von unterwegs aktualisieren:
    ssh patrik@raspberrypiz2 wps --deploy
    """
    text = _menu_text()
    assert "--deploy)" in text, "das Menue behandelt '--deploy' nicht"
    assert "rpi-deploy.sh" in text, "der Deploy wird nicht aufgerufen"
    # Und der Wrapper reicht das Argument durch (er leitet alles Unbekannte
    # an den Launcher weiter).
    wrapper = (UPDATER_DIR / "wps").read_text(encoding="utf-8")
    assert 'exec sh "$LAUNCHER" "$@"' in wrapper, (
        "der Wrapper muss alle Argumente an den Launcher durchreichen"
    )
    assert "--deploy" in wrapper, "der Wrapper nennt '--deploy' nicht in der Hilfe"


def test_unbekannte_argumente_werden_abgewiesen():
    """REGRESSION: ein Tippfehler im Argument startete still das Menue.

    "wps --deply" lieferte das volle Menue - der Fehler war nicht zu
    sehen. Jetzt gibt es eine Meldung und Exitcode 2.
    """
    text = _menu_text()
    assert "--*)" in text, "unbekannte Argumente werden nicht abgewiesen"
    assert "Unbekanntes Argument" in text


# --- git-fetch-Diagnose: SSH-Key oder Netzwerk? ------------------------


def _deploy_text():
    return (UPDATER_DIR / "rpi-deploy.sh").read_text(encoding="utf-8")


def test_fetch_fehlerpfade_werten_die_ursache_aus():
    """REGRESSION: es wurde immer "Netzwerk" gemeldet.

    Auf dem Pi schlug der Deploy mit "Permission denied (publickey)" fehl
    (der SSH-Key war nach dem Reboot nicht im Agent) - gemeldet wurde
    "GitHub nicht erreichbar? Bitte Netz/WireGuard pruefen". Das fuehrte
    bei der Fehlersuche in die falsche Richtung: das VPN war aktiv, der
    Key fehlte.
    """
    text = _deploy_text()
    assert 'print_fetch_hint 1 ""' not in text, (
        "die Fehlerpfade melden immer noch fest 'Netzwerk' - die Ursache "
        "wird nicht ausgewertet"
    )
    assert "melde_fetch_fehler" in text, "die Ursachen-Auswertung fehlt"
    assert "klassifiziere_fetch_fehler" in text, "die Klassifikation fehlt"


def test_fetch_fehler_wird_korrekt_klassifiziert():
    """Die echte Shell-Funktion muss publickey als Key-Problem erkennen."""
    import shutil
    import subprocess

    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("keine POSIX-Shell verfuegbar")
    deploy = UPDATER_DIR / "rpi-deploy.sh"
    res = subprocess.run(
        [
            sh, "-c",
            "sed -n '/^klassifiziere_fetch_fehler() {/,/^}/p' \"$1\" > /tmp/_kf.sh; "
            ". /tmp/_kf.sh; rm -f /tmp/_kf.sh; klassifiziere_fetch_fehler \"$2\"",
            "sh", str(deploy),
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository.",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert res.stdout.strip() == "2", (
        f"publickey muss als SSH-Key-Problem erkannt werden, war {res.stdout.strip()!r}"
    )


def test_header_zeigt_den_ssh_key_status():
    """Der Header muss zeigen, ob ein SSH-Key geladen ist.

    Nach einem Reboot ist der ssh-agent weg - der Key wird nicht mehr
    angeboten und jeder Deploy scheitert. Das sieht man so VOR dem
    Versuch statt erst in der Fehlermeldung.
    """
    text = _menu_text()
    assert "SSH_KEY_STATE" in text, "der SSH-Key-Status fehlt"
    assert "ssh-add -l" in text, "der Key-Status wird nicht geprueft"
    assert "Deploy moeglich" in text, "die Meldung nennt keine Abhilfe"
    assert "ssh-add ~/.ssh/id_ed25519" in text, "die Abhilfe fehlt"


# --- SSH-Agent: der Key muss ohne Handgriff funktionieren --------------


def test_ssh_agent_hilfe_existiert_und_wird_eingebunden():
    """REGRESSION: "ssh-add" scheiterte mit "Could not open a connection
    to your authentication agent".

    Auf dem Pi laeuft der ssh-agent nach einem Reboot nicht - er wird
    nur beim Login per 'eval $(ssh-agent -s)' gestartet. Ohne ihn
    scheitert JEDER Deploy an "Permission denied (publickey)", auch wenn
    der Key auf der Platte liegt.

    Der Manager startet den Agent deshalb selbst und laedt den Key nach.
    """
    hilfe = UPDATER_DIR / "ssh_agent.sh"
    assert hilfe.exists(), "Updater/ssh_agent.sh fehlt"
    with open(hilfe, encoding="utf-8") as f:
        inhalt = f.read()
    assert "sorge_fuer_ssh_agent" in inhalt, "die Hilfsfunktion fehlt"
    assert "ssh-agent -s" in inhalt, "der Agent wird nicht gestartet"
    assert "ssh-add" in inhalt, "der Key wird nicht geladen"

    # Beide Wege muessen die Hilfe einbinden: Menue und direkter Deploy.
    assert 'ssh_agent.sh' in _menu_text(), "das Menue bindet die Hilfe nicht ein"
    assert 'ssh_agent.sh' in _deploy_text(), "rpi-deploy.sh bindet die Hilfe nicht ein"


def test_ssh_agent_startet_den_agent_und_laedt_den_key(tmp_path):
    """Der Funktionstest: ohne Agent wird einer gestartet und der Key
    nachgeladen. Genau der Ablauf, der auf dem Pi fehlschlug."""
    import shutil
    import subprocess

    sh = shutil.which("sh")
    keygen = shutil.which("ssh-keygen")
    if sh is None or keygen is None:
        pytest.skip("sh oder ssh-keygen nicht verfuegbar")
    hilfe = UPDATER_DIR / "ssh_agent.sh"
    # Ein Key OHNE Passphrase - die Passphrase kann hier nicht interaktiv
    # eingegeben werden, das Verhalten ist aber ansonsten identisch.
    key = tmp_path / "id_ed25519"
    subprocess.run(
        [keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        capture_output=True, timeout=60,
    )
    if not key.exists():
        pytest.skip("ssh-keygen konnte keinen Key erzeugen")

    skript = (
        f'. "{hilfe}"\n'
        f'export WPS_SSH_KEY="{key}"\n'
        'unset SSH_AUTH_SOCK\n'
        'if sorge_fuer_ssh_agent; then echo "ERGEBNIS:OK"; else echo "ERGEBNIS:FEHLER"; fi\n'
        'ssh-add -l 2>/dev/null | grep -c . | sed "s/^/KEYS:/"\n'
    )
    res = subprocess.run(
        [sh, "-c", skript], capture_output=True, text=True, timeout=60
    )
    ausgabe = res.stdout
    assert "ERGEBNIS:OK" in ausgabe, (
        f"der Agent wurde nicht gestartet / der Key nicht geladen: {ausgabe} {res.stderr[:200]}"
    )
    anzahl = 0
    for zeile in ausgabe.splitlines():
        if zeile.startswith("KEYS:"):
            anzahl = int(zeile.split(":", 1)[1].strip() or 0)
    assert anzahl >= 1, f"kein Key im Agent: {ausgabe}"
