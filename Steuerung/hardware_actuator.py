"""Einziger Einstiegspunkt für Kompressor-GPIO-Schaltungen."""
import asyncio
import logging


class CompressorActuator:
    """Serialisiert Hardware-Schreibzugriffe und meldet den Erfolg."""

    def __init__(self, hardware, lock):
        self.hardware = hardware
        self.lock = lock

    async def set_state(self, state: bool) -> bool:
        if self.hardware is None:
            return False
        try:
            if self.lock is not None:
                async with self.lock:
                    result = await asyncio.to_thread(
                        self.hardware.set_compressor_state, bool(state)
                    )
            else:
                result = await asyncio.to_thread(
                    self.hardware.set_compressor_state, bool(state)
                )
            return result is not False
        except Exception:
            logging.exception("Kompressor-Aktuator konnte Hardware nicht schalten")
            return False
