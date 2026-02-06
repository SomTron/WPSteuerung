import logging
import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz
import pandas as pd

API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"

async def get_solax_data(session, state):
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)

    # Stelle sicher, dass state.solar.last_api_call zeitzonenbewusst ist
    if state.solar.last_api_call and state.solar.last_api_call.tzinfo is None:
        state.solar.last_api_call = local_tz.localize(state.solar.last_api_call)

    if state.solar.last_api_call and (now - state.solar.last_api_call) < timedelta(minutes=5):
        return state.solar.last_api_data

    max_retries = 3
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            # Config access via state.config
            token_id = state.config.SolaxCloud.TOKEN_ID
            sn = state.config.SolaxCloud.SN
            
            if not token_id or not sn:
                logging.warning("Solax Config fehlt (Token/SN)")
                return None

            params = {"tokenId": token_id, "sn": sn}
            async with session.get(API_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("success"):
                    state.solar.last_api_data = data.get("result")
                    state.solar.last_api_call = now
                    return state.solar.last_api_data
                else:
                    logging.error(f"API-Fehler: {data.get('exception', 'Unbekannter Fehler')}")
                    return None
        except aiohttp.ClientError as e:
            logging.error(f"Fehler bei der API-Anfrage (Versuch {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Maximale Wiederholungen erreicht, verwende Fallback-Daten.")
                return None
    return None

async def fetch_solax_data(session, state):
    """
    Holt die aktuellen Solax-Daten und gibt sie mit Fallback-Werten zurück.
    """
    now = datetime.now(pytz.timezone("Europe/Berlin"))
    
    fallback_data = {
        "acpower": 0,
        "feedinpower": 0,
        "consumeenergy": 0,
        "batPower": 0,
        "soc": 0,
        "powerdc1": 0,
        "powerdc2": 0,
        "api_fehler": True
    }

    try:
        solax_data = await get_solax_data(session, state) or fallback_data.copy()

        # Upload-Zeit prüfen
        if "utcDateTime" in solax_data:
            try:
                # utcDateTime format check needed? usually standard ISO or similar
                upload_time = pd.to_datetime(solax_data["utcDateTime"]).tz_convert("Europe/Berlin")
                # delay = (now - upload_time).total_seconds()
            except Exception:
                pass

        return {
            "solax_data": solax_data,
            "acpower": solax_data.get("acpower", "N/A"),
            "feedinpower": solax_data.get("feedinpower", "N/A"),
            "batPower": solax_data.get("batPower", "N/A"),
            "soc": solax_data.get("soc", "N/A"),
            "powerdc1": solax_data.get("powerdc1", "N/A"),
            "powerdc2": solax_data.get("powerdc2", "N/A"),
            "consumeenergy": solax_data.get("consumeenergy", "N/A"),
        }

    except Exception as e:
        logging.error(f"Fehler beim Abrufen von Solax-Daten: {e}", exc_info=True)
        return {
            "solax_data": fallback_data,
            # ... defaults
        }
