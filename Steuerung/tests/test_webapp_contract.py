"""Statische Verträge für WebApp-Einheiten, Fehleranzeige und PWA."""
from pathlib import Path


WEBAPP = Path(__file__).resolve().parents[2] / "webapp"


def test_forecast_einheiten_werden_korrekt_angezeigt():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "kWh/m²" in html
    assert "Summe: ${forecastSum.toFixed(0)} Wh/qm" not in html
    assert "Summe: ${forecastSum.toFixed(0)} Wh/m²" in html
    assert "energy.forecast_today" in html
    assert "fmtForecast(energy.forecast_today)" in html
    assert "W/m²" in html


def test_control_api_fehler_werden_nicht_als_erfolg_verschluckt():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "if (!response.ok)" in html
    assert "API-Fehler ' + response.status" in html


def test_service_worker_existiert_und_cachet_keine_daten_endpunkte():
    worker = WEBAPP / "service-worker.js"
    assert worker.exists()
    text = worker.read_text(encoding="utf-8")
    assert "url.pathname === '/status'" in text
    assert "url.pathname.startsWith('/history')" in text
