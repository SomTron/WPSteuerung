"""Persistente Legionellen-Tagesplanung.

Der Plan wird nicht als RAM-only Zustand behandelt: Nach einem Service-Neustart
muss derselbe bewusst gewaehlte PV-Tag weiterhin gelten, solange der Forecast
noch frisch ist.  Das Modul ist absichtlich klein und ohne externe Abhaengigkeiten.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from datetime import date, datetime
from typing import Any, Optional
from constants import FORECAST_MAX_AGE_HOURS

PLAN_FILE = os.path.join(os.getcwd(), "legionellen_plan.json")
PLAN_FIELDS = (
    "legionellen_planned_day",
    "legionellen_planned_tag",
    "legionellen_planned_time",
    "legionellen_planned_date",
    "legionellen_planned_forecast_wh",
    "legionellen_plan_revision",
    "legionellen_plan_created_at",
    "legionellen_planned_reason",
)


def _parse_value(field: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    if field == "legionellen_planned_date":
        return date.fromisoformat(str(value))
    if field == "legionellen_plan_created_at":
        return datetime.fromisoformat(str(value))
    if field in {"legionellen_planned_tag", "legionellen_plan_revision"}:
        return int(value)
    if field == "legionellen_planned_forecast_wh":
        return float(value)
    return str(value)


def load_plan(state, path: Optional[str] = None) -> bool:
    """Lädt einen gültigen Plan; ungültige/veraltete Dateien werden entfernt."""
    target = path or PLAN_FILE
    if not os.path.exists(target):
        return False
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict) or raw.get("schema_version", 1) != 1:
            raise ValueError("unbekanntes Plan-Schema")
        for field in PLAN_FIELDS:
            if field in raw:
                setattr(state, field, _parse_value(field, raw[field]))
        planned_date = getattr(state, "legionellen_planned_date", None)
        planned_forecast = getattr(state, "legionellen_planned_forecast_wh", None)
        planned_tag = getattr(state, "legionellen_planned_tag", None)
        if (
            not isinstance(planned_tag, int)
            or planned_tag < 0
            or planned_tag > 6
            or not isinstance(planned_forecast, (int, float))
            or isinstance(planned_forecast, bool)
            or not math.isfinite(float(planned_forecast))
            or planned_forecast < 0
        ):
            clear_plan(state, "Plan ungültig", persist=False)
            save_plan(state, path=target)
            return False
        created_at = getattr(state, "legionellen_plan_created_at", None)
        local_tz = getattr(state, "local_tz", None)
        heute = datetime.now(local_tz).date() if local_tz is not None else date.today()
        if planned_date is None or planned_date < heute or not isinstance(created_at, datetime):
            clear_plan(state)
            save_plan(state, path=target)
            return False
        if created_at.tzinfo is None:
            age = (datetime.now() - created_at).total_seconds()
        else:
            age = (datetime.now(created_at.tzinfo) - created_at).total_seconds()
        if age < 0 or age > FORECAST_MAX_AGE_HOURS * 3600:
            clear_plan(state, "Plan zu alt", persist=False)
            save_plan(state, path=target)
            return False
        logging.info("Legionellenplan aus %s wiederhergestellt", target)
        return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logging.warning("Legionellenplan nicht lesbar (%s); Plan wird verworfen", exc)
        try:
            os.remove(target)
        except OSError:
            pass
        clear_plan(state)
        return False


def save_plan(state, path: Optional[str] = None) -> bool:
    """Speichert den Plan atomar; Fehler dürfen den Steuerloop nicht stoppen."""
    target = path or PLAN_FILE
    raw: dict[str, Any] = {"schema_version": 1}
    for field in PLAN_FIELDS:
        value = getattr(state, field, None)
        if value is not None and not isinstance(value, (str, int, float, bool, date, datetime)):
            value = None
        if isinstance(value, (date, datetime)):
            raw[field] = value.isoformat()
        else:
            raw[field] = value
    try:
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="legionellen_plan_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, target)
            return True
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
    except (OSError, TypeError, ValueError) as exc:
        logging.warning("Legionellenplan nicht speicherbar: %s", exc)
        return False


def clear_plan(state, reason: str = "Kein belastbarer PV-Tag", persist: bool = False) -> None:
    """Leert einen bestehenden Plan idempotent.

    Die Revisionsnummer gehört nicht zur Plan-Existenz. Deshalb darf ein leerer
    Plan bei jedem Forecast-Stale-Check nicht erneut hochgezählt und geschrieben
    werden.
    """
    plan_fields = (
        "legionellen_planned_day",
        "legionellen_planned_tag",
        "legionellen_planned_time",
        "legionellen_planned_date",
        "legionellen_planned_forecast_wh",
        "legionellen_plan_created_at",
    )
    had_plan = any(getattr(state, field, None) is not None for field in plan_fields)
    if not had_plan:
        state.legionellen_planned_reason = reason
        return
    for field in plan_fields:
        setattr(state, field, None)
    state.legionellen_planned_reason = reason
    state.legionellen_plan_revision = (
        getattr(state, "legionellen_plan_revision", 0) + 1
    )
    if persist:
        save_plan(state)
