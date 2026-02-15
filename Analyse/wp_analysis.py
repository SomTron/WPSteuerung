import os
import pandas as pd
import numpy as np
import glob
from datetime import datetime, timedelta
import logging
import json

# Configuration
ANALYSE_DIR = "Analyse"
MERGED_CSV = os.path.join(ANALYSE_DIR, "merged_data.csv")
RESULTS_MD = os.path.join(ANALYSE_DIR, "analysis_results.md")
DASHBOARD_HTML = os.path.join(ANALYSE_DIR, "dashboard.html")
BOILER_VOLUME_L = 300  # Default 300 Liters
SPECIFIC_HEAT_WATER = 4186  # J/(kg*K)
DENSITY_WATER = 1.0  # kg/L

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def merge_csv_files():
    """Merges all CSV files in the Analyse folder and saves the result."""
    csv_files = glob.glob(os.path.join(ANALYSE_DIR, "*.csv"))
    csv_files = [f for f in csv_files if os.path.basename(f) != "merged_data.csv"]
    
    if not csv_files:
        logging.warning("No CSV files found in the Analyse folder.")
        if os.path.exists(MERGED_CSV):
            return pd.read_csv(MERGED_CSV, parse_dates=["Zeitstempel"])
        return None

    logging.info(f"Merging {len(csv_files)} files.")
    
    df_list = []
    for f in csv_files:
        try:
            temp_df = pd.read_csv(f, on_bad_lines='skip', low_memory=False)
            df_list.append(temp_df)
        except Exception as e:
            logging.error(f"Error reading {f}: {e}")

    if not df_list: return None

    df = pd.concat(df_list, ignore_index=True)
    if "Zeitstempel" in df.columns:
        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
        df = df.dropna(subset=["Zeitstempel"]).drop_duplicates(subset=["Zeitstempel"]).sort_values(by="Zeitstempel")
    
    df.to_csv(MERGED_CSV, index=False)
    return df

def analyze_cycles(df):
    """Identifies and analyzes heating and standby periods."""
    if "Kompressor" not in df.columns: return [], []

    # Reset index to ensure range-based indexing works perfectly
    df = df.reset_index(drop=True)
    
    # Identify boolean state
    is_on = df["Kompressor_Bool"].values
    
    heating_cycles = []
    standby_periods = []
    
    # Find indices where state changes
    starts = np.where(np.diff(is_on.astype(int)) == 1)[0] + 1
    ends = np.where(np.diff(is_on.astype(int)) == -1)[0] + 1

    # Heating Cycles
    for start_pos in starts:
        # Find next end
        next_ends = ends[ends > start_pos]
        if len(next_ends) > 0:
            end_pos = next_ends[0]
            cycle = df.iloc[start_pos:end_pos].copy()
            if len(cycle) > 5:
                # Ambient Temp: T_Verd just before start
                # Base Power: ACPower just before start (to subtract household consumption)
                ambient_val = df.iloc[max(0, start_pos - 1)]["T_Verd"]
                base_power = df.iloc[max(0, start_pos - 1)]["ACPower"]
                heating_cycles.append((cycle, ambient_val, base_power))

    # Standby Periods (Loss analysis)
    for end_pos in ends:
        next_starts = starts[starts > end_pos]
        if len(next_starts) > 0:
            next_start_pos = next_starts[0]
            period = df.iloc[end_pos:next_start_pos].copy()
            if len(period) > 30:
                standby_periods.append(period)

    return heating_cycles, standby_periods

def calculate_metrics(heating_cycles, standby_periods):
    """Calculates granular metrics for reporting."""
    cycle_results = []
    for cycle, ambient, base_power in heating_cycles:
        start_t = cycle["Zeitstempel"].iloc[0]
        duration_min = (cycle["Zeitstempel"].iloc[-1] - start_t).total_seconds() / 60.0
        
        # Filter obvious data gaps (e.g. cycle lasting > 6 hours due to missing 'AUS' entry)
        if duration_min > 360 or duration_min < 2:
            continue

        dt_mittig = cycle["T_Mittig"].iloc[-1] - cycle["T_Mittig"].iloc[0]
        dt_oben = cycle["T_Oben"].iloc[-1] - cycle["T_Oben"].iloc[0]
        dt_unten = cycle["T_Unten"].iloc[-1] - cycle["T_Unten"].iloc[0]
        
        # COP: 3-Zone Model (100L per sensor)
        thermal_joule = (100 * SPECIFIC_HEAT_WATER * dt_oben) + \
                        (100 * SPECIFIC_HEAT_WATER * dt_mittig) + \
                        (100 * SPECIFIC_HEAT_WATER * dt_unten)
        
        thermal_kwh = thermal_joule / 3600000.0
        
        # Electrical: Use user's measured fixed value (623W) for WP
        # This is more stable than subtracting fluctuating household base load
        wp_power = 623.0 
        
        elec_kwh = (wp_power * (duration_min / 60.0)) / 1000.0
        cop = thermal_kwh / elec_kwh if elec_kwh > 0.02 else 0
        
        # Evaporator Health: T_Verd delta from ambient
        avg_t_verd = cycle["T_Verd"].mean()
        verd_delta = avg_t_verd - ambient
        
        cycle_results.append({
            "timestamp": start_t.isoformat(),
            "duration": round(duration_min, 1),
            "cop": round(cop, 2) if 0 < cop < 10 else None,
            "ambient": round(ambient, 1),
            "t_verd_avg": round(avg_t_verd, 1),
            "verd_delta": round(verd_delta, 1),
            "rate_oben": round(dt_oben / duration_min, 3) if duration_min > 0 else 0,
            "rate_mittig": round(dt_mittig / duration_min, 3) if duration_min > 0 else 0,
            "rate_unten": round(dt_unten / duration_min, 3) if duration_min > 0 else 0
        })

    loss_results = []
    for period in standby_periods:
        duration_h = (period["Zeitstempel"].iloc[-1] - period["Zeitstempel"].iloc[0]).total_seconds() / 3600.0
        dt_loss = period["T_Mittig"].iloc[0] - period["T_Mittig"].iloc[-1]
        if duration_h > 1 and dt_loss > 0:
            loss_results.append({
                "start": period["Zeitstempel"].iloc[0].isoformat(),
                "loss_k_per_h": round(dt_loss / duration_h, 3)
            })

    return cycle_results, loss_results

def generate_html(cycle_data, loss_data):
    """Generates the HTML dashboard with interactive charts."""
    
    # Filter valid COPs for trend
    cops = [c for c in cycle_data if c["cop"] is not None]
    
    # Prepare data for JSON
    json_cycles = json.dumps(cycle_data)
    json_losses = json.dumps(loss_data)

    html_template = f"""
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <title>WP Steuerung Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #f4f7f6; color: #333; margin: 0; padding: 20px; }}
        .header {{ background: #2c3e50; color: white; padding: 15px 30px; border-radius: 8px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }}
        .grid {{ display: grid; grid-template-columns: 1fr; gap: 25px; }}
        .card {{ background: white; padding: 25px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }}
        h2 {{ margin-top: 0; color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-bottom: 20px; }}
        .metric-list {{ list-style: none; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 10px; }}
        .metric-list li {{ padding: 12px; background: #f8f9fa; border-radius: 6px; display: flex; flex-direction: column; align-items: center; border: 1px solid #eee; }}
        .metric-val {{ font-size: 1.2em; font-weight: bold; color: #2980b9; margin-top: 5px; }}
        .metric-label {{ font-size: 0.9em; color: #666; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>WP Analyse Dashboard</h1>
        <span>Generiert am: {datetime.now().strftime('%d.%m.%Y %H:%M')}</span>
    </div>

    <div class="card" style="margin-bottom: 25px;">
        <h2>Interaktive Vorhersage (Wann starten?)</h2>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;">
            <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 5px solid #3498db;">
                <h4 style="margin-top:0">Einstellungen</h4>
                <table style="width: 100%; border-collapse: collapse;">
                    <thead>
                        <tr style="text-align: left; font-size: 0.85em; color: #666;">
                            <th>Bereich</th>
                            <th>Aktiv</th>
                            <th>Aktuell (°C)</th>
                            <th>Ziel (°C)</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Oben</td>
                            <td><input type="checkbox" id="check-oben" checked></td>
                            <td><input type="number" id="input-curr-oben" value="38" step="0.1" style="width: 50px;"></td>
                            <td><input type="number" id="input-target-oben" value="45" step="0.1" style="width: 50px;"></td>
                        </tr>
                        <tr>
                            <td>Mittig</td>
                            <td><input type="checkbox" id="check-mittig" checked></td>
                            <td><input type="number" id="input-curr-mittig" value="35" step="0.1" style="width: 50px;"></td>
                            <td><input type="number" id="input-target-mittig" value="42" step="0.1" style="width: 50px;"></td>
                        </tr>
                        <tr>
                            <td>Unten</td>
                            <td><input type="checkbox" id="check-unten" checked></td>
                            <td><input type="number" id="input-curr-unten" value="30" step="0.1" style="width: 50px;"></td>
                            <td><input type="number" id="input-target-unten" value="40" step="0.1" style="width: 50px;"></td>
                        </tr>
                    </tbody>
                </table>
                <div id="validation-error" style="color: #e74c3c; font-size: 0.85em; margin-top: 10px; display: none;">
                    <b>Achtung:</b> Der Zielwert eines unteren Sensors darf nicht höher sein als der eines oberen Sensors.
                </div>
                <div style="margin-top: 10px; font-size: 0.8em; color: #888;">
                    Genutzte Raten (K/Min): O:<span id="hint-rate-oben"></span>, M:<span id="hint-rate-mittig"></span>, U:<span id="hint-rate-unten"></span>
                </div>
            </div>

            <div style="background: #e8f4fd; padding: 15px; border-radius: 8px; display: flex; flex-direction: column;">
                <h4 style="margin-top:0">Ergebnis: Dauer & Abschluss</h4>
                <div id="finish-time-overall" style="font-size: 1.4em; font-weight: bold; color: #1e3799; margin-bottom: 10px; border-bottom: 2px solid #1e3799; padding-bottom: 5px;"></div>
                <div id="prediction-result" style="font-size: 0.95em; color: #2c3e50; flex-grow: 1;"></div>
            </div>
            
            <div style="background: white; padding: 15px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); grid-column: span 2;">
                <h4 style="margin-top:0">Temperaturverlauf-Prognose</h4>
                <div id="chart-prediction" style="height: 250px;"></div>
            </div>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <h2>Effizienz Trend (COP)</h2>
            <div id="chart-cop"></div>
        </div>
        <div class="card">
            <h2>Ambient Temp vs. Effizienz</h2>
            <div id="chart-ambient"></div>
        </div>
        <div class="card">
            <h2>Verdampfer Performance (T_Verd Delta)</h2>
            <div id="chart-verd"></div>
        </div>
        <div class="card">
            <h2>Boiler Standby-Verluste (K/h)</h2>
            <div id="chart-loss"></div>
        </div>
        <div class="card">
            <h2>Statistiken</h2>
            <ul class="metric-list" id="stats-list"></ul>
        </div>
    </div>

    <script>
        const cycleData = {json_cycles};
        const lossData = {json_losses};

        function calculateSMA(data, period) {{
            let result = [];
            for (let i = 0; i < data.length; i++) {{
                if (i < period - 1) {{
                    result.push({{ x: data[i].x, y: null }});
                    continue;
                }}
                let sum = 0;
                for (let j = 0; j < period; j++) {{
                    sum += data[i - j].y;
                }}
                result.push({{ x: data[i].x, y: parseFloat((sum / period).toFixed(2)) }});
            }}
            return result;
        }}

        // 1. COP Trend
        const copPoints = cycleData.filter(d => d.cop).map(d => ({{ x: d.timestamp, y: d.cop }}));
        const copSMA = calculateSMA(copPoints, 7);

        new ApexCharts(document.querySelector("#chart-cop"), {{
            series: [
                {{ name: 'COP (Rohdaten)', data: copPoints, type: 'scatter' }},
                {{ name: 'COP (Gleitender Durchschnitt)', data: copSMA, type: 'line' }}
            ],
            chart: {{ height: 450, zoom: {{ enabled: true }} }},
            stroke: {{ curve: 'smooth', width: [0, 4] }},
            markers: {{ size: [4, 0] }},
            xaxis: {{ type: 'datetime' }},
            yaxis: {{ title: {{ text: 'Wirkungsgrad (COP)' }}, min: 0 }},
            colors: ['#3498db', '#e74c3c']
        }}).render();

        // 2. Ambient correlation
        new ApexCharts(document.querySelector("#chart-ambient"), {{
            series: [{{ name: 'COP vs Ambient', data: cycleData.filter(d => d.cop).map(d => [d.ambient, d.cop]) }}],
            chart: {{ type: 'scatter', height: 450 }},
            xaxis: {{ title: {{ text: 'Raumtemp / T_Verd Start (°C)' }}, tickAmount: 10 }},
            yaxis: {{ title: {{ text: 'COP' }} }},
            colors: ['#2ecc71']
        }}).render();

        // 3. Verdampfer Health
        const verdPoints = cycleData.map(d => ({{ x: d.timestamp, y: d.verd_delta }}));
        const verdSMA = calculateSMA(verdPoints, 7);
        
        new ApexCharts(document.querySelector("#chart-verd"), {{
            series: [
                {{ name: 'Abweichung (Rohdaten)', data: verdPoints, type: 'scatter' }},
                {{ name: 'Abweichung (Trend)', data: verdSMA, type: 'line' }}
            ],
            chart: {{ height: 450, zoom: {{ enabled: true }} }},
            stroke: {{ curve: 'smooth', width: [0, 4] }},
            markers: {{ size: [4, 0] }},
            xaxis: {{ type: 'datetime' }},
            yaxis: {{ title: {{ text: 'K (Diff zu Ambient)' }} }},
            colors: ['#9b59b6', '#f1c40f']
        }}).render();

        // 4. Losses
        new ApexCharts(document.querySelector("#chart-loss"), {{
            series: [{{ name: 'Verlustrate', data: lossData.map(d => ({{ x: d.start, y: d.loss_k_per_h }})) }}],
            chart: {{ type: 'bar', height: 450 }},
            xaxis: {{ type: 'datetime' }},
            colors: ['#e67e22']
        }}).render();

        // Calc Stats
        const validRatesOben = cycleData.filter(d => d.rate_oben > 0);
        const validRatesMittig = cycleData.filter(d => d.rate_mittig > 0);
        const validRatesUnten = cycleData.filter(d => d.rate_unten > 0);

        const avgRateOben = validRatesOben.reduce((a,b) => a + b.rate_oben, 0) / validRatesOben.length || 0;
        const avgRateMittig = validRatesMittig.reduce((a,b) => a + b.rate_mittig, 0) / validRatesMittig.length || 0;
        const avgRateUnten = validRatesUnten.reduce((a,b) => a + b.rate_unten, 0) / validRatesUnten.length || 0;

        document.getElementById('hint-rate-oben').innerText = avgRateOben.toFixed(3);
        document.getElementById('hint-rate-mittig').innerText = avgRateMittig.toFixed(3);
        document.getElementById('hint-rate-unten').innerText = avgRateUnten.toFixed(3);
 
        const validCops = cycleData.filter(d => d.cop !== null && d.cop > 0);
        const avgCop = validCops.reduce((a,b) => a + b.cop, 0) / validCops.length || 0;
        const avgLoss = lossData.reduce((a,b) => a + b.loss_k_per_h, 0) / lossData.length || 0;
        
        const statsHtml = `
            <li><span class="metric-label">Ø Wirkungsgrad (COP)</span> <span class="metric-val">${{avgCop.toFixed(2)}}</span></li>
            <li><span class="metric-label">Ø Standby-Verlust</span> <span class="metric-val">${{avgLoss.toFixed(3)}} K/h</span></li>
            <li><span class="metric-label">Ø Rate Oben</span> <span class="metric-val">${{avgRateOben.toFixed(3)}} K/min</span></li>
            <li><span class="metric-label">Ø Rate Mittig</span> <span class="metric-val">${{avgRateMittig.toFixed(3)}} K/min</span></li>
            <li><span class="metric-label">Ø Rate Unten</span> <span class="metric-val">${{avgRateUnten.toFixed(3)}} K/min</span></li>
            <li><span class="metric-label">Anzahl Heizzyklen</span> <span class="metric-val">${{cycleData.length}}</span></li>
        `;
        document.getElementById('stats-list').innerHTML = statsHtml;

        // Global chart variable to allow updating
        let predChart = null;

        // Prediction logic
        function updatePrediction() {{
            const targetOben = parseFloat(document.getElementById('input-target-oben').value);
            const targetMittig = parseFloat(document.getElementById('input-target-mittig').value);
            const targetUnten = parseFloat(document.getElementById('input-target-unten').value);
            
            const currOben = parseFloat(document.getElementById('input-curr-oben').value);
            const currMittig = parseFloat(document.getElementById('input-curr-mittig').value);
            const currUnten = parseFloat(document.getElementById('input-curr-unten').value);

            const activeOben = document.getElementById('check-oben').checked;
            const activeMittig = document.getElementById('check-mittig').checked;
            const activeUnten = document.getElementById('check-unten').checked;

            // Validation: Top >= Mid >= Bottom
            let isValid = true;
            if (activeOben && activeMittig && targetMittig > targetOben) isValid = false;
            if (activeMittig && activeUnten && targetUnten > targetMittig) isValid = false;
            if (activeOben && activeUnten && targetUnten > targetOben) isValid = false;

            document.getElementById('validation-error').style.display = isValid ? 'none' : 'block';

            const calcMin = (curr, target, rate) => (target > curr && rate > 0) ? Math.round((target - curr) / rate) : 0;

            const durations = [];
            if (activeOben) durations.push(calcMin(currOben, targetOben, avgRateOben));
            if (activeMittig) durations.push(calcMin(currMittig, targetMittig, avgRateMittig));
            if (activeUnten) durations.push(calcMin(currUnten, targetUnten, avgRateUnten));

            const maxMin = durations.length > 0 ? Math.max(...durations) : 0;
            
            // Calc overall time
            const now = new Date();
            const finishDate = new Date(now.getTime() + maxMin * 60000);
            const timeStr = finishDate.toLocaleTimeString([], {{hour: '2-digit', minute:'2-digit'}});
            
            document.getElementById('finish-time-overall').innerHTML = maxMin > 0 ? `Abgeschlossen um: ${{timeStr}} (+${{maxMin}} Min)` : "---";

            let resultHtml = "";
            let series = [];
            
            if (activeOben) {{
                const d = calcMin(currOben, targetOben, avgRateOben);
                resultHtml += `<div style="display:flex; justify-content:space-between; border-bottom:1px solid #cde; padding: 2px 0;">
                                <span>Oben (${{targetOben}}°C):</span> <span>${{d}} Min</span>
                               </div>`;
                series.push({{ name: 'Oben (Prognose)', data: [[0, currOben], [d, targetOben]] }});
            }}
            if (activeMittig) {{
                const d = calcMin(currMittig, targetMittig, avgRateMittig);
                resultHtml += `<div style="display:flex; justify-content:space-between; border-bottom:1px solid #cde; padding: 2px 0;">
                                <span>Mittig (${{targetMittig}}°C):</span> <span>${{d}} Min</span>
                               </div>`;
                series.push({{ name: 'Mittig (Prognose)', data: [[0, currMittig], [d, targetMittig]] }});
            }}
            if (activeUnten) {{
                const d = calcMin(currUnten, targetUnten, avgRateUnten);
                resultHtml += `<div style="display:flex; justify-content:space-between; padding: 2px 0;">
                                <span>Unten (${{targetUnten}}°C):</span> <span>${{d}} Min</span>
                               </div>`;
                series.push({{ name: 'Unten (Prognose)', data: [[0, currUnten], [d, targetUnten]] }});
            }}

            if (!activeOben && !activeMittig && !activeUnten) {{
                resultHtml = "Keine Sensoren ausgewählt.";
            }}
            document.getElementById('prediction-result').innerHTML = resultHtml;

            // Chart update
            const options = {{
                series: series,
                chart: {{ type: 'line', height: 250, toolbar: {{ show: false }} }},
                stroke: {{ width: 3, dashArray: [5, 5, 5] }},
                xaxis: {{ title: {{ text: 'Dauer (Minuten)' }}, type: 'numeric' }},
                yaxis: {{ title: {{ text: 'Temp (°C)' }} }},
                legend: {{ position: 'top' }}
            }};

            if (predChart) {{
                predChart.updateOptions(options);
            }} else {{
                predChart = new ApexCharts(document.querySelector("#chart-prediction"), options);
                predChart.render();
            }}
        }}

        const ids = ['input-curr-oben', 'input-curr-mittig', 'input-curr-unten', 
                     'input-target-oben', 'input-target-mittig', 'input-target-unten',
                     'check-oben', 'check-mittig', 'check-unten'];
        ids.forEach(id => {{
            document.getElementById(id).addEventListener('input', updatePrediction);
        }});
        updatePrediction();

    </script>
</body>
</html>
    """
    with open(DASHBOARD_HTML, "w", encoding="utf-8") as f:
        f.write(html_template)

def main():
    logging.info("Starting WP Enhanced Analysis...")
    df = merge_csv_files()
    if df is not None:
        # Preprocessing columns
        df["Kompressor_Bool"] = df["Kompressor"].astype(str).map({
            "EIN": True, "AUS": False, "1": True, "0": False, "1.0": True, "0.0": False
        }).fillna(False)
        
        # Ensure all numeric columns are float
        num_cols = ["T_Oben", "T_Mittig", "T_Unten", "T_Verd", "ACPower"]
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        heating_cycles, standby_periods = analyze_cycles(df)
        logging.info(f"Analyzed {len(heating_cycles)} heating phases and {len(standby_periods)} standby phases.")
        cycle_metrics, loss_metrics = calculate_metrics(heating_cycles, standby_periods)
        generate_html(cycle_metrics, loss_metrics)
        logging.info("Dashboard generated: Analyse/dashboard.html")

if __name__ == "__main__":
    main()
