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

Dieses Modul kennt den vollstaendigen Bestand und kann ihn als ZIP
buendeln. Es wird sowohl vom Manager (Upload) als auch von der WebApp
(Anzeige) genutzt, damit beide denselben Bestand sehen.

Aufruf als Skript:
    python3 datenbestand.py --art log --pfad /var/log/wps/heizungssteuerung.log
    python3 datenbestand.py --art log --pfad ... --tar-out /tmp/x.zip
"""
from __future__ import annotations

import argparse
import gzip
import os
import re
import sys
import zipfile
from typing import List, Optional

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


def _roh_groesse(datei: str) -> int:
    try:
        return os.path.getsize(datei)
    except OSError:
        return 0


def _menschen_bytes(bytes_wert: int) -> str:
    """Formatiert Bytes lesbar: 0 B, 512 KB, 12,5 MB, 1,3 GB."""
    wert = float(bytes_wert)
    for einheit, faktor in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if wert >= faktor:
            return f"{wert / faktor:.1f} {einheit}"
    return f"{int(wert)} B"


def kompressionsfaktor(dateien: List[str], stichprobe_bytes: int = 256 * 1024) -> float:
    """Schaetzt, wie stark die Daten komprimieren.

    Wird gebraucht, um zu entscheiden, wie viele Rohdaten in ein Archiv
    passen. Ohne diese Schaetzung waere jede Abschaetzung eine
    Ratesache: 1 MB Log komprimiert auf ~100 KB, 1 MB JSON auf ~40 KB,
    aber 1 MB Zufallsdaten gar nicht.

    Gemessen wird an der groessten verfuegbaren Datei - die schaechste
    Datei bestimmt, wie schnell das Archiv wächst.
    """
    kandidaten = [d for d in dateien if _roh_groesse(d) > 0]
    if not kandidaten:
        return 4.0
    groesste = max(kandidaten, key=_roh_groesse)
    try:
        with open(groesste, "rb") as f:
            probe = f.read(stichprobe_bytes)
    except OSError:
        return 4.0
    if len(probe) < 1024:
        return 4.0
    komprimiert = len(gzip.compress(probe, 9))
    if komprimiert <= 0:
        return 4.0
    return max(1.0, len(probe) / komprimiert)


def nach_budget(dateien: List[str], budget: Optional[int]) -> tuple:
    """Beschnittet den Bestand auf ein Rohdaten-Budget.

    Die AELTESTEN Dateien fallen weg - so bleibt derjuengste Zeitraum
    erhalten. Zurueck: (behalten, weggelassen).

    Warum das noetig ist: nicht jeder Datenbestand waechst begrenzt.
    Die Log-Rotation hat ein Byte-Budget, die CSV-Monatsarchive
    (heizungsdaten_YYYY-MM.csv, ~17 MB pro Monat) dagegen nicht - die
    sammeln sich unbegrenzt an. Ohne Beschnitt waere der Upload irgendwann
    zu gross und wuerde abgelehnt - mit Beschnitt laedt er immer, und der
    Nutzer sieht, dass aeltere Daten weggelassen wurden.
    """
    if not budget or budget <= 0:
        return list(dateien), []
    behalten: List[str] = []
    weggelassen: List[str] = []
    summe = 0
    # `dateien` kommt chronologisch (aelteste zuerst). Von hinten her
    # fuellen: so bleiben die juengsten Daten erhalten.
    for datei in reversed(dateien):
        groesse = _roh_groesse(datei)
        if summe + groesse > budget:
            weggelassen.append(datei)
            continue
        behalten.append(datei)
        summe += groesse
    behalten.reverse()
    weggelassen.reverse()
    return behalten, weggelassen


def als_archive(
    art: str,
    pfad: str,
    ziel_verzeichnis: str,
    label: str,
    suchverzeichnisse: List[str] = None,
    max_bytes: Optional[int] = None,
    budget: Optional[int] = None,
) -> dict:
    """Buendelt den Bestand als ZIP-Archive. Gibt eine Zusammenfassung zurueck.

    max_bytes: harte Grenze je Archiv. Waehrend des Baus gemessen - der
        Umbau bricht ab, sobald ein Archiv die Grenze ueberschreitet, und
        macht ein neues auf (geteilter Upload). Ohne diese Ueberwachung
        entstuende erst ein riesiges Temp-Archiv, das man nachher
        wegwerfen muesste.
    budget: Rohdaten-Budget fuer den Bestand. Die aeltesten Dateien
        werden weggelassen, bis die Summe passt.

    Warum ZIP statt tar.gz oder gzip:
      * Die Groesse ist praktisch identisch. Gemessen an 202 MB echter
        Logdaten: ZIP 10,17 MB, tar.gz 10,16 MB, cat|gzip 10,16 MB -
        also unter 0,02 % Differenz. Alle drei benutzen dieselbe
        Kompression (deflate/zlib), es ist nur der Container.
      * cat|gzip waere ein reiner Datenstrom: die Dateigrenzen und damit
        die Dateinamen gingen verloren. Bei 14 Log-Generationen laesst sich
        dann nicht mehr sagen, welcher Tag in welchem Abschnitt steht.
      * ZIP laesst sich auf Windows per Doppelklick entpacken (tar.gz
        braucht dort 7-Zip, unter Windows 11 geht es ueber tar), auf Linux
        mit unzip. Der Empfaenger der Daten soll sie ohne Werkzeug
        aufmachen koennen.

    Im Archiv liegt jede Datei unter "<art>/<Elternordner>/<Dateiname>". So
    bleiben die Namen erkennbar UND gleichnamige Dateien aus verschiedenen
    Ordnern (z. B. zyklen.csv aus mehreren analyse-Laeufen) kollidieren nicht.
    """
    dateien = bestand(art, pfad, suchverzeichnisse)
    if not dateien:
        raise FileNotFoundError(f"Keine Dateien fuer '{art}' unter {pfad!r} gefunden")

    weggelassen: List[str] = []
    if budget:
        dateien, weggelassen = nach_budget(dateien, budget)

    ordner = ART_ORDNER.get(art, art)
    os.makedirs(ziel_verzeichnis, exist_ok=True)

    # Wie viele Rohdaten passen in ein Archiv? Das haengt vom Kompressions-
    # faktor ab, der gemessen wird (Log ~10, JSON ~20, Zufallsdaten ~1).
    # 0,9 als Sicherheitsabschlag: das fertige Archiv soll mit Reserve
    # unter dem Limit bleiben, nicht genau darauf.
    roh_limit = None
    if max_bytes:
        faktor = kompressionsfaktor(dateien)
        roh_limit = int(max_bytes * faktor * 0.9)

    archive: List[str] = []
    eingepackt: List[str] = []
    aktuell = None
    zf = None
    roh_aktuell = 0

    def _archiv_schliessen() -> None:
        nonlocal zf, aktuell, roh_aktuell
        if zf is not None:
            zf.close()
            archive.append(aktuell)
            zf = None
            aktuell = None
            roh_aktuell = 0

    try:
        for voll in dateien:
            groesse = _roh_groesse(voll)
            # Passt die Datei noch in das aktuelle Archiv? Entscheidend ist
            # die ROH-Groesse, denn die bestimmt ueber den gemessenen
            # Kompressionsfaktor die Archivgroesse.
            if roh_limit is not None and zf is not None:
                if roh_aktuell + groesse > roh_limit:
                    _archiv_schliessen()
            if zf is None:
                # Jedes Archiv endet auf .zip - auch die Teile. Sonst laesst
                # sich der Download nicht per Doppelklick entpacken.
                basis = os.path.join(ziel_verzeichnis, f"{label}.zip")
                aktuell = basis if not archive else os.path.join(
                    ziel_verzeichnis, f"{label}-{len(archive) + 1}.zip"
                )
                zf = zipfile.ZipFile(
                    aktuell, "w", zipfile.ZIP_DEFLATED, compresslevel=9
                )
            eltern = os.path.basename(os.path.dirname(os.path.abspath(voll))) or "root"
            eltern = eltern.replace(" ", "_")
            zf.write(voll, arcname=f"{ordner}/{eltern}/{os.path.basename(voll)}")
            eingepackt.append(voll)
            roh_aktuell += groesse
            # Echte Groesse pruefen: schlecht komprimierbare Daten
            # koennten das Limit trotz der Schaetzung reissen.
            if max_bytes and _roh_groesse(aktuell) > max_bytes:
                raise ValueError(
                    f"Archiv ueberschreitet das Limit "
                    f"({_roh_groesse(aktuell)} > {max_bytes} Bytes)"
                )
    finally:
        _archiv_schliessen()

    return {
        "archive": archive,
        "dateien": eingepackt,
        "weggelassen": weggelassen,
        "roh_bytes": sum(_roh_groesse(d) for d in eingepackt),
        "archiv_bytes": sum(_roh_groesse(a) for a in archive),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Vollstaendiger Datenbestand fuer Anzeige und Upload"
    )
    parser.add_argument("--art", required=True,
                        choices=["log", "csv", "jsonl", "lern", "zyklen"])
    parser.add_argument("--pfad", default="", help="Pfad der aktiven Datei")
    parser.add_argument("--suchverzeichnis", action="append", default=[],
                        help="Zusatzverzeichnis (mehrfach moeglich)")
    parser.add_argument("--out-dir", default="",
                        help="Verzeichnis fuer die erzeugten Archive")
    parser.add_argument("--label", default="datenbestand",
                        help="Basisname der Archive")
    parser.add_argument("--max-bytes", type=int, default=0,
                        help="Harte Grenze je Archiv in Bytes (0 = keine)")
    parser.add_argument("--budget", type=int, default=0,
                        help="Rohdaten-Budget in Bytes (0 = unbeschnitten)")
    args = parser.parse_args(argv)

    try:
        if args.out_dir:
            ergebnis = als_archive(
                args.art, args.pfad, args.out_dir, args.label,
                args.suchverzeichnis,
                max_bytes=args.max_bytes or None,
                budget=args.budget or None,
            )
            # stdout: die erzeugten Archive, eine pro Zeile.
            for pfad in ergebnis["archive"]:
                print(pfad)
            # stderr: die Zusammenfassung fuer den Manager.
            info = (
                f"{len(ergebnis['dateien'])} Dateien, "
                f"{_menschen_bytes(ergebnis['roh_bytes'])} roh, "
                f"{_menschen_bytes(ergebnis['archiv_bytes'])} komprimiert"
            )
            if ergebnis["weggelassen"]:
                info += (
                    f" | {len(ergebnis['weggelassen'])} Dateien weggelassen "
                    f"(Budget)"
                )
            if len(ergebnis["archive"]) > 1:
                info += f" | auf {len(ergebnis['archive'])} Archive verteilt"
            print(info, file=sys.stderr)
        else:
            for voll in bestand(args.art, args.pfad, args.suchverzeichnis):
                print(voll)
    except (OSError, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
