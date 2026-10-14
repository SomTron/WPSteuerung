import aiohttp

import asyncio
import logging
from aiohttp.resolver import AsyncResolver

try:
    import aiodns  # noqa: F401  - nur Verfuegbarkeitspruefung, siehe unten
    AIODNS_INSTALLIERT = True
except ImportError:  # aiodns ist optional (requirements.txt)
    AIODNS_INSTALLIERT = False


def fehler_text(exc: BaseException) -> str:
    """Fehlerbeschreibung, die auch bei leerer Exception-Message aussagekraeftig ist.

    ``asyncio.TimeoutError`` sowie einige ``aiohttp``-Verbindungsfehler (z.B.
    fehlgeschlagene Namensaufloesung) tragen in Python eine leere Meldung. Im
    Betriebslog stand dann nur ``Healthcheck-Ping Fehler:  -> https://...`` -
    die Ursache (Timeout / DNS / Netz) war nicht erkennbar.

    Befund 13.10.2026: Healthcheck-Alarm, obwohl der Fernzugriff auf den Pi
    lief. Gleichzeitig fielen Telegram, Solax und hc-ping aus - also die
    ausgehende Strecke, nicht das Geraet. Ohne Exception-Typ war das aus dem
    Log nicht belegbar.
    """
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return f"{type(exc).__name__} (keine Meldung - typisch fuer Timeout/DNS)"


def dns_nameserver(config=None) -> list:
    """Nameserver aus ``[Netzwerk] DNS_SERVER`` lesen (komma-getrennt).

    Leere Liste = kein Override, es gilt der System-Resolver. Bewusst KEINE
    fest verdrahteten oeffentlichen Resolver: sind sie im Netz gesperrt,
    scheitert sonst jeder Lookup mit
    ``aiodns.error.DNSError: (12, 'Timeout while contacting DNS servers')``.
    """
    if config is None:
        return []
    netzwerk = getattr(config, "Netzwerk", None)
    roh = getattr(netzwerk, "DNS_SERVER", "") or ""
    return [teil.strip() for teil in roh.split(",") if teil.strip()]


def create_robust_aiohttp_session(config=None):
    """aiohttp-Session mit nicht-blockierendem DNS-Resolver.

    ``aiodns`` verhindert, dass DNS-Lookups einen Thread des Default-Executors
    belegen. Ohne aiodns nutzt aiohttp den ThreadedResolver - stockt die
    Namensaufloesung, blockiert das auch ``asyncio.to_thread``-Aufrufe wie die
    Sensor-Lesung (Befund 13.10.2026: Healthcheck-Alarm bei laufendem
    Fernzugriff, dazu Sensor-Timeouts).

    Der Nameserver kommt aus ``[Netzwerk] DNS_SERVER``; ohne Angabe bleibt es
    beim System-Resolver aus ``/etc/resolv.conf``.
    """
    nameserver = dns_nameserver(config)

    if nameserver:
        try:
            resolver = AsyncResolver(nameservers=nameserver)
        except Exception as e:
            # z.B. aiodns fehlt (RuntimeError) oder die Angabe ist unbrauchbar.
            logging.warning(
                "DNS-Nameserver %s nicht nutzbar (%s) - verwende System-Resolver.",
                ", ".join(nameserver), fehler_text(e),
            )
        else:
            logging.info("DNS-Resolver: aiodns, Nameserver %s", ", ".join(nameserver))
            connector = aiohttp.TCPConnector(resolver=resolver, limit_per_host=10)
            return aiohttp.ClientSession(connector=connector)

    if not AIODNS_INSTALLIERT:
        logging.warning(
            "aiodns nicht installiert - DNS laeuft blockierend ueber "
            "/etc/resolv.conf. Das belegt Threads und kann z.B. die "
            "Sensor-Lesung verzoegern, wenn die Namensaufloesung stockt. "
            "Abhilfe: pip install aiodns (siehe requirements.txt)."
        )
    else:
        logging.info("DNS-Resolver: aiodns, System-Nameserver (/etc/resolv.conf)")

    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit_per_host=10))

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
            is_last_attempt = attempt == retries - 1
            if is_last_attempt:
                logging.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht (Versuch {attempt + 1}/{retries}): {fehler_text(e)}")
            else:
                logging.debug(f"Netzwerkfehler beim Senden der Telegram-Nachricht (Versuch {attempt + 1}/{retries}): {fehler_text(e)}")
                backoff = retry_delay * (2 ** attempt)
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
        except asyncio.TimeoutError:
            is_last_attempt = attempt == retries - 1
            if is_last_attempt:
                logging.error(f"Timeout beim Senden der Telegram-Nachricht (Versuch {attempt + 1}/{retries})")
            else:
                logging.debug(f"Timeout beim Senden der Telegram-Nachricht (Versuch {attempt + 1}/{retries})")
                backoff = retry_delay * (2 ** attempt)
                logging.debug(f"Warte {backoff} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(backoff)
        except Exception as e:
            logging.error(f"Unerwarteter Fehler beim Senden der Telegram-Nachricht: {fehler_text(e)}", exc_info=True)
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
            logging.debug(f"Netzwerkfehler beim Abrufen von Telegram-Updates (Versuch {attempt}/{retries}): {fehler_text(e)}")
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
            logging.error(f"Unerwarteter Fehler beim Abrufen von Telegram-Updates: {fehler_text(e)}", exc_info=True)
            return None
    return None

async def _send_healthcheck_ping(session: aiohttp.ClientSession, url: str) -> bool:
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
        logging.error(f"Healthcheck-Ping Fehler: {fehler_text(e)} → {url}", exc_info=False)
    return False

async def start_healthcheck_task(session: aiohttp.ClientSession, state):
    """
    Hintergrund-Task: Pinged periodisch die HEALTHCHECK_URL aus dem State.
        """
    import pytz
    from datetime import datetime, timedelta
    from constants import DEFAULT_TIMEZONE
    local_tz = pytz.timezone(DEFAULT_TIMEZONE)

    # Optional: Start-Ping senden (Healthchecks.io unterstützt /start)
    start_url = state.healthcheck_url if state.healthcheck_url.endswith("/start") else state.healthcheck_url + "/start"
    await _send_healthcheck_ping(session, start_url)

    while True:
        try:
            now = datetime.now(local_tz)
            interval = timedelta(minutes=state.healthcheck_interval)

            # Zeit für nächsten Ping?
            if state.last_healthcheck_ping is None or (now - state.last_healthcheck_ping) >= interval:
                success = await _send_healthcheck_ping(session, state.healthcheck_url)
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
            # Graceful Shutdown → NICHT als Fail melden!
            # Healthchecks.io erwartet Pings alle X Minuten; ein geordneter Shutdown
            # ist kein Fehlerzustand und sollte keinen Fail-Ping auslösen.
            logging.info("Healthcheck-Task beendet (graceful Shutdown)")
            break

        except Exception as e:
            logging.error(f"Unbekannter Fehler im Healthcheck-Task: {fehler_text(e)}", exc_info=True)
            await asyncio.sleep(60)
