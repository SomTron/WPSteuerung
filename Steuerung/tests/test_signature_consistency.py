"""Tests for function signature consistency between modules."""
import inspect
from pathlib import Path


def test_handle_compressor_on_signature():
    """Verify handle_compressor_on has the correct parameter count.

    `regel_name` kam am 04.10.2026 hinzu (Befund Regelanalyse): die
    Start-Antizipation brauchte die gewinnende Regel, um
    `pv_min_laufzeit_minuten` anzuwenden - `active_rule_name` ist im
    Aus-Zustand immer None, also war die PV-Mindestlaufzeit dort nie
    wirksam. Der Test muss die Erweiterung mitnehmen, sonst wuerde eine
    kuenftige Aenderung stillschweigend durchfallen.
    """
    from priority_control_logic import handle_compressor_on

    sig = inspect.signature(handle_compressor_on)
    params = list(sig.parameters.keys())

    # Should have exactly 11 parameters including t_mittig and regel_name
    expected_params = [
        'state', 'session', 'regelfuehler', 'einschaltpunkt', 'ausschaltpunkt',
        'min_laufzeit', 'min_pause', 't_oben', 't_mittig',
        'set_kompressor_status_func', 'regel_name'
    ]

    assert len(params) == 11, (
        f"handle_compressor_on should have 11 parameters, has {len(params)}: {params}"
    )
    assert params == expected_params, (
        f"handle_compressor_on parameters mismatch.\n"
        f"Expected: {expected_params}\n"
        f"Got: {params}"
    )
    # regel_name MUSS optional sein: Tests und Aufrufer ohne Regelangabe
    # sollen weiterhin funktionieren.
    assert sig.parameters['regel_name'].default is None


def test_main_passes_correct_args_to_handle_compressor_on():
    """Verify main.py calls handle_compressor_on with t_mittig argument."""
    main_file = Path(__file__).parent.parent / "main.py"
    content = main_file.read_text(encoding="utf-8")
    
    # Check that handle_compressor_on call exists in main.py
    assert 'handle_compressor_on(' in content, "handle_compressor_on call not found in main.py"
    
    # Check that t_mittig is passed to handle_compressor_on
    assert 't_mittig' in content, "t_mittig not found in main.py"


def test_handle_compressor_off_signature():
    """Verify handle_compressor_off has the correct parameter count."""
    from priority_control_logic import handle_compressor_off
    
    sig = inspect.signature(handle_compressor_off)
    params = list(sig.parameters.keys())
    
    # Should have 8 parameters
    assert len(params) == 8, (
        f"handle_compressor_off should have 8 parameters, has {len(params)}: {params}"
    )


def test_determine_mode_and_setpoints_signature():
    """Verify determine_mode_and_setpoints has the correct signature."""
    from priority_control_logic import determine_mode_and_setpoints
    
    sig = inspect.signature(determine_mode_and_setpoints)
    params = list(sig.parameters.keys())
    
    expected = ['state', 't_unten', 't_mittig', 'learning_engine']
    assert params == expected, (
        f"determine_mode_and_setpoints parameters mismatch.\n"
        f"Expected: {expected}\n"
        f"Got: {params}"
    )


def test_telegram_context_ersetzt_lange_parameterlisten():
    """Contract: Telegram nutzt Context-/Runtime-Objekte statt 17-Parameter.

    Vorher hatte `process_telegram_messages_async` 17 und
    `send_status_telegram` 15 Parameter (Befund Task 3). Die Umstellung auf
    `TelegramContext` + `TelegramRuntime` soll verhindern, dass die
    Parameterlisten wieder wachsen.
    """
    from telegram_handler import (
        TelegramContext, TelegramRuntime,
        process_telegram_messages_async, send_status_telegram, telegram_task,
    )

    # Context: genau die Basis-Kopplung (Session, Chat, Token, Config, State,
    # Solar-Daten-Callback).
    ctx_params = list(inspect.signature(TelegramContext).parameters.keys())
    assert ctx_params == [
        'session', 'chat_id', 'bot_token', 'config', 'state',
        'get_solax_data_func',
    ], f"TelegramContext-Felder: {ctx_params}"

    # Runtime: die flüchtigen Messwerte.
    rt_params = list(inspect.signature(TelegramRuntime).parameters.keys())
    assert rt_params == [
        't_oben', 't_unten', 't_mittig', 't_verd',
        'kompressor_status', 'current_runtime', 'total_runtime',
    ], f"TelegramRuntime-Felder: {rt_params}"

    # Die Handler nehmen nur noch (ctx, rt) bzw. (ctx, ...).
    assert list(inspect.signature(send_status_telegram).parameters.keys()) == ['ctx', 'rt']
    assert list(inspect.signature(process_telegram_messages_async).parameters.keys()) == [
        'ctx', 'rt', 'updates', 'last_update_id']
    # telegram_task: ctx + die vier Funktions-Argumente (keine 12 Einzelwerte).
    assert list(inspect.signature(telegram_task).parameters.keys()) == [
        'ctx', 'read_temperature_func', 'sensor_ids',
        'kompressor_status_func', 'current_runtime_func', 'total_runtime_func']
