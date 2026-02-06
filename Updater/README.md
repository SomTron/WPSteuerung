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

## 🛠️ Einrichtung auf dem RPi

```bash
git clone [https://github.com/SomTron/RPI_updater.git](https://github.com/SomTron/RPI_updater.git)
chmod +x RPI_updater/*.sh