"""Statische Verträge für WebApp-Einheiten, Fehleranzeige und PWA."""
from pathlib import Path
import json
import re


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


def test_webapp_zeigt_override_hinweis_im_regel_tooltip():
    """A2: Der Chart-Tooltip nennt den Override, wenn die Handlung von der
    Regel-Empfehlung abwich (Mindestlaufzeit, Debounce, Sicherheit)."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "regelOverride: []" in html
    assert "handlung_override" in html
    assert "tooltipOverride: temperatureHistory.regelOverride" in html


def test_webapp_zeigt_datenalter_der_solardaten():
    """B4b: Das Alter der Solax-Livedaten wird sichtbar (5-min-Granularitaet)."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'id="energy-age"' in html
    assert "setText('energy-age', formatAge(energy.solar_age_s))" in html


def test_webapp_nutzt_die_sperr_einstufung_der_api():
    """Normalzustaende (Boiler heiss, planmaessiger Boiler-Max-Abschluss samt
    Kuehlphase) duerfen in der WebApp nicht als Warnung erscheinen. Die
    Einstufung liefert die API als `blocking_kind`; der hartcodierte
    Code-Vergleich bleibt nur als Rueckfall.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "mode.blocking_kind" in html
    assert "bkKind === 'info' ? 'badge badge-info' : 'badge badge-warning'" in html


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
    # Befund 24.10.2026: 8 s lagen unter der realen /status-Dauer auf dem Pi
    # (Warnschwelle der API 3 s, gemessen 3,7 s) - die WebApp brach ab,
    # obwohl die Werte kurz darauf bereitstanden. 12 s puffern Spitzen.
    assert "const STATUS_TIMEOUT_MS = 12000" in html
    assert "if (statusRequestActive) return" in html
    assert "if (analysisRequestActive) return" in html
    assert "analysisRequestActive = false" in html
    assert "new AbortController()" in html
    assert "cache: 'no-store'" in html
    assert "API_CACHE_BUST" not in html


def test_server_meldet_dieselbe_webapp_grenze():
    """Die API-Warnung nennt exakt den Timeout, den die WebApp nutzt.

    Ohne diesen Abgleich driftet `WEBAPP_STATUS_TIMEOUT_SEC` (Logtext) vom
    `STATUS_TIMEOUT_MS` in index.html weg - das Log wuerde dann eine Grenze
    nennen, die der Client gar nicht mehr hat.
    """
    from api import WEBAPP_STATUS_TIMEOUT_SEC

    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    millisekunden = int(WEBAPP_STATUS_TIMEOUT_SEC * 1000)
    assert f"const STATUS_TIMEOUT_MS = {millisekunden}" in html


def test_live_api_und_service_worker_cache_vertraege():
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    worker = (WEBAPP / "service-worker.js").read_text(encoding="utf-8")
    assert "cache: 'no-store'" in html
    # Frueher stand hier fest: "const CACHE_NAME = 'wp-webapp-shell-v3'".
    # Genau das verhinderte die Fehlererkennung: Der Name aendert sich
    # bewusst, sobald index.html sich aendert. Wer den konkreten Wert
    # festschreibt, muss bei jeder WebApp-Aenderung mitziehen - und tut
    # es nicht. Ein vergessenes Update fuehrt dazu, dass der Browser die
    # alte, kaputte Fassung ausliefert und ein harter Reload nicht
    # hilft (05.10.2026).
    #
    # Geprueft wird deshalb das VERSCHRAENKUNGSMUSTER, nicht die Zahl.
    assert re.search(r"const CACHE_NAME = 'wp-webapp-shell-v\d+'", worker), (
        "CACHE_NAME muss eine Versionsziffer tragen, damit eine geaenderte "
        "WebApp den Cache invalidiert"
    )
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


def test_webapp_schleust_kein_fremdes_html_ein():
    """Regression: Fremde API-/Logtexte duerfen NIE per innerHTML gesetzt werden.

    Betroffen waren zuvor (Stand vor dieser Aenderung):
    - ``errorData.error``            (Fehler-API / HTTP-Body)
    - ``e.gewinner`` / ``e.grund``   (Entscheidungslog, Regeltexte aus Config)
    - ``system.exclusion_reason``    (Sperrgrund der Steuerung)
    - ``compressor.runtime_current`` (Laufzeitanzeige)

    Alle vier setzen die Werte jetzt per textContent. Statisches Markup bzw.
    durch Number() sanitisiertes Zahlenmaterial (Solarprofil-Balken,
    Regelliste aus der eigenen Konfiguration) bleibt unveraendert.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")

    for verbot in ("insertAdjacentHTML", "outerHTML", "document.write"):
        assert verbot not in html, f"{verbot} darf nicht verwendet werden"

    # Keine der verbotenen Stellen darf noch mit innerHTML gearbeitet werden.
    # Kommentarzeilen sind ausgenommen - dort wird das Muster nur benannt.
    verbotene_zeilen = [
        z for z in html.splitlines()
        if "innerHTML" in z
        and not z.lstrip().startswith(("//", "*", "/*"))
        and any(feld in z for feld in (
            "e.grund", "e.gewinner", "errorData.error", "exclusion_reason",
            "runtime_current", "e.message", "letzter.message",
        ))
    ]
    assert not verbotene_zeilen, (
        "Fremde Texte per innerHTML gesetzt:\n" + "\n".join(verbotene_zeilen)
    )

    # ...und sie tauchen jetzt als textContent auf.
    assert "fehlerwert.textContent = (errorData && errorData.error)" in html
    assert "liste.replaceChildren(fehlerzeile)" in html
    assert "regelB.textContent = e.gewinner || 'Keine'" in html
    assert "grund.textContent = e.grund || ''" in html
    assert "val.textContent = system.exclusion_reason" in html
    assert "wert.textContent = compressor.runtime_current" in html


def test_chart_fallback_warnt_statt_still_zu_scheitern():
    """Ohne CDN (Chart.js) bekommt der Nutzer eine sichtbare Meldung."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "function showChartUnavailable" in html
    assert "function hideChartUnavailable" in html
    assert 'id="chart-unavailable"' in html
    assert "typeof Chart === 'undefined'" in html
    assert "showChartUnavailable(" in html
    assert "role=\"status\"" in html


def test_aria_live_meldet_kompressor_und_regelwechsel():
    """Screenreader brauchen den betrieblich relevanten Zustandswechsel."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    for element_id in ("quick-compressor", "quick-rule"):
        zeile = next(z for z in html.splitlines() if f'id="{element_id}"' in z)
        assert 'aria-live="polite"' in zeile, f"{element_id} ohne aria-live"
        assert 'aria-atomic="true"' in zeile, f"{element_id} nicht atomar"


def test_alle_api_aufe_mit_api_key():
    """Jeder API-Aufruf schickt den Key mit - auch die Leserouten.

    Serverseitig ist der Key nur fuer Schreib-/Exportrouten Pflicht, aber
    das mitschleusen haelt die WebApp konsistent und zukunftssicher.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    import re
    aufrufe = re.findall(r"fetch\(`\$\{API_BASE\}(/[^`?]+)", html)
    assert aufrufe, "keine API-Aufrufe gefunden"
    for pfad in aufrufe:
        block = html.split(f"fetch(`${{API_BASE}}{pfad}", 1)[1][:400]
        assert "apiHeaders(" in block, f"{pfad} schickt keinen API-Key mit"


def test_versionsangabe_ist_eindeutig_und_aktuell():
    """Die Version darf nicht doppelt und veraltet im HTML stehen."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    import re
    # Keine veralteten Versionsnummern mehr irgendwo im Dokument - auch nicht
    # in Kommentaren, die beim naechsten Release sonst wieder zur Vorlage
    # eines falschen Werts werden.
    assert "v1.4.0" not in html, "veraltete Version v1.4.0 noch im HTML"
    # Genau EINE Versionsangabe: die APP_VERSION-Konstante.
    assert html.count("const APP_VERSION = ") == 1
    assert 'id="app-version"' in html
    assert "document.title = `WP Steuerung v${APP_VERSION}`" in html
    versionen = re.findall(r"const APP_VERSION = '([\d.]+)'", html)
    assert len(versionen) == 1, f"mehrere APP_VERSION-Angaben: {versionen}"
    # Die Versionsnummer ist mindestens zweistellig (Schema major.minor.patch).
    assert re.fullmatch(r"\d+\.\d+\.\d+", versionen[0]), versionen[0]


def test_fehlerzaehler_zeigt_tail_einschraenkung():
    """Der 24-h-Zaehler darf keine scheinbar vollstaendige Zahl vortaeuschen."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "errorData.vollstaendig === false" in html
    assert "(Log-Tail)" in html
    assert "mind. (Log-Tail)" in html
    assert "anzahl.title = nurTail" in html


def test_webapp_belastet_das_status_polling_nicht():
    """Die Fehlerhistorie wird separat, nicht alle 5 s, abgerufen."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "const ERROR_INTERVAL_MS = 60000;" in html


def test_service_worker_install_ueberlebt_ein_fehlendes_asset():
    """Regression (17.10.2026): cache.addAll() ist alles-oder-nichts.

    Es lehnt mit TypeError ab, sobald EINE Antwort keinen 200er-Status hat -
    hier war das /manifest.json (404) bzw. zeitweise ein 429 des Rate-Limits.
    Firefox meldete dann "ServiceWorker script at ... for scope ...
    encountered an error during installation", und die WebApp lief ohne
    Offline-Huelle weiter. Jetzt wird jede Datei einzeln gecacht.
    """
    worker = (WEBAPP / "service-worker.js").read_text(encoding="utf-8")
    assert not re.search(r"cache\.addAll\(\s*APP_SHELL\s*\)", worker), (
        "cache.addAll() ist alles-oder-nichts: ein einzelnes fehlendes Asset "
        "oder ein 429 sprengt die komplette Installation"
    )
    assert re.search(r"cache\.add\(url\)\.catch", worker), (
        "Jede Huelle-Datei muss einzeln und fehlertolerant gecacht werden"
    )


def test_manifest_icons_existieren():
    """Das Manifest verwies auf icon-192/512.png - die Dateien fehlten (404).

    Eine PWA ohne ladbare Icons ist nicht installierbar.
    """
    manifest = json.loads((WEBAPP / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["icons"], "Ohne Icons ist die PWA nicht installierbar"
    for icon in manifest["icons"]:
        assert (WEBAPP / icon["src"]).is_file(), f"Icon {icon['src']} fehlt"


# --- Regression: Die WebApp blieb beim 1. Anlauf leer --------------------
#
# Beobachtet: Die WebApp lud "teilweise erst beim 2. Anlauf oder gar
# nicht". Ursache war ein blockierendes <script src="https://cdn..."> im
# <head>: der HTML-Parser wartet auf die CDN-Antwort, bevor er die Seite
# ueberhaupt aufbaut. Ist das CDN langsam oder nicht erreichbar (Pi im WLAN
# ohne Internet, gesperrtes Netz, captive portal), bleibt die Seite komplett
# LEER - kein <div id="app">, kein Skeleton, kein JavaScript. Beim zweiten
# Versuch liegt die Datei im HTTP-Cache und es funktioniert.
#
# Nachgewiesen im Headless-Browser: mit 20 s langsamem CDN war das Dokument
# nach 9 s 774 Bytes lang und enthielt weder #app noch ein Skeleton.
def test_keine_blockierenden_cdn_skripte_im_head():
    """Chart.js/Zoom kommen lokal; kein blockierender CDN-Request im head."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]

    # Nur echte Tags pruefen, nicht den erlaeuternden Kommentar.
    tags = re.findall(r"<script[^>]*\bsrc\s*=[^>]*>", head)
    for tag in tags:
        assert "cdn." not in tag, (
            f"Das Skript {tag} laedt vom CDN. Ein blockierendes Skript im "
            "<head> haelt den HTML-Parser an: ist das CDN langsam oder nicht "
            "erreichbar, bleibt die Seite komplett leer und laedt erst beim "
            "2. Versuch (HTTP-Cache). Chart.js liegt jetzt lokal unter "
            "webapp/vendor/."
        )
        assert "defer" in tag, (
            f"Das Skript {tag} blockiert den HTML-Parser. 'defer' ist Pflicht."
        )

    assert "https://cdn.jsdelivr.net" not in html, (
        "Die WebApp darf keine CDN-Abhaengigkeit mehr haben"
    )


def test_chart_bibliothek_liegt_lokal_vor():
    """Die lokalen Vendor-Dateien muessen im Repo liegen und referenziert sein."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    for datei in ("chart.umd.min.js", "chartjs-plugin-zoom.min.js"):
        assert (WEBAPP / "vendor" / datei).is_file(), (
            f"webapp/vendor/{datei} fehlt - ohne die Datei gibt es keinen Chart"
        )
        assert f"vendor/{datei}" in html, (
            f"index.html laedt {datei} nicht lokal ein"
        )


def test_fonts_blockieren_den_aufbau_nicht():
    """Das Font-Stylesheet darf den Erstaufbau nicht blockieren."""
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    head = html.split("</head>", 1)[0]
    # <noscript> gilt nur ohne JavaScript - dort blockiert nichts.
    head = re.sub(r"<noscript>.*?</noscript>", "", head, flags=re.DOTALL)
    # Nur echte Stylesheet-Links, nicht die preconnect-Vorabverbindungen.
    tags = re.findall(r"<link[^>]*rel=[\"']stylesheet[\"'][^>]*>", head, re.DOTALL)
    for tag in tags:
        if "fonts.googleapis" in tag:
            assert 'media="print"' in tag, (
                "Ein synchrones Font-Stylesheet blockiert den Erstaufbau. "
                "Ohne Internet bleibt die Seite weiss. Erwartet wird das "
                "Muster media=\"print\" onload=\"this.media='all'\"."
            )


def test_status_polling_plant_sich_auch_offline_neu():
    """Regression: Das Status-Polling stoppte, sobald navigator.onLine false war.

    navigator.onLine ist false, sobald das Geraet kein Internet hat - die
    WebApp laeuft aber ueber Tailscale/Cloudflare oder das lokale WLAN
    trotzdem erreichbar. Ohne Neuplanung war die Schleife fuer immer tot:
    die Anzeige blieb beim letzten Stand stehen und aktualisierte sich nie
    wieder (die Verbindung wurde trotzdem als "verbunden" gemeldet).
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    start = html.index("function startStatusPolling")
    # Block bis zum naechsten "function " auf Zeilenanfang.
    rest = html[start + 10:]
    m = re.search(r"\n\s*function\s+\w+\s*\(", rest)
    block = rest[: m.start()] if m else rest
    assert "!navigator.onLine" in block
    assert "statusPollTimer = window.setTimeout(() => startStatusPolling(), delay)" in block, (
        "startStatusPolling muss sich nach dem Offline-Zweig neu planen, "
        "sonst stirbt die Schleife endgueltig"
    )


def test_update_values_bricht_bei_fehlendem_element_nicht_ab():
    """Kein ungeschuetzter Element-Zugriff in updateValues().

    Ein TypeError auf null (Element fehlt: alter Cache, umbenannte id, nicht
    gerendertes Card) wuerde den Rest von updateValues abbrechen - die
    Anzeige bliebe halb aktualisiert, waehrend die Verbindung "verbunden"
    gemeldet wurde.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    start = html.index("function updateValues()")
    end = html.index("function renderApp()", start)
    block = html[start:end]

    # const x = document.getElementById('id');  ->  x.something
    treffer = re.findall(
        r"const\s+(\w+)\s*=\s*document\.getElementById\([^)]*\)\s*;\s*\n\s*\1\.",
        block,
    )
    assert not treffer, (
        "Ungeschuetzte Element-Zugriffe in updateValues() - ein TypeError auf "
        f"null bricht die Aktualisierung ab: {treffer}"
    )


def test_service_worker_cacht_keine_fehlerseiten():
    """Regression: Eine 429/503-Antwort vergiftete die Offline-Huelle.

    Im HTML-Zweig des Service Workers landete JEDE Antwort im Cache - auch
    eine 429 (Rate-Limit) oder ein 503 ("System not initialized"). Nach
    einem einzigen Fehlversuch lieferte der Browser dann die Fehlerseite
    statt der WebApp. Nachgewiesen im Headless-Browser: der Cache-Eintrag
    fuer "/" hatte Status 429 und den Inhalt der Fehlerseite.
    """
    worker = (WEBAPP / "service-worker.js").read_text(encoding="utf-8")
    html_zweig = worker.split("url.pathname === '/manifest.json'")[1]
    html_zweig = html_zweig.split("return;")[0]
    assert "response.ok" in html_zweig, (
        "Der Service Worker darf nur erfolgreiche Antworten cachen - "
        "sonst wird die Offline-Huelle durch eine 429/503 vergiftet"
    )


def test_history_request_hat_timeout_und_abbruch():
    """Regression: Ein haengender /history-Request blockierte die WebApp.

    loadHistory() hatte weder Timeout noch Abort. Ein Request, der auf dem
    Pi haengt (100.000 CSV-Zeilen im 7-Tage-Fenster bei langsamer
    SD-Karte), liess den Chart dauerhaft leer. Und solange ein Request
    lief, blockierte `if (isLoadingHistory) return;` jede weitere
    Auswahl - der Nutzer konnte das Zeitfenster nicht mehr wechseln.
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert "const HISTORY_TIMEOUT_MS = 20000" in html
    start = html.index("async function loadHistory(hours)")
    # Block bis zum naechsten Top-Level-Funktionsaufruf. loadHistory
    # enthaelt verschachtelte Funktionsaufrufe (forEach/map), deshalb
    # zaehlt die Klammerbilanz, nicht die naechste "function ".
    depth = 0
    ende = None
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                ende = i
                break
    assert ende, "loadHistory nicht gefunden"
    block = html[start:ende]

    assert "signal: controller.signal" in block, (
        "loadHistory muss den Request abbrechen koennen"
    )
    assert "historyAbortController.abort()" in block, (
        "Ein neuer Aufruf muss den laufenden Request abbrechen"
    )
    assert "if (isLoadingHistory) return;" not in block, (
        "isLoadingHistory darf keinen weiteren Aufruf blockieren - der "
        "Nutzer muss jederzeit das Zeitfenster wechseln koennen"
    )
    assert "error.name !== 'AbortError'" in block, (
        "Ein abgebrochener Request ist kein Fehler und darf keinen leeren "
        "Chart erzeugen"
    )


