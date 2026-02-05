import csv
import shutil
import os
import sys

# Define correct header (20 columns)
HEADER = [
    "Zeitstempel", "T_Oben", "T_Unten", "T_Mittig", "T_Boiler", "T_Verd", "Kompressor",
    "ACPower", "FeedinPower", "BatPower", "SOC", "PowerDC1", "PowerDC2", "ConsumeEnergy",
    "Einschaltpunkt", "Ausschaltpunkt", "Solarüberschuss", "Urlaubsmodus", "PowerSource",
    "Prognose_Morgen"
]

INPUT_FILE = "csv log/heizungsdaten.csv"
BACKUP_FILE = "csv log/heizungsdaten.csv.bak_corruption_fix"
OUTPUT_FILE = "csv log/heizungsdaten_fixed.csv"

def fix_csv():
    if not os.path.exists(INPUT_FILE):
        print(f"Datei nicht gefunden: {INPUT_FILE}")
        # Versuch absoluten Pfad falls im falschen cwd
        if os.path.exists(os.path.join("VPSteuerung", INPUT_FILE)):
             print("Pfad angepasst...")
        return

    print(f"Erstelle Backup: {BACKUP_FILE}")
    try:
        shutil.copy2(INPUT_FILE, BACKUP_FILE)
    except Exception as e:
        print(f"Backup fehlgeschlagen: {e}")
        return

    print("Starte Reparatur...")
    good_lines = 0
    bad_lines = 0
    fixed_lines = 0
    
    with open(INPUT_FILE, "r", encoding="utf-8", errors="replace") as f_in, \
         open(OUTPUT_FILE, "w", encoding="utf-8", newline="") as f_out:
        
        reader = csv.reader(f_in)
        writer = csv.writer(f_out)
        
        # Write Header
        writer.writerow(HEADER)
        
        for i, row in enumerate(reader):
            if not row: continue
            
            # Skip old header lines found in middle of file
            if row[0] == "Zeitstempel":
                continue

            if len(row) == 20:
                writer.writerow(row)
                good_lines += 1
            elif len(row) == 19:
                # Old format, missing Prognose
                row.append("0.0")
                writer.writerow(row)
                fixed_lines += 1
            else:
                # Garbage or merge error
                bad_lines += 1
                if bad_lines < 5:
                    print(f"Verwerfe Zeile {i+1}: {len(row)} Spalten (Erwartet: 20). Inhalt Start: {row[:3]}")

    print("-" * 30)
    print(f"Fertig!")
    print(f"Gute Zeilen übernommen: {good_lines}")
    print(f"Alte Zeilen repariert (Prognose=0.0): {fixed_lines}")
    print(f"Defekte Zeilen verworfen: {bad_lines}")
    print("-" * 30)
    
    # Replace original
    try:
        shutil.move(OUTPUT_FILE, INPUT_FILE)
        print(f"Originaldatei {INPUT_FILE} wurde erfolgreich überschrieben.")
    except Exception as e:
        print(f"Fehler beim Ersetzen der Datei: {e}")
        print(f"Die reparierte Datei liegt hier: {OUTPUT_FILE}")

if __name__ == "__main__":
    fix_csv()
