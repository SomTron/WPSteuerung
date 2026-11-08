"""Wochenende-Regel (Blockiert Einschalten am Wochenende vor fruehestens_uhr).

Ausgelagert aus priority_control.py. Re-Export in priority_control.py.
"""
from datetime import datetime

from rule_types import RegelErgebnis
from json_config import WochenendeConfig
from regel_helpers import _is_weekend


def evaluate_wochenende(
    wochenende: WochenendeConfig,
    now: datetime,
    kompressor_ein: bool = False,
) -> RegelErgebnis:
    if not wochenende.aktiv:
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,
            aktiv=False,
            grund="Wochenende-Regel inaktiv",
        )

    if not _is_weekend(now):
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,
            aktiv=False,
            grund="Kein Wochenende",
        )

    if now.hour < wochenende.fruehestens_uhr:
        return RegelErgebnis(
            name="Wochenende",
            prioritaet=wochenende.prioritaet,
            aktiv=True,
            einschalten=None if kompressor_ein else False,
            grund=(
                f"Wochenende: Vor {wochenende.fruehestens_uhr} Uhr ({now.hour}:xx); "
                + (
                    "laufender Zyklus bleibt bis Mindestlaufzeit aktiv"
                    if kompressor_ein
                    else "Start gesperrt -> AUS"
                )
            ),
        )

    return RegelErgebnis(
        name="Wochenende",
        prioritaet=wochenende.prioritaet,
        aktiv=True,
        einschalten=None,
        grund=f"Wochenende: Ab {wochenende.fruehestens_uhr} Uhr erlaubt ({now.hour}:xx)",
    )