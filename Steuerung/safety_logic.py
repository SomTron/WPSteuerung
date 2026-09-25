import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Callable
from telegram_api import send_telegram_message
from logic_utils import is_valid_temperature, check_log_throttle
from utils import safe_timedelta
from clock import now_for
import alert_throttle
from constants import (
    TEMP_VERD_MIN_VALID, TEMP_VERD_MAX_VALID,
    COMPRESSOR_VERIFICATION_DELAY_MIN, COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN,
    COMPRESSOR_VERD_DELTA_MIN, COMPRESSOR_VERD_START_TEMP_COLD,
    COMPRESSOR_VERD_DELTA_COLD_MIN, COMPRESSOR_VERD_COLD_MAX,
    COMPRESSOR_UNTEN_DELTA_MIN,
    LEGIONELLEN_VERIFY_NUR_VERDAMPFER,
)

_REAL_DATETIME = datetime


def _now_for_state(state):
    """State-Zeit mit Rückwärtskompatibilität für Test-/Alt-Time-Patches."""
    if datetime is not _REAL_DATETIME:
        return datetime.now(getattr(state, "local_tz", None))
    return now_for(state)

async def handle_critical_compressor_error(session, state, error_context: str):
    """Behandelt kritische Fehler beim Kompressor-Ausschalten.

    Sicherheitsrelevant und deshalb *nicht* stumm: der Kompressor laeuft
    weiter. Trotzdem kann dieser Pfad im 10-Sekunden-Takt erneut
    durchlaufen, wenn das Ausschalten dauerhaft fehlschlaegt (z.B. GPIO- oder
    Relaisfehler). Ohne Drosselung waeren das bis zu sechs Nachrichten pro
    Minute. Deshalb: erste Meldung sofort, danach gestaffelt wiederholen.
    """
    msg = f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!"
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    # Getrennte Drosselung je Fehlerart: Ein Ueberhitzungsfehler und ein
    # Boiler-Maximum sollen nicht gegenseitig die Meldung unterdruecken.
    key = f"tg_kritisch_{_kritisch_key(error_context)}"
    if not alert_throttle.soll_senden(
        state, key, _now_for_state(state), getattr(state, "local_tz", None)
    ):
        return
    # Kritische Alarme awaiten statt fire-and-forget: Task-Referenz wuerde sonst
    # vom GC eingesammelt, bevor die Nachricht zugestellt ist (stiller Fehler).
    await send_telegram_message(
        session, state.config.Telegram.CHAT_ID, msg, state.config.Telegram.BOT_TOKEN)


def _kritisch_key(error_context: str) -> str:
    """Stabile Sperrfamilie fuer einen kritischen Abschaltfehler."""
    text = (error_context or "").strip().lower()
    if "ueberhitzung" in text or "überhitzung" in text:
        return "ueberhitzung"
    if "boiler" in text or "max" in text:
        return "boiler_max"
    return "allgemein"


async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten, t_mittig):
    """Prueft alle für die Regelung benötigten Boiler-Sensoren."""
    errors = []
    if not is_valid_temperature(t_boiler_oben):
        errors.append(f"T_Oben invalid: {t_boiler_oben}")
    if not is_valid_temperature(t_boiler_unten):
        errors.append(f"T_Unten invalid: {t_boiler_unten}")
    if not is_valid_temperature(t_mittig):
        errors.append(f"T_Mittig invalid: {t_mittig}")
    
    if errors:
        error_msg = ", ".join(errors)
        state.control.blocking_reason = f"Sensorfehler: {error_msg}"
        state.last_sensor_error_time = _now_for_state(state)
        # Separater Throttle-Marker: last_sensor_error_time fachlich der letzte
        # Fehlerzeitpunkt; sonst wird die erste Meldung durch das sofortige Setzen
        # des Markers unterdrückt.
        if check_log_throttle(state, "_last_sensor_error_log"):
            logging.error(f"Sensorfehler: {error_msg}")
        return False
    state.last_sensor_error_time = None
    state._last_sensor_error_log = None
    return True

async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """Sicherheitsabschaltung und Sensorprüfung."""
    state.sensors.t_oben, state.sensors.t_unten, state.sensors.t_mittig, state.sensors.t_verd = t_oben, t_unten, t_mittig, t_verd
    state.sensors.t_boiler = t_oben if t_oben is not None else ((t_mittig if t_mittig is not None else t_unten))
    
    if not await check_for_sensor_errors(session, state, t_oben, t_unten, t_mittig):
        state.control.ausschluss_grund = "Sensorfehler"
        state.control.blocking_reason = "Sensor-Fehler"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(
                state, False, force=True, end_grund="sensorfehler"
            )
        return False

    safety_temp = None
    if hasattr(state, 'priority_config') and getattr(state.priority_config, 'sicherheit', None):
        # Waehrend einer aktiven Legionellenfahrt wird die Sicherheitsschwelle
        # dynamisch auf legionellen_max_temp_c angehoben, damit die Prophylaxe
        # (Ziel 60/65C) nicht vom Ueberhitzungsschutz abgebrochen wird.
        # Prueft sowohl legionellen_aktiv als auch legionellen_temp_override
        # (robust gegen Mocks/Neustart-Zustaende).
        legionellen_betrieb = (
            getattr(state, 'legionellen_aktiv', False) is True
            or getattr(state, 'legionellen_temp_override', None) is not None
        )
        if legionellen_betrieb:
            lle_val = getattr(
                getattr(state.priority_config, 'legionellen', None),
                'legionellen_max_temp_c', None,
            )
            if isinstance(lle_val, (int, float)) and float(lle_val) > 40.0:
                safety_temp = float(lle_val)
        if safety_temp is None:
            val = getattr(state.priority_config.sicherheit, 'ueberhitzung_c', None)
            # Nur echte numerische Werte (int/float) akzeptieren, keine Mocks/MagicMocks
            if isinstance(val, (int, float)):
                try:
                    candidate = float(val)
                    if candidate > 40.0:  # Plausibilitätscheck: Sicherheitstemp muss > 40°C sein
                        safety_temp = candidate
                except (TypeError, ValueError):
                    pass
            
    if safety_temp is None:
        val = getattr(getattr(state, 'config', None), 'Heizungssteuerung', None)
        if val is not None:
            raw_temp = getattr(val, 'SICHERHEITS_TEMP', 58.0)
            try:
                safety_temp = float(raw_temp) if isinstance(raw_temp, (int, float)) else 58.0
                if not (40.0 < safety_temp < 150.0):
                    safety_temp = 58.0
            except (TypeError, ValueError):
                safety_temp = 58.0
        else:
            safety_temp = 58.0

    if (t_oben is not None and t_oben >= safety_temp) or (t_unten is not None and t_unten >= safety_temp):
        state.control.ausschluss_grund = f"Übertemperatur (>= {safety_temp} Grad)"
        state.control.blocking_reason = f"Sicherheitstemp (>= {safety_temp}°C)"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(
                state, False, force=True, end_grund="uebertemperatur"
            )
        return False

    if not is_valid_temperature(t_verd, min_temp=TEMP_VERD_MIN_VALID, max_temp=TEMP_VERD_MAX_VALID):
        state.control.ausschluss_grund = "Verdampfertemperatur ungültig"
        state.control.blocking_reason = "Verdampfer ungültig"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(
                state, False, force=True, end_grund="verdampfer_ungueltig"
            )
        return False
    
    verd_limit = state.config.Heizungssteuerung.VERDAMPFERTEMPERATUR
    restart_temp = state.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
    
    # Logic for evaporator hysteresis
    already_blocked = getattr(state, 'verdampfer_blocked', False)
    too_cold = t_verd < verd_limit
    recovering = already_blocked and t_verd < restart_temp
    
    if too_cold or recovering:
        state.verdampfer_blocked = True
        # Verdampfer-Abschaltung tracken (nur bei erstem Blockieren pro Zyklus)
        if not already_blocked:
            now = _now_for_state(state)
            state.verdampfer_shutdowns.append(now)
            # Altvte Einträge außerhalb der letzten Stunde bereinigen
            cutoff = now - timedelta(hours=1)
            state.verdampfer_shutdowns = [t for t in state.verdampfer_shutdowns if t >= cutoff]
            # Warnung bei häufigem Vereisen (> 2 Abschaltungen/Stunde)
            if len(state.verdampfer_shutdowns) > 2:
                logging.warning(
                    f"Verdampfer-Vereisung: {len(state.verdampfer_shutdowns)} Abschaltungen in der letzten Stunde. "
                    "Mögliche Ursachen: schlechter Luftstrom, Kältemittelmangel, oder Filter verschmutzt."
                )
        if already_blocked:
            state.control.ausschluss_grund = f"Verdampfer: Warten auf Erwärmung ({t_verd:.1f} Grad < {restart_temp} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C, warte auf >{restart_temp}°C)"
        else:
            state.control.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {verd_limit} Grad)"
            state.control.blocking_reason = f"Verdampfer zu kalt ({t_verd:.1f}°C < {verd_limit}°C)"
        
        if state.control.kompressor_ein:
            await set_kompressor_status_func(
                state, False, force=True, end_grund="verdampfer_zu_kalt"
            )
        return False
    
    state.verdampfer_blocked = False
    return True

async def verify_compressor_running(state, session, current_t_verd, current_t_unten, verification_delay_minutes=COMPRESSOR_VERIFICATION_DELAY_MIN):
    """Verifiziert den Lauf des Kompressors über Temperaturänderungen."""
    now = _now_for_state(state)
    if not state.control.kompressor_ein or state.kompressor_verification_start_time is None:
        state.kompressor_verification_start_time = None
        return True, None

    elapsed = safe_timedelta(now, state.kompressor_verification_start_time, state.local_tz)
    if elapsed < timedelta(minutes=verification_delay_minutes):
        return True, None

    if state.kompressor_verification_last_check:
        if safe_timedelta(now, state.kompressor_verification_last_check, state.local_tz) < timedelta(minutes=COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN):
            return True, None
    state.kompressor_verification_last_check = now

    verd_delta = state.kompressor_verification_start_t_verd - current_t_verd
    unten_delta = abs(current_t_unten - state.kompressor_verification_start_t_unten)

    # Legionellenmodus (Incident 11.09): unten saettigt mit ~0.5 C/h und
    # erfuellt die 0.2-K-Schwelle nie -> nur der Verdampfer zaehlt als
    # Betriebsbeweis, solange state.legionellen_temp_override gesetzt ist.
    # (isinstance-Check: der Override ist eine Zahl; schuetzt vor MagicMock-True)
    legionellen_aktiv = isinstance(
        getattr(state, "legionellen_temp_override", None), (int, float))
    nur_verdampfer = bool(legionellen_aktiv and LEGIONELLEN_VERIFY_NUR_VERDAMPFER)

    verd_ok = verd_delta >= COMPRESSOR_VERD_DELTA_MIN
    if not verd_ok and state.kompressor_verification_start_t_verd < COMPRESSOR_VERD_START_TEMP_COLD:
        if verd_delta >= COMPRESSOR_VERD_DELTA_COLD_MIN and current_t_verd < COMPRESSOR_VERD_COLD_MAX:
            verd_ok = True

    unten_ok = True if nur_verdampfer else unten_delta >= COMPRESSOR_UNTEN_DELTA_MIN

    # Im Normalbetrieb genuegt ein plausibles Aktivitaetssignal. Log 20.09.:
    # Der Verdampfer stieg sensorbedingt um ~2.9 K, waehrend der Boiler
    # zugleich klar um >1 K stieg. Ein UND-Zwang wertete das als Stillstand.
    # Im Legionellenmodus bleibt der Verdampfer alleiniger Beweis, weil der
    # untere Fuehler dort saettigen kann.
    betriebsbeweis = verd_ok if nur_verdampfer else (verd_ok or unten_ok)
    if nur_verdampfer:
        checks = getattr(state.control, "_legionellen_verify_checks", None)
        if not isinstance(checks, deque):
            checks = deque(maxlen=3)
            state.control._legionellen_verify_checks = checks
        checks.append(bool(betriebsbeweis))
        if len(checks) < 3 or sum(checks) >= 2:
            state.kompressor_verification_failed = False
            state.kompressor_verification_error_count = 0
            alert_throttle.reset(state, "tg_verifizierung")
            return True, None
        betriebsbeweis = False
    if betriebsbeweis:
        state.kompressor_verification_failed = False
        state.kompressor_verification_error_count = 0
        # Erholung: Drosselung zuruecksetzen, damit ein spaeter auftretendes,
        # neues Ereignis sofort wieder gemeldet wird.
        alert_throttle.reset(state, "tg_verifizierung")
        return True, None
    
    state.kompressor_verification_failed = True
    state.kompressor_verification_error_count += 1
    
    error_parts = []
    if not verd_ok:
        error_parts.append(f"Verdampfer: nur {verd_delta:.1f}°C Abfall (Soll: >{COMPRESSOR_VERD_DELTA_MIN}°C)")
    if not nur_verdampfer and not unten_ok:
        error_parts.append(f"Unterer Fühler: nur {unten_delta:.1f}°C Änderung (Soll: >{COMPRESSOR_UNTEN_DELTA_MIN}°C)")
    
    error_msg = "⚠️ Wärmepumpe läuft möglicherweise NICHT:\n" + "\n".join(error_parts)
    if state.bot_token:
        # Die Pruefung laeuft einmal pro Minute. Schlaegt anschliessend auch
        # das Ausschalten fehl, laeuft sie hier weiter und wuerde jede Minute
        # eine Nachricht schicken. Deshalb gestaffelt wiederholen; die erste
        # Meldung und jede Fehlerstufe gehen weiterhin raus.
        if alert_throttle.soll_senden(
            state,
            "tg_verifizierung",
            _now_for_state(state),
            getattr(state, "local_tz", None),
            alert_throttle.VERIFIKATION_STEPS_MIN,
        ):
            # Verifizierungsfehler synchron mit begrenzten Telegram-Retries senden;
            # ein fehlgeschlagener Versand darf nicht still verschwinden.
            sent = await send_telegram_message(
                session, state.config.Telegram.CHAT_ID,
                f"{error_msg}\nFehler #{state.kompressor_verification_error_count}",
                state.config.Telegram.BOT_TOKEN,
            )
            if not sent:
                logging.warning("Telegram-Warnung zur Kompressorverifizierung nicht zugestellt")
    return False, error_msg
