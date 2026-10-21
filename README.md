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
- **📊 Daten-Logging**: Kontinuierliches Logging aller Messwerte (oben/mittig/unten/Verdampfer) im 10-Sekunden-Takt; monatliche CSV-Rotation verhindert unbegrenztes Wachstum (Altdaten werden nach 24 Monaten entfernt, einstellbar über `WPS_CSV_ARCHIV_MAX_MONATE`). Jeder Neustart schreibt die **GitHub-Revision** (Commit) ins Log.
- **🔄 Zyklus-Historie**: Nach jedem realen Kompressorlauf wird sofort `Steuerung/csv log/zyklen.csv` fortgeschrieben – inklusive Start-/Endzeit, Dauer, Stromquelle, Regel, Abschaltgrund und Temperaturen oben/mittig/unten (Start + Maxima).
- **🕵️ Automatische Log-Analyse**: Der systemd-Timer `wp-analyse.timer` aktualisiert täglich den vollständigen Analysereport (Tages-KPIs, Zyklen, Overshoot, Morgen-Netzbezug, Stale-Phasen) plus CSV-Dateien.
- **🛡️ API-Rate-Limiting**: Schreibende Endpoints (`/control`, `/command`, `/config`, `/config/export`) sind per **slowapi** gegen Brute-Force und DoS geschützt – Limit pro API-Key (10/min), nicht pro IP. Funktioniert durch Tailscale/Cloudflare Tunnel hinweg.
- **🌐 Fernzugriff ohne Portforwarding**: **Tailscale Funnel** (`*.ts.net`) oder **Cloudflare Tunnel** – beide funktionieren hinter CGNAT, ohne offene Ports und ohne IPv6. Tailscale Funnel bietet zusätzliche Access Control („Only tailnet devices“).

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

## 📁 Betriebslog & Rotation

Das Betriebslog liegt auf dem Pi unter `/var/log/wps` (log2ram-tmpfs, 128 MB).
Es wird **tagesbasiert rotiert** - der Tag steht direkt im Dateinamen:

```
/var/log/wps/heizungssteuerung.log              # aktueller Tag (aktiv)
/var/log/wps/heizungssteuerung.log.2026-10-19   # Log vom 19.10.
/var/log/wps/heizungssteuerung.log.2026-10-20   # Log vom 20.10.
```

Warum nicht die übliche `.1`/`.2`-Nummerierung: die Zahlen sagen nichts über
den Zeitraum aus – man muss raten, welcher Tag in welcher Datei steht. Mit dem
Datum im Namen ist der Log eines bestimmten Tages direkt auffindbar. Zusätzlich
rotiert die Datei auch mitten im Tag, wenn sie 25 MB überschreitet (dann
`…2026-10-20.2`).

**Alle Leser ziehen die Generationen zusammen.** Manager-Option 11/12
(„Query Logs by Time/Duration"), die Fehlerhistorie der WebApp und das
Fehlerlog im Menü lesen über alle Generationen hinweg – nach einer Rotation
bleibt die Historie also sichtbar.

Begrenzung: 14 Tage Historie **und** ein Gesamtbudget von 60 MB (15 MB für
`error.log`). Das Byte-Budget ist auf dem Pi entscheidend, weil
`/var/log/wps` im RAM liegt.

### Uploads (WP-Manager 9/14/16/18/21)

Die Upload-Optionen laden **nicht** nur die aktive Datei hoch, sondern den
**vollständigen Datenbestand** als ZIP:

| Option | Inhalt des Archivs |
|---|---|
| 9) CSV | `heizungsdaten.csv` + alle Monatsarchive (`heizungsdaten_2026-09.csv`) |
| 14) Log | `heizungssteuerung.log` + alle Tages-Generationen |
| 16) Entscheidungslog | `entscheidungs_log.jsonl` + alle `.old.N`-Generationen |
| 18) Zyklen | laufende `zyklen.csv` + alle aus `logs/analyse_*/` |
| 21) Lerndaten | `learning_data.json` + Sicherungskopien |

Ohne das ging bei rotierenden Daten die komplette Historie verloren – ein
Upload der aktiven Datei liefert nach der Tagesrotation praktisch nur den
laufenden Tag. Die Datei-Auswahl liegt in `Steuerung/datenbestand.py` und
ist damit für Anzeige und Upload dieselbe.

#### Größensteuerung

Catbox nimmt max. 200 MB pro Upload (`WPS_UPLOAD_MAX_BYTES`). Damit das
Limit nie gerissen wird, arbeiten **drei** Mechanismen zusammen:

1. **Budget** (`WPS_UPLOAD_BUDGET_BYTES`, Standard 4 × Limit): Der Bestand
   wird auf dieses Rohdaten-Budget beschnitten – die **ältesten** Dateien
   fallen weg, der jüngste Zeitraum bleibt erhalten. Nötig, weil nicht alle
   Bestände begrenzt wachsen: die CSV-Monatsarchive (~17 MB/Monat) und die
   `zyklen.csv` aus den Analyse-Läufen sammeln sich unbegrenzt an.
2. **Messung während des Baus**: Wie viele Rohdaten in ein Archiv passen,
   hängt vom Kompressionsfaktor ab – der wird an der größten Datei
   **gemessen** (Log ≈ 10, JSON ≈ 20, Binärdaten ≈ 1). Wird das Limit
   erreicht, geht die nächste Datei in ein neues Archiv.
3. **Geteilte Uploads**: Es entstehen mehrere Archive
   (`heizungssteuerung.zip`, `heizungssteuerung-2.zip`, …), die einzeln
   bestätigt und hochgeladen werden. Jedes ist für sich ein gültiges ZIP
   und per Doppelklick zu entpacken.

Die Größenprüfung läuft **vor und während** des Archivbaus – nicht erst
danach. Sonst entstünde auf dem Pi erst ein riesiges Temp-Archiv, das man
nachher wegwerfen müsste.

> ⚠️ Die Daten landen **öffentlich** auf catbox.moe und sind ohne Login
> abrufbar. Der Upload bleibt daher bestätigungspflichtig.

---

## 🖥️ WP-Manager starten

Bisher war der Aufruf umständlich:

```bash
cd /home/patrik/WPSteuerung
sh Updater/wp-manager.sh
```

Der **`cd` ist überflüssig** – der Launcher findet sein Verzeichnis selbst
(`SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)`). Mit dem neuen Kurzstart
`Updater/wps` geht es noch kürzer:

```bash
~/WPSteuerung/Updater/wps          # aus jedem Verzeichnis
wps                                # einmalig in die PATH gelegt
ssh patrik@raspberrypiz2 wps        # auch remote
wps --deploy                       # nur deployen, ohne Menü
```

**Einmalig einrichten** (dann geht `wps` überall, auch aus
Unterverzeichnissen und per SSH):

```bash
sudo ln -s ~/WPSteuerung/Updater/wps /usr/local/bin/wps
```

Ohne `sudo` in die `~/.bashrc`:

```bash
alias wps='~/WPSteuerung/Updater/wps'
```

Der Wrapper ruft bewusst den **stabilen Launcher** `wp-manager.sh` auf –
nicht direkt das Menü. Der Launcher prüft die Menü-Syntax und fällt auf die
letzte funktionierende Kopie zurück (`wps --downgrade` startet sie direkt).
Ein kaputtes Menü-Update nimmt so nicht auch noch den einzigen Startweg
mit.

**`wps --deploy`** führt nur das Deploy aus und beendet sich – ohne Menü.
Damit lässt sich die Anlage von unterwegs aktualisieren:

```bash
ssh patrik@raspberrypiz2 wps --deploy
```

Die Version im Banner wird aus dem Changelog-Kopf des Menüs abgeleitet –
sie kann also nicht mehr hinterherlaufen (was bei der fest eingetragenen
Version zweimal passiert ist: Code v1.17, Banner v1.16).

### SSH-Key und Deploy

Nach einem Reboot läuft auf dem Pi **kein `ssh-agent`** – er wird nur beim
Login per `eval $(ssh-agent -s)` gestartet. Ohne ihn scheitert `ssh-add`
mit *„Could not open a connection to your authentication agent"* und
**jeder** Deploy mit *„Permission denied (publickey)"* – auch wenn die
Key-Datei vorhanden ist.

Der Manager startet den Agent deshalb selbst und lädt den Key nach
(`Updater/ssh_agent.sh`, verwendet von Menü **und** `rpi-deploy.sh`). Im
Header steht der Status:

```
GitHub:  ✓ SSH-Key geladen (Deploy möglich)
GitHub:  ✗ kein SSH-Key - Deploy schlägt fehl (ssh-add ~/.ssh/id_ed25519)
```

Dauerhaft für alle SSH-Sitzungen (in `~/.bashrc` des Pi):

```bash
if [ -z "$SSH_AUTH_SOCK" ]; then eval "$(ssh-agent -s)" >/dev/null; fi
```

Meldet der Deploy *„Permission denied (publickey)"*, ist das **kein
Netzproblem** – der Key fehlt im Agent. Das Netz wird getrennt geprüft
(Timeout, DNS, „Connection refused").

---

## 💾 SD-Karten-Health & Neustart-Schutz

Auf dem Pi trägt die SD-Karte fast alles: die Heizungsdaten-CSV, das
Entscheidungslog, `last_state.txt`. **Die Logs liegen dagegen im RAM**
(`/var/log/wps`, log2ram-tmpfs) und werden stündlich auf die SD gesynct –
das gilt aber *nur* für die Logs. Die CSV schreibt direkt auf die SD-Karte.

### Früherkennung (`sd_health.py`)

Ein Karten-Ausfall kommt selten überraschend. Der Check wertet vier
Signale aus und hängt sie in `/health` (`sd_karte`):

| Signal | Quelle |
|---|---|
| I/O- und Dateisystem-Fehler | `dmesg` (Muster u. a. `Buffer I/O error`, `EXT4-fs … error`, `error -84`) |
| Dateisystem-Fehlerzähler | `/sys/fs/ext4/<dev>/errors_count` |
| Schreibtest | kleine Datei im Datenverzeichnis schreiben/lesen/löschen |
| CSV-Konsistenz | Header + Datenzeilen lesbar? |

Angezeigt wird das im **Header-Badge der WebApp** (rot: „SD-Karte
prüfen!") und in der **Manager-Option 22**. Geprüft wird alle 5 Minuten
(gecacht) – `/health` wird alle 5 s abgerufen.

### Neustart-Dämpfung (`start_daempfung.py`)

Die Unit startet den Dienst mit `Restart=always` und **ohne** StartLimit –
bewusst, denn eine Heizungssteuerung darf nach einem Absturz nicht
aufgeben. Kehrseite: Startet der Dienst dauerhaft fehl, entstehen 8.640
Neustarts pro Tag mit Stacktrace im Journal.

Ab dem **dritten** Fehlstart in 10 Minuten wartet der Prozess 5 Minuten
*in-place* und versucht es erneut – ohne Neustart durch systemd, ohne
Journal-Spam. Die Heizung versucht es weiter, denn der Fehler kann
weggehen (z. B. wenn ein Sensor zurückkommt).

> Wichtig: Es wird **nur** ein Fehler beim *Start* gezählt. Ein Absturz im
> laufenden Betrieb führt weiterhin zum sofortigen Neustart – das ist
> richtig so.

Einstellbar über `WPS_MAX_FEHLSTARTS`, `WPS_FEHLSTART_WARTEZEIT_MIN`.

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

Statt die Datei von Hand anzulegen, erzeugt **Option 24** des Updater-Menüs
den Schlüssel: erzeugen, anzeigen, rotieren und entfernen, jeweils mit
Rückfrage und anschließender Prüfung gegen den laufenden Dienst. Details in
`Updater/README.md`.

## 📱 Fernzugriff von unterwegs

Steht der Internetanschluss hinter **CGNAT** (in der Fritz!Box unter
*Internet → Online-Monitor* erkennbar an einer IPv4-Adresse in
`100.64.0.0/10`), erreicht eine Portweiterleitung das Heimnetz nie. Dann ist
auch WireGuard von unterwegs nicht nutzbar – unabhängig von Schlüssel und
Portfreigabe. Hinzu kommt, dass der Mobilfunk-Zugang oft gar kein IPv6
mitbringt, ein IPv6-Endpoint dort also ebenfalls nicht erreichbar ist.

Zwei Lösungen, **beide ohne offene Ports und ohne IPv6**:

### Option A: Tailscale Funnel (empfohlen, kostenlos, `*.ts.net`)

```bash
# Auf dem Pi:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
# → Link im Terminal öffnen, mit GitHub/Google/Microsoft anmelden

# Funnel dauerhaft aktivieren (Port 8000):
sudo tailscale set --funnel=8000
# → permanente URL: https://raspberrypiz2.tailfb8dd1.ts.net/
```

**Zugriffsschutz (Access Control):**
- Admin-Panel: https://login.tailscale.com/admin/network/funnel
- **Only tailnet devices** → nur Geräte mit installierter Tailscale-App (Handy/PC) kommen ran
- Alternative: **Specific users** (E-Mail/PIN) oder **Anyone on the internet**

### Option B: Cloudflare Tunnel (wenn eigene Domain gewünscht)

```bash
cd ~/WPSteuerung
./Updater/setup_cloudflare_tunnel.sh              # nur Diagnose
./Updater/setup_cloudflare_tunnel.sh --install    # einrichten
```

Danach im Cloudflare-Dashboard:
1. **Public Hostname** auf `http://localhost:8000` setzen
2. **Access Policy** anlegen (*Zero Trust → Access → Applications*) – Pflicht!

Für die reine WireGuard-Diagnose (Dienst, Konfiguration, CGNAT, Stabilität
der IPv6-Adresse) gibt es `Updater/wg-endpoint-check.sh`.

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
