# -*- coding: utf-8 -*-
"""Verdrahtungstests der Sicherheitskette (Befund 21.10.2026).

BUGFIX-Hintergrund
-------------------
Am 02.09.2026 (Commit 51540d3, "refactor: 2.1 redundante Safety-Pruefung
entfernt") wurde der Aufruf von `safety_logic.check_sensors_and_safety` aus
`main.run_logic_step` geloescht. Begruendet wurde das damit, dass
`priority_control_logic.check_safety_limits` bereits intern delegiere.

Das tat es NICHT. `check_sensors_and_safety` wurde danach nie mehr
aufgerufen - und damit waren drei Schutzabschaltungen 7 Wochen lang tot:

  1. der Verdampfer-Eisfrei-Schutz (Hardware-Risiko),
  2. die Sensorfehler-Sperre (abgerissener DS18B20),
  3. die Ubertemperaturpruefung am unteren Fuehler.

Warum die Testsuite das nicht sah: `tests/test_evaporator_hysteresis.py`
pruefte `check_sensors_and_safety` direkt - die Funktion funktionierte ja.
Getestet wurde nie die VERDRAHTUNG zwischen main und der Schutzfunktion.

Diese Datei schliesst genau diese Luecke: sie ruft `check_safety_limits`
(das, was main.py tatsaechlich aufruft) mit realistischen States auf und
prueft, dass die Schutzwirkung dort ANKOMMT.
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402


def _state(t_oben=45.0, t_unten=35.0, t_mittig=40.0, t_verd=15.0,
           kompressor_ein=True, ueberhitzung_c=58.0, legionellen_aktiv=False):
    """Realistischer State - im Gegensatz zu einem MagicMock liefert er
    echte Zahlen fuer alle Schwellen (Config-Werte wie im Betrieb)."""
    return SimpleNamespace(
        local_tz=None,
        priority_config=SimpleNamespace(
            sicherheit=SimpleNamespace(
                ueberhitzung_c=ueberhitzung_c, max_temp_c=48.0,
            ),
            legionellen=SimpleNamespace(legionellen_max_temp_c=65.0),
        ),
        legionellen_aktiv=legionellen_aktiv,
        legionellen_temp_override=None,
        config=SimpleNamespace(Heizungssteuerung=SimpleNamespace(
            VERDAMPFERTEMPERATUR=6.0,
            VERDAMPFER_RESTART_TEMP=9.0,
            SICHERHEITS_TEMP=58.0,
        )),
        sensors=SimpleNamespace(
            t_oben=t_oben, t_unten=t_unten, t_mittig=t_mittig,
            t_verd=t_verd, t_boiler=t_oben,
        ),
        control=SimpleNamespace(
            kompressor_ein=kompressor_ein, blocking_reason=None,
            active_rule_name=None, ausschluss_grund=None,
        ),
        verdampfer_blocked=False,
        verdampfer_shutdowns=[],
        last_sensor_error_time=None,
        _last_sensor_error_log=None,
    )


async def _pruefe(state, t_oben, t_unten, t_mittig, t_verd):
    from unittest.mock import AsyncMock
    setter = AsyncMock(return_value=True)
    erg = await pcl.check_safety_limits(
        None, state, t_oben, t_unten, t_mittig, t_verd, setter,
    )
    return erg, setter


# ── 1) Verdampfer-Eisfrei-Schutz: der Hardware-Schutz ──────────────────

@pytest.mark.asyncio
async def test_verdampfer_zu_kalt_blockiert_und_schaltet_ab():
    """Vereister Verdampfer -> kein Heizen, laufender Kompressor AUS.

    Das ist der wichtigste Test der Datei: ein vereister Verdampfer kann
    den Kompressor mechanisch zerstoeren.
    """
    state = _state(t_verd=2.0)
    erg, setter = await _pruefe(state, 45.0, 35.0, 40.0, 2.0)

    assert erg is False, "Verdampfer-Eisfrei-Schutz muss blockieren"
    assert "Verdampfer" in state.control.blocking_reason, state.control.blocking_reason
    setter.assert_awaited_once()
    assert setter.await_args.args[1] is False, "Kompressor muss abgeschaltet werden"


@pytest.mark.asyncio
async def test_verdampfer_hysterese_haelt_blockiert_bis_restart():
    """Einmal blockiert -> bleibt blockiert bis oberhalb der Restart-Temperatur."""
    state = _state(t_verd=2.0)
    erg, _ = await _pruefe(state, 45.0, 35.0, 40.0, 2.0)
    assert erg is False
    assert state.verdampfer_blocked is True

    # 7 C: ueber der Eisfrei-Grenze (6 C), aber unter Restart (9 C)
    state2 = _state(t_verd=7.0)
    state2.verdampfer_blocked = True
    erg2, _ = await _pruefe(state2, 45.0, 35.0, 40.0, 7.0)
    assert erg2 is False, "Hysterese: erst nach Restart freigeben"
    assert "Warten auf Erwärmung" in state2.control.ausschluss_grund

    # 12 C: ueber der Restart-Temperatur -> wieder frei
    state3 = _state(t_verd=12.0)
    state3.verdampfer_blocked = True
    erg3, setter3 = await _pruefe(state3, 45.0, 35.0, 40.0, 12.0)
    assert erg3 is True, state3.control.blocking_reason
    assert state3.verdampfer_blocked is False
    setter3.assert_not_awaited()


@pytest.mark.asyncio
async def test_verdampfer_wird_als_ereignis_erfasst():
    """Der Verdampfer-Block wird protokolliert (fuer die API-Anzeige)."""
    state = _state(t_verd=1.0)
    await _pruefe(state, 45.0, 35.0, 40.0, 1.0)
    assert len(state.verdampfer_shutdowns) == 1


# ── 2) Sensorfehler-Sperre ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sensorfehler_blockiert_und_schaltet_ab():
    """NaN an einem Fuehler -> keine Heizung, laufender Zyklus AUS."""
    state = _state()
    erg, setter = await _pruefe(state, 45.0, float("nan"), 40.0, 15.0)

    assert erg is False, "Sensorfehler muss blockieren"
    assert "Sensor" in state.control.blocking_reason, state.control.blocking_reason
    setter.assert_awaited_once()
    assert setter.await_args.args[1] is False


@pytest.mark.asyncio
async def test_abgerissener_fuehler_minus_127_blockiert():
    """-127 C ist das klassische DS18B20-Fehlerbild ('Fuehler abgerissen')."""
    state = _state()
    erg, setter = await _pruefe(state, -127.0, 35.0, 40.0, 15.0)

    assert erg is False
    setter.assert_awaited_once()


@pytest.mark.asyncio
async def test_sensorfehler_bei_kompressor_aus_blockiert_start():
    """Auch ohne laufenden Kompressor: ein Start darf nicht stattfinden."""
    state = _state(kompressor_ein=False)
    erg, setter = await _pruefe(state, 45.0, float("nan"), 40.0, 15.0)

    assert erg is False
    # Kein laufender Kompressor -> nichts abzuschalten, aber Start blockiert.
    setter.assert_not_awaited()


# ── 3) Ueberhitzung am unteren Fuehler ──────────────────────────────────

@pytest.mark.asyncio
async def test_uebertemperatur_unten_wird_erkannt():
    """Der untere Fuehler war nie geprueft - jetzt schon."""
    state = _state(t_unten=70.0)
    erg, setter = await _pruefe(state, 45.0, 70.0, 40.0, 15.0)

    assert erg is False, "Ueberhitzung am Bezugsfuehler muss blockieren"
    assert "UEBERHITZUNG" in state.control.blocking_reason, state.control.blocking_reason
    setter.assert_awaited_once()


@pytest.mark.asyncio
async def test_uebertemperatur_oben_wird_erkannt():
    """Der obere Fuehler war der einzige gepruefte - er bleibt es."""
    state = _state(t_oben=70.0)
    erg, setter = await _pruefe(state, 70.0, 35.0, 40.0, 15.0)

    assert erg is False
    assert "UEBERHITZUNG" in state.control.blocking_reason
    setter.assert_awaited_once()


@pytest.mark.asyncio
async def test_ueberhitzung_wird_als_uebertemperatur_klassifiziert():
    """Der Sperrtext muss in die richtige Alarmfamilie.

    `blocking_codes.blocking_code` klassifiziert nach Textfragmenten. Ein
    abweichender Wortlaut wuerde den Alarm als 'sonstige_sperre' melden
    statt als 'uebertemperatur' - der Betreiber saehe die Stoerung nicht
    als Ueberhitzung.
    """
    from blocking_codes import blocking_code

    state = _state(t_oben=70.0)
    await _pruefe(state, 70.0, 35.0, 40.0, 15.0)
    assert blocking_code(state.control.blocking_reason) == "uebertemperatur"


# ── 4) Normalbetrieb: nichts blockiert ─────────────────────────────────

@pytest.mark.asyncio
async def test_normalbetrieb_blockiert_nicht():
    """Alle Werte unauffaellig -> kein Eingriff, kein Abschalten."""
    state = _state()
    erg, setter = await _pruefe(state, 45.0, 35.0, 40.0, 15.0)

    assert erg is True, state.control.blocking_reason
    setter.assert_not_awaited()


@pytest.mark.asyncio
async def test_legionellen_bypass_hebt_die_schwelle():
    """Waehrend der Legionellenfahrt gilt 65 C statt 58 C.

    Sonst wuerde der Schutz die Prophylaxe (Ziel 60/65 C) abbrechen.
    """
    state = _state(t_oben=62.0, legionellen_aktiv=True)
    erg, setter = await _pruefe(state, 62.0, 35.0, 40.0, 15.0)

    assert erg is True, state.control.blocking_reason
    setter.assert_not_awaited()

    # Oberhalb des Legionellen-Limits wird abgeschaltet.
    state2 = _state(t_oben=66.0, legionellen_aktiv=True)
    erg2, setter2 = await _pruefe(state2, 66.0, 35.0, 40.0, 15.0)
    assert erg2 is False
    setter2.assert_awaited_once()


# ── 5) Die Verdrahtung selbst ──────────────────────────────────────────

def test_check_safety_limits_ruft_die_schutzbausteine_auf():
    """Struktur-Guard: die Schutzfunktionen muessen importiert und used sein.

    Der urspruengliche Bug war ein MISSING CALL - eine vorhandene, korrekt
    getestete Funktion, die niemand aufrief. Dieser Test schuetzt davor,
    dass die Verdrahtung erneut verloren geht.
    """
    import inspect

    quelle = inspect.getsource(pcl.check_safety_limits)
    assert "check_for_sensor_errors" in quelle, "Sensorpruefung nicht verdrahtet!"
    assert "check_verdampfer_schutz" in quelle, "Verdampfer-Schutz nicht verdrahtet!"
    assert "ueberhitz" in quelle.lower(), "Ueberhitzungsschutz nicht verdrahtet!"


def test_safety_logic_exporte_die_bausteine():
    """Die Bausteine muessen importierbar sein (Import-Fehler = toter Schutz)."""
    import safety_logic

    assert hasattr(safety_logic, "check_for_sensor_errors")
    assert hasattr(safety_logic, "check_verdampfer_schutz")
    assert hasattr(safety_logic, "check_sensors_and_safety")


def test_main_ruft_check_safety_limits_auf():
    """Der Hauptloop muss `check_safety_limits` aufrufen.

    `check_sensors_and_safety` wird NICHT mehr direkt aufgerufen - der
    Einstieg ist bewusst `check_safety_limits`, das alle Schutzwirkungen
    buendelt.
    """
    import ast
    import inspect
    import main

    quelle = inspect.getsource(main.run_logic_step)
    assert "check_safety_limits" in quelle, "Hauptloop ohne Sicherheitspruefung!"

    # Strukturpruefung statt Textsuche: ein Kommentar darf den Hinweis
    # enthalten, ein AUFRUF aber nicht (doppelte Pruefung, zwei Schwellen).
    baum = ast.parse(inspect.getsource(main.run_logic_step))
    aufrufe = [
        node.func.attr
        for node in ast.walk(baum)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "check_safety_limits" in aufrufe, "check_safety_limits nicht aufgerufen!"
    assert "check_sensors_and_safety" not in aufrufe, (
        "Doppelter Sicherheitsaufruf - die Schwellen koennten auseinanderlaufen!"
    )


@pytest.mark.asyncio
async def test_verdampfer_ereignis_ist_ein_zeitstempel():
    """Der Verdampfer-Block nutzt die State-Zeit (nicht die Systemzeit).

    Sonst wuerde die Ereignisliste der API bei Tests und im Betrieb
    auseinanderlaufen.
    """
    state = _state(t_verd=1.0)
    await _pruefe(state, 45.0, 35.0, 40.0, 1.0)
    assert len(state.verdampfer_shutdowns) == 1
    assert isinstance(state.verdampfer_shutdowns[0], datetime)
