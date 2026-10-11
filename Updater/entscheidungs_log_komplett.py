#!/usr/bin/env python3
"""Liest den gesamten Entscheidungslog-Bestand EINMALIG aus.

Der laufende Dienst (``Steuerung/api.py`` -> ``entscheidungs_log.historie``)
liest nur die neuesten ``MAX_EINTRAEGE_LESEN`` Zeilen und deckelt die Anzeige
zeitlich (Manager-Option 15 = 8 Tage). Fuer eine einmalige Vollauswertung oder
Archivierung fuehrt dieses Skript ALLE Dateien des Bestands zusammen:

    entscheidungs_log.jsonl              (aktuelle Datei)
    entscheidungs_log.jsonl.old.1 ... .old.7   (Rotations-Generationen)
    entscheidungs_log.jsonl.old          (Alt-Datei aus dem frueheren
                                          Ein-Generationen-Schema)

Letztere kennt der laufende Dienst gar nicht - sie wuerde sonst nie wieder
sichtbar. Aufruf:

    python3 Updater/entscheidungs_log_komplett.py            # Standardpfad
    python3 Updater/entscheidungs_log_komplett.py /pfad/logdir
    python3 Updater/entscheidungs_log_komplett.py /pfad -o - # nur stdout

Ergebnis: eine nach Zeitstempel sortierte Datei
``entscheidungs_log_komplett.jsonl`` im Log-Verzeichnis (per ``-o`` aenderbar)
plus eine Zusammenfassung (Zeilen je Datei/Tag, Zeitraum).

Weiterverarbeitung der Ausgabe:
    python3 Analyse/log_analyse.py --decisions <ZIEL>
    python3 Analyse/diag_auswertung.py <Verzeichnis mit entscheidungs_log.jsonl>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter

BASISNAME = "entscheidungs_log.jsonl"
STANDARD_DIRS = (
    "/home/patrik/WPSteuerung/Steuerung",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Steuerung"),
)


def finde_logdateien(verzeichnis: str) -> list[str]:
    """Alle Generationen des Entscheidungslogs in einem Verzeichnis.

    Reihenfolge ist fuer das Ergebnis unerheblich (es wird nach Zeitstempel
    sortiert); die aktuelle Datei steht zur Lesbarkeit am Ende.
    """
    muster = os.path.join(verzeichnis, BASISNAME + "*")
    treffer = [p for p in glob.glob(muster)
               if os.path.isfile(p) and not p.endswith(".tmp")]
    treffer.sort(key=lambda p: (p.endswith(".jsonl"), p))
    return treffer


def standard_verzeichnis() -> "str | None":
    """Erstes Standard-Verzeichnis, das Logdateien enthaelt (sonst None)."""
    for kandidat in STANDARD_DIRS:
        pfad = os.path.abspath(kandidat)
        if os.path.isdir(pfad) and glob.glob(os.path.join(pfad, BASISNAME + "*")):
            return pfad
    return None


def _sortierschluessel(eintrag: dict) -> str:
    """Zeitstempel-Praefix (YYYY-MM-DDTHH:MM:SS).

    Bewusst nur der Sekundenpraefix: so werden auch alte NAIVE Zeitstempel
    (``2026-09-18T14:45:19``) und neue mit Zeitzone (``...+02:00``) einheitlich
    und chronologisch sortiert, ohne Zeitzonen-Arithmetik.
    """
    return str(eintrag.get("ts") or "")[:19]


def lies_alles(verzeichnis: str):
    """Liest alle Zeilen des Bestands, entdoppelt exakte Wiederholungen.

    Rueckgabe: (dateien, eintraege_nach_zeit, zeilen_je_datei, unlesbare).
    """
    dateien = finde_logdateien(verzeichnis)
    eintraege: list[dict] = []
    gesehen: set[str] = set()
    je_datei: Counter = Counter()
    unlesbar = 0
    for pfad in dateien:
        anzahl = 0
        try:
            with open(pfad, encoding="utf-8") as fh:
                for roh in fh:
                    zeile = roh.strip()
                    if not zeile or zeile in gesehen:
                        continue
                    try:
                        eintrag = json.loads(zeile)
                    except json.JSONDecodeError:
                        unlesbar += 1
                        continue
                    gesehen.add(zeile)
                    eintraege.append(eintrag)
                    anzahl += 1
        except OSError as exc:
            print(f"Warnung: {pfad} nicht lesbar ({exc})")
            continue
        je_datei[os.path.basename(pfad)] = anzahl
    eintraege.sort(key=_sortierschluessel)
    return dateien, eintraege, je_datei, unlesbar


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Entscheidungslog einmalig vollstaendig auslesen")
    parser.add_argument("verzeichnis", nargs="?", default=None,
                        help="Verzeichnis mit entscheidungs_log.jsonl* "
                             f"(Standard: {STANDARD_DIRS[0]})")
    parser.add_argument("-o", "--out", default=None,
                        help="Zieldatei (Standard: <Verzeichnis>/"
                             "entscheidungs_log_komplett.jsonl; '-' = stdout)")
    args = parser.parse_args(argv)

    # Bei Ausgabe nach stdout ("-o -") wandert die Zusammenfassung nach stderr,
    # damit stdout ausschliesslich JSON-Lines enthaelt (pipe-freundlich).
    protokoll = sys.stderr if args.out == "-" else sys.stdout

    verzeichnis = args.verzeichnis or standard_verzeichnis()
    if not verzeichnis or not os.path.isdir(verzeichnis):
        print("FEHLER: kein Log-Verzeichnis gefunden - bitte als Argument angeben.",
              file=protokoll)
        return 2

    dateien, eintraege, je_datei, unlesbar = lies_alles(verzeichnis)
    if not dateien:
        print(f"Keine {BASISNAME}* in {verzeichnis}", file=protokoll)
        return 1

    print(f"Verzeichnis: {verzeichnis}", file=protokoll)
    print(f"Dateien ({len(dateien)}):", file=protokoll)
    for name, anzahl in je_datei.items():
        print(f"  {name:<34} {anzahl:>7} Zeilen", file=protokoll)
    zusatz = f"  ({unlesbar} unlesbare Zeilen uebersprungen)" if unlesbar else ""
    print(f"Eintraege gesamt: {len(eintraege)}{zusatz}", file=protokoll)
    if eintraege:
        print(f"Zeitraum: {eintraege[0].get('ts')}  bis  {eintraege[-1].get('ts')}",
              file=protokoll)
        je_tag = Counter(_sortierschluessel(e)[:10] for e in eintraege)
        print(f"Tage: {len(je_tag)}", file=protokoll)
        for tag in sorted(je_tag):
            print(f"  {tag}  {je_tag[tag]:>6}", file=protokoll)

    if args.out == "-":
        for eintrag in eintraege:
            sys.stdout.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
        return 0

    ziel = args.out or os.path.join(verzeichnis, "entscheidungs_log_komplett.jsonl")
    with open(ziel, "w", encoding="utf-8") as fh:
        for eintrag in eintraege:
            fh.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    print(f"\nGeschrieben: {ziel} ({len(eintraege)} Zeilen)", file=protokoll)
    return 0


if __name__ == "__main__":
    sys.exit(main())
