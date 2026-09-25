"""Gemeinsame Regelverträge der Prioritäten-Engine."""
from dataclasses import dataclass
from typing import Dict, Optional


def _infer_reason_code(name: str, enabled: bool, action: Optional[bool], reason: str) -> str:
    """Stabile, textunabhängige Klassifikation für API/Logging."""
    text = (reason or "").lower()
    if "nachtsperre" in text:
        return "nachtsperre"
    if "sensor" in text and ("fehlt" in text or "nicht verfuegbar" in text or "invalid" in text):
        return "sensor_unavailable"
    if "stale" in text or "veraltet" in text:
        return "data_stale"
    if action is None:
        if "legionellen" in name.lower() and ("warte" in text or "quelle" in text):
            return "waiting_source"
        if "pv" in name.lower() and (
            "keine aktion" in text or "keine bedingung" in text
            or ("pv" in text and " < " in text)
        ):
            return "pv_unterbrechung"
        return "no_action"
    if action is False:
        return "stop"
    return "start"


@dataclass
class RegelErgebnis:
    """Ergebnis einer einzelnen Regelbewertung."""

    name: str
    prioritaet: int
    aktiv: bool
    einschalten: Optional[bool] = None
    grund: str = ""
    regel_dict: Optional[Dict] = None
    reason_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.reason_code is None:
            self.reason_code = _infer_reason_code(
                self.name, self.aktiv, self.einschalten, self.grund
            )

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "grund" and self.__dict__.get("reason_code") in (None, "no_action"):
            object.__setattr__(self, "reason_code", _infer_reason_code(
                self.name, self.aktiv, self.einschalten, value
            ))
        elif name == "einschalten" and self.__dict__.get("reason_code") in (None, "no_action"):
            object.__setattr__(self, "reason_code", _infer_reason_code(
                self.name, self.aktiv, value, self.grund
            ))
