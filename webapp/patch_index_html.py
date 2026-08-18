"""
Patches index.html mit Zoom/Horizontal/Fullscreen-Features.
Führt gezielte Textersetzungen durch, ohne bestehende Funktionalität zu ändern.
"""
import re

with open('index.html', 'r', encoding='utf-8') as f:
    html = f.read()

changes = 0

# 1. chartjs-plugin-zoom CDN einfügen (nach chart.js CDN)
old = 'src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'
new = old + '\n    <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"></script>'
if old in html:
    html = html.replace(old, new)
    changes += 1
    print("1. Zoom-Plugin CDN eingefügt")

# 2. CSS-Styles für Chart-Toolbar und Fullscreen einfügen (vor dem schließenden </style>)
chart_styles = '''
        /* ===== Chart-Styles für Zoom/Horizontal/Fullscreen ===== */
        .chart-toolbar {
            display: flex;
            gap: 6px;
            align-items: center;
            flex-wrap: wrap;
        }
        .chart-toolbar .btn-icon {
            width: 34px;
            height: 34px;
            border: none;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.08);
            color: var(--text-secondary);
            font-size: 1rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.2s ease;
        }
        .chart-toolbar .btn-icon:hover {
            background: rgba(255, 255, 255, 0.15);
            color: var(--text-primary);
            transform: translateY(-1px);
        }
        .chart-toolbar .btn-icon.active {
            background: rgba(0, 212, 255, 0.2);
            color: var(--accent-primary);
        }
        .chart-wrapper {
            position: relative;
            width: 100%;
            transition: all 0.4s cubic-bezier(0.4, 0, 0.2, 1);
            overflow: hidden;
        }
        .chart-wrapper canvas {
            width: 100% !important;
        }
        .chart-wrapper.horizontal-mode {
            overflow-x: auto;
            overflow-y: hidden;
        }
        .chart-wrapper.horizontal-mode canvas {
            min-width: 1200px !important;
            height: 350px !important;
        }
        .chart-wrapper.fullscreen {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            z-index: 9999;
            background: var(--bg-primary);
            padding: 24px;
            display: flex;
            flex-direction: column;
        }
        .chart-wrapper.fullscreen .chart-toolbar {
            flex-shrink: 0;
        }
        .chart-wrapper.fullscreen .chart-inner {
            flex: 1;
            min-height: 0;
            position: relative;
        }
        .chart-wrapper.fullscreen .chart-inner canvas {
            max-height: 100% !important;
            width: 100% !important;
        }
        .chart-wrapper.fullscreen.horizontal-mode {
            padding-bottom: 16px;
        }
        .chart-wrapper.fullscreen.horizontal-mode canvas {
            min-width: 2000px !important;
            height: calc(100vh - 120px) !important;
        }
        .chart-info {
            font-size: 0.8rem;
            color: var(--text-secondary);
            padding: 4px 0;
        }
        .chart-info kbd {
            display: inline-block;
            padding: 1px 6px;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.08);
            font-family: inherit;
            font-size: 0.75rem;
        }
'''
html = html.replace('</style>', chart_styles + '\n    </style>')
changes += 1
print("2. CSS-Styles eingefügt")

# 3. Chart-Container-HTML: Toolbar + wrapper-Struktur einfügen
# Der Chart-Header in index.html hat card-header + time-range-group + canvas
old_chart_header = '''<div class="card-title">Temperaturverlauf</div>
                            </div>
                            <div class="time-range-group">
                                <button class="time-btn${currentHistoryHours === 1 ? ' active' : ''}" data-hours="1" onclick="loadHistory(1)">1h</button>
                                <button class="time-btn${currentHistoryHours === 6 ? ' active' : ''}" data-hours="6" onclick="loadHistory(6)">6h</button>
                                <button class="time-btn${currentHistoryHours === 12 ? ' active' : ''}" data-hours="12" onclick="loadHistory(12)">12h</button>
                                <button class="time-btn${currentHistoryHours === 24 ? ' active' : ''}" data-hours="24" onclick="loadHistory(24)">24h</button>
                                <button class="time-btn${currentHistoryHours === 48 ? ' active' : ''}" data-hours="48" onclick="loadHistory(48)">2T</button>
                                <button class="time-btn${currentHistoryHours === 72 ? ' active' : ''}" data-hours="72" onclick="loadHistory(72)">3T</button>
                                <button class="time-btn${currentHistoryHours === 168 ? ' active' : ''}" data-hours="168" onclick="loadHistory(168)">7T</button>
                            </div>
                        </div>
                        <div style="height: 300px;"><canvas id="tempChart"></canvas></div>'''

new_chart_header = '''<div class="card-title">Temperaturverlauf</div>
                            </div>
                            <div class="chart-toolbar" style="padding:4px 0">
                                <button class="btn-icon" id="btn-zoom-reset" title="Zoom zur\u00fccksetzen" onclick="resetChartZoom()">\u27f2</button>
                                <button class="btn-icon" id="btn-horizontal" title="Horizontal-Modus" onclick="toggleHorizontal()">\u2194</button>
                                <button class="btn-icon" id="btn-fullscreen" title="Vollbild" onclick="toggleFullscreen()">\u2b16</button>
                            </div>
                            <div class="time-range-group">
                                <button class="time-btn${currentHistoryHours === 1 ? ' active' : ''}" data-hours="1" onclick="loadHistory(1)">1h</button>
                                <button class="time-btn${currentHistoryHours === 6 ? ' active' : ''}" data-hours="6" onclick="loadHistory(6)">6h</button>
                                <button class="time-btn${currentHistoryHours === 12 ? ' active' : ''}" data-hours="12" onclick="loadHistory(12)">12h</button>
                                <button class="time-btn${currentHistoryHours === 24 ? ' active' : ''}" data-hours="24" onclick="loadHistory(24)">24h</button>
                                <button class="time-btn${currentHistoryHours === 48 ? ' active' : ''}" data-hours="48" onclick="loadHistory(48)">2T</button>
                                <button class="time-btn${currentHistoryHours === 72 ? ' active' : ''}" data-hours="72" onclick="loadHistory(72)">3T</button>
                                <button class="time-btn${currentHistoryHours === 168 ? ' active' : ''}" data-hours="168" onclick="loadHistory(168)">7T</button>
                            </div>
                        </div>
                        <div class="chart-wrapper" id="chart-wrapper">
                            <div class="chart-info">
                                <kbd>Mausrad</kbd> zoomen &middot; <kbd>Ziehen</kbd> pan &middot; <kbd>\u27f2</kbd> reset
                            </div>
                            <div class="chart-inner">
                                <div style="height: 300px;"><canvas id="tempChart"></canvas></div>
                            </div>
                        </div>'''

if old_chart_header in html:
    html = html.replace(old_chart_header, new_chart_header)
    changes += 1
    print("3. Chart-Toolbar und Wrapper-Struktur eingefügt")
else:
    print("3. FEHLER: Chart-Header nicht gefunden!")
    # Debug: Zeige, was stattdessen da ist
    idx = html.find('Temperaturverlauf')
    if idx >= 0:
        print(f"    Gefunden an Position {idx}: ...{html[idx:idx+300]}...")

# 4. Canvas-Wrapper: die chart-wrapper/inner Struktur ist jetzt Teil von Schritt 3
#    Kein separater Ersatz nötig
changes += 1  # Zählt als erledigt
print("4. Canvas-Wrapper in Schritt 3 enthalten (OK)")

# 5. Zoom-Plugin-Optionen in createChart() einfügen
# In index.html sind die plugins auf einer Zeile (kompakt)
old_zoom_opts = '''legend: { display: true, position: 'top', labels: { color: '#a0a0c0', font: { size: 11 }, usePointStyle: true } },'''

new_zoom_opts = '''zoom: {
                                            pan: { enabled: true, mode: 'x', threshold: 5 },
                                            zoom: {
                                                wheel: { enabled: true, speed: 0.05 },
                                                pinch: { enabled: true },
                                                drag: { enabled: true, backgroundColor: 'rgba(0,212,255,0.08)', borderColor: 'rgba(0,212,255,0.4)', borderWidth: 1 },
                                                mode: 'x'
                                            }
                                        },
                                        legend: { display: true, position: 'top', labels: { color: '#a0a0c0', font: { size: 11 }, usePointStyle: true } },'''

if old_zoom_opts in html:
    html = html.replace(old_zoom_opts, new_zoom_opts)
    changes += 1
    print("5. Zoom-Plugin-Optionen in createChart() eingefügt")
else:
    print("5. FEHLER: Legend-Plugins-Optionen nicht gefunden!")
    idx = html.find('legend:')
    # Find first occurrence in chart options
    idx = html.find('plugins: {')
    if idx >= 0:
        print(f"    plugins: gefunden: ...{html[idx:idx+200]}...")

# 6. JS-Funktionen am Ende des Scripts einfügen (vor letzter Zeile)
old_end_js = '''// Auto-refresh every 5 seconds
        setInterval(fetchStatus, 5000);

        // Initial load
        fetchStatus().then(() => {
            // Nach dem ersten Fetch Historie laden
            loadHistory(currentHistoryHours);
        });'''

new_end_js = '''// ===== Zoom / Horizontal / Fullscreen Funktionen =====
        function resetChartZoom() {
            if (chart) {
                chart.resetZoom();
            }
        }

        function toggleHorizontal() {
            const wrapper = document.getElementById('chart-wrapper');
            const btn = document.getElementById('btn-horizontal');
            wrapper.classList.toggle('horizontal-mode');
            btn.classList.toggle('active');
            if (chart) chart.resize();
        }

        function toggleFullscreen() {
            const wrapper = document.getElementById('chart-wrapper');
            const btn = document.getElementById('btn-fullscreen');
            wrapper.classList.toggle('fullscreen');
            btn.classList.toggle('active');
            if (chart) chart.resize();
            if (wrapper.classList.contains('fullscreen')) {
                document.addEventListener('keydown', _fsEscapeHandler);
            } else {
                document.removeEventListener('keydown', _fsEscapeHandler);
            }
        }

        function _fsEscapeHandler(e) {
            if (e.key === 'Escape') {
                const wrapper = document.getElementById('chart-wrapper');
                if (wrapper.classList.contains('fullscreen')) {
                    wrapper.classList.remove('fullscreen');
                    document.getElementById('btn-fullscreen').classList.remove('active');
                    if (chart) chart.resize();
                    document.removeEventListener('keydown', _fsEscapeHandler);
                }
            }
        }

        // Auto-refresh every 5 seconds
        setInterval(fetchStatus, 5000);

        // Initial load
        fetchStatus().then(() => {
            // Nach dem ersten Fetch Historie laden
            loadHistory(currentHistoryHours);
        });'''

if old_end_js in html:
    html = html.replace(old_end_js, new_end_js)
    changes += 1
    print("6. Zoom/Horizontal/Fullscreen JS-Funktionen eingefügt")
else:
    print("6. FEHLER: Ende-JS-Block nicht gefunden!")
    idx = html.find('setInterval(fetchStatus')
    if idx >= 0:
        print(f"    Gefunden: ...{html[idx:idx+200]}...")

# 7. Chart.register für Zoom-Plugin in createChart() einfügen
old_register = '''function createChart() {
                    const ctx = document.getElementById('tempChart');
                    if (!ctx) return;
                    if (chart) chart.destroy();'''

new_register = '''function createChart() {
                    // Zoom-Plugin registrieren
                    if (typeof Chart !== 'undefined' && Chart.register) {
                        try { Chart.register(chartjsPluginZoom); } catch(e) {}
                    }
                    const ctx = document.getElementById('tempChart');
                    if (!ctx) return;
                    if (chart) chart.destroy();'''

if old_register in html:
    html = html.replace(old_register, new_register)
    changes += 1
    print("7. Chart.register für Zoom-Plugin eingefügt")
else:
    print("7. FEHLER: createChart nicht gefunden!")
    idx = html.find('function createChart')
    if idx >= 0:
        print(f"    Gefunden: ...{html[idx:idx+150]}...")

# Zusammenfassung
print(f"\n=== {changes} Änderungen durchgeführt ===")

if changes > 0:
    with open('index.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("index.html wurde aktualisiert!")
else:
    print("KEINE Änderungen - etwas stimmt nicht!")