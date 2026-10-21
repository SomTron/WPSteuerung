"""Vollstaendiger Datenbestand je Datenart - fuer Anzeige und Upload.

Hintergrund: Der WP-Manager laedt bisher genau EINE Datei hoch (die aktive).
Bei rotierenden Daten geht dabei die Historie verloren:

  * heizungssteuerung.log  - nach der Tagesrotation enthaelt die aktive
    Datei nur noch den laufenden Tag; die Vortage liegen in
    heizungssteuerung.log.2026-10-19, ...
  * heizungsdaten.csv      - die Monatsarchive heizungsdaten_2026-09.csv
    liegen daneben
  * entscheidungs_log.jsonl - die Generationen .old.1 ... .old.7 daneben
  * learning_data.json     - Sicherungskopien daneben

Dieses Modul kennt den vollstaendigen Bestand und kann ihn als tar.gz
buendeln. Es wird sowohl vom Manager (Upload) als auch von der WebApp
(Anzeige) genutzt, damit beide denselben Bestand sehen.

Aufruf als Skript:
    python3 datenbestand.py --art log --pfad /var/log/wps/heizungssteuerung.log
    python3 datenbestand.py --art log --pfad ... --tar-out /tmp/x.tar.gz
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import tarfile
from typing import List

from log_files import generationen

# Monatsarchive der Heizungsdaten: heizungsdaten_2026-09.csv
_RE_MONATSARCHIV = re.compile(
    r"^(?P<basis>.+?)(?:_(?P<jahr>\d{4})-(?P<monat>\d{2}))?\.csv$"
)
# Generationen des Entscheidungslogs: entscheidungs_log.jsonl.old.1
_RE_ENTSCHEIDUNGSGEN = re.compile(r"^(?P<basis>.+?)\.old\.(?P<nr>\d+)$")
# Lern-Backups: learning_data.json.korrupt-20261020_101500
_RE_LERNBACKUP = re.compile(r"^(?P<basis>.+)\.(?:korrupt|bak|backup)")


def bestand_log(pfad: str) -> List[str]:
    """Alle Generationen eines Logs: aktive Datei + datierte + numerische.

    Reihenfolge: aelteste zuerst. Genau dieselbe Sicht, die der Leser
    (log_query) verwendet - Anzeige und Upload sehen denselben Bestand.
    """
    if not pfad:
        return []
    return [d for d in generationen(pfad) if os.path.isfile(d)]


def bestand_csv(pfad: str) -> List[str]:
    """Aktuelle Heizungsdaten-CSV + alle Monatsarchive im selben Ordner.

    Beispiel: heizungsdaten.csv, heizungsdaten_2026-09.csv,
    heizungsdaten_2026-08.csv
    """
    if not pfad:
        return []
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    basis_name = os.path.basename(pfad)
    treffer = _RE_MONATSARCHIV.match(basis_name)
    if not treffer:
        return [pfad] if os.path.isfile(pfad) else []
    basis = treffer.group("basis")

    dateien: List[str] = []
    if not os.path.isdir(verzeichnis):
        return [pfad] if os.path.isfile(pfad) else []
    for datei in sorted(os.listdir(verzeichnis)):
        if datei == basis_name:
            continue
        if not datei.startswith(basis + "_") or not datei.endswith(".csv"):
            continue
        voll = os.path.join(verzeichnis, datei)
        if os.path.isfile(voll):
            dateien.append(voll)
    # Die aktive Datei kommt zuletzt (sie ist die juengste).
    if os.path.isfile(pfad):
        dateien.append(os.path.abspath(pfad))
    return dateien


def bestand_jsonl(pfad: str) -> List[str]:
    """Entscheidungslog + alle .old.N-Generationen, aelteste zuerst."""
    if not pfad:
        return []
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    basis_name = os.path.basename(pfad)
    if not os.path.isdir(verzeichnis):
        return [pfad] if os.path.isfile(pfad) else []

    gen_liste: List[tuple] = []
    for datei in os.listdir(verzeichnis):
        if not datei.startswith(basis_name + ".old."):
            continue
        treffer = _RE_ENTSCHEIDUNGSGEN.match(datei)
        if not treffer or treffer.group("basis") != basis_name:
            continue
        voll = os.path.join(verzeichnis, datei)
        try:
            gen_liste.append((int(treffer.group("nr")), voll))
        except ValueError:
            continue
    # Groessere Nummer = aelter (Log-Logik: .old.1 ist die juengste).
    gen_liste.sort(key=lambda e: -e[0])
    dateien = [voll for _, voll in gen_liste if os.path.isfile(voll)]
    if os.path.isfile(pfad):
        dateien.append(os.path.abspath(pfad))
    return dateien


def bestand_lerndaten(pfad: str) -> List[str]:
    """Lerndaten + Sicherungskopien (korrupt-/bak-Sicherungen) daneben."""
    if not pfad:
        return []
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    basis_name = os.path.basename(pfad)
    dateien: List[str] = []
    if os.path.isdir(verzeichnis):
        for datei in sorted(os.listdir(verzeichnis)):
            if datei == basis_name:
                continue
            if not datei.startswith(basis_name + "."):
                continue
            if not _RE_LERNBACKUP.match(datei):
                continue
            voll = os.path.join(verzeichnis, datei)
            if os.path.isfile(voll):
                dateien.append(voll)
    if os.path.isfile(pfad):
        dateien.append(os.path.abspath(pfad))
    return dateien


def bestand_zyklen(pfad: str, suchverzeichnisse: List[str] = None) -> List[str]:
    """zyklen.csv der laufenden Analyse + die aus den Analyse-Laeufen.

    `pfad` ist der bevorzugte Pfad (leer, wenn keiner existiert).
    `suchverzeichnisse` sind Verzeichnisse, in denen nach weiteren
    zyklen.csv gesucht wird (z. B. logs/analyse_*/).
    """
    dateien: List[str] = []
    if pfad and os.path.isfile(pfad):
        dateien.append(os.path.abspath(pfad))
    for basis_verz in suchverzeichnisse or []:
        if not basis_verz or not os.path.isdir(basis_verz):
            continue
        for wurzel, _dirs, dateinamen in os.walk(basis_verz):
            for datei in dateinamen:
                if datei == "zyklen.csv":
                    voll = os.path.join(wurzel, datei)
                    if voll not in dateien:
                        dateien.append(voll)
    return dateien


def bestand(art: str, pfad: str = "", suchverzeichnisse: List[str] = None) -> List[str]:
    """Dispatch: liefert den Bestand fuer die angegebene Datenart."""
    if art == "log":
        return bestand_log(pfad)
    if art == "csv":
        return bestand_csv(pfad)
    if art == "jsonl":
        return bestand_jsonl(pfad)
    if art == "lern":
        return bestand_lerndaten(pfad)
    if art == "zyklen":
        return bestand_zyklen(pfad, suchverzeichnisse)
    raise ValueError(f"Unbekannte Datenart: {art}")


# Ordnername im Archiv je Datenart. Derart kommen Dateien verschiedener
# Datenarten nicht durcheinander, und gleichnamige Dateien aus
# verschiedenen Ordnern (zyklen.csv aus mehreren analyse-Laeufen) behalten
# ihre Herkunft.
ART_ORDNER = {
    "log": "log",
    "csv": "csv",
    "jsonl": "entscheidung",
    "lern": "lern",
    "zyklen": "zyklen",
}


def als_archiv(
    art: str, pfad: str, ziel: str, suchverzeichnisse: List[str] = None
) -> List[str]:
    """Buendelt den Bestand als tar.gz nach `ziel`. Gibt die Dateiliste zurueck.

    Im Archiv liegt jede Datei unter "<art>/<Elternordner>/<Dateiname>". So
    bleiben die Namen erkennbar UND gleichnamige Dateien aus verschiedenen
    Ordnern (z. B. zyklen.csv aus mehreren analyse-Laeufen) kollidieren nicht.
    """
    dateien = bestand(art, pfad, suchverzeichnisse)
    if not dateien:
        raise FileNotFoundError(f"Keine Dateien fuer '{art}' unter {pfad!r} gefunden")
    ordner = ART_ORDNER.get(art, art)
    with tarfile.open(ziel, "w:gz") as tar:
        for voll in dateien:
            eltern = os.path.basename(os.path.dirname(os.path.abspath(voll))) or "root"
            eltern = eltern.replace(" ", "_")
            tar.add(voll, arcname=f"{ordner}/{eltern}/{os.path.basename(voll)}")
    return dateien


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Vollstaendiger Datenbestand fuer Anzeige und Upload"
    )
    parser.add_argument("--art", required=True,
                        choices=["log", "csv", "jsonl", "lern", "zyklen"])
    parser.add_argument("--pfad", default="", help="Pfad der aktiven Datei")
    parser.add_argument("--suchverzeichnis", action="append", default=[],
                        help="Zusatzverzeichnis (mehrfach moeglich)")
    parser.add_argument("--tar-out", default="",
                        help="Schreibt den Bestand als tar.gz in diese Datei")
    args = parser.parse_args(argv)

    try:
        if args.tar_out:
            dateien = als_archiv(args.art, args.pfad, args.tar_out,
                                 args.suchverzeichnis)
            # Auf stdout nur die Kurzinfo, damit der Manager sie auswerten kann.
            print(f"{len(dateien)} Dateien")
        else:
            for voll in bestand(args.art, args.pfad, args.suchverzeichnis):
                print(voll)
    except (OSError, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
