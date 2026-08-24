"""Regression: API-Routen muessen an die richtigen Funktionen gebunden sein.

Hintergrund (Bug c8ab18c): Der Helper _solar_stale_status wurde zwischen den
@app.get("/status")-Dekorator und get_status() eingefuegt. Dadurch bediente der
Helper die Route /status und lieferte nur true/false - das Frontend crashte beim
Destrukturieren und blieb fuer immer bei "Lade Daten..." haengen.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402


def _endpunkte_fuer(pfad):
    """Liefert alle Route-Endpoints fuer einen Pfad."""
    return [
        getattr(route, "endpoint", None)
        for route in api.app.routes
        if getattr(route, "path", None) == pfad
    ]


def test_status_route_ist_get_status():
    """GET /status muss von get_status bedient werden, nicht vom Stale-Helper."""
    endpoints = _endpunkte_fuer("/status")
    assert endpoints, "Route /status fehlt komplett"
    namen = [getattr(e, "__name__", "?") for e in endpoints]
    assert namen == ["get_status"], (
        f"/status ist an {namen} gebunden - erwartet get_status. "
        "Vermutlich wurde eine Hilfsfunktion zwischen Dekorator und "
        "get_status() verschoben!"
    )


def test_stale_helper_hat_keine_eigene_route():
    """Der Helper _solar_stale_status darf keinen Pfad bedienen."""
    for route in api.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if getattr(endpoint, "__name__", "") == "_solar_stale_status":
            raise AssertionError(
                f"_solar_stale_status haengt an Route {getattr(route, 'path', '?')} - "
                "der Helper muss NUR intern aufgerufen werden!"
            )


def test_wichtige_routen_vorhanden():
    """Smoke-Check: Alle vom Webapp benoetigten Routen existieren."""
    pfade = {"/", "/index.html", "/status", "/history", "/history/regeln",
             "/config", "/control"}
    vorhandene = {getattr(route, "path", "") for route in api.app.routes}
    for p in pfade:
        assert p in vorhandene, f"Route {p} fehlt"


def test_status_liefert_dict_keine_primitiven():
    """get_status() muss ein dict liefern (Frontend destrukturiert Felder)."""
    from types import SimpleNamespace

    import pytz
    from json_config import WPSteuerungConfigManager

    fake = SimpleNamespace(
        priority_config=WPSteuerungConfigManager().config,
        local_tz=pytz.timezone("Europe/Berlin"),
        solar=SimpleNamespace(last_api_call=None, batpower=0.0,
                              feedinpower=0.0, soc=50.0),
        sensors=SimpleNamespace(t_oben=42.0, t_mittig=41.0, t_unten=40.0,
                                t_verd=-2.0, t_boiler=41.0),
        compressor=SimpleNamespace(status="AUS", laeuft=False,
                                   runtime_today="0m", runtime_current="0m",
                                   start_time=None),
        mode=SimpleNamespace(current="Normal", active_rule="Keine",
                             active_rule_sensor="", blocking_reason=None,
                             soll_einschalten=False, bath_active=False,
                             holiday_active=False, solar_active=False,
                             nightsperre_active=False, sommer_modus_aktiv=False,
                             sommer_modus_offset_c=0.0, sommer_modus_tage_ueber=0,
                             sommer_modus_benoetigte=3),
        energy=SimpleNamespace(soc=50.0, battery_power=0.0, feed_in=0.0,
                               ac_power=0.0, forecast_today=None,
                               forecast_tomorrow=None, forecast_day2=None,
                               sunrise=None, sunset=None),
        system=SimpleNamespace(last_update="-", exclusion_reason=None),
        setpoints=SimpleNamespace(einschaltpunkt=40.0, ausschaltpunkt=38.0),
        regel_ergebnisse=[],
        stats=SimpleNamespace(current_runtime="0m", total_runtime_today="0m"),
        control=SimpleNamespace(alle_ergebnisse=[], kompressor_ein=False,
                                aktueller_einschaltpunkt=40.0,
                                aktueller_ausschaltpunkt=38.0,
                                ausschluss_grund=None),
        learning_engine=None,
        sicherheits_temp=55.0,
        verdampfertemperatur=-5.0,
    )
    original = api.shared_state
    try:
        api.shared_state = fake
        d = api.get_status()
    finally:
        api.shared_state = original

    assert isinstance(d, dict), f"get_status liefert {type(d).__name__}, kein dict"
    for schluessel in ("temperatures", "compressor", "mode", "energy",
                       "system", "setpoints", "priority"):
        assert schluessel in d, f"Key {schluessel} fehlt in /status"
