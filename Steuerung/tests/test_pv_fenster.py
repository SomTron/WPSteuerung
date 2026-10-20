# -*- coding: utf-8 -*-
"""PV-Fenster: Gate und Messung (Nutzerfrage 18.10.2026).

Betreiber-Frage: "Sollte nicht nach dem Sonnenuntergang geregelt werden,
sondern nach dem PV-Forecast? Wenn der fuer den spaeten Nachmittag wenig
Einstrahlung vorhersagt, ist der Sonnenuntergang irrelevant - in dem waere er
ja automatisch enthalten."

Umgesetzt ueber die STUENDLICHE Prognose (`state.solar.forecast_hourly_wm2`,
kalibriert mit dem gelernten `fc_ratio`):

  * `logic_utils.pv_fenster_endstunde` = letzte Stunde mit erwarteter PV
    ueber der Schwelle,
  * `energy_source.classify_energy_source(pv_erlaubt=False)` = ausserhalb
    zaehlt die PV-Momentanmessung nicht mehr als Quelle (nur Batterie),
  * `evaluate_abweichung`/`evaluate_calculated_start`/`evaluate_forecast`
    bekommen das Fenster durchgereicht; CalcStart zieht seinen Spaetest-Start
    auf das Fensterende vor.
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control as pc  # noqa: E402
from energy_source import Energiequelle, classify_energy_source  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402
from logic_utils import pv_fenster_endstunde  # noqa: E402


# ── (a) Ermittlung des Fensters ─────────────────────────────────────────

def test_letzte_stunde_ueber_der_schwelle():
    profil = {12: 900.0, 14: 800.0, 15: 500.0, 16: 320.0, 17: 120.0, 18: 0.0}
    assert pv_fenster_endstunde(profil, 14.5, 300.0) == (16, pytest.approx(320.0))
    # Stunden in der Vergangenheit zaehlen nicht
    assert pv_fenster_endstunde(profil, 15.1, 300.0) == (16, pytest.approx(320.0))
    # hoehere Schwelle -> frueher zu Ende
    assert pv_fenster_endstunde(profil, 12.0, 600.0) == (14, pytest.approx(800.0))


def test_kalibrierung_wirkt_auf_das_fenster():
    """fc_ratio (gelernter Forecast-Faktor) verschiebt das Fenster mit."""
    profil = {14: 800.0, 15: 700.0, 16: 320.0}
    # unkalibriert: bis 16 Uhr; x0.5 -> 400/350/160 W, nur der 15 Uhr-Wert
    # bleibt ueber 300 W
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=1.0)[0] == 16
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=0.5)[0] == 15
    # unbrauchbare Werte fallen auf 1.0 zurueck
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=0)[0] == 16


def test_ohne_prognose_kein_fenster():
    """Kein Profil = keine Aussage (der Aufrufer laesst das Gate dann aus)."""
    assert pv_fenster_endstunde(None, 12.0, 300.0) is None
    assert pv_fenster_endstunde({}, 12.0, 300.0) is None
    assert pv_fenster_endstunde({12: 100.0, 13: 200.0}, 12.0, 300.0) is None


# ── (b) Quellen-Gate ────────────────────────────────────────────────────

def test_ausserhalb_des_fensters_zaehlt_pv_nicht_als_quelle():
    """Daemmerlicht liefert oft noch 300 W DC - ein 600-W-Lauf zieht dann
    trotzdem Netz. Ausserhalb bleibt nur die Batterie."""
    werte = dict(pv_acpower=320.0, feedin_watt=0.0, batpower_raw=0.0,
                 soc=60.0, pv_min_watt=50.0, max_netzkauf_watt=-50.0)

    drin = classify_energy_source(**werte)
    assert drin.quelle is Energiequelle.PV

    draussen = classify_energy_source(**werte, pv_erlaubt=False)
    assert draussen.quelle is Energiequelle.NETZ
    assert "PV-Fenster" in draussen.begruendung

    # Batterie traegt weiterhin
    mit_batterie = classify_energy_source(
        pv_acpower=320.0, feedin_watt=0.0, batpower_raw=-300.0, soc=95.0,
        pv_min_watt=50.0, max_netzkauf_watt=-50.0, pv_erlaubt=False,
    )
    assert mit_batterie.quelle is Energiequelle.BATTERIE
    assert "PV-Fenster" in mit_batterie.begruendung


# ── (b) Wirkung in den Regeln ───────────────────────────────────────────

_SCHICHTUNG_TEMP = {"unten": 33.0, "mittig": 45.0, "oben": 48.0}


def _allel(config, temp_dict=None, **kw):
    kw.setdefault("pv_leistung", 320.0)
    kw.setdefault("pv_acpower", 320.0)
    kw.setdefault("soc", 60.0)
    kw.setdefault("battery_power", 0.0)
    kw.setdefault("now", datetime(2026, 12, 8, 16, 40))
    _gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp_dict or _SCHICHTUNG_TEMP,
        kompressor_ein=False, **kw
    )
    return {e.name: e for e in alle}


def _config():
    config = WPSteuerungConfig()
    config.abweichung.schichtung_erlaube_start = True
    return config


def test_abweichung_startet_ausserhalb_nicht_mit_pv():
    """Innerhalb des Fensters startet der Schichtungs-Start mit PV, ausserhalb
    nicht mehr (dort nur Batterie)."""
    drin = _allel(_config(), pv_fenster_aktiv=True)["Abweichung"]
    assert drin.einschalten is True, drin.grund

    draussen = _allel(_config(), pv_fenster_aktiv=False)["Abweichung"]
    assert draussen.einschalten is None, draussen.grund

    mit_batterie = _allel(
        _config(), pv_fenster_aktiv=False, battery_power=-300.0, soc=95.0
    )["Abweichung"]
    assert mit_batterie.einschalten is True, mit_batterie.grund


def test_calcstart_startet_am_fensterende_und_nicht_spaeter():
    """Kernfall Winter: Spaetest-Start ohne Fenster erst kurz vor der
    Zielzeit (nach Sonnenuntergang), mit Fenster bis 16:30 - der Lauf bleibt
    im PV-Fenster."""
    config = _config()
    temp = {"unten": 41.0, "mittig": 41.5, "oben": 46.0}   # 1 K Defizit
    gemeinsam = dict(
        temp_dict=temp, forecast_wh_qm=2000.0, learned_target_hour=18.5,
        learned_heating_rate_unten=3.0, learned_heating_rate_gesamt=2.0,
    )

    # 16:35, Puffer ~1,6 h: ohne Fenster -> "Puffer reicht, warte auf PV"
    ohne = _allel(config, pv_fenster_stunde=None, **gemeinsam)["CalcStart"]
    assert ohne.einschalten is None, ohne.grund
    assert "warte" in ohne.grund, ohne.grund

    # Mit Fenster (letzte Stunde 16 Uhr, Reserve 30 min) -> Spaetest-Start
    mit = _allel(config, pv_fenster_stunde=16.0, **gemeinsam)["CalcStart"]
    assert mit.einschalten is True, mit.grund
    assert "SPAETEST-START" in mit.grund, mit.grund

    # Deutlich frueher (12:00) greift die Fenstergrenze nicht
    frueh = _allel(
        config, pv_fenster_stunde=16.0,
        **{**gemeinsam, "now": datetime(2026, 12, 8, 12, 0)}
    )["CalcStart"]
    assert frueh.einschalten is None, frueh.grund


def test_fenster_reserve_ist_konfigurierbar():
    """Defaults der neuen Felder - und abschaltbar ueber die Config."""
    config = WPSteuerungConfig()
    assert config.forecast.pv_fenster_aktiv is True
    assert config.forecast.pv_fenster_min_w == pytest.approx(300.0)
    assert config.forecast.pv_fenster_reserve_min == pytest.approx(30.0)
    config.forecast.pv_fenster_aktiv = False
    assert config.forecast.pv_fenster_aktiv is False


def test_pv_fenster_im_statuslog_und_zaehler():
    """(a) Messung: State-Felder sind vorhanden und werden hochgezaehlt."""
    from state import ControlState

    class _Cfg:
        class Heizungssteuerung:
            AUSSCHALTPUNKT = 48.0
            EINSCHALTPUNKT = 42.0

    control = ControlState(_Cfg)
    assert control.pv_fenster_stunde is None
    assert control.pv_fenster_aktiv is True
    assert control.pv_fenster_ausserhalb is False
    assert control.starts_ausserhalb_pv_fenster == 0

