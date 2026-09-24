"""AdaptivePV muss Temperaturgrenzen statt Watt-Schwellen als Setpoints melden."""
import pytest

from json_config import WPSteuerungConfig
from priority_control import RegelErgebnis
from priority_control_logic import _extract_einschaltpunkt


def test_adaptivepv_einschaltpunkt_ist_temperaturgrenze():
    config = WPSteuerungConfig()
    ergebnis = RegelErgebnis(
        name="AdaptivePV",
        prioritaet=config.adaptive_pv.prioritaet,
        aktiv=True,
        einschalten=True,
    )

    einschaltpunkt = _extract_einschaltpunkt(ergebnis, config)

    assert einschaltpunkt == pytest.approx(config.adaptive_pv.tmax_c - 3.0)
    assert einschaltpunkt != pytest.approx(config.adaptive_pv.base_threshold_watt)
