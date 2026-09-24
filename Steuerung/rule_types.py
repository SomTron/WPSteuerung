"""Gemeinsame Regelverträge der Prioritäten-Engine."""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class RegelErgebnis:
    """Ergebnis einer einzelnen Regelbewertung."""

    name: str
    prioritaet: int
    aktiv: bool
    einschalten: Optional[bool] = None
    grund: str = ""
    regel_dict: Optional[Dict] = None
