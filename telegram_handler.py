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

async def send_telegram_message(session, chat_id, message, bot_token):
    """Sendet eine Nachricht über Telegram mit Fehlerbehandlung."""
    if len(message) > 4096:
        message = message[:4093] + "..."
        logging.warning("Nachricht gekürzt, da Telegram-Limit von 4096 Zeichen überschritten.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        async with session.post(url, json=payload, timeout=10) as response:
            if response.status == 200:
                # Zusätzliche Debugging-Ausgabe
                logging.debug(f"INFO-Nachricht wird geloggt: {message[:150]}... (Länge={len(message)})")
                logging.info(f"Telegram-Nachricht gesendet: {message[:100]}...")
                return True
            else:
                error_text = await response.text()
                logging.error(f"Fehler beim Senden der Telegram-Nachricht: Status {response.status}, Details: {error_text}")
                return False
    except aiohttp.ClientError as e:
        logging.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)
        return False
    except asyncio.TimeoutError:
        logging.error("Timeout beim Senden der Telegram-Nachricht", exc_info=True)
        return False
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)
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
            logging.debug(f"HTTP-Status von getUpdates: {response.status}")
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
    logging.debug("Status-Nachricht: Linie 1-2 generiert")
    status_lines.append(f"  • Oben: {'N/A' if t_oben is None else f'{t_oben:.1f}°C'}")
    logging.debug("Status-Nachricht: Linie 3 generiert")
    status_lines.append(f"  • Mittig: {'N/A' if t_mittig is None else f'{t_mittig:.1f}°C'}")
    logging.debug("Status-Nachricht: Linie 4 generiert")
    status_lines.append(f"  • Unten: {'N/A' if t_unten is None else f'{t_unten:.1f}°C'}")
    logging.debug("Status-Nachricht: Linie 5 generiert")
    try:
        status_lines.append(f"  • Verdampfer: {'N/A' if t_verd is None else f'{t_verd:.1f}°C'}")
        logging.debug("Status-Nachricht: Linie 6 (Verdampfer) generiert")
    except Exception as e:
        logging.error(f"Fehler beim Formatieren von Verdampfer: t_verd={t_verd}, Fehler: {e}", exc_info=True)
        status_lines.append("  • Verdampfer: Fehler")
    status_lines.append("🛠️ **Kompressor**")
    logging.debug("Status-Nachricht: Linie 7 generiert")
    status_lines.append(f"  • Status: {compressor_status_str}")
    logging.debug("Status-Nachricht: Linie 8 generiert")
    status_lines.append(f"  • Aktuelle Laufzeit: {format_time(current_runtime)}")
    logging.debug("Status-Nachricht: Linie 9 generiert")
    status_lines.append(f"  • Gesamtlaufzeit heute: {format_time(total_runtime)}")
    logging.debug("Status-Nachricht: Linie 10 generiert")
    status_lines.append(f"  • Letzte Laufzeit: {format_time(state.last_runtime)}")
    logging.debug("Status-Nachricht: Linie 11 generiert")
    status_lines.append("🎯 **Sollwerte**")
    logging.debug("Status-Nachricht: Linie 12 generiert")
    status_lines.append(f"  • Einschaltpunkt: {state.aktueller_einschaltpunkt}°C")
    logging.debug("Status-Nachricht: Linie 13 generiert")
    status_lines.append(f"  • Ausschaltpunkt: {state.aktueller_ausschaltpunkt}°C")
    logging.debug("Status-Nachricht: Linie 14 generiert")
    status_lines.append(f"  • Gilt für: {'Unten' if state.solar_ueberschuss_aktiv else 'Oben, Mitte'}")
    logging.debug("Status-Nachricht: Linie 15 generiert")
    status_lines.append("⚙️ **Betriebsmodus**")
    logging.debug("Status-Nachricht: Linie 16 generiert")
    status_lines.append(f"  • {mode_str}")
    logging.debug("Status-Nachricht: Linie 17 generiert")
    status_lines.append("ℹ️ **Zusatzinfo**")
    logging.debug("Status-Nachricht: Linie 18 generiert")
    status_lines.append(f"  • Solarüberschuss: {feedinpower:.1f} W")
    logging.debug("Status-Nachricht: Linie 19 generiert")
    status_lines.append(f"  • Batterieleistung: {bat_power:.1f} W ({'Laden' if bat_power > 0 else 'Entladung' if bat_power < 0 else 'Neutral'})")
    logging.debug("Status-Nachricht: Linie 20 generiert")
    status_lines.append(f"  • Solarüberschuss aktiv: {'Ja' if state.solar_ueberschuss_aktiv else 'Nein'}")
    logging.debug("Status-Nachricht: Linie 21 generiert")
    if state.ausschluss_grund:
        status_lines.append(f"  • Ausschlussgrund: {state.ausschluss_grund}")
        logging.debug("Status-Nachricht: Linie 22 generiert")

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
        logging.debug("telegram_task Schleife ausgeführt")
        try:
            if not state.bot_token or not state.chat_id:
                logging.warning(f"Telegram bot_token oder chat_id fehlt (bot_token={state.bot_token}, chat_id={state.chat_id}). Überspringe telegram_task.")
                await asyncio.sleep(60)
                continue
            logging.debug("Versuche Telegram-Updates abzurufen")
            updates = await get_telegram_updates(session, state.bot_token, last_update_id)
            if updates is not None:
                logging.debug("Updates erfolgreich empfangen")
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
                logging.debug(f"Rohwerte (vor Verarbeitung): t_oben={t_boiler_oben}, t_unten={t_boiler_unten}, t_mittig={t_boiler_mittig}, t_verd={t_verd}")
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

async def get_boiler_temperature_history(session, hours, state, config):
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)

        expected_columns = [
            "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "Kompressor",
            "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "PowerSource"
        ]

        try:
            with open("heizungsdaten.csv", "r") as f:
                os.fsync(f.fileno())
        except Exception as e:
            logging.warning(f"Fehler bei Dateisynchronisation: {e}")

        try:
            df_header = pd.read_csv("heizungsdaten.csv", nrows=1)
            available_columns = [col for col in expected_columns if col in df_header.columns]
            if not available_columns:
                raise ValueError("Keine der erwarteten Spalten in der CSV gefunden.")
            logging.debug(f"Verfügbare Spalten: {available_columns}")
        except Exception as e:
            logging.error(f"Fehler beim Lesen des Headers: {e}")
            await send_telegram_message(session, state.chat_id, "CSV-Header konnte nicht gelesen werden.", state.bot_token)
            return

        try:
            df = pd.read_csv(
                "heizungsdaten.csv",
                usecols=available_columns,
                on_bad_lines='skip',
                engine='python'
            )
            logging.debug(f"{len(df)} Zeilen aus CSV geladen.")
        except Exception as e:
            logging.error(f"Fehler beim Laden der CSV: {e}")
            await send_telegram_message(session, state.chat_id, "Fehler beim Laden der CSV-Datei.", state.bot_token)
            return

        try:
            df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce', dayfirst=True, format='mixed')
            invalid_rows = df[df["Zeitstempel"].isna()]
            if not invalid_rows.empty:
                invalid_indices = invalid_rows.index.tolist()
                sample = invalid_rows.iloc[0]["Zeitstempel"] if len(invalid_rows) > 0 else "unbekannt"
                logging.warning(f"{len(invalid_rows)} Zeilen mit ungültigen Zeitstempeln übersprungen (z. B. '{sample}', Indizes: {invalid_indices[:10]}...)")
                df = df.dropna(subset=["Zeitstempel"]).reset_index(drop=True)
            df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz)
            logging.debug(f"{len(df)} Zeilen nach Zeitstempelparsing.")
        except Exception as e:
            logging.error(f"Fehler beim Parsen der Zeitstempel: {e}")
            await send_telegram_message(session, state.chat_id, "Fehler beim Verarbeiten der Zeitstempel.", state.bot_token)
            return

        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]
        logging.debug(f"{len(df)} Zeilen nach Zeitfilterung.")

        if df.empty:
            logging.warning(f"Keine Daten im Zeitfenster ({hours}h) gefunden.")
            await send_telegram_message(session, state.chat_id, "Keine Daten für den Verlauf verfügbar.", state.bot_token)
            return

        df = df.copy()
        df["Einschaltpunkt"] = pd.to_numeric(df.get("Einschaltpunkt", pd.Series(42)), errors="coerce").fillna(42)
        df["Ausschaltpunkt"] = pd.to_numeric(df.get("Ausschaltpunkt", pd.Series(45)), errors="coerce").fillna(45)
        df["Solarüberschuss"] = pd.to_numeric(df.get("Solarüberschuss", pd.Series(0)), errors="coerce").fillna(0).astype(int)
        df["PowerSource"] = df.get("PowerSource", pd.Series("Unbekannt")).fillna("Unbekannt").replace(["N/A", "Fehler"], "Unbekannt")
        df["Kompressor"] = df.get("Kompressor", pd.Series(0)).replace({"EIN": 1, "AUS": 0}).fillna(0).astype(int)

        temp_columns = [c for c in ["T_Oben", "T_Unten", "T_Mittig"] if c in df.columns]
        for col in temp_columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        if not df.empty:
            gap_threshold = timedelta(minutes=5)
            gaps = df["Zeitstempel"].diff()[1:] > gap_threshold
            gap_indices = gaps[gaps].index
            if not gap_indices.empty:
                synthetic_rows = []
                for idx in gap_indices:
                    prev_time = df.loc[idx - 1, "Zeitstempel"]
                    next_time = df.loc[idx, "Zeitstempel"]
                    synthetic_time = prev_time + timedelta(minutes=1)
                    synthetic_row = {
                        "Zeitstempel": synthetic_time,
                        "Kompressor": 0,
                        "PowerSource": "Keine aktive Energiequelle",
                        "Einschaltpunkt": df.loc[idx - 1, "Einschaltpunkt"],
                        "Ausschaltpunkt": df.loc[idx - 1, "Ausschaltpunkt"],
                        "Solarüberschuss": 0
                    }
                    for col in ["T_Oben", "T_Unten", "T_Mittig"]:
                        synthetic_row[col] = np.nan
                    synthetic_rows.append(synthetic_row)

                if synthetic_rows:
                    synthetic_df = pd.DataFrame(synthetic_rows)
                    df = pd.concat([df, synthetic_df], ignore_index=True)
                    df = df.sort_values("Zeitstempel").reset_index(drop=True)
                    logging.info(f"{len(synthetic_rows)} synthetische Punkte zur Lückenbehandlung hinzugefügt.")

        target_points = 50
        if len(df) > target_points:
            df = df.iloc[::len(df) // target_points].head(target_points)
        logging.debug(f"{len(df)} Zeilen nach Downscaling.")

        timestamps = df["Zeitstempel"]
        t_oben = df.get("T_Oben")
        t_unten = df.get("T_Unten")
        t_mittig = df.get("T_Mittig")
        einschaltpunkte = df["Einschaltpunkt"]
        ausschaltpunkte = df["Ausschaltpunkt"]
        kompressor_status = df["Kompressor"]
        power_sources = df["PowerSource"]
        solar_ueberschuss = df["Solarüberschuss"]

        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "Keine aktive Energiequelle": "blue",
            "Unbekannt": "gray"
        }

        untere_grenze = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MIN", 20))
        obere_grenze = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 55))

        plt.figure(figsize=(12, 6))

        shown_labels = set()

        for source, color in color_map.items():
            mask = (power_sources == source) & (kompressor_status == 1)
            if mask.any():
                label = f"Kompressor EIN ({source})"
                plt.fill_between(
                    timestamps[mask],
                    0, max(untere_grenze, obere_grenze) + 5,
                    color=color,
                    alpha=0.3,
                    label=label
                )
                shown_labels.add(label)

        if t_oben is not None:
            plt.plot(timestamps, t_oben, label="T_Oben", marker="o", color="blue")
        if t_unten is not None:
            plt.plot(timestamps, t_unten, label="T_Unten", marker="x", color="red")
        if t_mittig is not None:
            plt.plot(timestamps, t_mittig, label="T_Mittig", marker="^", color="purple")

        plt.plot(timestamps, einschaltpunkte, label="Einschaltpunkt (historisch)", linestyle="--", color="green")
        plt.plot(timestamps, ausschaltpunkte, label="Ausschaltpunkt (historisch)", linestyle="--", color="orange")

        if solar_ueberschuss.any():
            plt.axhline(y=state.aktueller_einschaltpunkt, color="purple", linestyle="-.",
                        label=f"Einschaltpunkt ({state.aktueller_einschaltpunkt}°C)")
            plt.axhline(y=state.aktueller_ausschaltpunkt, color="cyan", linestyle="-.",
                        label=f"Ausschaltpunkt ({state.aktueller_ausschaltpunkt}°C)")

        plt.xlim(time_ago, now)
        plt.ylim(0, max(untere_grenze, obere_grenze) + 5)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf (letzte {hours} Stunden)")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left")
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        form.add_field("caption", f"📈 Verlauf {hours}h (T_Oben = blau, T_Unten = rot, T_Mittig = lila)")
        form.add_field("photo", buf, filename="temperature_graph.png", content_type="image/png")

        async with session.post(url, data=form) as response:
            response.raise_for_status()
            logging.info(f"Temperaturdiagramm für {hours}h gesendet.")
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

        df = pd.read_csv("heizungsdaten.csv", on_bad_lines="skip", engine="python")
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