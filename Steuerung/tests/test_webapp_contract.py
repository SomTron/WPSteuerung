"""Statische Verträge für WebApp-Einheiten, Fehleranzeige und PWA."""
from pathlib import Path


WEBAPP = Path(__file__).resolve().parents[2] / "webapp"
NGINX = Path(__file__).resolve().parents[1] / "Nginx-Konfiguration"


def test_forecast_einheiten_werden_korrekt_angezeigt():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "kWh/m²" in html
    assert "Summe: ${forecastSum.toFixed(0)} Wh/qm" not in html
    assert "Summe: ${forecastSum.toFixed(0)} Wh/m²" in html
    assert "energy.forecast_today" in html
    assert "fmtForecast(energy.forecast_today)" in html
    assert "W/m²" in html
    assert "pvp.peak_wm2" in html
    assert "W/m²" in html


def test_control_api_fehler_werden_nicht_als_erfolg_verschluckt():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "if (!response.ok)" in html
    assert "API-Fehler ' + response.status" in html


def test_webapp_sortiert_regelhistorie_und_sendet_api_key():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "regelnDaten = (jd.data || []).sort" in html
    assert "function apiHeaders(extra = {})" in html
    assert "X-API-Key" in html
    assert "headers: apiHeaders({ 'Content-Type': 'application/json' })" in html


def test_mobile_layout_verhindert_globales_horizontales_scrolling():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "@media (max-width: 600px)" in html
    assert "grid-template-columns: minmax(0, 1fr)" in html
    assert "overflow-wrap: anywhere" in html
    assert "min-width: 0" in html
    assert ".chart-wrapper.horizontal-mode" in html


def test_quick_status_und_skeleton_sind_vorhanden():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'class="quick-status"' in html
    assert 'id="quick-compressor"' in html
    assert 'id="quick-rule"' in html
    assert 'class="skeleton wide"' in html
    assert "Betriebsdaten werden geladen" in html


def test_debug_texte_sind_nicht_mehr_sichtbar_und_leerwerte_einheitlich():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    for debug_text in ("Script loaded!", "API Request...", "Parsing JSON...", "renderApp() called...", "updateValues() called..."):
        assert debug_text not in html
    assert "const NO_DATA = '—'" in html
    assert "errorPanel.style.display = 'none'" in html


def test_touch_ziele_und_chart_hinweis_sind_vorhanden():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "min-height: 44px" in html
    assert "min-width: 44px" in html
    assert "chart-scroll-hint" in html
    assert "Chart horizontal scrollen" in html


def test_status_polling_hat_timeout_und_verhindert_ueberlappung():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "const STATUS_TIMEOUT_MS = 8000" in html
    assert "if (statusRequestActive) return" in html
    assert "if (analysisRequestActive) return" in html
    assert "analysisRequestActive = false" in html
    assert "new AbortController()" in html
    assert "cache: 'no-store'" in html
    assert "API_CACHE_BUST" not in html


def test_live_api_und_service_worker_cache_vertraege():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    worker = (WEBAPP / "service-worker.js").read_text(encoding="utf-8")
    assert "cache: 'no-store'" in html
    assert "const CACHE_NAME = 'wp-webapp-shell-v3'" in worker
    assert "fetch(event.request, { cache: 'no-store' })" in worker
    assert "caches.match(event.request)" in worker
    assert "url.pathname === '/status'" in worker
    assert "url.pathname.startsWith('/history')" in worker
    assert "url.pathname === '/health'" in worker
    assert "url.pathname === '/command'" in worker
    assert "url.pathname.startsWith('/debug/')" in worker


def test_webapp_zeigt_health_und_queue_und_offline_status():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'id="header-health"' in html
    assert 'id="control-feedback"' in html
    assert "fetchHealth" in html
    assert "/health?ts=" in html
    assert "Steuerung eingeschränkt" in html
    assert "Befehl eingereiht" in html
    assert "showOfflineState" in html
    assert "visibilitychange" in html
    assert "navigator.onLine" in html
    assert "window.addEventListener('online'" in html
    assert "planned_start_hour" in html
    assert "spätestens:" in html
    assert "formatCalcHour" in html


def test_history_quality_und_api_fehlertext_sichtbar():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "responseError" in html
    assert "quality.partial" in html
    assert "quality.returned_hours" in html
    assert "quality.sampling_seconds" in html
    assert "quality.stale_end_s" in html


def test_analyse_qualitaetskarte_und_abruf_sind_im_webapp_und_service_worker():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    worker = (WEBAPP / "service-worker.js").read_text(encoding="utf-8")
    assert 'id="analysis-quality-card"' in html
    assert 'id="analysis-quality-status"' in html
    assert 'id="analysis-quality-summary"' in html
    assert "fetchAnalysis" in html
    assert "/analysis/quality?ts=" in html
    assert "function startAnalysisPolling" in html
    assert "analysisPollTimer" in html
    assert "startAnalysisPolling();" in html
    assert "url.pathname.startsWith('/analysis/')" in worker


    nginx = NGINX.read_text(encoding="utf-8")
    assert "location /api/" not in nginx
    assert "location / {" in nginx
    assert "proxy_pass http://127.0.0.1:8000;" in nginx
    assert "proxy_set_header X-API-Key $http_x_api_key;" in nginx
    assert "proxy_connect_timeout 5s;" in nginx
    assert "proxy_read_timeout 30s;" in nginx


    worker = WEBAPP / "service-worker.js"
    assert worker.exists()
    text = worker.read_text(encoding="utf-8")
    assert "url.pathname === '/status'" in text
    assert "url.pathname.startsWith('/history')" in text


# ------------------------------------------------------------- Fehlerhistorie


def test_webapp_hat_letzten_fehler_mit_zeitstempel():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'id="card-last-error"' in html
    assert 'id="last-error-text"' in html
    assert 'id="last-error-time"' in html
    assert 'id="last-error-level"' in html
    assert 'id="error-count-24h"' in html


def test_webapp_hat_knopf_fuer_die_fehlerhistorie():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'id="btn-error-history"' in html
    assert 'id="card-error-history"' in html
    assert "toggleErrorHistory" in html
    # Der Knopf muss auch wirklich verdrahtet sein.
    assert "btnHisto.addEventListener" in html


def test_webapp_rendert_fehlerhistorie_mit_zeitstempel():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "renderErrorHistory" in html
    assert "formatErrorTime" in html
    # Zeitstempel je Zeile, nicht nur als Ueberschrift.
    assert "formatErrorTime(e.timestamp)" in html


def test_webapp_fragt_die_fehler_api_ab():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "/errors?limit=" in html
    assert "fetchErrors" in html


def test_webapp_benutzt_textcontent_fuer_fehlertexte():
    """Logeintrae duerfen kein HTML einschleusen (XSS)."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "text.textContent = e.message" in html


def test_webapp_belastet_das_status_polling_nicht():
    """Die Fehlerhistorie wird separat, nicht alle 5 s, abgerufen."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "const ERROR_INTERVAL_MS = 60000;" in html
