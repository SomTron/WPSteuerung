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
                ambient_val = df.iloc[max(0, start_pos - 1)]["T_Verd"]
                heating_cycles.append((cycle, ambient_val))

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
    for cycle, ambient in heating_cycles:
        start_t = cycle["Zeitstempel"].iloc[0]
        duration_min = (cycle["Zeitstempel"].iloc[-1] - start_t).total_seconds() / 60.0
        
        dt_mittig = cycle["T_Mittig"].iloc[-1] - cycle["T_Mittig"].iloc[0]
        dt_oben = cycle["T_Oben"].iloc[-1] - cycle["T_Oben"].iloc[0]
        dt_unten = cycle["T_Unten"].iloc[-1] - cycle["T_Unten"].iloc[0]
        
        # COP
        thermal_kwh = (BOILER_VOLUME_L * SPECIFIC_HEAT_WATER * dt_mittig) / 3600000.0
        avg_power = cycle["ACPower"].mean()
        elec_kwh = (avg_power * (duration_min / 60.0)) / 1000.0
        cop = thermal_kwh / elec_kwh if elec_kwh > 0.05 else 0
        
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
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(450px, 1fr)); gap: 20px; }}
        .card {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
        h2 {{ margin-top: 0; color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
        .metric-list {{ list-style: none; padding: 0; }}
        .metric-list li {{ padding: 8px 0; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; }}
        .metric-val {{ font-weight: bold; color: #2980b9; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>WP Analyse Dashboard</h1>
        <span>Generiert am: {datetime.now().strftime('%d.%m.%Y %H:%M')}</span>
    </div>

    <div class="card" style="margin-bottom: 20px;">
        <h2>Interaktive Vorhersage (Wann starten?)</h2>
        <div style="display: flex; gap: 30px; flex-wrap: wrap; align-items: flex-end;">
            <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 5px solid #3498db;">
                <h4 style="margin-top:0">Aktuelle Temperaturen</h4>
                <div style="display: flex; gap: 15px;">
                    <div>
                        <label>Oben (°C):</label><br>
                        <input type="number" id="input-curr-oben" value="38" step="0.1" style="width: 60px; padding: 5px;">
                    </div>
                    <div>
                        <label>Mittig (°C):</label><br>
                        <input type="number" id="input-curr-mittig" value="35" step="0.1" style="width: 60px; padding: 5px;">
                    </div>
                    <div>
                        <label>Unten (°C):</label><br>
                        <input type="number" id="input-curr-unten" value="30" step="0.1" style="width: 60px; padding: 5px;">
                    </div>
                </div>
            </div>
            
            <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; border-left: 5px solid #27ae60;">
                <h4 style="margin-top:0">Ziel-Einstellung</h4>
                <div style="display: flex; gap: 15px; align-items: center;">
                    <div>
                        <label>Ziel Temp (°C):</label><br>
                        <input type="number" id="input-target" value="42" step="0.1" style="width: 60px; padding: 5px;">
                    </div>
                    <div style="font-size: 0.9em;">
                        (Gilt für alle 3 Sensoren)
                    </div>
                </div>
            </div>

            <div style="flex-grow: 1; min-width: 350px; background: #e8f4fd; padding: 15px; border-radius: 8px;">
                <h4 style="margin-top:0">Ergebnis: Dauer bis Ziel erreicht</h4>
                <div id="prediction-result" style="font-size: 1.2em; color: #2c3e50;"></div>
            </div>
        </div>
        <p style="font-size: 0.85em; color: #666; margin-top: 15px; line-height: 1.4;">
            <b>Berechnungsbasis:</b> Die Dauer basiert auf den historischen Durchschnittswerten: <br>
            <i>Oben: <span id="hint-rate-oben"></span> K/min | Mittig: <span id="hint-rate-mittig"></span> K/min | Unten: <span id="hint-rate-unten"></span> K/min</i>
        </p>
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

        // 1. COP Trend
        new ApexCharts(document.querySelector("#chart-cop"), {{
            series: [{{ name: 'COP', data: cycleData.filter(d => d.cop).map(d => ({{ x: d.timestamp, y: d.cop }})) }}],
            chart: {{ type: 'line', height: 350, zoom: {{ enabled: true }} }},
            stroke: {{ curve: 'smooth', width: 2 }},
            xaxis: {{ type: 'datetime' }},
            yaxis: {{ title: {{ text: 'Wirkungsgrad (COP)' }} }}
        }}).render();

        // 2. Ambient correlation
        new ApexCharts(document.querySelector("#chart-ambient"), {{
            series: [{{ name: 'COP vs Ambient', data: cycleData.filter(d => d.cop).map(d => [d.ambient, d.cop]) }}],
            chart: {{ type: 'scatter', height: 350 }},
            xaxis: {{ title: {{ text: 'Raumtemp / T_Verd Start (°C)' }}, tickAmount: 10 }},
            yaxis: {{ title: {{ text: 'COP' }} }}
        }}).render();

        // 3. Verdampfer Health
        new ApexCharts(document.querySelector("#chart-verd"), {{
            series: [{{ name: 'T_Verd Delta', data: cycleData.map(d => ({{ x: d.timestamp, y: d.verd_delta }})) }}],
            chart: {{ type: 'area', height: 350 }},
            xaxis: {{ type: 'datetime' }},
            yaxis: {{ title: {{ text: 'Abweichung von Ambient (K)' }} }}
        }}).render();

        // 4. Losses
        new ApexCharts(document.querySelector("#chart-loss"), {{
            series: [{{ name: 'Verlustrate', data: lossData.map(d => ({{ x: d.start, y: d.loss_k_per_h }})) }}],
            chart: {{ type: 'bar', height: 350 }},
            xaxis: {{ type: 'datetime' }}
        }}).render();

        // Calc Stats
        const validRatesOben = cycleData.filter(d => d.rate_oben > 0);
        const validRatesMittig = cycleData.filter(d => d.rate_mittig > 0);
        const validRatesUnten = cycleData.filter(d => d.rate_unten > 0);

        const avgRateOben = validRatesOben.reduce((a,b) => a + b.rate_oben, 0) / validRatesOben.length;
        const avgRateMittig = validRatesMittig.reduce((a,b) => a + b.rate_mittig, 0) / validRatesMittig.length;
        const avgRateUnten = validRatesUnten.reduce((a,b) => a + b.rate_unten, 0) / validRatesUnten.length;

        document.getElementById('hint-rate-oben').innerText = avgRateOben.toFixed(3);
        document.getElementById('hint-rate-mittig').innerText = avgRateMittig.toFixed(3);
        document.getElementById('hint-rate-unten').innerText = avgRateUnten.toFixed(3);

        const avgCop = cycleData.reduce((a,b) => a + (b.cop || 0), 0) / cycleData.filter(d => d.cop).length;
        const avgLoss = lossData.reduce((a,b) => a + b.loss_k_per_h, 0) / lossData.length;
        
        const statsHtml = `
            <li><span>Ø Wirkungsgrad (COP):</span> <span class="metric-val">${{avgCop.toFixed(2)}}</span></li>
            <li><span>Ø Standby-Verlust:</span> <span class="metric-val">${{avgLoss.toFixed(3)}} K/h</span></li>
            <li><span>Ø Aufheizrate Oben:</span> <span class="metric-val">${{avgRateOben.toFixed(3)}} K/min</span></li>
            <li><span>Ø Aufheizrate Mittig:</span> <span class="metric-val">${{avgRateMittig.toFixed(3)}} K/min</span></li>
            <li><span>Ø Aufheizrate Unten:</span> <span class="metric-val">${{avgRateUnten.toFixed(3)}} K/min</span></li>
            <li><span>Anzahl Heizzyklen:</span> <span class="metric-val">${{cycleData.length}}</span></li>
        `;
        document.getElementById('stats-list').innerHTML = statsHtml;

        // Prediction logic
        function updatePrediction() {{
            const target = parseFloat(document.getElementById('input-target').value);
            
            const currOben = parseFloat(document.getElementById('input-curr-oben').value);
            const currMittig = parseFloat(document.getElementById('input-curr-mittig').value);
            const currUnten = parseFloat(document.getElementById('input-curr-unten').value);

            const calcMin = (curr, rate) => (target > curr) ? Math.round((target - curr) / rate) : 0;

            const minOben = calcMin(currOben, avgRateOben);
            const minMittig = calcMin(currMittig, avgRateMittig);
            const minUnten = calcMin(currUnten, avgRateUnten);

            document.getElementById('prediction-result').innerHTML = `
                <div style="display:flex; justify-content:space-between; border-bottom:1px solid #cde;">
                   <span>Oben:</span> <span>${{minOben}} Min</span>
                </div>
                <div style="display:flex; justify-content:space-between; border-bottom:1px solid #cde;">
                   <span>Mittig:</span> <span>${{minMittig}} Min</span>
                </div>
                <div style="display:flex; justify-content:space-between;">
                   <span>Unten:</span> <span>${{minUnten}} Min</span>
                </div>
            `;
        }}
        ['input-curr-oben', 'input-curr-mittig', 'input-curr-unten', 'input-target'].forEach(id => {{
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
