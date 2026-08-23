"""Guards: Legacy-System ist archiviert (Design-Fix #4/#7).

Die modusbasierte Logik (determine_mode_and_setpoints, handle_compressor_*)
wurde von der Pareto-Prioritaeten-Engine ersetzt. control_logic ist jetzt
nur noch eine schlanke Fassade mit den 4 live genutzten Re-Exports.
Die naive-Zeit-Falle is_nighttime ist aus der Produktion entfernt
(Produktion nutzt tz-aware pcl._is_nachtsperre_aktiv).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import control_logic  # noqa: E402
import logic_utils  # noqa: E402


def test_fassade_exportiert_live_funktionen():
    assert hasattr(control_logic, 'verify_compressor_running')
    assert hasattr(control_logic, 'check_sensors_and_safety')
    assert hasattr(control_logic, 'is_solar_window')
    assert hasattr(control_logic, 'send_telegram_message')


def test_legacy_modus_logik_weg():
    for alter_name in (
        'determine_mode_and_setpoints',
        'handle_compressor_on',
        'handle_compressor_off',
        'handle_mode_switch',
        'check_pressure_and_config',
        'set_last_compressor_off_time',  # pcl hat die eigene Kopie
    ):
        assert not hasattr(control_logic, alter_name), alter_name


def test_naive_nachtzeit_falle_entfernt():
    """#7: logic_utils.is_nighttime (datetime.now() ohne TZ) ist geloescht.

    Produktion nutzt stattdessen priority_control_logic._is_nachtsperre_aktiv,
    die mit state.local_tz DST-sicher rechnet.
    """
    assert not hasattr(logic_utils, 'is_nighttime')


def test_archiv_kopie_vollstaendig():
    """Die alte Implementierung liegt vollstaendig im Archiv."""
    archiv_pfad = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'archive', 'legacy_control_logic.py',
    )
    assert os.path.exists(archiv_pfad)
    with open(archiv_pfad, encoding='utf-8') as f:
        quelle = f.read()
    for symbol in (
        'def determine_mode_and_setpoints',
        'def handle_compressor_on',
        'def handle_mode_switch',
    ):
        assert symbol in quelle, symbol
