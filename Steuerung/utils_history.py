import pandas as pd
from datetime import datetime
import io
import os
import logging

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
