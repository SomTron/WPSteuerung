import asyncio
from collections import deque

import logging
import threading
import signal
from queue import Empty, Full, Queue
import uvicorn
import aiofiles
import os
from datetime import datetime, timedelta, date

# Modules
from config_manager import ConfigManager
from state import State
from sensors import SensorManager
from hardware import HardwareManager
from hardware_mock import MockHardwareManager
from hardware_actuator import CompressorActuator
from atomic_io import atomic_write_text
from clock import Clock, now_for
from logging_config import setup_logging
from solax import get_solax_data
import control_logic
import priority_control_logic as pcl
from runtime_validation import (
    RuntimeContractError,
    validate_runtime_contract,
    validate_startup_dependencies,
)
import startup_diagnose
from telegram_handler import telegram_task
from telegram_ui import send_welcome_message, escape_markdown
from telegram_api import start_healthcheck_task, create_robust_aiohttp_session
from telegram_charts import get_boiler_temperature_history, get_runtime_bar_chart
from vpn_manager import check_vpn_status
from api import app, init_api, update_status_snapshot
from utils import safe_timedelta, HEIZUNGSDATEN_CSV, EXPECTED_CSV_HEADER, check_and_fix_csv_header, rotiere_csv_monatlich
from learning_engine import LearningEngine
from weather_forecast import get_solar_forecast
from cycle_logging import CYCLE_CSV, begin_cycle, ensure_cycle_csv, finish_cycle, update_cycle_maxima
from legionellen_plan import clear_plan, load_plan, save_plan
from energy_source import (
    batterie_entladung_watt,
    batterie_ladung_watt,
    classify_energy_source,
)
from logic_utils import (
    check_log_throttle,
    evaluate_sommer_modus,
    normalize_forecast_wh_qm,
    SOMMER_AKTIVIERT, SOMMER_DEAKTIVIERT_PROGNOSE, SOMMER_DEAKTIVIERT_DATEN,
)
from constants import (
    VPN_CHECK_INTERVAL_SEC,
    FORECAST_UPDATE_INTERVAL_HOURS,
    FORECAST_RETRY_INTERVAL_MIN,
    MAIN_LOOP_INTERVAL_SEC,
    COMPRESSOR_VERIFICATION_ERROR_THRESHOLD,
    SOLAR_DATA_STALE_THRESHOLD_MIN,
    SOLAR_REFRESH_DEADLINE_SEC,
    SOLAR_REFRESH_INTERVAL_SEC,
    MEMORY_LOG_INTERVAL_SEC,
)

# Global objects
config_manager = ConfigManager()
state = None
sensor_manager = None
hardware_manager = None
compressor_actuator = None
stop_event = threading.Event()

# Referenzen auf Hintergrund-Tasks halten (ohne Referenz kÃ¶nnen sie vom
# Garbage Collector eingesammelt werden, wÃ¤hrend sie noch laufen!)
background_tasks = []
control_command_queue: Queue = Queue(maxsize=16)


def _state_now(state):
    return now_for(state)


def _state_monotonic(state):
    clock = getattr(state, "clock", None)
    if isinstance(clock, Clock):
        return clock.monotonic()
    import time
    return time.monotonic()


def enqueue_control_command(command: str, params=None) -> None:
    """Thread-sichere Übergabe manueller API-Befehle an den Main-Loop."""
    try:
        control_command_queue.put_nowait((command, params or {}))
    except Full as exc:
        raise RuntimeError("Steuerbefehl-Queue ist voll") from exc


def _pop_control_commands():
    commands = []
    while True:
        try:
            commands.append(control_command_queue.get_nowait())
        except Empty:
            break
    return commands

def _log_task_exception(task):
    """Loggt unerwartete Fehler aus Hintergrund-Tasks (verhindert stillen Absturz)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logging.error(
            f"Hintergrund-Task '{task.get_name()}' wurde mit Fehler beendet: {exc}",
            exc_info=exc,
        )

def handle_exit(signum, frame):
    # Wichtig: Hier bewusst KEIN sys.exit()!
    # SystemExit wÃ¼rde den finally-Block in main_loop Ã¼berspringen
    # (GPIO-Cleanup und session.close() wÃ¼rden nie laufen).
    # Wir setzen nur das Stop-Event; der Loop beendet sich kontrolliert selbst.
    logging.info(f"Signal {signum} empfangen. Beende Programm...")
    stop_event.set()

def _record_hardware_change(state, now, status):
    """Zeichnet nur echte Hardware-Übergänge für den Taktschutz auf."""
    try:
        hist = getattr(state.control, "_hardware_wechsel_historie", None)
        if not isinstance(hist, deque):
            hist = deque(maxlen=32)
            state.control._hardware_wechsel_historie = hist
        cutoff = now - timedelta(hours=1)
        while hist and isinstance(hist[0][0], datetime) and hist[0][0] < cutoff:
            hist.popleft()
        hist.append((now, bool(status)))
    except Exception:
        logging.debug("Hardware-Wechselhistorie nicht aktualisierbar", exc_info=True)


async def _set_hardware_state(state, status: bool) -> bool:
    """Schreibt den Kompressor ausschließlich über den zentralen Aktuator."""
    global compressor_actuator
    if compressor_actuator is None or compressor_actuator.hardware is not hardware_manager:
        # Kompatibler Fallback für isolierte Tests vor setup_application().
        if hardware_manager is None:
            return False
        compressor_actuator = CompressorActuator(
            hardware_manager, getattr(state, "gpio_lock", None)
        )
    return await compressor_actuator.set_state(status)


async def set_kompressor_status(state, status, force=False, t_boiler_oben=None, end_grund=None):
    """
    Schaltet den Kompressor und aktualisiert den State sowie Statistiken.
    """
    now = _state_now(state)
    was_ein = state.control.kompressor_ein

    if status:
        # Bereits laufender Zyklus darf durch ein redundantes force_on weder
        # Laufzeit noch Start-Snapshot/Historieneintrag ueberschreiben.
        if was_ein:
            if force:
                if not await _set_hardware_state(state, True):
                    return False
            return True

        if not await _set_hardware_state(state, True):
            state.control.blocking_reason = "Kompressor-Einschalten fehlgeschlagen"
            return False
        state.control.kompressor_ein = True
        pending_rule = getattr(state.control, "_pending_start_rule", None)
        manual_rule = getattr(state.control, "_lauf_start_regel", None) == "API force_on"
        if manual_rule:
            state.control.source_at_start = "Manuell"
        else:
            state.control.source_at_start = (
                getattr(state.control, "source_at_start", None)
                or getattr(state.control, "_pending_start_source", None)
                or getattr(getattr(state, "solar", None), "energy_source", None)
                or pcl._aktuelle_quellenbezeichnung(state)
            )
        state.control.source_current = state.control.source_at_start
        if manual_rule or pending_rule:
            state.control._lauf_start_regel = (
                "API force_on" if manual_rule else pending_rule
            )
            state.control.effective_rule_name = state.control._lauf_start_regel
            state.control.active_rule_name = state.control._lauf_start_regel
        _record_hardware_change(state, now, True)
        
        # Statistiken aktualisieren + Zyklus-ID je Kompressor-Lauf inkrementieren
        state.stats.last_compressor_on_time = now
        state.control.zyklus_id = getattr(state.control, 'zyklus_id', 0) + 1
        begin_cycle(state, now, state.control.zyklus_id)
        
        # Startwerte fÃ¼r Verifizierung speichern
        state.kompressor_verification_start_time = now
        state.kompressor_verification_start_t_verd = state.sensors.t_verd
        state.kompressor_verification_start_t_unten = state.sensors.t_unten
        state.kompressor_verification_last_check = None
        logging.info(f"Kompressor EIN (cycle={getattr(state.control, 'zyklus_id', '?')}) - Verifizierung gestartet (t_verd={state.sensors.t_verd}, t_unten={state.sensors.t_unten})")
        
        return True
    else:
        # Ausschalten
        if not was_ein:
            # Idempotenter manueller Off-Befehl: keine neue Pause, keine
            # Statistikänderung und kein künstliches Verlängern der Sperre.
            if force:
                return await _set_hardware_state(state, False)
            return True

        if not await _set_hardware_state(state, False):
            state.control.blocking_reason = "Kompressor-Ausschalten fehlgeschlagen"
            return False
        state.control.kompressor_ein = False
        state.control.effective_rule_name = None
        state.control.active_rule_name = None
        state.control.effective_source = None
        state.control._lauf_start_regel = None
        state.control.source_at_start = None
        _record_hardware_change(state, now, False)
        
        # Statistiken nur bei einem echten Übergang EIN -> AUS aktualisieren.
        state.stats.last_compressor_off_time = now
        if was_ein and state.stats.last_compressor_on_time:
            elapsed = safe_timedelta(now, state.stats.last_compressor_on_time, state.local_tz)
            state.stats.total_runtime_today += elapsed
            state.stats.last_completed_cycle = now
            zyklus = getattr(state.control, 'zyklus_id', '?')
            finish_cycle(state, now, end_grund=end_grund)
            abschaltgrund = end_grund or getattr(state.control, "blocking_reason", None) or "unbekannt"
            logging.info(
                f"Kompressor AUS (cycle={zyklus}). Laufzeit: {elapsed} "
                f"reason={abschaltgrund}"
            )
        else:
            zyklus = getattr(state.control, 'zyklus_id', '?')
            logging.info(f"Kompressor AUS (cycle={zyklus})")
            
        return True

async def handle_pressure_check(session, state):
    """Liest den Druckschalter ueber den HardwareManager.

    Reine Lese-Funktion: Die Erkennung von Zustandsaenderungen inkl. Logging
    und Setzen von ausschluss_grund passiert in pcl.check_pressure_and_config --
    dort wird auch state.control.last_pressure_state gepflegt."""
    return hardware_manager.read_pressure_sensor()

def run_api():
    """Startet den FastAPI-Server."""
    try:
        # Host/Port aus Config
        host = state.config.Heizungssteuerung.API_HOST
        port = state.config.Heizungssteuerung.API_PORT
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except Exception as e:
        logging.error(f"Fehler beim Starten der API: {e}")

async def setup_application():
    """Initialisiert Konfiguration, Hardware, Sensoren und API."""
    global state, sensor_manager, hardware_manager, compressor_actuator
    
    # 0. Sicherheitsvertrag und Import-Smoke-Test vor jedem Hardwarezugriff.
    validate_startup_dependencies()
    # 1. Config laden
    config_manager.load_config()
    
    # 2. State init
    state = State(config_manager)
    try:
        validate_runtime_contract(state)
    except RuntimeContractError:
        logging.critical("Runtime-Vertrag ungültig - Starte nicht mit Hardware", exc_info=True)
        raise
    load_plan(state)
    # Nach einem sauberen Service-Restart die letzte AUS-Zeit aus dem
    # Snapshot uebernehmen. Sonst waere die JSON-Mindestpause nach einem
    # Neustart nicht mehr geschuetzt und ein Kompressor koennte erneut
    # direkt nach dem vorherigen Lauf starten.
    restore_persisted_compressor_pause(state)
    learning_engine = LearningEngine()
    state.learning_engine = learning_engine
    
    # 3. Logging setup
    setup_logging(enable_full_log=True, telegram_config=state.config.Telegram)
    logging.info("Starten der Wärmepumpensteuerung (Refactored)...")

    # 3b. Startup-Diagnose: Wurde der VORIGE Lauf unsauber beendet (z. B.
    # OOM-Kill)? Muss VOR markiere_lauf_start() passieren, sonst ist die
    # Information ueberschrieben. Meldung erfolgt im main_loop (Session/TG).
    state.letzter_lauf = startup_diagnose.pruefe_lauf_start_grund()
    startup_diagnose.markiere_lauf_start()

    # 4. Hardware & Sensors init
    try:
        import RPi.GPIO  # noqa: F401 - Pi-Erkennung
        hardware_manager = HardwareManager()
        logging.info("Using real hardware (Raspberry Pi detected)")
    except ImportError:
        hardware_manager = MockHardwareManager()
        logging.info("Using mock hardware (non-Raspberry Pi platform)")
    
    hardware_manager.init_gpio()
    compressor_actuator = CompressorActuator(
        hardware_manager, getattr(state, "gpio_lock", None)
    )
    if not await _set_hardware_state(state, False):
        raise RuntimeError("Kompressor-GPIO konnte nicht fail-safe auf AUS gesetzt werden")
    logging.info("Kompressor-Hardware fail-safe initialisiert: AUS")
    await hardware_manager.init_lcd()
    
    sensor_manager = SensorManager()
    
    # 5. API init
    control_funcs = {
        "set_kompressor": set_kompressor_status,
        "enqueue_control": enqueue_control_command,
    }
    init_api(state, control_funcs)
    
    # Start API Thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    
    # 6. Session & Tasks
    session = create_robust_aiohttp_session()
    state.session = session
    
    
    # 7. CSV: Monatsrotation pruefen (holt ggf. den Rueckstand nach Ausfall),
    # dann Header-Check
    try:
        archiv = rotiere_csv_monatlich(HEIZUNGSDATEN_CSV)
        if archiv:
            logging.info(f"CSV-Monatsrotation beim Start: {archiv}")
    except Exception as e:
        logging.error(f"CSV-Rotation beim Start fehlgeschlagen: {e}")

    # 7b. CSV Header Check (einmalig beim Start)
    try:
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
            # Neue Datei beim Start direkt anlegen
            async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
            logging.info(f"Created new CSV file: {csv_file}")
        else:
            if check_and_fix_csv_header(csv_file):
                logging.warning("CSV Header was redundant/fixed at startup.")
            else:
                logging.info("CSV Header check passed.")
    except Exception as e:
        logging.error(f"Startup CSV check failed: {e}")

    # 7c. Abgeschlossene Kompressorzyklen persistent vorbereiten/rotieren.
    ensure_cycle_csv(CYCLE_CSV, jetzt=_state_now(state))

    # 8. Start Telegram Task
    tg_task = asyncio.create_task(telegram_task(
        read_temperature_func=sensor_manager.read_temperature,
        sensor_ids=sensor_manager.sensor_ids,
        kompressor_status_func=lambda: state.control.kompressor_ein,
        current_runtime_func=lambda: state.stats.current_runtime,
        total_runtime_func=lambda: state.stats.total_runtime_today + state.stats.current_runtime,
        config=state.config,
        get_solax_data_func=get_solax_data,
        state=state,
        get_temperature_history_func=get_boiler_temperature_history,
        get_runtime_bar_chart_func=get_runtime_bar_chart,
        is_nighttime_func=lambda config: pcl._is_nachtsperre_aktiv(state.priority_config, _state_now(state)),
        is_solar_window_func=control_logic.is_solar_window
    ))

    # Start Healthcheck Task
    hc_task = asyncio.create_task(start_healthcheck_task(session, state))

    # Solar-Live-Daten laufen außerhalb des sicherheitskritischen 10-s-Loops.
    solar_task = asyncio.create_task(solar_refresh_loop(session, state))

    # Referenzen halten und Crash-Fruehwarnung aktivieren
    for task in (tg_task, hc_task, solar_task):
        task.add_done_callback(_log_task_exception)
        background_tasks.append(task)
    
    return session

def handle_day_transition(state, now):
    """FÃ¼hrt Aktionen beim Tageswechsel durch."""
    current_date = now.date()
    if state.stats.last_day is None:
        state.stats.last_day = current_date
    elif state.stats.last_day != current_date:
        logging.info(f"Tageswechsel erkannt ({state.stats.last_day} -> {current_date}). Setze Statistiken zurÃ¼ck.")

        # Endstand des alten Tages sichern (inkl. Anteil nach Mitternacht),
        # BEVOR der Tageszaehler zurueckgesetzt wird.
        alter_tag_gesamt = state.stats.total_runtime_today

        # Falls der Kompressor Ã¼ber Mitternacht lÃ¤uft: Restzeit des alten Tages dazurechnen
        if state.control.kompressor_ein and state.stats.last_compressor_on_time:
            # Ende des alten Tages (23:59:59.999...)
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            elapsed_old_day = safe_timedelta(midnight, state.stats.last_compressor_on_time, state.local_tz)
            if elapsed_old_day.total_seconds() > 0:
                alter_tag_gesamt += elapsed_old_day
                logging.info(f"Laufzeitanteil alter Tag: {elapsed_old_day}")

            # Startzeit fÃ¼r neuen Tag auf Mitternacht setzen
            state.stats.last_compressor_on_time = midnight

        # Vortags-Wert fuer Statistik/Anzeige erhalten (statt verwerfen),
        # Tageszaehler fuer den neuen Tag nullen.
        state.stats.total_runtime_yesterday = alter_tag_gesamt
        state.stats.total_runtime_today = timedelta()

        # CSV-Monatsrotation pruefen (billig, einmal pro Tag)
        try:
            archiv = rotiere_csv_monatlich(heute=now)
            if archiv:
                logging.info(f"CSV-Monatsrotation am Tageswechsel: {archiv}")
        except Exception as e:
            logging.error(f"CSV-Rotation am Tageswechsel fehlgeschlagen: {e}")
        state.stats.last_completed_cycle = None
        state.stats.last_day = current_date

async def solar_refresh_loop(session, state) -> None:
    """Hintergrund-Refresh für Solax; harte Deadline, nie im 10-s-Regelloop."""
    while True:
        try:
            await asyncio.wait_for(
                get_solax_data(session, state),
                timeout=SOLAR_REFRESH_DEADLINE_SEC,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Solax-Hintergrund-Refresh fehlgeschlagen")
        await asyncio.sleep(SOLAR_REFRESH_INTERVAL_SEC)


async def update_system_data(session, state, refresh_solar: bool = True):
    """Liest Sensoren und PV-Daten."""
    # 1. Sensoren lesen
    temps = await sensor_manager.get_all_temperatures()
    state.sensors.t_oben = temps.get("oben")
    state.sensors.t_mittig = temps.get("mittig")
    state.sensors.t_unten = temps.get("unten")
    state.sensors.t_verd = temps.get("verd")
    # t_boiler ist der obere Boiler-Fühler; die WebApp bezeichnet ihn nicht
    # als Durchschnitt (siehe API-/Frontend-Vertrag).
    state.sensors.t_boiler = temps.get("oben")
    
    # 2. PV-Daten aktualisieren. Im Produktivbetrieb übernimmt der
    # Hintergrund-Task; der Parameter bleibt fuer direkte Tests/Diagnose.
    if refresh_solar:
        try:
            await asyncio.wait_for(
                get_solax_data(session, state),
                timeout=SOLAR_REFRESH_DEADLINE_SEC,
            )
        except asyncio.TimeoutError:
            logging.exception("Solax-API-Timeout trotz Retry")
        except Exception:
            logging.exception("Unerwarteter Fehler beim Solax-API-Abruf")

    # Stale-Schutz ZUERST prüfen. Ein fehlender oder ungültiger Zeitstempel
    # darf niemals dazu führen, dass last_api_data wieder als frisch übernommen wird.
    alter_min = None
    last_api_call = getattr(state.solar, "last_api_call", None)
    if isinstance(last_api_call, datetime):
        try:
            alter_min = safe_timedelta(
                _state_now(state), last_api_call, state.local_tz
            ).total_seconds() / 60
        except (TypeError, ValueError):
            alter_min = None
    else:
        alter_min = None

    frisch = alter_min is not None and 0 <= alter_min <= SOLAR_DATA_STALE_THRESHOLD_MIN
    if not frisch:
        if check_log_throttle(state, "_log_solar_stale", interval_minutes=5):
            if alter_min is not None and alter_min > SOLAR_DATA_STALE_THRESHOLD_MIN:
                logging.warning(
                    f"Solar-Daten veraltet ({alter_min:.0f} min > "
                    f"{SOLAR_DATA_STALE_THRESHOLD_MIN} min) - PV-Werte auf 0 gesetzt"
                )
            else:
                logging.warning("Solar-Datenzeitstempel fehlt/ungültig - PV-Werte auf 0 gesetzt")
        state.solar.acpower = 0.0
        state.solar.feedinpower = 0.0
        state.solar.batpower = 0.0
        state.solar.soc = 0.0
        state.solar.battery_discharge_watt = 0.0
        state.solar.battery_charge_watt = 0.0
    elif state.solar.last_api_data:
        state.solar.acpower = state.solar.last_api_data.get("acpower", 0)
        state.solar.feedinpower = state.solar.last_api_data.get("feedinpower", 0)
        state.solar.batpower = state.solar.last_api_data.get("batPower", 0)
        state.solar.soc = state.solar.last_api_data.get("soc", 0)
        state.solar.battery_discharge_watt = batterie_entladung_watt(state.solar.batpower) or 0.0
        state.solar.battery_charge_watt = batterie_ladung_watt(state.solar.batpower) or 0.0
    else:
        state.solar.acpower = 0.0
        state.solar.feedinpower = 0.0
        state.solar.batpower = 0.0
        state.solar.soc = 0.0
        state.solar.battery_discharge_watt = 0.0
        state.solar.battery_charge_watt = 0.0

    cfg = getattr(getattr(state, "priority_config", None), "batterie", None)
    source_status = classify_energy_source(
        pv_acpower=state.solar.acpower,
        feedin_watt=state.solar.feedinpower,
        battery_discharge_watt=state.solar.battery_discharge_watt,
        soc=state.solar.soc,
        solar_stale=not frisch,
        pv_min_watt=float(getattr(cfg, "pv_einspeisung_min_watt", 50.0)),
        battery_min_watt=float(getattr(cfg, "min_batterieleistung_watt", 50.0)),
        soc_min_prozent=float(getattr(cfg, "min_soc_prozent", 90.0)),
        max_netzkauf_watt=float(getattr(cfg, "max_netzbezug_watt", -50.0)),
    )
    state.solar.energy_source = source_status.quelle.value
    state.energy_source_detail = source_status.begruendung


def track_api_error(state, api_name: str, error_type: str):
    """Zeichnet einen API-Fehler im State auf fuer das Health-Monitoring."""
    now = _state_now(state)
    if api_name not in state.api_errors:
        state.api_errors[api_name] = {"errors": [], "last_alert": None}
    state.api_errors[api_name]["errors"].append((now, error_type))
    # Nur die letzten 100 Fehler behalten (Speicherschutz)
    if len(state.api_errors[api_name]["errors"]) > 100:
        state.api_errors[api_name]["errors"] = state.api_errors[api_name]["errors"][-100:]


async def check_api_health(session, state):
    """
    Ueberprueft die API-Fehlerdichte der letzten 30 Minuten.
    Bei mehr als 10 Fehlern pro API wird eine Warnung via Telegram gesendet
    (maximal alle 60 Minuten).
    """
    from datetime import timedelta
    now = _state_now(state)
    threshold_30min = now - timedelta(minutes=30)

    for api_name, data in state.api_errors.items():
        # Fehler der letzten 30 Minuten zaehlen
        recent = [e for e in data["errors"] if e[0] > threshold_30min]
        if len(recent) < 10:
            continue  # Weniger als 10 Fehler in 30 Min -> kein Alarm

        # Pruefen ob bereits eine Warnung in den letzten 60 Min gesendet wurde
        last_alert = data.get("last_alert")
        if last_alert and (now - last_alert).total_seconds() < 3600:
            continue

        # Fehlertypen analysieren
        error_counts = {}
        for _, err_type in recent:
            error_counts[err_type] = error_counts.get(err_type, 0) + 1

        fehler_details = ", ".join(f"{t}: {c}x" for t, c in sorted(error_counts.items(), key=lambda x: -x[1]))
        warnung = (
            f"⚠️ API-Warnung: {api_name}\n"
            f"{len(recent)} Fehler in 30 Minuten\n"
            f"Details: {fehler_details}"
        )
        logging.warning(warnung)

        # Telegram-Alarm senden (falls konfiguriert)
        if state.bot_token and state.chat_id:
            try:
                from telegram_api import send_telegram_message
                await send_telegram_message(session, state.chat_id, warnung, state.bot_token)
                logging.info(f"API-Health-Warnung via Telegram gesendet: {api_name}")
            except Exception as e:
                logging.error(f"API-Health-Warnung konnte nicht via Telegram gesendet werden: {e}")

        data["last_alert"] = now
def _as_local_datetime(value, local_tz, default=None):
    """Normalisiert einen Zeitstempel fail-safe auf die lokale Zeitzone."""
    if not isinstance(value, datetime):
        return default
    if value.tzinfo is not None:
        return value
    try:
        return local_tz.localize(value)
    except AttributeError:
        return value.replace(tzinfo=local_tz)


async def check_periodic_tasks(session, state, last_vpn_check):
    """FÃ¼hrt zeitgesteuerte Hintergrundaufgaben aus."""
    now_dt = _state_now(state)
    now_local = now_dt
    
    # 1. VPN Check -- alte/ungültige Snapshots dürfen den Loop nicht beenden.
    last_vpn_check = _as_local_datetime(last_vpn_check, state.local_tz)
    if last_vpn_check is None:
        last_vpn_check = now_dt - timedelta(seconds=VPN_CHECK_INTERVAL_SEC)
    vpn_due = (
        safe_timedelta(now_dt, last_vpn_check, state.local_tz).total_seconds()
        >= VPN_CHECK_INTERVAL_SEC
    )
    if vpn_due:
        await check_vpn_status(state)
        last_vpn_check = now_dt
    
    # 2. Solar Forecast (alle FORECAST_UPDATE_INTERVAL_HOURS)
    last_forecast_update = _as_local_datetime(
        getattr(state, "last_forecast_update", None), state.local_tz
    )
    forecast_due = (
        last_forecast_update is None
        or safe_timedelta(now_local, last_forecast_update, state.local_tz).total_seconds()
        >= FORECAST_UPDATE_INTERVAL_HOURS * 3600
    )
    if forecast_due:
        # Retry-Throttle: Ein Fehlversuch (Netz/DNS weg) darf NICHT im
        # 10-s-Loop-Takt wiederholt werden - sonst API-/Log-Spam. Erst nach
        # FORECAST_RETRY_INTERVAL_MIN erneut fragen.
        letzter_versuch = _as_local_datetime(
            getattr(state, "last_forecast_attempt", None), state.local_tz
        )
        versuch_faellig = (
            letzter_versuch is None
            or safe_timedelta(now_local, letzter_versuch, state.local_tz).total_seconds()
            >= FORECAST_RETRY_INTERVAL_MIN * 60
        )
    else:
        versuch_faellig = False

    if versuch_faellig:
        state.last_forecast_attempt = now_local
        rad_today, rad_tomorrow, rad_day2, sr_today, ss_today, sr_tomorrow, ss_tomorrow, hourly_today_wm2 = await get_solar_forecast(session, state.config)
        # Nur ein vollständiger Tagesforecast gilt als erfolgreich. Sonst
        # dürfen weder alte Prognose noch alter Legionellenplan weiterlaufen.
        if any(value is None for value in (rad_today, rad_tomorrow, rad_day2)):
            state.last_forecast_update = None
            pcl._set_stale_forecast(state)
        else:
            state.solar.forecast_today = rad_today
            state.solar.forecast_hourly_wm2 = hourly_today_wm2
            state.solar.forecast_tomorrow = rad_tomorrow
            state.solar.forecast_day2 = rad_day2
            state.solar.sunrise_today = sr_today
            state.solar.sunset_today = ss_today
            state.sunrise_tomorrow = sr_tomorrow
            state.sunset_tomorrow = ss_tomorrow
            state.last_forecast_update = now_local
            state.forecast_stale = False
            state.forecast_age_s = 0

            # --- Sommer-Modus: max. EINE Bewertung pro Kalendertag ---
            # Aktivierung erst nach 'benoetigte_tage' AUFENANDERFOLGENDEN Kalendertagen
            # mit durchgehend guter Prognose (heute+morgen+uebermorgen >= Schwelle).
            # Idee: Kommt sicher PV-Ueberschuss, ist Vorheizen/Buffern unnoetig ->
            # die Solltemperatur wird dann um temperatur_offset_c gesenkt (s. pcl).
            sommer_cfg = state.priority_config.sommer_modus
            rad_today_wh = normalize_forecast_wh_qm(rad_today)
            rad_tomorrow_wh = normalize_forecast_wh_qm(rad_tomorrow)
            rad_day2_wh = normalize_forecast_wh_qm(rad_day2)
            if sommer_cfg.aktiv:
                neuer_zaehler, ist_aktiv, bewertungstag, ereignis = evaluate_sommer_modus(
                    benoetigte_tage=sommer_cfg.benoetigte_tage,
                    mindest_prognose_wh=sommer_cfg.mindest_prognose_wh,
                    rad_today=rad_today_wh,
                    rad_tomorrow=rad_tomorrow_wh,
                    rad_day2=rad_day2_wh,
                    heute=now_local.date(),
                    aktueller_zaehler=getattr(state, 'sommer_modus_zaehler', 0),
                    ist_aktiv=getattr(state, 'sommer_modus_aktiv', False),
                    letzter_bewertungstag=getattr(state, 'sommer_letzter_bewertungstag', None),
                )
                state.sommer_modus_zaehler = neuer_zaehler
                state.sommer_modus_aktiv = ist_aktiv
                state.sommer_letzter_bewertungstag = bewertungstag

                if ereignis == SOMMER_AKTIVIERT:
                    logging.info(
                        f"Sommer-Modus AKTIV nach {neuer_zaehler} guten Prognosetag(en): "
                        f"Solltemperatur wird um {abs(sommer_cfg.temperatur_offset_c):.1f}C gesenkt "
                        f"(Offset {sommer_cfg.temperatur_offset_c:+.1f}C, "
                        f"Schwelle >={sommer_cfg.mindest_prognose_wh:.0f} Wh/qm)"
                    )
                elif ereignis == SOMMER_DEAKTIVIERT_PROGNOSE:
                    logging.info("Sommer-Modus INAKTIV: PV-Prognose nicht mehr durchgehend gut")
                elif ereignis == SOMMER_DEAKTIVIERT_DATEN:
                    logging.info("Sommer-Modus INAKTIV: Keine vollstaendigen Prognosedaten verfuegbar")

            # --- Legionellenprophylaxe Planung (nach Prognose-Update) ---
            legionellen_cfg = state.priority_config.legionellen
            if legionellen_cfg.aktiv:
                aktuelle_kw = now_local.isocalendar()[1]
                letzte_kw = None
                if state.legionellen_last_done is not None:
                    letzte_kw = state.legionellen_last_done.isocalendar()[1]

                # Nur planen, wenn nicht bereits in dieser KW erledigt
                if any(prognose is None for prognose in (rad_today_wh, rad_tomorrow_wh, rad_day2_wh)):
                    clear_plan(state, "Unvollständige Tagesprognose", persist=True)
                elif letzte_kw != aktuelle_kw or state.legionellen_last_done is None:
                    aktueller_wochentag = now_local.weekday()

                    # Nur die verbleibenden erlaubten Kalendertage berücksichtigen.
                    # Ein bereits verstrichener Plan darf nicht wieder auf einen
                    # alten Wochentag zurückfallen.
                    verfuegbare_tage = [
                        tag
                        for tag in range(legionellen_cfg.bevorzugter_tag, legionellen_cfg.letzter_tag + 1)
                        if tag >= aktueller_wochentag
                    ]

                    if not verfuegbare_tage:
                        clear_plan(state, "Kein weiterer Legionellen-Tag in dieser Woche", persist=True)
                    else:
                        tages_prognose = {
                            aktueller_wochentag: rad_today_wh,
                            (aktueller_wochentag + 1) % 7: rad_tomorrow_wh,
                            (aktueller_wochentag + 2) % 7: rad_day2_wh,
                        }
                        # Planbare Tage benötigen die allgemeine Mindestprognose.
                        # Der bevorzugte Tag benötigt zusätzlich seine eigene
                        # Anforderung; ein guter PV-Tag wird für die Auswahl
                        # und den sichtbaren Planstatus berücksichtigt.
                        tages_prognose = {
                            tag: prognose for tag, prognose in tages_prognose.items()
                            if prognose is not None
                            and tag in verfuegbare_tage
                            and prognose >= legionellen_cfg.mindest_prognose_wh_qm
                            and (
                                tag != legionellen_cfg.bevorzugter_tag
                                or prognose >= legionellen_cfg.erforderliche_wh_qm
                            )
                        }
                        if not tages_prognose:
                            clear_plan(
                                state,
                                f"Kein belastbarer PV-Tag (mindestens {legionellen_cfg.mindest_prognose_wh_qm:.0f} Wh/m²)",
                                persist=True,
                            )
                        else:
                            bester_tag = max(
                                tages_prognose,
                                key=lambda tag: (
                                    tages_prognose[tag],
                                    tages_prognose[tag] >= legionellen_cfg.pv_prognose_schwelle_gut,
                                    tag == legionellen_cfg.bevorzugter_tag,
                                ),
                            )
                            # Nur bei deutlich besserem Alternativtag wechseln.
                            preferred = tages_prognose.get(legionellen_cfg.bevorzugter_tag)
                            if preferred is not None and (
                                tages_prognose[bester_tag] - preferred
                                < legionellen_cfg.tagwechsel_ab_diff_wh_qm
                            ):
                                bester_tag = legionellen_cfg.bevorzugter_tag
                            from priority_control import _wochentag_name
                            delta = (bester_tag - aktueller_wochentag) % 7
                            state.legionellen_planned_day = _wochentag_name(bester_tag)
                            state.legionellen_planned_tag = bester_tag
                            state.legionellen_planned_time = f"{legionellen_cfg.start_uhr:02d}:00"
                            state.legionellen_planned_date = now_local.date() + timedelta(days=delta)
                            state.legionellen_planned_forecast_wh = float(tages_prognose[bester_tag])
                            state.legionellen_plan_revision += 1
                            state.legionellen_plan_created_at = now_local
                            gueter_tag = tages_prognose[bester_tag] >= legionellen_cfg.pv_prognose_schwelle_gut
                            state.legionellen_planned_reason = (
                                f"Bester PV-Tag: {tages_prognose[bester_tag]:.0f} Wh/m² "
                                f"(>= {legionellen_cfg.mindest_prognose_wh_qm:.0f} Wh/m², "
                                + (
                                    "guter PV-Tag"
                                    if gueter_tag
                                    else f"unter Gut-Schwelle {legionellen_cfg.pv_prognose_schwelle_gut:.0f} Wh/m²"
                                )
                                + ")"
                            )
                            save_plan(state)

    return last_vpn_check

async def check_and_send_alerts(session, state):
    """PrÃ¼ft auf Ã„nderungen im blocking_reason und sendet sofortige Telegram-Alarme (einmalig)."""
    current_blocking = state.control.blocking_reason
    
    # Normalisierung: Dynamische Teile (Zeiten, Temperaturen) entfernen
    # Beispiel: "Min. Pause (noch 1m 10s)" -> "Min. Pause"
    # Beispiel: "Verdampfer zu kalt (5.0Â°C < 6Â°C)" -> "Verdampfer zu kalt"
    # Beispiel: "Sensorfehler: T_Oben invalid" -> "Sensorfehler"
    import re
    def normalize(text):
        if not text:
            return ""
        # 1. Alles in Klammern entfernen (Zeiten, Werte)
        res = re.sub(r'\(.*?\)', '', text)
        # 2. Alles nach Doppelpunkt entfernen (Details)
        res = res.split(':')[0]
        return res.strip()

    current_type = normalize(current_blocking)
    last_type = getattr(state.control, 'last_alert_type', "")
    
    if current_type != last_type:
        if current_type:
            # Filtere bekannte Infos, die keine Alarme sein sollen
            is_solar = "Solarfenster" in current_type
            is_zieltemp = "Zieltemp" in current_type
            
            if not is_solar and not is_zieltemp:
                # Boiler-Max-Naehe: pro Tag nur 1x senden (sonst Telegram-Spam
                # bei vollem Boiler an sonnigen Tagen - Empfehlung 3.1).
                sende_erlaubt = True
                if "Boiler-Max-Naehe" in current_type:
                    sende_erlaubt = check_log_throttle(
                        state, "log_boiler_naehe_alert", interval_minutes=24 * 60)
                if sende_erlaubt:
                    emoji = "⚠"
                    if any(x in current_type for x in ["Fehler", "Sicherheit", "🚨"]):
                        emoji = "🚨"
                    elif any(x in current_type for x in ["Pause", "Mindestlaufzeit"]):
                        emoji = "⏳"
                    
                    # Wir schicken die VOLLE Nachricht (inkl. Details/Zeit) beim ersten Mal
                    msg = f"{emoji} *Kompressor blockiert:* {escape_markdown(current_blocking)}"
                    logging.info(f"Sende Einmal-Alarm: {current_type} (Voll: {current_blocking})")
                    await control_logic.send_telegram_message(
                        session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN, parse_mode="Markdown"
                    )
        
        state.control.last_alert_type = current_type
    
    # Der technische Statuswechsel wird weiterhin fÃ¼r andere Zwecke geloggt/gespeichert
    state.control.last_blocking_reason = current_blocking

async def _aktualisiere_legionellen_lifecycle(session, state, result):
    """Startet, ueberwacht und beendet die Legionellenfahrt nach dem Hardware-Start."""
    cfg = state.priority_config.legionellen
    if not cfg.aktiv:
        return
    winner = result.get("gewinner_ergebnis")
    if not state.legionellen_aktiv and state.legionellen_temp_override is not None:
        state.legionellen_temp_override = None
    if winner is None or winner.name != "Legionellen":
        return

    now = _state_now(state)
    if (
        winner.einschalten is True
        and not state.legionellen_aktiv
        and state.control.kompressor_ein
        and getattr(state.control, "_lauf_start_regel", None) == "Legionellen"
    ):
        state.legionellen_aktiv = True
        state.legionellen_started_at = now
        state.legionellen_target_reached_at = None
        state.legionellen_telegram_start_sent = False
        state.legionellen_telegram_done_sent = False
        state.legionellen_temp_override = cfg.legionellen_max_temp_c
        state.control.requested_rule_name = "Legionellen"
        state.control.effective_rule_name = "Legionellen"
        state.control.effective_source = pcl._aktuelle_quellenbezeichnung(state)
        logging.info(
            "Legionellenprophylaxe GESTARTET: Heize auf %.0fC (max %.0fC)",
            cfg.target_temp_c, cfg.legionellen_max_temp_c,
        )
        if not state.legionellen_telegram_start_sent:
            try:
                msg = (f"🦠 *Legionellenprophylaxe gestartet!*\n"
                       f"Heize auf {cfg.target_temp_c:.0f}°C "
                       f"(unten: {state.sensors.t_unten:.1f}°C)")
                from telegram_api import send_telegram_message as _send_tg
                await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                               state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
                state.legionellen_telegram_start_sent = True
            except Exception as exc:
                logging.warning(f"Legionellen-Telegram-Start fehlgeschlagen: {exc}")
        return

    if not state.legionellen_aktiv:
        return

    started = state.legionellen_started_at
    if started is not None:
        try:
            elapsed = safe_timedelta(now, started, state.local_tz)
        except (TypeError, ValueError):
            elapsed = timedelta(0)
        if elapsed >= timedelta(hours=cfg.max_duration_hours):
            state.legionellen_aktiv = False
            state.legionellen_temp_override = None
            state.legionellen_started_at = None
            state.legionellen_target_reached_at = None
            clear_plan(state, "Legionellenlauf Timeout", persist=True)
            logging.warning(
                "Legionellenprophylaxe ABGEBROCHEN (Timeout: %.1fh >= %.1fh)",
                elapsed.total_seconds() / 3600.0, cfg.max_duration_hours,
            )
            return

    t_unten = state.sensors.t_unten
    if (
        isinstance(t_unten, (int, float))
        and t_unten >= cfg.target_temp_c
        and state.legionellen_target_reached_at is None
    ):
        state.legionellen_target_reached_at = now
        logging.info(
            "Legionellen: Zieltemperatur erreicht, Probezeit startet (%d min)",
            cfg.probezeit_minuten,
        )

    if winner.einschalten is not False or state.legionellen_target_reached_at is None:
        return
    try:
        probe_done = safe_timedelta(
            now, state.legionellen_target_reached_at, state.local_tz
        ) >= timedelta(minutes=cfg.probezeit_minuten)
    except (TypeError, ValueError):
        probe_done = False
    if not probe_done:
        return

    state.legionellen_last_done = now.date()
    state.legionellen_wochennummer = now.isocalendar()[1]
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.legionellen_started_at = None
    state.legionellen_target_reached_at = None
    state.legionellen_end_time = now
    clear_plan(state, "Legionellenlauf abgeschlossen", persist=True)
    state._last_was_legionellen = True
    state.control.requested_rule_name = "Legionellen"
    state.control.effective_rule_name = "Legionellen"
    state.control.effective_source = pcl._aktuelle_quellenbezeichnung(state)
    logging.info(
        "Legionellenprophylaxe ABGESCHLOSSEN: KW %d, Ziel %.0fC inklusive %d min Probezeit",
        now.isocalendar()[1], cfg.target_temp_c, cfg.probezeit_minuten,
    )
    if not state.legionellen_telegram_done_sent:
        try:
            msg = (f"✅ *Legionellenprophylaxe abgeschlossen!*\n"
                   f"KW {now.isocalendar()[1]}: {cfg.target_temp_c:.0f}°C und Probezeit erreicht")
            from telegram_api import send_telegram_message as _send_tg
            await _send_tg(session, state.config.Telegram.CHAT_ID, msg,
                           state.config.Telegram.BOT_TOKEN, parse_mode="Markdown")
            state.legionellen_telegram_done_sent = True
        except Exception as exc:
            logging.warning(f"Legionellen-Telegram-Done fehlgeschlagen: {exc}")


async def run_logic_step(session, state, learning_engine=None):
    """Fuehrt einen Schritt der Steuerungslogik aus (Pareto-Prioritaeten)."""
    # Manuelle API-Befehle werden nur hier im Main-Loop verarbeitet.
    # Die Reihenfolge innerhalb eines Queue-Batches ist relevant: Ein späteres
    # force_on nach force_off darf nicht durch das frühere force_off verworfen
    # werden.
    manual_force_on = getattr(state.control, "manual_force_on_pending", False) is True
    force_off_requested = False
    for command, params in _pop_control_commands():
        if command == "force_off":
            force_off_requested = True
            manual_force_on = False
            state.control.manual_force_on_pending = False
            # Auch ein manueller Off-Befehl muss den laufenden
            # Legionellen-Lifecycle sauber beenden.
            off_ok = await set_kompressor_status(state, False, force=True, end_grund="api_manuell")
            if not off_ok:
                state.control.blocking_reason = "Manuelles Ausschalten fehlgeschlagen"
                return
            if getattr(state, "legionellen_aktiv", False):
                state.legionellen_aktiv = False
                state.legionellen_temp_override = None
                state.legionellen_started_at = None
                state.legionellen_target_reached_at = None
                state.legionellen_end_time = _state_now(state)
                state._last_was_legionellen = True
                clear_plan(state, "Legionellenlauf manuell beendet", persist=True)
        elif command == "set_mode":
            mode = params.get("mode")
            active = bool(params.get("active"))
            if mode == "bademodus":
                state.bademodus_aktiv = active
            elif mode == "urlaubsmodus":
                state.urlaubsmodus_aktiv = active
        elif command == "force_on":
            manual_force_on = True
            state.control.manual_force_on_pending = True

    # Nur ein reiner Off-Batch beendet diesen Durchlauf. Folgt danach ein
    # force_on im selben Batch, wird der On-Wunsch noch in diesem Zyklus
    # nach den Sicherheitsprüfungen ausgeführt.
    if force_off_requested and not manual_force_on:
        return

    # 1. Druckschalter & Config
    if not await pcl.check_pressure_and_config(
            session, state, handle_pressure_check, set_kompressor_status
        ):
        return  # Druckfehler: Restliche Logik ueberspringen

    # 2. Kompressor-Verifizierung
    if state.control.kompressor_ein:
        is_running, error_msg = await control_logic.verify_compressor_running(state, session, state.sensors.t_verd, state.sensors.t_unten)
        if not is_running and state.kompressor_verification_error_count >= COMPRESSOR_VERIFICATION_ERROR_THRESHOLD:
            logging.error(f"Kompressor-Verifizierung fehlgeschlagen (2x): {error_msg} - Schalte aus!")
            await set_kompressor_status(
                state, False, force=True, end_grund="kompressor_verifizierung"
            )
            state.control.ausschluss_grund = "Kompressor laeuft nicht (Verifizierung fehlgeschlagen)"
            # Explizite Neustartsperre statt Zukunftszeitstempel in last_compressor_off_time
            pcl.setze_neustartsperre(state, minuten=10)

    # 3. Sensoren & Safety (Sicherheits-Check)
    # Hinweis: pcl.check_safety_limits delegiert intern bereits an
    # safety_logic.check_sensors_and_safety â€“ ein zweiter Aufruf waere redundant.
    if await pcl.check_safety_limits(session, state, state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd, set_kompressor_status):
        if manual_force_on:
            state.control.requested_rule_name = "API force_on"
            state.control.effective_rule_name = "API force_on"
            state.control.effective_source = "Manuell"
            state.control._lauf_start_regel = "API force_on"
            ok = await set_kompressor_status(state, True, force=True, end_grund="api_manuell")
            if not ok:
                state.control.blocking_reason = "Manuelles Einschalten blockiert: Sicherheitsprüfung/GPIO"
            else:
                state.control.manual_force_on_pending = False
            return bool(ok)
        # 4. Prioritaeten-Engine: Regel bewerten
        result = await pcl.determine_mode_and_setpoints(state, state.sensors.t_unten, state.sensors.t_mittig, learning_engine=learning_engine)
        
        # 5. Schaltentscheidung
        should_on = result.get("soll_einschalten", False)
        state.control._soll_einschalten = should_on

        # Gewinner-Regel fuer eindeutige AUS-Logs durchreichen
        # (unterscheidet "Regel X sagt AUS" von "keine Regel aktiv")
        gewinner = result.get("gewinner_ergebnis")
        gewinner_name = gewinner.name if gewinner is not None else None

        regelfuehler = result["regelfuehler"]
        ausschaltpunkt = state.control.aktueller_ausschaltpunkt
        einschaltpunkt = state.control.aktueller_einschaltpunkt

        if state.control.kompressor_ein:
            # Kompressor laeuft: Ausschalten pruefen
            await pcl.handle_compressor_off(
                state, session, regelfuehler, ausschaltpunkt,
                state.min_laufzeit, state.sensors.t_oben, set_kompressor_status,
                regel_name=gewinner_name
            )
        else:
            # Kompressor aus: Einschalten pruefen
            await pcl.handle_compressor_on(
                state, session, regelfuehler, einschaltpunkt, ausschaltpunkt,
                state.min_laufzeit, state.min_pause, state.sensors.t_oben,
                state.sensors.t_mittig,
                set_kompressor_status
            )
        
        # 6. Sofort-Alarme pruefen
        await check_and_send_alerts(session, state)

        # 7. Legionellen-Lifecycle nach dem tatsächlichen Hardware-Start
        await _aktualisiere_legionellen_lifecycle(session, state, result)
        if not state.legionellen_aktiv and state.legionellen_temp_override is not None:
            state.legionellen_temp_override = None


def build_heizungsdaten_zeile(state):
    """Baut die CSV-Datenzeile fuer heizungsdaten.csv (20 Spalten).

    Muss mit utils.EXPECTED_CSV_HEADER uebereinstimmen -- abgesichert
    durch tests/test_csv_konsistenz.py."""
    def fmt_csv(val):
        return str(val) if val is not None else "N/A"

    solax = getattr(state.solar, "last_api_data", None) or {}

    # Power Source aus der zentralen, validierten Klassifikation übernehmen.
    # Keine erneute lokale Heuristik aus einzelnen Rohwerten.
    power_source = getattr(state.solar, "energy_source", None) or getattr(
        state, "energy_source", None
    )
    if not power_source:
        source_status = classify_energy_source(
            pv_acpower=getattr(state.solar, "acpower", None),
            feedin_watt=getattr(state.solar, "feedinpower", None),
            battery_discharge_watt=(
                getattr(state.solar, "battery_discharge_watt", None)
                if getattr(state.solar, "battery_discharge_watt", None) is not None
                else batterie_entladung_watt(getattr(state.solar, "batpower", None))
            ),
            soc=getattr(state.solar, "soc", None),
            solar_stale=bool(getattr(state, "solar_stale", False)),
        )
        power_source = source_status.quelle.value
        # Rückwärtskompatibilität für historische/teilweise befüllte States:
        # Die Produktionsklassifikation verlangt für Batterie zusätzlich SOC.
        # Im Diagnose-CSV darf ein eindeutig positives BatPower-Signal bei
        # fehlendem SOC dennoch als historische Batteriequelle markiert werden.
        if (
            power_source == "Netz"
            and not bool(getattr(state, "solar_stale", False))
            and batterie_entladung_watt(getattr(state.solar, "batpower", None)) is not None
            and float(getattr(state.solar, "batpower", 0.0)) >= 50.0
            and getattr(state.solar, "feedinpower", None) is not None
            and float(state.solar.feedinpower) >= -50.0
        ):
            power_source = "Batterie"

    if power_source == "Daten stale":
        power_source = "Netz"
    elif power_source == "PV":
        # Historischer CSV-Vertrag: "Solar" statt der internen Bezeichnung "PV".
        power_source = "Solar"

    return [
        _state_now(state).strftime("%Y-%m-%d %H:%M:%S"),
        fmt_csv(state.sensors.t_oben), fmt_csv(state.sensors.t_unten), fmt_csv(state.sensors.t_mittig),
        fmt_csv(state.sensors.t_boiler), fmt_csv(state.sensors.t_verd),
        "1" if state.control.kompressor_ein else "0",
        fmt_csv(getattr(state.solar, "acpower", None)),
        fmt_csv(getattr(state.solar, "feedinpower", None)),
        fmt_csv(state.solar.batpower), fmt_csv(state.solar.soc),
        fmt_csv(solax.get("powerdc1", 0)), fmt_csv(solax.get("powerdc2", 0)),
        fmt_csv(solax.get("consumeenergy", 0)),
        fmt_csv(state.control.aktueller_einschaltpunkt), fmt_csv(state.control.aktueller_ausschaltpunkt),
        "1" if state.control.solar_ueberschuss_aktiv else "0",
        "1" if getattr(state, "urlaubsmodus_aktiv", False) else "0",
        power_source, fmt_csv(state.solar.forecast_tomorrow)
    ]


# Haltbarer Zustands-Schnappschuss direkt auf der SD (kein tmpfs/log2ram!).
# Ueberlebt einen harten Watchdog-/Strom-Reset und zeigt nach einem Hänger
# den letzten guten Systemzustand samt exakter Loop-Zeit, auch wenn bis zu
# 1 h RAM-Logs (log2ram) verloren gingen.
LAST_STATE_FILE = os.path.join(os.getcwd(), "last_state.txt")


def restore_persisted_compressor_pause(state):
    """Uebernimmt den letzten bekannten AUS-Zeitpunkt aus ``last_state.txt``.

    Der Snapshot wird bei jedem Loop geschrieben. Nach einem systemctl-
    Neustart ist der RAM-State otherwise leer und die Mindestpause beginnt
    erneut bei null. Ein alter Snapshot wird bewusst nur bis zu 24h genutzt;
    danach ist eine Pause ohnehin nicht mehr wirksam.
    """
    try:
        if not os.path.exists(LAST_STATE_FILE):
            return False
        with open(LAST_STATE_FILE, "r", encoding="utf-8") as f:
            line = f.readline().strip()
        if not line:
            return False
        zeit_text, _, rest = line.partition(" | ")
        zeit = datetime.strptime(zeit_text, "%Y-%m-%d %H:%M:%S")
        if state.local_tz is not None:
            zeit = state.local_tz.localize(zeit)
        if zeit.tzinfo is None:
            zeit = zeit.replace(tzinfo=state.local_tz)
        aus_seit_marker = ""
        for feld in rest.split(" | "):
            if feld.startswith("AUS_seit="):
                aus_seit_marker = feld.split("=", 1)[1].strip()
                break
        if "Komp=EIN" in rest:
            # Der Snapshot lief waehrend eines aktiven Laufs. Der sichere
            # Fallback ist der Startzeitpunkt des neuen Prozesses; ein alter
            # AUS_seit-Marker darf hier nicht die Pause wieder veralten lassen.
            zeit = _state_now(state)
        elif aus_seit_marker not in ("", "-"):
            zeit = datetime.strptime(aus_seit_marker, "%Y-%m-%d %H:%M:%S")
            if state.local_tz is not None:
                zeit = state.local_tz.localize(zeit)
        now = _state_now(state)
        if (now - zeit).total_seconds() > 24 * 3600:
            return False
        state.stats.last_compressor_off_time = zeit
        logging.info(
            "Letzte AUS-Zeit aus last_state.txt uebernommen: %s (%s)",
            zeit.isoformat(timespec="seconds"),
            "Snapshot AUS" if "Komp=AUS" in rest else "Snapshot EIN, Neustart-Fallback",
        )
        return True
    except (OSError, ValueError, TypeError) as exc:
        logging.debug("Letzte Kompressorpause konnte nicht gelesen werden: %s", exc)
        return False


def _fmt_temp(v):
    return f"{v:.1f}" if isinstance(v, (int, float)) else "-"


def write_last_state_snapshot(state):
    """Schreibt one Zeile (Overwrite) mit dem aktuellen Systemzustand."""
    try:
        now = _state_now(state)
        komp = "EIN" if state.control.kompressor_ein else "AUS"
        rule = getattr(state.control, "active_rule_name", "") or "-"
        blocking = getattr(state.control, "blocking_reason", "") or "-"
        line = (
            f"{now:%Y-%m-%d %H:%M:%S} | Komp={komp} | Regel={rule} | "
            f"Blocking={blocking} | "
            f"AUS_seit={getattr(state.stats, 'last_compressor_off_time', None).strftime('%Y-%m-%d %H:%M:%S') if getattr(state.stats, 'last_compressor_off_time', None) is not None else '-'} | "
            f"T_oben={_fmt_temp(getattr(state.sensors, 't_oben', None))} | "
            f"T_mittig={_fmt_temp(getattr(state.sensors, 't_mittig', None))} | "
            f"T_unten={_fmt_temp(getattr(state.sensors, 't_unten', None))} | "
            f"T_verd={_fmt_temp(getattr(state.sensors, 't_verd', None))} | "
            f"PV={state.solar.acpower or 0.0:.0f}W | "
            f"SOC={state.solar.soc or 0.0:.0f}%\n"
        )
        atomic_write_text(LAST_STATE_FILE, line)
    except Exception:
        # Diagnose-Datei ist optional - niemals den Haupt-Loop belasten.
        pass


async def log_system_state(state):
    """Schreibt CSV-Log, aktualisiert LCD und loggt Temperaturen + Entscheidungen."""
    # Stunde einmalig pruefen, damit auch ein dauerhaft laufender Pi
    # heizungsdaten.csv zum Monatswechsel atomar archiviert.
    if check_log_throttle(state, "_last_csv_rotation_check", interval_minutes=60.0):
        archiv = rotiere_csv_monatlich(HEIZUNGSDATEN_CSV)
        if archiv:
            logging.info(f"CSV-Monatsrotation im Betrieb: {archiv}")

    # Temperaturmaxima des laufenden Zyklus mit jedem 10-s-Sample fortschreiben.
    update_cycle_maxima(state)

    # 1. Temperatur- und Entscheidungs-Logging (gethrottelt alle 5 Min)
    if check_log_throttle(state, '_last_temp_log', interval_minutes=5.0):
        logging.info(
            f"Sensoren: Oben={state.sensors.t_oben or 0:.1f}°C | Mittig={state.sensors.t_mittig or 0:.1f}°C | "
            f"Unten={state.sensors.t_unten or 0:.1f}°C | Verd={state.sensors.t_verd or 0:.1f}°C"
        )
        komp_status = "EIN" if state.control.kompressor_ein else "AUS"
        # Kontext-Anreicherung (Empfehlung "Log-Analyse"): PV-Leistung,
        # Einspeisung, SOC und Datenalter der letzten Solax-Daten direkt in
        # die Status-Zeile schreiben, damit Entscheidungen aus dem Log heraus
        # ohne Nachrecherche nachvollziehbar sind.
        def _fmt_w(w):
            if w is None:
                return "n/a"
            try:
                return f"{float(w):.0f}W"
            except (TypeError, ValueError):
                return "n/a"

        def _fmt_soc(w):
            if w is None:
                return "n/a"
            try:
                return f"{float(w):.0f}%"
            except (TypeError, ValueError):
                return "n/a"

        solar_now = _state_now(state)
        last_call = getattr(state.solar, 'last_api_call', None)
        alter_s = None
        if last_call is not None:
            try:
                alter_s = round(max((solar_now - last_call).total_seconds(), 0))
            except (TypeError, ValueError):
                alter_s = None
        alter_txt = "n/a" if alter_s is None else f"{alter_s}s"
        # Stale-Daten-Marker (Empfehlung 3.5): Kennzeichnet Entscheidungen, die
        # auf veralteten Solax-Daten beruhen - fuer die spätere Analyse sofort
        # erkennbar, ob eine Ausschaltung einer Stillstand-Phase geschuldet war.
        stale_flag = ""
        if alter_s is not None and alter_s > SOLAR_DATA_STALE_THRESHOLD_MIN * 60:
            stale_flag = " | STALE"

        log_line = (
            f"Status: {komp_status} | "
            f"EP={state.control.aktueller_einschaltpunkt:.1f}°C | "
            f"AP={state.control.aktueller_ausschaltpunkt:.1f}°C | "
            f"PV={_fmt_w(getattr(state.solar, 'acpower', None))} | "
            f"Einspeis={_fmt_w(getattr(state.solar, 'feedinpower', None))} | "
            f"SOC={_fmt_soc(getattr(state.solar, 'soc', None))} | "
            f"Alter={alter_txt}{stale_flag}"
        )
        if state.control.blocking_reason:
            log_line += f" | Blocking: {state.control.blocking_reason}"
        if state.control.active_rule_name:
            log_line += f" | Regel: {state.control.active_rule_name}"
        if state.control.previous_modus:
            log_line += f" | Modus: {state.control.previous_modus}"
        logging.info(log_line)

    # 3. LCD Update
    pv_w = state.solar.feedinpower if state.solar.feedinpower else 0
    rule_name = getattr(state.control, 'active_rule_name', '') or ''
    if rule_name and len(rule_name) > 12:
        rule_name = rule_name[:12]
    hardware_manager.write_lcd(
        f"E:{state.sensors.t_oben if state.sensors.t_oben else 0:.1f} U:{state.sensors.t_unten if state.sensors.t_unten else 0:.1f}",
        f"M:{state.sensors.t_mittig if state.sensors.t_mittig else 0:.1f} V:{state.sensors.t_verd if state.sensors.t_verd else 0:.0f}",
        f"{'ON' if state.control.kompressor_ein else 'OFF'} PV:{pv_w:.0f}W {rule_name[:7]}",
        f"{state.solar.soc if state.solar.soc else 0}% {state.control.previous_modus[:7] if state.control.previous_modus else ''}"
    )

    # 4. CSV Logging
    try:
        csv_file = HEIZUNGSDATEN_CSV
        log_dir = os.path.dirname(csv_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(csv_file):
            async with aiofiles.open(csv_file, mode="w", encoding="utf-8") as f:
                await f.write(",".join(EXPECTED_CSV_HEADER) + "\n")
        # Optimization: Header check removed from loop (done at startup)

        csv_line = build_heizungsdaten_zeile(state)
        async with aiofiles.open(csv_file, mode="a", encoding="utf-8") as f:
            await f.write(",".join(csv_line) + "\n")
    except Exception as e:
        logging.error(f"Fehler beim Schreiben der CSV: {e}")

    # 5. Haltbarer Zustands-Schnappschuss auf der SD (ueberlebt harten Reset)
    write_last_state_snapshot(state)

def _git_revision() -> str:
    """Git-Identitaet des laufenden Standes (fuer das Startup-Log).

    Liefert 'kurzer-Hash [Branch] Betreff' oder 'unbekannt', falls kein
    Git-Repo verfuegbar ist. So ist spaeter jedem Log eindeutig zuzuordnen,
    mit welcher GitHub-Version die Steuerung gelaufen ist.
    """
    try:
        import subprocess as _sp
        _base = os.path.dirname(os.path.abspath(__file__))

        def _git(*args):
            return _sp.run(
                ["git", "-C", _base, *args],
                capture_output=True, text=True, timeout=5,
                check=False,
            ).stdout.strip()

        kurz = _git("rev-parse", "--short", "HEAD")
        branch = _git("branch", "--show-current")
        if not branch:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        subj = _git("log", "-1", "--format=%s")
        if kurz:
            return f"{kurz} [{branch or 'detached'}] {subj[:80]}"
    except Exception:
        pass
    return "unbekannt"


async def _melde_unsauberen_lauf(session, state):
    """Meldet einen nicht sauber beendeten Vorlauf (OOM-Kill, Crash, Reset).

    Ohne diese Meldung sieht ein OOM-Kill im Steuerungs-Log wie ein voellig
    normaler Neustart aus (Incident 15.09.: der Kernel killte den Prozess mit
    SIGKILL, sichtbar nur im Kernel-Journal).
    """
    info = getattr(state, "letzter_lauf", None) or {}
    if not info.get("unsauber"):
        return

    start = info.get("vorheriger_start") or "unbekannt"
    ende = info.get("vorheriges_ende") or "kein sauberes Ende verzeichnet"
    logging.warning(
        f"Letzter Lauf wurde NICHT sauber beendet (Start: {start}, Ende: {ende})."
    )

    oom = info.get("oom_hinweis")
    if oom:
        logging.warning(f"Kernel-Log: {oom} -> Verdacht: OOM-Kill.")
        grund = "OOM-Kill (Speicher)"
    else:
        logging.warning(
            "Kein OOM-Hinweis im Kernel-Log lesbar -> Ursache unklar "
            "(Crash, harter Reset oder Stromausfall)."
        )
        grund = "unklar (Crash/Reset/Strom?)"

    if not (state.bot_token and state.chat_id):
        return
    try:
        speicher = startup_diagnose.formatiere_speicher(startup_diagnose.speicher_werte())
        from telegram_api import send_telegram_message as _send_tg

        await _send_tg(
            session,
            state.chat_id,
            f"⚠️ Steuerung wurde unsauber beendet – Verdacht: {grund}.\n"
            f"Start {start}, Ende {ende}.\nSpeicher jetzt: {speicher}",
            state.bot_token,
        )
    except Exception as e:
        logging.debug(f"Telegram-Warnung (unsauberer Lauf) nicht gesendet: {e}")


def _logge_speicher(state, letzter_log):
    """Loggt stuendlich RSS/verfuegbaren Speicher (OOM-Nachvollziehbarkeit)."""
    jetzt = _state_now(state)
    if letzter_log is not None and (jetzt - letzter_log).total_seconds() < MEMORY_LOG_INTERVAL_SEC:
        return letzter_log
    try:
        werte = startup_diagnose.speicher_werte()
        logging.info(f"Speicher: {startup_diagnose.formatiere_speicher(werte)}")
    except Exception:
        pass
    return jetzt


def _markiere_sensor_update_fehler(state) -> None:
    """Bei fehlgeschlagenem Datenupdate fail-safe ungültige Sensorwerte markieren."""
    for name in ("t_oben", "t_unten", "t_mittig", "t_verd", "t_boiler"):
        try:
            setattr(state.sensors, name, None)
        except Exception:
            pass
    try:
        state.last_data_update_ok = False
    except Exception:
        pass


async def _run_data_phase(session, state) -> bool:
    """Sensor-/Solar-Update isoliert ausführen; niemals Control überspringen."""
    try:
        await update_system_data(session, state, refresh_solar=False)
        state.last_data_update_ok = True
        state.last_sensor_success = _state_now(state)
        return True
    except Exception:
        logging.exception("Fehler in der Daten-Update-Phase")
        _markiere_sensor_update_fehler(state)
        return False


async def _run_periodic_phase(session, state, last_vpn_check):
    """VPN/Forecast und weitere optionale Aufgaben dürfen Control nicht blockieren."""
    try:
        return await check_periodic_tasks(session, state, last_vpn_check)
    except Exception:
        logging.exception("Fehler in der periodischen Aufgabenphase")
        return last_vpn_check


async def _run_api_health_phase(session, state) -> None:
    """API-Health ist Diagnose und niemals Teil des sicherheitskritischen Pfads."""
    try:
        now_local = _state_now(state)
        letzte_warnung = getattr(state, "_last_api_health_warning", None)
        faellig = (
            letzte_warnung is None
            or (now_local - letzte_warnung).total_seconds() >= 600
        )
    except Exception:
        faellig = True
    if not faellig:
        return
    try:
        await check_api_health(session, state)
    except Exception:
        logging.exception("Fehler im API-Health-Check")
    try:
        state._last_api_health_warning = _state_now(state)
    except Exception:
        logging.debug("API-Health-Zeitstempel konnte nicht gespeichert werden", exc_info=True)


async def _run_control_phase(session, state, data_update_ok: bool) -> None:
    """Regelung ausführen und bei unerwarteten Regelfehlern fail-safe ausschalten."""
    if not data_update_ok:
        state.control.blocking_reason = "Sensor-Update fehlgeschlagen"
        if getattr(state.control, "kompressor_ein", False):
            try:
                await set_kompressor_status(
                    state, False, force=True, end_grund="sensor_update_fehler"
                )
            except Exception:
                logging.exception("Fail-safe-Ausschalten nach Sensor-Update-Fehler fehlgeschlagen")
        return
    try:
        await run_logic_step(session, state, learning_engine=state.learning_engine)
        state.control.consecutive_control_errors = 0
        state.last_control_success = _state_now(state)
    except Exception:
        state.control.consecutive_control_errors = (
            getattr(state.control, "consecutive_control_errors", 0) + 1
        )
        state.control.manual_force_on_pending = False
        state.control.blocking_reason = "Regelungsfehler"
        logging.exception(
            "Fehler in der sicherheitskritischen Regelphase (%d aufeinanderfolgende Fehler)",
            state.control.consecutive_control_errors,
        )
        if getattr(state.control, "kompressor_ein", False):
            try:
                await set_kompressor_status(
                    state, False, force=True, end_grund="regelungsfehler"
                )
            except Exception:
                logging.exception("Fail-safe-Ausschalten nach Regelungsfehler fehlgeschlagen")


async def _run_logging_phase(state) -> None:
    """Diagnose-/CSV-Logging isoliert; ein Fehler darf die Regelung nie stoppen."""
    try:
        await log_system_state(state)
    except Exception:
        logging.exception("Fehler in der Logging-/CSV-Phase")


async def _run_status_snapshot_phase(state) -> None:
    """API liest nur aus einem konsistenten Snapshot des aktuellen Loops."""
    try:
        state.last_status_snapshot_at = _state_now(state)
        update_status_snapshot(state)
    except Exception:
        logging.exception("Status-Snapshot konnte nicht aktualisiert werden")


async def main_loop():
    session = None
    try:
        session = await setup_application()

        # Versionsmarker: Welche Repo-Version dieses Log erzeugt hat (GitHub-Rev).
        # Damit laesst sich bei spaeteren Analysen der genaue Steuerungsstand
        # jedes Logs rekonstruieren.
        logging.info(f"Start WPSteuerung | GitHub-Rev: {_git_revision()}")

        # Send Startup Message
        if state.bot_token and state.chat_id:
            try:
                await send_welcome_message(session, state.chat_id, state.bot_token, state)
                logging.info("Startup message sent.")
            except Exception:
                logging.exception("Failed to send startup message")

        # Diagnose: unsauber beendeten Vorlauf melden (OOM-Kill/Crash/Reset).
        await _melde_unsauberen_lauf(session, state)
        letzter_speicher_log = _logge_speicher(state, None)
        last_vpn_check = _state_now(state) - timedelta(minutes=1)

        while not stop_event.is_set():
            iteration_started = _state_monotonic(state)
            try:
                now = _state_now(state)
                state.loop_heartbeat = now
                handle_day_transition(state, now)

                # Urlaubsmodus: automatisches Beenden nach Ablauf von urlaubsmodus_ende
                urlaub_ende = getattr(state, "urlaubsmodus_ende", None)
                if (
                    getattr(state, "urlaubsmodus_aktiv", False) is True
                    and isinstance(urlaub_ende, (datetime, date))
                    and now >= urlaub_ende
                ):
                    state.urlaubsmodus_aktiv = False
                    state.urlaubsmodus_ende = None
                    state.urlaubsmodus_start = None
                    logging.info("Urlaubsmodus automatisch beendet (Endzeitpunkt erreicht).")
                if state.control.kompressor_ein and state.stats.last_compressor_on_time:
                    state.stats.current_runtime = safe_timedelta(
                        now, state.stats.last_compressor_on_time, state.local_tz
                    )
                else:
                    state.stats.current_runtime = timedelta()
            except Exception:
                # Tageswechsel/Statistik ist nicht sicherheitskritisch.
                logging.exception("Fehler in der Tageswechsel-/Laufzeitphase")

            data_update_ok = await _run_data_phase(session, state)
            last_vpn_check = await _run_periodic_phase(session, state, last_vpn_check)
            try:
                letzter_speicher_log = _logge_speicher(state, letzter_speicher_log)
            except Exception:
                logging.exception("Fehler in der Speicher-Diagnosephase")
            await _run_api_health_phase(session, state)
            await _run_control_phase(session, state, data_update_ok)
            await _run_logging_phase(state)
            await _run_status_snapshot_phase(state)

            # In kurzen Abschnitten schlafen, damit ein Stop-Signal zuegig reagiert.
            elapsed = _state_monotonic(state) - iteration_started
            remaining = max(0.0, MAIN_LOOP_INTERVAL_SEC - elapsed)
            while remaining > 0 and not stop_event.is_set():
                step = min(remaining, 0.5)
                await asyncio.sleep(step)
                remaining -= step

    except asyncio.CancelledError:
        pass
    except Exception:
        logging.critical("Unbehandelter Fehler in Main Loop", exc_info=True)
    finally:
        logging.info("Shutting down...")
        # Sauberes Ende markieren, damit der naechste Start einen kontrollierten
        # Stopp von einem OOM-Kill/Crash unterscheiden kann.
        try:
            startup_diagnose.markiere_sauberes_ende()
        except Exception:
            logging.debug("Laufstatus konnte beim Shutdown nicht geschrieben werden", exc_info=True)
        # Hintergrund-Tasks kontrolliert beenden, BEVOR die Session geschlossen wird.
        tasks = list(background_tasks)
        background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Bei einem laufenden Prozess muss der GPIO-AUS auch als echter
        # Abschluss mit technischem Grund verbucht werden. Sonst erzeugt der
        # naechste Start einen scheinbar neuen Zyklus und verliert die Pause.
        if state is not None and getattr(state.control, "kompressor_ein", False) is True:
            try:
                await set_kompressor_status(
                    state, False, force=True, end_grund="dienst_neustart"
                )
            except Exception:
                logging.exception("Kompressor konnte beim Shutdown nicht sauber beendet werden")
        if state is not None:
            try:
                write_last_state_snapshot(state)
            except Exception:
                logging.debug("Letzter Zustands-Snapshot beim Shutdown fehlgeschlagen", exc_info=True)
        if hardware_manager:
            try:
                hardware_manager.cleanup()
            except Exception:
                logging.exception("GPIO-Cleanup beim Shutdown fehlgeschlagen")
        if session is not None:
            try:
                await session.close()
            except Exception:
                logging.exception("HTTP-Session beim Shutdown konnte nicht geschlossen werden")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        pass
