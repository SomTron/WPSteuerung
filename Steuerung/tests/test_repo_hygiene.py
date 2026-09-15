"""Repository-Hygiene: keine kaputten oder halb-gemergten Quelldateien.

Hintergrund (Aufraeum-Runde D): `Steuerung/archive/legacy_main.py` lag als
UTF-16-Datei mit Mojibake-Umlauten und 13 unaufgeloesten Merge-Konfliktmarkern
im Repo. Weder ruff noch compileall pruefen `archive/`, deshalb fiel das nie
auf. Dieser Test prueft ALLE Python-Dateien des Repos.
"""
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Verzeichnisse ohne eigenen Quellcode (Venvs, Caches, Fremd-Backups)
AUSGESCHLOSSEN = {
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".idea",
    "node_modules", ".ci_venv", "_fremde_aenderungen_backup",
}

KONFLIKT_MARKER = ("<<<<<<<", "=======", ">>>>>>>")


def _python_dateien():
    for wurzel, dirs, dateien in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in AUSGESCHLOSSEN]
        for name in dateien:
            if name.endswith(".py"):
                yield os.path.join(wurzel, name)


def test_alle_python_dateien_sind_utf8():
    kaputt = []
    for pfad in _python_dateien():
        try:
            open(pfad, "rb").read().decode("utf-8")
        except (UnicodeDecodeError, OSError) as e:
            kaputt.append(f"{os.path.relpath(pfad, REPO_ROOT)}: {e}")
    assert not kaputt, "Nicht als UTF-8 lesbar:\n" + "\n".join(kaputt)


def test_keine_merge_konfliktmarker():
    treffer = []
    for pfad in _python_dateien():
        with open(pfad, encoding="utf-8") as f:
            for nr, zeile in enumerate(f, start=1):
                if zeile.startswith(KONFLIKT_MARKER):
                    treffer.append(f"{os.path.relpath(pfad, REPO_ROOT)}:{nr}: {zeile.strip()}")
    assert not treffer, "Unaufgeloeste Merge-Konflikte:\n" + "\n".join(treffer)