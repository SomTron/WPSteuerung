import logging
import aiohttp
import os
import aiofiles
from datetime import datetime, timedelta
import pytz
from constants import DEFAULT_TIMEZONE

async def get_solar_forecast(session: aiohttp.ClientSession, config=None):
    """
    Fetches solar radiation forecast from Open-Meteo.
    Returns: (rad_today, rad_tomorrow, rad_day2, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow)
        Radiation in kWh/m², times as strings "HH:MM"."""
    # Use config values or defaults
    lat = config.Wetterprognose.LATITUDE if config else 46.7142
    lon = config.Wetterprognose.LONGITUDE if config else 13.6361
    tilt = config.Wetterprognose.TILT if config else 30
    
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "sunrise,sunset",
        "hourly": "direct_radiation,diffuse_radiation",
        "timezone": DEFAULT_TIMEZONE,
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
                    return None, None, None, None, None, None, None
                
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
                
                tz = pytz.timezone(DEFAULT_TIMEZONE)
                now = datetime.now(tz)
                today_str = now.strftime("%Y-%m-%d")
                tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
                
                rad_today = daily_totals.get(today_str)
                rad_tomorrow = daily_totals.get(tomorrow_str)
                day2_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")
                rad_day2 = daily_totals.get(day2_str)
                
                sunrise_today = sun_data.get(today_str, {}).get("sunrise")
                sunset_today = sun_data.get(today_str, {}).get("sunset")
                sunrise_tomorrow = sun_data.get(tomorrow_str, {}).get("sunrise")
                sunset_tomorrow = sun_data.get(tomorrow_str, {}).get("sunset")
                
                # Nur loggen wenn Daten vorhanden, sonst None safe behandeln
                rad_today_str = f"{rad_today:.2f}" if rad_today is not None else "None"
                rad_tomorrow_str = f"{rad_tomorrow:.2f}" if rad_tomorrow is not None else "None"
                rad_day2_str = f"{rad_day2:.2f}" if rad_day2 is not None else "None"
                sr_str = sunrise_today if sunrise_today is not None else "None"
                ss_str = sunset_today if sunset_today is not None else "None"
                logging.info(f"Solar forecast updated: Today={rad_today_str} kWh/m² ({sr_str}-{ss_str}), Tomorrow={rad_tomorrow_str} kWh/m², Day2={rad_day2_str} kWh/m²")
                
                # Log to dedicated CSV
                await log_forecast_to_csv(rad_today, rad_tomorrow, rad_day2, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow)
                
                return rad_today, rad_tomorrow, rad_day2, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow
            else:
                error_text = await response.text()
                logging.error(f"Error fetching solar forecast: Status {response.status}, Details: {error_text}")
                return None, None, None, None, None, None, None
    except Exception as e:
        logging.error(f"Unexpected error in get_solar_forecast: {e}")
        return None, None, None, None, None, None, None

async def log_forecast_to_csv(rad_today, rad_tomorrow, rad_day2, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow):
    """Logs the forecast results to a separate CSV file."""
    # Use path relative to this script's directory for consistency
    script_dir = os.path.dirname(os.path.abspath(__file__))
    csv_file = os.path.join(script_dir, "sonnen_prognose.csv")
    try:
        header = "Zeitstempel,Today_kWh,Tomorrow_kWh,Day2_kWh,Sunrise_Today,Sunset_Today,Sunrise_Tomorrow,Sunset_Tomorrow\n"
        file_exists = os.path.exists(csv_file)
        
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            if not file_exists:
                await f.write(header)
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # None-Werte sicher behandeln (z.B. bei Test mit Zukunfts-Daten)
            rad_t_str = f"{rad_today:.2f}" if rad_today is not None else ""
            rad_tom_str = f"{rad_tomorrow:.2f}" if rad_tomorrow is not None else ""
            rad_d2_str = f"{rad_day2:.2f}" if rad_day2 is not None else ""
            sr_t_str = sunrise_today if sunrise_today is not None else ""
            ss_t_str = sunset_today if sunset_today is not None else ""
            sr_tom_str = sunrise_tomorrow if sunrise_tomorrow is not None else ""
            ss_tom_str = sunset_tomorrow if sunset_tomorrow is not None else ""
            line = f"{timestamp},{rad_t_str},{rad_tom_str},{rad_d2_str},{sr_t_str},{ss_t_str},{sr_tom_str},{ss_tom_str}\n"
            await f.write(line)
            logging.debug(f"Logged solar forecast to {csv_file}")
    except Exception as e:
        logging.error(f"Error logging solar forecast to CSV: {e}")
