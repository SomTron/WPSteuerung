import logging
import os
import io
import pandas as pd
import matplotlib.pyplot as plt
plt.switch_backend('Agg')  # Headless mode for RPi
import pytz
from datetime import datetime, timedelta
from aiohttp import FormData
from telegram_api import send_telegram_message
from utils import check_and_fix_csv_header, backup_csv, EXPECTED_CSV_HEADER, HEIZUNGSDATEN_CSV

async def get_boiler_temperature_history(session, hours, state, config):
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf, historischen Sollwerten, Grenzwerten und Kompressorstatus."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)
        logging.debug(f"⏳ Starte Temperaturverlauf für {hours} Stunden, Zeitfenster: {time_ago} bis {now}")
        file_path = HEIZUNGSDATEN_CSV
        if not os.path.isfile(file_path):
            logging.error(f"❌ CSV-Datei nicht gefunden: {file_path}")
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, f"CSV-Datei nicht gefunden ({file_path}).", state.bot_token, reply_markup=keyboard)
            return
        # Header regelmäßig prüfen und ggf. korrigieren
        check_and_fix_csv_header(file_path)
        try:
            # Robust: Trennzeichen automatisch erkennen, Header prüfen, Fehlerhafte Zeilen überspringen
            df = pd.read_csv(file_path, sep=None, engine="python", on_bad_lines='skip')
            # Prüfe, ob alle erwarteten Spalten vorhanden sind
            missing = [col for col in EXPECTED_CSV_HEADER if col not in df.columns]
            if missing:
                logging.warning(f"Fehlende Spalten in CSV: {missing}")
            logging.debug(f"CSV geladen, {len(df)} Zeilen, Spalten: {df.columns.tolist()}")
            # Optional: Nur relevante Spalten weitergeben
            usecols = [c for c in ["Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Verd", "Kompressor", "PowerSource", "Einschaltpunkt", "Ausschaltpunkt"] if c in df.columns]
            df = df[usecols]
        except Exception as e:
            logging.error(f"❌ Fehler beim Einlesen der CSV: {e}", exc_info=True)
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, "Fehler beim Lesen der CSV-Datei.", state.bot_token, reply_markup=keyboard)
            return
        if "Zeitstempel" not in df.columns:
            logging.error("❌ Spalte 'Zeitstempel' fehlt in der CSV.")
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, "Spalte 'Zeitstempel' fehlt in der CSV.", state.bot_token, reply_markup=keyboard)
            return
        try:
            df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
            logging.debug(f"Zeitstempel-Datentyp nach Parsing: {df['Zeitstempel'].dtype}")
        except Exception as e:
            logging.error(f"❌ Fehler beim Parsen der Zeitstempel: {e}", exc_info=True)
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, "Fehler beim Parsen der Zeitstempel.", state.bot_token, reply_markup=keyboard)
            return
        invalid_rows = df[df["Zeitstempel"].isna()]
        if not invalid_rows.empty:
            logging.warning(f"⚠️ {len(invalid_rows)} Zeilen mit ungültigen Zeitstempeln gefunden.")
            df = df[df["Zeitstempel"].notna()].copy()
        if df.empty:
            logging.warning(f"❌ Keine gültigen Daten nach Zeitstempel-Parsing für die letzten {hours} Stunden.")
            try:
                latest_data = pd.read_csv(file_path, usecols=["Zeitstempel"])
                latest_data["Zeit stempel"] = pd.to_datetime(latest_data["Zeitstempel"], errors='coerce')
                latest_time = latest_data["Zeitstempel"].dropna().max() if not latest_data["Zeitstempel"].dropna().empty else "unbekannt"
                from telegram_ui import get_keyboard
                keyboard = get_keyboard(state)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine gültigen Daten für die letzten {hours} Stunden vorhanden. Letzter Eintrag: {latest_time}.",
                    state.bot_token, reply_markup=keyboard
                )
            except Exception as e:
                logging.error(f"❌ Fehler beim Abrufen des neuesten Zeitstempels: {e}", exc_info=True)
                from telegram_ui import get_keyboard
                keyboard = get_keyboard(state)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine gültigen Daten für die letzten {hours} Stunden vorhanden. Fehler beim Abrufen des neuesten Eintrags.",
                    state.bot_token, reply_markup=keyboard
                )
            return
        try:
            df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz, ambiguous='infer', nonexistent='shift_forward')
        except Exception as e:
            logging.error(f"❌ Fehler beim Hinzufügen der Zeitzone: {e}", exc_info=True)
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, "Fehler beim Hinzufügen der Zeitzone.", state.bot_token, reply_markup=keyboard)
            return
        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]
        if df.empty:
            logging.warning(f"❌ Keine Daten für die letzten {hours} Stunden gefunden.")
            try:
                latest_data = pd.read_csv(file_path, usecols=["Zeitstempel"])
                latest_data["Zeitstempel"] = pd.to_datetime(latest_data["Zeitstempel"], errors='coerce')
                latest_time = latest_data["Zeitstempel"].dropna().max() if not latest_data["Zeitstempel"].dropna().empty else "unbekannt"
                from telegram_ui import get_keyboard
                keyboard = get_keyboard(state)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine Daten für die letzten {hours} Stunden vorhanden. Letzter Eintrag: {latest_time}.",
                    state.bot_token, reply_markup=keyboard
                )
            except Exception as e:
                logging.error(f"❌ Fehler beim Abrufen des neuesten Zeitstempels: {e}", exc_info=True)
                from telegram_ui import get_keyboard
                keyboard = get_keyboard(state)
                await send_telegram_message(
                    session, state.chat_id,
                    f"Keine Daten für die letzten {hours} Stunden vorhanden. Fehler beim Abrufen des neuesten Eintrags.",
                    state.bot_token, reply_markup=keyboard
                )
            return
        temp_columns = ["T_Oben", "T_Unten", "T_Mittig", "T_Verd"]
        for col in temp_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            else:
                logging.warning(f"Spalte {col} fehlt in der CSV.")
                df[col] = float('nan')
        min_temp = df[temp_columns].min().min()
        max_temp = df[temp_columns].max().max()
        y_min = max(0, min_temp - 2) if pd.notna(min_temp) else 0
        y_max = max_temp + 5 if pd.notna(max_temp) else 60
        color_map = {
            "Direkter PV-Strom": "green",
            "Solar": "green",
            "Strom aus der Batterie": "yellow",
            "Batterie": "yellow",
            "Strom vom Netz": "red",
            "Netz": "red",
            "Keine aktive Energiequelle": "blue",
            "Unbekannt": "gray"
        }
        plt.figure(figsize=(12, 6))
        shown_labels = set()
        if "Kompressor" in df.columns and "PowerSource" in df.columns:
            # Support both old format (EIN/AUS) and new format (1/0)
            df["Kompressor"] = df["Kompressor"].astype(str).map({
                "EIN": True, "AUS": False, 
                "1": True, "0": False,
                "1.0": True, "0.0": False
            }).fillna(False)
            for source, color in color_map.items():
                mask = (df["PowerSource"] == source) & df["Kompressor"]
                if mask.any():
                    label = f"Kompressor EIN ({source})"
                    if label not in shown_labels:
                        plt.fill_between(df["Zeitstempel"], y_min, y_max, where=mask, color=color, alpha=0.3, label=label)
                        shown_labels.add(label)
                    else:
                        plt.fill_between(df["Zeitstempel"], y_min, y_max, where=mask, color=color, alpha=0.3)
        for col, color, linestyle in [
            ("T_Oben", "blue", "-"),
            ("T_Unten", "red", "-"),
            ("T_Mittig", "purple", "-"),
            ("T_Verd", "gray", "--")
        ]:
            if col in df.columns and df[col].notna().any():
                plt.plot(df["Zeitstempel"], df[col], label=col, color=color, linestyle=linestyle, linewidth=1.2)
        if "Einschaltpunkt" in df.columns:
            df["Einschaltpunkt"] = pd.to_numeric(df["Einschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Einschaltpunkt"], label="Einschaltpunkt (historisch)", linestyle="--", color="green")
        if "Ausschaltpunkt" in df.columns:
            df["Ausschaltpunkt"] = pd.to_numeric(df["Ausschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Ausschaltpunkt"], label="Ausschaltpunkt (historisch)", linestyle="--", color="orange")
        plt.xlim(time_ago, now)
        plt.ylim(y_min, y_max)
        plt.xlabel("Zeit")
        plt.ylabel("Temperatur (°C)")
        plt.title(f"Boiler-Temperaturverlauf – Letzte {hours} Stunden")
        plt.grid(True, which='both', linestyle='--', linewidth=0.5)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left")
        plt.tight_layout()
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
                from telegram_ui import get_keyboard
                keyboard = get_keyboard(state)
                await send_telegram_message(session, state.chat_id, "Fehler beim Senden des Diagramms.", state.bot_token, reply_markup=keyboard)
        buf.close()
        
        # Re-send keyboard after photo
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(session, state.chat_id, "📊", state.bot_token, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Temperaturverlaufs: {e}", exc_info=True)
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(
            session, state.chat_id, f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}", state.bot_token, reply_markup=keyboard
        )

async def get_runtime_bar_chart(session, days=7, state=None):
    """Balkendiagramm der Laufzeiten."""
    try:
        file_path = HEIZUNGSDATEN_CSV
        if not os.path.exists(file_path):
             await send_telegram_message(session, state.chat_id, "Laufzeit-Daten nicht verfügbar (CSV fehlt).", state.bot_token)
             return
        df = pd.read_csv(file_path, parse_dates=["Zeitstempel"])
        df = df.tail(1000)
        df["Date"] = df["Zeitstempel"].dt.date
        df["Kompressor"] = df["Kompressor"].astype(str).map({"EIN": True, "AUS": False, "1": True, "0": False}).fillna(False)
        runtime_by_date = df[df["Kompressor"]].groupby("Date").size() * (10 / 60)
        plt.figure(figsize=(10, 5))
        runtime_by_date.plot(kind="bar")
        plt.xlabel("Datum")
        plt.ylabel("Laufzeit (Minuten)")
        plt.title(f"Kompressor Laufzeit ({days} Tage)")
        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()
        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        form.add_field("photo", buf, filename="runtime.png", content_type="image/png")
        await session.post(url, data=form)
        buf.close()
        
        # Re-send keyboard after photo
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(session, state.chat_id, "📊", state.bot_token, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Error in runtime chart: {e}", exc_info=True)
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(session, state.chat_id, f"❌ Fehler beim Erstellen des Laufzeit-Diagramms: {str(e)[:100]}", state.bot_token, reply_markup=keyboard)
