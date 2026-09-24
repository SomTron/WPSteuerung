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
