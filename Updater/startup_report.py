#!/usr/bin/env python3
"""Start-/Absturzbericht fuer den WP-Manager.

Frueher lag der Python-Code als mehrzeiliges `python3 -c '...'` direkt im
Menue-Skript. Das ist fehleranfaellig: ein einzelnes einfaches Anfuehrungszeichen
im Code beendet den Shell-String, und der Pfad wurde per String-Interpolation
eingebettet. Deshalb liegt der Code jetzt in dieser Datei.

Aufruf:

    WPS_STEER_DIR=/pfad/zum/Steuerung python3 startup_report.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _lade_diagnose(steuer_dir: str | os.PathLike):
    ziel = str(steuer_dir)
    if ziel not in sys.path:
        sys.path.insert(0, ziel)
    import startup_diagnose as sd  # noqa: PLC0415 - absichtlich spaeter Import

    return sd


def _zeilen(sd) -> list[str]:
    zeilen: list[str] = []
    info = sd.pruefe_lauf_start_grund()
    if sd.lauf_ist_aktiv():
        zeilen.append(
            "OK: Die Steuerung laeuft gerade - die Statusdatei gehoert "
            "zum aktiven Prozess."
        )
    elif info.get("erster_start"):
        zeilen.append("Kein Vorlauf verzeichnet (erster Start seit Einfuehrung der Diagnose).")
    elif info.get("unsauber"):
        zeilen.append("WARNUNG: Letzter Lauf wurde NICHT sauber beendet.")
        zeilen.append(
            f"  Vorheriger Start:      {info.get('vorheriger_start') or '?'}"
        )
        zeilen.append(
            f"  Letztes sauberes Ende: {info.get('vorheriges_ende') or 'kein Eintrag'}"
        )
        if info.get("oom_hinweis"):
            zeilen.append(f"  Kernel-Log: {info['oom_hinweis']}")
            zeilen.append("  => Verdacht: OOM-Kill (Speicher).")
        else:
            zeilen.append(
                "  Kein OOM-Hinweis im Kernel-Log lesbar -> "
                "Crash / harter Reset / Strom?"
            )
    else:
        zeilen.append("OK: Letzter Lauf wurde sauber beendet.")
    zeilen.append("")
    try:
        zeilen.append(f"Speicher jetzt: {sd.formatiere_speicher(sd.speicher_werte())}")
    except Exception as exc:  # pragma: no cover - defensiv
        zeilen.append(f"Speicher nicht lesbar: {exc}")
    return zeilen


def main(argv=None) -> int:
    steuer_dir = (os.environ.get("WPS_STEER_DIR") or str(Path.cwd())).strip()
    try:
        sd = _lade_diagnose(steuer_dir)
    except Exception as exc:
        print(f"startup_diagnose nicht verfuegbar: {exc}")
        return 0
    for zeile in _zeilen(sd):
        print(zeile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
