"""Tests: Die CI-Datei bleibt gueltig und benutzt keine Node-20-Actions mehr.

Ausloeser war die Runner-Warnung:

    Node.js 20 is deprecated ... actions/checkout@v4,
    actions/setup-python@v5 ... forced to run on Node.js 24

Node 20 wurde laut GitHub-Changelog am 23.09.2026 endgueltig entfernt
("Retired Node 20 is no longer available in GitHub Actions"). Die
Warnung war also der letzte Vorlauf, kein Vorbehalt.

Die Mindestversionen sind bewusst nicht einfach auf den jeweils
neuesten Stand gesetzt, sondern gegen das eigene Workflow geprueft:
- checkout v7 blockiert das Auschecken von Fork-PRs bei
  pull_request_target und workflow_run. Dieser Workflow loest nur auf
  push und pull_request aus, ist also nicht betroffen.
- setup-python v7 entfernt das Input `pip-install` und EOL-Python-
  Versionen. Hier wird `cache: pip` genutzt (etwas anderes) und
  Python 3.12, das nicht EOL ist.
"""

import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'ci.yml')

#: Actions-Major, die noch auf Node 20 laufen.
NODE20_ALT = ("actions/checkout@v4", "actions/setup-python@v5")

#: Mindestversionen, die Node 24 verwenden. Bewusst nicht die neuesten,
#: sondern die ersten Node-24-Staende - kleinster Sprung, klarste
#: Ursache. Wer weiter springen will, tut es bewusst.
MINIMUM = {
    "actions/checkout": 5,
    "actions/setup-python": 6,
}


def _text():
    with open(WORKFLOW, encoding='utf-8') as fh:
        return fh.read()


def _verwendete_actions():
    return re.findall(r"uses:\s*([\w.-]+/[\w.-]+)@v(\d+)", _text())


def test_keine_node20_actions_mehr():
    text = _text()
    alt = [a for a in NODE20_ALT if a in text]
    assert not alt, (
        f"diese Actions laufen auf Node 20 und sind dort seit 23.09.2026 "
        f"entfernt: {alt}"
    )


def test_actions_sind_mindestens_node24():
    for name, major in _verwendete_actions():
        if name not in MINIMUM:
            continue          # github-script ist nicht betroffen
        assert int(major) >= MINIMUM[name], (
            f"{name}@{major} laeuft noch auf Node 20; mindestens "
            f"v{MINIMUM[name]} ist noetig"
        )


def test_github_script_bleibt_unangetastet():
    """Nicht in der Warnung gelistet - und der Fehlbericht nutzt require().

    github-script wird mit einem CommonJS-Skript gefuettert (`require`).
    Ein Sprung auf eine ESM-Version wuerde den Fehlerbericht still
    brechen, ohne dass der Testlauf es zeigt.
    """
    assert 'actions/github-script@v7' in _text(), (
        "github-script wurde angefasst - sein Skript nutzt require() und "
        "wuerde auf einer ESM-Version brechen"
    )


def test_yaml_ist_wohlgeformt():
    """Die CI-Datei muss gültiges YAML sein.

    BUGFIX: Vorher stand hier `pytest.importorskip("yaml")`. PyYAML war
    nirgends installiert (weder lokal noch in der CI), also war dieser Test
    ein STILLER Skip - er meldete "gruen", ohne je ein YAML geparst zu haben.
    Eine kaputte Workflow-Datei waere erst beim naechsten Push aufgefallen.

    Jetzt: PyYAML steht in requirements-dev.txt (auf Python < 3.14, dort gibt
    es Wheels). Fehlt es trotzdem, ist das ein Fehler - kein stiller Skip.
    Nur wenn das Paket nachweislich nicht installierbar ist (Python 3.14+,
    kein Wheel), wird mit Begruendung uebersprungen.
    """
    try:
        import yaml
    except ImportError:
        # Nur mit Begruendung ueberspringen, wenn das Paket fuer diese
        # Python-Version gar kein Wheel hat. Sonst ist es ein echter Fehler.
        if sys.version_info >= (3, 14):
            pytest.skip(
                "PyYAML 6.0.3 hat kein Wheel fuer Python "
                f"{sys.version_info.major}.{sys.version_info.minor} - "
                "die YAML-Pruefung laeuft in der CI (3.12)")
        raise AssertionError(
            "PyYAML fehlt - es steht in requirements-dev.txt. Ohne das Paket "
            "wird die Workflow-Datei nie geprueft (stiller Skip).")

    with open(WORKFLOW, encoding='utf-8') as fh:
        d = yaml.safe_load(fh)
    assert isinstance(d, dict), "die Workflow-Datei ist kein YAML-Mapping"
    assert d.get("jobs"), "keine Jobs in der Workflow-Datei"