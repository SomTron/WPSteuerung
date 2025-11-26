from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from datetime import datetime

# Data Models
class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: str

class ControlCommand(BaseModel):
    command: str # "force_on", "force_off", "set_mode"
    params: Optional[Dict[str, Any]] = None

app = FastAPI(title="WPSteuerung API", description="API for Heat Pump Control Android App", version="1.0.0")

# Global state reference (will be injected from main.py)
shared_state = None
control_funcs = None

def init_api(state, funcs):
    global shared_state, control_funcs
    shared_state = state
    control_funcs = funcs

@app.get("/status")
def get_status():
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    return {
        "temperatures": {
            "oben": shared_state.t_oben,
            "mittig": shared_state.t_mittig,
            "unten": shared_state.t_unten,
            "verdampfer": shared_state.t_verd,
            "boiler": shared_state.t_boiler
        },
        "compressor": {
            "status": "EIN" if shared_state.kompressor_ein else "AUS",
            "runtime_current": str(shared_state.last_runtime).split('.')[0] if shared_state.kompressor_ein else "0:00:00",
            "runtime_today": str(shared_state.total_runtime_today).split('.')[0]
        },
        "mode": {
            "current": shared_state.previous_modus,
            "solar_active": shared_state.solar_ueberschuss_aktiv,
            "holiday_active": shared_state.urlaubsmodus_aktiv,
            "bath_active": shared_state.bademodus_aktiv
        },
        "energy": {
            "battery_power": shared_state.batpower,
            "soc": shared_state.soc,
            "feed_in": shared_state.feedinpower
        },
        "system": {
            "exclusion_reason": shared_state.ausschluss_grund,
            "last_update": datetime.now().strftime("%H:%M:%S")
        }
    }

@app.post("/config")
def update_config(config: ConfigUpdate):
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    if config.section not in shared_state.config:
        raise HTTPException(status_code=404, detail=f"Section {config.section} not found")
    
    shared_state.config[config.section][config.key] = config.value
    # Trigger config save/reload if necessary (implementation depends on config handling)
    return {"status": "success", "message": f"Updated {config.section}.{config.key} to {config.value}"}

@app.post("/control")
async def control_system(cmd: ControlCommand):
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