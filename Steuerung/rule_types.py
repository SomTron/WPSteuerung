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
    """Ergebnis einer einzelnen Regelbewertung.

    `reason_code` wird aus dem Grundtext abgeleitet. Wird das Ergebnis nach
    der Erzeugung veraendert (`grund` oder `einschalten`), wird der Code neu
    abgeleitet - unabhaengig davon, in welcher Reihenfolge veraendert wird.
    Wer einen bestimmten Code erzwingen will, nutzt `set_reason_code()`;
    dieser bleibt dann gegenueber weiteren Aenderungen stabil.
    """

    name: str
    prioritaet: int
    aktiv: bool
    einschalten: Optional[bool] = None
    grund: str = ""
    regel_dict: Optional[Dict] = None
    reason_code: Optional[str] = None
    # Vom Aufrufer bewusst gesetzt -> keine automatische Ableitung mehr.
    reason_code_gesetzt: bool = False

    def __post_init__(self) -> None:
        if self.reason_code is None:
            self.reason_code = _infer_reason_code(
                self.name, self.aktiv, self.einschalten, self.grund
            )
        else:
            # Explizit beim Erzeugen uebergeben -> gilt als gesetzt.
            object.__setattr__(self, "reason_code_gesetzt", True)

    def set_reason_code(self, code: Optional[str]) -> None:
        """Setzt den Code fest und sperrt ihn gegenueber Re-Inferenz."""
        object.__setattr__(self, "reason_code", code)
        object.__setattr__(self, "reason_code_gesetzt", True)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "reason_code_gesetzt":
            return
        if self.__dict__.get("reason_code_gesetzt"):
            return
        if name in ("grund", "einschalten"):
            # Bewusst aus beiden Feldern neu ableiten, damit die Reihenfolge
            # der Zuweisungen das Ergebnis nicht beeinflusst.
            object.__setattr__(
                self, "reason_code",
                _infer_reason_code(
                    self.name, self.aktiv, self.einschalten, self.grund
                ),
            )
