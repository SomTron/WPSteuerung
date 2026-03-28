const API_BASE = window.location.origin;

// ── API-Key Management ────────────────────────────────────────────────────
const KEY_STORAGE = 'wp_api_key';

function getApiKey() {
    return localStorage.getItem(KEY_STORAGE) || '';
}

function saveApiKey(key) {
    localStorage.setItem(KEY_STORAGE, key);
}

function buildHeaders() {
    const key = getApiKey();
    const h = { 'Content-Type': 'application/json' };
    if (key) h['X-API-Key'] = key;
    return h;
}

// Shows or hides the API key modal
function showKeyModal(showError = false) {
    document.getElementById('api-key-modal').classList.remove('hidden');
    document.getElementById('api-key-error').classList.toggle('hidden', !showError);
}

function hideKeyModal() {
    document.getElementById('api-key-modal').classList.add('hidden');
}

document.getElementById('api-key-submit').addEventListener('click', () => {
    const key = document.getElementById('api-key-input').value.trim();
    if (key) saveApiKey(key);
    hideKeyModal();
    fetchStatus();
    renderHistoryChart(currentHours);
});

document.getElementById('btn-change-key').addEventListener('click', () => {
    document.getElementById('api-key-input').value = getApiKey();
    showKeyModal(false);
});

// ── DOM Elements ──────────────────────────────────────────────────────────
const el = {
    connDot: document.getElementById('conn-dot'),
    connText: document.getElementById('conn-text'),
    badge: document.getElementById('compressor-badge'),
    rtCurrent: document.getElementById('runtime-current'),
    rtToday: document.getElementById('runtime-today'),
    reason: document.getElementById('system-reason'),
    statusInfo: document.getElementById('system-status-info'),

    tOben: document.getElementById('temp-oben'),
    tMittig: document.getElementById('temp-mittig'),
    tUnten: document.getElementById('temp-unten'),
    tVorlauf: document.getElementById('temp-vorlauf'),
    tVerd: document.getElementById('temp-verdampfer'),

    eNetz: document.getElementById('energy-feedin'),
    eBat: document.getElementById('energy-battery'),
    eBatKwh: document.getElementById('energy-battery-kwh'),
    eSoc: document.getElementById('energy-soc'),
    socBar: document.getElementById('soc-bar'),
    ePv: document.getElementById('energy-pv'),

    ctrlSensor: document.getElementById('ctrl-sensor'),
    ctrlSetpoints: document.getElementById('ctrl-setpoints'),
    sysVpn: document.getElementById('sys-vpn'),

    fcToday: document.getElementById('forecast-today'),
    fcTomorrow: document.getElementById('forecast-tomorrow'),
    fcSun: document.getElementById('forecast-sun'),
    fcThresholds: document.getElementById('forecast-thresholds'),

    toggleBade: document.getElementById('toggle-bademodus'),
    toggleUrlaub: document.getElementById('toggle-urlaubsmodus'),

    btnOn: document.getElementById('btn-force-on'),
    btnOff: document.getElementById('btn-force-off'),

    chartContainer: document.getElementById('chart-container'),
};

// ── State ─────────────────────────────────────────────────────────────────
let isFetching = false;
let isConnected = true;
let chartInstance = null;
let currentHours = 6;
let chartDataCache = null;
let chartRenderTimeout = null;

// ── Helpers ───────────────────────────────────────────────────────────────
function formatTemp(val) {
    if (val === null || val === undefined) return '-- °C';
    return `${parseFloat(val).toFixed(1)} °C`;
}

/** Dims all data cards visually when offline */
function setStaleMode(stale) {
    const cards = document.querySelectorAll('section.card');
    cards.forEach(c => c.classList.toggle('stale', stale));
}

/** Debounce function for chart rendering */
function debounce(func, wait) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            clearTimeout(timeout);
            func(...args);
        };
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
    };
}

/** Downsample data for better performance on larger time ranges */
function downsampleData(data, maxPoints = 100) {
    if (data.length <= maxPoints) return data;

    const bucketSize = Math.ceil(data.length / maxPoints);
    const downsampled = [];

    for (let i = 0; i < data.length; i += bucketSize) {
        const bucket = data.slice(i, i + bucketSize);
        // Take average of bucket
        const avg = { ...bucket[0] };
        if (typeof bucket[0].t_oben === 'number') {
            avg.t_oben = bucket.reduce((sum, d) => sum + (d.t_oben || 0), 0) / bucket.length;
            avg.t_mittig = bucket.reduce((sum, d) => sum + (d.t_mittig || 0), 0) / bucket.length;
            avg.t_unten = bucket.reduce((sum, d) => sum + (d.t_unten || 0), 0) / bucket.length;
            avg.t_verd = bucket.reduce((sum, d) => sum + (d.t_verd || 0), 0) / bucket.length;
        }
        // Keep kompressor state (any ON in bucket = ON)
        avg.kompressor = bucket.some(d => d.kompressor === 'True' || d.kompressor === 'EIN') ? 'EIN' : 'AUS';
        downsampled.push(avg);
    }

    return downsampled;
}

// ── Status Fetch ──────────────────────────────────────────────────────────
async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/status`, { headers: buildHeaders() });

        if (res.status === 401) {
            showKeyModal(isConnected === false);
            el.connDot.classList.add('offline');
            el.connText.textContent = 'Auth erforderlich';
            return;
        }

        if (!res.ok) throw new Error('Network not ok');
        const data = await res.json();

        // ── Connection ──
        isConnected = true;
        setStaleMode(false);
        el.connDot.classList.remove('offline');
        el.connText.textContent = `Live (${data.system.last_update})`;

        // ── Compressor ──
        const isEin = data.compressor.status === 'EIN';
        el.badge.textContent = isEin ? 'EIN' : 'AUS';
        el.badge.className = isEin ? 'badge badge-on' : 'badge badge-off';
        el.rtCurrent.textContent = isEin ? data.compressor.runtime_current : '--:--:--';
        el.rtToday.textContent = data.compressor.runtime_today;

        // Nächste Umschaltung
        let nextSwitchText = '--';
        if (data.compressor.next_switch) {
            const sw = data.compressor.next_switch;
            const mins = data.compressor.next_switch_minutes;
            const target = data.compressor.next_switch_target;
            const reason = data.compressor.next_switch_reason;

            if (mins !== null && mins !== undefined) {
                nextSwitchText = `${sw} in ~${mins} min`;
            } else {
                nextSwitchText = sw;
            }
            if (target) {
                nextSwitchText += ` (bei ${target.toFixed(1)}°C)`;
            }
            if (reason) {
                nextSwitchText += ` - ${reason}`;
            }
        }
        el.reason.textContent = nextSwitchText;

        // Status Info
        let statusInfo = 'Keine Sperre';
        if (isEin && data.compressor.activation_reason) {
            statusInfo = `✅ ${data.compressor.activation_reason}`;
        } else if (!isEin && data.compressor.blocking_reason) {
            statusInfo = `🚫 ${data.compressor.blocking_reason}`;
        } else if (data.system.exclusion_reason) {
            statusInfo = data.system.exclusion_reason;
        }
        el.statusInfo.textContent = statusInfo;

        // ── Temperatures ──
        el.tOben.textContent = formatTemp(data.temperatures.oben);
        el.tMittig.textContent = formatTemp(data.temperatures.mittig);
        el.tUnten.textContent = formatTemp(data.temperatures.unten);
        el.tVorlauf.textContent = formatTemp(data.temperatures.vorlauf);
        el.tVerd.textContent = formatTemp(data.temperatures.verdampfer);

        // ── Energy ──
        el.eNetz.textContent = `${Math.round(data.energy.feed_in || 0)} W`;
        el.eBat.textContent = `${Math.round(data.energy.battery_power || 0)} W`;
        el.ePv.textContent = `${Math.round(data.energy.pv_power || 0)} W`;
        el.eBatKwh.textContent = `${(data.energy.battery_capacity_kwh || 0).toFixed(1)} kWh`;

        const soc = data.energy.soc || 0;
        el.eSoc.textContent = `${soc} %`;
        el.socBar.style.width = `${Math.max(0, Math.min(100, soc))}%`;

        // ── Control & Forecast ──
        el.ctrlSensor.textContent = data.setpoints.active_sensor || 'Automatisch';
        el.ctrlSetpoints.textContent = `${data.setpoints.einschaltpunkt?.toFixed(1) || '--'}° / ${data.setpoints.ausschaltpunkt?.toFixed(1) || '--'}°`;
        el.sysVpn.textContent = data.system.vpn_ip || 'N/A';

        if (data.forecast) {
            el.fcToday.textContent = `${data.forecast.today?.toFixed(1) || '--'} kWh`;
            el.fcTomorrow.textContent = `${data.forecast.tomorrow?.toFixed(1) || '--'} kWh`;
            el.fcSun.textContent = `${data.forecast.sunrise || '--:--'} – ${data.forecast.sunset || '--:--'}`;
            const low = data.forecast.threshold_low;
            const high = data.forecast.threshold_high;
            if (typeof low === 'number' && typeof high === 'number') {
                el.fcThresholds.textContent = `LOW=${low.toFixed(1)} | HIGH=${high.toFixed(1)} kWh`;
            } else {
                el.fcThresholds.textContent = '--';
            }
        }

        // PV-Plan Klassifizierung
        if (data.pv_plan) {
            const todayPlan = data.pv_plan.today || '--';
            const tomorrowPlan = data.pv_plan.tomorrow || '--';

            const getPlanEmoji = (cls) => {
                if (cls === 'HIGH') return '🟢';
                if (cls === 'MID') return '🟡';
                if (cls === 'LOW') return '🔴';
                return '⚪';
            };

            el.fcToday.textContent = `${getPlanEmoji(todayPlan)} ${data.forecast?.today?.toFixed(1) || '--'} kWh (${todayPlan})`;
            el.fcTomorrow.textContent = `${getPlanEmoji(tomorrowPlan)} ${data.forecast?.tomorrow?.toFixed(1) || '--'} kWh (${tomorrowPlan})`;
        }

        // ── Toggles ──
        if (!isFetching) {
            el.toggleBade.checked = data.mode.bath_active;
            el.toggleUrlaub.checked = data.mode.holiday_active;
        }

    } catch (e) {
        console.error(e);
        isConnected = false;
        setStaleMode(true);
        el.connDot.classList.add('offline');
        el.connText.textContent = 'Getrennt';
    }
}

// ── Send Command ──────────────────────────────────────────────────────────
async function sendCommand(cmd, params = {}) {
    isFetching = true;
    try {
        const res = await fetch(`${API_BASE}/control`, {
            method: 'POST',
            headers: buildHeaders(),
            body: JSON.stringify({ command: cmd, params: params }),
        });
        if (res.status === 401) { showKeyModal(true); return; }
        const data = await res.json();
        console.log(data);
    } catch (e) {
        console.error('Error sending command', e);
        alert('Befehl fehlgeschlagen! Keine Verbindung.');
    } finally {
        setTimeout(() => { isFetching = false; fetchStatus(); }, 500);
    }
}

// ── History Chart ─────────────────────────────────────────────────────────
async function renderHistoryChart(hours = 24) {
    currentHours = hours;

    // Clear any pending render
    if (chartRenderTimeout) clearTimeout(chartRenderTimeout);

    // Show loading state
    if (el.chartContainer) {
        el.chartContainer.style.opacity = '0.5';
    }

    try {
        const res = await fetch(`${API_BASE}/history?hours=${hours}`, { headers: buildHeaders() });
        if (!res.ok) throw new Error('Chart data network error');
        const json = await res.json();

        if (!json.data || json.data.length === 0) {
            if (el.chartContainer) el.chartContainer.style.opacity = '1';
            return;
        }

        // Downsample data for larger time ranges
        let chartData = json.data;
        if (hours >= 24) {
            chartData = downsampleData(chartData, hours === 168 ? 150 : 100);
        }

        const labels = chartData.map(d => new Date(d.timestamp));
        const tOben = chartData.map(d => d.t_oben);
        const tMittig = chartData.map(d => d.t_mittig);
        const tUnten = chartData.map(d => d.t_unten);
        const tVerd = chartData.map(d => d.t_verd);
        const komp = chartData.map(d => (d.kompressor === 'True' || d.kompressor === 'EIN') ? 100 : 0);

        const ctx = document.getElementById('historyChart').getContext('2d');

        // Configure time scale based on time range
        let stepSize = 1;
        let unit = 'hour';
        if (hours >= 168) {
            stepSize = 12;
            unit = 'day';
        } else if (hours >= 24) {
            stepSize = 4;
            unit = 'hour';
        } else {
            stepSize = 2;
            unit = 'hour';
        }

        if (chartInstance) {
            // Update existing chart
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = tOben;
            chartInstance.data.datasets[1].data = tMittig;
            chartInstance.data.datasets[2].data = tUnten;
            chartInstance.data.datasets[3].data = tVerd;
            chartInstance.data.datasets[4].data = komp;

            // Update time scale config
            chartInstance.options.scales.x.time.stepSize = stepSize;
            chartInstance.options.scales.x.time.unit = unit;

            chartInstance.update('none');
        } else {
            // Create new chart
            Chart.defaults.color = '#94a3b8';
            Chart.defaults.font.family = "'Outfit', sans-serif";

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: 'Oben',
                            data: tOben,
                            borderColor: '#ef4444',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            pointRadius: 0,
                            borderWidth: 2,
                            fill: false
                        },
                        {
                            label: 'Mittig',
                            data: tMittig,
                            borderColor: '#f59e0b',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            pointRadius: 0,
                            borderWidth: 2,
                            fill: false
                        },
                        {
                            label: 'Unten',
                            data: tUnten,
                            borderColor: '#10b981',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            pointRadius: 0,
                            borderWidth: 2,
                            fill: false
                        },
                        {
                            label: 'Verdampfer',
                            data: tVerd,
                            borderColor: '#0ea5e9',
                            backgroundColor: 'transparent',
                            tension: 0.3,
                            pointRadius: 0,
                            borderWidth: 2,
                            hidden: true,
                            fill: false
                        },
                        {
                            label: 'Kompressor',
                            data: komp,
                            borderColor: 'rgba(16, 185, 129, 0.3)',
                            backgroundColor: 'rgba(16, 185, 129, 0.15)',
                            type: 'bar',
                            yAxisID: 'y1',
                            barThickness: 'flex',
                            maxBarThickness: 50
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 300 },
                    interaction: {
                        mode: 'index',
                        intersect: false,
                        axis: 'x'
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                displayFormats: { hour: 'HH:mm', day: 'dd.MM' },
                                tooltipFormat: 'dd.MM HH:mm',
                                unit: unit,
                                stepSize: stepSize,
                                minUnit: 'hour'
                            },
                            grid: {
                                color: 'rgba(255,255,255,0.05)',
                                drawOnChartArea: false
                            },
                        },
                        y: {
                            position: 'left',
                            grid: { color: 'rgba(255,255,255,0.05)' },
                            suggestedMin: 5,
                            suggestedMax: 60
                        },
                        y1: {
                            position: 'right',
                            min: 0,
                            max: 100,
                            display: false,
                            grid: { drawOnChartArea: false }
                        },
                    },
                    plugins: {
                        legend: {
                            position: 'bottom',
                            labels: {
                                usePointStyle: true,
                                boxWidth: 8,
                                padding: 15
                            }
                        },
                        tooltip: {
                            backgroundColor: 'rgba(15,23,42,0.95)',
                            titleColor: '#fff',
                            bodyColor: '#cbd5e1',
                            borderColor: 'rgba(255,255,255,0.1)',
                            borderWidth: 1,
                            padding: 12,
                            displayColors: true,
                            callbacks: {
                                label: function (context) {
                                    let label = context.dataset.label || '';
                                    if (label) {
                                        label += ': ';
                                    }
                                    if (context.parsed.y !== null) {
                                        if (context.dataset.label === 'Kompressor') {
                                            label += context.parsed.y > 0 ? 'EIN' : 'AUS';
                                        } else {
                                            label += context.parsed.y.toFixed(1) + ' °C';
                                        }
                                    }
                                    return label;
                                }
                            }
                        },
                    },
                },
            });
        }
    } catch (e) {
        console.error('Error drawing chart', e);
    } finally {
        if (el.chartContainer) {
            el.chartContainer.style.opacity = '1';
        }
    }
}

// Debounced chart render for button clicks
const debouncedRenderChart = debounce(renderHistoryChart, 100);

// ── Chart Range Buttons ───────────────────────────────────────────────────
document.querySelectorAll('.range-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        debouncedRenderChart(parseInt(btn.dataset.hours));
    });
});

// ── Control Event Listeners ───────────────────────────────────────────────
el.toggleBade.addEventListener('change', e => {
    sendCommand('set_mode', { mode: 'bademodus', active: e.target.checked });
});

el.toggleUrlaub.addEventListener('change', e => {
    sendCommand('set_mode', { mode: 'urlaubsmodus', active: e.target.checked });
});

el.btnOn.addEventListener('click', () => {
    if (confirm('Achtung: Du greifst manuell in die Steuerung ein. Kompressor wirklich starten?')) {
        sendCommand('force_on');
    }
});

el.btnOff.addEventListener('click', () => {
    if (confirm('Achtung: Kompressor ausschalten kann Mindestlaufzeiten unterbrechen. Wirklich stoppen?')) {
        sendCommand('force_off');
    }
});

// ── Init ──────────────────────────────────────────────────────────────────
fetchStatus();
renderHistoryChart(currentHours);

setInterval(fetchStatus, 2000);
setInterval(() => renderHistoryChart(currentHours), 5 * 60 * 1000);
