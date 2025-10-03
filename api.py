from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telegram_handler import send_telegram_message  # Deine bestehenden Funktionen
from WW_skript import State, main_loop  # Deine State und main_loop
import asyncio
import uvicorn

app = FastAPI()

# CORS für Android-App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Für lokale Tests; enger einschränken in Prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Globale State-Instanz (für Einfachheit; in Prod thread-safe machen)
state = State(config)  # Deine State-Initialisierung

class CommandRequest(BaseModel):
    command: str  # z.B. "status", "bademodus"

@app.post("/command")
async def execute_command(request: CommandRequest):
    try:
        if request.command == "status":
            # Deine Status-Logik
            return {"status": "ok", "data": {"temp_oben": 46.4, "kompressor": "EIN"}}  # Beispiel
        elif request.command == "bademodus":
            state.bademodus_aktiv = True
            await send_telegram_message(...)  # Optional: Benachrichtigung
            return {"status": "ok", "message": "Bademodus aktiviert"}
        else:
            raise HTTPException(status_code=400, detail="Unbekannter Befehl")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status")
async def get_status():
    # Deine Status-Logik (Temperatur, Modus, etc.)
    return {"temp_oben": state.t_oben, "modus": "Normal", "kompressor": state.kompressor_ein}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)