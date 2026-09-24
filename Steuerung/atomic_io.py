"""Atomare Dateischreibvorgänge für Zustands- und Diagnosedateien."""
import json
import os
import tempfile


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Schreibt Text atomar; bei Fehler bleibt die alte Datei erhalten."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def atomic_write_json(path: str, data, encoding: str = "utf-8") -> None:
    """Schreibt JSON atomar und mit stabilem Format."""
    atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding=encoding,
    )
