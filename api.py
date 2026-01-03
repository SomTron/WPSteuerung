from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
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

# CORS Middleware hinzufügen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion spezifische Origins angeben
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        "setpoints": {
            "einschaltpunkt": shared_state.aktueller_einschaltpunkt,
            "ausschaltpunkt": shared_state.aktueller_ausschaltpunkt,
            "sicherheits_temp": shared_state.sicherheits_temp,
            "verdampfertemperatur": shared_state.verdampfertemperatur
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

    # Trigger config save/reload not fully implemented yet for INI write-back
    # shared_state.update_config() # This would reload from file, overwriting changes!
    # Ideally we should write to file here. For now, in-memory update.
    return {"status": "success", "message": f"Updated {config.section}.{config.key} to {new_value}"}

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

@app.get("/history")
def get_history(hours: int = 24):
    """Get historical data from CSV"""
    import os
    import pandas as pd
    
    csv_path = "heizungsdaten.csv"
    if not os.path.exists(csv_path):
        raise HTTPException(status_code=404, detail="No historical data available")
    
    try:
        df = pd.read_csv(csv_path)
        # Filter last N hours
        df['Zeitstempel'] = pd.to_datetime(df['Zeitstempel'])
        cutoff = datetime.now() - pd.Timedelta(hours=hours)
        df = df[df['Zeitstempel'] >= cutoff]
        
        # Convert to JSON-friendly format
        data = []
        for _, row in df.iterrows():
            data.append({
                "timestamp": row['Zeitstempel'].strftime("%Y-%m-%d %H:%M:%S"),
                "t_oben": row['T_Oben'],
                "t_mittig": row['T_Mittig'],
                "t_unten": row['T_Unten'],
                "t_verd": row['T_Verd'],
                "kompressor": row['Kompressor']
            })
        
        return {"data": data, "count": len(data)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")