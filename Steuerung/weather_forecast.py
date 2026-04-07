import logging
import aiohttp
import os
import aiofiles
from datetime import datetime, timedelta
import pytz
import csv
from collections import OrderedDict
import asyncio

def _compute_pv_geometry(config):
    """
    Liefert (effektiver_tilt_deg, total_area_m2, panel_efficiency).

    PANEL_GROUPS-Format:
      'anzahl,länge_m,breite_m,tilt_deg,azimuth_deg; ...'
    Beispiel:
      '12,1.722,1.134,5,90;24,1.722,1.134,30,60'
    """
    default_tilt = config.Wetterprognose.TILT if config else 30
    try:
        panel_eff = getattr(config.Wetterprognose, "PANEL_EFFICIENCY", 0.20) if config else 0.20
    except Exception:
        panel_eff = 0.20

    try:
        panel_str = getattr(config.Wetterprognose, "PANEL_GROUPS", "") if config else ""
    except Exception:
        panel_str = ""

    if not panel_str:
        return default_tilt, 0.0, panel_eff

    total_area = 0.0
    weighted_tilt_sum = 0.0

    for group in panel_str.split(";"):
        group = group.strip()
        if not group:
            continue
        parts = [p.strip() for p in group.split(",")]
        if len(parts) < 4:
            logging.warning(f"Ignoriere ungueltige PANEL_GROUPS-Gruppe: '{group}'")
            continue
        try:
            count = float(parts[0])
            length_m = float(parts[1])
            width_m = float(parts[2])
            tilt_deg = float(parts[3])
            # azimuth_deg = float(parts[4])  # aktuell nicht verwendet, aber im Format vorgesehen

            area = count * length_m * width_m
            if area <= 0:
                continue

            total_area += area
            weighted_tilt_sum += area * tilt_deg
        except ValueError:
            logging.warning(f"Konnte PANEL_GROUPS-Eintrag nicht parsen: '{group}'")
            continue

    effective_tilt = default_tilt
    if total_area > 0:
        effective_tilt = weighted_tilt_sum / total_area

    return effective_tilt, total_area, panel_eff

def _percentile(sorted_vals, p: float) -> float:
    if not sorted_vals:
        raise ValueError("empty data")
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 1:
        return float(sorted_vals[-1])
    idx = int(round(p * (len(sorted_vals) - 1)))
    return float(sorted_vals[max(0, min(idx, len(sorted_vals) - 1))])

def compute_adaptive_pv_thresholds_from_csv(
    csv_path: str,
    lookback_days: int = 45,
    low_percentile: float = 0.25,
    high_percentile: float = 0.75,
    min_days: int = 10,
):
    """
    Liest `sonnen_prognose.csv` und berechnet adaptive LOW/HIGH Schwellen (kWh/Tag).

    Robustheit:
    - Unterstützt alte Header (Today_kWh/Tomorrow_kWh) und neue (Today_PV_kWh/Tomorrow_PV_kWh).
    - Nutzt pro Kalendertag den letzten geloggten Wert (Forecast wird i.d.R. mehrfach pro Tag aktualisiert).

    Returns: (low_kwh, high_kwh) oder (None, None) wenn zu wenig Daten vorhanden sind.
    """
    if lookback_days <= 0:
        return None, None

    if not os.path.exists(csv_path):
        return None, None

    tz = pytz.timezone("Europe/Berlin")
    cutoff_date = (datetime.now(tz) - timedelta(days=lookback_days)).date()

    # OrderedDict: date -> value (last wins, preserve insertion order)
    daily_vals = OrderedDict()

    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return None, None

            # Prefer PV-kWh columns if present, fallback to older radiation columns if needed
            today_col = None
            for cand in ("Today_PV_kWh", "Today_kWh", "Today_Rad_kWh_m2"):
                if cand in reader.fieldnames:
                    today_col = cand
                    break

            if today_col is None:
                return None, None

            for row in reader:
                ts = (row.get("Zeitstempel") or "").strip()
                if len(ts) < 10:
                    continue
                day_str = ts[:10]
                try:
                    day = datetime.strptime(day_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if day < cutoff_date:
                    continue

                raw_val = (row.get(today_col) or "").strip()
                if not raw_val or raw_val.lower() == "n/a":
                    continue
                try:
                    val = float(raw_val)
                except ValueError:
                    continue
                if val < 0:
                    continue

                daily_vals[day] = val  # last entry of the day wins
    except Exception as e:
        logging.error(f"Fehler beim Lesen von {csv_path} für adaptive PV-Schwellen: {e}")
        return None, None

    if len(daily_vals) < min_days:
        return None, None

    vals = sorted(daily_vals.values())
    low = _percentile(vals, low_percentile)
    high = _percentile(vals, high_percentile)

    # Ensure sane ordering
    if high < low:
        low, high = high, low

    return low, high

async def get_solar_forecast(session: aiohttp.ClientSession, config=None):
    """
    Fetches solar radiation forecast from Open-Meteo with retry logic.
    Returns:
      (rad_today_m2, rad_tomorrow_m2,
       pv_today_kwh, pv_tomorrow_kwh,
       sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow)

    rad_* in kWh/m², PV-Werte als geschätzte Anlagenenergie in kWh.
    """
    max_retries = 3
    base_delay = 5  # Sekunden
    
    for attempt in range(max_retries):
        success, result = await _fetch_solar_forecast_once(session, config)
        
        if success:
            return result
        
        # Wenn wir noch Retries haben, warte mit Exponential Backoff
        if attempt < max_retries - 1:
            # Bei Rate Limiting (429) länger warten
            error_type = result.get("error_type", "unknown") if result else "unknown"
            if error_type == "rate_limit":
                delay = base_delay * (2 ** attempt) * 3  # 15s, 30s, 60s
            elif error_type == "server_error":
                delay = base_delay * (2 ** attempt) * 2  # 10s, 20s, 40s
            else:
                delay = base_delay * (2 ** attempt)  # 5s, 10s, 20s
            
            logging.warning(
                f"Solar forecast attempt {attempt + 1}/{max_retries} failed, "
                f"retrying in {delay}s..."
            )
            await asyncio.sleep(delay)
        else:
            logging.error(
                f"Solar forecast failed after {max_retries} attempts. "
                f"Using fallback values."
            )
    
    # Alle Versuche fehlgeschlagen
    return None, None, None, None, None, None, None, None


async def _fetch_solar_forecast_once(session: aiohttp.ClientSession, config=None):
    """
    Einzelner Fetch-Versuch. Gibt (success, result_tuple) zurück.
    """
    # Use config values or defaults
    lat = config.Wetterprognose.LATITUDE if config else 46.7142
    lon = config.Wetterprognose.LONGITUDE if config else 13.6361
    tilt, total_area_m2, panel_eff = _compute_pv_geometry(config)
    
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "sunrise,sunset",
        "hourly": "direct_radiation,diffuse_radiation",
        "timezone": "Europe/Berlin",
        "forecast_days": 3,
        "tilt": tilt
    }
    
    try:
        async with session.get(url, params=params, timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                
                # Extract Hourly Data (Radiation)
                hourly = data.get("hourly", {})
                times = hourly.get("time", [])
                direct = hourly.get("direct_radiation", [])
                diffuse = hourly.get("diffuse_radiation", [])
                
                if not times or not direct or not diffuse:
                    logging.warning("Open-Meteo API returned empty hourly data.")
                    return None, None, None, None, None, None, None, None
                
                total_radiation = [dir + diff for dir, diff in zip(direct, diffuse)]
                daily_totals = {}
                for t_str, rad in zip(times, total_radiation):
                    date_str = t_str.split("T")[0]
                    daily_totals[date_str] = daily_totals.get(date_str, 0) + rad
                
                for date in daily_totals:
                    daily_totals[date] = daily_totals[date] / 1000.0
                
                # Extract Daily Data (Sunrise/Sunset)
                daily = data.get("daily", {})
                daily_times = daily.get("time", [])
                sunrises = daily.get("sunrise", [])
                sunsets = daily.get("sunset", [])
                
                sun_data = {}
                for d_str, sr, ss in zip(daily_times, sunrises, sunsets):
                    sun_data[d_str] = {
                        "sunrise": sr.split("T")[1] if "T" in sr else None,
                        "sunset": ss.split("T")[1] if "T" in ss else None
                    }
                
                tz = pytz.timezone("Europe/Berlin")
                now = datetime.now(tz)
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                
                rad_today = daily_totals.get(today_str)
                rad_tomorrow = daily_totals.get(tomorrow_str)
                
                sunrise_today = sun_data.get(today_str, {}).get("sunrise")
                sunset_today = sun_data.get(today_str, {}).get("sunset")
                sunrise_tomorrow = sun_data.get(tomorrow_str, {}).get("sunrise")
                sunset_tomorrow = sun_data.get(tomorrow_str, {}).get("sunset")

                # PV-Gesamtenergie abschätzen (kWh)
                pv_today = None
                pv_tomorrow = None
                if isinstance(rad_today, (int, float)) and total_area_m2 > 0 and panel_eff > 0:
                    pv_today = rad_today * total_area_m2 * panel_eff
                if isinstance(rad_tomorrow, (int, float)) and total_area_m2 > 0 and panel_eff > 0:
                    pv_tomorrow = rad_tomorrow * total_area_m2 * panel_eff

                # Sichere Formatierung auch bei None-Werten
                def _fmt_rad(val):
                    return f"{val:.2f}" if isinstance(val, (int, float)) else "n/a"

                def _fmt_pv(val):
                    return f"{val:.1f}" if isinstance(val, (int, float)) else "n/a"

                logging.info(
                    "Solar forecast updated: "
                    f"RadToday={_fmt_rad(rad_today)} kWh/m², RadTomorrow={_fmt_rad(rad_tomorrow)} kWh/m², "
                    f"PVToday={_fmt_pv(pv_today)} kWh, PVMorrow={_fmt_pv(pv_tomorrow)} kWh "
                    f"({sunrise_today}-{sunset_today})"
                )

                # Log to dedicated CSV
                await log_forecast_to_csv(
                    rad_today, rad_tomorrow,
                    pv_today, pv_tomorrow,
                    sunrise_today, sunset_today,
                    sunrise_tomorrow, sunset_tomorrow
                )

                result = (
                    rad_today, rad_tomorrow,
                    pv_today, pv_tomorrow,
                    sunrise_today, sunset_today,
                    sunrise_tomorrow, sunset_tomorrow
                )
                return True, result
            else:
                error_text = await response.text()
                logging.error(f"Error fetching solar forecast: Status {response.status}, Details: {error_text}")
                
                # Fehler-Typ klassifizieren für Backoff-Strategie
                if response.status == 429:
                    return False, {"error_type": "rate_limit"}
                elif response.status >= 500:
                    return False, {"error_type": "server_error"}
                else:
                    return False, {"error_type": "client_error"}
                    
    except aiohttp.ClientError as e:
        # Netzwerk-Fehler (Connection reset, timeout, etc.)
        error_type = "network_error"
        if "Connection reset" in str(e) or "timeout" in str(e).lower():
            error_type = "network_error"
        
        logging.error(f"Network error in get_solar_forecast: {e}")
        return False, {"error_type": error_type}
    except Exception as e:
        logging.error(f"Unexpected error in get_solar_forecast: {e}")
        return False, {"error_type": "unknown"}
    
    # success path - wird unten im Code erreicht bei response.status == 200

async def log_forecast_to_csv(rad_today, rad_tomorrow, pv_today, pv_tomorrow, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow):
    """Logs the forecast results to a separate CSV file."""
    csv_file = "sonnen_prognose.csv"
    try:
        header = (
            "Zeitstempel,"
            "Today_Rad_kWh_m2,Tomorrow_Rad_kWh_m2,"
            "Today_PV_kWh,Tomorrow_PV_kWh,"
            "Sunrise_Today,Sunset_Today,Sunrise_Tomorrow,Sunset_Tomorrow\n"
        )
        file_exists = os.path.exists(csv_file)
        
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            if not file_exists:
                await f.write(header)
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            def _fmt_csv_rad(val):
                return f"{val:.2f}" if isinstance(val, (int, float)) else "n/a"

            def _fmt_csv_pv(val):
                return f"{val:.2f}" if isinstance(val, (int, float)) else "n/a"

            line = (
                f"{timestamp},"
                f"{_fmt_csv_rad(rad_today)},"
                f"{_fmt_csv_rad(rad_tomorrow)},"
                f"{_fmt_csv_pv(pv_today)},"
                f"{_fmt_csv_pv(pv_tomorrow)},"
                f"{sunrise_today},{sunset_today},"
                f"{sunrise_tomorrow},{sunset_tomorrow}\n"
            )
            await f.write(line)
            logging.debug(f"Logged solar forecast to {csv_file}")
    except Exception as e:
        logging.error(f"Error logging solar forecast to CSV: {e}")
