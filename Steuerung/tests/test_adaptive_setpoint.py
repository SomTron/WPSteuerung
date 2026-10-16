"""AdaptivePV muss Temperaturgrenzen statt Watt-Schwellen als Setpoints melden."""
import pytest

from json_config import WPSteuerungConfig
from priority_control import RegelErgebnis, evaluate_adaptive_pv
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

    # Seit Oktober 2026 explizit konfiguriert (Default 43 C) statt implizit
    # `tmax_c - 3` (45 C). Grund: Starts bei 43,4-43,9 C rissen nach 9-14 min
    # am Boiler-Maximum (48 C) ab.
    assert einschaltpunkt == pytest.approx(config.adaptive_pv.einschalten_bis_c)
    assert einschaltpunkt == pytest.approx(43.0)
    assert einschaltpunkt != pytest.approx(config.adaptive_pv.base_threshold_watt)


def _adaptive(temp, **kw):
    config = WPSteuerungConfig()
    basis = dict(
        adaptive_cfg=config.adaptive_pv,
        temp_dict=temp,
        pv_leistung=1000.0,
        forecast_wh_qm=2000.0,
        kompressor_ein=False,
        now_hour=12,
        nachtsperre_start=config.sicherheit.nachtsperre_start,
        nachtsperre_ende=config.sicherheit.nachtsperre_ende,
        pv_acpower=2000.0,
        battery_power=0.0,
        soc=99.0,
        forecast_today_wh_qm=2000.0,
    )
    basis.update(kw)
    return evaluate_adaptive_pv(**basis)


def test_adaptivepv_start_nur_unter_der_einschaltgrenze():
    """43,5 C >= 43,0 C -> Hysterese, kein Start. 42,0 C -> EIN (PV)."""
    grenze = _adaptive({"unten": 43.5, "oben": 45.0})
    assert grenze.einschalten is None
    assert "Einschaltgrenze" in grenze.grund

    kalt = _adaptive({"unten": 42.0, "oben": 45.0})
    assert kalt.einschalten is True


def test_adaptivepv_kein_start_wenn_oben_heiss():
    """Der obere Speicherbereich ist voll -> Schutz vor Ueberhitzung (58 C).

    Log 12.10.2026: AdaptivePV startete bei unten 43,9 C, waehrend oben
    57,1 C stand - nur 0,9 K vor der Ueberhitzung. Der obere Fuehler muss
    den PV-Start deckeln.
    """
    erg = _adaptive({"unten": 30.0, "oben": 56.0})
    assert erg.einschalten is False
    assert "Speicher oben voll" in erg.grund

