"""Tests for function signature consistency between modules."""
import inspect
import re
from pathlib import Path


def test_handle_compressor_on_signature():
    """Verify handle_compressor_on has the correct parameter count."""
    from priority_control_logic import handle_compressor_on
    
    sig = inspect.signature(handle_compressor_on)
    params = list(sig.parameters.keys())
    
    # Should have exactly 10 parameters including t_mittig
    expected_params = [
        'state', 'session', 'regelfuehler', 'einschaltpunkt', 'ausschaltpunkt',
        'min_laufzeit', 'min_pause', 't_oben', 't_mittig', 'set_kompressor_status_func'
    ]
    
    assert len(params) == 10, (
        f"handle_compressor_on should have 10 parameters, has {len(params)}: {params}"
    )
    assert params == expected_params, (
        f"handle_compressor_on parameters mismatch.\n"
        f"Expected: {expected_params}\n"
        f"Got: {params}"
    )


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
