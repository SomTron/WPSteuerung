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
- **📅 Betriebsmodi**: Normal, Nachtabsenkung, PV-Boost, Bademodus (erhöhter Bedarf) und Urlaubsmodus (Energiesparen).
- **📊 Daten-Logging**: Kontinuierliches Logging aller Messwerte in CSV-Dateien für Langzeitanalysen.

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

## 📦 System-Management (Updater)

Für eine einfache Wartung und Updates nutzen Sie die Skripte im `Updater/` Verzeichnis:
- `wp-manager.sh`: Ein interaktives Menü für Logs, Neustarts und Status.
- `rpi-deploy.sh`: Einfaches Deployment neuer Code-Versionen per „One-Click“.

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
