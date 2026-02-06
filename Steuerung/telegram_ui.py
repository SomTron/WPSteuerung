import logging
from datetime import datetime, timedelta
from telegram_api import send_telegram_message

def get_keyboard(state):
    """Erstellt das dynamische Keyboard basierend auf dem Urlaubsmodus und Bademodus."""
    urlaub_button = "🌴 Urlaub" if not state.urlaubsmodus_aktiv else "🌴 Urlaub Ende"
    bademodus_button = "🛁 Bademodus" if not state.bademodus_aktiv else "🛁 Bademodus aus"
    keyboard = {
        "keyboard": [
            ["🌡️ Temperaturen", "📊 Status"],
            ["📈 Verlauf 6h", "📉 Verlauf 24h"],
            [urlaub_button, bademodus_button],
            ["🆘 Hilfe", "⏱️ Laufzeiten"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    return keyboard

async def send_welcome_message(session, chat_id, bot_token, state):
    """Sendet die Willkommensnachricht mit benutzerdefiniertem Keyboard."""
    message = "Willkommen! Verwende die Schaltflächen unten, um das System zu steuern."
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

def escape_markdown(text):
    """Maskiert Markdown-Sonderzeichen für MarkdownV1."""
    if not isinstance(text, str):
        text = str(text)
    markdown_chars = ['_', '*', '`', '[']
    for char in markdown_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_time(seconds_str):
    try:
        if isinstance(seconds_str, timedelta):
            seconds = int(seconds_str.total_seconds())
        else:
            seconds = int(seconds_str)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
    except (ValueError, TypeError):
        return "0h 0m"

def fmt_temp(v): 
    return f"{v:.1f}°C" if v is not None else "N/A"

async def send_help_message(session, chat_id, bot_token, state):
    """Sendet eine Hilfenachricht mit verfügbaren Befehlen über Telegram."""
    help_text = (
        "🆘 *Hilfe - Verfügbare Befehle:*\n\n"
        "🌡️ *Temperaturen*: Zeigt aktuelle Sensorwerte.\n"
        "📊 *Status*: Kompakter Systemstatus & Energie.\n"
        "📈 *Verlauf 6h/24h*: Temperaturdiagramme.\n"
        "⏱️ *Laufzeiten*: Balkendiagramm der letzten 7 Tage.\n"
        "🌴 *Urlaub*: Aktiviert/Deaktiviert Urlaubsabsenkung.\n"
        "🛁 *Bademodus*: Erhöht WW-Sollwert temporär.\n"
        "🆘 *Hilfe*: Zeigt diese Nachricht."
    )
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, help_text, bot_token, reply_markup=keyboard, parse_mode="Markdown")

async def send_unknown_command_message(session, chat_id, bot_token, state):
    """Sendet eine Nachricht bei unbekanntem Befehl."""
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, "❓ Unbekannter Befehl. Verwende 'Hilfe' für eine Liste der Befehle.", bot_token, reply_markup=keyboard)
