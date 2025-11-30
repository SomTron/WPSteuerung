import logging
import math
from datetime import datetime, timedelta
import asyncio
from typing import Optional, Callable

from utils import safe_timedelta
from telegram_handler import is_solar_window, send_telegram_message

def is_valid_temperature(temp: Optional[float], min_temp: float = -50.0, max_temp: float = 150.0) -> bool:
    """
    Prüft, ob ein Temperaturwert gültig ist.
    
    Args:
        temp: Der zu prüfende Temperaturwert
        min_temp: Minimale plausible Temperatur in °C (Standard: -50°C)
        max_temp: Maximale plausible Temperatur in °C (Standard: 150°C)
        
    Returns:
        bool: True wenn die Temperatur gültig ist, sonst False
        
    Prüft auf:
        - None-Werte
        - NaN (Not a Number)
        - Inf (Unendlich)
        - Werte außerhalb des plausiblen Bereichs
    """
    if temp is None:
        return False
    if not isinstance(temp, (int, float)):
        return False
    if math.isnan(temp) or math.isinf(temp):
        return False
    if temp < min_temp or temp > max_temp:
        return False
    return True

def check_log_throttle(state, attribute_name: str, interval_minutes: float = 5.0) -> bool:
    """
    Prüft, ob eine Log-Nachricht gesendet werden soll (Throttling).
    Aktualisiert automatisch den Zeitstempel im State.
    
    Args:
        state: Das State-Objekt
        attribute_name: Name des Attributs für den letzten Zeitstempel (z.B. 'last_sensor_error_time')
        interval_minutes: Mindestabstand in Minuten
        
    Returns:
        bool: True wenn geloggt werden soll, sonst False
    """
    last_time = getattr(state, attribute_name, None)
    now = datetime.now(state.local_tz)
    
    if last_time is None or safe_timedelta(now, last_time, state.local_tz) > timedelta(minutes=interval_minutes):
        setattr(state, attribute_name, now)
        return True
    return False

def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.last_compressor_off_time = time_val

# Helper functions moved from main.py
def is_nighttime(config):
    """Prüft, ob es Nachtzeit ist, mit korrekter Behandlung von Mitternacht."""
    try:
        # Priorisiere NACHTABSENKUNG_START/END, Fallback auf NACHT_START/END
        start_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_START", 
                    config["Heizungssteuerung"].get("NACHT_START", "22:00"))
        end_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", 
                  config["Heizungssteuerung"].get("NACHT_ENDE", "06:00"))
        
        now = datetime.now().time()
        start = datetime.strptime(start_str, "%H:%M").time()
        end = datetime.strptime(end_str, "%H:%M").time()
        
        if start <= end:
            return start <= now <= end
        else:  # Nacht geht über Mitternacht
            return start <= now or now <= end
    except Exception as e:
        logging.error(f"Fehler bei is_nighttime: {e}")
        return False

def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob aktuell der Übergangsmodus (morgens oder abends) aktiv ist, basierend auf Uhrzeit im State."""
    try:
        now_time = datetime.now(state.local_tz).time()
        
        # Morgens: Von Nachtende bis Morgenende
        morgens_aktiv = state.nachtabsenkung_ende <= now_time <= state.uebergangsmodus_morgens_ende
        
        # Abends: Von Abendstart bis Nachtstart
        abends_aktiv = state.uebergangsmodus_abends_start <= now_time <= state.nachtabsenkung_start
        
        return morgens_aktiv or abends_aktiv
    except Exception as e:
        logging.error(f"Fehler bei ist_uebergangsmodus_aktiv: {e}")
        return False

def set_last_compressor_off_time(state, value):
    # Hilfsfunktion zum Setzen von last_compressor_off_time mit Debugging.
    logging.debug(f"Setze last_compressor_off_time auf: {value}")
    state.last_compressor_off_time = value

async def handle_critical_compressor_error(session, state, error_context: str):
    """
    Behandelt kritische Fehler beim Kompressor-Ausschalten.
    
    Args:
        session: Die HTTP-Session für Telegram-Nachrichten
        state: Der aktuelle Systemzustand
        error_context: Beschreibung des Fehlerkontexts (z.B. "trotz Übertemperatur")
    """
    logging.critical(f"Kritischer Fehler: Kompressor konnte {error_context} nicht ausgeschaltet werden!")
    asyncio.create_task(send_telegram_message(
        session, state.chat_id,
        f"🚨 KRITISCHER FEHLER: Kompressor bleibt {error_context} eingeschaltet!",
        state.bot_token,
        parse_mode=None
    ))

def get_validated_reduction(config, section: str, key: str, default: float = 0.0) -> float:
    """
    Validiert und gibt Temperaturreduktionswerte zurück.
    
    Args:
        config: Das Konfigurationsobjekt
        section: Die Konfigurationssektion (z.B. "Heizungssteuerung")
        key: Der Konfigurationsschlüssel (z.B. "NACHTABSENKUNG")
        default: Der Standardwert, falls der Schlüssel nicht existiert oder ungültig ist
        
    Returns:
        float: Der validierte Reduktionswert (0-35°C) oder der Standardwert
    """
    try:
        value = config[section].get(key, str(default))
        reduction = float(value)
        # Validierung: Absenkung sollte zwischen 0 und 2 Grad liegen
        if reduction < 0 or reduction > 35:
            logging.warning(f"{key} ({reduction}) außerhalb des gültigen Bereichs (0-35°C), setze auf {default}")
            return default
        return reduction
    except (ValueError, TypeError) as e:
        logging.error(f"Ungültiger Wert für {key}: {e}, verwende {default}")
        return default

async def check_for_sensor_errors(session, state, t_boiler_oben, t_boiler_unten):
    """Prüft auf Sensorfehler mit robuster Validierung."""
    # Detaillierte Fehlerprüfung für bessere Diagnostik
    errors = []
    if not is_valid_temperature(t_boiler_oben):
        if t_boiler_oben is None:
            errors.append("T_Oben ist None")
        elif math.isnan(t_boiler_oben):
            errors.append("T_Oben ist NaN")
        elif math.isinf(t_boiler_oben):
            errors.append("T_Oben ist Inf")
        else:
            errors.append(f"T_Oben ({t_boiler_oben}°C) außerhalb des gültigen Bereichs")
    
    if not is_valid_temperature(t_boiler_unten):
        if t_boiler_unten is None:
            errors.append("T_Unten ist None")
        elif math.isnan(t_boiler_unten):
            errors.append("T_Unten ist NaN")
        elif math.isinf(t_boiler_unten):
            errors.append("T_Unten ist Inf")
        else:
            errors.append(f"T_Unten ({t_boiler_unten}°C) außerhalb des gültigen Bereichs")
    
    if errors:
        if check_log_throttle(state, "last_sensor_error_time"):
            error_msg = ", ".join(errors)
            logging.error(f"Sensorfehler: {error_msg}")
            asyncio.create_task(send_telegram_message(
                session, state.chat_id,
                f"⚠️ Sensorfehler: {error_msg}",
                state.bot_token
            ))
        return False
    state.last_sensor_error_time = None
    return True

# Core control functions
async def check_sensors_and_safety(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """Prüft Sensorwerte, Verdampfertemperatur und Sicherheitsabschaltung."""
    state.t_oben = t_oben
    state.t_unten = t_unten
    state.t_mittig = t_mittig
    state.t_verd = t_verd
    state.t_boiler = (t_oben + t_unten) / 2 if t_oben is not None and t_unten is not None else None

    if not await check_for_sensor_errors(session, state, t_oben, t_unten):
        state.ausschluss_grund = "Sensorfehler: Ungültige Werte"
        logging.info("Kompressor bleibt aus wegen Sensorfehler")
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        return False

    if t_oben is not None and t_unten is not None and (
            t_oben >= state.sicherheits_temp or t_unten >= state.sicherheits_temp):
        state.ausschluss_grund = f"Übertemperatur (>= {state.sicherheits_temp} Grad)"
        logging.error(f"Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad")
        if state.kompressor_ein:
            result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                    state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(f"Kompressor ausgeschaltet (Sicherheitsabschaltung). Laufzeit: {state.last_runtime}")
            else:
                await handle_critical_compressor_error(session, state, "trotz Übertemperatur")
        asyncio.create_task(send_telegram_message(
            session, state.chat_id,
            f"⚠️ Sicherheitsabschaltung: T_Oben={t_oben:.1f} Grad, T_Unten={t_unten:.1f} Grad >= {state.sicherheits_temp} Grad",
            state.bot_token,
            parse_mode=None
        ))
        return False

    # Prüfe Verdampfertemperatur mit robuster Validierung
    if not is_valid_temperature(t_verd, min_temp=-20.0, max_temp=50.0):
        state.ausschluss_grund = f"Verdampfertemperatur ungültig (t_verd={'None' if t_verd is None else f'{t_verd:.1f}'} Grad)"
        logging.warning(state.ausschluss_grund)
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        return False
    
    if t_verd < state.verdampfertemperatur:
        state.ausschluss_grund = f"Verdampfertemperatur zu niedrig ({t_verd:.1f} Grad < {state.verdampfertemperatur} Grad)"
        # Throttle Logging um Spam und Watchdog-Timeouts zu vermeiden
        if check_log_throttle(state, "last_verdampfer_notification"):
            logging.warning(state.ausschluss_grund)
            asyncio.create_task(send_telegram_message(
                session, state.chat_id,
                f"⚠️ Kompressor bleibt aus oder wird ausgeschaltet: {state.ausschluss_grund}",
                state.bot_token,
                parse_mode=None
            ))
        else:
            # Debug-Level für wiederholte Meldungen
            logging.debug(state.ausschluss_grund)
        if state.kompressor_ein:
            result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = False
                state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                    state.local_tz)
                state.total_runtime_today += state.last_runtime
                logging.info(
                    f"Kompressor ausgeschaltet wegen zu niedriger Verdampfertemperatur. Laufzeit: {state.last_runtime}")
            else:
                await handle_critical_compressor_error(session, state, "trotz niedriger Verdampfertemperatur")
        return False
    return True

async def check_pressure_and_config(session, state, handle_pressure_check_func: Callable, set_kompressor_status_func: Callable, reload_config_func: Callable, calculate_file_hash_func: Callable, only_pressure: bool = False):
    """Prüft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.ausschluss_grund = "Druckschalterfehler"
        logging.info("Kompressor bleibt aus wegen Druckschalterfehler")
        if state.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False

    if not only_pressure:
        if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=60):
            current_hash = calculate_file_hash_func("config.ini")
            if current_hash != state.last_config_hash:
                await reload_config_func(session, state)
                state.last_config_hash = current_hash
            state._last_config_check = datetime.now(state.local_tz)
    return True

async def determine_mode_and_setpoints(state, t_unten, t_mittig):
    """Bestimmt den Betriebsmodus und setzt Sollwerte."""
    now = datetime.now(state.local_tz)
    # is_nighttime ist eine einfache Zeitvergleichsfunktion, kein Thread nötig
    is_night = is_nighttime(state.config)

    # Prüfe Solarfenster nur alle 5 Minuten
    within_solar_window = state.last_solar_window_status
    if (state.last_solar_window_check is None or
            safe_timedelta(now, state.last_solar_window_check, state.local_tz) >= timedelta(minutes=5)):
        within_solar_window = is_solar_window(state.config, state)
        state.last_solar_window_check = now
        state.last_solar_window_status = within_solar_window

    # Nacht- und Urlaubsabsenkung sicher abrufen (nur sinnvolle Temperaturwerte 0-20°C)
    nacht_reduction = 0.0
    urlaubs_reduction = 0.0
    
    if is_night:
        nacht_reduction = get_validated_reduction(state.config, "Heizungssteuerung", "NACHTABSENKUNG", 0.0)
    
    if state.urlaubsmodus_aktiv:
        urlaubs_reduction = get_validated_reduction(state.config, "Urlaubsmodus", "URLAUBSABSENKUNG", 0.0)
    
    total_reduction = nacht_reduction + urlaubs_reduction

    # Solarüberschuss-Schwellwerte aus Konfiguration lesen (nur bei Config-Änderung)
    if not hasattr(state, '_cached_solar_thresholds') or state.last_config_hash != getattr(state, '_last_threshold_config_hash', None):
        state._cached_solar_thresholds = {
            'batpower': state.config.getfloat("Solarueberschuss", "BATPOWER_THRESHOLD", fallback=600.0),
            'soc': state.config.getfloat("Solarueberschuss", "SOC_THRESHOLD", fallback=95.0),
            'feedinpower': state.config.getfloat("Solarueberschuss", "FEEDINPOWER_THRESHOLD", fallback=600.0)
        }
        state._last_threshold_config_hash = state.last_config_hash

    state.solar_ueberschuss_aktiv = (
            state.batpower > state._cached_solar_thresholds['batpower'] or
            (state.soc >= state._cached_solar_thresholds['soc'] and 
             state.feedinpower > state._cached_solar_thresholds['feedinpower'])
    )

    # Prüfe Übergangsmodus (morgens oder abends)
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)

    if state.bademodus_aktiv:
        if state.previous_modus != "Bademodus":
            logging.info("Wechsel zu Bademodus – steuere nach T_Unten")
            state.previous_modus = "Bademodus"
        return {
            "modus": "Bademodus",
            "ausschaltpunkt": state.ausschaltpunkt_erhoeht,
            "einschaltpunkt": state.ausschaltpunkt_erhoeht - 4,
            "regelfuehler": t_unten,
            "nacht_reduction": 0,
            "urlaubs_reduction": 0,
            "solar_ueberschuss_aktiv": False
        }

    if state.previous_modus == "Bademodus":
        logging.info("Wechsel von Bademodus zu Normalmodus")

    if state.solar_ueberschuss_aktiv:
        modus = "Solarüberschuss"
        ausschaltpunkt = state.ausschaltpunkt_erhoeht
        einschaltpunkt = state.einschaltpunkt_erhoeht
        regelfuehler = t_unten
    elif within_uebergangsmodus:
        modus = "Übergangsmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    elif is_night:
        modus = "Nachtmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig
    else:
        modus = "Normalmodus"
        ausschaltpunkt = state.basis_ausschaltpunkt - total_reduction
        einschaltpunkt = state.basis_einschaltpunkt - total_reduction
        regelfuehler = t_mittig

    if state.previous_modus != modus:
        logging.info(f"Wechsel zu Modus: {modus}")
        state.previous_modus = modus

    # DEBUG LOGGING für Config-Werte
    logging.debug(
        f"Modus-Ermittlung: {modus} | "
        f"Basis-Werte (State): Ein={state.basis_einschaltpunkt}, Aus={state.basis_ausschaltpunkt} | "
        f"Erhöht-Werte (State): Ein={state.einschaltpunkt_erhoeht}, Aus={state.ausschaltpunkt_erhoeht} | "
        f"Reduktion: Nacht={nacht_reduction}, Urlaub={urlaubs_reduction}, Total={total_reduction} | "
        f"Ergebnis: Ein={einschaltpunkt}, Aus={ausschaltpunkt}, Fühler={regelfuehler}"
    )

    return {
        "modus": modus,
        "ausschaltpunkt": ausschaltpunkt,
        "einschaltpunkt": einschaltpunkt,
        "regelfuehler": regelfuehler,
        "nacht_reduction": nacht_reduction,
        "urlaubs_reduction": urlaubs_reduction,
        "solar_ueberschuss_aktiv": state.solar_ueberschuss_aktiv
    }

async def handle_compressor_off(state, session, regelfuehler, ausschaltpunkt, min_laufzeit, t_oben, set_kompressor_status_func: Callable):
    """Prüft Abschaltbedingungen und schaltet Kompressor aus."""
    abschalten = regelfuehler is not None and regelfuehler >= ausschaltpunkt
    if abschalten and (state.previous_abschalten != abschalten or check_log_throttle(state, "last_abschalt_log")):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Abschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad >= {ausschaltpunkt:.1f} Grad"
        )
        logging.info(state.ausschluss_grund)
    state.previous_abschalten = abschalten

    can_turn_off = True
    if state.kompressor_ein and abschalten:
        elapsed_time = safe_timedelta(datetime.now(state.local_tz), state.start_time or state.last_compressor_on_time,
                                      state.local_tz)
        if elapsed_time.total_seconds() < min_laufzeit.total_seconds() - 0.5:
            can_turn_off = False
            state.ausschluss_grund = f"Mindestlaufzeit nicht erreicht ({min_laufzeit.total_seconds() - elapsed_time.total_seconds():.1f}s)"
            logging.debug(state.ausschluss_grund)

    if abschalten and state.kompressor_ein and can_turn_off:
        result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
        if result:
            state.kompressor_ein = False
            set_last_compressor_off_time(state, datetime.now(state.local_tz))
            state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time,
                                                state.local_tz)
            state.total_runtime_today += state.last_runtime
            state.last_completed_cycle = datetime.now(state.local_tz)
            logging.info(f"Kompressor ausgeschaltet. Laufzeit: {state.last_runtime}")
            return True
        await handle_critical_compressor_error(session, state, "")
    return False

async def handle_compressor_on(state, session, regelfuehler, einschaltpunkt, min_laufzeit, min_pause,
                               within_solar_window, t_oben, set_kompressor_status_func: Callable):
    """Prüft Einschaltbedingungen und schaltet Kompressor ein."""
    now = datetime.now(state.local_tz)
    temp_conditions_met = regelfuehler is not None and regelfuehler <= einschaltpunkt
    if temp_conditions_met and state.previous_temp_conditions != temp_conditions_met:
        logging.info(
            f"[{state.previous_modus}] Einschaltbedingung erreicht: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{regelfuehler:.1f} Grad <= {einschaltpunkt:.1f} Grad"
        )
    elif not temp_conditions_met and check_log_throttle(state, "last_no_start_log"):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten: "
            f"{'T_Unten' if state.previous_modus in ['Bademodus', 'Solarüberschuss'] else 'T_Mittig'}="
            f"{f'{regelfuehler:.1f}' if regelfuehler is not None else 'N/A'} Grad > {einschaltpunkt:.1f} Grad"
        )
        logging.debug(state.ausschluss_grund)
    state.previous_temp_conditions = temp_conditions_met

    # Prüfe ob wir im Übergangsmodus sind (nicht gecachten within_solar_window nutzen!)
    within_uebergangsmodus = ist_uebergangsmodus_aktiv(state)
    
    # Im Übergangsmodus nur heizen wenn Solarüberschuss aktiv ist (außer Bademodus)
    solar_conditions_met = not (not state.bademodus_aktiv and within_uebergangsmodus and not state.solar_ueberschuss_aktiv)
    if not solar_conditions_met and check_log_throttle(state, "last_no_start_log"):
        state.ausschluss_grund = (
            f"[{state.previous_modus}] Kein Einschalten im Übergangsmodus: Solarüberschuss nicht aktiv "
            f"(Morgens: {state.nachtabsenkung_ende.strftime('%H:%M')}–{state.uebergangsmodus_morgens_ende.strftime('%H:%M')}, "
            f"Abends: {state.uebergangsmodus_abends_start.strftime('%H:%M')}–{state.nachtabsenkung_start.strftime('%H:%M')})"
        )
        logging.debug(state.ausschluss_grund)

    pause_ok = True
    if not state.kompressor_ein and temp_conditions_met and solar_conditions_met and state.last_compressor_off_time:
        if state.last_compressor_off_time is None:
            time_since_off = timedelta.max
        else:
            time_since_off = safe_timedelta(now, state.last_compressor_off_time, state.local_tz)
        if time_since_off.total_seconds() < min_pause.total_seconds() - 0.5:
            pause_ok = False
            pause_remaining = min_pause - time_since_off
            reason = f"Zu kurze Pause ({pause_remaining.total_seconds():.1f}s verbleibend)"
            if check_log_throttle(state, "last_pause_log"):
                logging.info(f"Kompressor START VERHINDERT: {reason}")
                asyncio.create_task(send_telegram_message(
                    session, state.chat_id,
                    f"⚠️ Kompressor bleibt aus: {reason}...",
                    state.bot_token,
                    parse_mode=None
                ))
                state.last_pause_telegram_notification = now
                state.current_pause_reason = reason
            state.ausschluss_grund = reason
        else:
            state.current_pause_reason = None
            state.last_pause_log = None
            state.last_pause_telegram_notification = None

    if not state.kompressor_ein and temp_conditions_met and pause_ok and solar_conditions_met:
        can_start_new_cycle = True
        if state.last_completed_cycle and safe_timedelta(now, state.last_completed_cycle,
                                                         state.local_tz).total_seconds() < min_laufzeit.total_seconds() + min_pause.total_seconds():
            can_start_new_cycle = False
            state.ausschluss_grund = (
                f"Neuer Zyklus nicht erlaubt: Warte auf Abschluss von Mindestlaufzeit + Mindestpause "
                f"({min_laufzeit.total_seconds() + min_pause.total_seconds() - safe_timedelta(now, state.last_completed_cycle, state.local_tz).total_seconds():.1f}s)"
            )
            logging.debug(state.ausschluss_grund)

        if can_start_new_cycle:
            logging.info(
                f"Alle Bedingungen für Kompressorstart erfüllt. Versuche einzuschalten (Modus: {state.previous_modus}).")
            result = await set_kompressor_status_func(state, True, t_boiler_oben=t_oben)
            if result:
                state.kompressor_ein = True
                state.start_time = now
                state.last_compressor_on_time = now
                logging.info(f"Kompressor eingeschaltet. Startzeit: {now}")
                state.ausschluss_grund = None
                return True
            state.ausschluss_grund = state.ausschluss_grund or "Unbekannter Fehler beim Einschalten"
            logging.info(f"Kompressor nicht eingeschaltet: {state.ausschluss_grund}")
    return False

async def handle_mode_switch(state, session, t_oben, t_mittig, set_kompressor_status_func: Callable):
    """Prüft und behandelt Moduswechsel bei Solarüberschuss-Änderung."""
    if state.kompressor_ein and state.solar_ueberschuss_aktiv != state.previous_solar_ueberschuss_aktiv and not state.bademodus_aktiv:
        effective_ausschaltpunkt = state.previous_ausschaltpunkt or state.aktueller_ausschaltpunkt
        if not state.solar_ueberschuss_aktiv and t_oben is not None and t_mittig is not None:
            if t_oben >= effective_ausschaltpunkt or t_mittig >= effective_ausschaltpunkt:
                result = await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben)
                if result:
                    state.kompressor_ein = False
                    set_last_compressor_off_time(state, datetime.now(state.local_tz))
                    state.last_runtime = safe_timedelta(datetime.now(state.local_tz), state.last_compressor_on_time)
                    state.total_runtime_today += state.last_runtime
                    logging.info(f"Kompressor ausgeschaltet bei Moduswechsel. Laufzeit: {state.last_runtime}")
                    state.ausschluss_grund = None
                    return True
                await handle_critical_compressor_error(session, state, "bei Moduswechsel")
    return False
