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
};

// ── State ─────────────────────────────────────────────────────────────────
let isFetching = false;
let isConnected = true;
let chartInstance = null;
let currentHours = 6;

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

// ── Status Fetch ──────────────────────────────────────────────────────────
async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/status`, { headers: buildHeaders() });

        if (res.status === 401) {
            // API key wrong or missing → show modal
            showKeyModal(isConnected === false); // show error only if was already connected
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

        // Status Info: Activation reason (running) OR blocking reason (stopped)
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
            // PV-Schwellenwerte anzeigen
            const low = data.forecast.threshold_low;
            const high = data.forecast.threshold_high;
            if (typeof low === 'number' && typeof high === 'number') {
                el.fcThresholds.textContent = `LOW=${low.toFixed(1)} | HIGH=${high.toFixed(1)} kWh`;
            } else {
                el.fcThresholds.textContent = '--';
            }
        }

        // PV-Plan Klassifizierung (LOW/MID/HIGH)
        if (data.pv_plan) {
            const todayPlan = data.pv_plan.today || '--';
            const tomorrowPlan = data.pv_plan.tomorrow || '--';

            // Emoji für Klassifizierung
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
    try {
        const res = await fetch(`${API_BASE}/history?hours=${hours}`, { headers: buildHeaders() });
        if (!res.ok) throw new Error('Chart data network error');
        const json = await res.json();

        if (!json.data || json.data.length === 0) return;

        const labels = json.data.map(d => new Date(d.timestamp));
        const tOben = json.data.map(d => d.t_oben);
        const tMittig = json.data.map(d => d.t_mittig);
        const tUnten = json.data.map(d => d.t_unten);
        const tVerd = json.data.map(d => d.t_verd);
        const komp = json.data.map(d => (d.kompressor === 'True' || d.kompressor === 'EIN') ? 100 : 0);

        const ctx = document.getElementById('historyChart').getContext('2d');

        if (chartInstance) {
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = tOben;
            chartInstance.data.datasets[1].data = tMittig;
            chartInstance.data.datasets[2].data = tUnten;
            chartInstance.data.datasets[3].data = tVerd;
            chartInstance.data.datasets[4].data = komp;
            chartInstance.update('none'); // skip animation on refresh
        } else {
            Chart.defaults.color = '#94a3b8';
            Chart.defaults.font.family = "'Outfit', sans-serif";

            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        { label: 'Oben', data: tOben, borderColor: '#ef4444', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Mittig', data: tMittig, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Unten', data: tUnten, borderColor: '#10b981', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Verdampfer', data: tVerd, borderColor: '#0ea5e9', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2, hidden: true },
                        { label: 'Kompressor', data: komp, borderColor: 'rgba(255,255,255,0.1)', backgroundColor: 'rgba(255,255,255,0.05)', type: 'bar', yAxisID: 'y1' },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 400 },
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            type: 'time',
                            time: {
                                displayFormats: { hour: 'HH:mm', day: 'dd.MM' },
                                tooltipFormat: 'dd.MM HH:mm',
                                unit: 'hour',
                                stepSize: 2,
                                minUnit: 'hour'
                            },
                            grid: { color: 'rgba(255,255,255,0.05)' },
                        },
                        y: { position: 'left', grid: { color: 'rgba(255,255,255,0.05)' } },
                        y1: { position: 'right', min: 0, max: 100, display: false, grid: { drawOnChartArea: false } },
                    },
                    plugins: {
                        legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8 } },
                        tooltip: { backgroundColor: 'rgba(15,23,42,0.9)', titleColor: '#fff', bodyColor: '#cbd5e1', borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1 },
                    },
                },
            });
        }
    } catch (e) {
        console.error('Error drawing chart', e);
    }
}

// ── Chart Range Buttons ───────────────────────────────────────────────────
document.querySelectorAll('.range-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderHistoryChart(parseInt(btn.dataset.hours));
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
