"""Gestaffelte Wiederholung fuer unbeaufsichtigte Telegram-Alarme.

Hintergrund: Der Hauptloop laeuft alle 10 Sekunden. Mehrere sicherheits-
relevante Ereignisse - ein nicht abschaltbarer Kompressor, eine fehlgeschlagene
Kompressorverifizierung, ein unsauberer Dienstneustart - koennen in diesem
Takt erneut auftreten. Ohne Drosselung ergaeben sich im ungünstigsten Fall
bis zu sechs Telegram-Nachrichten pro Minute.

Zusaetzlich ist `Restart=always` mit `RestartSec=10` aktiv: Ein Crash-Loop
erzeugt einen Neustart alle zehn Sekunden und damit ebenso viele
Absturz-Meldungen.

Diese Drosselung ersetzt das vollstaendige Stillschalten: Die **erste**
Meldung geht immer sofort raus, danach wird mit wachsendem Abstand
wiederholt. Ein dauerhafter Fehler bleibt sichtbar, ohne die Kette zu
fluten.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Sequence

from utils import safe_timedelta

# Wartezeit in Minuten nach der 1., 2., 3., ... bereits gesendeten Meldung.
# Der erste Eintrag gilt fuer "noch nie gesendet" und wird nie abgewartet.
ESCALATION_STEPS_MIN: Sequence[int] = (0, 5, 15, 60, 240)

# Kuerzere Stufen fuer Ereignisse, die sich im Minutentakt wiederholen.
VERIFIKATION_STEPS_MIN: Sequence[int] = (0, 5, 30, 120)

# Kuerzere Stufen fuer Neustarts: hier zaehlt die Haeufigkeit im Betrieb.
NEUSTART_STEPS_MIN: Sequence[int] = (0, 10, 30, 60)


def _zaehler_key(key: str) -> str:
    return f"{key}_zaehler"


def gesendet_anzahl(state, key: str) -> int:
    """Anzahl der bereits gesendeten Meldungen fuer diesen Alarm."""
    try:
        return int(getattr(state, _zaehler_key(key), 0) or 0)
    except (TypeError, ValueError):
        return 0


def wartezeit_minuten(zaehler: int, steps: Sequence[int]) -> float:
    """Naechste Wartezeit, abhaengig von der bisherigen Zahl gesendeter Meldungen."""
    index = min(max(int(zaehler), 0), len(steps) - 1)
    return float(steps[index])


def reset(state, key: str) -> None:
    """Setzt die Drosselung zurueck - die naechste Meldung geht sofort raus.

    Wird bei erfolgreicher Erholung aufgerufen, damit ein spaeter
    auftretendes, gleiches Ereignis nicht unterdrueckt wird.
    """
    setattr(state, key, None)
    setattr(state, _zaehler_key(key), 0)


def soll_senden(
    state,
    key: str,
    now: datetime,
    local_tz=None,
    steps: Sequence[int] = ESCALATION_STEPS_MIN,
) -> bool:
    """Entscheidet, ob jetzt gesendet werden darf, und zaehlt mit.

    Rueckgabe ``True`` heisst: jetzt senden. ``False`` heisst: noch nicht
    genug Zeit seit dem letzten Versand vergangen.

    Die erste Meldung eines Ereignisses wird immer sofort gesendet.
    """
    marker: Optional[datetime] = getattr(state, key, None)
    anzahl = gesendet_anzahl(state, key)

    if not isinstance(marker, datetime):
        setattr(state, key, now)
        setattr(state, _zaehler_key(key), 1)
        return True

    index = min(max(anzahl, 0), len(steps) - 1)
    schwelle = timedelta(minutes=float(steps[index]))

    try:
        seit = safe_timedelta(now, marker, local_tz)
    except (TypeError, ValueError, OverflowError):
        seit = schwelle

    if seit >= schwelle:
        setattr(state, key, now)
        setattr(state, _zaehler_key(key), anzahl + 1)
        return True
    return False
