"""Fruehwarnung vor einem Ausfall der SD-Karte (Raspberry Pi).

Hintergrund: Auf dem Pi liegt fast alles Wichtige auf der SD-Karte - die
Heizungsdaten-CSV, das Entscheidungslog, last_state.txt. Faellt die Karte
aus, ist die Historie weg (die Steuerung laeuft zwar weiter, aber ohne
Daten). Der Ausfall kommt selten ueraschend: die Karte meldet vorher
I/O-Fehler, das Dateisystem zaehlt Fehler, und irgendwann sind
Schreibvorgaenge nicht mehr moeglich.

Diese Pruefung fasst die verfuegbaren Signale zusammen:

  1. Kernel-Meldungen (dmesg): "Buffer I/O error", "EXT4-fs error",
     "I/O error", "mmcblk*: error -84" usw. Das ist das verlaesslichste
     Fruehzeichen - die Fehler treten Wochen vor dem Totalausfall auf.
  2. Dateisystem-Fehlerzaehler (EXT4: /sys/fs/ext4/<dev>/errors_count).
  3. Schreibtest: eine kleine Datei im Datenverzeichnis schreiben, lesen
     und loeschen. Faellt der Schreibvorgang fehl, ist die Karte am Ende.
  4. Datenkonsistenz: ist die Heizungsdaten-CSV lesbar (Header + letzte
     Zeile)? Das schuetzt die Historie direkt.

Wichtig: Die Pruefung liest nur und schreibt eine kleine Testdatei. Sie
ist fuer den Betrieb unkritisch und darf jederzeit laufen.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

# I/O- und Dateisystem-Fehler im Kernel-Log. Die Muster sind absichtlich
# breit gefasst: ein Treffer ist ein Warnsignal, kein Beweis.
_KERNEL_FEHLER = [
    re.compile(r"Buffer I/O error", re.IGNORECASE),
    re.compile(r"I/O error", re.IGNORECASE),
    # "EXT4-fs (mmcblk0p2): error 5" - der Geraetename steht zwischen
    # Dateisystem und "error". Das Muster muss beides zusammenpassen.
    re.compile(r"EXT4-fs\b.*\berror\b", re.IGNORECASE),
    re.compile(r"EXT4-fs\b.*\bwarning\b", re.IGNORECASE),
    re.compile(r"mmcblk\d*:? .*error", re.IGNORECASE),
    re.compile(r"error -84", re.IGNORECASE),
    re.compile(r"Failed to mount", re.IGNORECASE),
    re.compile(r"Remounting filesystem read-only", re.IGNORECASE),
    re.compile(r"ata.*error", re.IGNORECASE),
    re.compile(r"blk_update_request|critical medium", re.IGNORECASE),
]


def _dmesg_lesen() -> List[str]:
    """Liest dmesg. Gibt [] zurueck, wenn es nicht lesbar ist (z. B. Windows)."""
    try:
        with open("/dev/kmsg", "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        pass
    try:
        import subprocess
        res = subprocess.run(
            ["dmesg", "--time-format=iso"], capture_output=True, timeout=10
        )
        if res.returncode == 0:
            return res.stdout.decode("utf-8", "replace").splitlines()
    except (OSError, ValueError, ImportError):
        pass
    return []


def kernel_fehler(zeilen: Optional[List[str]] = None) -> List[str]:
    """I/O- und Dateisystem-Fehler aus dem Kernel-Log."""
    if zeilen is None:
        zeilen = _dmesg_lesen()
    treffer: List[str] = []
    for z in zeilen:
        for muster in _KERNEL_FEHLER:
            if muster.search(z):
                treffer.append(z.strip()[:200])
                break
    return treffer


def dateisystem_fehler() -> Optional[int]:
    """EXT4-Fehlerzaehler des Dateisystems, auf dem die Daten liegen.

    Gibt None zurueck, wenn die Information nicht verfuegbar ist (z. B.
    ext3, squashfs oder ein anderes Dateisystem).
    """
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for zeile in f:
                teile = zeile.split()
                if len(teile) < 3:
                    continue
                if not teile[2].startswith("ext4"):
                    continue
                # Der Geraetename (z. B. /dev/mmcblk0p2) -> sysfs-Pfad
                geraet = teile[0].rsplit("/", 1)[-1]
                kandidaten = [
                    f"/sys/fs/ext4/{geraet}/errors_count",
                    f"/sys/fs/ext4/{teile[0]}/errors_count",
                ]
                for kandidat in kandidaten:
                    try:
                        with open(kandidat, "r", encoding="utf-8") as g:
                            return int(g.read().strip())
                    except (OSError, ValueError):
                        continue
    except OSError:
        pass
    return None


def schreibtest(verzeichnis: str) -> Dict:
    """Schreibt, liest und loescht eine kleine Testdatei.

    Erkennt eine Karte, auf der nicht mehr geschrieben werden kann.
    """
    ergebnis = {"ok": False, "verzeichnis": verzeichnis, "fehler": None}
    if not verzeichnis or not os.path.isdir(verzeichnis):
        ergebnis["fehler"] = "Verzeichnis existiert nicht"
        return ergebnis
    pfad = os.path.join(verzeichnis, ".sd_health_test")
    inhalt = f"probe {time.time()}"
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            f.write(inhalt)
            f.flush()
            os.fsync(f.fileno())
        with open(pfad, "r", encoding="utf-8") as f:
            gelesen = f.read()
        os.remove(pfad)
        ergebnis["ok"] = gelesen == inhalt
        if not ergebnis["ok"]:
            ergebnis["fehler"] = "Inhalt nach dem Schreiben abweichend"
    except OSError as exc:
        ergebnis["fehler"] = f"{type(exc).__name__}: {exc}"
        try:
            if os.path.exists(pfad):
                os.remove(pfad)
        except OSError:
            pass
    return ergebnis


def csv_konsistenz(csv_pfad: str, mindest_zeilen: int = 1) -> Dict:
    """Prueft, ob die Heizungsdaten-CSV lesbar und plausibel ist.

    Ein leeres oder abgeschnittenes Log waere ein stummer Datenverlust -
    genau das, was der Check verhindern soll.
    """
    ergebnis = {"ok": False, "pfad": csv_pfad, "fehler": None, "zeilen": 0}
    if not csv_pfad or not os.path.exists(csv_pfad):
        # Keine Datei ist noch kein Fehler - sie entsteht beim ersten Lauf.
        ergebnis["ok"] = True
        return ergebnis
    try:
        groesse = os.path.getsize(csv_pfad)
        ergebnis["groesse"] = groesse
        with open(csv_pfad, "r", encoding="utf-8", errors="replace") as f:
            header = f.readline()
            zeilen = 0
            for zeile in f:
                if zeile.strip():
                    zeilen += 1
        ergebnis["zeilen"] = zeilen
        if groesse == 0:
            ergebnis["fehler"] = "CSV ist leer"
        elif not header.strip():
            ergebnis["fehler"] = "CSV ohne Kopfzeile"
        elif zeilen < mindest_zeilen:
            ergebnis["fehler"] = f"nur {zeilen} Datenzeilen"
        else:
            ergebnis["ok"] = True
    except OSError as exc:
        ergebnis["fehler"] = f"{type(exc).__name__}: {exc}"
    return ergebnis


def pruefe(
    daten_verzeichnis: str,
    csv_pfad: str = "",
    dmesg_zeilen: Optional[List[str]] = None,
) -> Dict:
    """Fuehrt die gesamte Pruefung durch. Liefert einen Status-Dict.

    `sd_ok` ist True, wenn kein Warnzeichen gefunden wurde. Die einzelnen
    Teilergebnisse bleiben erhalten, damit der Anwender sieht, woran es
    liegt.
    """
    kernel = kernel_fehler(dmesg_zeilen)
    fs_fehler = dateisystem_fehler()
    schreiben = schreibtest(daten_verzeichnis)
    csv = csv_konsistenz(csv_pfad) if csv_pfad else {"ok": True, "fehler": None}

    hinweise: List[str] = []
    if kernel:
        hinweise.append(
            f"{len(kernel)} I/O-Meldung(en) im Kernel-Log "
            f"(z. B. {kernel[0][:80]})"
        )
    if fs_fehler:
        hinweise.append(f"Dateisystem meldet {fs_fehler} Fehler")
    if not schreiben["ok"]:
        hinweise.append(f"Schreibtest fehlgeschlagen: {schreiben['fehler']}")
    if not csv.get("ok"):
        hinweise.append(f"CSV nicht plausibel: {csv.get('fehler')}")

    return {
        "sd_ok": not hinweise,
        "hinweise": hinweise,
        "kernel_fehler": kernel[:10],
        "kernel_fehler_anzahl": len(kernel),
        "dateisystem_fehler": fs_fehler,
        "schreibtest": schreiben,
        "csv": csv,
        "geprueft_am": datetime.now().isoformat(timespec="seconds"),
    }
