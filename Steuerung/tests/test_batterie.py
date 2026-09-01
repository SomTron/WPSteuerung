"""Tests: Batterie-Regel (Design-Anpassung Nutzeranforderungen).

Anforderung: "die Waermepumpe soll so gut wie moeglich mit dem Strom
direkt von der PV laufen, dann von der Batterie und erst ganz zum Schluss
von Netzstrom."

Die PV-Regeln feuern bei echter Netzeinspeisung. Die Batterie-Regel
erlaubt zusaetzlich Heizen, wenn die Batterie genug geladen ist und das
Haus nicht aus dem Netz bezieht (feedinpower >= Toleranz).
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402
import priority_control as pc  # noqa: E402


def baue_config(calcstart_aus=True):
    config = WPSteuerungConfig()
    if calcstart_aus:
        # Determinismus im Integrationstest: CalcStart wuerde sonst um 12 Uhr
        # mit hoher Prioritaet einschalten wollen.
        config.calculated_start.aktiv = False
    return config


def bewerte(temp_dict, soc, feedin, kompressor_ein=False, now_hour=12, config=None):
    config = config or baue_config()
    return pc.evaluate_batterie(
        config.batterie, temp_dict, feedin, soc, kompressor_ein,
        now_hour, config.sicherheit.nachtsperre_start,
        config.sicherheit.nachtsperre_ende,
    )


# ── Einschalten ──

def test_soc_hoch_kein_netzbezug_einschalten():
    erg = bewerte({"unten": 41.0}, soc=95.0, feedin=0.0)
    assert erg.einschalten is True
    assert "SOC" in erg.grund


def test_soc_zu_niedrig_nicht_einschalten():
    erg = bewerte({"unten": 41.0}, soc=80.0, feedin=0.0)
    assert erg.einschalten is None
    assert "Schonung" in erg.grund


def test_netzbezug_blockiert():
    """feedinpower < Toleranz (-50W) = Haus kauft Netzstrom -> nicht heizen."""
    erg = bewerte({"unten": 41.0}, soc=95.0, feedin=-500.0)
    assert erg.einschalten is None
    assert "kein Netzstrom" in erg.grund


def test_kleiner_messfehler_bezug_ist_ok():
    """-30W ist innerhalb der Toleranz (-50W) -> Heizen erlaubt."""
    erg = bewerte({"unten": 41.0}, soc=95.0, feedin=-30.0)
    assert erg.einschalten is True


# ── Hysterese / Ausschalten ──

def test_ausschalten_wenn_warm_genug():
    erg = bewerte({"unten": 47.5}, soc=95.0, feedin=0.0)
    assert erg.einschalten is False


def test_weiterlauf_mit_batteriestrom():
    """Laeuft schon + Batterie traegt -> weiter bis ausschalten_bei_c."""
    erg = bewerte({"unten": 44.0}, soc=92.0, feedin=0.0, kompressor_ein=True)
    assert erg.einschalten is True
    assert "Weiterlauf" in erg.grund


def test_weiterlauf_endet_bei_soc_fall():
    erg = bewerte({"unten": 44.0}, soc=60.0, feedin=-200.0, kompressor_ein=True)
    assert erg.einschalten is None


# ── Nachtsperre ──

def test_nachts_inaktiv():
    erg = bewerte({"unten": 41.0}, soc=95.0, feedin=0.0, now_hour=23)
    assert not erg.aktiv
    erg = bewerte({"unten": 41.0}, soc=95.0, feedin=0.0, now_hour=5)
    assert not erg.aktiv


# ── Integration: Gewinner-Berechnung ──

def test_integration_batterie_gewinnt_wenn_pv_schwelle_nicht_erreicht():
    """PV 200W (< 500W Schwelle), SOC voll, kein Netzbezug -> Batterie-Regel gewinnt."""
    config = baue_config()
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 41.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=200.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, forecast_today_wh_qm=2800.0,
        soc=95.0, battery_power=1500.0,
    )
    namen = {e.name for e in alle}
    assert "Batterie" in namen
    assert gewinner.name == "Batterie"
    assert gewinner.einschalten is True


def test_integration_pv_regel_bleibt_vorrangig():
    """Bei echter Netzeinspeisung (1000W) gewinnt weiterhin die PV-Regel.

    Hinweis: WPSteuerungConfig()-Defaults haben keine PV-Regeln (die kommen
    aus dem JSON) - daher hier eine explizite PV-Regel setzen.
    """
    from json_config import PVRegel
    config = baue_config()
    config.pv_regeln = [
        PVRegel(name="PV_unten", prioritaet=81, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0),
    ]
    temp = {"oben": 43.0, "mittig": 42.0, "unten": 41.0}
    gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp, pv_leistung=1000.0, kompressor_ein=False,
        now=datetime(2026, 1, 15, 12, 0),
        forecast_wh_qm=2500.0, soc=95.0, battery_power=1500.0,
    )
    assert gewinner.prioritaet >= pc.evaluate_batterie(
        config.batterie, temp, 1000.0, 95.0, False, 12, 19, 8
    ).prioritaet
    assert gewinner.name.startswith("PV_")


# ── pcl-Durchreichung ──

@pytest.mark.asyncio
async def test_determine_mode_reicht_soc_durch():
    """determine_mode_and_setpoints uebergibt state.solar.soc an die Regeln."""
    from types import SimpleNamespace
    import pytz
    import priority_control_logic as pcl

    TZ = pytz.timezone("Europe/Berlin")
    config = baue_config()
    state = SimpleNamespace(
        local_tz=TZ,
        priority_config=config,
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=43.0, t_unten=41.0, t_mittig=42.0, t_verd=30.0),
        solar=SimpleNamespace(
            feedinpower=0.0, batpower=1200.0, soc=95.0,
            forecast_today=None, forecast_tomorrow=None,
            forecast_day2=None,
            # frische API-Daten -> solar_stale=False (Stale-Guard)
            last_api_call=TZ.localize(__import__('datetime').datetime(2026, 1, 15, 11, 50)),
        ),
        control=SimpleNamespace(
            kompressor_ein=False,
            previous_modus="Normalmodus",
            aktueller_einschaltpunkt=None,
            aktueller_ausschaltpunkt=None,
            active_rule_name=None,
            active_rule_sensor=None,
            komfort_aktiv=False,
            alle_ergebnisse=[],
            _soll_einschalten=False,
        ),
    )

    from unittest.mock import patch as mock_patch

    with mock_patch('priority_control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(__import__('datetime').datetime(2026, 1, 15, 12, 0))
        result = await pcl.determine_mode_and_setpoints(state, t_unten=41.0, t_mittig=42.0)

    namen = [e.name for e in result["alle_ergebnisse"]]
    batterie = next(e for e in result["alle_ergebnisse"] if e.name == "Batterie")
    assert batterie.aktiv and batterie.einschalten is True
    assert "MinTemp-" in namen[0] or any(n.startswith("MinTemp-") for n in namen)
