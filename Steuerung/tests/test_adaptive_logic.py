import pytest
from datetime import datetime, time
import pytz
from unittest.mock import MagicMock
from adaptive_logic import classify_pv_value, get_pv_strategy, estimate_heating_runtime, get_heating_deadline

def test_classify_pv_value():
    assert classify_pv_value(2.0, 5.0, 10.0) == "low"
    assert classify_pv_value(7.0, 5.0, 10.0) == "mid"
    assert classify_pv_value(12.0, 5.0, 10.0) == "high"

def test_get_pv_strategy():
    state = MagicMock()
    state.solar.pv_threshold_low_kwh = 5.0
    state.solar.pv_threshold_high_kwh = 10.0
    
    # Case 1: Today High, Tomorrow Low -> Aggressive
    state.solar.forecast_today = 15.0
    state.solar.forecast_tomorrow = 3.0
    assert get_pv_strategy(state) == "aggressive"
    
    # Case 2: Today High, Tomorrow High -> Balanced
    state.solar.forecast_tomorrow = 15.0
    assert get_pv_strategy(state) == "balanced"
    
    # Case 3: Today Low, Tomorrow High -> Conservative
    state.solar.forecast_today = 3.0
    assert get_pv_strategy(state) == "conservative"
    
    # Case 4: Today Low, Tomorrow Low -> Cautious
    state.solar.forecast_tomorrow = 3.0
    assert get_pv_strategy(state) == "cautious"

def test_estimate_heating_runtime():
    assert estimate_heating_runtime(40.0, 50.0, 2.0) == 5.0
    assert estimate_heating_runtime(50.0, 40.0, 2.0) == 0.0

def test_get_heating_deadline():
    state = MagicMock()
    state.local_tz = pytz.timezone("Europe/Berlin")
    state.config.Heizungssteuerung.NACHTABSENKUNG_START = "18:00"
    state.sensors.t_mittig = 40.0
    state.heating_rate = 2.0
    state.control.learned_heating_rate = 2.0
    
    # 52°C target, 40°C ist -> 12°C delta -> 6 hours runtime
    # Window end 18:00 -> Deadline 12:00
    deadline = get_heating_deadline(state, 52.0)
    assert deadline.hour == 12
    assert deadline.minute == 0
