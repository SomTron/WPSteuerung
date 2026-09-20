"""
Zentraler, robuster API-Client fuer aiohttp-basierte HTTP-Aufrufe.

Bietet einheitliche Timeout-Behandlung, Retry mit Exponential-Backoff
und strukturiertes Error-Logging fuer alle API-Calls im System.
"""
import logging
import asyncio
from typing import Optional, Callable, Any
from dataclasses import dataclass

import aiohttp


@dataclass
class ApiResult:
    """Standardisierter Rueckgabewert fuer API-Calls."""
    success: bool
    data: Any = None
    status_code: Optional[int] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
async def robust_api_call(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    data: Any = None,
    headers: Optional[dict] = None,
    timeout_sec: int = 30,
    max_retries: int = 3,
    retry_delay_sec: int = 5,
    retry_backoff_factor: float = 2.0,
    extract_json: bool = True,
    response_validator: Optional[Callable[[aiohttp.ClientResponse], bool]] = None,
    logger: Optional[logging.Logger] = None,
) -> ApiResult:
    """
    Fuehrt einen robusten API-Call mit Retry-Logik durch.

    Args:
        session: aiohttp-Session
        method: HTTP-Methode ('GET', 'POST', ...)
        url: Ziel-URL
        params: URL-Parameter (Query-String)
        json: JSON-Body
        data: Form-Daten oder roher Body
        headers: Zusaetzliche HTTP-Header
        timeout_sec: Timeout pro Versuch
        max_retries: Maximale Anzahl Versuche
        retry_delay_sec: Basis-Wartezeit vor dem ersten Retry
        retry_backoff_factor: Exponentieller Backoff (delay * factor^attempt)
        extract_json: Wenn True, wird response.json() extrahiert
        response_validator: Optionale Callback-Funktion zur Validierung der Response
        logger: Logger-Instanz (Default: logging.getLogger(__name__))

    Returns:
        ApiResult mit success=True/False, data, status_code, error_type, error_message
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    for attempt in range(max_retries):
        try:
            # Nutze spezifische HTTP-Methoden (get/post) statt session.request(),
            # damit externe Mocks (wie in den Tests) kompatibel bleiben
            http_method = getattr(session, method.lower())
            async with http_method(
                url,
                params=params,
                json=json,
                data=data,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_sec),
            ) as response:
                status = response.status

                # Validierung: Entweder per Callback oder Standard (HTTP 2xx)
                if response_validator is not None:
                    ok = response_validator(response)
                else:
                    ok = 200 <= status < 300

                if ok:
                    result_data = await response.json() if extract_json else await response.read()
                    return ApiResult(
                        success=True,
                        data=result_data,
                        status_code=status,
                    )

                # HTTP-Fehler: 4xx = Client-Fehler (kein Retry), 5xx = Server-Fehler (Retry)
                error_text = ""
                try:
                    error_text = await response.text()
                except Exception:
                    pass

                if 400 <= status < 500:
                    logger.error(
                        f"HTTP {status} bei {method} {url}: {error_text[:200]}"
                    )
                    return ApiResult(
                        success=False,
                        status_code=status,
                        error_type=f"HTTP_{status}",
                        error_message=error_text[:500],
                    )

                # 5xx Server-Fehler -> Retry
                logger.warning(
                    f"HTTP {status} bei {method} {url} "
                    f"(Versuch {attempt + 1}/{max_retries}): {error_text[:100]}"
                )

        except asyncio.TimeoutError as e:
            logger.error(
                f"Timeout bei {method} {url} "
                f"(Versuch {attempt + 1}/{max_retries}): {e}"
            )
        except aiohttp.ClientError as e:
            logger.error(
                f"ClientError bei {method} {url} "
                f"(Versuch {attempt + 1}/{max_retries}): "
                f"{type(e).__name__}: {e}"
            )
        except (OSError, ConnectionError) as e:
            logger.error(
                f"Netzwerkfehler bei {method} {url} "
                f"(Versuch {attempt + 1}/{max_retries}): "
                f"{type(e).__name__}: {e}"
            )
        except Exception as e:
            logger.error(
                f"Unerwarteter Fehler bei {method} {url} "
                f"(Versuch {attempt + 1}/{max_retries}): "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            return ApiResult(
                success=False,
                error_type=type(e).__name__,
                error_message=str(e),
            )

        # Warte vor dem naechsten Retry (Exponential-Backoff)
        if attempt < max_retries - 1:
            delay = retry_delay_sec * (retry_backoff_factor ** attempt)
            logger.debug(f"Warte {delay:.1f}s vor Retry {attempt + 2}/{max_retries}...")
            await asyncio.sleep(delay)

    # Alle Versuche ausgeschoepft
    logger.error(f"Alle {max_retries} Versuche fuer {method} {url} fehlgeschlagen.")
    return ApiResult(
        success=False,
        error_type="MaxRetriesExceeded",
        error_message=f"Alle {max_retries} Versuche fehlgeschlagen",
    )
async def robust_get(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout_sec: int = 30,
    max_retries: int = 3,
    retry_delay_sec: int = 5,
    extract_json: bool = True,
    response_validator: Optional[Callable] = None,
    logger: Optional[logging.Logger] = None,
) -> ApiResult:
    """Robuster GET-Request (Wrapper um robust_api_call)."""
    return await robust_api_call(
        session, "GET", url,
        params=params, headers=headers,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        retry_delay_sec=retry_delay_sec,
        extract_json=extract_json,
        response_validator=response_validator,
        logger=logger,
    )


async def robust_post(
    session: aiohttp.ClientSession,
    url: str,
    *,
    json: Optional[dict] = None,
    data: Any = None,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout_sec: int = 30,
    max_retries: int = 3,
    retry_delay_sec: int = 5,
    extract_json: bool = True,
    response_validator: Optional[Callable] = None,
    logger: Optional[logging.Logger] = None,
) -> ApiResult:
    """Robuster POST-Request (Wrapper um robust_api_call)."""
    return await robust_api_call(
        session, "POST", url,
        json=json, data=data, params=params, headers=headers,
        timeout_sec=timeout_sec,
        max_retries=max_retries,
        retry_delay_sec=retry_delay_sec,
        extract_json=extract_json,
        response_validator=response_validator,
        logger=logger,
    )