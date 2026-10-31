"""Persistenter Abschluss der Legionellenprophylaxe.

Der Lifecycle merkt sich im RAM, wann die Prophylaxe zuletzt erfolgreich
abgeschlossen wurde (``legionellen_last_done``) und in welcher Kalenderwoche
das war. Genau daran haengt das Wochen-Gate in ``evaluate_legionellen``
("Bereits in KW 44 durchgefuehrt").

BEFUND 31.10.2026: Nach einem Service-Neustart war dieser Wert weg. Die
Regel hielt die Prophylaxe sofort wieder fuer faellig und heizte den Speicher
am Folgetag erneut auf 60 C:

  * 30.10.2026, 14:47 - Ziel 60 C unten erreicht, 10 min Halteprobe,
    Abschluss (im Log: ``legionellen_aktiv=false``,
    ``legionellen_unvollstaendig=false``).
  * 31.10.2026, 01:49 - Neustart der Steuerung (``rev`` wechselt von
    ``e9e60d1`` auf ``67adf23``).
  * 31.10.2026, 11:00 - "Legionellenprophylaxe faellig: Starte mit PV auf
    60C" - dieselbe Prozedur ein zweites Mal in derselben Kalenderwoche.

Darum wird der Abschluss - wie der Tagesplan in ``legionellen_plan`` -
atomar auf Platte gelegt und beim Start wieder geladen. Gespeichert wird nur,
was den Abschluss definiert: Datum, Kalenderwoche und Endzeit. Die Endzeit
braucht der Nachlauf/die Abkuehlphase (``_legionellen_nachlauf_aktiv``), der
sonst nach einem Neustart ebenfalls verloren waere.

Ein ABBRUCH (``legionellen_unvollstaendig``) laesst den gespeicherten
Abschluss unberuehrt - er beschreibt den letzten erfolgreichen Lauf.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime
from typing import Optional

ZUSTAND_FILE = os.path.join(os.getcwd(), "legionellen_zustand.json")

#: Felder, die den Abschluss beschreiben (State-Attribut -> JSON-Wert).
ZUSTAND_FELDER = (
    "legionellen_last_done",
    "legionellen_wochennummer",
    "legionellen_end_time",
)


def _parse_wert(feld: str, wert):
    """JSON-Wert in den passenden Python-Typ wandeln (oder None)."""
    if wert in (None, ""):
        return None
    if feld == "legionellen_last_done":
        return date.fromisoformat(str(wert))
    if feld == "legionellen_end_time":
        return datetime.fromisoformat(str(wert))
    if feld == "legionellen_wochennummer":
        return int(wert)
    return wert


def save_abschluss(state, path: Optional[str] = None) -> bool:
    """Schreibt den letzten erfolgreichen Abschluss atomar.

    Fehler duerfen den Steuerloop nicht stoppen - sie werden nur geloggt.
    Ohne ``legionellen_last_done`` gibt es nichts zu speichern (kein Lauf
    abgeschlossen). Bestehende Dateien werden dann entfernt, damit kein
    veralteter Abschluss stehen bleibt.
    """
    target = path or ZUSTAND_FILE
    last_done = getattr(state, "legionellen_last_done", None)
    if not isinstance(last_done, date):
        try:
            os.remove(target)
        except OSError:
            pass
        return False
    raw = {"schema_version": 1}
    for feld in ZUSTAND_FELDER:
        wert = getattr(state, feld, None)
        raw[feld] = wert.isoformat() if isinstance(wert, (date, datetime)) else wert
    try:
        verzeichnis = os.path.dirname(target) or "."
        os.makedirs(verzeichnis, exist_ok=True)
        fd, temporaer = tempfile.mkstemp(
            prefix="legionellen_zustand_", suffix=".tmp", dir=verzeichnis
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporaer, target)
            return True
        finally:
            if os.path.exists(temporaer):
                os.remove(temporaer)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("Legionellen-Abschluss nicht speicherbar: %s", exc)
        return False


def load_abschluss(state, path: Optional[str] = None) -> bool:
    """Laedt den letzten Abschluss und setzt ihn in ``state``.

    Nur ein plausibler Wert wird uebernommen: lesbares JSON, bekanntes Schema,
    gueltiges Datum und kein Datum aus der Zukunft (verdrehte Uhr). Alles
    andere wird verworfen - die Prophylaxe ist dann wie bisher faellig.
    """
    target = path or ZUSTAND_FILE
    if not os.path.exists(target):
        return False
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
            raise ValueError("unbekanntes Schema")
        werte = {
            feld: _parse_wert(feld, raw[feld])
            for feld in ZUSTAND_FELDER
            if feld in raw
        }
        last_done = werte.get("legionellen_last_done")
        if not isinstance(last_done, date):
            raise ValueError("legionellen_last_done fehlt")
        local_tz = getattr(state, "local_tz", None)
        heute = datetime.now(local_tz).date() if local_tz is not None else date.today()
        if last_done > heute:
            raise ValueError(f"Abschluss liegt in der Zukunft ({last_done})")
        for feld, wert in werte.items():
            setattr(state, feld, wert)
        logging.info(
            "Legionellen-Abschluss wiederhergestellt: %s (KW %s)",
            last_done,
            werte.get("legionellen_wochennummer"),
        )
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logging.warning(
            "Legionellen-Abschluss nicht lesbar (%s); Datei wird verworfen", exc
        )
        try:
            os.remove(target)
        except OSError:
            pass
        return False
