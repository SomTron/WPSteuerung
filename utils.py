from datetime import datetime, timedelta
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
    "Prognose_Morgen"
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
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(",".join(expected_header) + "\n")
            # Nur Zeilen behalten, die nicht wie ein Header aussehen (Timestamp-Check)
            for line in lines:
                if not line.strip():
                    continue
                parts = line.split(",")
                if parts[0].strip() == expected_header[0]:
                    continue # Überspringe alten Header
                f.write(line)
        logging.info(f"CSV-Header in {file_path} wurde korrigiert.")
        return True
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