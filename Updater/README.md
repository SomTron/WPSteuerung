# 🚀 RPI_updater - Management Tools für WPSteuerung

Dieses Repository enthält Hilfsskripte zur komfortablen Wartung und Aktualisierung der Wärmepumpensteuerung auf dem Raspberry Pi.

## 📦 Inhalt

- **`wp-manager.sh`**: Ein interaktives Konsolen-Menü für:
    - Live-Ansicht der Logfiles
    - Dienst-Steuerung (Start/Stop/Restart)
    - Schnellen Zugriff auf Projekt-Dateien
- **`rpi-deploy.sh`**: Automatisiertes Deployment:
    - Holt die neueste Version von GitHub
    - Ermöglicht bequeme Branch-Wechsel
    - Führt automatische Resets und Service-Neustarts durch

### Automatische Datenanalyse

`wp-manager.sh` Option 17/18 verwendet bevorzugt die laufende, monatlich
rotierte Historie `Steuerung/csv log/zyklen.csv`. Sie wird nach jedem
abgeschlossenen Kompressorlauf fortgeschrieben.

Option 20 installiert `wp-analyse.service` und `wp-analyse.timer`. Dadurch
aktualisiert die Standardbibliothek-Analyse täglich um 04:30 Uhr (plus bis zu
5 Minuten randomized delay) den vollständigen Bericht und alle Detail-CSVs
unter `logs/analyse_auto/`. Manuell startbar mit:

```bash
sudo systemctl start wp-analyse.service
```

## 🛠️ Einrichtung auf dem RPi

```bash
git clone [https://github.com/SomTron/RPI_updater.git](https://github.com/SomTron/RPI_updater.git)
chmod +x RPI_updater/*.sh