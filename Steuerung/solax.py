import logging
import math
import asyncio
import aiohttp
from datetime import timedelta
from clock import now_for
from typing import Optional
import pytz
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from constants import (
    DEFAULT_TIMEZONE,
    SOLAR_API_TIMEOUT_SEC,
    SOLAR_DATA_STALE_THRESHOLD_MIN,
    SOLAX_MAX_RETRIES,
    SOLAX_RETRY_DELAY_SEC,
)

API_URL = "https://global.solaxcloud.com/proxyApp/proxy/api/getRealtimeInfo.do"


class SolaxRealtime(BaseModel):
    """Validiertes Realtime-Schema; unbekannte Felder werden ignoriert."""
    model_config = ConfigDict(extra="ignore")

    acpower: Optional[float] = None
    feedinpower: Optional[float] = None
    batPower: Optional[float] = None
    soc: Optional[float] = None
    powerdc1: Optional[float] = None
    powerdc2: Optional[float] = None
    consumeenergy: Optional[float] = None

    @field_validator("*", mode="before")
    @classmethod
    def _finite_number(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError("bool ist kein Solar-Messwert")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Solar-Messwert ist nicht endlich")
        return number


async def get_solax_data(session, state):
    """
    Holt aktuelle Solax-Daten mit Freshness-Check.
    Gibt None zurück wenn Daten zu alt sind (>15 min) oder API nicht erreichbar.
    """
    local_tz = pytz.timezone(DEFAULT_TIMEZONE)
    now = now_for(state, fallback_tz=local_tz)

    # Frische-Cache: kurze API-Sperre, aber fail-safe nach 15 Minuten.
    CACHE_TTL = timedelta(minutes=5)
    MAX_DATA_AGE = timedelta(minutes=SOLAR_DATA_STALE_THRESHOLD_MIN)

    # Stelle sicher, dass state.solar.last_api_call zeitzonenbewusst ist
    if state.solar.last_api_call and state.solar.last_api_call.tzinfo is None:
        state.solar.last_api_call = local_tz.localize(state.solar.last_api_call)

    # Prüfe ob gecachte Daten zu alt sind (stale)
    if state.solar.last_api_call and (now - state.solar.last_api_call) > MAX_DATA_AGE:
        logging.warning(
            f"Solax-Daten sind {int((now - state.solar.last_api_call).total_seconds() / 60)} min alt "
            f"(> {MAX_DATA_AGE.total_seconds() / 60} min) – zwinge frischen Abruf"
        )
        state.solar.last_api_call = None  # Cache invalidieren

    # Gib gecachte Daten zurück wenn noch frisch genug
    if state.solar.last_api_call and (now - state.solar.last_api_call) < CACHE_TTL:
        return state.solar.last_api_data

    max_retries = SOLAX_MAX_RETRIES
    # Die Wartezeit kommt aus constants.py, damit die Retry-Logik in Tests
    # ohne echtes Warten pruefbar ist. Produktiv bleiben 5 s sinnvoll: der
    # Aufruf laeuft ohnehin im Hintergrund-Task, nicht im 10-s-Hauptloop.
    retry_delay = SOLAX_RETRY_DELAY_SEC
    for attempt in range(max_retries):
        try:
            # Config access via state.config
            token_id = state.config.SolaxCloud.TOKEN_ID
            sn = state.config.SolaxCloud.SN
            
            if not token_id or not sn:
                logging.warning("Solax Config fehlt (Token/SN)")
                return None

            params = {"tokenId": token_id, "sn": sn}
            async with session.get(
                API_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=SOLAR_API_TIMEOUT_SEC),
            ) as response:
                response.raise_for_status()
                data = await response.json()
                if not isinstance(data, dict):
                    logging.error("Solax-API lieferte kein JSON-Objekt")
                    return None
                if data.get("success"):
                    try:
                        validated = SolaxRealtime.model_validate(data.get("result"))
                    except (ValidationError, TypeError, ValueError) as exc:
                        logging.error("Solax-API lieferte ein ungültiges Realtime-Schema: %s", exc)
                        return None
                    state.solar.last_api_data = validated.model_dump(exclude_none=True)
                    state.solar.last_api_call = now
                    return state.solar.last_api_data
                else:
                    logging.error(f"API-Fehler: {data.get('exception', 'Unbekannter Fehler')}")
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.error(f"Fehler bei der API-Anfrage (Versuch {attempt + 1}/{max_retries}): {type(e).__name__}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Maximale Wiederholungen erreicht, verwende Fallback-Daten.")
                return None
        except Exception as e:
            logging.error(
                f"Unerwarteter Fehler bei der API-Anfrage (Versuch {attempt + 1}/{max_retries}): {type(e).__name__}: {e}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Maximale Wiederholungen erreicht (unexpected), verwende Fallback-Daten.")
                return None
    return None

async def fetch_solax_data(session, state):
    """
    Holt die aktuellen Solax-Daten und gibt sie mit Fallback-Werten zurück.
    """
    
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
            "acpower": 0,
            "feedinpower": 0,
            "batPower": 0,
            "soc": 0,
            "powerdc1": 0,
            "powerdc2": 0,
            "consumeenergy": 0,
        }
