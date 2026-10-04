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
    yaml = pytest.importorskip("yaml")
    with open(WORKFLOW, encoding='utf-8') as fh:
        d = yaml.safe_load(fh)
    assert d.get("jobs"), "keine Jobs in der Workflow-Datei"