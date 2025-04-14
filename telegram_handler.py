import aiohttp
import asyncio
import logging
from telegram import ReplyKeyboardMarkup
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
    """Sendet eine Willkommensnachricht mit Tastatur."""
    keyboard = [
        ["🌡️ Temperaturen", "📊 Status"],
        ["🌴 Urlaub", "🏠 Urlaub aus"],
        ["📈 Verlauf 6h", "📉 Verlauf 24h"],
        ["⏱️ Laufzeiten", "🆘 Hilfe"]
    ]
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": "🤖 Willkommen beim Heizungssteuerungs-Bot!\n\nVerwende die Tastatur, um Befehle auszuwählen.",
        "reply_markup": {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
    }
    async with session.post(url, json=payload) as response:
        if response.status == 200:
            logging.info("Willkommensnachricht gesendet")
        else:
            logging.error(f"Fehler beim Senden der Willkommensnachricht: {response.status}")


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
    """Aktiviert den Urlaubsmodus und passt die Sollwerte an."""
    state.urlaubsmodus_aktiv = True
    urlaubsabsenkung = int(config["Urlaubsmodus"].get("URLAUBSABSENKUNG", 6))
    state.aktueller_ausschaltpunkt -= urlaubsabsenkung
    state.aktueller_einschaltpunkt -= urlaubsabsenkung
    await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert (-{urlaubsabsenkung}°C).", bot_token)
    logging.info("Urlaubsmodus aktiviert")


async def deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state):
    """Deaktiviert den Urlaubsmodus und stellt die ursprünglichen Sollwerte wieder her."""
    state.urlaubsmodus_aktiv = False
    ausschaltpunkt = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT", 45))
    temp_offset = int(config["Heizungssteuerung"].get("TEMP_OFFSET", 3))
    state.aktueller_ausschaltpunkt = ausschaltpunkt
    state.aktueller_einschaltpunkt = ausschaltpunkt - temp_offset
    await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus deaktiviert.", bot_token)
    logging.info("Urlaubsmodus deaktiviert")


async def send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, chat_id,
                                    bot_token):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = (
        f"🌡️ Aktuelle Temperaturen:\n"
        f"Boiler oben: {t_boiler_oben:.1f}°C\n"
        f"Boiler hinten: {t_boiler_hinten:.1f}°C\n"
        f"Boiler mittig: {t_boiler_mittig:.1f}°C\n"
        f"Verdampfer: {t_verd:.1f}°C"
    )
    await send_telegram_message(session, chat_id, message, bot_token)


# telegram_handler.py (angepasste send_status_telegram)
async def send_status_telegram(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, kompressor_status,
                              aktuelle_laufzeit, gesamtlaufzeit, aktueller_einschaltpunkt, aktueller_ausschaltpunkt,
                              chat_id, bot_token, config, get_solax_data_func, urlaubsmodus_aktiv, solar_ueberschuss_aktiv,
                              last_runtime, is_nighttime_func, ausschluss_grund):
    """Sendet den aktuellen Status über Telegram in einem übersichtlichen Format."""
    solax_data = await get_solax_data_func(session)
    is_night = is_nighttime_func(config)

    # Laufzeiten in Stunden und Minuten umwandeln
    def format_time(seconds_str):
        try:
            seconds = int(seconds_str)
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}min"
        except (ValueError, TypeError):
            return str(seconds_str)

    # Temperaturen auf Fehler prüfen und formatieren
    t_oben_str = f"{t_boiler_oben:.1f}°C" if t_boiler_oben is not None else "N/A"
    t_hinten_str = f"{t_boiler_hinten:.1f}°C" if t_boiler_hinten is not None else "N/A"
    t_mittig_str = f"{t_boiler_mittig:.1f}°C" if t_boiler_mittig is not None else "N/A"
    t_verd_str = f"{t_verd:.1f}°C" if t_verd is not None else "N/A"

    # Betriebsmodus bestimmen und Sollwertänderungen anzeigen
    nacht_reduction = int(config["Heizungssteuerung"].get("NACHTABSENKUNG", 0)) if is_night else 0
    if urlaubsmodus_aktiv:
        mode_str = "Urlaub"
    elif solar_ueberschuss_aktiv and is_night:
        mode_str = f"Solarüberschuss + Nachtabsenkung (-{nacht_reduction}°C)"
    elif solar_ueberschuss_aktiv:
        mode_str = "Solarüberschuss"
    elif is_night:
        mode_str = f"Nachtabsenkung (-{nacht_reduction}°C)"
    else:
        mode_str = "Normal"

    # Kompressor-Status mit "Laufzeit zu kurz" prüfen
    MIN_RUNTIME_SECONDS = 300  # 5 Minuten als Schwelle, anpassbar
    try:
        current_seconds = int(aktuelle_laufzeit)
        if kompressor_status and current_seconds < MIN_RUNTIME_SECONDS:
            compressor_status_str = f"EIN (Laufzeit zu kurz: {format_time(aktuelle_laufzeit)})"
        else:
            compressor_status_str = "EIN" if kompressor_status else "AUS"
    except (ValueError, TypeError):
        compressor_status_str = "EIN" if kompressor_status else "AUS"

    # Nachricht zusammenstellen
    message = (
        "📊 **Systemstatus**\n\n"
        "🌡️ **Temperaturen**\n"
        f"  • Oben: {t_oben_str}\n"
        f"  • Hinten: {t_hinten_str}\n"
        f"  • Mittig: {t_mittig_str}\n"
        f"  • Verdampfer: {t_verd_str}\n\n"
        "🛠️ **Kompressor**\n"
        f"  • Status: {compressor_status_str}\n"
        f"  • Aktuelle Laufzeit: {format_time(aktuelle_laufzeit)}\n"
        f"  • Gesamtlaufzeit heute: {format_time(gesamtlaufzeit)}\n"
        f"  • Letzte Laufzeit: {format_time(last_runtime)}\n\n"
        "🎯 **Sollwerte**\n"
        f"  • Einschaltpunkt: {aktueller_einschaltpunkt}°C\n"
        f"  • Ausschaltpunkt: {aktueller_ausschaltpunkt}°C\n\n"
        "⚙️ **Betriebsmodus**\n"
        f"  • {mode_str}\n\n"
        "ℹ️ **Zusatzinfo**\n"
        f"  • Solax AC Power: {solax_data.get('acpower', 'N/A')}W"
    )

    # Ausschlussgrund nur hinzufügen, wenn vorhanden
    if ausschluss_grund:
        message += f"\n  • Ausschlussgrund: {ausschluss_grund}"

    await send_telegram_message(session, chat_id, message, bot_token)


async def send_unknown_command_message(session, chat_id, bot_token):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    await send_telegram_message(session, chat_id, "❓ Unbekannter Befehl. Verwende 'Hilfe' für eine Liste der Befehle.",
                                bot_token)


async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates,
                                         last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                                         chat_id, bot_token, config, get_solax_data_func, state,
                                         get_boiler_temperature_history_func, get_runtime_bar_chart_func,
                                         is_nighttime_func):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    try:
        if updates:
            for update in updates:
                message_text = update.get('message', {}).get('text')
                chat_id_from_update = update.get('message', {}).get('chat', {}).get('id')
                if message_text and chat_id_from_update:
                    message_text = message_text.strip().lower()
                    logging.debug(f"Telegram-Nachricht empfangen: Text={message_text}, Chat-ID={chat_id_from_update}")

                    if message_text == "🌡️ temperaturen" or message_text == "temperaturen":
                        if all(x is not None for x in [t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd]):
                            await send_temperature_telegram(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig,
                                                            t_verd, chat_id, bot_token)
                        else:
                            await send_telegram_message(session, chat_id, "Fehler beim Abrufen der Temperaturen.",
                                                        bot_token)
                    elif message_text == "📊 status" or message_text == "status":
                        if all(x is not None for x in [t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd]):
                            await send_status_telegram(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd,
                                                       kompressor_status, aktuelle_laufzeit, gesamtlaufzeit,
                                                       state.aktueller_einschaltpunkt, state.aktueller_ausschaltpunkt,
                                                       chat_id, bot_token,
                                                       config, get_solax_data_func, state.urlaubsmodus_aktiv,
                                                       state.solar_ueberschuss_aktiv,
                                                       state.last_runtime, is_nighttime_func,
                                                       state.ausschluss_grund)  # Funktion übergeben
                        else:
                            await send_telegram_message(session, chat_id, "Fehler beim Abrufen des Status.", bot_token)
                    elif message_text == "🆘 hilfe" or message_text == "hilfe":
                        await send_help_message(session, chat_id, bot_token)
                    elif message_text == "🌴 urlaub" or message_text == "urlaub":
                        if state.urlaubsmodus_aktiv:
                            await send_telegram_message(session, chat_id, "🌴 Urlaubsmodus ist bereits aktiviert.",
                                                        bot_token)
                        else:
                            await aktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
                    elif message_text == "🏠 urlaub aus" or message_text == "urlaub aus":
                        if not state.urlaubsmodus_aktiv:
                            await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus ist bereits deaktiviert.",
                                                        bot_token)
                        else:
                            await deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
                    elif message_text == "📈 verlauf 6h" or message_text == "verlauf 6h":
                        await get_boiler_temperature_history_func(session, 6, state)
                    elif message_text == "📉 verlauf 24h" or message_text == "verlauf 24h":
                        await get_boiler_temperature_history_func(session, 24, state)
                    elif message_text.startswith("⏱️ laufzeiten") or message_text.startswith("laufzeiten"):
                        parts = message_text.split()
                        days = 7
                        if len(parts) > 1:
                            try:
                                days = int(parts[1])
                                if days < 1 or days > 900:
                                    days = 7
                                    logging.warning(f"Ungültige Tagesanzahl '{parts[1]}', verwende Standardwert 7.")
                            except ValueError:
                                days = 7
                                logging.warning(f"Ungültige Zahl '{parts[1]}', verwende Standardwert 7.")
                        await get_runtime_bar_chart_func(session, days=days)
                    else:
                        await send_unknown_command_message(session, chat_id, bot_token)
                    return update['update_id'] + 1
        return last_update_id
    except Exception as e:
        logging.error(f"Fehler in process_telegram_messages_async: {e}", exc_info=True)
        return last_update_id


async def telegram_task(session, bot_token, chat_id, read_temperature_func, sensor_ids, kompressor_status,
                        aktuelle_laufzeit, gesamtlaufzeit, config, get_solax_data_func, state,
                        get_boiler_temperature_history_func, get_runtime_bar_chart_func,
                        is_nighttime_func):
    last_update_id = None
    max_retries = 3
    while True:
        for attempt in range(max_retries):
            try:
                updates = await get_telegram_updates(session, bot_token, last_update_id)
                if updates is not None:
                    t_boiler_oben = await asyncio.to_thread(read_temperature_func, sensor_ids["oben"])
                    t_boiler_hinten = await asyncio.to_thread(read_temperature_func, sensor_ids["hinten"])
                    t_boiler_mittig = await asyncio.to_thread(read_temperature_func, sensor_ids["mittig"])
                    t_verd = await asyncio.to_thread(read_temperature_func, sensor_ids["verd"])

                    last_update_id = await process_telegram_messages_async(
                        session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates, last_update_id,
                        kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, chat_id, bot_token, config,
                        get_solax_data_func, state, get_boiler_temperature_history_func, get_runtime_bar_chart_func,
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