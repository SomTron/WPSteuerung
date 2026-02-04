import aiohttp
import asyncio
import logging
import pytz
import os
import io
import pandas as pd
from datetime import datetime, timedelta
from utils import safe_timedelta

# New Modules
from telegram_api import (
    create_robust_aiohttp_session, 
    send_telegram_message, 
    get_telegram_updates,
    start_healthcheck_task
)
from telegram_ui import (
    get_keyboard, 
    send_welcome_message, 
    escape_markdown, 
    format_time, 
    fmt_temp, 
    send_help_message, 
    send_unknown_command_message
)
from telegram_charts import (
    get_boiler_temperature_history, 
    get_runtime_bar_chart
)

async def aktivere_bademodus(session, chat_id, bot_token, state):
    """Aktiviert den Bademodus."""
    state.bademodus_aktiv = True
    keyboard = get_keyboard(state)
    message = "🛁 Bademodus aktiviert. Kompressor steuert nach erhöhtem Sollwert (untere Temperatur)."
    logging.info("Bademodus aktiviert")
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

async def deaktivere_bademodus(session, chat_id, bot_token, state):
    """Deaktiviert den Bademodus."""
    state.bademodus_aktiv = False
    keyboard = get_keyboard(state)
    message = "🛁 Bademodus deaktiviert."
    logging.info("Bademodus deaktiviert")
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

async def aktivere_urlaubsmodus(session, chat_id, bot_token, config, state):
    """Aktiviert den Urlaubsmodus mit Zeitauswahl."""
    time_keyboard = {
        "keyboard": [
            ["🌴 1 Tag", "🌴 3 Tage", "🌴 7 Tage"],
            ["🌴 14 Tage", "🌴 Benutzerdefiniert", "❌ Abbrechen"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }
    await send_telegram_message(session, chat_id, "🌴 Wähle die Dauer des Urlaubsmodus:", bot_token, reply_markup=time_keyboard)
    state.awaiting_urlaub_duration = True

async def set_urlaubsmodus_duration(session, chat_id, bot_token, config, state, duration_text):
    """Setzt die Urlaubsmodus-Dauer basierend auf der Auswahl."""
    try:
        if duration_text == "❌ Abbrechen":
            keyboard = get_keyboard(state)
            await send_telegram_message(session, chat_id, "❌ Urlaubsmodus-Aktivierung abgebrochen.", bot_token, reply_markup=keyboard)
            state.awaiting_urlaub_duration = False
            return

        if duration_text == "🌴 1 Tag": duration_days = 1
        elif duration_text == "🌴 3 Tage": duration_days = 3
        elif duration_text == "🌴 7 Tage": duration_days = 7
        elif duration_text == "🌴 14 Tage": duration_days = 14
        elif duration_text == "🌴 Benutzerdefiniert":
            keyboard = get_keyboard(state)
            await send_telegram_message(session, chat_id, "📅 Bitte sende die Anzahl der Tage (z.B. '5' für 5 Tage):", bot_token, reply_markup=keyboard)
            state.awaiting_custom_duration = True
            state.awaiting_urlaub_duration = False
            return
        else:
            try: duration_days = int(duration_text.replace("🌴 ", "").replace(" Tage", "").strip())
            except ValueError:
                keyboard = get_keyboard(state)
                await send_telegram_message(session, chat_id, "❌ Ungültige Eingabe.", bot_token, reply_markup=keyboard)
                state.awaiting_urlaub_duration = False
                return

        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        state.urlaubsmodus_aktiv = True
        state.urlaubsmodus_start = now
        state.urlaubsmodus_ende = now + timedelta(days=duration_days)

        urlaubsabsenkung = int(config.Urlaubsmodus.URLAUBSABSENKUNG)
        keyboard = get_keyboard(state)
        await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert für {duration_days} Tage (-{urlaubsabsenkung}°C).", bot_token, reply_markup=keyboard)
        state.awaiting_urlaub_duration = False
        state.awaiting_custom_duration = False
    except Exception as e:
        logging.error(f"Fehler bei Urlaubsdauer: {e}")

async def handle_custom_duration(session, chat_id, bot_token, config, state, message_text):
    """Behandelt benutzerdefinierte Dauer-Eingabe."""
    try:
        if message_text in ["🌴 Benutzerdefiniert", "❌ Abbrechen", "🌴 1 Tag", "🌴 3 Tage", "🌴 7 Tage", "🌴 14 Tage"]: return
        duration_days = int(message_text.strip())
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        state.urlaubsmodus_aktiv = True
        state.urlaubsmodus_start = now
        state.urlaubsmodus_ende = now + timedelta(days=duration_days)
        keyboard = get_keyboard(state)
        await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert für {duration_days} Tage.", bot_token, reply_markup=keyboard)
        state.awaiting_urlaub_duration = False
        state.awaiting_custom_duration = False
    except: pass

async def deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state):
    """Deaktiviert den Urlaubsmodus."""
    state.urlaubsmodus_aktiv = False
    keyboard = get_keyboard(state)
    await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus deaktiviert.", bot_token, reply_markup=keyboard)

async def send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, chat_id, bot_token, state):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = f"🌡️ Aktuelle Temperaturen:\nBoiler oben: {fmt_temp(t_boiler_oben)}\nBoiler mittig: {fmt_temp(t_boiler_mittig)}\nBoiler unten: {fmt_temp(t_boiler_unten)}\nVerdampfer: {fmt_temp(t_verd)}"
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

async def send_status_telegram(session, t_oben, t_unten, t_mittig, t_verd, kompressor_status, current_runtime, total_runtime, config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func=None, is_solar_window_func=None):
    """Sendet den aktuellen Systemstatus über Telegram."""
    solax_data = await get_solax_data_func(session, state) or {"feedinpower": 0, "batPower": 0, "soc": 0}
    feedinpower = solax_data.get("feedinpower", 0)
    bat_power = solax_data.get("batPower", 0)

    nacht_reduction = int(config.Heizungssteuerung.NACHTABSENKUNG) if is_nighttime_func and is_nighttime_func(config) and not state.bademodus_aktiv else 0
    
    mode_str = "Normal"
    if state.bademodus_aktiv: mode_str = "🛁 Bademodus"
    elif state.urlaubsmodus_aktiv: mode_str = "🌴 Urlaub"
    elif state.control.solar_ueberschuss_aktiv: mode_str = "Solarüberschuss"

    # Additional Details calculation
    t_soll_ein = state.control.aktueller_einschaltpunkt
    t_soll_aus = state.control.aktueller_ausschaltpunkt
    vpn_ip = state.vpn_ip if state.vpn_ip else "N/A"
    
    # Forecast formatting
    forecast_text = "N/A"
    if state.solar.forecast_today is not None:
        today_val = f"{state.solar.forecast_today:.1f}"
        tomorrow_val = f"{state.solar.forecast_tomorrow:.1f}" if state.solar.forecast_tomorrow is not None else "??"
        sunrise = state.solar.sunrise_today if state.solar.sunrise_today else "??"
        sunset = state.solar.sunset_today if state.solar.sunset_today else "??"
        forecast_text = f"Heute: {today_val}kWh | Morgen: {tomorrow_val}kWh\n☀️ {sunrise} - 🌙 {sunset}"
        
    # Active Sensor
    active_sensor = state.control.active_rule_sensor if state.control.active_rule_sensor else "Automatisch"

    # Status Message Definition
    status_lines = [
        "📊 *SYSTEMSTATUS*",
        "",
        "🌡️ *Temperaturen*",
        f"Oben: {fmt_temp(t_oben)} | Mittig: {fmt_temp(t_mittig)}",
        f"Unten: {fmt_temp(t_unten)} | Verd: {fmt_temp(t_verd)}",
        "",
        "🛠️ *Kompressor*",
        f"Status: *{'EIN' if kompressor_status else 'AUS'}*",
        f"Laufzeit: {format_time(current_runtime)} (Heute: {format_time(total_runtime)})",
        "",
        "⚙️ *Regelung*",
        f"Sensor: {active_sensor}",
        f"Ein: {t_soll_ein:.1f}°C | Aus: {t_soll_aus:.1f}°C",
        "",
        "⚡ *Energie*",
        f"Netz: {feedinpower:.0f}W | Akku: {bat_power:.0f}W",
        f"PV: {solax_data.get('acpower', 0):.0f}W | SOC: {solax_data.get('soc', 0)}%",
        "",
        "ℹ️ *Infos*",
        f"Modus: {mode_str}",
        f"VPN IP: `{vpn_ip}`",
        f"Update: {datetime.now().strftime('%H:%M:%S')}",
        "",
        "🌤️ *Prognose*",
        forecast_text
    ]
    message = "\n".join(status_lines)
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard, parse_mode="Markdown")

async def process_telegram_messages_async(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates, last_update_id, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, chat_id, bot_token, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func, is_solar_window_func):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    if not updates: return last_update_id
    for update in updates:
        message = update.get('message', {})
        text = message.get('text', "").strip().lower()
        if not text: continue
        
        if state.awaiting_custom_duration: await handle_custom_duration(session, chat_id, bot_token, config, state, text)
        elif state.awaiting_urlaub_duration: await set_urlaubsmodus_duration(session, chat_id, bot_token, config, state, text)
        elif "temperaturen" in text: await send_temperature_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, chat_id, bot_token, state)
        elif "status" in text: await send_status_telegram(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, config, get_solax_data_func, chat_id, bot_token, state, is_nighttime_func, is_solar_window_func)
        elif "urlaub" in text:
            if "ende" in text:
                await deaktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
            else:
                await aktivere_urlaubsmodus(session, chat_id, bot_token, config, state)
        elif "bademodus" in text:
            if "aus" in text:
                await deaktivere_bademodus(session, chat_id, bot_token, state)
            else:
                await aktivere_bademodus(session, chat_id, bot_token, state)
        elif "verlauf 6h" in text: await get_boiler_temperature_history(session, 6, state, config)
        elif "verlauf 24h" in text: await get_boiler_temperature_history(session, 24, state, config)
        elif "laufzeiten" in text: await get_runtime_bar_chart(session, days=7, state=state)
        elif "hilfe" in text: await send_help_message(session, chat_id, bot_token, state)
        else: await send_unknown_command_message(session, chat_id, bot_token, state)
        last_update_id = update['update_id'] + 1
    return last_update_id

async def telegram_task(read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func, is_solar_window_func):
    """Telegram-Task zur Verarbeitung von Nachrichten."""
    last_update_id = None
    while True:
        async with create_robust_aiohttp_session() as session:
            try:
                if not state.bot_token or not state.chat_id:
                    await asyncio.sleep(60); continue
                updates = await get_telegram_updates(session, state.bot_token, last_update_id)
                if updates is not None:
                    t_boiler_oben = await read_temperature_func("oben")
                    t_boiler_unten = await read_temperature_func("unten")
                    t_boiler_mittig = await read_temperature_func("mittig")
                    t_verd = await read_temperature_func("verd")
                    
                    last_update_id = await process_telegram_messages_async(session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates, last_update_id, kompressor_status_func(), current_runtime_func(), total_runtime_func(), state.chat_id, state.bot_token, config, get_solax_data_func, state, get_boiler_temperature_history, get_runtime_bar_chart, is_nighttime_func, is_solar_window_func)
            except Exception as e:
                logging.error(f"Error in telegram_task: {e}")
            await asyncio.sleep(5)
