import pytest
import importlib

def test_module_imports():
    """Versucht alle wichtigen Module zu importieren, um Syntaxfehler früh zu finden."""
    modules = [
        "config_manager",
        "state",
        "sensors",
        "hardware",
        "hardware_mock",
        "logging_config",
        "solax",
        "control_logic",
        "telegram_handler",
        "telegram_ui",
        "telegram_api",
        "telegram_charts",
        "vpn_manager",
        "api",
        "utils",
        "weather_forecast",
        "logic_utils"
    ]
    
    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except Exception as e:
            pytest.fail(f"Fehler beim Importieren von {module_name}: {e}")
