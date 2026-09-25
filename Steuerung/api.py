try:
    from constants import SOLAR_DATA_STALE_THRESHOLD_MIN
except ImportError:
    SOLAR_DATA_STALE_THRESHOLD_MIN = 15

import hmac
import json
import logging
import math

try:
    import boiler_modell
    import pv_profil as _pv_profil_modul
except ImportError as _e:
    logging.warning(f"Neue Module nicht ladbar: {_e}")
    boiler_modell = None
    _pv_profil_modul = None

try:
    import entscheidungs_log
except ImportError:
    entscheidungs_log = None
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Dict, Any
import os
from datetime import datetime, timedelta
import re

from utils import HEIZUNGSDATEN_CSV, to_naive
from clock import now_for
from logic_utils import forecast_kwh_m2_to_wh_m2
from status_snapshot import build_status_snapshot
from blocking_codes import INFO_BLOCKING_CODES, blocking_code as _blocking_code
from api_contract import (
    API_CONTRACT_VERSION,
    with_contract_metadata,
    validate_status_shape,
)

try:
    from priority_control_logic import _is_nachtsperre_aktiv
except ImportError:
    _is_nachtsperre_aktiv = None

# Allowed commands and modes for validation
ALLOWED_COMMANDS = {"force_on", "force_off", "set_mode"}
ALLOWED_MODES = {"bademodus", "urlaubsmodus"}
ALLOWED_SECTIONS = {
    "Heizungssteuerung", "Healthcheck", "SolaxCloud", "Telegram",
    "Urlaubsmodus", "Solarueberschuss", "Logging", "Wetterprognose",
}

# WPS_API_KEY kann auf dem Pi als systemd-Environment gesetzt werden. Ohne
# Schluessel bleiben nur die nicht-sensiblen Status-/Historienrouten offen;
# Schreib- und Exportbefehle werden dann bewusst abgewiesen.
API_KEY = (os.environ.get("WPS_API_KEY") or "").strip()
_raw_origins = (os.environ.get("WPS_CORS_ORIGINS") or "").strip()
CORS_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]


def _check_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Prueft den optionalen Schluessel fuer schreibende/sensible Routen."""
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail="Schreibzugriff ist deaktiviert: WPS_API_KEY nicht konfiguriert",
        )
    if not isinstance(x_api_key, str) or not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Ungueltiger oder fehlender API-Key")


def _api_now(state):
    """Nutzt für API-Snapshots deren konsistente Erzeugungszeit."""
    for name in ("_is_status_snapshot_created_at", "last_status_snapshot_at"):
        value = getattr(state, name, None)
        if isinstance(value, datetime):
            return value
    return now_for(state)


def _redact_config(value, key: str = ""):
    """Exportiert Konfiguration ohne Telegram-/Solax-Geheimnisse."""
    if isinstance(value, dict):
        return {k: _redact_config(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_config(v, key) for v in value]
    if key.upper() in {"BOT_TOKEN", "TOKEN_ID", "SN", "CHAT_ID", "API_KEY"}:
        return "***" if value not in (None, "") else ""
    return value

def build_mode_payload(state, priority_info_override=None):
    """Baut das 'mode'-Objekt des /status Endpoints.

    Wichtig: Die Sommer-Modus-Felder liegen am State-Root (sommer_modus_aktiv,
    sommer_modus_zaehler) bzw. in der Priority-Config (Offset, benoetigte
    Tage) - NICHT unter state.control. (Vorher wurden sie dort gelesen,
    weshalb das Webinterface den Sommer-Modus immer als 'Inaktiv' anzeigte.)"""
    sommer_cfg = getattr(getattr(state, 'priority_config', None), 'sommer_modus', None)
    control = getattr(state, 'control', None)

    if priority_info_override is None:
        # Nachtsperre direkt aus der Priority-Config berechnen
        # (gleiche Logik wie im /status Endpoint)
        nachtsperre = False
        pc = getattr(state, 'priority_config', None)
        if pc is not None and _is_nachtsperre_aktiv is not None:
            try:
                nachtsperre = bool(_is_nachtsperre_aktiv(pc, _api_now(state)))
            except Exception as e:
                logging.warning(f"Konnte Nachtsperren-Status nicht ermitteln: {e}")
        priority_info_override = {"nachtsperre_aktiv": nachtsperre}
    info = priority_info_override
    # effective_rule_name/active_rule_name describe only a running hardware
    # cycle. A requested rule that is waiting for a source must not be shown
    # as if it already controls the compressor.
    running = bool(getattr(control, "kompressor_ein", False))
    effective_rule = getattr(control, "effective_rule_name", None) if running else None
    active_rule = getattr(control, "active_rule_name", None) if running else None
    source_at_start = getattr(control, "source_at_start", None) if running else None
    effective_source = getattr(control, "effective_source", None) if running else None
    source_at_start = source_at_start or "—"
    effective_source = effective_source or "—"
    return {
        "current": (getattr(control, 'previous_modus', None) or ""),
        "solar_active": bool(getattr(control, 'solar_ueberschuss_aktiv', False)),
        "holiday_active": bool(getattr(state, 'urlaubsmodus_aktiv', False)),
        "bath_active": bool(getattr(state, 'bademodus_aktiv', False)),
        "nightsperre_active": (bool(info.get("nachtsperre_aktiv", False))
                               if isinstance(info, dict) else False),
        "active_rule": effective_rule or active_rule or "",
        "effective_rule": effective_rule or "",
        "requested_rule": (getattr(control, 'requested_rule_name', None) or ""),
        "effective_source": effective_source or "Netz",
        "source_at_start": source_at_start or "Netz",
        "source_current": (getattr(control, 'source_current', None) or "Netz"),
        "active_rule_sensor": (getattr(control, 'active_rule_sensor', None) or ""),
        "blocking_reason": (getattr(control, 'blocking_reason', None) or ""),
        # Sperrfamilie und Einstufung, damit die WebApp einen Normalzustand
        # ("Boiler bereits heiss") nicht als Warnung darstellen muss.
        "blocking_code": _blocking_code(getattr(control, 'blocking_reason', None)),
        "blocking_kind": (
            "info"
            if _blocking_code(getattr(control, 'blocking_reason', None))
            in INFO_BLOCKING_CODES
            else "alarm"
        ),
        "soll_einschalten": bool(getattr(control, '_soll_einschalten', False)),
        "sommer_modus_aktiv": bool(getattr(state, 'sommer_modus_aktiv', False)),
        "sommer_modus_offset_c": float(getattr(sommer_cfg, 'temperatur_offset_c', 0.0) or 0.0),
        "sommer_modus_tage_ueber": int(getattr(state, 'sommer_modus_zaehler', 0) or 0),
        "sommer_modus_benoetigte": int(getattr(sommer_cfg, 'benoetigte_tage', 3) or 3),
    }


# Data Models
class ConfigUpdate(BaseModel):
    section: str = Field(..., min_length=1, max_length=50)
    key: str = Field(..., min_length=1, max_length=50)
    value: str = Field(..., min_length=0, max_length=500)

    @field_validator('section')
    @classmethod
    def section_must_be_valid(cls, v):
        """Prueft, dass der Section-Name nur erlaubte Werte enthaelt."""
        if v not in ALLOWED_SECTIONS:
            raise ValueError(f"Section '{v}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_SECTIONS))}")
        return v

    @field_validator('key')
    @classmethod
    def key_must_be_safe(cls, v):
        """Prueft, dass der Key-Name nur erlaubte Zeichen enthaelt (keine Injections)."""
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', v):
            raise ValueError(f"Key '{v}' enthaelt unerlaubte Zeichen. Nur Buchstaben, Zahlen und Unterstriche erlaubt.")
        return v

class ControlCommand(BaseModel):
    command: str = Field(..., min_length=1, max_length=50)
    params: Optional[Dict[str, Any]] = None

    @field_validator('command')
    @classmethod
    def command_must_be_allowed(cls, v):
        """Prueft, dass der Command-Name in der erlaubten Liste steht."""
        if v not in ALLOWED_COMMANDS:
            raise ValueError(f"Command '{v}' ist nicht erlaubt. Erlaubt: {', '.join(sorted(ALLOWED_COMMANDS))}")
        return v

    @model_validator(mode='after')
    def validate_mode_if_set_mode(self):
        """Prueft set_mode streng: Modus und Boolean sind Pflicht."""
        if self.command == "set_mode":
            if not self.params or self.params.get("mode") not in ALLOWED_MODES:
                raise ValueError(
                    "set_mode benoetigt mode=bademodus|urlaubsmodus"
                )
            if type(self.params.get("active")) is not bool:
                raise ValueError("active muss ein JSON-Boolean sein")
        return self

app = FastAPI(
    title="WPSteuerung API",
    description="API for Heat Pump Control Android App",
    version=API_CONTRACT_VERSION,
)

# Live-Daten niemals durch Browser, FastAPI-Proxy oder Service Worker cachen.
_LIVE_NO_STORE_PATHS = {
    "/status", "/health", "/history", "/history/regeln",
    "/control", "/command", "/config", "/config/export", "/debug/csv", "/analysis/quality",
}


@app.middleware("http")
async def add_live_no_store_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path in _LIVE_NO_STORE_PATHS or request.url.path.startswith("/history/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# CORS: Standardmaessig keine fremden Origins. Auf dem Pi koennen erlaubte
# Origins explizit als WPS_CORS_ORIGINS="https://domain,http://localhost:..." gesetzt werden.
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

# Static files: Serve webapp directory
_webapp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "webapp")
_webapp_dir = os.path.normpath(_webapp_dir)
if os.path.isdir(_webapp_dir):
    app.mount("/static", StaticFiles(directory=_webapp_dir), name="static")

@app.get("/", response_class=HTMLResponse)
def serve_index():
    """Serve the main dashboard HTML page."""
    index_path = os.path.join(_webapp_dir, "index.html")
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    raise HTTPException(status_code=404, detail="Webapp not found")

@app.get("/index.html", response_class=HTMLResponse)
def serve_index_html():
    """Serve the main dashboard HTML page (direct URL)."""
    return serve_index()

# Global state references. /status liest nur aus dem Snapshot; Schreib-/Config-
# Routen behalten den echten Main-Loop-State fuer die Queue.
shared_state = None
control_state = None
control_funcs = None


def init_api(state, funcs):
    global shared_state, control_state, control_funcs
    control_state = state
    shared_state = build_status_snapshot(state)
    control_funcs = funcs


def update_status_snapshot(state) -> None:
    """API-Snapshot atomar durch eine konsistente Kopie ersetzen."""
    global shared_state
    shared_state = build_status_snapshot(state)


def _control_state():
    if getattr(shared_state, "_is_status_snapshot", False):
        return control_state
    return shared_state

@app.get("/health")
def health_status():
    """Schneller, nicht-sensibler Healthcheck fuer Monitoring/Uptime."""
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")
    state = shared_state
    try:
        now = _api_now(state)
        heartbeat = getattr(state, "loop_heartbeat", None)
        age_s = None if not isinstance(heartbeat, datetime) else max(
            0.0, (now - heartbeat).total_seconds()
        )
    except (TypeError, ValueError):
        age_s = None
    raw_errors = getattr(getattr(state, "control", None), "consecutive_control_errors", 0)
    errors = int(raw_errors) if isinstance(raw_errors, (int, float)) else 0
    data_ok = getattr(state, "last_data_update_ok", None)
    snapshot_ok = getattr(state, "last_state_write_ok", None)
    healthy = (
        age_s is not None and age_s <= 30 and errors == 0
        and data_ok is not False and snapshot_ok is not False
    )
    return {
        "status": "ok" if healthy else "degraded",
        "loop_heartbeat_age_s": age_s,
        "last_control_success": getattr(state, "last_control_success", None),
        "last_sensor_success": getattr(state, "last_sensor_success", None),
        "last_status_snapshot_at": getattr(state, "last_status_snapshot_at", None),
        "consecutive_control_errors": errors,
        "data_update_ok": data_ok,
        "last_state_write_ok": snapshot_ok,
        "last_state_write_error": getattr(state, "last_state_write_error", None),
    }


# --- Fehlerhistorie ---------------------------------------------------------
#
# `error.log` enthaelt alle WARNING+-Ereignisse (rotierend, siehe
# logging_config). Die WebApp zeigt daraus das letzte Ereignis mit
# Zeitstempel und ueber einen Button die vollstaendige Historie.
#
# Es wird bewusst nur der TAIL gelesen: /status laeuft alle 5 s, ein
# vollstaendiges Lesen wuerde auf dem Pi RAM und CPU kosten. Die
# Cache-TTL ist kurz, damit ein neuer Fehler schnell sichtbar wird.

ERROR_LOG_DATEI = os.environ.get(
    "WPS_ERROR_LOG_FILE", "/var/log/wps/error.log"
)
ERROR_TAIL_BYTES = int(os.environ.get("WPS_ERROR_TAIL_BYTES", 512 * 1024))
ERROR_CACHE_TTL_SEC = 15
_ERROR_CACHE: dict = {"zeit": None, "eintraege": None}

# Format: "2026-09-25 14:03:22 +0200 ERROR - Nachricht"
_RE_FEHLERZEILE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\s*[+-]\d{4})?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*-\s*(?P<msg>.*)$"
)
_STUFEN_RANG = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _fehler_einstufung(level: str) -> str:
    """Faehrt Log-Level zu einer kompakten, fuer die UI brauchbaren Stufe."""
    if level == "CRITICAL":
        return "kritisch"
    if level == "ERROR":
        return "fehler"
    if level == "WARNING":
        return "warnung"
    return "info"


def _parse_fehlerzeilen(text: str) -> list:
    """Wandelt error.log-Zeilen in strukturierte Eintraege (neueste zuerst)."""
    eintraege = []
    for zeile in text.splitlines():
        treffer = _RE_FEHLERZEILE.match(zeile.strip())
        if not treffer:
            continue
        roh_ts = treffer.group("ts").replace("T", " ")
        ts = None
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                ts = datetime.strptime(roh_ts, fmt)
                break
            except ValueError:
                continue
        level = treffer.group("level")
        nachricht = treffer.group("msg").strip()
        if not nachricht:
            continue
        eintraege.append(
            {
                "timestamp": ts.isoformat() if ts else roh_ts,
                "level": level,
                "stufe": _fehler_einstufung(level),
                "message": nachricht[:500],
            }
        )
    eintraege.reverse()
    return eintraege


def _read_error_tail(max_bytes: int = ERROR_TAIL_BYTES) -> list:
    """Liest den Tail des Fehlerlogs robust - auch waehrend des Schreibens."""
    try:
        groesse = os.path.getsize(ERROR_LOG_DATEI)
    except OSError:
        return []
    try:
        with open(
            ERROR_LOG_DATEI, "rb"
        ) as handle:
            if groesse > max_bytes:
                handle.seek(groesse - max_bytes)
                handle.readline()  # angebrochene erste Zeile verwerfen
            daten = handle.read()
    except OSError:
        return []
    text = daten.decode("utf-8", errors="replace")
    return _parse_fehlerzeilen(text)


def fehler_historie(limit: int = 100, nur_ab_warnung: bool = True):
    """Strukturierte Fehlerhistorie fuer die WebApp (neueste zuerst)."""
    jetzt = datetime.now()
    eintraege = _ERROR_CACHE.get("eintraege")
    geholt = _ERROR_CACHE.get("zeit")
    if (
        eintraege is None
        or geholt is None
        or (jetzt - geholt).total_seconds() > ERROR_CACHE_TTL_SEC
    ):
        try:
            eintraege = _read_error_tail()
        except Exception as exc:  # Diagnose darf die API nie ausfallen lassen
            logging.warning("Fehlerlog nicht lesbar: %s", exc)
            eintraege = []
        _ERROR_CACHE["eintraege"] = eintraege
        _ERROR_CACHE["zeit"] = jetzt

    if nur_ab_warnung:
        eintraege = [e for e in eintraege if _STUFEN_RANG.get(e["level"], 0) >= 30]
    begrenzt = eintraege[: max(1, min(int(limit), 500))]
    # Es wird bewusst nur ein Tail gelesen (RAM-Schutz auf dem Pi). Deshalb
    # wird der tatsaechlich abgedeckte Zeitraum mitgeliefert, damit die WebApp
    # keine vollstaendige Historie vortaeuschen muss.
    zeiten = [e["timestamp"] for e in begrenzt]
    return {
        "eintraege": begrenzt,
        "anzahl": len(begrenzt),
        "gesamt_geprueft": len(eintraege),
        "quelle": os.path.basename(ERROR_LOG_DATEI),
        "abgerufen": jetzt.isoformat(timespec="seconds"),
        "letzter_fehler": begrenzt[0] if begrenzt else None,
        "zeitraum_von": zeiten[-1] if zeiten else None,
        "zeitraum_bis": zeiten[0] if zeiten else None,
        "vollstaendig": False,
        "tail_bytes": ERROR_TAIL_BYTES,
    }


@app.get("/errors")
def errors_endpoint(
    limit: int = 100,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """Fehlerhistorie mit Zeitstempel.

    Nur Fehler ab Stufe WARNING - die WebApp blendet reine
    Steuerungs-Informationen ohnehin aus.
    """
    limit = max(1, min(int(limit or 100), 500))
    return fehler_historie(limit=limit, nur_ab_warnung=True)


def _solar_stale_status() -> bool:
    """True, wenn Solax-Daten aelter als der Stale-Schwellwert sind."""
    if shared_state is None:
        return True
    try:
        last_api_call = getattr(shared_state.solar, 'last_api_call', None)
        if not isinstance(last_api_call, datetime):
            return True
        jetzt = now_for(shared_state)
        if jetzt.tzinfo is None and last_api_call.tzinfo is not None:
            jetzt = last_api_call.replace(tzinfo=None)
        return (jetzt - last_api_call).total_seconds() / 60.0 > SOLAR_DATA_STALE_THRESHOLD_MIN
    except Exception as exc:
        # Bei fehlerhaftem Zeitstempel lieber stale als eine potentiell
        # nicht existente Soladatenquelle als frisch ausgeben.
        logging.warning("Solar-Stale-Status nicht ermittelbar: %s", exc)
        return True


# Cache fuer das historische 14-Tage-Mittel der Einspeisung (Wh/qm).
# Grund: /status wird vom Dashboard alle 5 s abgefragt; ein vollstaendiger
# pd.read_csv() ueber ALLE 20 Spalten pro Aufruf kostet auf dem Pi spuerbar
# RAM/CPU (Benchmark: 25.9 MB vs 7.5 MB Peak bei 126k Zeilen). Der 14-Tage-
# Mittelwert aendert sich langsam -> 30-Minuten-Cache wie in pv_profil.py.
# Zusaetzlich bewusst OHNE pandas: ein pandas-Import kostet dauerhaft ~70 MB
# RSS und hat am 15.09. mit zum OOM-Kill (status=9/KILL) auf dem Pi gefuehrt.
_HIST_WH_QM_CACHE: dict = {"zeit": None, "wert": None}
HIST_WH_QM_TTL_SEC: int = 1800


def _parse_zeitstempel(raw):
    """CSV-Zeitstempel robust parsen: ISO-Text ODER Excel-Seriennummer.

    Die heizungsdaten.csv enthaelt historisch beide Varianten (Excel-Export),
    deshalb zuerst der numerische Versuch (Tage seit 1899-12-30).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime(1899, 12, 30) + timedelta(days=float(text))
    except (ValueError, TypeError, OverflowError):
        pass
    try:
        dt = datetime.fromisoformat(text)
        # Zeitzonen-Info entfernen fuer konsistenten Vergleich mit
        # datetime.now() (naive) in der Cache-Logik (_historisches_wh_qm)
        return to_naive(dt)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _to_float(val):
    """CSV-Wert still in einen endlichen float umwandeln (sonst None)."""
    if val is None:
        return None
    try:
        number = float(str(val).strip())
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _read_csv_tail(csv_path: str, max_rows: int = 25000):
    """Liest einen CSV-Tail robust und zählt unvollständige/versehrte Zeilen."""
    import csv
    from collections import deque

    with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        raw_tail = deque(reader, maxlen=max_rows)

    rows = []
    invalid_rows = 0
    null_bytes = 0
    for fields in raw_tail:
        if any("\x00" in field for field in fields):
            null_bytes += 1
            invalid_rows += 1
            continue
        if len(fields) != len(header):
            invalid_rows += 1
            continue
        rows.append(dict(zip(header, fields)))
    return rows, {
        "invalid_rows": invalid_rows,
        "null_bytes": null_bytes,
        "header": header,
    }


def _history_sampling_seconds(hours: int) -> int:
    """Zielintervall der History-Antwort abhängig vom angeforderten Zeitraum."""
    if hours <= 2:
        return 60
    if hours <= 6:
        return 300
    if hours <= 24:
        return 600
    if hours <= 72:
        return 900
    return 1800


def _downsample_history(rows, cutoff: datetime, max_points: int = 5000):
    """Filtert History zeitbasiert und reduziert sie auf wenige Minutenwerte.

    Die Rohdaten bleiben CSV-basiert; nur die JSON-Antwort wird reduziert.
    ``partial`` zeigt an, wenn der gelesene Tail den angeforderten Zeitraum
    nicht vollständig abdeckt.
    """
    parsed = []
    last_available = None
    for row in rows:
        ts = _parse_zeitstempel(row.get("Zeitstempel"))
        if ts is None:
            continue
        last_available = ts if last_available is None else max(last_available, ts)
        if ts >= cutoff:
            parsed.append((ts, row))
    parsed.sort(key=lambda item: item[0])
    if not parsed:
        return [], {
            "partial": True,
            "returned_hours": 0.0,
            "sampling_seconds": 0,
            "raw_rows_in_range": 0,
            "selected_rows": 0,
            "coverage_start": None,
            "coverage_end": last_available.strftime("%Y-%m-%d %H:%M:%S") if last_available else None,
        }

    sampling = _history_sampling_seconds(max(1, int((parsed[-1][0] - cutoff).total_seconds() / 3600) + 1))
    selected = []
    last_bucket = None
    for ts, row in parsed:
        bucket = int(ts.timestamp() // sampling)
        if bucket != last_bucket:
            selected.append((ts, row))
            last_bucket = bucket
    if selected[-1][0] != parsed[-1][0]:
        selected.append(parsed[-1])
    if len(selected) > max_points:
        step = (len(selected) + max_points - 1) // max_points
        selected = selected[::step]
        if selected[-1][0] != parsed[-1][0]:
            selected.append(parsed[-1])

    raw_start = parsed[0][0]
    raw_end = parsed[-1][0]
    partial = raw_start > cutoff + timedelta(seconds=sampling * 2)
    return [row for _ts, row in selected], {
        "partial": partial,
        "returned_hours": round(max(0.0, (raw_end - cutoff).total_seconds() / 3600), 2),
        "sampling_seconds": sampling,
        "raw_rows_in_range": len(parsed),
        "selected_rows": len(selected),
        "coverage_start": raw_start.strftime("%Y-%m-%d %H:%M:%S"),
        "coverage_end": raw_end.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _berechne_hist_wh_qm(csv_path: str, tage: int = 14, jetzt: datetime = None):
    """Mittlere taegliche Einspeisung in Wh, zeitbasiert integriert.

    FeedinPower ist eine Momentanleistung in Watt. Daher wird jeder Wert mit
    dem tatsaechlichen Abstand zum naechsten Sample multipliziert. Grosse Luecken
    (Service-/SD-Karten-Ausfall) werden nicht als PV-Erzeugung gewertet. Die
    Funktion liest aktuelle Datei und relevante Monatsarchive ohne pandas.
    """
    if jetzt is None:
        jetzt = now_for(shared_state) if shared_state is not None else datetime.now()
    # CSV-Zeitstempel werden bewusst als lokale naive Zeit verarbeitet. Damit
    # Monatsgrenzen und Cache-Zeit garantiert denselben Vergleichstyp.
    jetzt = to_naive(jetzt)
    grenze = jetzt - timedelta(days=max(1, int(tage)))
    if not os.path.exists(csv_path):
        return None

    from pathlib import Path

    basis = Path(csv_path)
    dateien = []
    # Monatsarchive im Format heizungsdaten_YYYY-MM.csv einbeziehen.
    for kandidat in basis.parent.glob(f"{basis.stem}_*.csv"):
        monat = kandidat.stem.rsplit("_", 1)[-1]
        try:
            monat_dt = datetime.strptime(monat, "%Y-%m")
        except ValueError:
            continue
        monats_ende = (monat_dt.replace(day=28) + timedelta(days=4)).replace(day=1)
        if monats_ende > grenze and monat_dt <= jetzt and kandidat not in dateien:
            dateien.append(kandidat)
    if basis not in dateien:
        dateien.append(basis)
    # Archive zuerst, aktuelle Datei zuletzt; Cross-Month-Grenzen werden nur
    # uebernommen, wenn der tatsaechliche Abstand plausibel ist.
    dateien = sorted(
        dateien,
        key=lambda p: (0, p.stem.rsplit("_", 1)[-1])
        if p != basis else (1, "9999-12"),
    )

    tages_energie: dict = {}
    # 15 Minuten ist dasmaximum fuer einen regulaeren Takt; echte 10/14-Sekunden
    # -Samples werden dadurch korrekt integriert, grosse Ausfallluecken nicht.
    max_gap_sec = 900.0
    previous = None
    letzte_delta_sec = None
    for datei in dateien:
        try:
            try:
                rows, _quality = _read_csv_tail(str(datei), max_rows=25000)
            except OSError as exc:
                logging.debug("PV-Historie: %s nicht lesbar (%s)", datei, exc)
                continue
            for row in rows:
                    ts = _parse_zeitstempel(row.get("Zeitstempel"))
                    feedin = _to_float(row.get("FeedinPower"))
                    if ts is None or feedin is None:
                        previous = None
                        letzte_delta_sec = None
                        continue
                    if previous is not None:
                        previous_ts, previous_feedin = previous
                        delta_sec = (ts - previous_ts).total_seconds()
                        tag = previous_ts.date()
                        if previous_ts.date() != ts.date():
                            # Letztes Sample des Vortages noch mit dem
                            # plausiblen letzten Intervall abschliessen.
                            if 0 < delta_sec <= max_gap_sec:
                                wh = max(0.0, previous_feedin) * delta_sec / 3600.0
                                tages_energie[tag] = tages_energie.get(tag, 0.0) + wh
                            elif delta_sec > max_gap_sec and letzte_delta_sec is not None:
                                wh = max(0.0, previous_feedin) * letzte_delta_sec / 3600.0
                                tages_energie[tag] = tages_energie.get(tag, 0.0) + wh
                            letzte_delta_sec = None
                        elif 0 < delta_sec <= max_gap_sec:
                            letzte_delta_sec = delta_sec
                            # Nur tatsaechliche Einspeisung; negative Werte
                            # sind Netzbezug und keine PV-Erzeugung.
                            wh = max(0.0, previous_feedin) * delta_sec / 3600.0
                            tages_energie[tag] = tages_energie.get(tag, 0.0) + wh
                        elif delta_sec > max_gap_sec:
                            letzte_delta_sec = None
                    previous = (ts, feedin)
        except OSError as exc:
            logging.debug("PV-Historie: %s nicht lesbar (%s)", datei, exc)

        # Zwischen zwei Monatsdateien wird der letzte Wert nicht kuenstlich
        # verlaengert. Nur der allerletzte Wert der gesamten Zeitreihe bekommt
        # eine Intervallschatzung.
        if datei == dateien[-1] and previous is not None and letzte_delta_sec is not None and 0 < letzte_delta_sec <= max_gap_sec:
            wh = max(0.0, previous[1]) * letzte_delta_sec / 3600.0
            tag = previous[0].date()
            tages_energie[tag] = tages_energie.get(tag, 0.0) + wh

    tages_werte = [wh for tag, wh in tages_energie.items() if tag >= grenze.date()]
    if not tages_werte:
        return None
    return sum(tages_werte) / len(tages_werte)


def _historisches_wh_qm(csv_path: str):
    """Gecachter 14-Tage-Mittelwert (hoechstens 1x pro HIST_WH_QM_TTL_SEC)."""
    jetzt = to_naive(now_for(shared_state) if shared_state is not None else datetime.now())
    zeit = _HIST_WH_QM_CACHE["zeit"]
    if isinstance(zeit, datetime) and zeit.tzinfo is not None:
        zeit = to_naive(zeit)
    if zeit is not None and (jetzt - zeit).total_seconds() < HIST_WH_QM_TTL_SEC:
        return _HIST_WH_QM_CACHE["wert"]

    wert = None
    try:
        wert = _berechne_hist_wh_qm(csv_path)
    except Exception as e:
        logging.debug(f"Historisches PV-Mittel nicht berechenbar: {e}")
        wert = None

    _HIST_WH_QM_CACHE["zeit"] = jetzt
    _HIST_WH_QM_CACHE["wert"] = wert
    return wert


@app.get("/status")
def get_status():
    if not shared_state:
        raise HTTPException(status_code=503, detail="System not initialized")

    pc = shared_state.priority_config
    priority_info = {}
    if pc:
        nightsperre = False
        if _is_nachtsperre_aktiv:
            nightsperre = _is_nachtsperre_aktiv(pc, _api_now(shared_state))
        priority_info = {
            "beschreibung": pc.beschreibung,
            "wp_leistung": pc.wp.leistung_watt,
            "zyklus_interval": pc.zyklus.interval_minuten,
            "zyklus_min_laufzeit": pc.zyklus.mindestlaufzeit_minuten,
            "zyklus_min_pause": pc.zyklus.mindestpausenzeit_minuten,
            "zyklus_pv_min_laufzeit": pc.zyklus.pv_min_laufzeit_minuten,
            "nachtsperre_aktiv": nightsperre,
            "nachtsperre_start": pc.sicherheit.nachtsperre_start,
            "nachtsperre_ende": pc.sicherheit.nachtsperre_ende,
            "sicherheit_max": pc.sicherheit.max_temp_c,
            "sicherheit_ueberhitzung": pc.sicherheit.ueberhitzung_c,
            "sicherheit_notfall": pc.sicherheit.notfall_c,
            "regeln": [],
        }
        # PV-Regeln
        for pv in pc.pv_regeln:
            priority_info["regeln"].append({
                "name": pv.name, "typ": "pv", "prio": pv.prioritaet,
                "schwellwert": pv.pv_schwelle_watt,
                "sensor": pv.temperaturfuehler,
                "ein": pv.einschalten_bei_c,
                "aus": pv.ausschalten_bei_c,
            })
        # Notfallschutz (Prio 110, ausgekoppelter Schutzleiter)
        priority_info["regeln"].append({
            "name": "Notfallschutz", "typ": "notfallschutz", "prio": pc.notfallschutz.prioritaet,
            "ein": pc.notfallschutz.einschalten_bei_c,
            "aus": pc.notfallschutz.ausschalten_bei_c,
        })
        # Komfort (ohne Notfall - der steckt jetzt im Notfallschutz)
        priority_info["regeln"].append({
            "name": "Komfort", "typ": "komfort", "prio": pc.komfort.prioritaet,
            "min_pvid": pc.komfort.min_pv_fuer_komfort_watt,
            "ein": pc.komfort.komfort_einschalten_bei_c,
            "aus": pc.komfort.ausschalten_bei_c,
        })
        # Zeitfenster
        priority_info["regeln"].append({
            "name": "Zeitfenster", "typ": "zeitfenster", "prio": pc.zeitfenster.prioritaet,
            "start": pc.zeitfenster.start_uhr,
            "ende": pc.zeitfenster.ende_uhr,
            "ein": pc.zeitfenster.max_temp_fuer_einschalten_c,
            "aus": pc.zeitfenster.max_temp_fuer_einschalten_c,
            "min_pv": pc.zeitfenster.min_pv_watt,
        })
                # Abweichung
        priority_info["regeln"].append({
            "name": "Abweichung", "typ": "abweichung", "prio": pc.abweichung.prioritaet,
            "soll": pc.abweichung.solltemperatur_c,
            "sensor": pc.abweichung.temperaturfuehler,
            "ein": pc.abweichung.einschalten_bei_abweichung_k,
            "aus": pc.abweichung.ausschalten_bei_abweichung_k,
        })
        # Wochenende
        priority_info["regeln"].append({
            "name": "Wochenende", "typ": "wochenende", "prio": pc.wochenende.prioritaet,
            "aktiv": pc.wochenende.aktiv,
            "fruehestens_uhr": pc.wochenende.fruehestens_uhr,
        })
        # MindestTemp-Garantien
        for _mt in pc.mindest_temp.eintraege:
            priority_info["regeln"].append({
                "name": f"MinTemp-{_mt.name}", "typ": "mindesttemp",
                "prio": pc.mindest_temp.prioritaet,
                "sensor": _mt.temperaturfuehler,
                "min_c": _mt.min_temp_c,
                "start": _mt.start_uhr,
                "ende": _mt.ende_uhr,
                "nachtsperre_ueberschreiben": _mt.nachtsperre_ueberschreiben,
            })
        # Batterie-Regel
        priority_info["regeln"].append({
            "name": "Batterie", "typ": "batterie", "prio": pc.batterie.prioritaet,
            "aktiv": pc.batterie.aktiv,
            "min_soc": pc.batterie.min_soc_prozent,
            "max_netzbezug_w": pc.batterie.max_netzbezug_watt,
        })
        # Einspeise-Begrenzung (PV-Shaping am Netzlimit)
        priority_info["regeln"].append({
            "name": "Einspeisung", "typ": "einspeisung", "prio": pc.einspeisung.prioritaet,
            "grenze_w": pc.einspeisung.einspeisegrenze_watt,
            "weiterlauf_w": pc.einspeisung.weiterlauf_ab_watt,
            "aus_c": pc.einspeisung.ausschalten_bei_c,
        })
        # Forecast-Regel (Prognose)
        priority_info["regeln"].append({
            "name": "Forecast", "typ": "forecast", "prio": pc.forecast.prioritaet,
            "aktiv": pc.forecast.aktiv,
            "vorheiz_c": pc.forecast.t_vorheiz_ab_c,
            "max_c": pc.forecast.tmax_c,
            "schlecht_wmq": pc.forecast.fc_schwelle_niedrig_wh,
            "gut_wmq": pc.forecast.fc_schwelle_hoch_wh,
        })
        # AdaptivePV-Regel
        priority_info["regeln"].append({
            "name": "AdaptivePV", "typ": "adaptivepv", "prio": pc.adaptive_pv.prioritaet,
            "aktiv": pc.adaptive_pv.aktiv,
            "basis_w": pc.adaptive_pv.base_threshold_watt,
            "sensor": pc.adaptive_pv.temperaturfuehler,
            "max_c": pc.adaptive_pv.tmax_c,
        })
        # CalcStart-Regel
        priority_info["regeln"].append({
            "name": "CalcStart", "typ": "calcstart", "prio": pc.calculated_start.prioritaet,
            "aktiv": pc.calculated_start.aktiv,
            "soll_c": pc.calculated_start.solltemperatur_c,
            "ziel_uhr": pc.calculated_start.target_uhr,
            "max_c": pc.calculated_start.tmax_c,
        })

    # Regel-Ergebnisse (Entscheidungen) aus State
    regel_ergebnisse = []
    if shared_state.control.alle_ergebnisse:
        for e in shared_state.control.alle_ergebnisse:
            regel_ergebnisse.append({
                "name": e.name,
                "prio": e.prioritaet,
                "aktiv": e.aktiv,
                "einschalten": e.einschalten,  # True/False/None
                "grund": e.grund,
                "reason_code": getattr(e, "reason_code", None),
                "regel_dict": getattr(e, "regel_dict", None),
            })

    # Entscheidungs-Historie + KPIs (Fehler hier duerfen /status nie killen)
    entscheidungen_info: list = []
    kpi_info: dict = {}
    try:
        if entscheidungs_log is not None:
            entscheidungen_info = [
                {
                    "ts": e.get("ts"), "gewinner": e.get("gewinner") or "",
                    "grund": e.get("grund") or "",
                    "reason_code": e.get("reason_code") or (e.get("diagnostics") or {}).get("reason_code"),
                    "soll_einschalten": bool(e.get("soll_einschalten")),
                    "laeuft": bool(e.get("kompressor_laeuft")),
                }
                for e in entscheidungs_log.historie(stunden=12, limit=30)
            ]
            wp_leistung_watt = float(getattr(pc, "wp", None) is not None and pc.wp.leistung_watt or 600.0)
            strompreis = float(
                getattr(getattr(shared_state, 'priority_config', None), 'kpi', None)
                and getattr(shared_state.priority_config.kpi, 'strompreis_eur_kwh', 0.35)
                or 0.35
            )
            kpi_info = entscheidungs_log.kpis(wp_leistung_watt, strompreis)
    except Exception as e:
        logging.warning(f"Entscheidungen/KPIs nicht verfuegbar: {e}")

    # Boiler-Fuellstand + Taktschutz + Komfort (Punkte A/B/D)
    boiler_info: dict = {}
    try:
        if boiler_modell is not None:
            cfg_bm = getattr(shared_state.priority_config, "boiler_modell", None)
            liter, anteil = boiler_modell.schaetze_warmwasser(
                {"unten": shared_state.sensors.t_unten,
                 "mittig": shared_state.sensors.t_mittig,
                 "oben": shared_state.sensors.t_oben},
                volumen_l=getattr(cfg_bm, "volumen_l", 150.0),
                nutztemp_c=getattr(cfg_bm, "nutztemp_c", 40.0),
                kaltwasser_c=getattr(cfg_bm, "kaltwasser_c", 10.0),
            )
            boiler_info = {"liter_warm": liter, "anteil_prozent": anteil,
                           "volumen_l": getattr(cfg_bm, "volumen_l", 150.0)}
    except Exception as e:
        logging.debug(f"Boiler-Modell nicht verfuegbar: {e}")

    taktschutz_info: dict = {}
    try:
        cfg_ts = getattr(shared_state.priority_config, "taktschutz", None)
        hardware_hist = getattr(shared_state.control, "_hardware_wechsel_historie", None)
        regel_hist = getattr(shared_state.control, "_wechsel_historie", None)
        taktschutz_info = {
            "wechsel_pro_stunde": len(hardware_hist) if hardware_hist else 0,
            "hardware_schaltvorgaenge_pro_stunde": len(hardware_hist) if hardware_hist else 0,
            "regelwechsel_pro_stunde": len(regel_hist) if regel_hist else 0,
            "max_wechsel": getattr(cfg_ts, "max_wechsel_pro_stunde", 8),
            "aktiv_cfg": getattr(cfg_ts, "aktiv", True),
        }
    except Exception as e:
        logging.debug(f"Taktschutz-Status nicht verfuegbar: {e}")

    komfort_info: dict = {}
    try:

        le = getattr(shared_state, "learning_engine_summary", None)
        if le is not None:
            komfort_info = {
                "verletzungen_7d": le.get("komfort_verletzungen_7d", 0),
                "verletzungen_1d": le.get("komfort_verletzungen_1d", 0),
                "grenz_c": 40.0,
                "bonus_vorlauf_h": 0.0,
            }
    except Exception as e:
        logging.debug(f"Komfort-Info nicht verfuegbar: {e}")

    learning_info: dict = {}
    try:
        le = getattr(shared_state, "learning_engine_summary", None)
        if le is not None:
            learning_info = dict(le) if isinstance(le, dict) else {}
    except Exception as e:
        # Ein Fehler im Lernmodul darf nie den kompletten /status in einen
        # HTTP 500 zwingen (sonst zeigt die WebApp dauerhaft nur "500").
        logging.warning(f"Learning-Engine-Info nicht verfuegbar: {e}")
        learning_info = {}

    # Einmal ermitteln und in 'energy' + 'status_indikatoren' wiederverwenden
    solar_stale = _solar_stale_status()

    pv_profil_info: dict = {}
    forecast_info: dict = {}
    try:
        if _pv_profil_modul is not None:
            # Forecast-Scaling berechnen (heute vs historisch)
            forecast_today = getattr(shared_state.solar, 'forecast_today', None)
            historische_einspeisung_wh = None
            if _pv_profil_modul is not None and hasattr(_pv_profil_modul, 'berechne_forecast_scaling'):
                # Historischer Wert: 14-Tage-Durchschnitt der CSV. Die Funktion
                # integriert FeedinPower zeitbasiert und liefert Wh (Gesamtanlage).
                historische_einspeisung_wh = _historisches_wh_qm(HEIZUNGSDATEN_CSV)

            # Forecast und historische Einspeisung auf dieselbe flächenbezogene
            # Energie beziehen. FeedinPower wird dafür durch die PV-Fläche geteilt.
            forecast_today_wh_m2 = forecast_kwh_m2_to_wh_m2(forecast_today)
            try:
                pv_area = float(
                    getattr(getattr(shared_state.priority_config, "wp", None), "pv_array_size_qm", 10.0)
                )
            except (TypeError, ValueError, OverflowError):
                pv_area = 10.0
            if pv_area <= 0:
                pv_area = 10.0
            historical_wh_m2 = (
                historische_einspeisung_wh / pv_area
                if historische_einspeisung_wh is not None else None
            )
            scaling = None
            if (
                historical_wh_m2 is not None
                and historical_wh_m2 > 0
                and forecast_today_wh_m2 is not None
                and forecast_today_wh_m2 > 0
            ):
                scaling = round(forecast_today_wh_m2 / historical_wh_m2, 3)
                forecast_info["scaling"] = scaling
                forecast_info["historical_feed_in_wh"] = round(historische_einspeisung_wh, 0)
                forecast_info["forecast_irradiation_wh_m2"] = round(forecast_today_wh_m2, 0)
                forecast_info["forecast_vs_historical"] = f"{round((scaling - 1) * 100, 1)}%"

            profil_total = _pv_profil_modul.berechne_profil(forecast_scaling=scaling)
            profil = {hour: round(value / pv_area, 1) for hour, value in profil_total.items()}
            peak = _pv_profil_modul.get_peak_leistung(profil)

            # Forecast ist W/m²; das gelernte Profil ist ebenfalls W/m².
            forecast_hourly = getattr(shared_state.solar, 'forecast_hourly_wm2', None)
            if forecast_hourly and isinstance(forecast_hourly, dict):
                try:
                    forecast_hourly = {k: round(float(v), 1) for k, v in forecast_hourly.items()}
                except (TypeError, ValueError, OverflowError):
                    forecast_hourly = None
            else:
                forecast_hourly = None
            
            pv_profil_info = {
                "stunden": {str(k): v for k, v in sorted(profil.items())},
                "peak_watt": peak,
                "peak_wm2": peak,
                "forecast_stunden": forecast_hourly,
                "forecast_available": forecast_hourly is not None,
            }
    except Exception as e:
        logging.debug(f"PV-Profil/Forecast nicht verfuegbar: {e}")

    status_now = _api_now(shared_state)
    status_age_s = None
    snapshot_at = getattr(shared_state, "_is_status_snapshot_created_at", None)
    if isinstance(snapshot_at, datetime):
        try:
            status_age_s = max(0, int((now_for(shared_state) - snapshot_at).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            status_age_s = None

    response = with_contract_metadata({
        "temperatures": {
            "oben": shared_state.sensors.t_oben,
            "mittig": shared_state.sensors.t_mittig,
            "unten": shared_state.sensors.t_unten,
            "verdampfer": shared_state.sensors.t_verd,
            "boiler": shared_state.sensors.t_boiler
        },
        "compressor": {
            "status": "EIN" if shared_state.control.kompressor_ein else "AUS",
            "runtime_current": str(shared_state.stats.current_runtime).split('.')[0] if shared_state.control.kompressor_ein else "0:00:00",
            "runtime_today": str(shared_state.stats.total_runtime_today).split('.')[0]
        },
        "setpoints": {
            "einschaltpunkt": shared_state.control.aktueller_einschaltpunkt,
            "ausschaltpunkt": shared_state.control.aktueller_ausschaltpunkt,
            "sicherheits_temp": shared_state.sicherheits_temp,
            "verdampfertemperatur": shared_state.verdampfertemperatur
        },
        "mode": build_mode_payload(shared_state),
        "energy": {
            "battery_power": shared_state.solar.batpower,
            "battery_discharge_watt": getattr(shared_state.solar, "battery_discharge_watt", None),
            "battery_charge_watt": getattr(shared_state.solar, "battery_charge_watt", None),
            "energy_source": getattr(shared_state.solar, "energy_source", None),
            "energy_source_detail": getattr(shared_state, "energy_source_detail", None),
            "soc": shared_state.solar.soc,
            "feed_in": shared_state.solar.feedinpower,
            "ac_power": getattr(shared_state.solar, 'acpower', None),
            "forecast_today": getattr(shared_state.solar, 'forecast_today', None),
            "forecast_tomorrow": getattr(shared_state.solar, 'forecast_tomorrow', None),
            "solar_stale": solar_stale,
            "forecast_day2": getattr(shared_state.solar, 'forecast_day2', None),
            "forecast_stale": bool(getattr(shared_state, "forecast_stale", False)),
            "forecast_age_s": getattr(shared_state, "forecast_age_s", None),
            "sunrise": getattr(shared_state.solar, 'sunrise_today', ''),
            "sunset": getattr(shared_state.solar, 'sunset_today', ''),
        },
        "forecast": forecast_info,
        "learning": learning_info,
                "system": {
            "exclusion_reason": shared_state.control.ausschluss_grund or "",
            "last_update": status_now.isoformat(timespec="seconds"),
            "last_update_age_s": status_age_s,
            "loop_heartbeat": getattr(shared_state, "loop_heartbeat", None),
            "consecutive_control_errors": getattr(shared_state.control, "consecutive_control_errors", 0),
        },
        "priority": priority_info,
        "regel_ergebnisse": regel_ergebnisse,
        # Entscheidungs-Historie (letzte 30) + Energiebilanz-KPIs
        "entscheidungen": entscheidungen_info,
        "kpi": kpi_info,
        "boiler": boiler_info,
        "taktschutz": taktschutz_info,
        "komfort": komfort_info,
        "legionellen": {
            "aktiv": getattr(shared_state, 'legionellen_aktiv', False),
            "planned_day": getattr(shared_state, 'legionellen_planned_day', None),
            "planned_time": getattr(shared_state, 'legionellen_planned_time', None),
            "planned_reason": getattr(shared_state, 'legionellen_planned_reason', None),
            "last_done": str(getattr(shared_state, 'legionellen_last_done', '')) if getattr(shared_state, 'legionellen_last_done', None) else None,
            "target_temp_c": getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None).target_temp_c if getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None) else None,
            "max_duration_hours": getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None).max_duration_hours if getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None) else None,
        },
        "pv_profil": pv_profil_info,
        "status_indikatoren": {
            "solar_stale": solar_stale,
            "forecast_stale": bool(getattr(shared_state, "forecast_stale", False)),
            "forecast_age_s": getattr(shared_state, "forecast_age_s", None),
            "verdampfer_shutdowns_stunde": getattr(shared_state.control, 'verdampfer_shutdowns', []),
        },
        "learning_engine": learning_info,
        "debug_info": {
            "solar_power": getattr(shared_state.solar, 'acpower', None),
            "feedin_power": getattr(shared_state.solar, 'feedinpower', None),
            "battery_soc": getattr(shared_state.solar, 'soc', None),
            "kompressor_on": getattr(shared_state.control, 'kompressor_ein', False),
            "active_rule": getattr(shared_state.control, 'active_rule_name', None),
        },
    })
    validate_status_shape(response)
    return response


@app.get("/history/regeln")
def get_history_regeln(
    hours: int = Query(default=24, ge=1, le=336),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """Zeitverlauf der gewinnenden Regel, chronologisch fuer das Chart-Overlay."""
    try:
        eintraege = entscheidungs_log.historie(stunden=hours, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Regel-Historie nicht lesbar: {e}")

    def _sort_key(e):
        try:
            return _parse_zeitstempel(e.get("ts")) or datetime.min
        except (TypeError, ValueError):
            return datetime.min

    # Die Historie-API liefert normalerweise neueste zuerst. Das Frontend
    # benoettigt fuer denstep-/Overlay-Abgleich dagegen aelteste zuerst.
    eintraege = sorted(eintraege, key=_sort_key)
    return {
        "data": [
            {"timestamp": e.get("ts"), "regel": e.get("gewinner") or "Keine",
             "reason_code": e.get("reason_code"),
             "laeuft": bool(e.get("kompressor_laeuft"))}
            for e in eintraege
        ],
        "count": len(eintraege),
    }


@app.post("/config")
def update_config(
    config: ConfigUpdate,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key)
    state = _control_state()
    if not state:
        raise HTTPException(status_code=503, detail="System not initialized")
    
    # Access Pydantic model sections
    section_obj = getattr(state.config, config.section, None)
    if not section_obj:
        raise HTTPException(status_code=404, detail=f"Section {config.section} not found")
    
    if not hasattr(section_obj, config.key):
        raise HTTPException(status_code=404, detail=f"Key {config.key} not found in section {config.section}")

    try:
        # Simple type casting based on current value type if possible, otherwise string
        current_value = getattr(section_obj, config.key)
        new_value = config.value
        
        if isinstance(current_value, bool):
            if config.value.strip().lower() not in {"true", "false", "1", "0"}:
                raise ValueError("Boolean muss true/false oder 1/0 sein")
            new_value = config.value.strip().lower() in {"true", "1"}
        elif isinstance(current_value, int):
             new_value = int(config.value)
        elif isinstance(current_value, float):
            new_value = float(config.value)
            if not math.isfinite(new_value):
                raise ValueError("Wert muss endlich sein")
             
        setattr(section_obj, config.key, new_value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid value for {config.key}: {str(e)}")

    # Änderungen bleiben bewusst Runtime-only; ein Neustart lädt die Datei.
    return {
        "status": "success",
        "persisted": False,
        "message": f"Updated {config.section}.{config.key} to {new_value} (nur für diese Laufzeit)",
    }

@app.post("/control")
async def control_system(
    cmd: ControlCommand,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_api_key(x_api_key)
    state = _control_state()
    if not state:
        raise HTTPException(status_code=503, detail="System not initialized")

    if cmd.command == "set_mode":
        enqueue = (control_funcs or {}).get("enqueue_control")
        if enqueue is not None:
            try:
                enqueue(cmd.command, cmd.params or {})
            except RuntimeError as exc:
                raise HTTPException(status_code=429, detail=str(exc)) from exc
            return {"status": "queued", "message": f"{cmd.params['mode']} queued"}
        mode = cmd.params["mode"]
        active = cmd.params["active"]
        if mode == "bademodus":
            state.bademodus_aktiv = active
        else:
            state.urlaubsmodus_aktiv = active
        return {"status": "success", "message": f"{mode} set to {active}"}

    if not control_funcs:
        raise HTTPException(status_code=503, detail="System not initialized")

    enqueue = control_funcs.get("enqueue_control")
    if enqueue is not None:
        try:
            enqueue(cmd.command, cmd.params or {})
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return {"status": "queued", "message": f"{cmd.command} queued for main loop"}

    # Kompatibilitäts-Fallback für API-Tests/Entwicklung ohne Main-Loop-Queue.
    if "set_kompressor" not in control_funcs:
        raise HTTPException(status_code=503, detail="Control function not available")
    await control_funcs["set_kompressor"](
        state, cmd.command == "force_on", force=True,
        end_grund="api_manuell" if cmd.command == "force_off" else None,
    )
    return {"status": "success", "message": f"{cmd.command} accepted"}


@app.get("/config/export")
def export_config(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """Exportkonfiguration ohne Secrets."""
    _check_api_key(x_api_key)
    state = _control_state()
    if not state:
        raise HTTPException(status_code=503, detail="System not initialized")

    try:
        return {
            "config_ini": _redact_config(
                state.config.model_dump()
                if hasattr(state.config, "model_dump") else {}
            ),
            "priority_config": _redact_config(
                state.priority_config.model_dump()
                if hasattr(state.priority_config, "model_dump") else {}
            ),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Konfiguration konnte nicht exportiert werden") from exc


@app.post("/command")
async def handle_command(
    cmd: ControlCommand,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """Legacy-Kommandoroute; für set_mode bleibt /control kanonisch."""
    _check_api_key(x_api_key)
    if not _control_state():
        raise HTTPException(status_code=503, detail="System not initialized")

    if cmd.command == "set_mode":
        return await control_system(cmd, x_api_key)

    raise HTTPException(status_code=400, detail="Unknown command")



def _csv_spaltentypen(kopf, daten):
    """Einfache Typ-Heuristik fuer /debug/csv (ersetzt die pandas-dtype-Ausgabe)."""
    typen = {}
    for i, name in enumerate(kopf):
        werte = [z[i] for z in daten if i < len(z) and str(z[i]).strip() != ""]
        if not werte:
            typen[name] = "leer"
        elif all(_to_float(w) is not None for w in werte):
            typen[name] = "float"
        else:
            typen[name] = "text"
    return typen


@app.get("/debug/csv")
def debug_csv(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    """Geschützte CSV-Qualitätsdiagnose ohne absolute Pfade oder Rohfehler."""
    _check_api_key(x_api_key)
    import os as _os
    from collections import deque
    import csv

    csv_path = HEIZUNGSDATEN_CSV
    # Diagnose gibt bewusst nur den Dateinamen zurück. Ein relativer Pfad
    # könnte bei einem abweichenden Arbeitsverzeichnis ebenfalls interne
    # Verzeichnisnamen preisgeben.
    display_path = os.path.basename(csv_path)
    result = {
        "csv_path": display_path,
        "csv_exists": _os.path.exists(csv_path),
    }
    if result["csv_exists"]:
        result["size_bytes"] = _os.path.getsize(csv_path)
        try:
            anzahl = 0
            _tail = deque(maxlen=1000)
            _erste_datenzeile = ""
            with open(csv_path, "r", encoding="utf-8") as _f:
                _header = _f.readline()
                for _zeile in _f:
                    if anzahl == 0:
                        _erste_datenzeile = _zeile
                    _tail.append(_zeile)
                    anzahl += 1

            reader = csv.reader([_header] + list(_tail))
            kopf = next(reader, [])
            daten = list(reader)
            result["rows"] = anzahl
            result["columns"] = kopf
            _erste_feld = _erste_datenzeile.split(",")[0].strip()
            result["first_timestamp"] = _erste_feld or None
            _letzte = daten[-1] if daten else []
            if "Zeitstempel" in kopf and _letzte:
                result["last_timestamp"] = _letzte[kopf.index("Zeitstempel")].strip() or None
            else:
                result["last_timestamp"] = None
            result["column_types"] = _csv_spaltentypen(kopf, daten)
        except Exception:
            logging.exception("CSV-Debugdiagnose fehlgeschlagen")
            result["read_error"] = "CSV konnte nicht gelesen werden"
    return result

@app.get("/analysis/quality")
def get_analysis_quality():
    """Read-only, redigierter Qualitätsbericht der automatischen Analyse."""
    report_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "analyse_auto", "quality_report.json",
    )
    # Der Analyse-Timer schreibt bewusst in logs/analyse_auto. In Tests oder
    # bei abweichendem CWD darf der Pfad über die Umgebung gesetzt werden.
    report_path = os.environ.get("WPS_ANALYSIS_QUALITY_FILE", report_path)
    if not os.path.isfile(report_path):
        raise HTTPException(status_code=404, detail="Analyse-Qualitätsbericht noch nicht vorhanden")
    try:
        with open(report_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("Analyse-Qualitätsbericht nicht lesbar: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Analyse-Qualitätsbericht nicht verfügbar") from exc
    if not isinstance(report, dict):
        raise HTTPException(status_code=503, detail="Analyse-Qualitätsbericht ungültig")
    return report


@app.get("/history")
def get_history(hours: int = Query(default=24, ge=1, le=168)):
    """Get historical data from CSV. Hours must be between 1 and 168 (7 days)."""
    csv_path = HEIZUNGSDATEN_CSV
    if not os.path.exists(csv_path):
        raise HTTPException(status_code=404, detail="No historical data available")

    try:
        # Für 7 Tage reicht der Tail nicht bei 14-Sekunden-Samples.
        # großzügiger Tail plus zeitbasiertes Downsampling hält RAM und Antwort klein.
        max_rows = max(25000, int(hours * 3600 / 5) + 5000)
        rows, quality = _read_csv_tail(csv_path, max_rows=min(max_rows, 100000))
        cutoff = to_naive(now_for(shared_state)) - timedelta(hours=hours)
        rows, coverage = _downsample_history(rows, cutoff, max_points=5000)
        quality.update(coverage)
        quality["requested_hours"] = hours
        now = to_naive(now_for(shared_state))
        coverage_end = _parse_zeitstempel(quality.get("coverage_end"))
        if coverage_end is not None and now - coverage_end > timedelta(seconds=quality.get("sampling_seconds", 60) * 2):
            quality["partial"] = True
            quality["stale_end_s"] = round((now - coverage_end).total_seconds(), 1)

        # Convert to JSON-friendly format
        data = []
        for row in rows:
            ts = _parse_zeitstempel(row.get("Zeitstempel"))
            if ts is None or ts < cutoff:
                continue
            data.append({
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "t_oben": _to_float(row.get("T_Oben")),
                "t_mittig": _to_float(row.get("T_Mittig")),
                "t_unten": _to_float(row.get("T_Unten")),
                "t_verd": _to_float(row.get("T_Verd")),
                "kompressor": row.get("Kompressor") or "",
                "einschaltpunkt": _to_float(row.get("Einschaltpunkt")),
                "ausschaltpunkt": _to_float(row.get("Ausschaltpunkt")),
            })

        return {
            "data": data,
            "count": len(data),
            "quality": quality,
        }
    except Exception as e:
        logging.error(f"Error reading history: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error reading history: {str(e)}")
