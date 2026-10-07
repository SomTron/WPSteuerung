# -*- coding: utf-8 -*-
"""Persistenz von Bademodus/Urlaubsmodus ueber Dienst-Neustarts.

Befund 07.10.2026: Beide Modi lebten ausschliesslich im RAM. Jeder
Dienst-Neustart (Deployment, Absturz) warf den Bademodus still weg - ein
abends aktivierter Bademodus war nachts nach einem Deployment verschwunden
und die WP heizte mit Normal-Soll. Der Legionellen-Plan wurde dafuer schon
bewusst persistiert (siehe legionellen_plan.py); derselbe Standard gilt
jetzt fuer die Nutzer-Modi.

Ablaufregeln beim Wiederherstellen (professionell: nie unendlich):
- Bademodus: nur wenn gespeichert < 24 h alt (sonst veraltet verworfen)
- Urlaubsmodus: nur wenn urlaubsmodus_ende in der Zukunft liegt
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from atomic_io import atomic_write_text

MODI_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "modi_zustand.json"
)
BADEMODUS_WIEDERHERSTELLUNG_MAX = timedelta(hours=24)


def _now_naiv():
    return datetime.now()


def _bis_utc_naiv(wert) -> Optional[datetime]:
    """Normalisiert aware/naive Datetimes auf naive UTC-Vergleichszeit."""
    dt = _parse_iso(wert)
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _jetzt_utc_naiv():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def speichere_modi(state, pfad: str = MODI_DATEI) -> bool:
    """Sichert die aktuellen Modi-Zustaende atomar. Scheitert nie hart."""
    try:
        start = state.urlaubsmodus_start
        ende = state.urlaubsmodus_ende
        daten = {
            "schema": 1,
            "gespeichert_utc": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
            "bademodus_aktiv": bool(getattr(state, "bademodus_aktiv", False)),
            "urlaubsmodus_aktiv": bool(getattr(state, "urlaubsmodus_aktiv", False)),
            "urlaubsmodus_start": start.isoformat() if isinstance(start, datetime) else None,
            "urlaubsmodus_ende": ende.isoformat() if isinstance(ende, datetime) else None,
        }
        atomic_write_text(pfad, json.dumps(daten, ensure_ascii=False, indent=2) + "\n")
        return True
    except Exception:
        logging.exception("Modi-Zustand konnte nicht gespeichert werden")
        return False


def lade_modi(state, pfad: str = MODI_DATEI, jetzt_utc_naiv: Optional[datetime] = None) -> bool:
    """Stellt Bademodus/Urlaubsmodus nach einem Neustart wieder her.

    Fail-safe: Jede Unstimmigkeit (fehlende/korrupte Datei, veralteter
    Bademodus, abgelaufener Urlaub) wird verworfen und geloggt.
    """
    jetzt = jetzt_utc_naiv or _jetzt_utc_naiv()
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            roh = json.load(f)
        if not isinstance(roh, dict) or roh.get("schema") != 1:
            return False

        gespeichert = roh.get("gespeichert_utc")
        try:
            gespeichert_ts = datetime.fromisoformat(str(gespeichert))
        except (TypeError, ValueError):
            return False

        wiederhergestellt = []

        if roh.get("bademodus_aktiv"):
            alter = _jetzt_utc_naiv() - gespeichert_ts
            if alter <= BADEMODUS_WIEDERHERSTELLUNG_MAX:
                state.bademodus_aktiv = True
                wiederhergestellt.append(
                    f"Bademodus (alter {int(alter.total_seconds() // 60)} min)"
                )
            else:
                logging.warning(
                    "Bademodus aus %d min alter Persistenz verworfen "
                    "(> %d h) - wie bei einer professionellen Steuerung "
                    "wird kein tagelanger Bademodus still reaktiviert",
                    int(alter.total_seconds() // 60),
                    BADEMODUS_WIEDERHERSTELLUNG_MAX.total_seconds() // 3600,
                )

        if roh.get("urlaubsmodus_aktiv"):
            ende = _bis_utc_naiv(roh.get("urlaubsmodus_ende"))
            if ende is not None and ende > jetzt:
                state.urlaubsmodus_aktiv = True
                state.urlaubsmodus_start = _parse_iso(roh.get("urlaubsmodus_start"))
                state.urlaubsmodus_ende = _parse_iso(roh.get("urlaubsmodus_ende"))
                verbleib = ende - jetzt
                wiederhergestellt.append(
                    f"Urlaubsmodus (noch {verbleib.days} d {verbleib.seconds // 3600} h)"
                )
            else:
                logging.info("Urlaubsmodus aus Persistenz verworfen (Ende verstrichen)")

        if wiederhergestellt:
            logging.warning(
                "Modi-Zustand nach Neustart wiederhergestellt: %s",
                ", ".join(wiederhergestellt),
            )
        return bool(wiederhergestellt)
    except FileNotFoundError:
        return False
    except Exception:
        logging.exception("Modi-Persistenz unlesbar - Modi bleiben im Default")
        return False


def _parse_iso(wert) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(wert))
    except (TypeError, ValueError):
        return None