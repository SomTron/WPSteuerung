import pandas as pd
from datetime import datetime
import io
import os
import logging
from typing import Optional, List

def read_history_data(file_path: str, hours: int):
    """
    Effizientes Einlesen der letzten X Stunden aus der CSV-Datei.
    Verwendet Datei-Seek, um nur das Ende der Datei zu lesen.
    """
    if not os.path.exists(file_path):
        return {"data": [], "count": 0}

    try:
        file_size = os.path.getsize(file_path)
        read_size = 4 * 1024 * 1024  # Letzte 4MB lesen (reicht meist für Tage/Wochen)
        
        with open(file_path, "rb") as f:
            if file_size > read_size:
                f.seek(-read_size, os.SEEK_END)
                f.readline() # Diskartiere unvollständige erste Zeile
            else:
                f.readline() # Header überspringen, da er unten explizit hinzugefügt wird
            content = f.read().decode("utf-8", errors="ignore")
            
        # Wir brauchen den Header für Pandas
        with open(file_path, "r", encoding="utf-8") as f:
            header = f.readline()
            
        data_io = io.StringIO(header + content)
        
        # Einlesen mit c-engine für Performance, fehlerhafte Zeilen ignorieren
        usecols = ["Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Verd", "T_Vorlauf", "Kompressor"]
        
        # Check if all usecols exist in header
        header_cols = [c.strip() for c in header.split(",")]
        actual_cols = [c for c in usecols if c in header_cols]

        df = pd.read_csv(data_io, engine="c", sep=",", on_bad_lines='skip', usecols=actual_cols)
        
        if df.empty or "Zeitstempel" not in df.columns:
            return {"data": [], "count": 0}

        # Filtern auf die letzten X Stunden
        df['Zeitstempel'] = pd.to_datetime(df['Zeitstempel'], errors='coerce')
        df = df[df['Zeitstempel'].notna()].copy()
        
        cutoff = datetime.now() - pd.Timedelta(hours=hours)
        df = df[df['Zeitstempel'] >= cutoff]
        
        data = []
        for _, row in df.iterrows():
            item = {
                "timestamp": row['Zeitstempel'].strftime("%Y-%m-%d %H:%M:%S")
            }
            # Nur vorhandene Spalten hinzufügen
            if "T_Oben" in df.columns: item["t_oben"] = float(row["T_Oben"]) if pd.notna(row["T_Oben"]) else None
            if "T_Mittig" in df.columns: item["t_mittig"] = float(row["T_Mittig"]) if pd.notna(row["T_Mittig"]) else None
            if "T_Unten" in df.columns: item["t_unten"] = float(row["T_Unten"]) if pd.notna(row["T_Unten"]) else None
            if "T_Verd" in df.columns: item["t_verd"] = float(row["T_Verd"]) if pd.notna(row["T_Verd"]) else None
            if "Kompressor" in df.columns: item["kompressor"] = str(row["Kompressor"]) if pd.notna(row["Kompressor"]) else None
            data.append(item)
            
        return {"data": data, "count": len(data)}
    except Exception as e:
        logging.error(f"Fehler beim effizienten Lesen der Historie: {e}")
        return {"data": [], "count": 0}

def estimate_heating_rate_from_csv(csv_path: str, lookback_hours: int = 72) -> Optional[float]:
    """
    Analysiert die CSV-Historie nach Heizzyklen und schätzt die durchschnittliche Aufheizrate (°C/h).
    """
    if not os.path.exists(csv_path):
        return None
        
    try:
        # Wir nutzen read_history_data um die Daten zu bekommen
        hist = read_history_data(csv_path, lookback_hours)
        df_list = hist.get("data", [])
        if not df_list or len(df_list) < 10:
            return None
            
        import pandas as pd
        df = pd.DataFrame(df_list)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        rates = []
        in_cycle = False
        start_t = 0
        start_time = None
        
        # Wir nutzen 't_mittig' als Referenz
        if 't_mittig' not in df.columns or 'kompressor' not in df.columns:
            return None

        for _, row in df.iterrows():
            is_on = str(row['kompressor']).upper() == 'EIN'
            curr_t = row['t_mittig']
            curr_time = row['timestamp']
            
            if curr_t is None or pd.isna(curr_t): continue

            if is_on and not in_cycle:
                in_cycle = True
                start_t = curr_t
                start_time = curr_time
            elif not is_on and in_cycle:
                in_cycle = False
                duration_h = (curr_time - start_time).total_seconds() / 3600.0
                delta_t = curr_t - start_t
                
                if duration_h > 0.33 and delta_t > 0.5:
                    rates.append(delta_t / duration_h)
        
        if not rates:
            return None
            
        avg_rate = sum(rates) / len(rates)
        logging.info(f"Historische Aufheizrate aus CSV geschätzt: {avg_rate:.2f}°C/h (Basis: {len(rates)} Zyklen)")
        return avg_rate
        
    except Exception as e:
        logging.error(f"Fehler bei Aufheizrate-Schätzung aus CSV: {e}")
        return None

def estimate_heating_rate_from_csv(csv_path: str, lookback_hours: int = 72) -> Optional[float]:
    """
    Analysiert die CSV-Historie nach Heizzyklen und schätzt die durchschnittliche Aufheizrate (°C/h).
    """
    if not os.path.exists(csv_path):
        return None
        
    try:
        # Wir nutzen read_history_data um die Daten zu bekommen
        hist = read_history_data(csv_path, lookback_hours)
        df_list = hist.get("data", [])
        if not df_list or len(df_list) < 10:
            return None
            
        import pandas as pd
        df = pd.DataFrame(df_list)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        rates = []
        in_cycle = False
        start_t = 0
        start_time = None
        
        # Wir nutzen 't_mittig' als Referenz
        if 't_mittig' not in df.columns or 'kompressor' not in df.columns:
            return None

        for _, row in df.iterrows():
            is_on = str(row['kompressor']).upper() == 'EIN'
            curr_t = row['t_mittig']
            curr_time = row['timestamp']
            
            if curr_t is None: continue

            if is_on and not in_cycle:
                in_cycle = True
                start_t = curr_t
                start_time = curr_time
            elif not is_on and in_cycle:
                in_cycle = False
                duration_h = (curr_time - start_time).total_seconds() / 3600.0
                delta_t = curr_t - start_t
                
                if duration_h > 0.33 and delta_t > 0.5:
                    rates.append(delta_t / duration_h)
        
        if not rates:
            return None
            
        avg_rate = sum(rates) / len(rates)
        logging.info(f"Historische Aufheizrate aus CSV geschätzt: {avg_rate:.2f}°C/h (Basis: {len(rates)} Zyklen)")
        return avg_rate
        
    except Exception as e:
        logging.error(f"Fehler bei Aufheizrate-Schätzung aus CSV: {e}")
        return None
