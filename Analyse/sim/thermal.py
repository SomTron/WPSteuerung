"""Re-Export: das Speichermodell liegt seit 05.11.2026 in Steuerung/.

Grund: Die Steuerung braucht das Modell fuer die Etappenrechnung
(Vorwaertssimulation der Zeit bis zur Zieltemperatur). Zwei Kopien waeren
auseinandergedriftet - genau der Fehler, den der Parameter-Fit vom
05.11.2026 aufgedeckt hat (Konvektion um Faktor 100 zu hoch, weil sie in
jedem Schritt an die Stabilitaetsgrenze geklemmt wurde).

Alle Namen bleiben hier importierbar, damit die Simulation unveraendert
laeuft: ``from thermal import Speicher, SpeicherParameter``.
"""
from __future__ import annotations

import os
import sys

_STEUERUNG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "Steuerung",
)
if _STEUERUNG not in sys.path:
    sys.path.insert(0, _STEUERUNG)

from thermal_modell import (  # noqa: E402,F401
    C_WASSER_WH_K,
    Schicht,
    Speicher,
    SpeicherParameter,
)

__all__ = ["C_WASSER_WH_K", "Schicht", "Speicher", "SpeicherParameter"]
