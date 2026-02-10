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
import asyncio

def _plot_temperature_history(hours, bot_token, chat_id, local_tz):
    """Synchronous helper for plotting temperature history (CPU/IO intensive)."""
    try:
        now = datetime.now(local_tz)
        time_ago = now - timedelta(hours=hours)
        file_path = HEIZUNGSDATEN_CSV
        
        if not os.path.isfile(file_path):
            return None, f"CSV-Datei nicht gefunden: {file_path}"

        check_and_fix_csv_header(file_path)
        
        # Optimize: Read only necessary part of the file (tail reading)
        file_size = os.path.getsize(file_path)
        read_size = 4 * 1024 * 1024  # 4MB
        
        with open(file_path, "rb") as f:
            if file_size > read_size:
                f.seek(-read_size, os.SEEK_END)
                f.readline() # discard partial line
            content = f.read().decode("utf-8", errors="ignore")
        
        header = ",".join(EXPECTED_CSV_HEADER) + "\n"
        data_io = io.StringIO(header + content)
        
        # Fast C engine, skip bad lines, only relevant columns
        usecols = [c for c in ["Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Verd", "Kompressor", "PowerSource", "Einschaltpunkt", "Ausschaltpunkt"] if c in EXPECTED_CSV_HEADER]
        df = pd.read_csv(data_io, engine="c", sep=",", on_bad_lines='skip', usecols=usecols)
        
        if df.empty or "Zeitstempel" not in df.columns:
            return None, "Keine validen Daten gefunden."

        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
        df = df[df["Zeitstempel"].notna()].copy()
        
        # Localize and filter
        df["Zeitstempel"] = df["Zeitstempel"].dt.tz_localize(local_tz, ambiguous='infer', nonexistent='shift_forward')
        df = df[(df["Zeitstempel"] >= time_ago) & (df["Zeitstempel"] <= now)]
        
        if df.empty:
            return None, "Keine Daten im gewählten Zeitraum."

        # Plotting
        temp_columns = ["T_Oben", "T_Unten", "T_Mittig", "T_Verd"]
        for col in temp_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        
        min_temp = df[temp_columns].min().min()
        max_temp = df[temp_columns].max().max()
        y_min = max(0, min_temp - 2) if pd.notna(min_temp) else 0
        y_max = (max_temp + 5) if pd.notna(max_temp) else 60

        plt.figure(figsize=(12, 6))
        color_map = {
            "Direkter PV-Strom": "green", "Solar": "green",
            "Strom aus der Batterie": "yellow", "Batterie": "yellow",
            "Strom vom Netz": "red", "Netz": "red",
            "Keine aktive Energiequelle": "blue", "Unbekannt": "gray"
        }
        
        shown_labels = set()
        if "Kompressor" in df.columns and "PowerSource" in df.columns:
            df["Kompressor"] = df["Kompressor"].astype(str).map({
                "EIN": True, "AUS": False, "1": True, "0": False, "1.0": True, "0.0": False
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

        plot_configs = [
            ("T_Oben", "blue", "-"), ("T_Unten", "red", "-"),
            ("T_Mittig", "purple", "-"), ("T_Verd", "gray", "--")
        ]
        for col, color, linestyle in plot_configs:
            if col in df.columns and df[col].notna().any():
                plt.plot(df["Zeitstempel"], df[col], label=col, color=color, linestyle=linestyle, linewidth=1.2)

        if "Einschaltpunkt" in df.columns:
            df["Einschaltpunkt"] = pd.to_numeric(df["Einschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Einschaltpunkt"], linestyle="--", color="green", alpha=0.5)
        if "Ausschaltpunkt" in df.columns:
            df["Ausschaltpunkt"] = pd.to_numeric(df["Ausschaltpunkt"], errors="coerce").ffill()
            plt.plot(df["Zeitstempel"], df["Ausschaltpunkt"], linestyle="--", color="orange", alpha=0.5)

        plt.xlim(time_ago, now)
        plt.ylim(y_min, y_max)
        plt.title(f"Boiler-Temperaturverlauf – Letzte {hours} Stunden")
        plt.grid(True, linestyle='--', linewidth=0.5)
        plt.xticks(rotation=45)
        plt.legend(loc="lower left", fontsize='small')
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        buf.seek(0)
        plt.close()
        return buf, None
    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Verlaufs: {e}", exc_info=True)
        return None, str(e)

async def get_boiler_temperature_history(session, hours, state, config):
    """Erstellt und sendet ein Diagramm (Async Wrapper um Thread-Worker)."""
    local_tz = pytz.timezone("Europe/Berlin")
    
    # Run heavy plotting in a separate thread to avoid blocking the event loop
    buf, error = await asyncio.to_thread(_plot_temperature_history, hours, state.bot_token, state.chat_id, local_tz)
    
    if error:
        from telegram_ui import get_keyboard
        await send_telegram_message(session, state.chat_id, f"❌ Fehler: {error}", state.bot_token, reply_markup=get_keyboard(state))
        return

    try:
        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", str(state.chat_id))
        caption = f"📈 Verlauf {hours}h | T_Oben=blau, T_Unten=rot, T_Mittig=lila, T_Verd=grau--"
        form.add_field("caption", caption)
        form.add_field("photo", buf, filename="temp_history.png", content_type="image/png")
        
        async with session.post(url, data=form, timeout=30) as resp:
            if resp.status != 200:
                logging.error(f"Telegram API Error: {resp.status} - {await resp.text()}")
    except Exception as e:
        logging.error(f"Fehler beim Erstellen des Temperaturverlaufs: {e}", exc_info=True)
        from telegram_ui import get_keyboard
        keyboard = get_keyboard(state)
        await send_telegram_message(
            session, state.chat_id, f"Fehler beim Abrufen des {hours}h-Verlaufs: {str(e)}", state.bot_token, reply_markup=keyboard
        )

def _plot_runtime_chart(days, local_tz):
    """Synchronous helper for plotting runtime chart."""
    try:
        now = datetime.now(local_tz)
        cutoff_date = (now - timedelta(days=days)).date()
        file_path = HEIZUNGSDATEN_CSV
        
        if not os.path.exists(file_path):
            return None, "CSV fehlt."
            
        file_size = os.path.getsize(file_path)
        read_size = 15 * 1024 * 1024  # 15MB should be plenty for weeks
        
        with open(file_path, "rb") as f:
            if file_size > read_size:
                f.seek(-read_size, os.SEEK_END)
                f.readline()
            content = f.read().decode("utf-8", errors="ignore")
            
        header = ",".join(EXPECTED_CSV_HEADER) + "\n"
        data_io = io.StringIO(header + content)
        df = pd.read_csv(data_io, engine="c", sep=",", on_bad_lines='skip', usecols=["Zeitstempel", "Kompressor"])
        
        if df.empty or "Zeitstempel" not in df.columns:
            return None, "Keine Daten."

        df["Zeitstempel"] = pd.to_datetime(df["Zeitstempel"], errors='coerce')
        df = df[df["Zeitstempel"].notna()].copy()
        df = df[df["Zeitstempel"].dt.date >= cutoff_date]
        
        if df.empty:
            return None, "Zeitraum leer."

        df["Date"] = df["Zeitstempel"].dt.date
        df["Kompressor"] = df["Kompressor"].astype(str).map({
            "EIN": True, "AUS": False, "1": True, "0": False, "1.0": True, "0.0": False
        }).fillna(False)
        
        runtime_by_date = df[df["Kompressor"]].groupby("Date").size() * (10 / 60)
        
        all_dates = pd.date_range(start=cutoff_date, end=now.date()).date
        runtime_series = pd.Series(0.0, index=all_dates)
        runtime_series.update(runtime_by_date)
        
        plt.figure(figsize=(10, 5))
        ax = runtime_series.plot(kind="bar", color="skyblue", edgecolor="navy")
        plt.xlabel("Datum")
        plt.ylabel("Laufzeit (Minuten)")
        plt.title(f"Kompressor Laufzeit (letzte {days} Tage)")
        plt.xticks(rotation=45)
        for idx, val in enumerate(runtime_series):
            if val > 0:
                ax.text(idx, val + 0.5, f"{val:.0f}", ha='center', va='bottom', fontsize=9)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        buf.seek(0)
        plt.close()
        return buf, None
    except Exception as e:
        logging.error(f"Fehler im Laufzeit-Worker: {e}", exc_info=True)
        return None, str(e)

async def get_runtime_bar_chart(session, days=7, state=None):
    """Balkendiagramm der Laufzeiten (Async Wrapper)."""
    local_tz = pytz.timezone("Europe/Berlin")
    
    buf, error = await asyncio.to_thread(_plot_runtime_chart, days, local_tz)
    
    if error:
        from telegram_ui import get_keyboard
        await send_telegram_message(session, state.chat_id, f"❌ Fehler: {error}", state.bot_token, reply_markup=get_keyboard(state))
        return

    try:
        url = f"https://api.telegram.org/bot{state.bot_token}/sendPhoto"
        form = FormData()
        form.add_field("chat_id", str(state.chat_id))
        form.add_field("photo", buf, filename="runtime.png", content_type="image/png")
        form.add_field("caption", f"📊 Kompressor-Laufzeiten der letzten {days} Tage (in Min.)")
        
        async with session.post(url, data=form, timeout=30) as resp:
            if resp.status != 200:
                logging.error(f"Error sending runtime chart: {resp.status}")
    finally:
        buf.close()
