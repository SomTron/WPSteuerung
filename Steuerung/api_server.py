"""
Standalone API Server für Entwicklung und Tests
Kann auf dem PC ausgeführt werden, ohne Raspberry Pi Hardware
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, Any
import logging
from datetime import datetime, timedelta
import random

# Data Models
class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: str

class ControlCommand(BaseModel):
    command: str  # "force_on", "force_off", "set_mode"
    params: Optional[Dict[str, Any]] = None

app = FastAPI(
    title="WPSteuerung API", 
    description="API for Heat Pump Control - Development Mode", 
    version="1.0.0"
)

# CORS Middleware hinzufügen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In Produktion spezifische Origins angeben
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Statische Dateien (PWA) servieren
from fastapi.staticfiles import StaticFiles
import os
if os.path.exists("webapp"):
    app.mount("/webapp", StaticFiles(directory="webapp", html=True), name="webapp")
else:
    logging.warning("Webapp directory not found - PWA will not be served")

# Mock State (für Entwicklung)
class MockState:
    def __init__(self):
        self.kompressor_ein = False
        self.t_oben = 42.5
        self.t_mittig = 41.0
        self.t_unten = 39.5
        self.t_verd = 10.2
        self.t_boiler = (self.t_oben + self.t_mittig + self.t_unten) / 3
        self.last_runtime = timedelta(minutes=15)
        self.total_runtime_today = timedelta(hours=2, minutes=30)
        self.previous_modus = "Normalmodus"
        self.solar_ueberschuss_aktiv = False
        self.urlaubsmodus_aktiv = False
        self.bademodus_aktiv = False
        self.batpower = 250
        self.soc = 75
        self.feedinpower = 100
        self.ausschluss_grund = None

mock_state = MockState()

@app.get("/")
def root():
    """Root endpoint - API info"""
    return {
        "name": "WPSteuerung API",
        "version": "1.0.0",
        "mode": "development",
        "description": "Heat Pump Control API - Running in development mode with mock data"
    }

@app.get("/status")
def get_status():
    """Get current system status"""
    # Simulate temperature variations
    mock_state.t_oben = 40 + random.uniform(-2, 2)
    mock_state.t_mittig = 39 + random.uniform(-2, 2)
    mock_state.t_unten = 38 + random.uniform(-2, 2)
    mock_state.t_verd = 10 + random.uniform(-1, 1)
    mock_state.t_boiler = (mock_state.t_oben + mock_state.t_mittig + mock_state.t_unten) / 3
    
    return {
        "temperatures": {
            "oben": round(mock_state.t_oben, 1),
            "mittig": round(mock_state.t_mittig, 1),
            "unten": round(mock_state.t_unten, 1),
            "verdampfer": round(mock_state.t_verd, 1),
            "boiler": round(mock_state.t_boiler, 1)
        },
        "compressor": {
            "status": "EIN" if mock_state.kompressor_ein else "AUS",
            "runtime_current": str(mock_state.last_runtime).split('.')[0] if mock_state.kompressor_ein else "0:00:00",
            "runtime_today": str(mock_state.total_runtime_today).split('.')[0]
        },
        "setpoints": {
            "einschaltpunkt": 42,
            "ausschaltpunkt": 45,
            "sicherheits_temp": 52,
            "verdampfertemperatur": 6.0
        },
        "mode": {
            "current": mock_state.previous_modus,
            "solar_active": mock_state.solar_ueberschuss_aktiv,
            "holiday_active": mock_state.urlaubsmodus_aktiv,
            "bath_active": mock_state.bademodus_aktiv
        },
        "energy": {
            "battery_power": mock_state.batpower,
            "soc": mock_state.soc,
            "feed_in": mock_state.feedinpower
        },
        "system": {
            "exclusion_reason": mock_state.ausschluss_grund,
            "last_update": datetime.now().strftime("%H:%M:%S"),
            "mode": "development"
        }
    }

@app.post("/config")
def update_config(config: ConfigUpdate):
    """Update configuration (mock)"""
    logging.info(f"Config update request: {config.section}.{config.key} = {config.value}")
    return {
        "status": "success", 
        "message": f"Updated {config.section}.{config.key} to {config.value} (mock)",
        "note": "Running in development mode - changes not persisted"
    }

@app.post("/control")
def control_system(cmd: ControlCommand):
    """Control system (mock)"""
    if cmd.command == "force_on":
        mock_state.kompressor_ein = True
        mock_state.ausschluss_grund = None
        return {"status": "success", "message": "Kompressor forced ON (mock)"}
        
    elif cmd.command == "force_off":
        mock_state.kompressor_ein = False
        mock_state.ausschluss_grund = "Manuell ausgeschaltet"
        return {"status": "success", "message": "Kompressor forced OFF (mock)"}
        
    elif cmd.command == "set_mode":
        mode = cmd.params.get("mode") if cmd.params else None
        active = cmd.params.get("active", False) if cmd.params else False
        
        if mode == "bademodus":
            mock_state.bademodus_aktiv = active
            return {"status": "success", "message": f"Bademodus set to {active} (mock)"}
        elif mode == "urlaubsmodus":
            mock_state.urlaubsmodus_aktiv = active
            return {"status": "success", "message": f"Urlaubsmodus set to {active} (mock)"}
        else:
            raise HTTPException(status_code=400, detail=f"Unknown mode: {mode}")
    
    raise HTTPException(status_code=400, detail=f"Unknown command: {cmd.command}")

@app.get("/history")
def get_history(hours: int = 24):
    """Get historical data from CSV (mock data for development)"""
    import pandas as pd
    from datetime import datetime, timedelta
    
    # Generate mock historical data
    now = datetime.now()
    data = []
    
    for i in range(hours * 12):  # 12 data points per hour (every 5 minutes)
        timestamp = now - timedelta(minutes=i * 5)
        data.append({
            "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "t_oben": round(40 + random.uniform(-3, 3), 1),
            "t_mittig": round(39 + random.uniform(-3, 3), 1),
            "t_unten": round(38 + random.uniform(-3, 3), 1),
            "t_verd": round(10 + random.uniform(-2, 2), 1),
            "kompressor": random.choice(["EIN", "AUS"])
        })
    
    data.reverse()  # Oldest first
    return {"data": data, "count": len(data)}

if __name__ == "__main__":
    import uvicorn
    
    logging.basicConfig(level=logging.INFO)
    print("=" * 60)
    print("WPSteuerung API Server - Development Mode")
    print("=" * 60)
    print(f"Starting API server on http://0.0.0.0:5000")
    print(f"Swagger UI: http://localhost:5000/docs")
    print(f"ReDoc: http://localhost:5000/redoc")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=5000, log_level="info")
