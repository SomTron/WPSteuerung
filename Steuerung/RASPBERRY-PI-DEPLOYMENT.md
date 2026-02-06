# Raspberry Pi Deployment - Setup Anleitung

## Übersicht
Diese Anleitung zeigt dir, wie du Git auf dem Raspberry Pi einrichtest, um Code einfach zu deployen und zwischen Branches zu wechseln.

## Einmalige Einrichtung auf Raspberry Pi

### 1. SSH-Verbindung zum Raspberry Pi
```bash
ssh pi@<raspberry-pi-ip>
```

### 2. Git installieren (falls noch nicht vorhanden)
```bash
sudo apt-get update
sudo apt-get install git -y
```

### 3. GitHub SSH-Key einrichten (empfohlen)

**Auf dem Raspberry Pi:**
```bash
# SSH-Key generieren
ssh-keygen -t ed25519 -C "raspberry-pi-wpsteuerung"
# Enter druecken fuer Standard-Pfad
# Optional: Passphrase eingeben oder Enter fuer keine

# Public Key anzeigen
cat ~/.ssh/id_ed25519.pub
```

**Kopiere den Output und:**
1. Gehe zu GitHub.com → Settings → SSH and GPG keys
2. Click "New SSH key"
3. Titel: "Raspberry Pi WPSteuerung"
4. Paste den Key
5. Save

**Teste die Verbindung:**
```bash
ssh -T git@github.com
# Sollte "Hi SomTron! You've successfully authenticated" anzeigen
```

### 4. Repository clonen
```bash
# Gehe ins Home-Verzeichnis
cd ~

# Clone das Repository
git clone git@github.com:SomTron/WPSteuerung.git

# Oder falls SSH nicht klappt (mit HTTPS):
# git clone https://github.com/SomTron/WPSteuerung.git

cd WPSteuerung/WPSteuerung
```

### 5. Python Virtual Environment einrichten
```bash
# Virtual Environment erstellen
python3 -m venv .venv

# Aktivieren
source .venv/bin/activate

# Abhängigkeiten installieren
pip install -r requirements.txt

# (Deaktivieren mit: deactivate)
``

`

### 6. Konfiguration anpassen
```bash
# Kopiere die Beispiel-Config
cp config.ini.example config.ini

# Editiere die Config mit deinen Einstellungen
nano config.ini
```

### 7. Systemd Service einrichten (optional aber empfohlen)

**Service-Datei erstellen:**
```bash
sudo nano /etc/systemd/system/wpsteuerung.service
```

**Inhalt:**
```ini
[Unit]
Description=WP Steuerung - Heat Pump Control
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/WPSteuerung/WPSteuerung
ExecStart=/home/pi/WPSteuerung/WPSteuerung/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Service aktivieren:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable wpsteuerung
sudo systemctl start wpsteuerung
```

### 8. Deployment-Script kopieren
```bash
# Script ausfuehrbar machen
chmod +x ~/WPSteuerung/WPSteuerung/rpi-deploy.sh

# Optional: Alias erstellen fuer einfachen Zugriff
echo 'alias deploy="~/WPSteuerung/WPSteuerung/rpi-deploy.sh"' >> ~/.bashrc
source ~/.bashrc
```

## Tägliche Verwendung

### Code vom PC aktualisieren
**Auf deinem PC:**
1. Änderungen machen
2. Commit & Push mit `git-branch-manager.ps1`

**Auf Raspberry Pi:**
```bash
./rpi-deploy.sh
# Wähle Option 1: Code aktualisieren
```

### Zwischen Branches wechseln
```bash
./rpi-deploy.sh
# Wähle Option 3: Branch wechseln UND aktualisieren
# Gib Branch ein: master / android-api / funktioniert
```

### Bei Fehler: Schnell zu "funktioniert" wechseln
```bash
./rpi-deploy.sh
# Option 3 wählen
# Branch: funktioniert
```

### Service-Befehle
```bash
# Status pruefen
sudo systemctl status wpsteuerung

# Logs anzeigen
sudo journalctl -u wpsteuerung -f

# Service neu starten
sudo systemctl restart wpsteuerung

# Service stoppen
sudo systemctl stop wpsteuerung
```

## Workflow: "Funktioniert" Branch aktualisieren

Der `funktioniert` Branch sollte **NUR** nach erfolgreichen Langzeit-Tests aktualisiert werden:

### Prozess:
1. **Entwicklung**: Features auf `master` oder `android-api` entwickeln
2. **PC Push**: Code mit `git-branch-manager.ps1` pushen
3. **Pi Deploy**: Auf Pi mit `rpi-deploy.sh` deployen
4. **Langzeit-Test**: **Mindestens 48-72 Stunden laufen lassen**
5. **Monitoring**: Logs überwachen, auf Fehler achten
6. **Update "funktioniert"**: Nur wenn **keine Fehler** gefunden wurden

### "Funktioniert" updaten (auf PC):
```powershell
# Nach erfolgreichen 48-72h Test
git checkout funktioniert
git reset --hard master  # oder android-api
git push -f origin funktioniert
```

### Oder mit git-branch-manager.ps1:
Die Option dafür ist bereits im Script eingebaut!

## Troubleshooting

### Problem: Git Pull schlägt fehl
```bash
# Lokale Änderungen verwerfen
git reset --hard
git pull origin <branch-name>
```

### Problem: Service startet nicht
```bash
# Logs pruefen
sudo journalctl -u wpsteuerung -n 50

# Manuell testen
cd ~/WPSteuerung/WPSteuerung
source .venv/bin/activate
python main.py
```

### Problem: SSH-Key funktioniert nicht
```bash
# Auf HTTPS umstellen
cd ~/WPSteuerung
git remote set-url origin https://github.com/SomTron/WPSteuerung.git
```

### Problem: "Permission denied" beim Script
```bash
chmod +x ~/WPSteuerung/WPSteuerung/rpi-deploy.sh
```

## Tipps
- **Backup vor großen Änderungen**: `git branch backup-$(date +%Y%m%d)`
- **Logs regelmäßig checken**: `sudo journalctl -u wpsteuerung -f`
- **Bei Problemen**: Immer zu `funktioniert` wechseln können
- **Nach Updates**: Mindestens 10 Minuten laufen lassen vor Trennen

## Nächste Schritte
1. Einrichtung auf Raspberry Pi durchführen
2. Ersten Deployment-Test machen
3. Workflow für deine Bedürfnisse anpassen
