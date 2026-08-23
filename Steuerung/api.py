from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Dict, Any
import logging
import os
from datetime import datetime
import re
import io

from utils import HEIZUNGSDATEN_CSV

try:
    from priority_control_logic import _is_nachtsperre_aktiv
except ImportError:
    _is_nachtsperre_aktiv = None

# Allowed commands and modes for validation
ALLOWED_COMMANDS = {"force_on", "force_off", "set_mode"}
ALLOWED_MODES = {"bademodus", "urlaubsmodus"}
ALLOWED_SECTIONS = {"Heizungssteuerung", "Telegram", "Hardware", "Sicherheitsgrenzen", "Solar"}

def build_mode_payload(state, priority_info_override=None):
    """Baut das 'mode'-Objekt des /status Endpoints.

    Wichtig: Die Sommer-Modus-Felder liegen am State-Root (sommer_modus_aktiv,
    sommer_modus_zaehler) bzw. in der Priority-Config (Offset, benoetigte
    Tage) - NICHT unter state.control. (Vorher wurden sie dort gelesen,
    weshalb das Webinterface den Sommer-Modus immer als 'Inaktiv' anzeigte.)"""
    sommer_cfg = getattr(getattr(state, 'priority_config', None), 'sommer_modus', None)
    control = getattr(state, 'control', None)

    if priority_info_override is None:
        # Nachtsperre direkt aus der Priority-Config berechnen
        # (gleiche Logik wie im /status Endpoint)
        nachtsperre = False
        pc = getattr(state, 'priority_config', None)
        if pc is not None and _is_nachtsperre_aktiv is not None:
            try:
                nachtsperre = bool(_is_nachtsperre_aktiv(pc, datetime.now(state.local_tz)))
            except Exception as e:
                logging.warning(f"Konnte Nachtsperren-Status nicht ermitteln: {e}")
        priority_info_override = {"nachtsperre_aktiv": nachtsperre}
    info = priority_info_override
    return {
        "current": (getattr(control, 'previous_modus', None) or ""),
        "solar_active": bool(getattr(control, 'solar_ueberschuss_aktiv', False)),
        "holiday_active": bool(getattr(state, 'urlaubsmodus_aktiv', False)),
        "bath_active": bool(getattr(state, 'bademodus_aktiv', False)),
        "nightsperre_active": (bool(info.get("nachtsperre_aktiv", False))
                               if isinstance(info, dict) else False),
        "active_rule": (getattr(control, 'active_rule_name', None) or ""),
        "active_rule_sensor": (getattr(control, 'active_rule_sensor', None) or ""),
        "blocking_reason": (getattr(control, 'blocking_reason', None) or ""),
        "soll_einschalten": bool(getattr(control, '_soll_einschalten', False)),
        "sommer_modus_aktiv": bool(getattr(state, 'sommer_modus_aktiv', False)),
        "sommer_modus_offset_c": float(getattr(sommer_cfg, 'temperatur_offset_c', 0.0) or 0.0),
        "sommer_modus_tage_ueber": int(getattr(state, 'sommer_modus_zaehler', 0) or 0),
        "sommer_modus_benoetigte": int(getattr(sommer_cfg, 'benoetigte_tage', 3) or 3),
    }


# Data Models
class ConfigUpdate(BaseModel):
    section: str = Field(..., min_length=1, max_length=50)
    key: str = Field(..., min_length=1, max_length=50)
    value: str = Field(..., min_length=0, max_length=500)

    @field_validator('section')
    @classmethod
    def section_must_be_valid(cls, v):
        """Prueft, dass der Section-Name nur erlaubte Werte enthaelt."""
        if v not in ALLOWED_SECTIONS:
            raise ValueError(f"Section '{v}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_SECTIONS))}")
        return v

    @field_validator('key')
    @classmethod
    def key_must_be_safe(cls, v):
        """Prueft, dass der Key-Name nur erlaubte Zeichen enthaelt (keine Injections)."""
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', v):
            raise ValueError(f"Key '{v}' enthaelt unerlaubte Zeichen. Nur Buchstaben, Zahlen und Unterstriche erlaubt.")
        return v

class ControlCommand(BaseModel):
    command: str = Field(..., min_length=1, max_length=50)
    params: Optional[Dict[str, Any]] = None

    @field_validator('command')
    @classmethod
    def command_must_be_allowed(cls, v):
        """Prueft, dass der Command-Name in der erlaubten Liste steht."""
        if v not in ALLOWED_COMMANDS:
            raise ValueError(f"Command '{v}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_COMMANDS))}")
        return v

    @model_validator(mode='after')
    def validate_mode_if_set_mode(self):
        """Prueft, dass der Modus erlaubte Werte hat, wenn command=set_mode."""
        if self.command == 'set_mode' and self.params:
            mode = self.params.get('mode')
            if mode and mode not in ALLOWED_MODES:
                raise ValueError(f"Modus '{mode}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_MODES))}")
        return self

app = FastAPI(title="WPSteuerung API", description="API for Heat Pump Control Android App", version="1.0.0")

# CORS Middleware hinzufügen
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: In Produktion spezifische Origins angeben (Sicherheitsrisiko!)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files: Serve webapp directory
_webapp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp")
_webapp_dir = os.path.normpath(_webapp_dir)
if os.path.isdir(_webapp_dir):
    app.mount("/static", StaticFiles(directory=_webapp_dir), name="static")

@app.get("/", response_class=HTMLResponse)
def serve_index():
    """Serve the main dashboard HTML page."""
    index_path = os.path.join(_webapp_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Webapp not found")

@app.get("/index.html", response_class=HTMLResponse)
def serve_index_html():
    """Serve the main dashboard HTML page (direct URL)."""
    return serve_index()

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

    pc = shared_state.priority_config
    priority_info = {}
    if pc:
        nightsperre = False
        if _is_nachtsperre_aktiv:
            nightsperre = _is_nachtsperre_aktiv(pc, datetime.now(shared_state.local_tz))
        priority_info = {
            "beschreibung": pc.beschreibung,
            "wp_leistung": pc.wp.leistung_watt,
            "zyklus_interval": pc.zyklus.interval_minuten,
            "zyklus_min_laufzeit": pc.zyklus.mindestlaufzeit_minuten,
            "zyklus_min_pause": pc.zyklus.mindestpausenzeit_minuten,
            "nachtsperre_aktiv": nightsperre,
            "nachtsperre_start": pc.sicherheit.nachtsperre_start,
            "nachtsperre_ende": pc.sicherheit.nachtsperre_ende,
            "sicherheit_max": pc.sicherheit.max_temp_c,
            "sicherheit_ueberhitzung": pc.sicherheit.ueberhitzung_c,
            "sicherheit_notfall": pc.sicherheit.notfall_c,
            "regeln": [],
        }
        # PV-Regeln
        for pv in pc.pv_regeln:
            priority_info["regeln"].append({
                "name": pv.name, "typ": "pv", "prio": pv.prioritaet,
                "schwellwert": pv.pv_schwelle_watt,
                "sensor": pv.temperaturfuehler,
                "ein": pv.einschalten_bei_c,
                "aus": pv.ausschalten_bei_c,
            })
        # Komfort
        priority_info["regeln"].append({
            "name": "Komfort", "typ": "komfort", "prio": pc.komfort.prioritaet,
            "notfall": pc.komfort.notfall_einschalten_bei_c,
            "min_pvid": pc.komfort.min_pv_fuer_komfort_watt,
            "ein": pc.komfort.komfort_einschalten_bei_c,
            "aus": pc.komfort.ausschalten_bei_c,
        })
        # Zeitfenster
        priority_info["regeln"].append({
            "name": "Zeitfenster", "typ": "zeitfenster", "prio": pc.zeitfenster.prioritaet,
            "start": pc.zeitfenster.start_uhr,
            "ende": pc.zeitfenster.ende_uhr,
            "ein": pc.zeitfenster.max_temp_fuer_einschalten_c,
            "aus": pc.zeitfenster.max_temp_fuer_einschalten_c,
            "min_pv": pc.zeitfenster.min_pv_watt,
        })
                # Abweichung
        priority_info["regeln"].append({
            "name": "Abweichung", "typ": "abweichung", "prio": pc.abweichung.prioritaet,
            "soll": pc.abweichung.solltemperatur_c,
            "sensor": pc.abweichung.temperaturfuehler,
            "ein": pc.abweichung.einschalten_bei_abweichung_k,
            "aus": pc.abweichung.ausschalten_bei_abweichung_k,
        })
        # Wochenende
        priority_info["regeln"].append({
            "name": "Wochenende", "typ": "wochenende", "prio": pc.wochenende.prioritaet,
            "aktiv": pc.wochenende.aktiv,
            "fruehestens_uhr": pc.wochenende.fruehestens_uhr,
        })
        # MindestTemp-Garantien
        for _mt in pc.mindest_temp.eintraege:
            priority_info["regeln"].append({
                "name": f"MinTemp-{_mt.name}", "typ": "mindesttemp",
                "prio": pc.mindest_temp.prioritaet,
                "sensor": _mt.temperaturfuehler,
                "min_c": _mt.min_temp_c,
                "start": _mt.start_uhr,
                "ende": _mt.ende_uhr,
            })
        # Batterie-Regel
        priority_info["regeln"].append({
            "name": "Batterie", "typ": "batterie", "prio": pc.batterie.prioritaet,
            "aktiv": pc.batterie.aktiv,
            "min_soc": pc.batterie.min_soc_prozent,
            "max_netzbezug_w": pc.batterie.max_netzbezug_watt,
        })
        # Forecast-Regel (Prognose)
        priority_info["regeln"].append({
            "name": "Forecast", "typ": "forecast", "prio": pc.forecast.prioritaet,
            "aktiv": pc.forecast.aktiv,
            "vorheiz_c": pc.forecast.t_vorheiz_ab_c,
            "max_c": pc.forecast.tmax_c,
            "schlecht_wmq": pc.forecast.fc_schwelle_niedrig_wh,
            "gut_wmq": pc.forecast.fc_schwelle_hoch_wh,
        })
        # AdaptivePV-Regel
        priority_info["regeln"].append({
            "name": "AdaptivePV", "typ": "adaptivepv", "prio": pc.adaptive_pv.prioritaet,
            "aktiv": pc.adaptive_pv.aktiv,
            "basis_w": pc.adaptive_pv.base_threshold_watt,
            "sensor": pc.adaptive_pv.temperaturfuehler,
            "max_c": pc.adaptive_pv.tmax_c,
        })
        # CalcStart-Regel
        priority_info["regeln"].append({
            "name": "CalcStart", "typ": "calcstart", "prio": pc.calculated_start.prioritaet,
            "aktiv": pc.calculated_start.aktiv,
            "soll_c": pc.calculated_start.solltemperatur_c,
            "ziel_uhr": pc.calculated_start.target_uhr,
            "max_c": pc.calculated_start.tmax_c,
        })

    # Regel-Ergebnisse (Entscheidungen) aus State
    regel_ergebnisse = []
    if shared_state.control.alle_ergebnisse:
        for e in shared_state.control.alle_ergebnisse:
            regel_ergebnisse.append({
                "name": e.name,
                "prio": e.prioritaet,
                "aktiv": e.aktiv,
                "einschalten": e.einschalten,  # True/False/None
                "grund": e.grund,
            })

    return {
        "temperatures": {
            "oben": shared_state.sensors.t_oben,
            "mittig": shared_state.sensors.t_mittig,
            "unten": shared_state.sensors.t_unten,
            "verdampfer": shared_state.sensors.t_verd,
            "boiler": shared_state.sensors.t_boiler
        },
        "compressor": {
            "status": "EIN" if shared_state.control.kompressor_ein else "AUS",
            "runtime_current": str(shared_state.stats.current_runtime).split('.')[0] if shared_state.control.kompressor_ein else "0:00:00",
            "runtime_today": str(shared_state.stats.total_runtime_today).split('.')[0]
        },
        "setpoints": {
            "einschaltpunkt": shared_state.control.aktueller_einschaltpunkt,
            "ausschaltpunkt": shared_state.control.aktueller_ausschaltpunkt,
            "sicherheits_temp": shared_state.sicherheits_temp,
            "verdampfertemperatur": shared_state.verdampfertemperatur
        },
        "mode": build_mode_payload(shared_state),
        "energy": {
            "battery_power": shared_state.solar.batpower,
            "soc": shared_state.solar.soc,
            "feed_in": shared_state.solar.feedinpower,
            "ac_power": getattr(shared_state.solar, 'acpower', None),
            "forecast_today": getattr(shared_state.solar, 'forecast_today', None),
            "forecast_tomorrow": getattr(shared_state.solar, 'forecast_tomorrow', None),
            "forecast_day2": getattr(shared_state.solar, 'forecast_day2', None),
            "sunrise": getattr(shared_state.solar, 'sunrise_today', ''),
            "sunset": getattr(shared_state.solar, 'sunset_today', ''),
        },
        "learning": shared_state.learning_engine.get_info() if hasattr(shared_state, 'learning_engine') and shared_state.learning_engine else {
            "heat_rates": {"winter": {"avg": 3.0, "count": 0}, "transition": {"avg": 3.0, "count": 0}, "summer": {"avg": 3.0, "count": 0}},
            "learned_target_hour": 17.0,
            "target_hour_samples": 0,
            "total_cycles": 0,
            "total_usage_events": 0,
        },
                "system": {
            "exclusion_reason": shared_state.control.ausschluss_grund or "",
            "last_update": datetime.now().strftime("%H:%M:%S"),
        },
        "priority": priority_info,
        "regel_ergebnisse": regel_ergebnisse,
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
    except ValueError as e:
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
        mode = cmd.params.get("mode") if cmd.params else None
        if mode == "bademodus":
            shared_state.bademodus_aktiv = cmd.params.get("active", False) if cmd.params else False
            return {"status": "success", "message": f"Bademodus set to {shared_state.bademodus_aktiv}"}
        elif mode == "urlaubsmodus":
            shared_state.urlaubsmodus_aktiv = cmd.params.get("active", False) if cmd.params else False
            return {"status": "success", "message": f"Urlaubsmodus set to {shared_state.urlaubsmodus_aktiv}"}

    raise HTTPException(status_code=400, detail="Unknown command")



@app.get("/debug/csv")
def debug_csv():
    """Debug: Zeigt CSV-Status und Daten an."""
    import os as _os
    csv_path = HEIZUNGSDATEN_CSV
    result = {
        "csv_path": csv_path,
        "csv_exists": _os.path.exists(csv_path),
    }
    if result["csv_exists"]:
        result["size_bytes"] = _os.path.getsize(csv_path)
        try:
            import pandas as _pd
            df = _pd.read_csv(csv_path)
            result["rows"] = len(df)
            result["columns"] = list(df.columns)
            result["first_timestamp"] = str(df.iloc[0]["Zeitstempel"]) if len(df) > 0 else None
            result["last_timestamp"] = str(df.iloc[-1]["Zeitstempel"]) if len(df) > 0 else None
            result["column_types"] = {str(k): str(v) for k, v in df.dtypes.items()}
        except Exception as e:
            result["read_error"] = str(e)
    return result

@app.get("/history")
def get_history(hours: int = Query(default=24, ge=1, le=168)):
    """Get historical data from CSV. Hours must be between 1 and 168 (7 days)."""
    import os
    import pandas as pd
    
    csv_path = HEIZUNGSDATEN_CSV
    if not os.path.exists(csv_path):
        raise HTTPException(status_code=404, detail="No historical data available")
    
    try:
        # Nur die letzten ~25k Zeilen lesen (126k komplett parsen = timeout auf Pi)
        from collections import deque
        MAX_ROWS = 25000
        with open(csv_path, "r", encoding="utf-8") as _f:
            _header = _f.readline()
            _tail = deque(_f, maxlen=MAX_ROWS)
        _lines = [_header] + list(_tail)
        df = pd.read_csv(io.StringIO("".join(_lines)))
        # Gemischte Formate vektorisiert (KEIN apply/Lambda!)
        _ts_raw = df['Zeitstempel']
        _ts_num = pd.to_numeric(_ts_raw, errors='coerce')
        _is_num = _ts_num.notna()
        if _is_num.any():
            df.loc[_is_num, 'Zeitstempel'] = pd.to_datetime(
                _ts_num[_is_num], unit='D', origin='1899-12-30')
        if (~_is_num).any():
            df.loc[~_is_num, 'Zeitstempel'] = pd.to_datetime(
                _ts_raw[~_is_num].astype(str).str.strip(), errors='coerce')
        df['Zeitstempel'] = pd.to_datetime(df['Zeitstempel'], errors='coerce')
        cutoff = datetime.now() - pd.Timedelta(hours=hours)
        df = df[df['Zeitstempel'] >= cutoff]
        
        # Convert to JSON-friendly format
        data = []
        for _, row in df.iterrows():
            entry = {
                "timestamp": row['Zeitstempel'].strftime("%Y-%m-%d %H:%M:%S"),
                "t_oben": row['T_Oben'] if pd.notna(row.get('T_Oben')) else None,
                "t_mittig": row['T_Mittig'] if pd.notna(row.get('T_Mittig')) else None,
                "t_unten": row['T_Unten'] if pd.notna(row.get('T_Unten')) else None,
                "t_verd": row['T_Verd'] if pd.notna(row.get('T_Verd')) else None,
                "kompressor": row.get('Kompressor', ''),
            }
            # Sollwerte aus CSV (falls vorhanden)
            if 'Einschaltpunkt' in df.columns:
                val = row.get('Einschaltpunkt')
                entry["einschaltpunkt"] = float(val) if pd.notna(val) and str(val).strip() != '' else None
            else:
                entry["einschaltpunkt"] = None
            if 'Ausschaltpunkt' in df.columns:
                val = row.get('Ausschaltpunkt')
                entry["ausschaltpunkt"] = float(val) if pd.notna(val) and str(val).strip() != '' else None
            else:
                entry["ausschaltpunkt"] = None
            data.append(entry)
        
        return {"data": data, "count": len(data)}
    except Exception as e:
        logging.error(f"Error reading history: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")