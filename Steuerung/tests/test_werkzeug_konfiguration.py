"""Vertrag: Werkzeug-Konfiguration bleibt widerspruchsfrei.

Zwei Befunde, die beide "nur" die Konfiguration betrafen und deshalb
nichts kaputt machten - aber die Fehlersuche teuer:

1. Die pytest-Konfiguration lag in `Steuerung/`. Ein nacktes
   `python -m pytest` im Repo-Root fand sie nicht, sammelte
   `Steuerung/archive/` ein und brach mit 2 Sammelfehlern ab.
   `pytest Steuerung/tests` (der CI-Aufruf) war davon nie betroffen.

2. Drei widerspruechliche Python-Versionen: README-Badge "3.9+",
   ruff `target-version = "py39"`, mypy `python_version = 3.12`.

Diese Tests pruefen die Absicht, nicht die Optik - deshalb `contract`.
"""
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PYTEST_INI = os.path.join(REPO_ROOT, "pytest.ini")
RUFF_TOML = os.path.join(REPO_ROOT, "ruff.toml")


def _text(pfad):
    with open(pfad, encoding="utf-8") as fh:
        return fh.read()


# --- 1. pytest-Konfiguration ------------------------------------------------

def test_pytest_konfiguration_liegt_im_repo_root():
    """Sonst greift ein nackter pytest-Aufruf im Root nicht."""
    assert os.path.isfile(PYTEST_INI), (
        "pytest.ini muss im Repo-Root liegen, nicht in Steuerung/ - sonst "
        "bricht `python -m pytest` im Root mit Sammelfehlern aus "
        "Steuerung/archive/ ab")


def test_pytest_ignoriert_archive_und_caches():
    """Die Altlast in archive/ ist absichtlich nicht Teil der Suite."""
    text = _text(PYTEST_INI)
    assert "archive" in text, "archive/ muss von der Sammlung ausgeschlossen sein"
    assert "__pycache__" in text


def test_pytest_testpaths_zeigt_auf_steuerung_tests():
    text = _text(PYTEST_INI)
    treffer = re.search(r"^testpaths\s*=\s*(.+)$", text, re.MULTILINE)
    assert treffer, "testpaths fehlt"
    assert "Steuerung/tests" in treffer.group(1)


def test_keine_zweite_pytest_konfiguration_in_steuerung():
    """Zwei Konfigurationen = zwei Wahrheiten (die alte war das Problem)."""
    assert not os.path.isfile(os.path.join(REPO_ROOT, "Steuerung", "pytest.ini"))


# --- 2. Python-Zielversion --------------------------------------------------

def test_ruff_und_mypy_nennen_dieselbe_python_version():
    """py39 gegen 3.12 war der eigentliche Widerspruch."""
    ruff = _text(RUFF_TOML)
    m = re.search(r'target-version\s*=\s*"py(\d+?)(\d+)"', ruff)
    assert m, "target-version fehlt in ruff.toml"
    ruff_version = f"{m.group(1)}.{m.group(2)}"

    mypy = _text(os.path.join(REPO_ROOT, "Steuerung", "mypy.ini"))
    m2 = re.search(r"^python_version\s*=\s*(\d+)\.(\d+)", mypy, re.MULTILINE)
    assert m2, "python_version fehlt in mypy.ini"
    mypy_version = f"{m2.group(1)}.{m2.group(2)}"

    assert ruff_version == mypy_version, (
        f"ruff prueft gegen {ruff_version}, mypy gegen {mypy_version}")


def test_ci_benutzt_dieselbe_version_wie_ruff():
    workflow = _text(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))
    m = re.search(r"python-version:\s*\"?(\d+)\.(\d+)\"?", workflow)
    assert m, "python-version fehlt in der CI"
    ci_version = f"{m.group(1)}.{m.group(2)}"

    ruff = _text(RUFF_TOML)
    m2 = re.search(r'target-version\s*=\s*"py(\d+?)(\d+)"', ruff)
    ruff_version = f"{m2.group(1)}.{m2.group(2)}"

    assert ci_version == ruff_version, (
        f"CI testet {ci_version}, ruff prueft {ruff_version}")


def test_readme_badge_nennt_die_richtige_version():
    """Das Badge ist die erste Angabe, die ein Leser sieht."""
    readme = _text(os.path.join(REPO_ROOT, "README.md"))
    m = re.search(r"badge/Python-(\d+)\.(\d+)\+", readme)
    assert m, "Python-Badge fehlt im README"
    badge = f"{m.group(1)}.{m.group(2)}"
    assert badge == "3.12", f"README bewirbt {badge}, getestet wird 3.12"


def test_readme_erklaert_die_voraussetzung():
    """Ohne Hinweis installiert man 3.9 und wundert sich ueber Syntaxfehler."""
    readme = _text(os.path.join(REPO_ROOT, "README.md"))
    assert "Voraussetzung: Python 3.12" in readme