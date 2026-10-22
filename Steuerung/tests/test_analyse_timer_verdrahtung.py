"""Verdrahtung des Analyse-Timers (BUGFIX 22.10.2026).

Der Qualitaetsbericht der WebApp entsteht durch `wp-analyse.timer`
(taeglich 04:30). Der Deploy hat den Timer vorher weder installiert
noch aktiviert - auf einem frischen Pi fehlte er komplett, und die
WebApp zeigte dauerhaft "Nicht verfuegbar" ohne erkennbaren Grund.

Diese Tests nageln die Verdrahtung fest: der Deploy muss die Units
uebertragen und den Timer aktivieren.
"""
import os
import re

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEPLOY = os.path.join(REPO, "Updater", "rpi-deploy.sh")


def _deploy_text() -> str:
    with open(DEPLOY, "r", encoding="utf-8") as fh:
        return fh.read()


def test_analyse_units_liegen_im_repo():
    """Die Units muessen im Repo liegen, sonst kann der Deploy sie nicht
    uebertragen."""
    assert os.path.isfile(os.path.join(REPO, "Steuerung", "wp-analyse.timer"))
    assert os.path.isfile(os.path.join(REPO, "Steuerung", "wp-analyse.service"))


def test_deploy_definiert_sync_analyse_timer():
    """Der Deploy muss eine eigene Funktion fuer den Timer haben."""
    text = _deploy_text()
    assert "sync_analyse_timer()" in text, (
        "Der Deploy braucht eine Funktion fuer den Analyse-Timer")


def test_deploy_ruft_sync_analyse_timer_auf():
    """Die Funktion muss auch wirklich aufgerufen werden - eine nur
    definierte Funktion waere wirkungslos.

    Geprueft wird der Aufruf INNERHALB des Deploy-Ablaufs, nicht die
    Definition: `def sync_analyse_timer()` zaehlt nicht als Aufruf.
    """
    text = _deploy_text()
    # Alle Vorkommen ohne die Definition
    ohne_def = text.replace("sync_analyse_timer() {", "")
    aufrufe = re.findall(r"sync_analyse_timer(?!\s*\()", ohne_def)
    # Der Timer muss in JEDEM Deploy-Pfad mitlaufen, in dem auch die
    # Service-Unit synchronisiert wird. Sonst fehlt der Qualitaetsbericht
    # je nach gewaehltem Menuepunkt.
    ohne_service_def = text.replace("sync_service_unit() {", "")
    sync_service_aufrufe = re.findall(
        r"sync_service_unit(?!\s*\()", ohne_service_def)
    assert len(aufrufe) == len(sync_service_aufrufe) >= 1, (
        f"sync_analyse_timer wird {len(aufrufe)}x aufgerufen, "
        f"sync_service_unit {len(sync_service_aufrufe)}x - der Timer muss "
        "in JEDEM Deploy-Pfad mitlaufen, sonst fehlt der Qualitaetsbericht "
        "je nach gewaehltem Menuepunkt")


def test_deploy_installiert_und_aktiviert_den_timer():
    """Installieren UND aktivieren - ein installierter, aber nicht
    aktivierter Timer laeuft nie."""
    text = _deploy_text()
    # Beide Units muessen konkret INSTALLIERT werden - blosses Erwaehnen
    # des Namens reicht nicht.
    assert re.search(r'install[^\n]*"\$timer_quelle"[^\n]*"\$timer_ziel"',
                     text), "Die Timer-Unit wird nicht installiert"
    assert re.search(r'install[^\n]*"\$service_quelle"[^\n]*"\$service_ziel"',
                     text), "Die Service-Unit wird nicht installiert"
    # Spezifisch: der Timer muss per `enable` aktiviert werden.
    # Ein blosses `start` laeuft nur einmal - der Timer waere dann
    # beim naechsten Boot weg.
    assert re.search(r"systemctl\s+enable[^\n]*wp-analyse\.timer", text), (
        "Der Timer muss per 'systemctl enable ... wp-analyse.timer' "
        "aktiviert werden")


def test_timer_ist_einmal_taeglich_aktiviert():
    """Der Timer muss im Repo auf einen taeglichen Takt zeigen."""
    with open(os.path.join(REPO, "Steuerung", "wp-analyse.timer"),
              "r", encoding="utf-8") as fh:
        inhalt = fh.read()
    assert "OnCalendar=" in inhalt
    assert "Persistent=true" in inhalt, (
        "Persistent=true - sonst laeuft die Analyse an einem ausgesetzten "
        "Pi nicht nach")


def test_analyse_service_ist_vollstaendig():
    """Die Unit-Datei muss alles noetige enthalten.

    Wichtig: das ist bewusst KEIN Test der Systemd-Installation. Auf
    dem CI-Runner (Ubuntu ohne installierte Unit) waere ein
    `systemctl cat`-Test sinnlos - er wuerde dort immer scheitern,
    obwohl der Code korrekt ist. Geprueft wird deshalb die Unit-Datei
    selbst, nicht der Systemzustand.
    """
    with open(os.path.join(REPO, "Steuerung", "wp-analyse.service"),
              "r", encoding="utf-8") as fh:
        inhalt = fh.read()
    assert "ExecStart=" in inhalt, "Die Unit startet nichts"
    assert "log_analyse.py" in inhalt, (
        "Die Unit muss die Analyse ausfuehren")
    assert "Type=oneshot" in inhalt, (
        "Die Analyse ist ein einmaliger Lauf, kein Dauerdienst")
    # Die Timer-Datei bindet den Timer an timers.target - nur so
    # startet er beim Boot.
    with open(os.path.join(REPO, "Steuerung", "wp-analyse.timer"),
              "r", encoding="utf-8") as fh:
        timer = fh.read()
    assert "WantedBy=timers.target" in timer, (
        "Der Timer muss bei timers.target aktiviert werden")
    assert "Unit=wp-analyse.service" in timer, (
        "Der Timer muss die Unit wp-analyse.service ausloesen")
