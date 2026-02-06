#!/usr/bin/env python3
"""
web_temp_control.py — Weboberfläche mit Schiebereglern zur Steuerung
der simulierten DS18B20-Temperaturen.
"""

from flask import Flask, render_template_string, request, jsonify
from fake_ds18b20 import SENSOR_IDS, read_temperature, write_temperature, init_sensors

app = Flask(__name__)
init_sensors()  # sicherstellen, dass die Dateien existieren

# HTML-Template (Bootstrap + etwas JS)
HTML = """
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>DS18B20 Temperatur-Simulation</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
body { background: #f5f7fa; padding: 2rem; font-family: sans-serif; }
.card { margin-bottom: 1rem; }
.value { font-size: 1.5rem; font-weight: bold; }
</style>
</head>
<body>
<div class="container">
  <h2 class="mb-4">🌡️ DS18B20 Temperatur-Simulation</h2>
  <div class="row">
    {% for name, sid in sensors.items() %}
    <div class="col-md-6">
      <div class="card shadow-sm p-3">
        <h5 class="card-title text-capitalize">{{ name }}</h5>
        <div class="d-flex align-items-center justify-content-between">
          <input type="range" class="form-range" id="{{ name }}" min="0" max="100" step="0.1" value="{{ temps[name] }}"
                 oninput="updateValue('{{ name }}', this.value)">
          <span class="value" id="val_{{ name }}">{{ temps[name] }}</span> °C
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
</div>

<script>
function updateValue(sensor, value) {
  document.getElementById('val_' + sensor).textContent = value;
  fetch('/set_temp', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sensor: sensor, value: parseFloat(value)})
  });
}
setInterval(() => {
  fetch('/get_temps').then(r => r.json()).then(data => {
    for (const [sensor, val] of Object.entries(data)) {
      document.getElementById('val_' + sensor).textContent = val;
      document.getElementById(sensor).value = val;
    }
  });
}, 2000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    temps = {name: round(read_temperature(sid), 2) for name, sid in SENSOR_IDS.items()}
    return render_template_string(HTML, sensors=SENSOR_IDS, temps=temps)

@app.route("/set_temp", methods=["POST"])
def set_temp():
    data = request.get_json()
    sensor = data.get("sensor")
    value = data.get("value")
    if sensor not in SENSOR_IDS:
        return jsonify({"error": "invalid sensor"}), 400
    write_temperature(SENSOR_IDS[sensor], float(value))
    return jsonify({"status": "ok", "sensor": sensor, "value": value})

@app.route("/get_temps")
def get_temps():
    temps = {name: round(read_temperature(sid), 2) for name, sid in SENSOR_IDS.items()}
    return jsonify(temps)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
