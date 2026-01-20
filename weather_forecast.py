import logging
import aiohttp
import os
import aiofiles
from datetime import datetime, timedelta
import pytz

async def get_solar_forecast(session: aiohttp.ClientSession, config=None):
    """
    Fetches solar radiation forecast from Open-Meteo.
    Returns: (rad_today, rad_tomorrow, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow)
    Radiation in kWh/m², times as strings "HH:MM".
    """
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
                    return None, None, None, None, None, None
                
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
                
                logging.info(f"Solar forecast updated: Today={rad_today:.2f} kWh/m² ({sunrise_today}-{sunset_today}), Tomorrow={rad_tomorrow:.2f} kWh/m²")
                
                # Log to dedicated CSV
                await log_forecast_to_csv(rad_today, rad_tomorrow, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow)
                
                return rad_today, rad_tomorrow, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow
            else:
                error_text = await response.text()
                logging.error(f"Error fetching solar forecast: Status {response.status}, Details: {error_text}")
                return None, None, None, None, None, None
    except Exception as e:
        logging.error(f"Unexpected error in get_solar_forecast: {e}")
        return None, None, None, None, None, None

async def log_forecast_to_csv(rad_today, rad_tomorrow, sunrise_today, sunset_today, sunrise_tomorrow, sunset_tomorrow):
    """Logs the forecast results to a separate CSV file."""
    csv_file = "sonnen_prognose.csv"
    try:
        header = "Zeitstempel,Today_kWh,Tomorrow_kWh,Sunrise_Today,Sunset_Today,Sunrise_Tomorrow,Sunset_Tomorrow\n"
        file_exists = os.path.exists(csv_file)
        
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            if not file_exists:
                await f.write(header)
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"{timestamp},{rad_today:.2f},{rad_tomorrow:.2f},{sunrise_today},{sunset_today},{sunrise_tomorrow},{sunset_tomorrow}\n"
            await f.write(line)
            logging.debug(f"Logged solar forecast to {csv_file}")
    except Exception as e:
        logging.error(f"Error logging solar forecast to CSV: {e}")
