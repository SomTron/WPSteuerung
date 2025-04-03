import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import ReplyKeyboardMarkup
from config_loader import BOT_TOKEN, CHAT_ID


# Logging einrichten
logging.basicConfig(
    filename="heizungssteuerung.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

def format_timedelta(td):
    """Formatiert eine timedelta in HH:MM:SS."""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# Funktion für die benutzerdefinierte Telegram-Tastatur
def get_custom_keyboard():
    """Erstellt eine benutzerdefinierte Tastatur mit verfügbaren Befehlen."""
    keyboard = [
        ["🌡️ Temperaturen", "📊 Status"],
        ["📈 Verlauf 6h", "📉 Verlauf 24h"],
        ["🌴 Urlaub", "🏠 Urlaub aus"],
        ["🆘 Hilfe", "⏱️ Laufzeiten"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

async def send_telegram_message(session, chat_id, message, reply_markup=None, parse_mode=None):
    """Sendet eine Nachricht über die Telegram-API."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        if reply_markup:
            data["reply_markup"] = reply_markup.to_json()
        if parse_mode:
            data["parse_mode"] = parse_mode
        async with session.post(url, json=data) as response:
            response.raise_for_status()
            logging.info(f"Telegram-Nachricht gesendet: {message}")
            return True
    except aiohttp.ClientError as e:
        logging.error(f"Fehler beim Senden der Telegram-Nachricht: {e}, Nachricht={message}")
        return False

async def get_telegram_updates(session, offset=None):
    """Abrufen von Telegram-Updates."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": offset, "timeout": 20} if offset else {"timeout": 20}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=35)) as response:
            response.raise_for_status()
            updates = await response.json()
            logging.debug(f"Telegram-Updates empfangen: {updates}")
            return updates.get('result', [])
    except aiohttp.ClientError as e:
        logging.error(f"Fehler bei der Telegram-API-Abfrage: {e}")
        return None

async def send_welcome_message(session, chat_id):
    """Sendet eine Willkommensnachricht mit Tastatur."""
    message = (
        "🤖 Willkommen beim Heizungssteuerungs-Bot!\n\n"
        "Verwende die Tastatur, um Befehle auszuwählen."
    )
    return await send_telegram_message(session, chat_id, message, reply_markup=get_custom_keyboard())

async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates, last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, letzte_laufzeit, solar_ueberschuss_aktiv, aktueller_einschaltpunkt, aktueller_ausschaltpunkt, is_nighttime, config, set_kompressor_status, urlaubsmodus_aktiv):
    """Verarbeitet eingehende Telegram-Nachrichten und führt entsprechende Aktionen aus."""
    if updates:
        for update in updates:
            message_text = update.get('message', {}).get('text')
            chat_id = update.get('message', {}).get('chat', {}).get('id')
            if message_text and chat_id:
                message_text = message_text.strip().lower()
                logging.debug(f"Empfangene Nachricht: '{message_text}'")

                if message_text == "📊 status" or message_text == "status":
                    mode = "PV-Überschuss" if solar_ueberschuss_aktiv else ("Nacht" if is_nighttime(config) else "Normal")
                    formatted_aktuelle_laufzeit = format_timedelta(timedelta(seconds=0) if not kompressor_status else aktuelle_laufzeit)
                    formatted_gesamtlaufzeit = format_timedelta(gesamtlaufzeit)
                    formatted_letzte_laufzeit = format_timedelta(letzte_laufzeit)

                    status_msg = (
                        f"📊 Status\n"
                        f"Modus: {mode}\n\n"
                        f"🔧 Kompressor: {'🟢 EIN' if kompressor_status else '🔴 AUS'}\n"
                        f"🌡️ Temperaturen:\n"
                        f"  - Oben: {t_boiler_oben:.1f}°C\n"
                        f"  - Mitte: {t_boiler_mittig:.1f}°C\n"
                        f"  - Hinten: {t_boiler_hinten:.1f}°C\n"
                        f"  - Verdampfer: {t_verd:.1f}°C\n\n"
                        f"⚙️ Regelung:\n"
                    )
                    if solar_ueberschuss_aktiv:
                        status_msg += (
                            f"  - 🟢 Einschalten: Ein Fühler < {config['Heizungssteuerung']['EINSCHALTPUNKT']}°C\n"
                            f"  - 🔴 Ausschalten: Ein Fühler ≥ {config['Heizungssteuerung']['AUSSCHALTPUNKT_ERHOEHT']}°C\n"
                        )
                    else:
                        status_msg += (
                            f"  - 🟢 Einschalten: Oben < {aktueller_einschaltpunkt}°C oder Mitte < {aktueller_einschaltpunkt}°C\n"
                            f"  - 🔴 Ausschalten: Oben ≥ {aktueller_ausschaltpunkt}°C und Mitte ≥ {aktueller_ausschaltpunkt}°C\n"
                        )
                    status_msg += (
                        f"\n⏱️ Laufzeiten:\n"
                        f"  - Aktuell: {formatted_aktuelle_laufzeit}\n"
                        f"  - Heute: {formatted_gesamtlaufzeit}\n"
                        f"  - Letzte: {formatted_letzte_laufzeit}"
                    )
                    await send_telegram_message(session, chat_id, status_msg)

                # Weitere Befehle (z. B. Verlauf, Urlaubsmodus) hier hinzufügen, falls benötigt
                elif message_text == "🔛 manuell ein" or message_text == "manuell ein":
                    if not kompressor_status:
                        await asyncio.to_thread(set_kompressor_status, True)
                        await send_telegram_message(session, chat_id, "✅ Kompressor manuell eingeschaltet.")
                    else:
                        await send_telegram_message(session, chat_id, "ℹ️ Kompressor läuft bereits.")
                elif message_text == "🔴 manuell aus" or message_text == "manuell aus":
                    if kompressor_status:
                        await asyncio.to_thread(set_kompressor_status, False, force_off=True)
                        await send_telegram_message(session, chat_id, "✅ Kompressor manuell ausgeschaltet.")
                    else:
                        await send_telegram_message(session, chat_id, "ℹ️ Kompressor ist bereits aus.")

                last_update_id = update['update_id'] + 1
        return last_update_id  # Rückgabe des aktualisierten Werts
    return last_update_id  # Rückgabe, wenn keine Updates

async def telegram_task(session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, kompressor_ein, current_runtime, total_runtime_today, last_runtime, solar_ueberschuss_aktiv, aktueller_einschaltpunkt, aktueller_ausschaltpunkt, is_nighttime, config, set_kompressor_status, urlaubsmodus_aktiv, last_update_id):
    """Task zum kontinuierlichen Abrufen und Verarbeiten von Telegram-Nachrichten."""
    while True:
        try:
            updates = await get_telegram_updates(session, last_update_id)
            if updates:
                last_update_id = await process_telegram_messages_async(
                    session, t_boiler_oben, t_boiler_hinten, t_boiler_mittig, t_verd, updates, last_update_id,
                    kompressor_ein, current_runtime, total_runtime_today, last_runtime,
                    solar_ueberschuss_aktiv, aktueller_einschaltpunkt, aktueller_ausschaltpunkt,
                    is_nighttime, config, set_kompressor_status, urlaubsmodus_aktiv
                )
        except Exception as e:
            logging.error(f"Fehler in telegram_task: {e}", exc_info=True)
        await asyncio.sleep(2)
    return last_update_id  # Optional, falls Task beendet wird