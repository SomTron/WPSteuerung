"""Daempfung von Neustart-Schleifen bei nicht behebbaren Startfehlern.

Hintergrund: Die Unit startet den Dienst mit `Restart=always` und
`RestartSec=10` - BEWUSST ohne StartLimit, denn eine Heizungssteuerung
darf nach einem Absturz nicht aufgeben (sonst bleibt die Anlage
unbemerkt aus). Das hat aber eine Kehrseite: startet der Dienst dauerhaft
fehl (fehlende Hardware, kaputte Konfiguration), entstehen 8.640
Neustarts pro Tag. Jeder schreibt einen Stacktrace ins Journal und
belastet die SD-Karte.

Loesung: Der Dienst zaehlt seine eigenen Fehlstarts. Ab dem dritten
Fehlstart in kurzer Zeit wartet er mehrere Minuten, BEVOR er es erneut
versucht - ohne den Prozess zu beenden. systemd sieht damit keinen
Neustart, das Journal bleibt ruhig, und die Heizung versucht es weiter
(denn der Fehler kann ja weggehen, z. B. wenn ein Sensor wieder da ist).

Wichtig: Nur Fehler beim START werden gezaehlt. Ein Absturz im laufenden
Betrieb fuehrt weiterhin zum sofortigen Neustart durch systemd - da ist
ein Neustart genau das Richtige.

Die Historie liegt bewusst auf der SD-Karte (nicht im RAM): sie muss
Fehlstarts ueber Dienstneustarts hinweg zaehlen. Ist die SD-Karte
defekt, kann sie nicht geschrieben werden - dann greift die Daempfung
nicht, aber das Problem ist dann ein groesseres.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import List

# Standardpfad neben last_state.txt (auf der SD-Karte des Pi).
FEHLSTART_DATEI = os.environ.get(
    "WPS_FEHLSTART_DATEI",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "startfehler.json"),
)

# Schwellen. Erreicht der Dienst sie, wird gewartet statt neu gestartet.
MAX_FEHLSTARTS = int(os.environ.get("WPS_MAX_FEHLSTARTS", "3"))
FEHLSTART_FENSTER_MIN = float(os.environ.get("WPS_FEHLSTART_FENSTER_MIN", "10"))
WARTEZEIT_MIN = float(os.environ.get("WPS_FEHLSTART_WARTEZEIT_MIN", "5"))

# So viele Eintraege werden historisch behalten.
MAX_EINTRAEGE = 20


def _lade_historie(pfad: str = None) -> List[str]:
    """Liest die Zeitstempel der letzten Fehlstarts (ISO-Format)."""
    pfad = pfad or FEHLSTART_DATEI
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            daten = json.load(f)
        if isinstance(daten, list):
            return [str(z) for z in daten]
    except (OSError, ValueError):
        pass
    return []


def _schreibe_historie(zeitstempel: List[str], pfad: str = None) -> None:
    pfad = pfad or FEHLSTART_DATEI
    try:
        verzeichnis = os.path.dirname(os.path.abspath(pfad))
        if verzeichnis:
            os.makedirs(verzeichnis, exist_ok=True)
        tmp = f"{pfad}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(zeitstempel[-MAX_EINTRAEGE:], f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, pfad)
    except OSError:
        # Kann die Datei nicht geschrieben werden (z. B. SD-Karte defekt),
        # ist das kein Grund fuer einen Absturz - die Daempfung entfaellt
        # dann eben.
        pass


def fehlstart_verzeichnen(pfad: str = None, jetzt: datetime = None) -> int:
    """Traegt den aktuellen Fehlstart ein. Gibt die Zahl im Fenster zurueck.

    Im Fenster (FEHLSTART_FENSTER_MIN) zaehlen nur die Fehlstarts, die
    wirklich neu sind - jeder Prozess zaehlt sich also genau einmal.
    """
    if jetzt is None:
        jetzt = datetime.now()
    pfad = pfad or FEHLSTART_DATEI
    grenze = jetzt - timedelta(minutes=FEHLSTART_FENSTER_MIN)
    eintraege: List[str] = []
    for wert in _lade_historie(pfad):
        try:
            ts = datetime.fromisoformat(wert)
        except ValueError:
            continue
        if ts >= grenze:
            eintraege.append(wert)
    eintraege.append(jetzt.isoformat(timespec="seconds"))
    _schreibe_historie(eintraege, pfad)
    return len(eintraege)


def fehlstarts_im_fenster(pfad: str = None, jetzt: datetime = None) -> int:
    """Wie viele Fehlstarts liegen im Fenster (ohne einen neuen zu zaehlen)."""
    if jetzt is None:
        jetzt = datetime.now()
    grenze = jetzt - timedelta(minutes=FEHLSTART_FENSTER_MIN)
    anzahl = 0
    for wert in _lade_historie(pfad or FEHLSTART_DATEI):
        try:
            ts = datetime.fromisoformat(wert)
        except ValueError:
            continue
        if ts >= grenze:
            anzahl += 1
    return anzahl


def fehlstart_zuruecksetzen(pfad: str = None) -> None:
    """Wird nach einem erfolgreichen Start aufgerufen: die Schleife ist vorbei."""
    pfad = pfad or FEHLSTART_DATEI
    try:
        if os.path.exists(pfad):
            os.remove(pfad)
    except OSError:
        pass


def warte_bei_fehlerschleife(
    pfad: str = None,
    logger=None,
    jetzt: datetime = None,
    max_start: int = None,
    wartezeit_min: float = None,
    schlaf=time.sleep,
) -> float:
    """Zaehlt den Fehlstart und wartet bei einer Schleife. Gibt die Wartezeit zurueck.

    Der Dienst beendet sich dabei NICHT: systemd startet ihn nicht neu, das
    Journal bleibt ruhig, und der naechste Versuch erfolgt in-process.
    """
    if max_start is None:
        max_start = MAX_FEHLSTARTS
    if wartezeit_min is None:
        wartezeit_min = WARTEZEIT_MIN
    anzahl = fehlstart_verzeichnen(pfad, jetzt=jetzt)
    if anzahl < max_start:
        return 0.0
    sekunden = wartezeit_min * 60
    nachricht = (
        f"{anzahl} Fehlstarts in {FEHLSTART_FENSTER_MIN:.0f} min - "
        f"warte {wartezeit_min:.0f} min, bevor der naechste Versuch startet "
        f"(kein Neustart durch systemd)."
    )
    if logger is not None:
        logger.warning(nachricht)
    else:
        import logging
        logging.warning(nachricht)
    schlaf(sekunden)
    return sekunden
