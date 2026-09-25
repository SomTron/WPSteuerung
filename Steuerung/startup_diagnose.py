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
import sys
from datetime import datetime, timedelta
from typing import Optional

from atomic_io import atomic_write_json

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
        atomic_write_json(ziel, daten)
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


def lauf_ist_aktiv(pfad: Optional[str] = None) -> bool:
    """True, wenn die in der Statusdatei vermerkte PID noch laeuft.

    Wichtig fuer Anzeigen wie `wp-manager.sh` (Option 19): Solange die Steuerung
    laeuft, steht in der Datei 'sauber_beendet: false' - das Ende wird erst im
    finally gesetzt. Ohne diese Pruefung wuerde ein gesunder, laufender Prozess
    als "unsauber beendet" gemeldet.

    Es wird bewusst kein Signal gesendet (os.kill(pid, 0) beendet unter Windows
    den Prozess!), sondern nur die Prozessliste geprueft.
    """
    daten = _lese_status(pfad) or {}
    pid = daten.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.path.isdir("/proc"):                 # Linux (Produktivsystem)
        return os.path.isdir(f"/proc/{pid}")
    if sys.platform == "win32":                # Entwicklungsrechner/Tests
        return _windows_prozess_existiert(pid)
    return False


def _windows_prozess_existiert(pid: int) -> bool:
    """Prozess-Existenz unter Windows pruefen - OHNE ihn zu beenden.

    os.kill(pid, 0) waere hier gefaehrlich: unter Windows beendet jeder
    Signalwert ausser CTRL_C/CTRL_BREAK den Prozess (TerminateProcess).
    """
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def oom_hinweis_aus_text(text: Optional[str]) -> Optional[str]:
    """Sucht OOM-Kill-Marker im Kernel-Log und gibt die Zeile zurueck."""
    if not text:
        return None
    for zeile in text.splitlines():
        if any(marker in zeile for marker in OOM_MARKER):
            return zeile.strip()[:300]
    return None


def _normalisiere_zeitstempel(zeit: Optional[str]) -> Optional[str]:
    """'2026-09-15T12:02:08' -> '2026-09-15 12:02:08' (journalctl-Format).

    Gibt None zurueck, wenn der Wert nicht plausibel wie ein Datum aussieht.
    """
    if not zeit or not isinstance(zeit, str):
        return None
    text = zeit.strip().replace("T", " ")
    if len(text) >= 16 and text[:4].isdigit() and text[4] == "-":
        return text[:19]
    return None


def kernel_log_lesen(seit: Optional[str] = None) -> Optional[str]:
    """Kernel-Log lesen (best effort): bevorzugt journalctl mit Zeitfenster.

    Mit `seit` wird nur der Zeitraum ab dem letzten Start betrachtet - sonst
    wuerde ein ALTER OOM-Eintrag aus dem Ringpuffer faelschlich dem aktuellen
    unsauberen Ende zugeordnet (dmesg liefert den kompletten Ringpuffer).
    Fallbacks: journalctl -k ohne Fenster, dann dmesg (ungenauer).

    Ohne Berechtigung/Root liefern alle nichts -> None (kein Fehler).
    """
    import shutil
    import subprocess

    befehle = []
    fenster = _normalisiere_zeitstempel(seit)
    if fenster:
        befehle.append(
            ["journalctl", "-k", "--since", fenster, "--no-pager", "-n", "500"]
        )
    befehle.append(["journalctl", "-k", "--no-pager", "-n", "500"])
    befehle.append(["dmesg"])

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

    Liest das Kernel-Log nur, wenn tatsaechlich ein unsauberes Ende vorliegt,
    und dann nur den Zeitraum ab dem vorherigen Start (kein Fehlalarm durch
    alte OOM-Eintraege im Ringpuffer).

    Zusaetzlich wird der Neustart gezaehlt. `Restart=always` mit
    `RestartSec=10` bedeutet: Ein reproduzierbarer Crash erzeugt alle zehn
    Sekunden einen neuen Prozess. Ohne Zaehler wuerde jede Instanz erneut
    eine Absturz-Meldung schicken. Der Zaehler liegt bewusst persistent,
    weil ein Neustart den RAM-Zustand zuruecksetzt.
    """
    letzter = pruefe_letzten_lauf()
    hinweis = None
    if letzter["unsauber"]:
        hinweis = oom_hinweis_aus_text(
            kernel_log_lesen(letzter.get("vorheriger_start"))
        )
    return {**letzter, "oom_hinweis": hinweis,
            "absturze_stunde": zaehle_absturze(letzter["unsauber"])}


ABSTURZ_HISTORIE_DATEI = "absturz_historie.json"
ABSTURZ_FENSTER_MIN = 60
ABSTURZ_STUFE_1 = 3
ABSTURZ_STUFE_2 = 10


def _lade_absturze() -> list:
    try:
        with open(ABSTURZ_HISTORIE_DATEI, "r", encoding="utf-8") as f:
            daten = json.load(f)
        if isinstance(daten, list):
            return [str(z) for z in daten]
    except Exception:
        pass
    return []


def _speichere_absturze(zeitstempel: list) -> None:
    try:
        atomic_write_json(ABSTURZ_HISTORIE_DATEI, zeitstempel[-50:])
    except Exception as e:
        logging.debug(f"Absturz-Historie nicht schreibbar: {e}")


def zaehle_absturze(unsauber: bool, jetzt: Optional[datetime] = None) -> int:
    """Zaehlt unsaubere Beendigungen im Zeitfenster und liefert die Anzahl.

    Bei einem *sauberen* Start wird die Historie zurueckgesetzt: Dann liegt
    kein Neustart-Sturm vor und die naechste echte Stoerung meldet wieder
    sofort.
    """
    jetzt = jetzt or datetime.now()
    if not unsauber:
        if _lade_absturze():
            _speichere_absturze([])
        return 0
    grenze = jetzt - timedelta(minutes=ABSTURZ_FENSTER_MIN)
    frisch = []
    for eintrag in _lade_absturze():
        try:
            ts = datetime.fromisoformat(eintrag)
        except (TypeError, ValueError):
            continue
        if ts >= grenze:
            frisch.append(eintrag)
    frisch.append(jetzt.isoformat())
    _speichere_absturze(frisch)
    return len(frisch)


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