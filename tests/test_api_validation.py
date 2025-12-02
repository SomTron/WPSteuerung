import pytest
from main import safe_float

def test_safe_float_valid_numbers():
    assert safe_float(100) == 100.0
    assert safe_float(123.45) == 123.45
    assert safe_float(0) == 0.0
    assert safe_float(-50.5) == -50.5

def test_safe_float_valid_strings():
    assert safe_float("100") == 100.0
    assert safe_float("123.45") == 123.45
    assert safe_float("  50  ") == 50.0
    assert safe_float("-10.5") == -10.5

def test_safe_float_invalid_inputs():
    # None
    assert safe_float(None, default=99.9) == 99.9
    
    # Invalid strings
    assert safe_float("N/A", default=0.0) == 0.0
    assert safe_float("error", default=-1.0) == -1.0
    assert safe_float("", default=0.0) == 0.0
    assert safe_float("   ", default=0.0) == 0.0
    assert safe_float("abc", default=0.0) == 0.0
    
    # Wrong types
    assert safe_float([], default=0.0) == 0.0
    assert safe_float({}, default=0.0) == 0.0

def test_safe_float_logging(caplog):
    import logging
    
    # Test warning for None
    with caplog.at_level(logging.WARNING):
        safe_float(None, field_name="test_field")
        assert "API: test_field is None" in caplog.text
        
    # Test warning for invalid string
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        safe_float("N/A", field_name="test_field")
        assert "API: test_field='N/A' invalid" in caplog.text
        
    # Test error for conversion failure
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        safe_float("invalid_num", field_name="test_field")
        assert "API: Cannot convert test_field='invalid_num'" in caplog.text
