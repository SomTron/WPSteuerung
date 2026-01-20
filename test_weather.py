import asyncio
import aiohttp
import logging
from weather_forecast import get_solar_forecast

async def test_forecast():
    logging.basicConfig(level=logging.INFO)
    async with aiohttp.ClientSession() as session:
        rad_today, rad_tomorrow, sr_today, ss_today, sr_tomorrow, ss_tomorrow = await get_solar_forecast(session)
        print(f"Today: {rad_today} kWh/m², Sunrise: {sr_today}, Sunset: {ss_today}")
        print(f"Tomorrow: {rad_tomorrow} kWh/m², Sunrise: {sr_tomorrow}, Sunset: {ss_tomorrow}")

if __name__ == "__main__":
    asyncio.run(test_forecast())
