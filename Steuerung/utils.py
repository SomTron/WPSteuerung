from datetime import datetime, timedelta
import os
import csv

def rotate_csv(file_path):
    """
    Rolliert die CSV-Datei: Einträge älter als 14 Tage werden in ein Backup verschoben.
    Stream-basierter Ansatz zur Verringerung des Speicherverbrauchs.
    """
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return

    date_format = '%Y-%m-%d'
    cutoff_date = datetime.now() - timedelta(days=14)
    temp_file = file_path + ".tmp"
    backup_file = f"backup_{datetime.now().strftime('%Y-%m-%d')}.csv"
    
    header = None
    rows_to_keep = 0
    rows_to_backup = 0
    
    try:
        # Erster Durchlauf: Finde heraus, wie viele Zeilen wir backupen müssen
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header: return
            
            for row in reader:
                try:
                    # Zeitstempel ist in der ersten Spalte: "YYYY-MM-DD HH:MM:SS"
                    # Wir brauchen nur den Datumsteil
                    row_date_str = row[0].split(" ")[0]
                    row_date = datetime.strptime(row_date_str, date_format)
                    if row_date < cutoff_date:
                        rows_to_backup += 1
                    else:
                        rows_to_keep += 1
                except (IndexError, ValueError):
                    rows_to_keep += 1 # Behalte Zeilen bei Fehlern sicherheitshalber
        
        if rows_to_backup == 0:
            return

        # Zweiter Durchlauf: Backup und Update
        with open(file_path, "r", encoding="utf-8") as f_in:
            reader = csv.reader(f_in)
            next(reader) # Skip header
            
            # Backup schreiben
            if rows_to_backup > 0:
                with open(backup_file, "w", encoding="utf-8", newline="") as f_bak:
                    writer = csv.writer(f_bak)
                    writer.writerow(header)
                    for _ in range(rows_to_backup):
                        writer.writerow(next(reader))
            
            # Neue Datei schreiben (Keepers)
            with open(temp_file, "w", encoding="utf-8", newline="") as f_tmp:
                writer = csv.writer(f_tmp)
                writer.writerow(header)
                for row in reader:
                    writer.writerow(row)
        
        # Atomares Ersetzen
        import shutil
        shutil.move(temp_file, file_path)
        logging.info(f"CSV-Rotation abgeschlossen: {rows_to_backup} Zeilen archiviert.")

    except Exception as e:
        logging.error(f"Fehler bei der CSV-Rotation: {e}")
        if os.path.exists(temp_file): os.remove(temp_file)

# Example usage (you would call this from your main.py or utils.py)
if __name__ == "__main__":
    rotate_csv("your_file.csv")
import pytz
import logging

import shutil
import os
from typing import List

# Erwarteter Header für heizungsdaten.csv (19 Spalten aus main.py)
EXPECTED_CSV_HEADER = [
    "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Boiler", "T_Verd", "Kompressor",
    "ACPower", "FeedinPower", "BatPower", "SOC", "PowerDC1", "PowerDC2", "ConsumeEnergy",
    "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "Urlaubsmodus", "PowerSource",
    "Prognose_Morgen", "Einschaltgrund"
]

HEIZUNGSDATEN_CSV = os.path.join("csv log", "heizungsdaten.csv")

def check_and_fix_csv_header(file_path: str, expected_header: List[str] = None) -> bool:
    """
    Prüft, ob der Header der CSV-Datei korrekt ist, und stellt ihn ggf. wieder her.
    Gibt True zurück, wenn eine Korrektur vorgenommen wurde.
    """
    if expected_header is None:
        expected_header = EXPECTED_CSV_HEADER
    if file_path is None:
        file_path = HEIZUNGSDATEN_CSV
    try:
        if not os.path.exists(file_path):
            return False

        with open(file_path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
            if not first_line:
                return False
            # Header vergleichen (als Liste)
            current_header = [h.strip() for h in first_line.split(",")]
            
            # Robustheits-Check: Wenn Spaltenanzahl stimmt und erste Spalte "Zeitstempel" ist, 
            # akzeptieren wir es vorerst, um unnötige Backups zu vermeiden.
            if len(current_header) == len(expected_header) and current_header[0] == expected_header[0]:
                return False

            if current_header == expected_header:
                return False  # Header ist exakt gleich

        # Header ist falsch: Backup anlegen und korrigieren
        backup_csv(file_path)
        
        # Memory-Safe: Stream processing with temp file
        temp_file = file_path + ".tmp"
        try:
            with open(file_path, "r", encoding="utf-8") as f_in, \
                 open(temp_file, "w", encoding="utf-8") as f_out:
                
                # Write correct header
                f_out.write(",".join(expected_header) + "\n")
                
                # Skip old header if present in first line
                first_line_content = f_in.readline() # We already read this above, but need to consume it or check again.
                # Actually, strictly speaking we just opened a fresh handle f_in.
                # So the first line read here IS the header (or whatever is first).
                
                # Check if the first line looks like the *old* header or just garbage data
                # If it's a data line (starts with timestamp), keep it. 
                # If it starts with "Zeitstempel", skip it.
                if first_line_content.strip() and not first_line_content.startswith(expected_header[0]):
                     f_out.write(first_line_content)
                
                # Stream the rest
                for line in f_in:
                    if not line.strip(): continue
                    # Safety: If another header line appears in middle (concatenated files?), skip it
                    if line.startswith(expected_header[0]): continue
                    f_out.write(line)
            
            # Atomic replace
            shutil.move(temp_file, file_path)
            logging.info(f"CSV-Header in {file_path} wurde korrigiert (Streaming-Modus).")
            return True
            
        except Exception as e:
            logging.error(f"Fehler beim Streaming-Fix: {e}")
            if os.path.exists(temp_file):
                os.remove(temp_file)
            raise e
    except Exception as e:
        logging.error(f"Fehler beim Prüfen/Korrigieren des CSV-Headers: {e}")
        return False

def backup_csv(file_path: str = None, backup_dir: str = "backup") -> str:
    """
    Erstellt ein Backup der CSV-Datei im backup/-Verzeichnis mit Zeitstempel.
    Gibt den Pfad zur Backup-Datei zurück.
    """
    if file_path is None:
        file_path = HEIZUNGSDATEN_CSV
    try:
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        base = os.path.basename(file_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{base}_{timestamp}.bak")
        shutil.copy2(file_path, backup_path)
        logging.info(f"Backup von {file_path} erstellt: {backup_path}")
        return backup_path
    except Exception as e:
        logging.error(f"Fehler beim Backup von {file_path}: {e}")
        return ""

def safe_timedelta(now: datetime, timestamp: datetime, local_tz: pytz.BaseTzInfo, default: timedelta = timedelta()) -> timedelta:
    """
    Berechnet die Zeitdifferenz zwischen zwei Zeitstempeln mit Zeitzonensicherheit.

    Args:
        now: Erster Zeitstempel (meist aktueller Zeitpunkt).
        timestamp: Zweiter Zeitstempel (Vergleichszeitpunkt).
        local_tz: Lokale Zeitzone (z.B. pytz.timezone("Europe/Berlin")).
        default: Standardwert, falls die Berechnung fehlschlägt.

    Returns:
        timedelta: Die berechnete Zeitdifferenz oder der default-Wert bei Fehlern.
    """
    try:
        if now.tzinfo is None:
            now = local_tz.localize(now)
        if timestamp.tzinfo is None:
            timestamp = local_tz.localize(timestamp)
        return now - timestamp
    except Exception as e:
        logging.error(f"Fehler bei safe_timedelta: {e}")
        return default


def safe_float(value, default=0.0, field_name="unknown"):
    """
    Safely convert value to float with comprehensive validation.
    
    Args:
        value: Value to convert (int, float, str, None)
        default: Fallback value if conversion fails
        field_name: Field name for logging
    
    Returns:
        float: Converted value or default
    """
    try:
        if value is None:
            logging.warning(f"API: {field_name} is None, using {default}")
            return default
        
        if isinstance(value, (int, float)):
            return float(value)
        
        if isinstance(value, str):
            value = value.strip()
            if not value or value.lower() in ['n/a', 'null', 'none', 'error', '-']:
                logging.warning(f"API: {field_name}='{value}' invalid, using {default}")
                return default
            return float(value)
        
        logging.error(f"API: {field_name} unexpected type {type(value).__name__}, using {default}")
        return default
    except (ValueError, TypeError) as e:
        logging.error(f"API: Cannot convert {field_name}='{value}': {e}, using {default}")
        return default