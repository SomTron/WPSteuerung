# 🌡️ WPSteuerung - Intelligente Wärmepumpensteuerung

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![CI](https://github.com/SomTron/WPSteuerung/actions/workflows/ci.yml/badge.svg)](https://github.com/SomTron/WPSteuerung/actions/workflows/ci.yml)

Eine umfassende Open-Source-Lösung zur Steuerung und Optimierung von Wärmepumpen/Heizungsanlagen auf Basis eines Raspberry Pi. Das System integriert Echtzeit-Sensorik, Solar-Überschuss-Optimierung (SolaxCloud) und eine komfortable Fernsteuerung via Telegram.

---

## 🚀 Hauptfunktionen

- **Intelligente Temperaturregelung**: Überwachung von bis zu 4 Sensoren (Oben, Mittig, Unten, Verdampfer) via DS18B20 (1-Wire).
- **🔋 Solar-Überschuss-Optimierung**: Automatische Erhöhung der Sollwerte bei PV-Überschuss oder vollem Akku (Integration mit SolaxCloud).
- **🤖 Telegram-Interface**: Fernsteuerung und Statusabfragen direkt via Messenger. Inklusive grafischer Darstellung (Matplotlib) der Temperaturverläufe (6h/24h) und Tages-Laufzeiten.
- **🛡️ Sicherheit & Hardware-Schutz**: 
    - Berücksichtigung von Mindestlaufzeiten und Mindestpausen.
    - Überwachung des Druckschalters (GPIO).
    - Lokale LCD-Anzeige (20x4 I2C) für schnellen Status-Check vor Ort.
    - **Vorausschauender Overshoot-Schutz**: EIN-Antizipation (Hub/Rate vs. Mindestlaufzeit) und Abschalt-Prognose vor der Obergrenze verhindern Kurzläufe und die alte „Boiler-Max“-Kühlphase.
- **📅 Betriebsmodi**: Normal, Nachtabsenkung, PV-Boost, Bademodus (erhöhter Bedarf) und Urlaubsmodus (Energiesparen).
- **🧠 Selbstlernend**: Heizraten (saisonal), Zapf-Zeiten, Forecast-Kalibrierung und Stunden-Surplus-Profil; „ZU-FRUEH“-Erkennung bewertet verpasste PV-Wh.
- **☀️ PV-optimiert**: PV-Warten der Abweichungs-Regel bei guter Tagesprognose (kein Netz-Heizen am Morgen), PV-Weiterlauf-Band gegen Kurzzyklen, Hysterese-Sparen.
- **📊 Daten-Logging**: Kontinuierliches Logging aller Messwerte (oben/mittig/unten/Verdampfer) im 10-Sekunden-Takt; monatliche CSV-Rotation verhindert unbegrenztes Wachstum. Jeder Neustart schreibt die **GitHub-Revision** (Commit) ins Log.
- **🔄 Zyklus-Historie**: Nach jedem realen Kompressorlauf wird sofort `Steuerung/csv log/zyklen.csv` fortgeschrieben – inklusive Start-/Endzeit, Dauer, Stromquelle, Regel, Abschaltgrund und Temperaturen oben/mittig/unten (Start + Maxima).
- **🕵️ Automatische Log-Analyse**: Der systemd-Timer `wp-analyse.timer` aktualisiert täglich den vollständigen Analysereport (Tages-KPIs, Zyklen, Overshoot, Morgen-Netzbezug, Stale-Phasen) plus CSV-Dateien.

---

## 🕵️ Log-Analyse & Versionierung

Jede Logdatei beginnt beim Neustart mit einer Versionszeile:
```
INFO - Start WPSteuerung | GitHub-Rev: a1b2c3d [branch] Commit-Betreff
```
Damit ist später eindeutig rekonstruierbar, mit welcher Code-Version ein Log erzeugt wurde.

**Analysescript** (`Analyse/log_analyse.py`, nur Standardbibliothek):
```bash
python3 Analyse/log_analyse.py
# erzeugt logs/analyse_*:
#   analyse_bericht.md       - Report (Tages-KPIs, Zyklen, Stale, Regeln)
#   zyklen.csv               - alle Kompressor-Zyklen (Quelle, Dauer, Endgrund)
#   entscheidungen_kontext.csv - jede Regelbewertung mit PV/SOC/Einspeisung
#   morgen_netzbezug.csv     - morgendlicher Netzbezug vs. Tagesprognose
#   ereignisse.csv           - Warnungen/Fehler mit stabilen Codes
```

**Log-Anreicherungen** (seit der Log-Review):
- **Kompakt-Log**: Die volle 15-Zeilen-Regelbewertung erscheint nur noch bei Entscheidungs-Wechsel oder als 60-Min-Snapshot.
- **Status-Zeile** mit PV, Einspeisung, SOC und Datenalter; veraltete Daten werden als `| STALE` markiert.
- **Eindeutige Abschalt-Codes**: `Kompressor AUS (cycle=7) reason=regel_aus …`
- **Sensor-Degradation**: steigende Lesefehler-Rate warnt frühzeitig (Verkabelung/Sensor).

---

## 🛠️ Hardware-Anforderungen

- **Raspberry Pi** (getestet auf Pi Zero 2 W und Pi 3/4)
- **Temperatursensoren**: DS18B20 (1-Wire)
- **Display**: LCD 20x4 mit I2C-Rucksack (PCF8574)
- **GPIO-Anbindung**: Relais für Kompressor-Steuerung, Optokoppler für Druckschalter.

---

## 📂 Projektstruktur

Das Projekt ist in funktionale Bereiche unterteilt:
- **`Steuerung/`**: Der Kern der Wärmepumpensteuerung (Logik, Hardware, Telegram-Bot).
- **`Updater/`**: Tools für Deployment und Fernwartung auf dem Raspberry Pi.
- **`Analyse/`**: (Neu) Bereich für Daten-Auswertungen und Langzeit-Statistiken.

---

## ⚙️ Installation & Setup

### 1. Repository klonen
```bash
git clone https://github.com/SomTron/WPSteuerung.git
cd WPSteuerung
```

### 2. Virtual Environment einrichten
```bash
# In WPSteuerung/
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-pi.txt
```

### 3. Konfiguration
Kopiere die Beispiel-Konfiguration in den Steuerungs-Ordner und passe sie an:
```bash
cp Steuerung/config.ini.example Steuerung/richtige_config.ini
nano Steuerung/richtige_config.ini
```

---

## 🔐 API-Schutz für Schreibzugriffe

Für einen produktiven API-Port sollte auf dem Raspberry Pi eine Datei mit einem zufälligen Schlüssel und den erlaubten Browser-Origins angelegt werden:

```ini
# /etc/wpssteuerung/api.env
WPS_API_KEY=ein_langer_zufaelliger_schluessel
WPS_CORS_ORIGINS=https://deine-domain.example
```

Danach `systemctl daemon-reload && systemctl restart wpsteuerung` ausführen. Ohne `WPS_API_KEY` bleiben Status-/Historieabfragen sichtbar, Schreib- und Exportbefehle liefern aber bewusst HTTP 503. Die WebApp kann den Schlüssel einmalig im Browser hinterlegen:

```javascript
localStorage.setItem('wp_api_key', 'DEIN_KEY');
```

## 📦 System-Management (Updater)

Für eine einfache Wartung und Updates nutzen Sie die Skripte im `Updater/` Verzeichnis:
- `wp-manager.sh`: Interaktives Menü für Logs, verifizierte Serviceaktionen, Produktionsstatus, Zyklusdaten und Analyse-Timer.
- `rpi-deploy.sh`: Deployment per Fast-Forward; lokale Änderungen werden gesichert und blockieren das Update, statt verworfen zu werden.
- Catbox-Uploads sind wegen der öffentlich abrufbaren URLs immer bestätigungspflichtig.

---

## 📊 Telegram-Befehle

| Befehl | Beschreibung |
| :--- | :--- |
| `🌡️ Temperaturen` | Aktuelle Sensorwerte |
| `📊 Status` | Kompletter Systemstatus inkl. Energie-Daten |
| `📈 Verlauf 6h` | Grafik der letzten 6 Stunden |
| `📉 Verlauf 24h` | Grafik der letzten 24 Stunden |
| `⏱️ Laufzeiten` | Balkendiagramm der Kompressor-Laufzeiten |
| `🌴 Urlaub` | Aktiviert / Deaktiviert den Urlaubsmodus |
| `🛁 Bademodus` | Aktiviert erhöhten Warmwasserbedarf |

---

## 📄 Lizenz

Dieses Projekt ist unter der MIT-Lizenz veröffentlicht. Siehe [LICENSE](LICENSE) für Details.

---

*Entwickelt für effizientes Energiemanagement und maximalen Komfort.*
