from fastapi import FastAPI
import random
from datetime import timedelta

app = FastAPI()

# Simulierte globale Variablen
kompressor_ein = True
current_runtime = timedelta(minutes=15)
last_runtime = timedelta(minutes=12)
total_runtime_today = timedelta(hours=2, minutes=30)

# Simulierte Temperaturdaten (synchron)
def read_temperature(sensor_id):
    if sensor_id == "oben":
        return random.uniform(40, 50)
    elif sensor_id == "hinten":
        return random.uniform(38, 45)
    elif sensor_id == "verd":
        return random.uniform(8, 15)
    return None

@app.get("/status")
def get_status():  # Hier keine 'async def', da keine asynchronen Aufrufe nötig sind
    t_oben = read_temperature("oben")
    t_hinten = read_temperature("hinten")
    t_verd = read_temperature("verd")
    power_source = "Direkter PV-Strom" if kompressor_ein else None
    return {
        "temperatures": {
            "oben": t_oben,
            "hinten": t_hinten,
            "verdampfer": t_verd
        },
        "compressor": "EIN" if kompressor_ein else "AUS",
        "power_source": power_source if kompressor_ein else None,
        "current_runtime": str(current_runtime).split('.')[0] if kompressor_ein else None,
        "last_runtime": str(last_runtime).split('.')[0] if not kompressor_ein else None,
        "total_runtime_today": str(total_runtime_today).split('.')[0]
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)