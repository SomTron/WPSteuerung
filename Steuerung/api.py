from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any
from fastapi.middleware.cors import CORSMiddleware
import logging
from datetime import datetime
import asyncio
from utils_history import read_history_data

# Data Models
class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: str

class ControlCommand(BaseModel):
    command: str # "force_on", "force_off", "set_mode"
    params: Optional[Dict[str, Any]] = None

app = FastAPI(title="WPSteuerung API", description="API for Heat Pump Control Android App", version="1.0.0")

# CORS Middleware hinzufügen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion spezifische Origins angeben
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/status")
def get_status(request: Request):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    return {
        "temperatures": {
            "oben": shared_state.sensors.t_oben,
            "mittig": shared_state.sensors.t_mittig,
            "unten": shared_state.sensors.t_unten,
            "verdampfer": shared_state.sensors.t_verd,
            "vorlauf": shared_state.sensors.t_vorlauf,
            "boiler": shared_state.sensors.t_boiler
        },
        "compressor": {
            "status": "EIN" if shared_state.control.kompressor_ein else "AUS",
            "runtime_current": str(shared_state.stats.last_runtime).split('.')[0] if shared_state.control.kompressor_ein else "0:00:00",
            "runtime_today": str(shared_state.stats.total_runtime_today).split('.')[0]
        },
        "setpoints": {
            "einschaltpunkt": shared_state.control.aktueller_einschaltpunkt,
            "ausschaltpunkt": shared_state.control.aktueller_ausschaltpunkt,
            "sicherheits_temp": shared_state.sicherheits_temp,
            "verdampfertemperatur": shared_state.verdampfertemperatur
        },
        "mode": {
            "current": shared_state.control.previous_modus,
            "solar_active": shared_state.control.solar_ueberschuss_aktiv,
            "holiday_active": shared_state.urlaubsmodus_aktiv,
            "bath_active": shared_state.bademodus_aktiv
        },
        "energy": {
            "battery_power": shared_state.solar.batpower,
            "soc": shared_state.solar.soc,
            "feed_in": shared_state.solar.feedinpower
        },
        "system": {
            "exclusion_reason": shared_state.control.ausschluss_grund,
            "last_update": datetime.now().strftime("%H:%M:%S")
        }
    }

@app.post("/config")
def update_config(request: Request, config: ConfigUpdate):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    # Access Pydantic model sections
    section_obj = getattr(shared_state.config, config.section, None)
    if not section_obj:
        raise HTTPException(status_code=404, detail=f"Section {config.section} not found")
    
    if not hasattr(section_obj, config.key):
        raise HTTPException(status_code=404, detail=f"Key {config.key} not found in section {config.section}")

    try:
        # Simple type casting based on current value type if possible, otherwise string
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

@app.post("/control")
async def control_system(request: Request, cmd: ControlCommand):
    shared_state = getattr(request.app.state, "shared_state", None)
    control_funcs = getattr(request.app.state, "control_funcs", None)
    if not shared_state or not control_funcs:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if cmd.command == "force_on":
        # Example: Force compressor ON
        # This requires exposing the set_kompressor_status_func or similar in control_funcs
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](shared_state, True, force=True)
            return {"status": "success", "message": "Compressor forced ON"}
            
    elif cmd.command == "force_off":
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](shared_state, False, force=True)
            return {"status": "success", "message": "Compressor forced OFF"}
            
    elif cmd.command == "set_mode":
        mode = cmd.params.get("mode")
        if mode == "bademodus":
            shared_state.bademodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Bademodus set to {shared_state.bademodus_aktiv}"}
        elif mode == "urlaubsmodus":
            shared_state.urlaubsmodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Urlaubsmodus set to {shared_state.urlaubsmodus_aktiv}"}

    raise HTTPException(status_code=400, detail="Unknown command")

@app.get("/history")
async def get_history(hours: int = 24):
    """Get historical data from CSV asynchronously to prevent blocking the event loop"""
    csv_path = "heizungsdaten.csv"
    try:
        # Führe Dateioperation in Thread-Pool aus
        result = await asyncio.to_thread(read_history_data, csv_path, hours)
        
        if not result["data"] and not os.path.exists(csv_path):
            raise HTTPException(status_code=404, detail="No historical data available")
            
        return result
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error reading history in API: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")