import csv

# Pfad zur CSV-Datei
input_file = 'heizungsdaten.csv'
output_file = 'heizungsdaten.csv'

# Überschriften für die fehlenden Spalten
missing_headers = ['Einschaltpunkt', 'Ausschaltpunkt', 'Solarüberschuss', 'Nachtabsenkung']

# Öffnen der Eingabedatei und Lesen der Daten
with open(input_file, mode='r', newline='', encoding='utf-8') as infile:
    reader = csv.reader(infile)
    rows = list(reader)

# Überprüfen, ob die Überschriften fehlen
if len(rows[0]) < len(missing_headers) + len(rows[0]):
    # Füge die fehlenden Überschriften hinzu
    rows[0].extend(missing_headers)

# Auffüllen der fehlenden Daten am Anfang der Aufzeichnung
for row in rows[1:]:
    if len(row) < len(rows[0]):
        row.extend([None] * (len(rows[0]) - len(row)))

# Schreiben der bearbeiteten Daten in die Ausgabedatei
with open(output_file, mode='w', newline='', encoding='utf-8') as outfile:
    writer = csv.writer(outfile)
    writer.writerows(rows)

print(f"Die bearbeitete Datei wurde als {output_file} gespeichert.")