"""AdaptivePV muss Temperaturgrenzen statt Watt-Schwellen als Setpoints melden."""
from datetime import datetime

import pytest

from json_config import WPSteuerungConfig
import priority_control as pc
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


def test_overshoot_planmaessig_ist_kein_alarm():
    """Befund 18.10.2026: Die Vorhersage schaltet nur, wenn die Regel ihren
    Ausschaltpunkt schon erreicht hat - der Lauf ist erfuellt. Knapp ueber dem
    Limit darf das kein Telegram-Alarm sein (im Log: "unten 48.0C + 46min x
    35C/h -> 74.9C (Limit 65.0C)", real 48,8 C)."""
    from blocking_codes import INFO_BLOCKING_CODES, blocking_code

    assert blocking_code(
        "Overshoot-Vorhersage planmaessig (unten 48.0C + 46min x 35C/h "
        "-> 52.6C, Limit 65.0C) - Ziel erreicht, nur 0.2K darueber"
    ) == "overshoot_planmaessig"
    assert "overshoot_planmaessig" in INFO_BLOCKING_CODES
    # Der echte Eingriff bleibt alarmwuerdig
    assert blocking_code(
        "Overshoot-Vorhersage (unten 48.0C + 46min x 35C/h -> 74.9C >= 65.0C)"
    ) == "overshoot_vorhersage"
    assert "overshoot_vorhersage" not in INFO_BLOCKING_CODES


def test_overshoot_zusatzgrenze_ist_konfigurierbar():
    """Die Grenze zwischen planmaessig und Warnung steht in der Config."""
    config = WPSteuerungConfig()
    assert config.sicherheit.overshoot_warn_zusatz_k > 0.0


def test_adaptivepv_start_nur_unter_der_einschaltgrenze():
    """43,5 C >= 43,0 C -> Hysterese, kein Start. 42,0 C -> EIN (PV)."""
    grenze = _adaptive({"unten": 43.5, "oben": 45.0})
    assert grenze.einschalten is None
    assert "Einschaltgrenze" in grenze.grund

    kalt = _adaptive({"unten": 42.0, "oben": 45.0})
    assert kalt.einschalten is True


def test_adaptivepv_kein_start_wenn_oben_heiss():
    """Der obere Speicherbereich ist voll -> kein PV-Start (Ueberhitzung 58 C).

    Log 12.10.2026: AdaptivePV startete bei unten 43,9 C, waehrend oben
    57,1 C stand - nur 0,9 K vor der Ueberhitzung. Der obere Fuehler muss
    den PV-Start deckeln.

    Seit dem Befund 18.10.2026 liefert der Aus-Zustand `None` ("kein Start")
    statt `False` ("kein Heizen"): Als Gewinner (Prio 78) verdeckte das
    `False` die rangniedrigere Abweichung (47) und verhinderte damit einen
    quellengestuetzten Schichtungs-Start.
    """
    erg = _adaptive({"unten": 30.0, "oben": 56.0})
    assert erg.einschalten is None
    assert "Speicher oben voll" in erg.grund
    assert "kein Start" in erg.grund


def test_adaptivepv_oben_heiss_im_laufenden_zyklus_schaltet_ab():
    """Laeuft der Kompressor, bleibt der Deckel ein AUS (Ueberhitzungs-Bremse
    des laufenden PV-Zyklus)."""
    erg = _adaptive({"unten": 30.0, "oben": 56.0}, kompressor_ein=True)
    assert erg.einschalten is False
    assert "-> AUS" in erg.grund


def test_oben_deckel_verdeckt_schichtungs_start_nicht_mehr():
    """Befund 18.10.2026, 18:29:54 (Betriebslog): oben 61,6 C nach der
    Legionellenprophylaxe, unten 37,0 C nach Zapfung, SOC 96 %, Batterie
    263 W. AdaptivePV (78) meldete "Speicher oben voll" mit `False` und
    verdeckte die Abweichung (47) - 35 min keine Heizung, unten fiel bis
    22,2 C. Mit `None` gewinnt die Abweichung den Schichtungs-Start.
    """
    config = WPSteuerungConfig()
    # Deployment-Schalter aus dem Log 18:29:54 spiegeln
    config.legionellen.aktiv = False     # "Bereits in KW 42 durchgefuehrt"
    config.forecast.aktiv = False        # "Forecast 3196 Wh/qm -> keine Aktion"
    config.batterie.aktiv = False        # "[75] --- Batterie [INAKTIV]"
    config.komfort.aktiv = False         # "[60] --- Komfort [INAKTIV]"
    config.zeitfenster.aktiv = False     # "[53] --- Zeitfenster [INAKTIV]"
    config.abweichung.schichtung_erlaube_start = True      # Deployment
    config.abweichung.einschalten_bei_abweichung_k = 4.9   # Deployment
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config,
        temp_dict={"unten": 37.0, "mittig": 61.3, "oben": 61.6, "verd": 19.8},
        pv_leistung=0.0,
        kompressor_ein=False,
        now=datetime(2026, 10, 18, 18, 29),
        forecast_wh_qm=3196.0,
        forecast_today_wh_qm=3196.0,
        soc=96.0,
        battery_power=-263.0,
    )
    adaptive = next(e for e in alle if e.name == "AdaptivePV")
    assert adaptive.einschalten is None, adaptive.grund
    assert "Speicher oben voll" in adaptive.grund
    assert gewinner is not None, "kein Gewinner - Abweichung wurde verdeckt"
    assert gewinner.name == "Abweichung", (
        f"{gewinner.name} gewinnt statt der Abweichung: {gewinner.grund}"
    )
    assert gewinner.einschalten is True
    assert "Schichtungs-Start" in gewinner.grund

