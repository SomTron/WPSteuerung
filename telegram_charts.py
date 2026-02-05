import logging
import os
import io
import pandas as pd
import matplotlib.pyplot as plt
import pytz
from datetime import datetime, timedelta
from aiohttp import FormData
from telegram_api import send_telegram_message
from utils import check_and_fix_csv_header, backup_csv, EXPECTED_CSV_HEADER

def prefilter_csv_lines(file_path, days, tz):
    now = datetime.now(tz)
    start_date = (now - timedelta(days=days - 1)).date()
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
                    timestamp_str = parts[0].strip()
                    
                    # Parse timestamp more safely
                    try:
                        # Try standard format first
                        timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                        timestamp = tz.localize(timestamp)
                    except ValueError:
                        # Fallback to pandas if standard format fails
                        try:
                            timestamp = pd.to_datetime(timestamp_str)
                            if pd.isna(timestamp):
                                continue
                            if timestamp.tzinfo is None:
                                timestamp = tz.localize(timestamp)
                            else:
                                timestamp = timestamp.astimezone(tz)
                        except:
                            continue
                    
                    if start_date <= timestamp.date() <= now.date():
                        relevant_lines.append(line.strip())
                except Exception as e:
                    # Silently skip bad lines
                    continue
        logging.debug(f"✅ {len(relevant_lines)} Zeilen nach Vorfilterung.")
        return relevant_lines
    except Exception as e:
        logging.error(f"❌ Fehler beim Lesen der CSV-Zeilen: {e}", exc_info=True)
        return []

async def get_boiler_temperature_history(session, hours, state, config):
    """Erstellt und sendet ein Diagramm mit Temperaturverlauf."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)
        file_path = "heizungsdaten.csv"
        if not os.path.isfile(file_path):
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, "CSV-Datei nicht gefunden.", state.bot_token, reply_markup=keyboard)
            return

        check_and_fix_csv_header(file_path)
        backup_csv(file_path)

        # Use prefilter for performance
        relevant_lines = prefilter_csv_lines(file_path, int(hours/24) + 1 if hours >= 24 else 1, local_tz)
        if len(relevant_lines) <= 1:  # Only header
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, f"Keine Daten für die letzten {hours} Stunden.", state.bot_token, reply_markup=keyboard)
            return
            
        from io import StringIO
        df = pd.read_csv(StringIO("\n".join(relevant_lines)), sep=",")
        usecols = [c for c in ["Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Verd", "Kompressor", "PowerSource", "Einschaltpunkt", "Ausschaltpunkt"] if c in df.columns]
        df = df[usecols]

        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
        df = df[df["Zeitstempel"].notna()].copy()
        df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz, ambiguous='infer', nonexistent='shift_forward')
        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]

        if df.empty:
            from telegram_ui import get_keyboard
            keyboard = get_keyboard(state)
            await send_telegram_message(session, state.chat_id, f"Keine Daten für die letzten {hours} Stunden.", state.bot_token, reply_markup=keyboard)
            return

        temp_columns = ["T_Oben", "T_Unten", "T_Mittig", "T_Verd"]
        for col in temp_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        y_min = max(0, df[temp_columns].min().min() - 2)
        y_max = df[temp_columns].max().max() + 5

        plt.figure(figsize=(12, 6))
        ax = plt.gca()
        
        # Power source background coloring (MUST be done first, before line plots)
        color_map = {"Solar": "green", "Batterie": "yellow", "Netz": "red"}
        
        if "Kompressor" in df.columns and "PowerSource" in df.columns:
            # Convert Kompressor to boolean
            df["Kompressor_Bool"] = df["Kompressor"].astype(str).str.strip().map({"EIN": True, "AUS": False, "1": True, "0": False}).fillna(False)
            
            # Fill background for each power source
            for source, color in color_map.items():
                mask = (df["PowerSource"].astype(str).str.strip() == source) & df["Kompressor_Bool"]
                if mask.any():
                    ax.fill_between(df["Zeitstempel"], y_min, y_max, where=mask, color=color, alpha=0.25, label=f"{source} (Kompressor ON)")
                    logging.debug(f"Filled {mask.sum()} points with {source} color")

        # Temperature line plots (drawn on top of background)
        for col, color, linestyle in [("T_Oben", "blue", "-"), ("T_Unten", "red", "-"), ("T_Mittig", "purple", "-"), ("T_Verd", "gray", "--")]:
            if col in df.columns:
                ax.plot(df["Zeitstempel"], df[col], label=col, color=color, linestyle=linestyle, linewidth=1.5)

        ax.set_xlim(time_ago, now)
        ax.set_ylim(y_min, y_max)
        ax.legend(loc="upper left", fontsize=8)
        ax.set_xlabel("Zeit")
        ax.set_ylabel("Temperatur (°C)")
        ax.set_title(f"Temperaturverlauf ({hours}h)")
        ax.grid(True, alpha=0.3)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", state.chat_id)
        form.add_field("caption", f"📈 Verlauf {hours}h")
        form.add_field("photo", buf, filename="temp_graph.png", content_type="image/png")
        await session.post(url, data=form, timeout=30)
        buf.close()
        
        # Re-send keyboard after photo
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(session, state.chat_id, "📊", state.bot_token, reply_markup=keyboard)
    except Exception as e:
        logging.error(f"Error in charts: {e}", exc_info=True)
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(session, state.chat_id, f"❌ Fehler beim Erstellen des Diagramms: {str(e)[:100]}", state.bot_token, reply_markup=keyboard)

async def get_runtime_bar_chart(session, days=7, state=None):
    """Balkendiagramm der Laufzeiten."""
    try:
        local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(local_tz)
        today = now.date()
        start_date = today - timedelta(days=days - 1)
        file_path = "heizungsdaten.csv"

        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            header = next(f).strip().split(",")
            for line in f:
                parts = line.strip().split(",")
                try:
                    ts = datetime.fromisoformat(parts[0])
                    if start_date <= ts.date() <= today:
                        rows.append(parts)
                except: continue

        if not rows: return
        df = pd.DataFrame(rows, columns=header)
        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"]).dt.tz_localize(local_tz, ambiguous='infer')
        df["Datum"] = df["Zeitstempel"].dt.date
        df["Kompressor"] = df["Kompressor"].astype(str).str.strip().replace({"EIN": 1, "AUS": 0}).astype(int)
        
        active = df[df["Kompressor"] == 1].copy()
        map_src = {"Direkter PV-Strom": "PV", "Solar": "PV", "Strom aus der Batterie": "Battery", "Batterie": "Battery", "Strom vom Netz": "Grid", "Netz": "Grid"}
        active["Kategorie"] = active["PowerSource"].map(map_src).fillna("Unbekannt")
        
        runtime_hours = active.groupby(["Datum", "Kategorie"]).size().unstack(fill_value=0) / 60.0
        date_range = pd.date_range(start_date, today).date
        runtime_hours = runtime_hours.reindex(date_range, fill_value=0)
        
        for c in ["Unbekannt", "PV", "Battery", "Grid"]:
            if c not in runtime_hours.columns: runtime_hours[c] = 0.0
        
        runtime_hours = runtime_hours[["Unbekannt", "PV", "Battery", "Grid"]]
        plt.figure(figsize=(10, 6))
        # Simplified plotting logic
        bottom = pd.Series(0.0, index=date_range)
        colors = {"Unbekannt": "gray", "PV": "green", "Battery": "orange", "Grid": "red"}
        for cat in runtime_hours.columns:
            plt.bar(date_range, runtime_hours[cat], bottom=bottom, label=cat, color=colors[cat])
            bottom += runtime_hours[cat]
        
        plt.xticks(rotation=45)
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
    except Exception as e:
        logging.error(f"Error in runtime chart: {e}")
