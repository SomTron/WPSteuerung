# -*- coding: utf-8 -*-
"""Startup-Diagnose: erkennt unsauber beendete Laeufe (OOM-Kill, Crash, Reset).

Hintergrund (15.09.): Der Kernel hat den Prozess mit SIGKILL beendet
(OOM-Killer: 'Killed process ... (python) anon-rss:257104kB' bei 416 MB RAM).
Im Steuerungs-Log sah das wie ein voellig normaler Neustart aus - die Ursache
war nur im Kernel-Journal sichtbar ("still").

Dieses Modul schreibt beim Start/Ende eine kleine Statusdatei und meldet beim
naechsten Start, ob der vorige Lauf sauber endete. Zusaetzlich liefert es
Speicherwerte (RSS/verfuegbar) fuer ein stuendliches Log.

Wichtig: Alles ist fehlertolerant und braucht KEINE root-Rechte - die Diagnose
darf den Start niemals verhindern.
"""
import json
import logging
import os
from datetime import datetime
from typing import Optional

# Liegt im Arbeitsverzeichnis der Steuerung (wie last_state.txt) und ist
# per .gitignore von der Versionierung ausgenommen.
LAUF_STATUS_DATEI = "letzter_lauf.json"

OOM_MARKER = ("Out of memory", "Killed process")


def _lese_status(pfad: Optional[str] = None) -> Optional[dict]:
    ziel = pfad or LAUF_STATUS_DATEI
    try:
        with open(ziel, "r", encoding="utf-8") as f:
            daten = json.load(f)
        return daten if isinstance(daten, dict) else None
    except Exception:
        return None


def _schreibe_status(daten: dict, pfad: Optional[str] = None) -> bool:
    ziel = pfad or LAUF_STATUS_DATEI
    try:
        with open(ziel, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False)
        return True
    except Exception as e:
        logging.debug(f"Lauf-Status konnte nicht geschrieben werden: {e}")
        return False


def markiere_lauf_start(pfad: Optional[str] = None) -> bool:
    """Merkt 'Lauf laeuft' - bleibt stehen, falls der Prozess hart gekillt wird."""
    return _schreibe_status({
        "sauber_beendet": False,
        "start_zeit": datetime.now().isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }, pfad)


def markiere_sauberes_ende(pfad: Optional[str] = None) -> bool:
    """Wird im finally des Main-Loops gerufen (z. B. bei systemctl restart)."""
    daten = _lese_status(pfad) or {}
    daten.update({
        "sauber_beendet": True,
        "ende_zeit": datetime.now().isoformat(timespec="seconds"),
    })
    return _schreibe_status(daten, pfad)


def pruefe_letzten_lauf(pfad: Optional[str] = None) -> dict:
    """Info ueber den vorigen Lauf.

    Fehlende Statusdatei = erster Start (kein Fehlalarm).
    """
    daten = _lese_status(pfad)
    if daten is None:
        return {
            "unsauber": False,
            "erster_start": True,
            "vorheriger_start": None,
            "vorheriges_ende": None,
        }
    sauber = bool(daten.get("sauber_beendet", False))
    return {
        "unsauber": not sauber,
        "erster_start": False,
        "vorheriger_start": daten.get("start_zeit"),
        "vorheriges_ende": daten.get("ende_zeit"),
    }


def oom_hinweis_aus_text(text: Optional[str]) -> Optional[str]:
    """Sucht OOM-Kill-Marker im Kernel-Log und gibt die Zeile zurueck."""
    if not text:
        return None
    for zeile in text.splitlines():
        if any(marker in zeile for marker in OOM_MARKER):
            return zeile.strip()[:300]
    return None


def kernel_log_lesen() -> Optional[str]:
    """Kernel-Log lesen (best effort): dmesg, sonst journalctl -k.

    Ohne Berechtigung/Root liefern beide nichts -> None (kein Fehler).
    """
    import shutil
    import subprocess

    befehle = (
        ["dmesg"],
        ["journalctl", "-k", "-n", "500", "--no-pager"],
    )
    for befehl in befehle:
        if shutil.which(befehl[0]) is None:
            continue
        try:
            ergebnis = subprocess.run(
                befehl, capture_output=True, text=True, timeout=5, check=False
            )
            if ergebnis.returncode == 0 and ergebnis.stdout:
                return ergebnis.stdout
        except Exception:
            continue
    return None


def pruefe_lauf_start_grund() -> dict:
    """Kombiniert: Wurde der vorige Lauf unsauber beendet - und war es OOM?

    Liest das Kernel-Log nur, wenn tatsaechlich ein unsauberes Ende vorliegt.
    """
    letzter = pruefe_letzten_lauf()
    hinweis = None
    if letzter["unsauber"]:
        hinweis = oom_hinweis_aus_text(kernel_log_lesen())
    return {**letzter, "oom_hinweis": hinweis}


def speicher_werte(
    meminfo_pfad: str = "/proc/meminfo",
    status_pfad: str = "/proc/self/status",
) -> dict:
    """MemTotal/MemAvailable (kB) + eigene RSS (kB). Nicht verfuegbar -> None."""
    werte = {"total_kb": None, "verfuegbar_kb": None, "rss_kb": None}
    try:
        with open(meminfo_pfad, "r", encoding="utf-8") as f:
            for zeile in f:
                if zeile.startswith("MemTotal:"):
                    werte["total_kb"] = int(zeile.split()[1])
                elif zeile.startswith("MemAvailable:"):
                    werte["verfuegbar_kb"] = int(zeile.split()[1])
    except Exception:
        pass
    try:
        with open(status_pfad, "r", encoding="utf-8") as f:
            for zeile in f:
                if zeile.startswith("VmRSS:"):
                    werte["rss_kb"] = int(zeile.split()[1])
                    break
    except Exception:
        pass
    return werte


def formatiere_speicher(werte: dict) -> str:
    """z. B. 'RSS=53 MB | verfuegbar=232 MB | total=416 MB'."""
    def _mb(wert):
        return f"{wert / 1024:.0f} MB" if isinstance(wert, (int, float)) else "-"

    return (
        f"RSS={_mb(werte.get('rss_kb'))} | "
        f"verfuegbar={_mb(werte.get('verfuegbar_kb'))} | "
        f"total={_mb(werte.get('total_kb'))}"
    )