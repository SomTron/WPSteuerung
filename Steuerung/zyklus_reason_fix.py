# -*- coding: utf-8 -*-
"""Korrigiert die Spalte `reason_code` bestehender Zyklus-CSVs (Altbestand).

BEFUND 31.10.2026: `cycle_logging._end_code` wertete den Blocking-Code VOR dem
Endgrund aus. Dadurch trugen 50 von 155 Laeufen (01.-31.10.) den Code
`mindestlaufzeit`, obwohl sie mit `regel_aus` (32), `boiler_max` (7),
`schichtung` (5), `overshoot_vorhersage` (4) bzw. `dienst_neustart` (1)
endeten; 5 weitere waren `unbekannt` (Ueberhitzung/Verdampfer). Der Fix
`abac440` schreibt neue Zyklen richtig - dieses Werkzeug zieht den Bestand
nach.

Aufruf auf dem Pi (im Steuerungsverzeichnis):
    python3 zyklus_reason_fix.py --pfad "csv log/zyklen.csv" --dry-run
    python3 zyklus_reason_fix.py --pfad "csv log/zyklen.csv"
    python3 zyklus_reason_fix.py --pfad "csv log"          # alle zyklen*.csv

Sicherheit:
  * Es wird nur `reason_code` neu berechnet - alle anderen Spalten bleiben
    unveraendert (inkl. Reihenfolge, Semikolon, CRLF, ohne BOM).
  * Vor dem Schreiben entsteht eine Sicherung `<datei>.bak-<zeitstempel>`.
  * Schreiben ist atomar (Temp-Datei + `os.replace`).
  * Aendert sich die Datei waehrend des Einlesens (laufender Dienst), bricht
    das Werkzeug ab. Am saubersten laeuft es bei stehendem Kompressor.
  * Idempotent: ein zweiter Lauf aendert nichts und legt kein Backup an.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import shutil
import sys
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from cycle_logging import _end_code

#: Spalten, ohne die eine Korrektur nicht moeglich ist.
ERWARTETE_SPALTEN = ("end_grund", "reason_code")


def _dateien(pfad: str) -> List[str]:
    """Datei oder Verzeichnis ('zyklen*.csv') aufloesen."""
    if os.path.isdir(pfad):
        return [p for p in sorted(glob.glob(os.path.join(pfad, "zyklen*.csv")))
                if os.path.isfile(p)]
    return [pfad] if os.path.isfile(pfad) else []


def korrigiere_datei(
    pfad: str,
    dry_run: bool = False,
    backup: bool = True,
) -> Tuple[int, int, Dict[Tuple[str, str, str], int]]:
    """Berechnet `reason_code` neu.

    Rueckgabe: (zeilen, geaendert, {(end_grund, alt, neu): anzahl}).
    """
    stat_vorher = os.stat(pfad)
    with open(pfad, "r", encoding="utf-8-sig", newline="") as fh:
        leser = csv.DictReader(fh, delimiter=";")
        kopf = tuple(leser.fieldnames or ())
        zeilen = list(leser)
    if not zeilen:
        return 0, 0, {}
    fehlend = [spalte for spalte in ERWARTETE_SPALTEN if spalte not in kopf]
    if fehlend:
        raise ValueError(
            f"Spalte(n) fehlen: {', '.join(fehlend)} (Header: {';'.join(kopf)})"
        )
    stat_nachher = os.stat(pfad)
    if (stat_vorher.st_size, stat_vorher.st_mtime_ns) != (
        stat_nachher.st_size, stat_nachher.st_mtime_ns
    ):
        raise RuntimeError(
            "Datei wurde waehrend des Lesens veraendert (Dienst schreibt gerade). "
            "Bitte bei stehendem Kompressor oder gestopptem Dienst erneut starten."
        )

    details: Dict[Tuple[str, str, str], int] = {}
    for zeile in zeilen:
        alt = str(zeile.get("reason_code") or "").strip()
        neu = _end_code(zeile.get("end_grund"), alt)
        if neu == alt:
            continue
        schluessel = (str(zeile.get("end_grund") or ""), alt, neu)
        details[schluessel] = details.get(schluessel, 0) + 1
        zeile["reason_code"] = neu
    geaendert = sum(details.values())

    if dry_run or not geaendert:
        return len(zeilen), geaendert, details

    if backup:
        stempel = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(pfad, f"{pfad}.bak-{stempel}")
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    fd, temporaer = tempfile.mkstemp(prefix="zyklen_fix_", suffix=".tmp", dir=verzeichnis)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            schreiber = csv.DictWriter(
                fh, fieldnames=list(kopf), delimiter=";", lineterminator="\r\n"
            )
            schreiber.writeheader()
            for zeile in zeilen:
                schreiber.writerow({name: zeile.get(name, "") for name in kopf})
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporaer, pfad)
    finally:
        if os.path.exists(temporaer):
            os.remove(temporaer)
    return len(zeilen), geaendert, details


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Altbestand der Zyklus-CSV: reason_code richtigstellen"
    )
    parser.add_argument("--pfad", required=True,
                        help="zyklen.csv oder Verzeichnis mit zyklen*.csv")
    parser.add_argument("--dry-run", action="store_true",
                        help="nur anzeigen, nichts schreiben")
    parser.add_argument("--kein-backup", action="store_true",
                        help="keine .bak-Datei anlegen (nicht empfohlen)")
    args = parser.parse_args(argv)

    dateien = _dateien(args.pfad)
    if not dateien:
        print(f"Keine zyklen*.csv gefunden: {args.pfad}", file=sys.stderr)
        return 1

    rc = 0
    for pfad in dateien:
        try:
            zeilen, geaendert, details = korrigiere_datei(
                pfad, dry_run=args.dry_run, backup=not args.kein_backup
            )
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"FEHLER {pfad}: {exc}", file=sys.stderr)
            rc = 1
            continue
        wort = "wuerde korrigieren" if args.dry_run else "korrigiert"
        print(f"{pfad}: {zeilen} Zeilen, {geaendert} {wort}")
        for (end_grund, alt, neu), anzahl in sorted(
            details.items(), key=lambda eintrag: -eintrag[1]
        ):
            print(f"  {end_grund[:26]:26s} {alt:18s} -> {neu:18s} {anzahl:4d}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
