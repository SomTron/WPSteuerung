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

### 5b. DNS-Resolver (`aiodns` + `[Netzwerk] DNS_SERVER`)

Der Pi loest Namen ueber `/etc/resolv.conf` auf - und die wird auf diesem Setup von
**Tailscale** verwaltet (`nameserver 100.100.100.100` / `fd7a:115c:a1e0::53`). Faellt
`tailscaled` kurz aus oder haengt das Upstream-Forwarding, stockt damit *jede*
Namensaufloesung: Healthcheck-Ping, Telegram und die Solax-API laufen in Timeouts,
**waehrend der Fernzugriff unveraendert funktioniert** (Befund 13.10.2026:
Healthcheck-Alarm bei erreichbarem Pi, dazu Sensor-Timeouts).

Zwei Stellschrauben, unabhaengig voneinander:

1. **`aiodns`** (in `requirements.txt`) - nicht-blockierender Resolver. Ohne
   `aiodns` belegt jeder Lookup einen Thread des Default-Executors und
   verzoegert damit auch die Sensor-Lesung (`asyncio.to_thread`).
2. **`[Netzwerk] DNS_SERVER`** in `config.ini` - Nameserver fuer die ausgehenden
   HTTP-Verbindungen (Healthcheck, Telegram, Solax). Leer = System-Resolver: der
   folgt automatisch jeder Netz-/Router-Aenderung, haengt aber an `tailscaled`.
   Wer Tailscale aus dem Pfad nehmen will, traegt hier Server ein.

```ini
[Netzwerk]
# Tailnet-Adresse zuerst, Router danach - Begruendung siehe unten
DNS_SERVER = 100.100.100.100, 192.168.178.1
```

**Die Reihenfolge ist nicht beliebig.** c-ares fragt den ersten Eintrag und wartet
dessen Timeout ab, bevor es den naechsten nimmt. Ein *veralteter erster* Eintrag
(z.B. nach einem Routerwechsel) kostet deshalb bei JEDEM Lookup Zeit und kann die
aiohttp-Deadline reissen - also genau den Ausfall erzeugen, den die Liste
verhindern soll. Ein veralteter Eintrag an *zweiter* Stelle kostet dagegen nichts:
c-ares bleibt beim ersten Antwortenden stehen.

`100.100.100.100` gehoert deshalb nach vorn: die Tailnet-Adresse haengt am
`tailscale0`-Interface, **nicht** an der Fritz!Box-IP, und bleibt bei Router-,
Subnetz- oder Anschlusswechsel gleich. Der Router ist der schnelle Rueckfall
(Messung 14.10.2026: Router 6 ms, Tailscale 17 ms, 1.1.1.1 25 ms, 8.8.8.8 32 ms).

**Nicht raten - vorher testen.** Sind oeffentliche Resolver im Netz gesperrt,
scheitert *jeder* Lookup mit
`aiodns.error.DNSError: (12, 'Timeout while contacting DNS servers')`.
`dig` ist auf dem Pi oft nicht installiert (Befund 14.10.2026), deshalb das
mitgelieferte Skript nehmen (read-only, ohne Zusatzpakete):

```bash
python3 Updater/dns-diag.py 100.100.100.100 192.168.178.1 1.1.1.1 8.8.8.8
```

Die eingetragenen Namen werden zusaetzlich **beim Start der Steuerung** geprueft
(UDP/53, 1 s je Eintrag, Ergebnis pro Prozess gecacht). Was nicht antwortet, wird
uebersprungen und geloggt - ein Tippfehler oder ein Routerwechsel kann damit
keinen Ausfall mehr erzeugen:

```bash
grep -i "DNS-Nameserver.*antwortet nicht" /var/log/wps/heizungssteuerung.log | tail -3
```

Antwortet keiner der Eintraege, greift automatisch der System-Resolver (ebenfalls
mit WARNING im Log). Die Pruefung selbst kann den Start nicht verhindern - sie
liefert im Fehlerfall einfach "nicht erreichbar".

Nur Server eintragen, die auch antworten. Welchen Resolver die Steuerung nutzt,
steht beim Start im Log:

```bash
grep -i "DNS-Resolver" /var/log/wps/heizungssteuerung.log | tail -2
```

Auf 32-bit-Raspberry-Pi-OS (armv7) vorher:

```bash
sudo apt-get install -y libc-ares-dev
```

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
