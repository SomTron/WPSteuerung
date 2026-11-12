import asyncio
import logging
import pytz
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from clock import now_for
from constants import DEFAULT_TIMEZONE, TELEGRAM_RATE_LIMIT_SECONDS

# New Modules
from telegram_api import (
    create_robust_aiohttp_session, 
    send_telegram_message, 
    get_telegram_updates
)
from telegram_ui import (
    get_keyboard, 
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

#: Exakt diese Eingaben loesen den Not-Aus aus. Wichtig: "notaus" enthaelt
#: selbst "aus" - eine blosse Substring-Pruefung wuerde den Ausloeser als
#: Aufhebung missverstehen und im gesperrten Zustand das Falsche tun.
_NOTAUS_AUSLOESER = frozenset({
    "notaus", "not aus", "not-aus", "🛑", "stopp",
})
#: Woerter, die eine bestehende Sperre aufheben.
_NOTAUS_AUFHEBEN_WOERTER = ("aus", "aufheben", "aufheb", "reset", "quittier")


@dataclass
class TelegramRuntime:
    """Laufzeitwerte eines Telegram-Aufrufs (Temperaturen, Kompressor, Zeiten).

    Die reinen Statuswerte, die pro Zyklus neu gemessen werden. Sie werden
    bewusst getrennt vom `TelegramContext` gehalten, damit die
    Session-/State-Kopplung nicht mit den flüchtigen Messwerten vermischt wird.
    """

    t_oben: Optional[float] = None
    t_unten: Optional[float] = None
    t_mittig: Optional[float] = None
    t_verd: Optional[float] = None
    kompressor_status: bool = False
    current_runtime: Optional[object] = None
    total_runtime: Optional[object] = None


@dataclass
class TelegramContext:
    """Bündelt Session, Chat, Token, Config und State für Telegram-Aufrufe.

    Ersetzt die 17-Parameter-Signatur von `process_telegram_messages_async`
    und die 15-Parameter-Signatur von `send_status_telegram` (Befund: die
    Parameterlisten waren unwartbar und fehleranfällig beim Aufrufer).

    Die Callbacks `get_temperature_history_func` und `get_runtime_bar_chart_func`
    werden aus den Modul-Imports aufgelöst, sind also immer die echten
    Funktionen (die Aufrufer in `telegram_task` reichen genau diese).
    """

    session: object
    chat_id: Optional[str]
    bot_token: Optional[str]
    config: object
    state: object
    get_solax_data_func: object


def _ist_notaus_ausloeser(text: str) -> bool:
    """True nur bei exakter Uebereinstimmung - nie bei einer Erwaehnung.

    Bewusst KEINE Praefix-Erkennung: "notaus funktioniert nicht" beginnt
    ebenfalls mit "notaus". Der Not-Aus haelt die Sperre bis zu einem von
    Hand gesetzten Reset und haelt dabei auch Legionellenprophylaxe und
    Notfallschutz an. Eine falsch ausgeloeste Sperre legt die Heizung still,
    bis jemand sie bemerkt und aufhebt; eine nicht erkannte Eingabe kostet
    dagegen nur Sekunden ("unbekannter Befehl", dann `notaus` tippen).
    Das Ungleichgewicht ist eindeutig.
    """
    return text in _NOTAUS_AUSLOESER


def _laufendes_main():
    """Das main-Modul, in dem der Main-Loop tatsaechlich laeuft.

    main.py startet ueber `python main.py` und laeuft damit als
    `__main__`. Ein blasses `import main` laedt die Datei ein zweites Mal
    und liefert ein ZWEITES Modulobjekt - mit eigener
    ``control_command_queue``. Befehle, die dort landen, holt niemand ab.

    Genau das ist auf dem Pi passiert: `notaus` per Telegram hat die
    Steuerung bestaetigt, aber nichts ausgefuehrt. Die Sperre wurde
    nicht geschrieben, der Dienst lief weiter, und die Meldung
    "Not-Aus ausgeloest" stand trotzdem im Log.

    Der Fallback bleibt fuer Tests und fuer Aufrufe, bei denen main.py
    tatsaechlich als regulaeres Modul importiert wurde.
    """
    import sys

    laufend = sys.modules.get("__main__")
    if laufend is not None and hasattr(laufend, "enqueue_control_command"):
        return laufend
    import main

    return main


async def _api_alarm_testen(ctx: TelegramContext):
    """Loest den API-Fehlerdichte-Alarm bewusst aus - ohne Netzausfall.

    Der Alarm liest `state.api_errors`, und bis eben wurde diese Sammlung
    von niemandem gefuellt. Ohne diesen Befehl laesst sich die Kette
    Sammlung -> Schwelle -> Telegram nur mit einem echten Verbindungsausfall
    nachweisen - und der dauert je nach API 25 bzw. 30 Minuten.

    Die Testeintraege bekommen die Fehlerart "test". Sie sind damit am
    Text der Warnung erkennbar und fallen nach 30 Minuten aus dem Fenster,
    ohne Spuren in der Statistik zu hinterlassen. Das Befuellen geht
    bewusst ueber denselben Aufrufer wie der echte Betrieb - sonst
    wuerde genau die Kopplung geprueft, die man eigentlich sehen will.
    """
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
    # Kein "import main": main.py laeuft als __main__, ein blasser Import
    # liefert eine zweite Modulkopie. Es gibt dafuer _laufendes_main().
    _main = _laufendes_main()
    from constants import API_FEHLER_SCHWELLE, API_FEHLER_SCHWELLE_DEFAULT

    schwelle = API_FEHLER_SCHWELLE.get("solax", API_FEHLER_SCHWELLE_DEFAULT)
    for _ in range(schwelle):
        _main.track_api_error(state, "solax", "test")

    # last_alert leeren, sonst greift die 60-Minuten-Drosselung eines
    # vorherigen echten Alarms und es kommt nichts an.
    sammler = getattr(state, "api_errors", None) or {}
    eintrag = sammler.get("solax")
    if eintrag is not None:
        eintrag["last_alert"] = None

    try:
        await _main.check_api_health(session, state)
    except Exception as exc:
        logging.error("API-Alarmtest fehlgeschlagen", exc_info=True)
        return await send_telegram_message(
            session, chat_id,
            f"❌ *API-Alarmtest fehlgeschlagen:* {exc}",
            bot_token,
        )

    aktuelle = len((sammler.get("solax") or {}).get("errors", []))
    return await send_telegram_message(
        session, chat_id,
        f"🧪 *API-Alarmtest ausgeführt.*\n"
        f"{schwelle} Testeinträge für `solax` erzeugt "
        f"(Schwelle: {schwelle}).\n"
        f"Gesammelt: {aktuelle}\n\n"
        "Liegt darüber eine ⚠️-Warnung, funktioniert die Kette. "
        "Die Einträge verschwinden nach 30 Minuten von selbst.",
        bot_token,
        parse_mode="Markdown",
    )


async def _notaus_ausloesen_cmd(ctx: TelegramContext):
    """Not-Aus ueber Telegram. Der eigentliche Ausloeser laeuft im Main-Loop."""
    session, chat_id, bot_token = ctx.session, ctx.chat_id, ctx.bot_token
    logging.critical("NOT-AUS per Telegram angefordert")
    # Der Befehl geht durch dieselbe Queue wie die API-Befehle, damit die
    # Abschaltung im Main-Loop erfolgt und nicht neben ihm im Telegram-Task.
    _laufendes_main().enqueue_control_command("notaus", {"grund": "Telegram"})
    return await send_telegram_message(
        session, chat_id,
        "🛑 *Not-Aus angefordert.*\n"
        "Die Ausführung erfolgt im nächsten Steuerungslauf, das dauert\n"
        "bis zu 10 Sekunden. Erst danach ist der Kompressor wirklich aus\n"
        "und die Steuerung beendet - dann folgt die Meldung\n"
        "*NOT-AUS aktiv*.\n\n"
        "Bitte diese erste Nachricht nicht als Bestätigung lesen.\n\n"
        "Der Kompressor wird abgeschaltet und die Steuerung beendet.\n\n"
        "⚠️ *Es heizt nichts mehr* – auch die Legionellenprophylaxe\n"
        "und der Notfallschutz sind angehalten. Der Brauchwasser-\n"
        "Speicher kühlt aus.\n\n"
        "⚠️ *Diese Nachricht ist die letzte.* Der Dienst beendet sich\n"
        "gleich – Telegram und WebApp sind dann offline, ein\n"
        "`notaus aus` ist also noch gar nicht möglich.\n"
        "Zum Wiederannehmen zuerst:\n"
        "  `systemctl restart wpsteuerung`\n"
        "danach `notaus aus`",
        bot_token, parse_mode="Markdown",
    )


async def _notaus_aufheben(ctx: TelegramContext):
    """Not-Aus-Sperre aufheben."""
    session, chat_id, bot_token = ctx.session, ctx.chat_id, ctx.bot_token
    import notaus as _notaus
    if _notaus.notaus_loeschen():
        _laufendes_main().enqueue_control_command("notaus_aus")
        logging.warning("NOT-AUS per Telegram aufgehoben")
        return await send_telegram_message(
            session, chat_id,
            "✅ *Not-Aus aufgehoben.* Die Steuerung heizt wieder.",
            bot_token, parse_mode="Markdown",
        )
    return await send_telegram_message(
        session, chat_id,
        "❌ *Not-Aus konnte nicht aufgehoben werden.*\n"
        "Die Sperrdatei `notaus.lock` ist nicht schreibbar.\n"
        "Bitte auf dem Pi pruefen und loeschen.",
        bot_token, parse_mode="Markdown",
    )


async def aktivere_bademodus(ctx: TelegramContext):
    """Aktiviert den Bademodus."""
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
    state.bademodus_aktiv = True
    import modi_persistenz
    modi_persistenz.speichere_modi(state)
    keyboard = get_keyboard(state)
    message = "🛁 Bademodus aktiviert. Kompressor steuert nach erhöhtem Sollwert (untere Temperatur)."
    logging.info("Bademodus aktiviert")
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

async def deaktivere_bademodus(ctx: TelegramContext):
    """Deaktiviert den Bademodus."""
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
    state.bademodus_aktiv = False
    import modi_persistenz
    modi_persistenz.speichere_modi(state)
    keyboard = get_keyboard(state)
    message = "🛁 Bademodus deaktiviert."
    logging.info("Bademodus deaktiviert")
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard)

async def aktivere_urlaubsmodus(ctx: TelegramContext):
    """Aktiviert den Urlaubsmodus mit Zeitauswahl."""
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
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

async def set_urlaubsmodus_duration(ctx: TelegramContext, duration_text):
    """Setzt die Urlaubsmodus-Dauer basierend auf der Auswahl."""
    session, chat_id, bot_token, config, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.config, ctx.state)
    try:
        # text is already lowercased in process_telegram_messages_async
        if duration_text == "❌ abbrechen":
            keyboard = get_keyboard(state)
            await send_telegram_message(session, chat_id, "❌ Urlaubsmodus-Aktivierung abgebrochen.", bot_token, reply_markup=keyboard)
            state.awaiting_urlaub_duration = False
            return

        if duration_text == "🌴 1 tag":
            duration_days = 1
        elif duration_text == "🌴 3 tage":
            duration_days = 3
        elif duration_text == "🌴 7 tage":
            duration_days = 7
        elif duration_text == "🌴 14 tage":
            duration_days = 14
        elif duration_text == "🌴 benutzerdefiniert":
            keyboard = get_keyboard(state)
            await send_telegram_message(session, chat_id, "📅 Bitte sende die Anzahl der Tage (z.B. '5' für 5 Tage):", bot_token, reply_markup=keyboard)
            state.awaiting_custom_duration = True
            state.awaiting_urlaub_duration = False
            return
        else:
            try: 
                # Cleanup string to extract number
                clean_text = duration_text.replace("🌴", "").replace("tage", "").replace("tag", "").strip()
                duration_days = int(clean_text)
            except ValueError:
                keyboard = get_keyboard(state)
                await send_telegram_message(session, chat_id, "❌ Ungültige Eingabe.", bot_token, reply_markup=keyboard)
                state.awaiting_urlaub_duration = False
                return

        local_tz = pytz.timezone(DEFAULT_TIMEZONE)
        now = now_for(state, fallback_tz=local_tz)
        state.urlaubsmodus_aktiv = True
        state.urlaubsmodus_start = now
        state.urlaubsmodus_ende = now + timedelta(days=duration_days)
        import modi_persistenz
        modi_persistenz.speichere_modi(state)

        urlaubsabsenkung = int(config.Urlaubsmodus.URLAUBSABSENKUNG)
        keyboard = get_keyboard(state)
        await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert für {duration_days} Tage (-{urlaubsabsenkung}°C).", bot_token, reply_markup=keyboard)
        state.awaiting_urlaub_duration = False
        state.awaiting_custom_duration = False
    except Exception as e:
        logging.error(f"Fehler bei Urlaubsdauer: {e}")

async def handle_custom_duration(ctx: TelegramContext, message_text):
    """Behandelt benutzerdefinierte Dauer-Eingabe."""
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
    try:
        # Check against lowercased button texts to prevent re-triggering logic if user clicks buttons unexpectedly
        if message_text in ["🌴 benutzerdefiniert", "❌ abbrechen", "🌴 1 tag", "🌴 3 tage", "🌴 7 tage", "🌴 14 tage"]:
            return
        duration_days = int(message_text.strip())
        local_tz = pytz.timezone(DEFAULT_TIMEZONE)
        now = now_for(state, fallback_tz=local_tz)
        state.urlaubsmodus_aktiv = True
        state.urlaubsmodus_start = now
        state.urlaubsmodus_ende = now + timedelta(days=duration_days)
        import modi_persistenz
        modi_persistenz.speichere_modi(state)
        keyboard = get_keyboard(state)
        await send_telegram_message(session, chat_id, f"🌴 Urlaubsmodus aktiviert für {duration_days} Tage.", bot_token, reply_markup=keyboard)
        state.awaiting_urlaub_duration = False
        state.awaiting_custom_duration = False
    except Exception as e:
        logging.error(f"Fehler bei benutzerdefinierter Dauer: {e}")

async def deaktivere_urlaubsmodus(ctx: TelegramContext):
    """Deaktiviert den Urlaubsmodus."""
    session, chat_id, bot_token, state = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.state)
    state.urlaubsmodus_aktiv = False
    import modi_persistenz
    modi_persistenz.speichere_modi(state)
    keyboard = get_keyboard(state)
    await send_telegram_message(session, chat_id, "🏠 Urlaubsmodus deaktiviert.", bot_token, reply_markup=keyboard)

async def send_temperature_telegram(ctx: TelegramContext, rt: TelegramRuntime):
    """Sendet die aktuellen Temperaturen über Telegram."""
    message = (
        f"🌡️ Aktuelle Temperaturen:\n"
        f"Boiler oben: {fmt_temp(rt.t_oben)}\n"
        f"Boiler mittig: {fmt_temp(rt.t_mittig)}\n"
        f"Boiler unten: {fmt_temp(rt.t_unten)}\n"
        f"Verdampfer: {fmt_temp(rt.t_verd)}"
    )
    keyboard = get_keyboard(ctx.state)
    return await send_telegram_message(ctx.session, ctx.chat_id, message, ctx.bot_token, reply_markup=keyboard)

async def send_status_telegram(ctx: TelegramContext, rt: TelegramRuntime):
    """Sendet den aktuellen Systemstatus über Telegram."""
    session, state = ctx.session, ctx.state
    chat_id, bot_token = ctx.chat_id, ctx.bot_token
    t_oben, t_unten, t_mittig, t_verd = rt.t_oben, rt.t_unten, rt.t_mittig, rt.t_verd
    kompressor_status, current_runtime, total_runtime = (
        rt.kompressor_status, rt.current_runtime, rt.total_runtime)
    get_solax_data_func = ctx.get_solax_data_func
    solax_data = await get_solax_data_func(session, state) or {"feedinpower": 0, "batPower": 0, "soc": 0}
    feedinpower = solax_data.get("feedinpower", 0)
    bat_power = solax_data.get("batPower", 0)

    
    # Mode mapping for icons
    mode_name = state.control.previous_modus or "Normal"
    if "Bademodus" in mode_name:
        mode_str = "🛁 " + mode_name
    elif "Urlaub" in mode_name:
        mode_str = "🌴 " + mode_name
    elif "Solar" in mode_name:
        mode_str = "☀️ " + mode_name
    elif "Frostschutz" in mode_name:
        mode_str = "❄️ " + mode_name
    elif "Übergang" in mode_name:
        mode_str = "🌓 " + mode_name
    elif "Nacht" in mode_name:
        mode_str = "🌙 " + mode_name
    else:
        mode_str = mode_name

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

        # --- Regel-Ergebnisse formatieren ---
        regel_lines = []
        if hasattr(state.control, 'alle_ergebnisse') and state.control.alle_ergebnisse:
            # Sortieren nach Priorität (absteigend)
            for e in sorted(state.control.alle_ergebnisse, key=lambda x: x.prioritaet, reverse=True):
                # Status-Symbol: ✅ = EIN, ❌ = AUS, ⏸️ = Keine Aktion
                if e.einschalten is True:
                    status_symbol = "✅ EIN"
                elif e.einschalten is False:
                    status_symbol = "❌ AUS"
                else:
                    status_symbol = "⏸️ ---"
            
                # Inaktiv-Markierung
                aktiv_marker = "🔇 " if not e.aktiv else ""
            
                # Grund kurz fassen (max 65 Zeichen)
                grund_kurz = e.grund[:65] if e.grund else ""
                if grund_kurz:
                    regel_lines.append(f"{aktiv_marker}{status_symbol} {escape_markdown(e.name)}: {escape_markdown(grund_kurz)}")
                else:
                    regel_lines.append(f"{aktiv_marker}{status_symbol} {escape_markdown(e.name)}")
        else:
            regel_lines.append("Keine Regel-Daten verfügbar")

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
        ]
    
        # Add blocking reason if compressor is off and reason exists
        if not kompressor_status and state.control.blocking_reason:

            status_lines.append(f"🚫 Blockiert: {escape_markdown(state.control.blocking_reason)}")
    
        status_lines.extend([
            f"Laufzeit: {format_time(current_runtime)} (Heute: {format_time(total_runtime)})",
            "",
            "⚙️ *Regelung*",

            f"Sensor: {escape_markdown(active_sensor)}",
            f"Ein: {t_soll_ein:.1f}°C | Aus: {t_soll_aus:.1f}°C",
            "",
            "📋 *Regel-Ergebnisse*",
        ])
    
        # Regel-Ergebnisse hinzufügen
        status_lines.extend(regel_lines)
    
        status_lines.extend([
            "",
            "⚡ *Energie*",
            f"Netz: {feedinpower:.0f}W | Akku: {bat_power:.0f}W",
            f"PV: {solax_data.get('acpower', 0):.0f}W | SOC: {solax_data.get('soc', 0)}%",
            "",
            "ℹ️ *Infos*",
            f"Modus: {escape_markdown(mode_str)}",
            f"VPN IP: `{vpn_ip}`",
            f"Update: {now_for(state).strftime('%H:%M:%S')}",
            "🌤️ *Prognose*",
            escape_markdown(forecast_text)
        ])
    
    message = "\n".join(status_lines)
    keyboard = get_keyboard(state)
    return await send_telegram_message(session, chat_id, message, bot_token, reply_markup=keyboard, parse_mode="Markdown")

async def process_telegram_messages_async(ctx: TelegramContext, rt: TelegramRuntime, updates, last_update_id):
    """Verarbeitet eingehende Telegram-Nachrichten asynchron."""
    if not updates:
        return last_update_id

    state = ctx.state
    session, chat_id, bot_token, config = (
        ctx.session, ctx.chat_id, ctx.bot_token, ctx.config)

    # Rate-Limiting: Prüfe ob letzte Nachricht zu schnell aufeinander folgt
    now = now_for(state)
    if state.last_telegram_command_time:
        elapsed = (now - state.last_telegram_command_time).total_seconds()
        if elapsed < TELEGRAM_RATE_LIMIT_SECONDS:
            logging.warning(f"Rate-Limit: Befehl ignoriert (nur {elapsed:.1f}s seit letztem Befehl, min. {TELEGRAM_RATE_LIMIT_SECONDS}s)")
            return last_update_id

    for update in updates:
        message = update.get('message', {})
        text = message.get('text', "").strip().lower()
        if not text:
            continue
        state.last_telegram_command_time = now
        
        try:
            if state.awaiting_custom_duration:
                await handle_custom_duration(ctx, text)
            elif state.awaiting_urlaub_duration:
                await set_urlaubsmodus_duration(ctx, text)
            elif "temperaturen" in text:
                await send_temperature_telegram(ctx, rt)
            elif "status" in text:
                await send_status_telegram(ctx, rt)
            elif "urlaub" in text:
                if "ende" in text:
                    await deaktivere_urlaubsmodus(ctx)
                else:
                    await aktivere_urlaubsmodus(ctx)
            elif "bademodus" in text:
                if "aus" in text:
                    await deaktivere_bademodus(ctx)
                else:
                    await aktivere_bademodus(ctx)
            elif "verlauf 6h" in text:
                await get_boiler_temperature_history(session, 6, state, config)
            elif "verlauf 24h" in text:
                await get_boiler_temperature_history(session, 24, state, config)
            elif "laufzeiten" in text:
                await get_runtime_bar_chart(session, days=7, state=state)
            elif "hilfe" in text:
                await send_help_message(session, chat_id, bot_token, state)
            elif "api test" in text:
                # Muss VOR dem Not-Aus-Zweig stehen: der Aufheb-Zweig
                # prueft auf Substrings wie "aus". Die Reihenfolge ist hier
                # sicherheitsrelevant, nicht nur bequem.
                await _api_alarm_testen(ctx)
            elif "notaus" in text or "not aus" in text or "not-aus" in text:
                # Reihenfolge und Absicht sind hier entscheidend:
                # "notaus" ENTHAELT selbst "aus" - eine blosse Pruefung auf
                # "aus" wuerde den Ausloeser als Aufhebung missverstehen.
                # Umgekehrt darf eine ERWAEHNUNG nicht ausloesen: vorher
                # loeste jeder else-Zweig aus, sodass "was bedeutet notaus"
                # die Heizung abschaltete - inklusive Legionelle - bis zu
                # einem von Hand gesetzten Reset.
                if _ist_notaus_ausloeser(text):
                    await _notaus_ausloesen_cmd(ctx)
                elif any(w in text for w in _NOTAUS_AUFHEBEN_WOERTER):
                    await _notaus_aufheben(ctx)
                else:
                    # Wort erwaehnt, aber keine Absicht -> nichts tun.
                    await send_unknown_command_message(
                        session, chat_id, bot_token, state
                    )
            else:
                await send_unknown_command_message(session, chat_id, bot_token, state)
        except Exception as e:
            logging.error(f"Fehler bei der Verarbeitung von '{text}': {e}", exc_info=True)
            await send_telegram_message(session, chat_id, f"❌ Fehler bei der Verarbeitung: {str(e)}", bot_token)
        last_update_id = update['update_id'] + 1
    return last_update_id

async def telegram_task(ctx: TelegramContext, read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func):
    """Telegram-Task zur Verarbeitung von Nachrichten."""
    last_update_id = None
    state = ctx.state
    async with create_robust_aiohttp_session(state.config) as session:
        # Session in den Kontext schreiben, damit alle Handler sie nutzen.
        ctx.session = session
        while True:
            try:
                if not state.bot_token or not state.chat_id:
                    await asyncio.sleep(60)
                    continue
                updates = await get_telegram_updates(session, state.bot_token, last_update_id)
                if updates is not None:
                    t_boiler_oben = await read_temperature_func("oben")
                    t_boiler_unten = await read_temperature_func("unten")
                    t_boiler_mittig = await read_temperature_func("mittig")
                    t_verd = await read_temperature_func("verd")

                    rt = TelegramRuntime(
                        t_oben=t_boiler_oben, t_unten=t_boiler_unten,
                        t_mittig=t_boiler_mittig, t_verd=t_verd,
                        kompressor_status=kompressor_status_func(),
                        current_runtime=current_runtime_func(),
                        total_runtime=total_runtime_func(),
                    )
                    last_update_id = await process_telegram_messages_async(ctx, rt, updates, last_update_id)
            except Exception as e:
                logging.error(f"Error in telegram_task: {e}")
            await asyncio.sleep(5)
