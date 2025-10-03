from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import logging
import asyncio
import pytz
from datetime import datetime, timedelta
from WW_skript import State, set_kompressor_status, read_temperature_func, kompressor_status_func, current_runtime_func, total_runtime_func
from telegram_handler import send_status_telegram, is_solar_window, is_nighttime_func
from utils import safe_timedelta
import aiohttp
import uvicorn
import os

# FastAPI app
app = FastAPI()

# CORS to allow Android app requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to your Android app's IP in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")

# Configuration and state (replace with your actual config loading)
config = {
    "Heizungssteuerung": {
        "SICHERHEITS_TEMP": 52.0,
        "GPIO_ATTEMPT_DELAY": 0.2,
        "NACHTABSENKUNG": 4,
        "NACHTABSENKUNG_END": "06:00"
    },
    "Urlaubsmodus": {
        "URLAUBSABSENKUNG": 6
    }
}

# Initialize state (simplified; use your actual State initialization)
class MockState:
    def __init__(self):
        self.local_tz = pytz.timezone("Europe/Berlin")
        self.kompressor_ein = False
        self.bademodus_aktiv = False
        self.urlaubsmodus_aktiv = False
        self.solar_ueberschuss_aktiv = False
        self.aktueller_einschaltpunkt = 40.0
        self.aktueller_ausschaltpunkt = 45.0
        self.last_runtime = timedelta()
        self.total_runtime_today = timedelta()
        self.start_time = None
        self.last_compressor_on_time = None
        self.ausschluss_grund = None
        self.chat_id = "your_chat_id"  # Replace with actual
        self.bot_token = "your_bot_token"  # Replace with actual
        self.session = aiohttp.ClientSession()
        self.gpio_lock = asyncio.Lock()
        self.config = config
        self.last_solar_window_log = None

state = MockState()

class CommandRequest(BaseModel):
    command: str  # e.g., "bademodus", "urlaub"

class StatusResponse(BaseModel):
    temp_oben: float | None
    temp_mittig: float | None
    temp_unten: float | None
    temp_verd: float | None
    kompressor_status: bool
    current_runtime: str
    total_runtime: str
    last_runtime: str
    einschaltpunkt: float
    ausschaltpunkt: float
    modus: str
    solar_ueberschuss: float
    batterieleistung: float
    solar_ueberschuss_aktiv: bool
    bademodus_aktiv: bool
    ausschluss_grund: str | None

@app.get("/status", response_model=StatusResponse)
async def get_status():
    """Returns the current system status."""
    try:
        async with aiohttp.ClientSession() as session:
            t_oben = read_temperature_func(["sensor_id_oben"])  # Replace with actual sensor IDs
            t_mittig = read_temperature_func(["sensor_id_mittig"])
            t_unten = read_temperature_func(["sensor_id_unten"])
            t_verd = read_temperature_func(["sensor_id_verd"])
            kompressor_status = kompressor_status_func()
            current_runtime = current_runtime_func()
            total_runtime = total_runtime_func()

            # Reuse send_status_telegram logic for consistency
            await send_status_telegram(
                session, t_oben, t_unten, t_mittig, t_verd, kompressor_status,
                current_runtime, total_runtime, config, get_solax_data_func,
                state.chat_id, state.bot_token, state, is_nighttime_func, is_solar_window
            )

            def format_time(seconds):
                if isinstance(seconds, timedelta):
                    seconds = int(seconds.total_seconds())
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                return f"{hours}h {minutes}min"

            solax_data = await get_solax_data_func(session, state) or {
                "feedinpower": 0,
                "batPower": 0,
                "soc": 0,
                "api_fehler": True
            }

            nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_nighttime_func and is_nighttime_func(config) and not state.bademodus_aktiv else 0
            is_solar_window_active = is_solar_window(config, state) if is_solar_window else False

            if state.bademodus_aktiv:
                mode_str = "Bademodus"
            elif state.urlaubsmodus_aktiv:
                mode_str = f"Urlaub (-{int(config['Urlaubsmodus'].get('URLAUBSABSENKUNG', 6))}°C)"
            elif is_solar_window_active:
                mode_str = "Übergangszeit (Solarfenster)"
                if state.solar_ueberschuss_aktiv:
                    mode_str += " + Solarüberschuss"
            elif state.solar_ueberschuss_aktiv and is_nighttime_func and is_nighttime_func(config):
                mode_str = f"Solarüberschuss + Nachtabsenkung (-{nacht_reduction}°C)"
            elif state.solar_ueberschuss_aktiv:
                mode_str = "Solarüberschuss"
            elif is_nighttime_func and is_nighttime_func(config):
                mode_str = f"Nachtabsenkung (-{nacht_reduction}°C)"
            else:
                mode_str = "Normal"

            return {
                "temp_oben": t_oben,
                "temp_mittig": t_mittig,
                "temp_unten": t_unten,
                "temp_verd": t_verd,
                "kompressor_status": kompressor_status,
                "current_runtime": format_time(current_runtime),
                "total_runtime": format_time(total_runtime),
                "last_runtime": format_time(state.last_runtime),
                "einschaltpunkt": state.aktueller_einschaltpunkt,
                "ausschaltpunkt": state.aktueller_ausschaltpunkt,
                "modus": mode_str,
                "solar_ueberschuss": solax_data.get("feedinpower", 0),
                "batterieleistung": solax_data.get("batPower", 0),
                "solar_ueberschuss_aktiv": state.solar_ueberschuss_aktiv,
                "bademodus_aktiv": state.bademodus_aktiv,
                "ausschluss_grund": state.ausschluss_grund
            }
    except Exception as e:
        logging.error(f"Fehler in get_status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/command")
async def execute_command(request: CommandRequest):
    """Executes commands like bademodus or urlaub."""
    try:
        if request.command == "bademodus":
            state.bademodus_aktiv = True
            state.urlaubsmodus_aktiv = False
            await set_kompressor_status(state, True)  # Example: Turn on compressor
            return {"status": "ok", "message": "Bademodus aktiviert"}
        elif request.command == "urlaub":
            state.urlaubsmodus_aktiv = True
            state.bademodus_aktiv = False
            await set_kompressor_status(state, False)  # Example: Turn off compressor
            return {"status": "ok", "message": "Urlaubsmodus aktiviert"}
        elif request.command == "normal":
            state.bademodus_aktiv = False
            state.urlaubsmodus_aktiv = False
            return {"status": "ok", "message": "Normalmodus aktiviert"}
        else:
            raise HTTPException(status_code=400, detail="Unbekannter Befehl")
    except Exception as e:
        logging.error(f"Fehler in execute_command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/temperatures")
async def get_temperatures():
    """Returns current temperatures."""
    try:
        t_oben = read_temperature_func(["sensor_id_oben"])  # Replace with actual sensor IDs
        t_mittig = read_temperature_func(["sensor_id_mittig"])
        t_unten = read_temperature_func(["sensor_id_unten"])
        t_verd = read_temperature_func(["sensor_id_verd"])
        return {
            "temp_oben": t_oben,
            "temp_mittig": t_mittig,
            "temp_unten": t_unten,
            "temp_verd": t_verd
        }
    except Exception as e:
        logging.error(f"Fehler in get_temperatures: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)