# -*- coding: utf-8 -*-
"""Code-Stand (Git-Revision) fuer ALLE Logs - eine Quelle.

Nutzerwunsch 18.10.2026: "Helfen wuerde, wenn bei jedem Neustart des Programms
der Commit in allen Logs stehen wuerde, dann koennte man es besser
analysieren." Anlass: Entscheidungslog und Zyklus-CSV werden monatlich
rotiert und mischen damit mehrere Code-Staende in EINER Datei - ohne
Revisionsangabe ist spaeter nicht entscheidbar, welcher Fix schon lief.

Das Startup-Journal nannte die Revision schon (`main._git_revision`, Zeile
"Start WPSteuerung | GitHub-Rev: ..."). Dieses Modul ist die gemeinsame Quelle
fuer Journal, Entscheidungslog (`rev` je Zeile) und Zyklus-CSV (`rev` je Zeile).

Ermittlung in dieser Reihenfolge:
  1. Umgebungsvariable ``WPS_GIT_REVISION`` (wie ``Analyse/analysis_core.py``),
  2. ``git`` im Repo (kurzer Hash, Branch, Betreff),
  3. sonst ``unbekannt``.

Ein ``+`` hinter dem Hash markiert lokale Aenderungen an Python-Dateien
(``git status --porcelain --untracked-files=no -- *.py``): der laufende Code
entspricht dann NICHT dem Commit. Laufzeit-Daten (CSV, JSON, Logs) zaehlen
bewusst nicht als Aenderung, sonst waere jedes Deployment dauerhaft "dirty".
"""
import os
import subprocess

#: Kennung, wenn die Revision nicht ermittelbar ist (kein Repo, kein git).
UNBEKANNT = "unbekannt"

_STEER_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_STEER_DIR)
_TIMEOUT_S = 5

_cache = {}


def _git(*args) -> str:
    """git-Aufruf im Repo; leerer String bei jedem Fehler (Robust-Fallback)."""
    try:
        aus = subprocess.run(
            ["git", "-C", _REPO, *args],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return aus.stdout.strip() if aus.returncode == 0 else ""


def kurz_hash() -> str:
    """Kurzer Commit-Hash des laufenden Standes, "" wenn nicht ermittelbar."""
    if "hash" not in _cache:
        env = str(os.environ.get("WPS_GIT_REVISION", "")).strip()
        _cache["hash"] = env[:40] if env else _git("rev-parse", "--short", "HEAD")
    return _cache["hash"]


def kennung() -> str:
    """Revisionskennung fuer Logzeilen: Hash, "+" bei lokalen Code-Aenderungen.

    Beispiel: ``0cc0760`` (identisch mit dem Commit) oder ``0cc0760+``
    (python-Dateien lokal geaendert, Stand weicht vom Commit ab) bzw.
    ``unbekannt``.
    """
    if "kennung" not in _cache:
        hash_wert = kurz_hash()
        if not hash_wert:
            _cache["kennung"] = UNBEKANNT
        else:
            geaendert = _git(
                "status", "--porcelain", "--untracked-files=no", "--", "*.py"
            )
            _cache["kennung"] = f"{hash_wert}+" if geaendert else hash_wert
    return _cache["kennung"]


def startup_text() -> str:
    """Format des Startup-Journals: 'hash [branch] Betreff'."""
    hash_wert = kurz_hash()
    if not hash_wert:
        return UNBEKANNT
    branch = _git("branch", "--show-current") or _git(
        "rev-parse", "--abbrev-ref", "HEAD"
    )
    betreff = _git("log", "-1", "--format=%s")
    return f"{hash_wert} [{branch or 'detached'}] {betreff[:80]}"


def reset_cache() -> None:
    """Nur fuer Tests/Neuladen: Zwischenspeicher leeren."""
    _cache.clear()
