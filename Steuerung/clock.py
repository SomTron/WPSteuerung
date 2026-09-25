"""Zentrale Zeitquelle mit lokaler Zeitzone und monotoner Laufzeit."""
import time
from datetime import datetime


class Clock:
    def __init__(self, local_tz):
        self.local_tz = local_tz

    def now(self) -> datetime:
        return datetime.now(self.local_tz)

    def monotonic(self) -> float:
        return time.monotonic()


def now_for(state, fallback_tz=None):
    """Einheitliche Zeitquelle für Live-State und API-Snapshots."""
    clock = getattr(state, "clock", None)
    if clock is not None and hasattr(clock, "now"):
        try:
            value = clock.now()
            if isinstance(value, datetime):
                return value
        except (AttributeError, TypeError, ValueError):
            pass
    local_tz = getattr(state, "local_tz", None) or fallback_tz
    return datetime.now(local_tz)
