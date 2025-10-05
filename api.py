from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from configparser import ConfigParser
import logging
import asyncio
import aiohttp
from WW_skript import State, set_kompressor_status, read_temperature, read_temperature_cached, kompressor_status_func, current_runtime_func, total_runtime_func
from telegram_handler import send_status_telegram, is_solar_window, is_nighttime_func, fetch_solax_data
from utils import safe_timedelta
from datetime import datetime, timedelta

# Initialize FastAPI app
app = FastAPI()

# CORS for Android app
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")

# Load config and initialize state
config = ConfigParser()
config.read('config.ini')

# Sensor IDs from config.ini
sensor_ids = {
    "temp_oben": config["Sensors"].get("temp_oben", "28-0bd6d4461d84"),
    "temp_mittig": config["Sensors"].get("temp_mittig", "28-6977d446424a"),
    "temp_unten": config["Sensors"].get("temp_unten", "28-445bd44686f4"),
    "temp_verd": config["Sensors"].get("temp_verd", "28-213bd4460d65")
}

# Initialize State
state = State(config)

class CommandRequest(BaseModel):
    command: str

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
    try:
        async with aiohttp.ClientSession() as session:
            # Read temperatures asynchronously using read_temperature_cached
            sensor_tasks = [
                asyncio.create_task(read_temperature_cached(sensor_ids[key]))
                for key in ["temp_oben", "temp_mittig", "temp_unten", "temp_verd"]
            ]
            t_oben, t_mittig, t_unten, t_verd = await asyncio.gather(*sensor_tasks, return_exceptions=True)

            # Handle potential exceptions from temperature readings
            for temp, key in zip([t_oben, t_mittig, t_unten, t_verd],
                                 ["temp_oben", "temp_mittig", "temp_unten", "temp_verd"]):
                if isinstance(temp, Exception):
                    logging.error(f"Fehler beim Lesen des Sensors {sensor_ids[key]}: {temp}")
                    temp = None

            # Get compressor status and runtime using state
            kompressor_status = kompressor_status_func(state)
            current_runtime = current_runtime_func(state)
            total_runtime = total_runtime_func(state)

            # Send status to Telegram
            await send_status_telegram(
                session, t_oben, t_unten, t_mittig, t_verd, kompressor_status,
                current_runtime, total_runtime, config, state.chat_id, state.bot_token, state
            )

            def format_time(seconds):
                if isinstance(seconds, timedelta):
                    seconds = int(seconds.total_seconds())
                hours = seconds // 3600
                minutes = (seconds % 3600) // 60
                return f"{hours}h {minutes}min"

            # Fetch Solax data
            solax_data = await fetch_solax_data(session, state, datetime.now(state.local_tz)) or {
                "feedinpower": 0,
                "batPower": 0,
                "soc": 0,
                "api_fehler": True
            }

            nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_nighttime_func(
                config) and not state.bademodus_aktiv else 0
            is_solar_window_active = is_solar_window(config, state)

            mode_str = (
                "Bademodus" if state.bademodus_aktiv else
                f"Urlaub (-{int(config['Urlaubsmodus'].get('URLAUBSABSENKUNG', 6))}°C)" if state.urlaubsmodus_aktiv else
                "Übergangszeit (Solarfenster)" + (
                    " + Solarüberschuss" if state.solar_ueberschuss_aktiv else "") if is_solar_window_active else
                f"Solarüberschuss + Nachtabsenkung (-{nacht_reduction}°C)" if state.solar_ueberschuss_aktiv and is_nighttime_func(
                    config) else
                "Solarüberschuss" if state.solar_ueberschuss_aktiv else
                f"Nachtabsenkung (-{nacht_reduction}°C)" if is_nighttime_func(config) else
                "Normal"
            )

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
        logging.error(f"Error in get_status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/command")
async def execute_command(request: CommandRequest):
    try:
        if request.command == "bademodus":
            state.bademodus_aktiv = True
            state.urlaubsmodus_aktiv = False
            await set_kompressor_status(state, True)
            return {"status": "ok", "message": "Bademodus aktiviert"}
        elif request.command == "urlaub":
            state.urlaubsmodus_aktiv = True
            state.bademodus_aktiv = False
            await set_kompressor_status(state, False)
            return {"status": "ok", "message": "Urlaubsmodus aktiviert"}
        elif request.command == "normal":
            state.bademodus_aktiv = False
            state.urlaubsmodus_aktiv = False
            return {"status": "ok", "message": "Normalmodus aktiviert"}
        else:
            raise HTTPException(status_code=400, detail="Unbekannter Befehl")
    except Exception as e:
        logging.error(f"Error in execute_command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/temperatures")
async def get_temperatures():
    try:
        sensor_tasks = [
            asyncio.create_task(read_temperature_cached(sensor_ids[key]))
            for key in ["temp_oben", "temp_mittig", "temp_unten", "temp_verd"]
        ]
        t_oben, t_mittig, t_unten, t_verd = await asyncio.gather(*sensor_tasks, return_exceptions=True)

        for temp, key in zip([t_oben, t_mittig, t_unten, t_verd],
                             ["temp_oben", "temp_mittig", "temp_unten", "temp_verd"]):
            if isinstance(temp, Exception):
                logging.error(f"Fehler beim Lesen des Sensors {sensor_ids[key]}: {temp}")
                temp = None

        return {
            "temp_oben": t_oben,
            "temp_mittig": t_mittig,
            "temp_unten": t_unten,
            "temp_verd": t_verd
        }
    except Exception as e:
        logging.error(f"Error in get_temperatures: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)