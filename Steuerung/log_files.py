"""Gemeinsamer Zugriff auf die rotierten Log-Dateien.

Hintergrund: Das Betriebslog rotiert, weil das tmpfs auf dem Pi begrenzt
ist (log2ram, SIZE=128M). Wer NUR die aktive Datei liest, sieht nach einer
Rotation nur noch den kleinen neuen Rest und glaubt, die Historie sei
verschwunden - dabei liegt sie unangetastet in den Generationsdateien.

Deshalb liegt hier die eine Logik, die bisher an drei Stellen fehlte:
`log_query.py` (Manager-Optionen 11/12), `api.py` (Fehlerhistorie der
WebApp) und `logging_config.py` (der Schreiber) brauchen dieselbe Sicht auf
dieselben Dateien.

Unterstuetzt werden zwei Benennungsschemata:

  * datiert  : ``heizungssteuerung.log.2026-10-20``  (Tages-Rotation)
  * numerisch: ``heizungssteuerung.log.1``          (RotatingFileHandler,
    Altbestand aus der Zeit vor der Umstellung)

Die Reihenfolge der Generationen ergibt sich aus den Zeitstempeln in den
Dateinamen (bzw. mtime bei den numerischen) - nicht aus der Dateisortierung.
Damit stimmt die Reihenfolge auch dann, wenn auf dem Pi beide Schemata
gleichzeitig vorliegen (Altbestand + neue Tagesdateien).
"""
from __future__ import annotations

import gzip
import os
import re
from datetime import date, datetime, timedelta
from typing import List, Tuple

# Numerische Generation: X.1, X.2, ... (RotatingFileHandler, Altbestand)
_RE_NUMERISCH = re.compile(r"^(?P<basis>.+)\.(?P<nr>\d+)$")
# Datierte Generation: X.2026-10-20  und  X.2026-10-20.3 (zweiter Teil
# desselben Tages, wenn die Groessengrenze mitten im Tag gerissen wird)
_RE_DATIERT = re.compile(
    r"^(?P<basis>.+)\.(?P<tag>\d{4}-\d{2}-\d{2})(?:\.(?P<teil>\d+))?$"
)


def ist_gz(pfad: str) -> bool:
    return pfad.endswith(".gz")


def _zeilen_lesen(pfad: str):
    """Liest eine (optional gzip-komprimierte) Logdatei zeilenweise."""
    if ist_gz(pfad):
        with gzip.open(pfad, "rt", encoding="utf-8", errors="replace") as f:
            for zeile in f:
                yield zeile
    else:
        with open(pfad, "r", encoding="utf-8", errors="replace") as f:
            for zeile in f:
                yield zeile


def _mtime(pfad: str) -> float:
    try:
        return os.path.getmtime(pfad)
    except OSError:
        return 0.0


def _groesse(pfad: str) -> int:
    try:
        return os.path.getsize(pfad)
    except OSError:
        return 0


def generationen(pfad: str) -> List[str]:
    """Alle Dateien dieser Log-Rotation, aelteste zuerst, aktive Datei zuletzt.

    Enthaelt sowohl die datierten Generationen (X.2026-10-20) als auch die
    numerischen (X.1) und natuerlich die aktive Datei selbst.
    """
    if not pfad:
        return []
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    basis = os.path.basename(pfad)
    if not verzeichnis or not os.path.isdir(verzeichnis):
        return [pfad] if os.path.exists(pfad) else []

    kandidaten: List[Tuple[datetime, float, int, str]] = []

    def _aufnehmen(datei: str, sortier_datum: datetime, aktiv: int = 0) -> None:
        voll = os.path.join(verzeichnis, datei)
        kandidaten.append((sortier_datum, _mtime(voll), aktiv, voll))

    for datei in os.listdir(verzeichnis):
        if datei == basis or not datei.startswith(basis + "."):
            continue
        rest = datei[len(basis) + 1:]
        treffer = _RE_DATIERT.match(basis + "." + rest)
        if treffer and treffer.group("basis") == basis:
            try:
                tag = datetime.strptime(treffer.group("tag"), "%Y-%m-%d")
            except ValueError:
                continue
            _aufnehmen(datei, tag)
            continue
        treffer = _RE_NUMERISCH.match(basis + "." + rest)
        if treffer and treffer.group("basis") == basis:
            # Altbestand: das mtime entspricht dem Rotationszeitpunkt.
            _aufnehmen(
                datei, datetime.fromtimestamp(_mtime(os.path.join(verzeichnis, datei)))
            )

    # Die aktive Datei ist per Definition die juengste. Sie bekommt ein
    # eigenes Sortiermerkmal (aktiv=1), damit sie auch dann hinten steht,
    # wenn das Dateisystem nur sekundengenau auflöst und ein generations-
    # Datei denselben Zeitstempel hat. Ohne das koennte die Reihenfolge
    # kippen und der Leser zeigte die Historie hinter der aktuellen Datei.
    if os.path.exists(pfad):
        _aufnehmen(basis, datetime.fromtimestamp(_mtime(pfad)), aktiv=1)

    kandidaten.sort(key=lambda e: (e[2], e[0], e[1]))
    return [p for _, _, _, p in kandidaten]


def tail_zeilen(pfad: str, anzahl: int) -> List[str]:
    """Die letzten `anzahl` Zeilen EINER Datei (ohne Zeilenumbruch am Ende)."""
    if anzahl <= 0 or not os.path.exists(pfad):
        return []
    if ist_gz(pfad):
        zeilen = [z.rstrip("\n") for z in _zeilen_lesen(pfad)]
        return zeilen[-anzahl:]

    # Von hinten blockweise lesen - ein 25-MB-Log darf dafuer nicht
    # komplett in den RAM.
    ergebnis: List[str] = []
    with open(pfad, "rb") as f:
        try:
            f.seek(0, 2)
            datei_groesse = f.tell()
            block_groesse = 4096
            puffer = b""
            while len(ergebnis) < anzahl and datei_groesse > 0:
                lese_groesse = min(block_groesse, datei_groesse)
                datei_groesse -= lese_groesse
                f.seek(datei_groesse)
                puffer = f.read(lese_groesse) + puffer
                zeilen_im_puffer = puffer.split(b"\n")
                # Der abschliessende Zeilenumbruch erzeugt ein leeres
                # Element am Ende. Es muss VOR dem Abschneiden entfernt
                # werden - sonst nimmt `[-lines:]` einen Platzhalter mit und
                # es kommt konsequent eine Zeile zu wenig zurueck.
                if zeilen_im_puffer and zeilen_im_puffer[-1] == b"":
                    zeilen_im_puffer = zeilen_im_puffer[:-1]
                if len(zeilen_im_puffer) >= anzahl:
                    ergebnis = zeilen_im_puffer[-anzahl:]
                    break
                ergebnis = zeilen_im_puffer
            if ergebnis and ergebnis[-1] == b"":
                ergebnis = ergebnis[:-1]
        except OSError:
            return []
    return [z.decode("utf-8", errors="replace") for z in ergebnis]


def tail_ueber_alle(pfad: str, anzahl: int) -> List[str]:
    """Die letzten `anzahl` Zeilen des gesamten Logs, chronologisch.

    Die Generationen werden von der juengsten zur aeltesten durchsucht;
    das Ergebnis wird in zeitlicher Reihenfolge zurueckgegeben. Genau das
    fehlte bisher: nach einer Rotation sah der Leser nur die aktive
    (winzige) Datei und die aeltere Historie war unsichtbar.
    """
    if anzahl <= 0:
        return []
    ergebnis: List[str] = []
    for datei in reversed(generationen(pfad)):
        if len(ergebnis) >= anzahl:
            break
        teile = tail_zeilen(datei, anzahl - len(ergebnis))
        ergebnis = teile + ergebnis
    return ergebnis[-anzahl:] if len(ergebnis) > anzahl else ergebnis


def tail_bytes_ueber_alle(pfad: str, max_bytes: int) -> str:
    """Die letzten `max_bytes` des gesamten Logs als Text, chronologisch.

    Aelteste Generation zuerst, damit der Text die Zeitachse einhaelt.
    """
    if max_bytes <= 0:
        return ""
    teile: List[str] = []
    rest = max_bytes
    for datei in reversed(generationen(pfad)):
        if rest <= 0:
            break
        if not os.path.exists(datei):
            continue
        groesse = _groesse(datei)
        try:
            with open(datei, "rb") as f:
                if groesse > rest:
                    f.seek(groesse - rest)
                    f.readline()  # angebrochene erste Zeile verwerfen
                daten = f.read()
        except OSError:
            continue
        rest -= len(daten)
        teile.append(daten.decode("utf-8", errors="replace"))
    return "".join(reversed(teile))


def archiv_name(pfad: str, tag: date, teil: int = 0) -> str:
    """Name einer datierten Generationsdatei fuer den Tag `tag`.

    `teil` > 1, wenn an einem Tag die Groessengrenze gerissen wurde und
    der Tag dadurch in mehrere Dateien zerfaellt.
    """
    basis = os.path.basename(pfad)
    name = f"{basis}.{tag.isoformat()}"
    if teil > 1:
        name = f"{name}.{teil}"
    return os.path.join(os.path.dirname(os.path.abspath(pfad)), name)


def verwalte_archiv(
    pfad: str, keep_tage: int, max_gesamt_bytes: int
) -> List[str]:
    """Loescht zu alte und zu viele Generationsdateien. Gibt geloeschte zurueck.

    Zwei Grenzen gleichzeitig:
      * `keep_tage`  - wie weit die Historie zeitlich zurueckreicht
      * `max_gesamt_bytes` - wie viel die Rotation im RAM belegen darf

    Die zweite Grenze ist auf dem Pi entscheidend: /var/log/wps liegt im
    log2ram-tmpfs (SIZE=128M). Ohne Byte-Budget koennte eine Serie
    "voller" Tage das tmpfs sprengen.
    """
    geloescht: List[str] = []
    if not pfad:
        return geloescht
    verzeichnis = os.path.dirname(os.path.abspath(pfad))
    basis = os.path.basename(pfad)
    if not os.path.isdir(verzeichnis):
        return geloescht

    eintraege: List[Tuple[datetime, str, int]] = []
    for datei in os.listdir(verzeichnis):
        if datei == basis or not datei.startswith(basis + "."):
            continue
        voll = os.path.join(verzeichnis, datei)
        treffer = _RE_DATIERT.match(datei)
        if treffer and treffer.group("basis") == basis:
            try:
                sortier_datum = datetime.strptime(treffer.group("tag"), "%Y-%m-%d")
            except ValueError:
                continue
        else:
            treffer = _RE_NUMERISCH.match(datei)
            if not (treffer and treffer.group("basis") == basis):
                continue
            # Altbestand: Alter ueber mtime bestimmen.
            sortier_datum = datetime.fromtimestamp(_mtime(voll))
        eintraege.append((sortier_datum, voll, _groesse(voll)))

    # 1) Zeitgrenze
    grenze = datetime.combine(
        date.today() - timedelta(days=max(0, keep_tage)), datetime.min.time()
    )
    for sortier_datum, voll, _ in eintraege:
        if sortier_datum < grenze:
            try:
                os.remove(voll)
                geloescht.append(voll)
            except OSError:
                pass

    # 2) Byte-Budget: die aeltesten zuerst opfern.
    rest = [(d, v, g) for d, v, g in eintraege if v not in geloescht]
    gesamt = sum(g for _, _, g in rest)
    rest.sort(key=lambda e: (e[0], e[1]))
    while gesamt > max_gesamt_bytes and rest:
        _, voll, groesse = rest.pop(0)
        try:
            os.remove(voll)
            geloescht.append(voll)
            gesamt -= groesse
        except OSError:
            pass

    return geloescht
