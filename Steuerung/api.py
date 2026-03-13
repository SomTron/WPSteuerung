from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional, Dict, Any
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
from datetime import datetime
import asyncio
from utils_history import read_history_data

# ── API-Key Authentifizierung ──────────────────────────────────────────────
# Setze die Umgebungsvariable WP_API_KEY um die API abzusichern.
# Wenn WP_API_KEY nicht gesetzt ist, ist die Authentifizierung deaktiviert.
_API_KEY = os.environ.get("WP_API_KEY", "")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

async def verify_api_key(key: str = Depends(api_key_header)):
    """Prüft den API-Key, falls einer konfiguriert ist."""
    if _API_KEY and key != _API_KEY:
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key")

# ── Data Models ────────────────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: str

class ControlCommand(BaseModel):
    command: str  # "force_on", "force_off", "set_mode"
    params: Optional[Dict[str, Any]] = None

# ── App Setup ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="WPSteuerung API",
    description="API for Heat Pump Control Web App",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe(obj, attr, default=None):
    """Sicherer Attributzugriff mit Fallback-Wert."""
    return getattr(obj, attr, default)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(verify_api_key)])
def get_status(request: Request):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")

    sensors = _safe(shared_state, "sensors")
    control = _safe(shared_state, "control")
    stats   = _safe(shared_state, "stats")
    solar   = _safe(shared_state, "solar")

    bat_cap = _safe(shared_state, "battery_capacity", 0)
    soc_val = _safe(solar, "soc", 0) if solar else 0
    bat_kwh = (bat_cap * soc_val / 100.0) if isinstance(bat_cap, (int, float)) and bat_cap > 0 else 0

    kompressor_ein = _safe(control, "kompressor_ein", False)

    return {
        "temperatures": {
            "oben":      _safe(sensors, "t_oben"),
            "mittig":    _safe(sensors, "t_mittig"),
            "unten":     _safe(sensors, "t_unten"),
            "verdampfer":_safe(sensors, "t_verd"),
            "vorlauf":   _safe(sensors, "t_vorlauf"),
            "boiler":    _safe(sensors, "t_boiler"),
        },
        "compressor": {
            "status":          "EIN" if kompressor_ein else "AUS",
            "runtime_current": str(_safe(stats, "last_runtime", "0:00:00")).split('.')[0] if kompressor_ein else "0:00:00",
            "runtime_today":   str(_safe(stats, "total_runtime_today", "0:00:00")).split('.')[0],
            "activation_reason": _safe(control, "activation_reason"),
            "blocking_reason":   _safe(control, "blocking_reason"),
        },
        "setpoints": {
            "einschaltpunkt":    _safe(control, "aktueller_einschaltpunkt"),
            "ausschaltpunkt":    _safe(control, "aktueller_ausschaltpunkt"),
            "sicherheits_temp":  _safe(shared_state, "sicherheits_temp"),
            "verdampfertemperatur": _safe(shared_state, "verdampfertemperatur"),
            "active_sensor":     _safe(control, "active_rule_sensor"),
        },
        "mode": {
            "current":      _safe(control, "previous_modus"),
            "solar_active": _safe(control, "solar_ueberschuss_aktiv", False),
            "holiday_active": _safe(shared_state, "urlaubsmodus_aktiv", False),
            "bath_active":   _safe(shared_state, "bademodus_aktiv", False),
        },
        "energy": {
            "battery_power":      _safe(solar, "batpower", 0),
            "soc":                soc_val,
            "feed_in":            _safe(solar, "feedinpower", 0),
            "pv_power":           _safe(solar, "acpower", 0),
            "battery_capacity_kwh": bat_kwh,
        },
        "forecast": {
            "today":    _safe(solar, "forecast_today"),
            "tomorrow": _safe(solar, "forecast_tomorrow"),
            "sunrise":  _safe(solar, "sunrise_today"),
            "sunset":   _safe(solar, "sunset_today"),
        },
        "system": {
            "exclusion_reason": _safe(control, "ausschluss_grund"),
            "last_update":      datetime.now().strftime("%H:%M:%S"),
            "vpn_ip":           _safe(shared_state, "vpn_ip"),
        },
    }


@app.post("/config", dependencies=[Depends(verify_api_key)])
def update_config(request: Request, config: ConfigUpdate):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")

    section_obj = getattr(shared_state.config, config.section, None)
    if not section_obj:
        raise HTTPException(status_code=404, detail=f"Section {config.section} not found")

    if not hasattr(section_obj, config.key):
        raise HTTPException(status_code=404, detail=f"Key {config.key} not found in section {config.section}")

    try:
        current_value = getattr(section_obj, config.key)
        new_value = config.value

        if isinstance(current_value, bool):
            new_value = config.value.lower() == 'true'
        elif isinstance(current_value, int):
            new_value = int(config.value)
        elif isinstance(current_value, float):
            new_value = float(config.value)

        setattr(section_obj, config.key, new_value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid value for {config.key}: {str(e)}")

    return {"status": "success", "message": f"Updated {config.section}.{config.key} to {new_value}"}


@app.post("/control", dependencies=[Depends(verify_api_key)])
async def control_system(request: Request, cmd: ControlCommand):
    shared_state  = getattr(request.app.state, "shared_state", None)
    control_funcs = getattr(request.app.state, "control_funcs", None)
    if not shared_state or not control_funcs:
        raise HTTPException(status_code=503, detail="System not initialized")

    if cmd.command == "force_on":
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](True, force=True)
            return {"status": "success", "message": "Compressor forced ON"}

    elif cmd.command == "force_off":
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](False, force=True)
            return {"status": "success", "message": "Compressor forced OFF"}

    elif cmd.command == "set_mode":
        mode = cmd.params.get("mode") if cmd.params else None
        if mode == "bademodus":
            shared_state.bademodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Bademodus set to {shared_state.bademodus_aktiv}"}
        elif mode == "urlaubsmodus":
            shared_state.urlaubsmodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Urlaubsmodus set to {shared_state.urlaubsmodus_aktiv}"}

    raise HTTPException(status_code=400, detail="Unknown command")


@app.get("/history")
async def get_history(hours: int = 24):
    """Liefert historische Daten asynchron aus der CSV-Datei."""
    csv_path = "heizungsdaten.csv"
    try:
        result = await asyncio.to_thread(read_history_data, csv_path, hours)

        if not result["data"] and not os.path.exists(csv_path):
            raise HTTPException(status_code=404, detail="No historical data available")

        return result
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error reading history in API: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")


# ── Static Frontend ────────────────────────────────────────────────────────
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    logging.warning(f"Frontend directory not found at {frontend_path}")