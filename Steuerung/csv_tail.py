# -*- coding: utf-8 -*-
"""RAM-schonender Zugriff auf das ENDE einer CSV-Datei.

BUGFIX (23.10.2026): Ausloeser war der OOM-Kill am 23.10.2026
(``Out of memory: Killed process ... (python) anon-rss:178428kB`` bei
416 MB Gesamt-RAM). Zwei Aufrufer lasen die heizungsdaten.csv bisher
komplett ein und hielten die letzten N Zeilen zusaetzlich als Liste mit
ALLEN Spalten:

  * ``api.get_history(hours=168)`` - 100.000 Tailzeilen:
    gemessen 113 MB Spitze (tracemalloc, 13,5-MB-CSV mit 121.000 Zeilen).
  * ``api._berechne_hist_wh_qm()`` - 25.000 Tailzeilen: 27 MB Spitze.

Der Speicherbedarf kam nicht von der Datei, sondern von den
materialisierten Zeilen: 100.000 Zeilen x 21 Spalten sind rund 2 Mio
String-Objekte. Genau das muss nicht im RAM liegen.

Dieses Modul loest das in zwei Schritten:

1. ``offset_der_letzten_zeilen()`` bestimmt durch Rueckwaertslesen in
   256-kB-Bloecken, wo die letzten N Zeilen beginnen. Die vorderen
   Dateiteile werden nie gelesen.
2. ``streame_tail()`` liest ab diesem Offset und reicht jede Zeile
   spaltenweise reduziert an einen Verarbeiter weiter. Der Aufrufer
   entscheidet, was er behaelt - beim Summieren sind das 0 Bytes, beim
   Downsampling wenige tausend Zeilen statt hunderttausend.

Fachlich aendert sich nichts: es werden dieselben Zeilen in derselben
Reihenfolge geliefert, ungueltige Zeilen (Spaltenzahl passt nicht) und
Zeilen mit Null-Bytes werden wie bisher gezaehlt und verworfen.
"""
import csv
import io
import os
from typing import Callable, Dict, List, Optional, Sequence, Tuple

#: Blockgroesse fuer das Rueckwaertslesen. 256 kB entsprechen auf dem Pi
#: etwa 2000 Zeilen - genug, um die gesuchte Grenze meist in einem Schritt
#: zu finden, klein genug, um den RAM nicht zu belasten.
BLOCK_BYTES = 256 * 1024


def offset_der_letzten_zeilen(roh, max_rows: int) -> int:
    """Byte-Offset, ab dem die letzten ``max_rows`` Datenzeilen beginnen.

    ``roh`` ist eine im Binaermodus geoeffnete Datei. Der Offset zeigt
    immer auf den ANFANG einer Zeile (also direkt hinter einem
    Zeilenumbruch) oder auf den Beginn der Daten (hinter der Kopfzeile,
    wenn die Datei weniger als ``max_rows`` Zeilen hat).

    Die Datei wird nur blockweise von hinten gelesen. Es wird nichts
    dekodiert und keine Zeile geparst - gesucht sind ausschliesslich
    Zeilenumbruch-Positionen.
    """
    roh.seek(0, os.SEEK_END)
    ende = roh.tell()
    roh.seek(0)
    roh.readline()  # Kopfzeile ueberspringen
    daten_start = roh.tell()
    if max_rows <= 0 or ende <= daten_start:
        return daten_start

    # Endet die Datei mit einem Umbruch, gehoert die letzte Zeile zu einem
    # Umbruch mehr: "a\nb\nc\n" hat 3 Zeilen und 3 Umbrueche, "a\nb\nc" hat
    # 3 Zeilen und 2 Umbrueche. Fuer den Start der max_rows-ten Zeile von
    # hinten braucht man daher einen Umbruch mehr bzw. genau max_rows
    # Umbrueche (Herleitung siehe test_offset_trifft_die_letzten_zeilen).
    roh.seek(ende - 1)
    endet_mit_umbruch = roh.read(1) == b"\n"
    benoetigt = max_rows + 1 if endet_mit_umbruch else max_rows

    # Nur der Block, in dem die gesuchte Zeile liegt, wird gehalten. Eine
    # Liste aller Umbruch-Positionen waere bei max_rows=100.000 rund 3 MB
    # plus der komplette Dateipuffer (gemessen 26 MB Spitze) - genau der
    # Speicher, den diese Funktion sparen soll.
    gezaehlt = 0
    position = ende
    while position > daten_start:
        lese = min(BLOCK_BYTES, position - daten_start)
        position -= lese
        roh.seek(position)
        teil = roh.read(lese)
        im_block = teil.count(b"\n")
        if gezaehlt + im_block >= benoetigt:
            # Ziel liegt in diesem Block: den (benoetigt - gezaehlt)-ten
            # Umbruch von HINTEN suchen.
            rest = benoetigt - gezaehlt
            idx = len(teil)
            while rest > 0:
                idx = teil.rfind(b"\n", 0, idx)
                if idx < 0:
                    return daten_start
                rest -= 1
            return position + idx + 1
        gezaehlt += im_block
    return daten_start


def _kopfzeile(csv_path: str) -> List[str]:
    """Kopfzeile als Feldliste (leer, wenn die Datei leer ist)."""
    with open(csv_path, "rb") as roh:
        roh.seek(0)
        kopf_zeile = roh.readline()
    text = kopf_zeile.decode("utf-8", errors="replace")
    if not text.strip():
        return []
    return next(csv.reader([text]), [])


def streame_tail(
    csv_path: str,
    max_rows: int,
    spalten: Optional[Sequence[str]] = None,
    verarbeite: Optional[Callable[[Tuple], None]] = None,
) -> Dict:
    """Liest die letzten ``max_rows`` Datenzeilen und reicht sie weiter.

    ``verarbeite`` bekommt je gueltiger Zeile ein Tupel mit genau den in
    ``spalten`` genannten Feldern (fehlt eine Spalte in der Datei, steht
    dort ``None``). Ohne ``verarbeite`` wird nur gezaehlt - das ist die
    billigste Form der Qualitaetspruefung.

    Rueckgabe (Qualitaet, wie ``api._read_csv_tail`` sie fuehrt)::

        {"invalid_rows": int, "null_bytes": int, "header": list,
         "rows": int}

    ``OSError`` (Datei fehlt/nicht lesbar) wird bewusst NICHT
    verschluckt: die Aufrufer unterscheiden zwischen "Datei fehlt" und
    "Datei leer" schon seit jeher ueber den Dateipfad.
    """
    if not os.path.exists(csv_path):
        return {"invalid_rows": 0, "null_bytes": 0, "header": [], "rows": 0}
    header = _kopfzeile(csv_path)
    qualitaet: Dict = {
        "invalid_rows": 0,
        "null_bytes": 0,
        "header": header,
        "rows": 0,
    }
    if not header:
        return qualitaet

    with open(csv_path, "rb") as roh:
        offset = offset_der_letzten_zeilen(roh, max_rows)

    index = [header.index(name) if name in header else None for name in (spalten or ())]
    with open(csv_path, "rb") as roh:
        roh.seek(offset)
        with io.TextIOWrapper(
            roh, encoding="utf-8", errors="replace", newline=""
        ) as text:
            for fields in csv.reader(text):
                qualitaet["rows"] += 1
                if any("\x00" in feld for feld in fields):
                    qualitaet["null_bytes"] += 1
                    qualitaet["invalid_rows"] += 1
                    continue
                if len(fields) != len(header):
                    qualitaet["invalid_rows"] += 1
                    continue
                if verarbeite is not None:
                    verarbeite(
                        tuple(fields[i] if i is not None else None for i in index)
                    )
    return qualitaet
