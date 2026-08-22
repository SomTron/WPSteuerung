from datetime import datetime, timedelta
import pytz
import logging

import shutil
import os
from typing import List

# Erwarteter Header für heizungsdaten.csv (20 Spalten aus main.py)
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
                    if not line.strip():
                        continue
                    # Safety: If another header line appears in middle (concatenated files?), skip it
                    if line.startswith(expected_header[0]):
                        continue
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


# ── CSV-Monatsrotation ────────────────────────────────────────────────

def _parse_zeitstempel_aus_csv_zeile(zeile: str):
    """Erste Spalte einer Datenzeile als datetime parsen (oder None)."""
    try:
        return datetime.strptime(zeile.split(",")[0].strip(), "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError, AttributeError):
        return None


def _letzter_csv_zeitstempel(csv_path: str):
    """Letzten parsebaren Zeitstempel lesen (nur ~4KB vom Dateiende)."""
    groesse = os.path.getsize(csv_path)
    with open(csv_path, "rb") as f:
        f.seek(max(0, groesse - 4096))
        tail = f.read().decode("utf-8", errors="replace")
    zeilen = [z for z in tail.split("\n") if z.strip()]
    if groesse > 4096 and len(zeilen) > 1:
        zeilen = zeilen[1:]  # evtl. angeschnittene erste Zeile verwerfen
    for zeile in reversed(zeilen):
        ts = _parse_zeitstempel_aus_csv_zeile(zeile)
        if ts:
            return ts
    return None


def rotiere_csv_monatlich(csv_path: str = None, heute: datetime = None):
    """Archiviert heizungsdaten.csv nach Monatswechsel (Schutz gegen Endwachstum).

    Stammt der letzte Eintrag aus einem frueheren Monat als 'heute', wird die
    Datei zu heizungsdaten_YYYY-MM.csv umbenannt (os.replace = atomar). Die
    neue Datei legt der naechste Schreibvorgang in main.log_system_state
    automatisch inklusive Header an.

    Args:
        csv_path: Pfad zur CSV (Default: HEIZUNGSDATEN_CSV).
        heute: Referenzzeitpunkt, fuer Tests injizierbar (Default: jetzt).

    Returns:
        Archiv-Pfad bei Rotation, sonst None.
    """
    if csv_path is None:
        csv_path = HEIZUNGSDATEN_CSV
    if heute is None:
        heute = datetime.now()
    try:
        if not os.path.exists(csv_path):
            return None
        letzter = _letzter_csv_zeitstempel(csv_path)
        if letzter is None:
            return None
        if (letzter.year, letzter.month) == (heute.year, heute.month):
            return None

        verzeichnis = os.path.dirname(csv_path)
        basis = os.path.splitext(os.path.basename(csv_path))[0]
        archiv_pfad = os.path.join(verzeichnis, f"{basis}_{letzter.strftime('%Y-%m')}.csv")
        if os.path.exists(archiv_pfad):
            # Archiv existiert bereits -> eindeutigen Namen verwenden
            archiv_pfad = os.path.join(
                verzeichnis,
                f"{basis}_{letzter.strftime('%Y-%m')}_{datetime.now().strftime('%H%M%S')}.csv",
            )
        os.replace(csv_path, archiv_pfad)
        logging.info(f"CSV-Monatsrotation: {csv_path} -> {archiv_pfad}")
        return archiv_pfad
    except Exception as e:
        logging.error(f"Fehler bei CSV-Monatsrotation ({csv_path}): {e}")
        return None


def relevante_csv_dateien(csv_path: str = None, jetzt: datetime = None) -> List[str]:
    """Aktuelle CSV + Archiv des Vormonats (falls vorhanden), chronologisch.

    Damit Charts auch kurz nach einem Monatswechsel die letzten Stunden des
    Vormonats anzeigen koennen, ohne je die komplette Historie zu laden.
    """
    if csv_path is None:
        csv_path = HEIZUNGSDATEN_CSV
    if jetzt is None:
        jetzt = datetime.now()
    dateien: List[str] = []
    vormonat = (jetzt.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    verzeichnis = os.path.dirname(csv_path)
    basis = os.path.splitext(os.path.basename(csv_path))[0]
    kandidat = os.path.join(verzeichnis, f"{basis}_{vormonat}.csv")
    if os.path.exists(kandidat):
        dateien.append(kandidat)
    if os.path.exists(csv_path):
        dateien.append(csv_path)
    return dateien
