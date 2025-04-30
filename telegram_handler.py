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


async def send_telegram_message(session, chat_id, message, bot_token):
    """Sendet eine Nachricht über Telegram mit Fehlerbehandlung."""
    # Prüfe Nachrichtenlänge (Telegram-Limit: 4096 Zeichen)
    if len(message) > 4096:
        message = message[:4093] + "..."
        logging.warning("Nachricht gekürzt, da Telegram-Limit von 4096 Zeichen überschritten.")

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        async with session.post(url, json=payload, timeout=10) as response:
            if response.status == 200:
                logging.info(f"Telegram-Nachricht gesendet: {message}")
            else:
                error_text = await response.text()
                logging.error(f"Fehler beim Senden der Telegram-Nachricht: Status {response.status}, Details: {error_text}")
    except aiohttp.ClientError as e:
        logging.error(f"Netzwerkfehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)
    except asyncio.TimeoutError:
        logging.error("Timeout beim Senden der Telegram-Nachricht", exc_info=True)
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Senden der Telegram-Nachricht: {e}", exc_info=True)


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


async def telegram_task(session, read_temperature_func, sensor_ids, kompressor_status_func, current_runtime_func, total_runtime_func, config, get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func, is_nighttime_func):
    last_update_id = None
    max_retries = 3
    while True:
        for attempt in range(max_retries):
            try:
                if not state.bot_token or not state.chat_id:
                    logging.warning("Telegram bot_token oder chat_id fehlt. Überspringe telegram_task.")
                    await asyncio.sleep(60)
                    continue
                updates = await get_telegram_updates(session, state.bot_token, last_update_id)
                if updates is not None:
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
                    kompressor_status = kompressor_status_func()
                    aktuelle_laufzeit = current_runtime_func()
                    gesamtlaufzeit = total_runtime_func()
                    last_update_id = await process_telegram_messages_async(
                        session, t_boiler_oben, t_boiler_unten, t_boiler_mittig, t_verd, updates, last_update_id,
                        kompressor_status, aktuelle_laufzeit, gesamtlaufzeit, state.chat_id, state.bot_token, config,
                        get_solax_data_func, state, get_temperature_history_func, get_runtime_bar_chart_func,
                        is_nighttime_func)
                    break
                else:
                    logging.warning(f"Telegram-Updates waren None, Versuch {attempt + 1}/{max_retries}")
            except Exception as e:
                logging.error(f"Fehler in telegram_task (Versuch {attempt + 1}/{max_retries}): {str(e)}", exc_info=True)
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



async def get_boiler_temperature_history(session, hours, state, config):
    logging.debug(f"get_boiler_temperature_history aufgerufen mit hours={hours}, state.bot_token={state.bot_token}")
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    try:
        # Zeitfenster definieren mit Zeitzone
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)

        # Erwartete Spalten definieren
        expected_columns = [
            "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "Kompressor",
            "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "PowerSource"
        ]

        # Sicherstellen, dass Datei synchronisiert ist
        try:
            with open("heizungsdaten.csv", "r") as f:
                os.fsync(f.fileno())
        except Exception as e:
            logging.warning(f"Fehler bei Dateisynchronisation: {e}")

        # Lese Header zur Bestimmung verfügbarer Spalten
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

        # Robustes Laden der CSV mit Überspringen fehlerhafter Zeilen
        try:
            df = pd.read_csv(
                "heizungsdaten.csv",
                usecols=available_columns,
                on_bad_lines='skip',  # ⚠️ Fehlerhafte Zeilen werden einfach ignoriert
                engine='python'
            )
            logging.debug(f"{len(df)} Zeilen aus CSV geladen.")
        except Exception as e:
            logging.error(f"Fehler beim Laden der CSV: {e}")
            await send_telegram_message(session, state.chat_id, "Fehler beim Laden der CSV-Datei.", state.bot_token)
            return

        # Parse Zeitstempel robust und lösche ungültige Zeilen
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

        # Filtere Zeitraum
        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]
        logging.debug(f"{len(df)} Zeilen nach Zeitfilterung.")

        if df.empty:
            logging.warning(f"Keine Daten im Zeitfenster ({hours}h) gefunden.")
            await send_telegram_message(session, state.chat_id, "Keine Daten für den Verlauf verfügbar.", state.bot_token)
            return

        # Standardwerte setzen / Bereinigung weiterer Spalten
        df = df.copy()  # Avoid SettingWithCopyWarning
        df["Einschaltpunkt"] = pd.to_numeric(df.get("Einschaltpunkt", pd.Series(42)), errors="coerce").fillna(42)
        df["Ausschaltpunkt"] = pd.to_numeric(df.get("Ausschaltpunkt", pd.Series(45)), errors="coerce").fillna(45)
        df["Solarüberschuss"] = pd.to_numeric(df.get("Solarüberschuss", pd.Series(0)), errors="coerce").fillna(0).astype(int)
        df["PowerSource"] = df.get("PowerSource", pd.Series("Unbekannt")).fillna("Unbekannt").replace(["N/A", "Fehler"], "Unbekannt")
        df["Kompressor"] = df.get("Kompressor", pd.Series(0)).replace({"EIN": 1, "AUS": 0}).fillna(0).astype(int)

        # Temperaturspalten sichern
        temp_columns = [c for c in ["T_Oben", "T_Unten", "T_Mittig"] if c in df.columns]
        for col in temp_columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        # Synthetische Lückenbehandlung (optional)
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

        # Reduziere auf max. target_points für bessere Darstellung
        target_points = 50
        if len(df) > target_points:
            df = df.iloc[::len(df) // target_points].head(target_points)
        logging.debug(f"{len(df)} Zeilen nach Downscaling.")

        # Daten für das Diagramm vorbereiten
        timestamps = df["Zeitstempel"]
        t_oben = df.get("T_Oben")
        t_unten = df.get("T_Unten")
        t_mittig = df.get("T_Mittig")
        einschaltpunkte = df["Einschaltpunkt"]
        ausschaltpunkte = df["Ausschaltpunkt"]
        kompressor_status = df["Kompressor"]
        power_sources = df["PowerSource"]
        solar_ueberschuss = df["Solarüberschuss"]

        # Farbkodierung für Stromquellen
        color_map = {
            "Direkter PV-Strom": "green",
            "Strom aus der Batterie": "yellow",
            "Strom vom Netz": "red",
            "Keine aktive Energiequelle": "blue",
            "Unbekannt": "gray"
        }

        untere_grenze = int(config["Heizungssteuerung"].get("UNTERER_FUEHLER_MIN", 20))
        obere_grenze = int(config["Heizungssteuerung"].get("AUSSCHALTPUNKT_ERHOEHT", 55))

        # Diagrammerstellung
        plt.figure(figsize=(12, 6))

        # Nur ein Eintrag pro Quelle, um Dopplungen in der Legende zu vermeiden
        shown_labels = set()

        # Überlagerung: Kompressorstatus stärker markieren
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

        # Temperaturkurven
        if t_oben is not None:
            plt.plot(timestamps, t_oben, label="T_Oben", marker="o", color="blue")
        if t_unten is not None:
            plt.plot(timestamps, t_unten, label="T_Unten", marker="x", color="red")
        if t_mittig is not None:
            plt.plot(timestamps, t_mittig, label="T_Mittig", marker="^", color="purple")

        # Historische Einschaltpunkte
        plt.plot(timestamps, einschaltpunkte, label="Einschaltpunkt (historisch)", linestyle="--", color="green")
        plt.plot(timestamps, ausschaltpunkte, label="Ausschaltpunkt (historisch)", linestyle="--", color="orange")

        # Aktuelle Grenzwerte
        if solar_ueberschuss.any():
            plt.axhline(y=state.aktueller_einschaltpunkt, color="purple", linestyle="-.",
                        label=f"Einschaltpunkt ({state.aktueller_einschaltpunkt}°C)")
            plt.axhline(y=state.aktueller_ausschaltpunkt, color="cyan", linestyle="-.",
                        label=f"Ausschaltpunkt ({state.aktueller_ausschaltpunkt}°C)")

        # Plot-Einstellungen
        plt.xlim(time_ago, now)
        plt.ylim(0, max(untere_grenze, obere_grenze) + 5)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf (letzte {hours} Stunden)")
        plt.grid(True)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left")
        plt.tight_layout()

        # Bild speichern
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        # Telegram-Bild senden
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
    """Erstellt ein gestapeltes Balkendiagramm der Kompressorlaufzeiten für die letzten 'days' Tage."""
    if state is None:
        logging.error("State-Objekt nicht übergeben, kann Telegram-Nachricht nicht senden.")
        return

    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        today = now.date()
        start_date = today - timedelta(days=days - 1)  # Initialisiere start_date vor dem try-Block
        async with aiofiles.open("heizungsdaten.csv", 'r') as csvfile:
            lines = await csvfile.readlines()
            if not lines:
                logging.warning("CSV-Datei ist leer.")
                await send_telegram_message(session, state.chat_id, "Keine Laufzeitdaten verfügbar.", state.bot_token)
                return

            header = lines[0].strip().split(',')
            logging.debug(f"CSV-Header: {header}")  # Logge den Header

            try:
                timestamp_col = header.index("Zeitstempel")  # Korrigiert: "Zeitstempel" statt "timestamp"
                kompressor_col = header.index("Kompressor")  # Korrigiert: "Kompressor" statt "kompressor_status"
                runtime_pv_col = header.index("PowerSource")  # Korrigiert: "PowerSource" (ggf. weitere Anpassung nötig)
                runtime_battery_col = header.index("BatPower")  # Korrigiert: "BatPower"
                runtime_grid_col = header.index(
                    "ConsumeEnergy")  # Korrigiert: "ConsumeEnergy" (ggf. weitere Anpassung nötig)
            except ValueError as e:
                logging.error(f"Notwendige Spaltennamen nicht in CSV-Header gefunden: {e}")
                await send_telegram_message(session, state.chat_id, "Fehler beim Lesen der CSV-Datei.", state.bot_token)
                return

            lines = lines[1:]

            for line in lines:
                parts = line.strip().split(',')
                if len(parts) > max(timestamp_col, kompressor_col, runtime_pv_col, runtime_battery_col, runtime_grid_col):
                    try:
                        timestamp_str = parts[timestamp_col].strip()
                        timestamp = local_tz.localize(datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S'))
                        date = timestamp.date()

                        if date >= start_date and date <= today:
                            if date not in dates:
                                dates.append(date)
                                runtime_pv_data.append(timedelta())
                                runtime_battery_data.append(timedelta())
                                runtime_grid_data.append(timedelta())

                            runtime_index = dates.index(date)

                            def parse_timedelta(time_str):
                                try:
                                    h, m, s = map(int, time_str.split(':'))
                                    return timedelta(hours=h, minutes=m, seconds=s)
                                except ValueError as e:
                                    logging.error(f"Fehler beim Parsen der Zeit '{time_str}': {e}")
                                    return timedelta()

                            try:
                                runtime_pv_data[runtime_index] += parse_timedelta(parts[runtime_pv_col].strip())
                            except (IndexError, ValueError):
                                runtime_pv_data[runtime_index] += timedelta()

                            try:
                                runtime_battery_data[runtime_index] += parse_timedelta(parts[runtime_battery_col].strip())
                            except (IndexError, ValueError):
                                runtime_battery_data[runtime_index] += timedelta()

                            try:
                                runtime_grid_data[runtime_index] += parse_timedelta(parts[runtime_grid_col].strip())
                            except (IndexError, ValueError):
                                runtime_grid_data[runtime_index] += timedelta()

                    except (ValueError, IndexError) as e:
                        logging.warning(f"Fehler beim Parsen der Zeile: {line.strip()}, Fehler: {e}")
                        continue

            if not dates:
                logging.warning("Keine Laufzeitdaten für die angegebenen Tage gefunden.")
                await send_telegram_message(session, state.chat_id, "Keine Laufzeitdaten verfügbar.", state.bot_token)
                return

            dates = sorted(dates)
            runtime_pv_hours = [td.total_seconds() / 3600 for td in runtime_pv_data]
            runtime_battery_hours = [td.total_seconds() / 3600 for td in runtime_battery_data]
            runtime_grid_hours = [td.total_seconds() / 3600 for td in runtime_grid_data]

            # **Gestapeltes Balkendiagramm erstellen**
            plt.figure(figsize=(10, 6))
            plt.bar(dates, runtime_pv_hours, label="PV", color="green")
            plt.bar(dates, runtime_battery_hours, bottom=runtime_pv_hours, label="Batterie", color="orange")
            plt.bar(dates, runtime_grid_hours, bottom=[sum(x) for x in zip(runtime_pv_hours, runtime_battery_hours)], label="Netz", color="blue")

            plt.xlabel("Datum")
            plt.ylabel("Laufzeit (Stunden)")
            plt.title(f"Kompressorlaufzeiten nach Energiequelle (letzte {days} Tage)")
            plt.xticks(dates, [date.strftime('%d-%m') for date in dates], rotation=45, ha='right')
            plt.legend()  # Legende hinzufügen
            plt.tight_layout()

            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=100)
            buf.seek(0)
            plt.close()

            url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
            form = FormData()
            form.add_field("chat_id", state.chat_id)
            form.add_field("caption", f"📊 Kompressorlaufzeiten nach Energiequelle (letzte {days} Tage)")
            form.add_field("photo", buf, filename="runtime_chart.png", content_type="image/png")

            async with session.post(url, data=form) as response:
                response.raise_for_status()
                logging.info(f"Laufzeitdiagramm für {days} Tage gesendet.")

            buf.close()

    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Laufzeitdiagramms: {str(e)}")
        await send_telegram_message(session, state.chat_id, f"Fehler beim Abrufen der Laufzeiten: {str(e)}", state.bot_token)