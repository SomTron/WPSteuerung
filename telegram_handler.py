import aiohttp
import asyncio
import logging
import pytz
import io
import aiofiles
import os
from aiohttp import FormData
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

# Logging-Konfiguration wird in main.py definiert
# Empfehlung: Stelle sicher, dass logger.setLevel(logging.DEBUG) in main.py gesetzt ist

def is_solar_window(config):
    """Prüft, ob die aktuelle Uhrzeit im Solarfenster nach der Nachtabsenkung liegt."""
    local_tz = pytz.timezone("Europe/Berlin")
    now = datetime.now(local_tz)
    logging.debug(f"is_solar_window: now={now}, tzinfo={now.tzinfo}")
    try:
        end_time_str = config["Heizungssteuerung"].get("NACHTABSENKUNG_END", "06:00")
        if not isinstance(end_time_str, str):
            raise ValueError("NACHTABSENKUNG_END muss ein String sein")
        try:
            end_hour, end_minute = map(int, end_time_str.split(':'))
        except ValueError:
            raise ValueError(f"Ungültiges Zeitformat: NACHTABSENKUNG_END={end_time_str}")
        if not (0 <= end_hour < 24 and 0 <= end_minute < 60):
            raise ValueError(f"Ungültige Zeitwerte: Ende={end_time_str}")

        potential_night_setback_end_today = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        if now < potential_night_setback_end_today + timedelta(hours=2):
            night_setback_end_time_today = potential_night_setback_end_today
        else:
            night_setback_end_time_today = potential_night_setback_end_today + timedelta(days=1)
        solar_only_window_start_time_today = night_setback_end_time_today
        solar_only_window_end_time_today = night_setback_end_time_today + timedelta(hours=2)
        within_solar_only_window = solar_only_window_start_time_today <= now < solar_only_window_end_time_today

        logging.debug(
            f"Solarfensterprüfung: Jetzt={now.strftime('%H:%M')}, "
            f"Start={solar_only_window_start_time_today.strftime('%H:%M')}, "
            f"Ende={solar_only_window_end_time_today.strftime('%H:%M')}, "
            f"Ist Solarfenster={within_solar_only_window}"
        )
        return within_solar_only_window
    except Exception as e:
        logging.error(f"Fehler in is_solar_window: {e}")
        return False

async def send_telegram_message(session, chat_id, message, bot_token, retries=3, retry_delay=5):
    """Sendet eine Nachricht über Telegram mit Fehlerbehandlung und Wiederholungslogik."""
    if len(message) > 4096:
        message = message[:4093] + "..."
        logging.warning("Nachricht gekürzt, da Telegram-Limit von 4096 Zeichen überschritten.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"  # Explizit Markdown setzen, falls gewünscht
    }

    logging.debug(f"Sende Telegram-Nachricht: chat_id={chat_id}, message={message[:150]}... (Länge={len(message)})")

    for attempt in range(1, retries + 1):
        try:
            async with session.post(url, json=payload, timeout=20) as response:
                if response.status == 200:
                    logging.info(f"Telegram-Nachricht gesendet: {message[:100]}...")
                    return True
                else:
                    error_text = await response.text()
                    logging.error(f"Fehler beim Senden der Telegram-Nachricht (Status {response.status}): {error_text}")
                    return False
        except aiohttp.ClientError as e:
            logging.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries}): {e}")
            if attempt < retries:
                logging.info(f"Warte {retry_delay} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Alle Versuche fehlgeschlagen (Netzwerkfehler).")
                return False
        except asyncio.TimeoutError:
            logging.error(f"Timeout beim Senden der Telegram-Nachricht (Versuch {attempt}/{retries})")
            if attempt < retries:
                logging.info(f"Warte {retry_delay} Sekunden vor dem nächsten Versuch...")
                await asyncio.sleep(retry_delay)
            else:
                logging.error("Alle Versuche fehlgeschlagen (Timeout).")
                return False
        except Exception as e:
            logging.error(f"Unerwarteter Fehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)
            return False
    return False
async def send_welcome_message(session, chat_id, bot_token):
    """Sendet die Willkommensnachricht mit benutzerdefiniertem Keyboard."""
    message = "Willkommen! Verwende die Schaltflächen unten, um das System zu steuern."
    keyboard = {
        "keyboard": [
            ["🌡️ Temperaturen", "📊 Status"],
            ["📈 Verlauf 6h", "📉 Verlauf 24h"],
            ["🌴 Urlaub", "🏠 Urlaub aus"],
            ["🆘 Hilfe", "⏱️ Laufzeiten"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "reply_markup": keyboard
    }
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            response.raise_for_status()
            logging.info("Willkommensnachricht mit Keyboard gesendet.")
            return True
    except Exception as e:
        logging.error(f"Fehler beim Senden der Willkommensnachricht: {e}", exc_info=True)
        return False

async def get_telegram_updates(session, bot_token, offset=None):
    """Ruft Telegram-Updates ab."""
    logging.debug(f"Rufe Telegram-Updates ab mit offset={offset}")
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 60}
    if offset is not None:
        params["offset"] = offset
    try:
        async with session.get(url, params=params, timeout=70) as response:
            #logging.debug(f"HTTP-Status von getUpdates: {response.status}")
            if response.status == 200:
                data = await response.json()
                updates = data.get("result", [])
                logging.debug(f"Empfangene Telegram-Updates: {len(updates)}")
                return updates
            else:
                error_text = await response.text()
                logging.error(f"Fehler beim Abrufen von Telegram-Updates: Status {response.status}, Details: {error_text}")
                return None
    except aiohttp.ClientError as e:
        logging.error(f"Netzwerkfehler beim Abrufen von Telegram-Updates: {e}", exc_info=True)
        return None
    except asyncio.TimeoutError:
        logging.warning("Timeout beim Abrufen von Telegram-Updates")
        return None
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Abrufen von Telegram-Updates: {e}", exc_info=True)
        return None

async def aktivere_urlaubsmodus(session, chat_id, bot_token, config, state):
    """Aktiviert den Urlaubsmodus."""
    state.urlaubsmodus_aktiv = True
    urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 6))
    await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert (-{urlaubsabsenkung}°C).", bot_token)
    logging.info("Urlaubsmodus aktiviert")

async def deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state):
    """Deaktiviert den Urlaubsmodus."""
    state.urlaubsmodus_aktiv = False
    await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus deaktiviert.", bot_token)
    logging.info("Urlaubsmodus deaktiviert")

async def send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, chat_id, bot_token):
    """Sendet die aktuellen Temperaturen über Telegram."""
    logging.debug(f"Generiere Temperaturen-Nachricht: t_oben={t_boiler_oben}, t_unten={t_boiler_unten}, t_mittig={t_boiler_mittig}, t_verd={t_verd}")

    # Explizite Typprüfung
    try:
        t_oben = float(t_boiler_oben) if t_boiler_oben is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_oben: {t_boiler_oben}, Fehler: {e}")
        t_oben = None
    try:
        t_unten = float(t_boiler_unten) if t_boiler_unten is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_unten: {t_boiler_unten}, Fehler: {e}")
        t_unten = None
    try:
        t_mittig = float(t_boiler_mittig) if t_boiler_mittig is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_mittig: {t_boiler_mittig}, Fehler: {e}")
        t_mittig = None
    try:
        t_verd = float(t_verd) if t_verd is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_verd: {t_verd}, Fehler: {e}")
        t_verd = None

    # Schrittweise Nachrichten-Generierung
    temp_lines = []
    temp_lines.append("🌡️ Aktuelle Temperaturen:")
    logging.debug("Temperaturen-Nachricht: Linie 1 generiert")
    temp_lines.append(f"Boiler oben: {'N/A' if t_oben is None else f'{t_oben:.1f}°C'}")
    logging.debug("Temperaturen-Nachricht: Linie 2 generiert")
    temp_lines.append(f"Boiler mittig: {'N/A' if t_mittig is None else f'{t_mittig:.1f}°C'}")
    logging.debug("Temperaturen-Nachricht: Linie 3 generiert")
    temp_lines.append(f"Boiler unten: {'N/A' if t_unten is None else f'{t_unten:.1f}°C'}")
    logging.debug("Temperaturen-Nachricht: Linie 4 generiert")
    try:
        temp_lines.append(f"Verdampfer: {'N/A' if t_verd is None else f'{t_verd:.1f}°C'}")
        logging.debug("Temperaturen-Nachricht: Linie 5 (Verdampfer) generiert")
    except Exception as e:
        logging.error(f"Fehler beim Formatieren von Verdampfer: t_verd={t_verd}, Fehler: {e}", exc_info=True)
        temp_lines.append("Verdampfer: Fehler")

    # Nachricht zusammenfügen
    message = "\n".join(temp_lines)
    logging.debug(f"Vollständige Temperaturen-Nachricht (Länge={len(message)}): {message}")

    await send_telegram_message(session, chat_id, message, bot_token)

async def send_status_telegram(session, t_oben, t_unten, t_mittig, t_verd, kompressor_status, current_runtime, total_runtime, config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func=None, is_solar_window_func=None):
    """Sendet den aktuellen Systemstatus über Telegram."""
    logging.debug(f"Generiere Status-Nachricht: t_oben={t_oben}, t_unten={t_unten}, t_mittig={t_mittig}, t_verd={t_verd}")

    # Explizite Typprüfung
    try:
        t_oben = float(t_oben) if t_oben is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_oben: {t_oben}, Fehler: {e}")
        t_oben = None
    try:
        t_unten = float(t_unten) if t_unten is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_unten: {t_unten}, Fehler: {e}")
        t_unten = None
    try:
        t_mittig = float(t_mittig) if t_mittig is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_mittig: {t_mittig}, Fehler: {e}")
        t_mittig = None
    try:
        t_verd = float(t_verd) if t_verd is not None else None
    except (ValueError, TypeError) as e:
        logging.warning(f"Ungültiger Wert für t_verd: {t_verd}, Fehler: {e}")
        t_verd = None

    solax_data = await get_solax_data_func(session, state) or {
        "feedinpower": 0, "batPower": 0, "soc": 0, "api_fehler": True
    }
    feedinpower = solax_data.get("feedinpower", 0)
    bat_power = solax_data.get("batPower", 0)

    def format_time(seconds_str):
        try:
            if isinstance(seconds_str, timedelta):
                seconds = int(seconds_str.total_seconds())
            else:
                seconds = int(seconds_str)
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}min"
        except (ValueError, TypeError):
            return "0h 0min"

    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_nighttime_func and is_nighttime_func(config) else 0
    is_solar_window_active = is_solar_window_func and is_solar_window_func(config)

    # Modusbestimmung
    if state.urlaubsmodus_aktiv:
        mode_str = f"Urlaub (-{int(config['Urlaubsmodus'].get('URLAUBSABSENKUNG', 6))}°C)"
    elif is_solar_window_active:
        mode_str = "Übergangszeit (Solarfenster)"
        if state.solar_ueberschuss_aktiv:
            mode_str += " + Solarüberschuss"
    elif state.solar_ueberschuss_aktiv and is_nighttime_func and is_nighttime_func(config):
        mode_str = f"Solarüberschuss + Nachtabsenkung (-{nacht_reduction}°C)"
    elif state.solar_ueberschuss_aktiv:
        mode_str = "Solarüberschuss"
    elif is_nighttime_func and is_nighttime_func(config):
        mode_str = f"Nachtabsenkung (-{nacht_reduction}°C)"
    else:
        mode_str = "Normal"

    compressor_status_str = "EIN" if kompressor_status else "AUS"

    # Schrittweise Nachrichten-Generierung
    status_lines = []
    status_lines.append("📊 **Systemstatus**")
    status_lines.append("🌡️ **Temperaturen**")
    #logging.debug("Status-Nachricht: Linie 1-2 generiert")
    status_lines.append(f"  • Oben: {'N/A' if t_oben is None else f'{t_oben:.1f}°C'}")
    #logging.debug("Status-Nachricht: Linie 3 generiert")
    status_lines.append(f"  • Mittig: {'N/A' if t_mittig is None else f'{t_mittig:.1f}°C'}")
    #logging.debug("Status-Nachricht: Linie 4 generiert")
    status_lines.append(f"  • Unten: {'N/A' if t_unten is None else f'{t_unten:.1f}°C'}")
    #logging.debug("Status-Nachricht: Linie 5 generiert")
    try:
        status_lines.append(f"  • Verdampfer: {'N/A' if t_verd is None else f'{t_verd:.1f}°C'}")
        #logging.debug("Status-Nachricht: Linie 6 (Verdampfer) generiert")
    except Exception as e:
        logging.error(f"Fehler beim Formatieren von Verdampfer: t_verd={t_verd}, Fehler: {e}", exc_info=True)
        status_lines.append("  • Verdampfer: Fehler")
    status_lines.append("🛠️ **Kompressor**")
    #logging.debug("Status-Nachricht: Linie 7 generiert")
    status_lines.append(f"  • Status: {compressor_status_str}")
    #logging.debug("Status-Nachricht: Linie 8 generiert")
    status_lines.append(f"  • Aktuelle Laufzeit: {format_time(current_runtime)}")
    #logging.debug("Status-Nachricht: Linie 9 generiert")
    status_lines.append(f"  • Gesamtlaufzeit heute: {format_time(total_runtime)}")
    #logging.debug("Status-Nachricht: Linie 10 generiert")
    status_lines.append(f"  • Letzte Laufzeit: {format_time(state.last_runtime)}")
   #logging.debug("Status-Nachricht: Linie 11 generiert")
    status_lines.append("🎯 **Sollwerte**")
    #logging.debug("Status-Nachricht: Linie 12 generiert")
    status_lines.append(f"  • Einschaltpunkt: {state.aktueller_einschaltpunkt}°C")
    #logging.debug("Status-Nachricht: Linie 13 generiert")
    status_lines.append(f"  • Ausschaltpunkt: {state.aktueller_ausschaltpunkt}°C")
    #logging.debug("Status-Nachricht: Linie 14 generiert")
    status_lines.append(f"  • Gilt für: {'Unten' if state.solar_ueberschuss_aktiv else 'Oben, Mitte'}")
    #logging.debug("Status-Nachricht: Linie 15 generiert")
    status_lines.append("⚙️ **Betriebsmodus**")
    #logging.debug("Status-Nachricht: Linie 16 generiert")
    status_lines.append(f"  • {mode_str}")
    #logging.debug("Status-Nachricht: Linie 17 generiert")
    status_lines.append("ℹ️ **Zusatzinfo**")
    #logging.debug("Status-Nachricht: Linie 18 generiert")
    status_lines.append(f"  • Solarüberschuss: {feedinpower:.1f} W")
    #logging.debug("Status-Nachricht: Linie 19 generiert")
    status_lines.append(f"  • Batterieleistung: {bat_power:.1f} W ({'Laden' if bat_power > 0 else 'Entladung' if bat_power < 0 else 'Neutral'})")
    #logging.debug("Status-Nachricht: Linie 20 generiert")
    status_lines.append(f"  • Solarüberschuss aktiv: {'Ja' if state.solar_ueberschuss_aktiv else 'Nein'}")
    #logging.debug("Status-Nachricht: Linie 21 generiert")
    if state.ausschluss_grund:
        status_lines.append(f"  • Ausschlussgrund: {state.ausschluss_grund}")
        #logging.debug("Status-Nachricht: Linie 22 generiert")

    # Nachricht zusammenfügen
    message = "\n".join(status_lines)
    logging.debug(f"Vollständige Status-Nachricht (Länge={len(message)}): {message}")

    await send_telegram_message(session, chat_id, message, bot_token)

async def send_unknown_command_message(session, chat_id, bot_token):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    await send_telegram_message(session, chat_id, "❓ Unbekannter Befehl. Verwende 'Hilfe' für eine Liste der Befehle.",
                                bot_token)

async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates,
                                         last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                                         chat_id, bot_token, config, get_solax_data_func, state,
                                         get_temperature_history_func, get_runtime_bar_chart_func,
                                         is_nighttime_func, is_solar_window_func):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    try:
        logging.debug(f"Verarbeite {len(updates)} Telegram-Updates")
        if updates:
            for update in updates:
                logging.debug(f"Update-Inhalt: {update}")
                message = update.get('message', {})
                message_text = message.get('text')
                chat_id_from_update = message.get('chat', {}).get('id')
                if message_text and chat_id_from_update:
                    message_text = message_text.strip()
                    logging.info(f"Empfangene Telegram-Nachricht: '{message_text}' von chat_id {chat_id_from_update}")
                    try:
                        chat_id_from_update = int(chat_id_from_update)
                        expected_chat_id = int(chat_id)
                        logging.debug(f"chat_id_from_update: {chat_id_from_update} (Typ: {type(chat_id_from_update)}), "
                                      f"expected_chat_id: {expected_chat_id} (Typ: {type(expected_chat_id)})")
                        if chat_id_from_update != expected_chat_id:
                            logging.warning(f"Ungültige chat_id: {chat_id_from_update}, erwartet: {expected_chat_id}")
                            continue
                    except (ValueError, TypeError) as e:
                        logging.error(f"Fehler bei der chat_id-Konvertierung: {e}, "
                                      f"chat_id_from_update={chat_id_from_update}, chat_id={chat_id}")
                        continue
                    message_text_lower = message_text.lower()
                    if message_text_lower == "🌡️ temperaturen" or message_text_lower == "temperaturen":
                        logging.debug(f"Sensorwerte: oben={t_boiler_oben}, unten={t_boiler_unten}, mittig={t_boiler_mittig}, verd={t_verd}")
                        await send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig,
                                                       t_verd, chat_id, bot_token)
                    elif message_text_lower == "📊 status" or message_text_lower == "status":
                        logging.debug(f"Sensorwerte: oben={t_boiler_oben}, unten={t_boiler_unten}, mittig={t_boiler_mittig}, verd={t_verd}")
                        await send_status_telegram(
                            session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd,
                            kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                            config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func, is_solar_window_func
                        )
                    elif message_text_lower == "🆘 hilfe" or message_text_lower == "hilfe":
                        await send_help_message(session, chat_id, bot_token)
                    elif message_text_lower == "🌴 urlaub" or message_text_lower == "urlaub":
                        if state.urlaubsmodus_aktiv:
                            await send_telegram_message(session, chat_id, "🌴 Urlaubsmodus ist bereits aktiviert.",
                                                       bot_token)
                        else:
                            await aktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
                    elif message_text_lower == "🏠 urlaub aus" or message_text_lower == "urlaub aus":
                        if not state.urlaubsmodus_aktiv:
                            await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus ist bereits deaktiviert.",
                                                       bot_token)
                        else:
                            await deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
                    elif message_text_lower == "📈 verlauf 6h" or message_text_lower == "verlauf 6h":
                        await get_temperature_history_func(session, 6, state, config)
                    elif message_text_lower == "📉 verlauf 24h" or message_text_lower == "verlauf 24h":
                        await get_temperature_history_func(session, 24, state, config)
                    elif "laufzeiten" in message_text_lower:
                        days = 7
                        try:
                            if len(message_text_lower.split()) > 1:
                                days = int(message_text_lower.split()[1])
                                if days <= 0:
                                    days = 7
                                    logging.warning(f"Ungültige Zahl '{message_text}', verwende Standardwert 7.")
                        except ValueError:
                            logging.warning(f"Ungültige Zahl '{message_text}', verwende Standardwert 7.")
                        await get_runtime_bar_chart_func(session, days=days, state=state)
                    else:
                        await send_unknown_command_message(session, chat_id, bot_token)
                    return update['update_id'] + 1
                else:
                    logging.debug(f"Update ohne gültigen Text oder chat_id: {update}")
        else:
            logging.debug("Keine Telegram-Updates empfangen")
        return last_update_id
    except Exception as e:
        logging.error(f"Fehler in process_telegram_messages_async: {e}", exc_info=True)
        return last_update_id

async def telegram_task(session, read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func):
    """Telegram-Task zur Verarbeitung von Nachrichten."""
    logging.info("Starte telegram_task")
    last_update_id = None
    while True:
        #logging.debug("telegram_task Schleife ausgeführt")
        try:
            if not state.bot_token or not state.chat_id:
                logging.warning(f"Telegram bot_token oder chat_id fehlt (bot_token={state.bot_token}, chat_id={state.chat_id}). Überspringe telegram_task.")
                await asyncio.sleep(60)
                continue
            logging.debug("Versuche Telegram-Updates abzurufen")
            updates = await get_telegram_updates(session, state.bot_token, last_update_id)
            if updates is not None:
                #logging.debug("Updates erfolgreich empfangen")
                # Parallele Sensorlesung
                sensor_tasks = [
                    asyncio.to_thread(read_temperature_func, sensor_ids[key])
                    for key in ["oben", "unten", "mittig", "verd"]
                ]
                t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd = await asyncio.gather(*sensor_tasks, return_exceptions=True)
                # Prüfe auf Sensorfehler
                for temp, key in zip([t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd], ["oben", "unten", "mittig", "verd"]):
                    if isinstance(temp, Exception) or temp is None:
                        logging.error(f"Fehler beim Lesen des Sensors {sensor_ids[key]}: {temp or 'Kein Wert'}")
                        temp = None
                # Erzwinge Rohwerte-Log
                #logging.debug(f"Rohwerte (vor Verarbeitung): t_oben={t_boiler_oben}, t_unten={t_boiler_unten}, t_mittig={t_boiler_mittig}, t_verd={t_verd}")
                kompressor_status = kompressor_status_func()
                aktuelle_laufzeit = current_runtime_func()
                gesamtlaufzeit = total_runtime_func()
                last_update_id = await process_telegram_messages_async(
                    session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates, last_update_id,
                    kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, state.chat_id, state.bot_token, config,
                    get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func,
                    is_nighttime_func, is_solar_window
                )
            else:
                logging.warning("Telegram-Updates waren None")
            await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Fehler in telegram_task: {str(e)}", exc_info=True)
            await asyncio.sleep(10)

async def send_help_message(session, chat_id, bot_token):
    """Sendet eine Hilfenachricht mit verfügbaren Befehlen über Telegram."""
    message = (
        "ℹ️ **Hilfe - Verfügbare Befehle**\n\n"
        "🌡️ **Temperaturen**: Zeigt die aktuellen Temperaturen an.\n"
        "📊 **Status**: Zeigt den vollständigen Systemstatus an.\n"
        "🆘 **Hilfe**: Zeigt diese Hilfenachricht an.\n"
        "🌴 **Urlaub**: Aktiviert den Urlaubsmodus.\n"
        "🏠 **Urlaub aus**: Deaktiviert den Urlaubsmodus.\n"
        "📈 **Verlauf 6h**: Zeigt den Temperaturverlauf der letzten 6 Stunden.\n"
        "📉 **Verlauf 24h**: Zeigt den Temperaturverlauf der letzten 24 Stunden.\n"
        "⏱️ **Laufzeiten [Tage]**: Zeigt die Laufzeiten der letzten X Tage (Standard: 7).\n"
    )
    await send_telegram_message(session, chat_id, message, bot_token)

# Neue Hilfsfunktion: Liefert Liste der Zeilen innerhalb des gewünschten Zeitfensters
def prefilter_csv_lines(file_path, hours, tz):
    now = datetime.now(tz)
    time_ago = now - timedelta(hours=hours)
    relevant_lines = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            header = next(f)
            relevant_lines.append(header.strip())
            for line_num, line in enumerate(f):
                try:
                    parts = line.strip().split(",")
                    if len(parts) < 2:
                        continue
                    timestamp_str = parts[0]
                    timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                    if pd.isna(timestamp):
                        continue
                    if timestamp.tzinfo is None:
                        timestamp = tz.localize(timestamp)
                    else:
                        timestamp = timestamp.astimezone(tz)
                    if time_ago <= timestamp <= now:
                        relevant_lines.append(line.strip())
                except Exception as e:
                    logging.warning(f"⚠️ Konnte Zeile {line_num} nicht verarbeiten: {e}")
        logging.debug(f"✅ {len(relevant_lines)} Zeilen nach Vorfilterung.")
        return relevant_lines
    except Exception as e:
        logging.error(f"❌ Fehler beim Lesen der CSV-Zeilen: {e}", exc_info=True)
        return []


async def get_boiler_temperature_history(session, hours, state, config):
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)

        logging.debug(f"⏳ Starte Temperaturverlauf für {hours} Stunden, Zeitfenster: {time_ago} bis {now}")

        file_path = "heizungsdaten.csv"
        if not os.path.isfile(file_path):
            logging.error(f"❌ CSV-Datei nicht gefunden: {file_path}")
            await send_telegram_message(session, state.chat_id, "CSV-Datei nicht gefunden.", state.bot_token)
            return

        # 1. CSV effizient einlesen mit Pandas
        logging.debug(f"🔍 Lese CSV-Datei: {file_path}")
        try:
            df = pd.read_csv(file_path, usecols=[
                "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Verd",
                "Kompressor", "PowerSource", "Einschaltpunkt", "Ausschaltpunkt"
            ], engine="c")
            logging.debug(f"CSV geladen, {len(df)} Zeilen, Spalten: {df.columns.tolist()}")
        except Exception as e:
            logging.error(f"❌ Fehler beim Einlesen der CSV: {e}", exc_info=True)
            await send_telegram_message(session, state.chat_id, "Fehler beim Lesen der CSV-Datei.", state.bot_token)
            return

        # 2. Prüfen, ob Zeitstempel-Spalte existiert
        if "Zeitstempel" not in df.columns:
            logging.error("❌ Spalte 'Zeitstempel' fehlt in der CSV.")
            await send_telegram_message(session, state.chat_id, "Spalte 'Zeitstempel' fehlt in der CSV.", state.bot_token)
            return

        # 3. Zeitstempel manuell parsen
        logging.debug(f"Erste Zeitstempel vor Parsing: {df['Zeitstempel'].head().tolist()}")
        try:
            df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
            logging.debug(f"Zeitstempel-Datentyp nach Parsing: {df['Zeitstempel'].dtype}")
        except Exception as e:
            logging.error(f"❌ Fehler beim Parsen der Zeitstempel: {e}", exc_info=True)
            await send_telegram_message(session, state.chat_id, "Fehler beim Parsen der Zeitstempel.", state.bot_token)
            return

        # 4. Ungültige Zeitstempel prüfen und entfernen
        invalid_rows = df[df["Zeitstempel"].isna()]
        if not invalid_rows.empty:
            logging.warning(f"⚠️ {len(invalid_rows)} Zeilen mit ungültigen Zeitstempeln gefunden. Beispiele: {invalid_rows['Zeitstempel'].head().tolist()}")
            df = df[df["Zeitstempel"].notna()].copy()
            logging.debug(f"Nach Entfernen ungültiger Zeitstempel: {len(df)} Zeilen")

        if df.empty:
            logging.warning(f"❌ Keine gültigen Daten nach Zeitstempel-Parsing für die letzten {hours} Stunden.")
            try:
                latest_data = pd.read_csv(file_path, usecols=["Zeitstempel"])
                latest_data["Zeitstempel"] = pd.to_datetime(latest_data["Zeitstempel"], errors='coerce')
                latest_time = latest_data["Zeitstempel"].dropna().max() if not latest_data["Zeitstempel"].dropna().empty else "unbekannt"
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine gültigen Daten für die letzten {hours} Stunden vorhanden. Letzter Eintrag: {latest_time}.",
                    state.bot_token
                )
            except Exception as e:
                logging.error(f"❌ Fehler beim Abrufen des neuesten Zeitstempels: {e}", exc_info=True)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine gültigen Daten für die letzten {hours} Stunden vorhanden. Fehler beim Abrufen des neuesten Eintrags.",
                    state.bot_token
                )
            return

        # 5. Zeitzone hinzufügen
        try:
            df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz, ambiguous='infer', nonexistent='shift_forward')
            logging.debug(f"Zeitstempel nach Zeitzone: {df['Zeitstempel'].head().tolist()}")
        except Exception as e:
            logging.error(f"❌ Fehler beim Hinzufügen der Zeitzone: {e}", exc_info=True)
            await send_telegram_message(session, state.chat_id, "Fehler beim Hinzufügen der Zeitzone.", state.bot_token)
            return

        # 6. Filtern nach Zeitfenster
        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]
        logging.debug(f"Nach Zeitfenster-Filter: {len(df)} Zeilen, Zeitstempel-Bereich: {df['Zeitstempel'].min()} bis {df['Zeitstempel'].max()}")

        if df.empty:
            logging.warning(f"❌ Keine Daten für die letzten {hours} Stunden gefunden.")
            try:
                latest_data = pd.read_csv(file_path, usecols=["Zeitstempel"])
                latest_data["Zeitstempel"] = pd.to_datetime(latest_data["Zeitstempel"], errors='coerce')
                latest_time = latest_data["Zeitstempel"].dropna().max() if not latest_data["Zeitstempel"].dropna().empty else "unbekannt"
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine Daten für die letzten {hours} Stunden vorhanden. Letzter Eintrag: {latest_time}.",
                    state.bot_token
                )
            except Exception as e:
                logging.error(f"❌ Fehler beim Abrufen des neuesten Zeitstempels: {e}", exc_info=True)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine Daten für die letzten {hours} Stunden vorhanden. Fehler beim Abrufen des neuesten Eintrags.",
                    state.bot_token
                )
            return

        # 7. Temperaturwerte konvertieren
        temp_columns = ["T_Oben", "T_Unten", "T_Mittig", "T_Verd"]
        for col in temp_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                logging.warning(f"Spalte {col} fehlt in der CSV.")
                df[col] = float('nan')

        # 8. Y-Achsen-Skalierung
        min_temp = df[temp_columns].min().min()
        max_temp = df[temp_columns].max().max()
        y_min = max(0, min_temp - 2) if pd.notna(min_temp) else 0
        y_max = max_temp + 5 if pd.notna(max_temp) else 60

        # 9. Farbschema für Energiequelle
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "Keine aktive Energiequelle": "blue",
            "Unbekannt": "gray"
        }

        # 10. Plot erstellen
        plt.figure(figsize=(12, 6))
        shown_labels = set()

        # 11. Kompressoreinschaltphasen farbig markieren
        if "Kompressor" in df.columns and "PowerSource" in df.columns:
            # Optional: Umwandlung in Boolean für bessere Lesbarkeit
            df["Kompressor"] = df["Kompressor"].map({"EIN": True, "AUS": False}).fillna(False)

            for source, color in color_map.items():
                mask = (df["PowerSource"] == source) & df["Kompressor"]  # Benutzt jetzt Boolean
                if mask.any():
                    label = f"Kompressor EIN ({source})"
                    if label not in shown_labels:
                        plt.fill_between(df["Zeitstempel"], y_min, y_max, where=mask, color=color, alpha=0.3,
                                         label=label)
                        shown_labels.add(label)
                    else:
                        plt.fill_between(df["Zeitstempel"], y_min, y_max, where=mask, color=color, alpha=0.3)

        # 12. Temperaturen plotten (ohne Marker)
        for col, color, linestyle in [
            ("T_Oben", "blue", "-"),
            ("T_Unten", "red", "-"),
            ("T_Mittig", "purple", "-"),
            ("T_Verd", "gray", "--")
        ]:
            if col in df.columns and df[col].notna().any():
                plt.plot(df["Zeitstempel"], df[col], label=col, color=color, linestyle=linestyle, linewidth=1.2)

        # 13. Historische Sollwerte
        if "Einschaltpunkt" in df.columns:
            df["Einschaltpunkt"] = pd.to_numeric(df["Einschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Einschaltpunkt"], label="Einschaltpunkt (historisch)", linestyle="--", color="green")

        if "Ausschaltpunkt" in df.columns:
            df["Ausschaltpunkt"] = pd.to_numeric(df["Ausschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Ausschaltpunkt"], label="Ausschaltpunkt (historisch)", linestyle="--", color="orange")


        # 15. Formatierung
        plt.xlim(time_ago, now)
        plt.ylim(y_min, y_max)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf – Letzte {hours} Stunden")
        plt.grid(True, which='both', linestyle='--', linewidth=0.5)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left")
        plt.tight_layout()

        # 16. Bild speichern und senden
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        caption = f"📈 Verlauf {hours}h | T_Oben = blau | T_Unten = rot | T_Mittig = lila | T_Verd = grau gestrichelt"
        form.add_field("caption", caption[:200])
        form.add_field("photo", buf, filename="temperature_graph.png", content_type="image/png")

        async with session.post(url, data=form, timeout=30) as response:
            if response.status == 200:
                logging.info(f"Temperaturdiagramm für {hours}h gesendet.")
            else:
                error_text = await response.text()
                logging.error(f"Fehler beim Senden des Diagramms: {response.status} – {error_text}")
                await send_telegram_message(session, state.chat_id, "Fehler beim Senden des Diagramms.", state.bot_token)

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Temperaturverlaufs: {e}", exc_info=True)
        await send_telegram_message(
            session, state.chat_id,
            f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}", state.bot_token
        )
async def get_runtime_bar_chart(session, days=7, state=None):
    """Erstellt ein Balkendiagramm der Kompressorlaufzeiten nach Energiequelle (nur wenn Kompressor == 'EIN')"""
    if state is None:
        logging.error("State-Objekt nicht übergeben.")
        return

    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        today = now.date()
        start_date = today - timedelta(days=days - 1)

        date_range = [start_date + timedelta(days=i) for i in range(days)]
        runtimes = {
            "PV": [timedelta() for _ in date_range],
            "Battery": [timedelta() for _ in date_range],
            "Grid": [timedelta() for _ in date_range],
            "Unbekannt": [timedelta() for _ in date_range]
        }

        # 1. Vorfilterung: Nur relevante Zeilen laden (ohne gesamte Datei einzulesen)
        file_path = "heizungsdaten.csv"
        if not os.path.isfile(file_path):
            await send_telegram_message(session, state.chat_id, "CSV-Datei nicht gefunden.", state.bot_token)
            return

        logging.debug(f"Lese nur Zeilen aus den letzten {hours} Stunden aus {file_path}")
        relevant_lines = []
        header = None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    if not header:
                        header = line.strip()
                        relevant_lines.append(header)
                        continue
                    try:
                        timestamp_str = line.split(",")[0]
                        timestamp = pd.to_datetime(timestamp_str, errors='coerce')
                        if pd.isna(timestamp):
                            continue
                        timestamp = timestamp.tz_localize(
                            local_tz) if timestamp.tzinfo is None else timestamp.astimezone(local_tz)
                        if time_ago <= timestamp <= now:
                            relevant_lines.append(line.strip())
                    except Exception as e:
                        logging.warning(f"Konnte Zeile nicht verarbeiten: {e}")
        except Exception as e:
            logging.error(f"Fehler beim Lesen der CSV-Zeilen: {e}")
            await send_telegram_message(session, state.chat_id, "Fehler beim Lesen der CSV-Zeilen.", state.bot_token)
            return

        if len(relevant_lines) < 2:
            await send_telegram_message(session, state.chat_id,
                                        f"Keine Daten für die letzten {hours} Stunden vorhanden.", state.bot_token)
            return

        # 2. DataFrame erstellen
        df = pd.DataFrame(relevant_lines[1:], columns=relevant_lines[0].split(","))
        if "Zeitstempel" not in df.columns or "Kompressor" not in df.columns or "PowerSource" not in df.columns:
            raise ValueError("Notwendige Spalten fehlen in der CSV.")

        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors="coerce")
        df = df[df["Zeitstempel"].notna()].copy()
        df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz)
        df["Datum"] = df["Zeitstempel"].dt.date

        df = df[(df["Datum"] >= start_date) & (df["Datum"] <= today)]

        df["Kompressor"] = df["Kompressor"].replace({"EIN": 1, "AUS": 0}).fillna(0).astype(int)

        active_rows = df[df["Kompressor"] == 1]

        if active_rows.empty:
            await send_telegram_message(
                session, state.chat_id,
                f"Keine Laufzeiten mit Kompressor=EIN in den letzten {days} Tagen gefunden.",
                state.bot_token
            )
            return

        power_source_to_category = {
            "Direkter PV-Strom": "PV",
            "Strom aus der Batterie": "Battery",
            "Strom vom Netz": "Grid"
        }

        for _, row in active_rows.iterrows():
            date = row["Datum"]
            source = row["PowerSource"]
            category = power_source_to_category.get(source, "Unbekannt")

            idx = (date - start_date).days
            if 0 <= idx < days:
                runtimes[category][idx] += timedelta(minutes=1)

        runtime_pv_hours = [rt.total_seconds() / 3600 for rt in runtimes['PV']]
        runtime_battery_hours = [rt.total_seconds() / 3600 for rt in runtimes['Battery']]
        runtime_grid_hours = [rt.total_seconds() / 3600 for rt in runtimes['Grid']]
        runtime_unknown_hours = [rt.total_seconds() / 3600 for rt in runtimes['Unbekannt']]

        fig, ax = plt.subplots(figsize=(10, 6))
        bottom_unk = [0] * len(date_range)
        bottom_pv = [u + p for u, p in zip(bottom_unk, runtime_unknown_hours)]
        bottom_bat = [u + p for u, p in zip(bottom_pv, runtime_pv_hours)]

        ax.bar(date_range, runtime_unknown_hours, label="Unbekannt / Kein Eintrag", color="gray", alpha=0.5)
        ax.bar(date_range, runtime_pv_hours, bottom=bottom_unk, label="PV-Strom", color="green")
        ax.bar(date_range, runtime_battery_hours, bottom=bottom_pv, label="Batterie", color="orange")
        ax.bar(date_range, runtime_grid_hours, bottom=bottom_bat, label="Netz", color="red")

        ax.set_title(f"Kompressorlaufzeiten nach Energiequelle (letzte {days} Tage)")
        ax.set_xlabel("Datum")
        ax.set_ylabel("Laufzeit (h)")
        ax.grid(True, axis='y', linestyle='--', alpha=0.3)
        ax.legend(loc="upper left")
        plt.xticks(rotation=45)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        form.add_field("caption", f"📊 Laufzeiten nach Quelle – Letzte {days} Tage")
        form.add_field("photo", buf, filename="runtime_chart.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Laufzeitdiagramm für {days} Tage gesendet.")

        buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Laufzeitdiagramms: {e}", exc_info=True)
        await send_telegram_message(
            session, state.chat_id,
            f"Fehler beim Abrufen der Laufzeiten: {str(e)}", state.bot_token
        )