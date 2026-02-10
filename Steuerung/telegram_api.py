import aiohttp
import asyncio
import logging
import socket
from aiohttp.resolver import AsyncResolver

def create_robust_aiohttp_session():
    """Hilfsfunktion zum Erstellen einer robusten aiohttp-Session mit DNS-Fallback."""
    try:
        resolver = AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
        connector = aiohttp.TCPConnector(resolver=resolver, limit_per_host=10)
    except RuntimeError: # aiodns not installed
        logging.warning("aiodns nicht installiert, verwende Standard-DNS-Resolver.")
        connector = aiohttp.TCPConnector(limit_per_host=10)
    except Exception as e:
        logging.warning(f"Fehler beim Initialisieren des DNS-Resolvers: {e}, verwende Standard.")
        connector = aiohttp.TCPConnector(limit_per_host=10)
    return aiohttp.ClientSession(connector=connector)

async def send_telegram_message(session, chat_id, message, bot_token, reply_markup=None, retries=3, retry_delay=5,
                                parse_mode=None):
    """Sendet eine Nachricht über Telegram mit Fehlerbehandlung und Retries."""
    if not bot_token or not chat_id:
        return False
    
    if len(message) > 4096:
        message = message[:4093] + "..."
        logging.warning("Nachricht gekürzt, da Telegram-Limit von 4096 Zeichen überschritten.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": str(message)
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        # Prevent serialization errors with mocks in tests
        if hasattr(reply_markup, "__dict__") or "MagicMock" in str(type(reply_markup)):
            payload["reply_markup"] = str(reply_markup)
        else:
            payload["reply_markup"] = reply_markup

    # Fallback for tests or when session is not provided
    if session is None:
        async with create_robust_aiohttp_session() as temp_session:
            return await send_telegram_message(temp_session, chat_id, message, bot_token, reply_markup, retries, retry_delay, parse_mode)

    # Log removed: blocking socket.getaddrinfo was here

    for attempt in range(retries):
        try:
            async with session.post(url, json=payload, timeout=20) as response:
                if response.status == 200:
                    logging.info(f"Telegram-Nachricht gesendet: {message[:100]}...")
                    return True
                else:
                    error_text = await response.text()
                    logging.error(f"Fehler beim Senden der Telegram-Nachricht (Status {response.status}): {error_text}")
                    logging.debug(f"Fehlgeschlagene Nachricht: '{message}' (Länge={len(message)})")
                    return False
        except (aiohttp.ClientConnectionError, OSError) as e:
            if attempt == retries:
                logging.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries}): {e}")
            else:
                logging.debug(f"Netzwerkfehler beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries}): {e}")
            if attempt < retries:
                backoff = retry_delay * (2 ** (attempt - 1))
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
            else:
                logging.error("Alle Versuche fehlgeschlagen (Netzwerkfehler).")
                return False
        except asyncio.TimeoutError:
            if attempt == retries:
                logging.error(f"Timeout beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries})")
            else:
                logging.debug(f"Timeout beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries})")
            if attempt < retries:
                backoff = retry_delay * (2 ** (attempt - 1))
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
            else:
                logging.error("Alle Versuche fehlgeschlagen (Timeout).")
                return False
        except Exception as e:
            logging.error(f"Unerwarteter Fehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)
            logging.debug(f"Fehlgeschlagene Nachricht: '{message}' (Länge={len(message)})")
            return False
    return False

async def get_telegram_updates(session, bot_token, offset=None, retries=3, retry_delay=5):
    """Ruft Telegram-Updates ab, mit Fehlerbehandlung, Retry und DNS-Fallback."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 60}
    if offset is not None:
        params["offset"] = offset

    # Log removed: blocking socket.getaddrinfo was here

    for attempt in range(1, retries + 1):
        try:
            async with session.get(url, params=params, timeout=70) as response:
                if response.status == 200:
                    data = await response.json()
                    updates = data.get("result", [])
                    return updates
                else:
                    error_text = await response.text()
                    logging.error(f"Fehler beim Abrufen von Telegram-Updates: Status {response.status}, Details: {error_text}")
                    return None
        except (aiohttp.ClientConnectionError, OSError) as e:
            logging.debug(f"Netzwerkfehler beim Abrufen von Telegram-Updates (Versuch {attempt}/{retries}): {e}")
            if attempt < retries:
                backoff = retry_delay * (2 ** (attempt - 1))
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
            else:
                logging.warning("Alle Versuche fehlgeschlagen (Netzwerkfehler).", extra={'rate_limit': True})
                return None
        except asyncio.TimeoutError:
            logging.debug(f"Timeout beim Abrufen von Telegram-Updates (Versuch {attempt}/{retries})")
            if attempt < retries:
                backoff = retry_delay * (2 ** (attempt - 1))
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
            else:
                logging.debug("Alle Versuche fehlgeschlagen (Timeout).")
                return []
        except Exception as e:
            logging.error(f"Unerwarteter Fehler beim Abrufen von Telegram-Updates: {e}", exc_info=True)
            return None
    return None

async def send_healthcheck_ping(session: aiohttp.ClientSession, url: str) -> bool:
    """Sendet einen einzelnen Ping. Gibt True bei Erfolg zurück."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 200:
                logging.debug(f"Healthcheck-Ping erfolgreich: {url}")
                return True
            else:
                text = await resp.text()
                logging.warning(f"Healthcheck-Ping fehlgeschlagen (Status {resp.status}): {text}")
    except Exception as e:
        logging.error(f"Healthcheck-Ping Fehler: {e} → {url}", exc_info=False)
    return False

async def start_healthcheck_task(session: aiohttp.ClientSession, state):
    """
    Hintergrund-Task: Pinged periodisch die HEALTHCHECK_URL aus dem State.
    """
    import pytz
    from datetime import datetime, timedelta
    local_tz = pytz.timezone("Europe/Berlin")

    # Optional: Start-Ping senden (Healthchecks.io unterstützt /start)
    start_url = state.healthcheck_url if state.healthcheck_url.endswith("/start") else state.healthcheck_url + "/start"
    await send_healthcheck_ping(session, start_url)

    while True:
        try:
            now = datetime.now(local_tz)
            interval = timedelta(minutes=state.healthcheck_interval)

            # Zeit für nächsten Ping?
            if state.last_healthcheck_ping is None or (now - state.last_healthcheck_ping) >= interval:
                success = await send_healthcheck_ping(session, state.healthcheck_url)
                state.last_healthcheck_ping = now

                if not success:
                    # Bei Fehler etwas öfter versuchen
                    await asyncio.sleep(60)
                    continue

            # Intelligentes Warten bis zum nächsten Ping
            next_ping_at = (state.last_healthcheck_ping or now) + interval
            sleep_sec = max(10, (next_ping_at - now).total_seconds() + 5)
            await asyncio.sleep(sleep_sec)

        except asyncio.CancelledError:
            # Beim Programmende → Fail-Ping senden
            fail_url = state.healthcheck_url + "/fail"
            await send_healthcheck_ping(session, fail_url)
            logging.info("Healthcheck-Task beendet – Fail-Ping gesendet")
            break

        except Exception as e:
            logging.error(f"Unbekannter Fehler im Healthcheck-Task: {e}", exc_info=True)
            await asyncio.sleep(60)
