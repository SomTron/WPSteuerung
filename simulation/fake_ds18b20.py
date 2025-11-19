import os
from pathlib import Path

# Sensor-IDs wie in deinem echten Projekt
SENSOR_IDS = {
    "oben": "28-0bd6d4461d84",
    "mittig": "28-6977d446424a",
    "unten": "28-445bd44686f4",
    "verd": "28-213bd4460d65",
}

# Basisordner für die simulierten Sensor-Dateien
SIM_PATH = Path("./simulated_w1/devices")

def init_sensors(default_temp=22.0):
    """Erstellt die simulierten 1-Wire-Dateien mit Starttemperatur."""
    os.makedirs(SIM_PATH, exist_ok=True)
    for sid in SENSOR_IDS.values():
        sensor_dir = SIM_PATH / sid
        sensor_dir.mkdir(parents=True, exist_ok=True)
        write_temperature(sid, default_temp)

def write_temperature(sensor_id, temp_c):
    """Schreibt eine manuell gesetzte Temperatur in die Simulationsdatei."""
    path = SIM_PATH / sensor_id / "w1_slave"
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w") as f:
        f.write(f"aa 00 4b 46 7f ff 0c 10 aa : crc=aa YES\n")
        f.write(f"aa 00 4b 46 7f ff 0c 10 aa t={int(temp_c*1000)}\n")

def read_temperature(sensor_id):
    """Liest die simulierte Temperatur (wie echtes System)."""
    path = SIM_PATH / sensor_id / "w1_slave"
    with open(path) as f:
        lines = f.readlines()
        temp_str = lines[1].split("t=")[-1]
        return float(temp_str) / 1000.0

def list_temps():
    """Zeigt alle simulierten Temperaturen an."""
    for name, sid in SENSOR_IDS.items():
        print(f"{name:7s}: {read_temperature(sid):.2f} °C")

if __name__ == "__main__":
    # Erstinitialisierung (nur beim ersten Mal nötig)
    init_sensors()
    list_temps()
