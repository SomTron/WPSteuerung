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


def test_keine_leeren_python_dateien():
    """Regression: `constants_clean.py` lag als 0-Byte-Datei im Repo.

    Eine leere Datei wird von ruff und compileall stillschweigend
    akzeptiert, sieht aber wie ein Modul aus und verwirrt beim Import.
    """
    leer = []
    for pfad in _python_dateien():
        if os.path.getsize(pfad) == 0:
            leer.append(os.path.relpath(pfad, REPO_ROOT))
    assert not leer, "Leere Python-Dateien (nicht verwaist):\n" + "\n".join(leer)


def test_keine_python_dateien_mit_bom():
    """Regression: `test_legionellen_plan.py` trug eine unsichtbare BOM.

    Ein BOM am Dateianfang fuehrt beim Parsen zu
    `SyntaxError: invalid non-printable character U+FEFF`.
    """
    mit_bom = []
    for pfad in _python_dateien():
        with open(pfad, "rb") as f:
            if f.read(3) == b"\xef\xbb\xbf":
                mit_bom.append(os.path.relpath(pfad, REPO_ROOT))
    assert not mit_bom, "Dateien mit UTF-8-BOM:\n" + "\n".join(mit_bom)