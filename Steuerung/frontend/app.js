const API_BASE = window.location.origin;

// DOM Elements
const el = {
    connDot: document.getElementById('conn-dot'),
    connText: document.getElementById('conn-text'),
    badge: document.getElementById('compressor-badge'),
    rtCurrent: document.getElementById('runtime-current'),
    rtToday: document.getElementById('runtime-today'),
    reason: document.getElementById('system-reason'),
    
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
    
    toggleBade: document.getElementById('toggle-bademodus'),
    toggleUrlaub: document.getElementById('toggle-urlaubsmodus'),
    
    btnOn: document.getElementById('btn-force-on'),
    btnOff: document.getElementById('btn-force-off')
};

// State to prevent toggling looping
let isFetching = false;
let updateInterval;
let chartInstance = null;

function formatTemp(val) {
    if (val === null || val === undefined) return "-- °C";
    return `${parseFloat(val).toFixed(1)} °C`;
}

async function fetchStatus() {
    try {
        const res = await fetch(`${API_BASE}/status`);
        if (!res.ok) throw new Error("Network not ok");
        const data = await res.json();
        
        // Update Connection
        el.connDot.classList.remove('offline');
        el.connText.textContent = `Live (${data.system.last_update})`;
        
        // Update Compressor
        const isEin = data.compressor.status === "EIN";
        el.badge.textContent = isEin ? "EIN" : "AUS";
        el.badge.className = isEin ? "badge badge-on" : "badge badge-off";
        
        el.rtCurrent.textContent = isEin ? data.compressor.runtime_current : "--:--:--";
        el.rtToday.textContent = data.compressor.runtime_today;
        el.reason.textContent = data.system.exclusion_reason || "Keine Sperre";
        
        // Update Temps
        el.tOben.textContent = formatTemp(data.temperatures.oben);
        el.tMittig.textContent = formatTemp(data.temperatures.mittig);
        el.tUnten.textContent = formatTemp(data.temperatures.unten);
        el.tVorlauf.textContent = formatTemp(data.temperatures.vorlauf);
        el.tVerd.textContent = formatTemp(data.temperatures.verdampfer);
        
        // Update Energy
        el.eNetz.textContent = `${Math.round(data.energy.feed_in || 0)} W`;
        el.eBat.textContent = `${Math.round(data.energy.battery_power || 0)} W`;
        el.ePv.textContent = `${Math.round(data.energy.pv_power || 0)} W`;
        el.eBatKwh.textContent = `${(data.energy.battery_capacity_kwh || 0).toFixed(1)} kWh`;
        
        const soc = data.energy.soc || 0;
        el.eSoc.textContent = `${soc} %`;
        el.socBar.style.width = `${Math.max(0, Math.min(100, soc))}%`;
        
        // Update Extended Info
        el.ctrlSensor.textContent = data.setpoints.active_sensor || "Automatisch";
        el.ctrlSetpoints.textContent = `${data.setpoints.einschaltpunkt?.toFixed(1) || '--'}° / ${data.setpoints.ausschaltpunkt?.toFixed(1) || '--'}°`;
        el.sysVpn.textContent = data.system.vpn_ip || "N/A";
        
        if (data.forecast) {
            el.fcToday.textContent = `${data.forecast.today?.toFixed(1) || '--'} kWh`;
            el.fcTomorrow.textContent = `${data.forecast.tomorrow?.toFixed(1) || '--'} kWh`;
            el.fcSun.textContent = `${data.forecast.sunrise || '--:--'} - ${data.forecast.sunset || '--:--'}`;
        }
        
        // Update Toggles (only if user is not actively clicking)
        if (!isFetching) {
            el.toggleBade.checked = data.mode.bath_active;
            el.toggleUrlaub.checked = data.mode.holiday_active;
        }

    } catch (e) {
        console.error(e);
        el.connDot.classList.add('offline');
        el.connText.textContent = "Getrennt";
    }
}

async function sendCommand(cmd, params = {}) {
    isFetching = true;
    try {
        const res = await fetch(`${API_BASE}/control`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd, params: params })
        });
        const data = await res.json();
        console.log(data);
    } catch(e) {
        console.error("Error sending command", e);
        alert("Befehl fehlgeschlagen! Keine Verbindung.");
    } finally {
        setTimeout(() => { isFetching = false; fetchStatus(); }, 500);
    }
}

async function renderHistoryChart() {
    try {
        const res = await fetch(`${API_BASE}/history?hours=24`);
        if (!res.ok) throw new Error("Chart data network error");
        const json = await res.json();
        
        if (!json.data || json.data.length === 0) return;

        const labels = json.data.map(d => new Date(d.timestamp));
        const tOben = json.data.map(d => d.t_oben);
        const tMittig = json.data.map(d => d.t_mittig);
        const tUnten = json.data.map(d => d.t_unten);
        const tVerd = json.data.map(d => d.t_verd);
        // "Ein" = 1, "Aus" = 0 for compressor to show as a step or bar
        const komp = json.data.map(d => d.kompressor === 'True' || d.kompressor === 'EIN' ? 100 : 0);

        const ctx = document.getElementById('historyChart').getContext('2d');
        
        if (chartInstance) {
            chartInstance.data.labels = labels;
            chartInstance.data.datasets[0].data = tOben;
            chartInstance.data.datasets[1].data = tMittig;
            chartInstance.data.datasets[2].data = tUnten;
            chartInstance.data.datasets[3].data = tVerd;
            chartInstance.data.datasets[4].data = komp;
            chartInstance.update();
        } else {
            Chart.defaults.color = '#94a3b8';
            Chart.defaults.font.family = "'Outfit', sans-serif";
            
            chartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        { label: 'Oben', data: tOben, borderColor: '#ef4444', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Mittig', data: tMittig, borderColor: '#f59e0b', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Unten', data: tUnten, borderColor: '#10b981', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Verdampfer', data: tVerd, borderColor: '#0ea5e9', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderWidth: 2, hidden: true },
                        { label: 'Kompressor', data: komp, borderColor: 'rgba(255, 255, 255, 0.1)', backgroundColor: 'rgba(255, 255, 255, 0.05)', type: 'bar', yAxisID: 'y1' }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: { type: 'time', time: { displayFormats: { hour: 'HH:mm' }, tooltipFormat: 'HH:mm' }, grid: { color: 'rgba(255, 255, 255, 0.05)' } },
                        y: { position: 'left', grid: { color: 'rgba(255, 255, 255, 0.05)' } },
                        y1: { position: 'right', min: 0, max: 100, display: false, grid: { drawOnChartArea: false } }
                    },
                    plugins: {
                        legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8 } },
                        tooltip: { backgroundColor: 'rgba(15, 23, 42, 0.9)', titleColor: '#fff', bodyColor: '#cbd5e1', borderColor: 'rgba(255,255,255,0.1)', borderWidth: 1 }
                    }
                }
            });
        }
    } catch(e) {
        console.error("Error drawing chart", e);
    }
}

// Event Listeners
el.toggleBade.addEventListener('change', (e) => {
    sendCommand('set_mode', { mode: 'bademodus', active: e.target.checked });
});

el.toggleUrlaub.addEventListener('change', (e) => {
    sendCommand('set_mode', { mode: 'urlaubsmodus', active: e.target.checked });
});

el.btnOn.addEventListener('click', () => {
    if(confirm('Achtung: Du greifst manuell in die Steuerung ein. Kompressor wirklich starten?')) {
        sendCommand('force_on');
    }
});

el.btnOff.addEventListener('click', () => {
    if(confirm('Achtung: Kompressor ausschalten kann Mindestlaufzeiten unterbrechen. Wirklich stoppen?')) {
        sendCommand('force_off');
    }
});

// Init
fetchStatus();
renderHistoryChart();

updateInterval = setInterval(fetchStatus, 2000); // 2 seconds refresh for status
// refresh chart every 5 minutes
setInterval(renderHistoryChart, 5 * 60 * 1000);

