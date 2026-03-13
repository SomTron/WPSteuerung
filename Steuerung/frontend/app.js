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
    eSoc: document.getElementById('energy-soc'),
    socBar: document.getElementById('soc-bar'),
    
    toggleBade: document.getElementById('toggle-bademodus'),
    toggleUrlaub: document.getElementById('toggle-urlaubsmodus'),
    
    btnOn: document.getElementById('btn-force-on'),
    btnOff: document.getElementById('btn-force-off')
};

// State to prevent toggling looping
let isFetching = false;
let updateInterval;

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
        
        const soc = data.energy.soc || 0;
        el.eSoc.textContent = `${soc} %`;
        el.socBar.style.width = `${Math.max(0, Math.min(100, soc))}%`;
        
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
updateInterval = setInterval(fetchStatus, 2000); // 2 seconds refresh
