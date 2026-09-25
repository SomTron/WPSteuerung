"""Regression: API-Routen muessen an die richtigen Funktionen gebunden sein.

Hintergrund (Bug c8ab18c): Der Helper _solar_stale_status wurde zwischen den
@app.get("/status")-Dekorator und get_status() eingefuegt. Dadurch bediente der
Helper die Route /status und lieferte nur true/false - das Frontend crashte beim
Destrukturieren und blieb fuer immer bei "Lade Daten..." haengen.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest  # noqa: E402

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


def test_stale_helper_behandelt_fehler_fail_safe():
    """Ein kaputter Zeitstempel darf Solardaten nicht als frisch ausgeben."""
    from types import SimpleNamespace

    original = api.shared_state
    try:
        api.shared_state = SimpleNamespace(
            solar=SimpleNamespace(last_api_call=object())
        )
        assert api._solar_stale_status() is True
    finally:
        api.shared_state = original


def test_wichtige_routen_vorhanden():
    """Smoke-Check: Alle vom Webapp benoetigten Routen existieren."""
    pfade = {"/", "/index.html", "/status", "/history", "/history/regeln",
             "/config", "/control"}
    vorhandene = {getattr(route, "path", "") for route in api.app.routes}
    for p in pfade:
        assert p in vorhandene, f"Route {p} fehlt"


def test_status_route_enthaelt_api_contract():
    """Statusantwort signalisiert ihre Version explizit."""
    assert api.API_CONTRACT_VERSION == "1.0.0"
    assert api.app.version == api.API_CONTRACT_VERSION


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


def test_analysis_quality_route_liest_redigierten_bericht(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import json

    bericht = {
        "status": "ok",
        "git_revision": "abc123",
        "summary": {"cycles": 3, "decisions": 4, "invalid_rows": 0},
        "scenarios": {"pv": 3},
    }
    pfad = tmp_path / "quality_report.json"
    pfad.write_text(json.dumps(bericht), encoding="utf-8")
    monkeypatch.setenv("WPS_ANALYSIS_QUALITY_FILE", str(pfad))
    assert api.get_analysis_quality() == bericht

    pfad.write_text("{invalid", encoding="utf-8")
    with pytest.raises(HTTPException) as exc:
        api.get_analysis_quality()
    assert exc.value.status_code == 503


def test_analysis_quality_route_404_wenn_bericht_fehlt(monkeypatch, tmp_path):
    from fastapi import HTTPException

    monkeypatch.setenv("WPS_ANALYSIS_QUALITY_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(HTTPException) as exc:
        api.get_analysis_quality()
    assert exc.value.status_code == 404


    from fastapi import HTTPException

    monkeypatch.setattr(api, "API_KEY", "test-secret")
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(tmp_path / "heizungsdaten.csv"))
    (tmp_path / "heizungsdaten.csv").write_text(
        "Zeitstempel,T_Oben\n2026-09-24 10:00:00,42\n", encoding="utf-8"
    )
    with pytest.raises(HTTPException) as exc:
        api.debug_csv("wrong")
    assert exc.value.status_code == 401

    result = api.debug_csv("test-secret")
    assert not os.path.isabs(result["csv_path"])
    assert str(tmp_path) not in result["csv_path"]
    assert result["read_error"] if "read_error" in result else True

    def kaputt_open(*args, **kwargs):
        raise OSError("interner /home/patrik/Geheimnis/Pfad")

    monkeypatch.setattr("builtins.open", kaputt_open)
    result = api.debug_csv("test-secret")
    assert result["read_error"] == "CSV konnte nicht gelesen werden"
    assert "Geheimnis" not in str(result)


def test_keine_doppelt_registrierten_routen():
    """Regression: /debug/csv war zweimal definiert - die erste Definition
    (Platzhalter mit 'pass') gewann das Routing und lieferte 'null', der echte
    Endpoint war toter Code."""
    from collections import Counter

    pfade = [getattr(route, "path", None) for route in api.app.routes]
    doppelt = {p: c for p, c in Counter(pfade).items() if p and c > 1}
    assert not doppelt, f"Doppelt registrierte Routen: {doppelt}"


def test_api_live_routen_haben_no_store_middleware():
    source = __import__("pathlib").Path(api.__file__).read_text(encoding="utf-8-sig")
    assert "def add_live_no_store_headers" in source
    assert '"Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"' in source
    assert '"/health"' in source


def test_api_cors_ist_nicht_wildcard_und_ist_konfigurierbar():
    from pathlib import Path

    source = Path(api.__file__).read_text(encoding="utf-8-sig")
    assert 'allow_origins=["*"]' not in source
    assert "WPS_CORS_ORIGINS" in source
    assert "X-API-Key" in source


def test_api_key_ist_fuer_schreibzugriffe_erforderlich():
    """Ohne konfigurierten Key werden Schreib-/Exportbefehle blockiert."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api._check_api_key(None)
    assert exc.value.status_code == 503


def test_api_key_wird_falsch_oder_richtig_geprueft(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(api, "API_KEY", "test-secret")
    with pytest.raises(HTTPException) as exc:
        api._check_api_key("wrong")
    assert exc.value.status_code == 401
    api._check_api_key("test-secret")


@pytest.mark.asyncio
async def test_control_set_mode_schaltet_und_validiert_aktiv(monkeypatch):
    from types import SimpleNamespace

    from pydantic import ValidationError

    original_state, original_key = api.shared_state, api.API_KEY
    try:
        api.API_KEY = "test-secret"
        api.shared_state = SimpleNamespace(bademodus_aktiv=False, urlaubsmodus_aktiv=False)
        result = await api.control_system(
            api.ControlCommand(command="set_mode", params={"mode": "bademodus", "active": True}),
            "test-secret",
        )
        assert result["status"] == "success"
        assert api.shared_state.bademodus_aktiv is True
        with pytest.raises(ValidationError):
            api.ControlCommand(command="set_mode", params={"mode": "bademodus", "active": "true"})
    finally:
        api.shared_state, api.API_KEY = original_state, original_key


def test_regelhistorie_wird_chronologisch_sortiert():
    from types import SimpleNamespace

    original_log = api.entscheidungs_log
    try:
        api.entscheidungs_log = SimpleNamespace(historie=lambda **_: [
            {"ts": "2026-09-01T12:02:00", "gewinner": "Neu", "kompressor_laeuft": True},
            {"ts": "2026-09-01T12:01:00", "gewinner": "Alt", "kompressor_laeuft": False},
        ])
        data = api.get_history_regeln(hours=24, limit=10)
    finally:
        api.entscheidungs_log = original_log
    assert [x["regel"] for x in data["data"]] == ["Alt", "Neu"]


def test_config_export_redigiert_secrets(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(api, "API_KEY", "test-secret")
    original_state = api.shared_state
    try:
        api.shared_state = SimpleNamespace(
            config=SimpleNamespace(model_dump=lambda: {
                "Telegram": {"BOT_TOKEN": "secret-token", "CHAT_ID": "123"},
                "SolaxCloud": {"TOKEN_ID": "secret-solax", "SN": "serial"},
            }),
            priority_config=SimpleNamespace(model_dump=lambda: {"wp": {"leistung_watt": 600}}),
        )
        data = api.export_config("test-secret")
    finally:
        api.shared_state = original_state

    assert data["config_ini"]["Telegram"]["BOT_TOKEN"] == "***"
    assert data["config_ini"]["SolaxCloud"]["TOKEN_ID"] == "***"
    assert data["config_ini"]["SolaxCloud"]["SN"] == "***"
    assert "secret-token" not in str(data)


@pytest.mark.asyncio
async def test_control_befehle_werden_via_queue_angenommen():
    from types import SimpleNamespace

    original_state, original_funcs, original_key = api.shared_state, api.control_funcs, api.API_KEY
    commands = []
    try:
        api.API_KEY = "test-secret"
        api.shared_state = SimpleNamespace()
        api.control_funcs = {"enqueue_control": lambda command, params: commands.append((command, params))}
        result = await api.control_system(
            api.ControlCommand(command="force_on"), "test-secret"
        )
        assert result["status"] == "queued"
        assert commands == [("force_on", {})]
    finally:
        api.shared_state, api.control_funcs, api.API_KEY = original_state, original_funcs, original_key


@pytest.mark.asyncio
async def test_control_ohne_set_kompressor_liefert_503():
    """Fehlt die Steuerfunktion, muss /control einen Fehler liefern statt
    still mit HTTP 200 und 'null' zu antworten."""
    from fastapi import HTTPException

    original_state, original_funcs = api.shared_state, api.control_funcs
    try:
        api.shared_state = object()
        api.control_funcs = {"etwas_anderes": None}  # truthy, aber ohne set_kompressor
        with pytest.raises(HTTPException) as exc:
            await api.control_system(api.ControlCommand(command="force_on"))
        assert exc.value.status_code == 503
    finally:
        api.shared_state, api.control_funcs = original_state, original_funcs
