import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta


async def send_telegram_message(session, chat_id, message, bot_token):
    """Sendet eine Nachricht über Telegram."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    async with session.post(url, json=payload) as response:
        if response.status == 200:
            logging.info(f"Telegram-Nachricht gesendet: {message}")
        else:
            logging.error(f"Fehler beim Senden der Telegram-Nachricht: {response.status}")


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
    async with session.post(url, json=payload) as response:
        response.raise_for_status()
        logging.info("Willkommensnachricht mit Keyboard gesendet.")


async def get_telegram_updates(session, bot_token, offset=None):
    """Ruft Telegram-Updates ab."""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 60}  # Standard-Timeout immer setzen
    if offset is not None:    # Nur offset hinzufügen, wenn er nicht None ist
        params["offset"] = offset
    async with session.get(url, params=params) as response:
        if response.status == 200:
            data = await response.json()
            return data.get("result", [])
        else:
            logging.error(f"Fehler beim Abrufen von Telegram-Updates: {response.status}")
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


async def send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, chat_id,
                                    bot_token):
    """Sendet die aktuellen Temperaturen über Telegram."""
    t_oben_str = f"{t_boiler_oben:.1f}°C" if t_boiler_oben is not None else "N/A"
    t_unten_str = f"{t_boiler_unten:.1f}°C" if t_boiler_unten is not None else "N/A"
    t_mittig_str = f"{t_boiler_mittig:.1f}°C" if t_boiler_mittig is not None else "N/A"
    t_verd_str = f"{t_verd:.1f}°C" if t_verd is not None else "N/A"
    message = (
        f"🌡️ Aktuelle Temperaturen:\n"
        f"Boiler oben: {t_oben_str}\n"
        f"Boiler mittig: {t_mittig_str}\n"
        f"Boiler unten: {t_unten_str}\n"
        f"Verdampfer: {t_verd_str}"
    )
    await send_telegram_message(session, chat_id, message, bot_token)


async def send_status_telegram(session, t_oben, t_unten, t_mittig, t_verd, kompressor_status, current_runtime, total_runtime, config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func=None):
    """Sendet den aktuellen Systemstatus über Telegram."""
    solax_data = await get_solax_data_func(session, state) or {
        "feedinpower": 0, "batPower": 0, "soc": 0, "api_fehler": True
    }
    feedinpower = solax_data.get("feedinpower", 0)
    bat_power = solax_data.get("batPower", 0)

    # Laufzeiten in Stunden und Minuten umwandeln
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

    # Temperaturen auf Fehler prüfen und formatieren
    t_oben_str = f"{t_oben:.1f}°C" if t_oben is not None else "N/A"
    t_unten_str = f"{t_unten:.1f}°C" if t_unten is not None else "N/A"
    t_mittig_str = f"{t_mittig:.1f}°C" if t_mittig is not None else "N/A"
    t_verd_str = f"{t_verd:.1f}°C" if t_verd is not None else "N/A"

    # Betriebsmodus bestimmen und Sollwertänderungen anzeigen
    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_nighttime_func and is_nighttime_func(config) else 0
    if state.urlaubsmodus_aktiv:
        mode_str = "Urlaub"
    elif state.solar_ueberschuss_aktiv and is_nighttime_func and is_nighttime_func(config):
        mode_str = f"Solarüberschuss + Nachtabsenkung (-{nacht_reduction}°C)"
    elif state.solar_ueberschuss_aktiv:
        mode_str = "Solarüberschuss"
    elif is_nighttime_func and is_nighttime_func(config):
        mode_str = f"Nachtabsenkung (-{nacht_reduction}°C)"
    else:
        mode_str = "Normal"

    # Verwende kompressor_status direkt
    compressor_status_str = "EIN" if kompressor_status else "AUS"

    message = (
        "📊 **Systemstatus**\n"
        "🌡️ **Temperaturen**\n"
        f"  • Oben: {t_oben_str}\n"
        f"  • Mittig: {t_mittig_str}\n"
        f"  • Unten: {t_unten_str}\n"
        f"  • Verdampfer: {t_verd_str}\n"
        "🛠️ **Kompressor**\n"
        f"  • Status: {compressor_status_str}\n"
        f"  • Aktuelle Laufzeit: {current_runtime}\n"
        f"  • Gesamtlaufzeit heute: {total_runtime}\n"
        f"  • Letzte Laufzeit: {format_time(state.last_runtime)}\n"
        "🎯 **Sollwerte**\n"
        f"  • Einschaltpunkt: {state.aktueller_einschaltpunkt}°C\n"
        f"  • Ausschaltpunkt: {state.aktueller_ausschaltpunkt}°C\n"
        f"  • Gilt für: {'Oben, Mitte, Unten' if state.solar_ueberschuss_aktiv else 'Oben, Mitte'}\n"
        "⚙️ **Betriebsmodus**\n"
        f"  • {mode_str}\n"
        "ℹ️ **Zusatzinfo**\n"
        f"  • Solarüberschuss: {feedinpower:.1f} W\n"
        f"  • Batterieleistung: {bat_power:.1f} W ({'Laden' if bat_power > 0 else 'Entladung' if bat_power < 0 else 'Neutral'})\n"
        f"  • Solarüberschuss aktiv: {'Ja' if state.solar_ueberschuss_aktiv else 'Nein'}\n"
    )
    # Ausschlussgrund nur hinzufügen, wenn vorhanden
    if state.ausschluss_grund:
        message += f"\n  • Ausschlussgrund: {state.ausschluss_grund}"

    await send_telegram_message(session, chat_id, message, bot_token)


async def send_unknown_command_message(session, chat_id, bot_token):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    await send_telegram_message(session, chat_id, "❓ Unbekannter Befehl. Verwende 'Hilfe' für eine Liste der Befehle.",
                                bot_token)


async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates,
                                         last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                                         chat_id, bot_token, config, get_solax_data_func, state,
                                         get_temperature_history_func, get_runtime_bar_chart_func,
                                         is_nighttime_func):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    try:
        if updates:
            for update in updates:
                message_text = update.get('message', {}).get('text')
                chat_id_from_update = update.get('message', {}).get('chat', {}).get('id')
                if message_text and chat_id_from_update:
                    message_text = message_text.strip()
                    logging.debug(f"Empfangener Telegram-Befehl: '{message_text}'")
                    message_text_lower = message_text.lower()
                    if message_text_lower == "🌡️ temperaturen" or message_text_lower == "temperaturen":
                        if all(x is not None for x in [t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd]):
                            await send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig,
                                                           t_verd, chat_id, bot_token)
                        else:
                            await send_telegram_message(session, chat_id, "Fehler beim Abrufen der Temperaturen.",
                                                       bot_token)
                    elif message_text_lower == "📊 status" or message_text_lower == "status":
                        if all(x is not None for x in [t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd]):
                            await send_status_telegram(
                                session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd,
                                kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                                config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func
                            )
                        else:
                            await send_telegram_message(session, chat_id, "Fehler beim Abrufen des Status.", bot_token)
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
        return last_update_id
    except Exception as e:
        logging.error(f"Fehler in process_telegram_messages_async: {e}", exc_info=True)
        return last_update_id


async def telegram_task(session, bot_token, chat_id, read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func):
    """Verarbeitet eingehende Telegram-Nachrichten und führt entsprechende Aktionen aus."""
    last_update_id = None
    max_retries = 3
    while True:
        for attempt in range(max_retries):
            try:
                updates = await get_telegram_updates(session, bot_token, last_update_id)
                if updates is not None:
                    t_boiler_oben = await asyncio.to_thread(read_temperature_func, sensor_ids["oben"])
                    t_boiler_unten = await asyncio.to_thread(read_temperature_func, sensor_ids["unten"])
                    t_boiler_mittig = await asyncio.to_thread(read_temperature_func, sensor_ids["mittig"])
                    t_verd = await asyncio.to_thread(read_temperature_func, sensor_ids["verd"])

                    # Rufe die Funktionen auf, um die aktuellen Werte zu erhalten
                    kompressor_status = kompressor_status_func()
                    aktuelle_laufzeit = current_runtime_func()
                    gesamtlaufzeit = total_runtime_func()

                    last_update_id = await process_telegram_messages_async(
                        session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates, last_update_id,
                        kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, chat_id, bot_token, config,
                        get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func,
                        is_nighttime_func)
                    break
                else:
                    logging.warning(f"Telegram-Updates waren None, Versuch {attempt + 1}/{max_retries}")
            except Exception as e:
                logging.error(f"Fehler in telegram_task (Versuch {attempt + 1}/{max_retries}): {e}", exc_info=True)
                if attempt < max_retries - 1:
                    await asyncio.sleep(10)
                else:
                    logging.error("Maximale Wiederholungen erreicht, warte 5 Minuten")
                    await asyncio.sleep(300)
        await asyncio.sleep(0.1)


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