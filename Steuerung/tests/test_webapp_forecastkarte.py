"""Webapp-Vertrag fuer die Forecast-Kalibrierungskarte."""
from pathlib import Path


WEBAPP = Path(__file__).resolve().parents[2] / "webapp" / "index.html"


def test_forecastkarte_hat_die_angezeigten_felder():
    html = WEBAPP.read_text(encoding="utf-8")
    assert 'id="calib-factor"' in html
    assert 'id="calib-samples"' in html
    assert 'id="calib-status"' in html


def test_forecastkarte_wird_beim_status_update_befuellt():
    html = WEBAPP.read_text(encoding="utf-8")
    assert "const calibFactorEl = document.getElementById('calib-factor')" in html
    assert "const calibSamplesEl = document.getElementById('calib-samples')" in html
    assert "lrn.forecast_ratio_samples" in html
    assert "lrn.forecast_ratio" in html
    assert "renderApp();\n                    // Nach dem ersten Rendering" in html
