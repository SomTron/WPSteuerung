"""Tests fuer den Hardware-Interface-Vertrag.

Warum wichtig: `set_compressor_state()` gibt `True` **nur** bei
erfolgreicher Schaltung zurueck. Darauf baut der zentrale Aktuator auf:
`state.control.kompressor_ein` wird erst nach bestaetigter Hardware-
Rueckmeldung gesetzt. Ein Interface, das stillschweigend `None` liefert
oder `True` ohne echte Schaltung meldet, wuerde die fail-safe-Kette
durchbrechen - die WP koennte als "aus" gelten, laeuft aber.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from hardware_interface import HardwareInterface  # noqa: E402


def baue(klasse_name, set_state=True, read_pressure=True, mit_cleanup=True):
    """Baut eine Dummy-Implementierung des Vertrags.

    `mit_cleanup=False` laesst `cleanup()` bewusst weg. `__abstractmethods__`
    wird bei der Klassenbildung festgehalten, deshalb muss die Variante
    wirklich im Klassenrumpf fehlen und nicht nachtraeglich entfernt werden.
    """
    ns = {
        "init_gpio": lambda self: None,
        "init_lcd": _async_none,
        "set_compressor_state": lambda self, state: set_state,
        "read_pressure_sensor": lambda self: read_pressure,
        "write_lcd": lambda self, line1="", line2="", line3="", line4="": None,
    }
    if mit_cleanup:
        ns["cleanup"] = lambda self: None
    Dummy = type(klasse_name, (HardwareInterface,), ns)
    return Dummy


async def _async_none():
    return None


# ------------------------------------------------------------- Abstraktion


def test_interface_kann_nicht_direkt_instanziiert_werden():
    with pytest.raises(TypeError):
        HardwareInterface()  # type: ignore[abstract]


def test_vollstaendige_implementation_ist_instantiierbar():
    d = baue("Voll")()
    assert isinstance(d, HardwareInterface)
    assert d.set_compressor_state(True) is True
    assert d.read_pressure_sensor() is True


def test_fehlende_einzige_methode_verhindert_instantiierung():
    with pytest.raises(TypeError):
        baue("OhneCleanup", mit_cleanup=False)()


def test_alle_abstrakten_methoden_sind_vorhanden():
    """Vertrag regressionssicher halten: neue Methode -> Test faellt auf."""
    erwartet = {
        "init_gpio",
        "init_lcd",
        "set_compressor_state",
        "read_pressure_sensor",
        "write_lcd",
        "cleanup",
    }
    assert set(HardwareInterface.__abstractmethods__) == erwartet


# ------------------------------------------- Rueckmeldung ist fail-safe


def test_set_compressor_state_muss_einen_wert_liefern():
    """Der Aufrufer wertet die Rueckmeldung aus - 'nichts' ist kein Erfolg."""
    d = baue("OhneRueckmeldung", set_state=None)()
    assert d.set_compressor_state(True) is not True


def test_fehlgeschlagene_schaltung_liefert_false():
    d = baue("Fehler", set_state=False, read_pressure=False)()
    assert d.set_compressor_state(True) is False
    assert d.read_pressure_sensor() is False


def test_mock_erfuellt_den_vertrag():
    """Die Mock-Hardware muss das Interface vollstaendig bedienen."""
    from hardware_mock import MockHardwareManager

    m = MockHardwareManager()
    assert isinstance(m, HardwareInterface)
    assert isinstance(m.set_compressor_state(True), bool)
    assert isinstance(m.read_pressure_sensor(), bool)
