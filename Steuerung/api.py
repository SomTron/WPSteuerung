from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from typing import Optional, Dict, Any
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
from datetime import datetime, timedelta
import asyncio
from utils_history import read_history_data

# ── API-Key Authentifizierung ──────────────────────────────────────────────
# Priorität: [API] API_KEY in config.ini > Umgebungsvariable WP_API_KEY
# Wenn keins gesetzt ist, ist die Authentifizierung deaktiviert.
_ENV_API_KEY = os.environ.get("WP_API_KEY", "")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def _get_active_api_key(request: Request) -> str:
    """Liest den API-Key: erst aus config (shared_state), dann Umgebungsvariable."""
    shared_state = getattr(request.app.state, "shared_state", None)
    key_val = ""
    if shared_state:
        key_val = getattr(getattr(shared_state.config, "API", None), "API_KEY", "")
    
    if not key_val:
        key_val = _ENV_API_KEY
    
    # Bereinigen: Leerzeichen und Anführungszeichen (falls in .ini oder env gesetzt)
    return key_val.strip(" '\"")

async def verify_api_key(request: Request, key: str = Depends(api_key_header)):
    """Prüft den API-Key (Header oder Query-Parameter)."""
    active_key = _get_active_api_key(request)
    if not active_key:
        return

    # Fallback: Falls Header fehlt, in Query-Params suchen
    query_key = request.query_params.get("key")
    used_key = key or query_key

    if used_key != active_key:
        masked_got = (used_key[:2] + "..." + used_key[-2:]) if used_key and len(used_key) > 4 else "***"
        masked_exp = (active_key[:2] + "..." + active_key[-2:]) if active_key and len(active_key) > 4 else "***"
        
        # Logge Header-Namen für Diagnose (ohne Werte!)
        header_names = ", ".join(request.headers.keys())
        logging.warning(
            f"API-Key Auth fehlgeschlagen. Erwartet: '{masked_exp}', Erhalten: '{masked_got}' "
            f"(Quelle: {'Header' if key else 'Query' if query_key else 'Keine'}). "
            f"Vorhandene Header: {header_names}"
        )
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key")

# ── Data Models ────────────────────────────────────────────────────────────
class ConfigUpdate(BaseModel):
    section: str
    key: str
    value: str

class ControlCommand(BaseModel):
    command: str  # "force_on", "force_off", "set_mode"
    params: Optional[Dict[str, Any]] = None

# ── App Setup ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="WPSteuerung API",
    description="API for Heat Pump Control Web App",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _safe(obj, attr, default=None):
    """Sicherer Attributzugriff mit Fallback-Wert."""
    return getattr(obj, attr, default)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(verify_api_key)])
def get_status(request: Request):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")

    sensors = _safe(shared_state, "sensors")
    control = _safe(shared_state, "control")
    stats   = _safe(shared_state, "stats")
    solar   = _safe(shared_state, "solar")

    bat_cap = _safe(shared_state, "battery_capacity", 0)
    soc_val = _safe(solar, "soc", 0) if solar else 0
    bat_kwh = (bat_cap * soc_val / 100.0) if isinstance(bat_cap, (int, float)) and bat_cap > 0 else 0

    kompressor_ein = _safe(control, "kompressor_ein", False)

    # PV-Plan Klassifizierung berechnen
    today = _safe(solar, "forecast_today")
    tomorrow = _safe(solar, "forecast_tomorrow")
    low_thr = _safe(solar, "pv_threshold_low_kwh")
    high_thr = _safe(solar, "pv_threshold_high_kwh")

    def _classify_pv(val, low, high):
        if val is None or low is None or high is None:
            return None
        if val < low:
            return "LOW"
        if val > high:
            return "HIGH"
        return "MID"

    pv_plan_today = _classify_pv(today, low_thr, high_thr)
    pv_plan_tomorrow = _classify_pv(tomorrow, low_thr, high_thr)

    # Nächste Umschaltung schätzen unter Berücksichtigung der PV-Plan-Logik
    t_mittig = _safe(sensors, "t_mittig")
    t_unten = _safe(sensors, "t_unten")
    active_sensor = _safe(control, "active_rule_sensor", "Mittig")
    regelfuehler = t_mittig if active_sensor == "Mittig" else t_unten

    next_switch = None
    next_switch_target = None
    next_switch_minutes = None
    next_switch_reason = None

    if regelfuehler is not None:
        einschaltpunkt = _safe(control, "aktueller_einschaltpunkt")
        ausschaltpunkt = _safe(control, "aktueller_ausschaltpunkt")
        rate = getattr(control, "learned_heating_rate", 2.0) if hasattr(control, "learned_heating_rate") else 2.0

        if kompressor_ein:
            # Schätze wann AUS
            delta = (ausschaltpunkt - regelfuehler) if ausschaltpunkt and regelfuehler else None
            if delta is not None and delta > 0 and rate > 0:
                minutes = int((delta / rate) * 60)
                next_switch = "AUS" if minutes <= 480 else (">8h" if minutes > 480 else None)
                next_switch_target = ausschaltpunkt
                next_switch_minutes = minutes if minutes <= 480 else None
                next_switch_reason = "Temperatur erreicht"
            elif delta is not None and delta <= 0:
                next_switch = "AUS"
                next_switch_minutes = 0
                next_switch_target = ausschaltpunkt
                next_switch_reason = "Ziel erreicht"
        else:
            # Schätze wann EIN - unter Berücksichtigung von PV-Plan, Strategie und Deadline
            # Sicherer Zugriff auf Zeitzone
            import pytz
            local_tz = getattr(shared_state, "local_tz", None)
            if local_tz is None or not isinstance(local_tz, pytz.BaseTzInfo):
                local_tz = pytz.timezone("Europe/Berlin")
            now = datetime.now(local_tz)

            pv_strategy = _safe(control, "pv_strategy", "balanced")
            heating_deadline = _safe(control, "heating_deadline")
            solar_ueberschuss_aktiv = _safe(control, "solar_ueberschuss_aktiv", False)
            modus = _safe(control, "previous_modus", "")

            # Prüfen ob PV-Plan ein Einschalten erlaubt (heute/morgen beide HIGH = Solarüberschuss möglich)
            pv_plan_erlaubt_solar = (pv_plan_today == "HIGH" and pv_plan_tomorrow == "HIGH")

            # Basis: Temperatur-basierte Einschaltzeit (Abkühlrate ~1.0 °C/h)
            delta_temp = (regelfuehler - einschaltpunkt) if einschaltpunkt and regelfuehler else None

            if delta_temp is not None and delta_temp <= 0:
                # Eigentlich sollte jetzt eingeschaltet werden
                if _safe(control, "blocking_reason"):
                    next_switch = "EIN (blockiert)"
                    next_switch_minutes = 0
                    next_switch_target = einschaltpunkt
                    next_switch_reason = _safe(control, "blocking_reason")[:40]
                else:
                    next_switch = "EIN"
                    next_switch_minutes = 0
                    next_switch_target = einschaltpunkt
                    next_switch_reason = "unter Einschaltpunkt"
            elif delta_temp is not None and delta_temp > 0:
                # Temperatur muss noch sinken - prüfe strategische Faktoren

                # 1. Wenn Deadline existiert und bevor Deadline -> Einschalten zur Deadline
                if heating_deadline is not None and isinstance(heating_deadline, datetime) and now < heating_deadline:
                    minutes_to_deadline = int((heating_deadline - now).total_seconds() / 60)
                    # Prüfen ob Temperatur bis Deadline voraussichtlich erreicht wird
                    cooling_rate = 1.0
                    temp_drop_by_deadline = (minutes_to_deadline / 60) * cooling_rate
                    expected_temp_at_deadline = regelfuehler - temp_drop_by_deadline

                    if expected_temp_at_deadline <= einschaltpunkt:
                        next_switch = "EIN"
                        next_switch_minutes = minutes_to_deadline if minutes_to_deadline <= 480 else None
                        next_switch_target = einschaltpunkt
                        next_switch_reason = f"Deadline ({heating_deadline.strftime('%H:%M')})"
                    else:
                        # Temperatur reicht bis Deadline nicht -> normale Abkühlung
                        minutes = int((delta_temp / cooling_rate) * 60)
                        next_switch = "EIN" if minutes <= 480 else (">8h" if minutes > 480 else None)
                        next_switch_minutes = minutes if minutes <= 480 else None
                        next_switch_target = einschaltpunkt
                        next_switch_reason = "Abkühlung + Deadline"
                else:
                    # Keine Deadline oder schon vorbei -> normale Abkühlung
                    cooling_rate = 1.0
                    minutes = int((delta_temp / cooling_rate) * 60)

                    # 2. PV-Plan-basierte Verzögerung: Wenn PV-Plan nicht HIGH/HIGH, warte auf besseres Fenster
                    if not pv_plan_erlaubt_solar and "Solar" not in modus:
                        # PV-Plan blockiert Solarüberschuss -> nächstes mögliches Fenster prüfen
                        # Wenn morgen HIGH: Verschiebe auf morgen (Solarfenster)
                        if pv_plan_tomorrow == "HIGH":
                            # Schätze Zeit bis morgen Solarfenster (ca. 10:00-14:00)
                            tomorrow_solar_start = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
                            minutes_to_tomorrow = int((tomorrow_solar_start - now).total_seconds() / 60)
                            minutes = max(minutes, minutes_to_tomorrow)
                            next_switch_reason = "PV-Plan: Warte auf morgen"
                        else:
                            next_switch_reason = "PV-Plan: kein HIGH"

                    next_switch = "EIN" if minutes <= 480 else (">8h" if minutes > 480 else None)
                    next_switch_minutes = minutes if minutes <= 480 else None
                    next_switch_target = einschaltpunkt
                    if not next_switch_reason:
                        next_switch_reason = "Abkühlung"

    return {
        "temperatures": {
            "oben":      _safe(sensors, "t_oben"),
            "mittig":    _safe(sensors, "t_mittig"),
            "unten":     _safe(sensors, "t_unten"),
            "verdampfer":_safe(sensors, "t_verd"),
            "vorlauf":   _safe(sensors, "t_vorlauf"),
            "boiler":    _safe(sensors, "t_boiler"),
        },
        "compressor": {
            "status":          "EIN" if kompressor_ein else "AUS",
            "runtime_current": str(_safe(stats, "last_runtime", "0:00:00")).split('.')[0] if kompressor_ein else "0:00:00",
            "runtime_today":   str(_safe(stats, "total_runtime_today", "0:00:00")).split('.')[0],
            "activation_reason": _safe(control, "activation_reason"),
            "blocking_reason":   _safe(control, "blocking_reason"),
            "pv_strategy":       _safe(control, "pv_strategy"),
            "heating_deadline":  _safe(control, "heating_deadline").strftime("%H:%M") if _safe(control, "heating_deadline") else None,
            "estimated_runtime": _safe(control, "estimated_runtime_minutes"),
            "next_switch":       next_switch,
            "next_switch_minutes": next_switch_minutes if 'next_switch_minutes' in locals() else None,
            "next_switch_target":  next_switch_target,
            "next_switch_reason":  next_switch_reason if 'next_switch_reason' in locals() else None,
        },
        "setpoints": {
            "einschaltpunkt":    _safe(control, "aktueller_einschaltpunkt"),
            "ausschaltpunkt":    _safe(control, "aktueller_ausschaltpunkt"),
            "sicherheits_temp":  _safe(shared_state, "sicherheits_temp"),
            "verdampfertemperatur": _safe(shared_state, "verdampfertemperatur"),
            "active_sensor":     _safe(control, "active_rule_sensor"),
        },
        "mode": {
            "current":      _safe(control, "previous_modus"),
            "solar_active": _safe(control, "solar_ueberschuss_aktiv", False),
            "holiday_active": _safe(shared_state, "urlaubsmodus_aktiv", False),
            "bath_active":   _safe(shared_state, "bademodus_aktiv", False),
        },
        "energy": {
            "battery_power":      _safe(solar, "batpower", 0),
            "soc":                soc_val,
            "feed_in":            _safe(solar, "feedinpower", 0),
            "pv_power":           _safe(solar, "acpower", 0),
            "battery_capacity_kwh": bat_kwh,
        },
        "pv_plan": {
            "today":        pv_plan_today,
            "tomorrow":     pv_plan_tomorrow,
            "threshold_low":  low_thr,
            "threshold_high": high_thr,
            "forecast_today_kwh":    today,
            "forecast_tomorrow_kwh": tomorrow,
        },
        "forecast": {
            "today":    today,
            "tomorrow": tomorrow,
            "sunrise":  _safe(solar, "sunrise_today"),
            "sunset":   _safe(solar, "sunset_today"),
            "threshold_low":  low_thr,
            "threshold_high": high_thr,
        },
        "system": {
            "exclusion_reason": _safe(control, "ausschluss_grund"),
            "last_update":      datetime.now().strftime("%H:%M:%S"),
            "vpn_ip":           _safe(shared_state, "vpn_ip"),
        },
    }


@app.post("/config", dependencies=[Depends(verify_api_key)])
def update_config(request: Request, config: ConfigUpdate):
    shared_state = getattr(request.app.state, "shared_state", None)
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")

    section_obj = getattr(shared_state.config, config.section, None)
    if not section_obj:
        raise HTTPException(status_code=404, detail=f"Section {config.section} not found")

    if not hasattr(section_obj, config.key):
        raise HTTPException(status_code=404, detail=f"Key {config.key} not found in section {config.section}")

    try:
        current_value = getattr(section_obj, config.key)
        new_value = config.value

        if isinstance(current_value, bool):
            new_value = config.value.lower() == 'true'
        elif isinstance(current_value, int):
            new_value = int(config.value)
        elif isinstance(current_value, float):
            new_value = float(config.value)

        setattr(section_obj, config.key, new_value)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid value for {config.key}: {str(e)}")

    return {"status": "success", "message": f"Updated {config.section}.{config.key} to {new_value}"}


@app.post("/control", dependencies=[Depends(verify_api_key)])
async def control_system(request: Request, cmd: ControlCommand):
    shared_state  = getattr(request.app.state, "shared_state", None)
    control_funcs = getattr(request.app.state, "control_funcs", None)
    if not shared_state or not control_funcs:
        raise HTTPException(status_code=503, detail="System not initialized")

    if cmd.command == "force_on":
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](True, force=True)
            return {"status": "success", "message": "Compressor forced ON"}

    elif cmd.command == "force_off":
        if "set_kompressor" in control_funcs:
            await control_funcs["set_kompressor"](False, force=True)
            return {"status": "success", "message": "Compressor forced OFF"}

    elif cmd.command == "set_mode":
        mode = cmd.params.get("mode") if cmd.params else None
        if mode == "bademodus":
            shared_state.bademodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Bademodus set to {shared_state.bademodus_aktiv}"}
        elif mode == "urlaubsmodus":
            shared_state.urlaubsmodus_aktiv = cmd.params.get("active", False)
            return {"status": "success", "message": f"Urlaubsmodus set to {shared_state.urlaubsmodus_aktiv}"}

    raise HTTPException(status_code=400, detail="Unknown command")


@app.get("/history")
async def get_history(hours: int = 24):
    """Liefert historische Daten asynchron aus der CSV-Datei."""
    from utils import HEIZUNGSDATEN_CSV
    csv_path = HEIZUNGSDATEN_CSV
    try:
        result = await asyncio.to_thread(read_history_data, csv_path, hours)

        if not result["data"] and not os.path.exists(csv_path):
            raise HTTPException(status_code=404, detail="No historical data available")

        return result
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error reading history in API: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")


# ── Static Frontend ────────────────────────────────────────────────────────
frontend_path = os.path.join(os.path.dirname(__file__), "frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    logging.warning(f"Frontend directory not found at {frontend_path}")