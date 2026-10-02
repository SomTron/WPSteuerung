"""Not-Aus: Kompressor aus, Steuerung aus, bis von Hand zurueckgesetzt.

Warum eine Datei und nur ein State-Flag
--------------------------------------
Die Unit laeuft mit ``Restart=always`` und ``RestartSec=10``. Ein blosses
Beenden des Main-Loops wuerde zehn Sekunden spaeter automatisch wieder
starten - der Not-Aus waere wirkungslos. Die Sperre liegt deshalb auf der
Platte: sie ueberlebt jeden Neustart, egal ob systemd, ein Absturz oder
ein manueller Neustart sie ausloest.

Zusaetzlich beendet sich der Dienst mit :data:`EXIT_CODE_NOTAUS`, und
``wpsteuerung.service`` enthaelt ``RestartPreventExitStatus=42``. Die
Dateisperre ist die belastbare Absicherung fuer den Fall, dass die Unit
auf dem Zielsystem noch nicht aktualisiert wurde.

Ablauf
------
1. Kompressor wird sofort und ohne Rueckfrage ausgeschaltet und das
   Ergebnis geprueft.
2. Die Sperre wird geschrieben, der State gesetzt, Telegram informiert.
3. ``stop_event`` wird gesetzt: der Main-Loop endet kontrolliert, der
   ``finally``-Block schaltet den GPIO noch einmal aus.

Ruecknahme
----------
Der Dienst startet danach zwar wieder (Telegram/WebApp sind dann
erreichbar), laeuft aber im gesperrten Zustand: keine Regel darf den
Kompressor einschalten. Aufgehoben wird die Sperre bewusst nur durch
``notaus aus`` (Telegram) bzw. ``{"command": "notaus_aus"}`` (API) oder
durch das Loeschen der Datei von Hand. Ein Absturz oder ein systemd-
Neustart setzt sie ausdruecklich NICHT zurueck.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

#: Prozess-Exitcode fuer einen regulaeren Not-Aus. In
#: ``wpsteuerung.service`` als ``RestartPreventExitStatus=42`` gefuehrt.
EXIT_CODE_NOTAUS: int = 42

#: Ablage der Sperre neben den uebrigen Zustandsdateien der Steuerung.
NOTAUS_DATEI = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "notaus.lock"
)


def _jetzt() -> str:
    return datetime.now().isoformat(timespec="seconds")


def notaus_gesetzt(datei: Optional[str] = None) -> bool:
    """True, wenn eine Not-Aus-Sperre auf der Platte liegt."""
    pfad = datei or NOTAUS_DATEI
    try:
        return os.path.isfile(pfad)
    except OSError:
        return False


def notaus_daten(datei: Optional[str] = None) -> Optional[dict]:
    """Liest Grund und Zeitpunkt der Sperre (None = keine Sperre)."""
    pfad = datei or NOTAUS_DATEI
    try:
        with open(pfad, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        # Vorhandene, aber unlesbare Datei ist trotzdem eine Sperre: Not-Aus
        # darf nie durch ein beschaedigtes File aufgehoben werden.
        if notaus_gesetzt(pfad):
            return {"grund": "unbekannt (Sperre nicht lesbar)", "ts": None}
        return None


def notaus_setzen(grund: str = "manuell", datei: Optional[str] = None) -> bool:
    """Schreibt die Sperre. True bei Erfolg.

    Fehlschlaege werden NICHT verschluckt, aber auch nicht zum Exception:
    der Aufrufer muss den Kompressor ohnehin ausschalten, ein fehlgeschlagener
    Schreibvorgang darf die Abschaltung nicht verhindern.
    """
    pfad = datei or NOTAUS_DATEI
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump(
                {"grund": str(grund), "ts": _jetzt(), "exit_code": EXIT_CODE_NOTAUS},
                f,
                ensure_ascii=False,
            )
            f.flush()
            os.fsync(f.fileno())
        logging.critical(f"NOT-AUS gesetzt: {grund} ({pfad})")
        return True
    except OSError as exc:
        logging.critical(
            f"NOT-AUS: Sperrdatei {pfad} nicht schreibbar ({exc}) - "
            "Abschaltung erfolgt trotzdem, Sperre haelt nur bis zum Neustart"
        )
        return False


def notaus_loeschen(datei: Optional[str] = None) -> bool:
    """Hebt die Sperre auf. True, wenn danach keine Sperre mehr liegt."""
    pfad = datei or NOTAUS_DATEI
    if not notaus_gesetzt(pfad):
        return True
    try:
        os.remove(pfad)
    except OSError as exc:
        logging.error(f"NOT-AUS: Sperre {pfad} nicht loeschbar: {exc}")
        return False
    logging.warning("NOT-AUS aufgehoben - die Steuerung heizt wieder")
    return not notaus_gesetzt(pfad)