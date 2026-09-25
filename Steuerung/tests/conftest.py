import sys
import os
from unittest.mock import MagicMock
import pytest

# Add the project root to the python path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# --- Vertrags-/Strukturtests automatisch markieren -----------------------
#
# Diese Tests pruefen Absichten im Quelltext ("diese Logzeile muss stehen").
# Sie haben mehrfach echte Regressionen gefunden, sind aber beim Refactoring
# rueckwaertsbrechend, auch wenn die Logik gleichwertig umgebaut wird.
# Deshalb sind sie als `contract` markiert und gezielt abruf-/ausblendbar:
#   pytest -m "contract"      nur diese
#   pytest -m "not contract"  nur Verhaltenstests
#
# Automatisch statt manuell, damit neue Tests nicht unmarkiert bleiben.
_QUELLTEXT_MARKER = (
    "read_text(",
    "in html",
    "in manager",
    "in deploy",
    "in text",
    "in main_text",
    "in script_text",
    "in nginx",
    "in worker",
)


def pytest_collection_modifyitems(config, items):
    """Kennzeichnet Quelltext-Tests automatisch mit `contract`."""
    import inspect

    mark = pytest.mark.contract
    for item in items:
        obj = getattr(item, "obj", None)
        if obj is None:
            continue
        try:
            quelle = inspect.getsource(obj)
        except (OSError, TypeError):
            continue
        if any(marker in quelle for marker in _QUELLTEXT_MARKER):
            item.add_marker(mark)

# --- MOCK HARDWARE MODULES BEFORE IMPORTING APP CODE ---

# Mock RPi.GPIO
mock_gpio = MagicMock()
mock_gpio.BCM = "BCM"
mock_gpio.OUT = "OUT"
mock_gpio.IN = "IN"
mock_gpio.HIGH = 1
mock_gpio.LOW = 0
mock_gpio.getmode.return_value = None # Default to None
sys.modules["RPi"] = MagicMock()
sys.modules["RPi.GPIO"] = mock_gpio

# Mock smbus2
sys.modules["smbus2"] = MagicMock()

# Mock RPLCD
mock_rplcd = MagicMock()
sys.modules["RPLCD"] = mock_rplcd
sys.modules["RPLCD.i2c"] = mock_rplcd

# Mock w1thermsensor (just in case)
sys.modules["w1thermsensor"] = MagicMock()

@pytest.fixture(autouse=True)
def isolate_entscheidungslog(tmp_path, monkeypatch):
    """Unit-Tests duerfen niemals in den echten Entscheidungslog schreiben."""
    import entscheidungs_log

    test_pfad = str(tmp_path / "entscheidungs_log.jsonl")
    monkeypatch.setattr(entscheidungs_log, "LOG_DATEI", test_pfad)
    monkeypatch.setattr(entscheidungs_log, "_cache_pfad", None)
    monkeypatch.setattr(entscheidungs_log, "_cache_zeile", None)
    yield
    entscheidungs_log._cache_pfad = None
    entscheidungs_log._cache_zeile = None


@pytest.fixture
def mock_aioresponse():
    """Fixture to mock aiohttp responses if needed."""
    with MagicMock() as mock:
        yield mock


def pytest_configure(config):
    """Registriere eigene Marker (verhindert PytestUnknownMarkWarning)."""
    config.addinivalue_line(
        "markers",
        "integration: benoetigt echte Credentials/Netzwerk und hat Seiteneffekte "
        "(z.B. sendet echte Telegram-Nachrichten). Mit -m 'not integration' ausschliessen."
    )
