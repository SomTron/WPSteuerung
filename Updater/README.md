# 🚀 RPI_updater - Management Tools für WPSteuerung

Dieses Repository enthält Hilfsskripte zur komfortablen Wartung und Aktualisierung der Wärmepumpensteuerung auf dem Raspberry Pi.

## 📦 Inhalt

- **`wp-manager.sh`**: Stabiler Launcher. Prueft das Menue, meldet Fehler
    und faellt auf die letzte funktionierende Kopie zurueck (Details unten).
- **`wp-manager-menu.sh`**: Interaktives POSIX-Shell-Menü (v1.12) für:
    - Live-Logs, Error-Log und 24-h-Fehler-/OOM-Diagnose
    - verifizierte Dienst-Steuerung (Start/Stopp/Neustart inkl. PID, `NRestarts` und Journal)
    - Produktionsstatus mit Kompressor, Temperatur-CSV-Datenalter, Zykluszähler und Analyse-Timer
    - Zyklus-/Entscheidungshistorie und bestätigungspflichtige, öffentliche Catbox-Uploads inkl. Lerndaten
- **`rpi-deploy.sh`**: Sicheres Deployment:
    - aktualisiert nur per `git pull --ff-only`
    - validiert Zielbranch und verweigert Updates aus detached HEAD
    - verwirft oder stasht lokale Änderungen **nicht**; bei Änderungen wird ein Snapshot erstellt und abgebrochen
    - verifiziert Service-Neustarts anhand Endzustand, `MainPID` und `NRestarts`
- **`manager_status.py`**: Standardbibliothek-Helfer für das Dashboard. Liest Statusfelder aus CSV-Header und Dateiende; die Zyklusanzahl wird blockweise gezählt, ohne große Dateien in den Speicher zu laden.
- **`wg-endpoint-check.sh`**: Read-only-Diagnose der WireGuard-Erreichbarkeit (Dienst, `wg0.conf`, Interface, `ip_forward`, ufw, öffentliche IPv4/IPv6, CGNAT-Hinweis, EUI-64-Prüfung der IPv6-Adresse). Gibt am Ende eine fertige `Endpoint`-Zeile aus.
- **`setup_cloudflare_tunnel.sh`**: Fernzugriff über Cloudflare Tunnel einrichten – der Weg, wenn der Anschluss hinter CGNAT steht und WireGuard von unterwegs nicht nutzbar ist. Ohne `--install` nur Diagnose.
- **`tailscale-funnel.service`** (Systemd-Unit): Dauerhafter Tailscale Funnel für Port 8000 (`*.ts.net`), Start nach `tailscaled`, Restart bei Crash. Kein Admin-Panel-Zugriff nötig, läuft mit Debian-Stable Tailscale.

### Cloudflare Tunnel Setup

`setup_cloudflare_tunnel.sh` richtet einen Cloudflare Tunnel für Fernzugriff ein.

**Features:**
- Prüft CGNAT (WAN-IP vs. Exit-IP)
- Prüft lokale Erreichbarkeit (`/health`)
- Prüft API-Key & CORS (`WPS_API_KEY`, `WPS_CORS_ORIGINS`)
- Installiert `cloudflared` als Systemd-Service
- Ohne `--install` nur Diagnose

```bash
./Updater/setup_cloudflare_tunnel.sh              # nur Diagnose
./Updater/setup_cloudflare_tunnel.sh --install    # einrichten (Token eingeben)
```

**Nach Installation im Cloudflare Dashboard:**
1. Public Hostname → `http://localhost:8000`
2. Access Policy (Zero Trust → Access → Applications) – Pflicht!
3. CORS in `/etc/wpssteuerung/api.env`: `WPS_CORS_ORIGINS=https://deine-domain.example`

---

### Tailscale Funnel (Alternative)

`tailscale-funnel.service` (Systemd-Unit) richtet einen dauerhaften Tailscale Funnel ein.

**Features:**
- Keine Domain nötig (`*.ts.net`)
- Funktioniert hinter CGNAT & ohne IPv6
- Access Control: "Only tailnet devices" (nur Geräte mit Tailscale-App)
- Systemd-Unit: Start nach `tailscaled`, Restart on failure
- Funktioniert mit Debian-Stable Tailscale (kein `tailscale set --funnel` nötig)

```bash
# Einmalig auf dem Pi:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --funnel=8000  # dauerhaft registrieren (Admin-Panel sichtbar)

# Oder als Systemd-Service (läuft mit Debian-Stable Tailscale):
sudo systemctl enable --now tailscale-funnel-8000
# URL: https://raspberrypiz2.tailfb8dd1.ts.net/
```

**Access Control (Admin-Panel):**
https://login.tailscale.com/admin/network/funnel → Access control → **Only tailnet devices**

---

### Rate-Limiting (API-Schutz)

Die API nutzt **slowapi** für Rate-Limiting auf schreibenden Endpoints.

**Konfiguration:**
- **Key**: API-Key > X-Forwarded-For > Remote-IP (pro Nutzer, nicht pro IP)
- **Default**: 100 req/min
- **Schreib-Endpoints**: 10/min (`/control`, `/command`, `/config`)
- **Config Export**: 30/min (`/config/export`)

**Endpoints mit Limit:**
| Endpoint | Methode | Limit |
|---|---|---|
| `/control` | POST | 10/min |
| `/command` | POST | 10/min |
| `/config` | POST | 10/min |
| `/config/export` | GET | 30/min |

**Dependency:** `slowapi` (in `requirements.txt`)

```bash
# Installation im Venv:
/home/patrik/WPSteuerung/.venv/bin/pip install -r requirements.txt
```

### Sicherer Start mit Fehlerausgabe und Downgrade

`wp-manager.sh` ist absichtlich ein kleiner, stabiler Launcher. Das eigentliche
Menue liegt in `wp-manager-menu.sh`. Dadurch kann ein fehlerhaftes Menue-Update
nicht mehr den einzigen Startweg unbrauchbar machen.

Ablauf eines Starts:

1. Menue auf Existenz, Lesbarkeit und POSIX-Syntax (`sh -n`) pruefen.
2. Bei Fehler: deutliche Fehlermeldung auf `stderr` und Start der letzten
   erfolgreich gelaufenen Kopie.
3. Nach erfolgreichem Menue-Ende (Exitcode 0) wird der aktuelle Stand als
   "letzter guter Stand" gesichert.

Gespeichert wird unter:

```bash
${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/wps-manager/
??? wp-manager-menu.last-good.sh
??? last-good-commit.txt
```

Ueberschreibbar fuer Tests und abweichende Installationen:

```bash
WPS_MANAGER_STATE_DIR=/pfad/zum/state \
    WPS_MANAGER_PROJECT_ROOT=/pfad/zum/Projekt \
    sh Updater/wp-manager.sh
```

### Downgrade auf den letzten funktionierenden Stand

Ohne Argument wird automatisch zurueckgefallen, sobald das aktuelle Menue nicht
startbar ist oder mit einem Fehler endet. Ein Downgrade laesst sich auch
explizit anfordern - auch dann, wenn das aktuelle Menue selbst defekt ist:

```bash
sh Updater/wp-manager.sh --downgrade     # oder: --fallback
```

Wichtig: Der Downgrade startet die gesicherte Menue-Kopie. Das Git-Repository wird
dabei **nicht** zurueckgesetzt - es werden weder `git reset` noch `git checkout`
oder `git revert` ausgefuehrt. Commit-Historie, lokale Aenderungen und Branches
bleiben unberuehrt.

Ist noch keine Ersatzkopie vorhanden, bricht der Launcher mit Exitcode 1 und
erklaertem Hinweis ab, statt still zu scheitern.

### Sicherer Start

### Sichere Updates

Lokale Änderungen an getrackten Dateien blockieren `wp-manager.sh` Option 10 und
`rpi-deploy.sh`. Vor dem Abbruch liegen ein Status-Snapshot und ein binärer Diff
unter `.git/wp-manager-backups/<Zeitstempel>-<PID>/`:

- `status.txt`
- `tracked-changes.patch`
- bei `rpi-deploy.sh` zusätzlich `base-commit.txt`

Es werden weder `git reset --hard` noch automatisches Stashen verwendet. Nach
einer eigenen Commit-/Stash-Auflösung wird das Update erneut gestartet.

### Zeilenanzahl bei den Log-Optionen

Die Optionen `2` (Logzeilen) und `3` (Error-Log) zeigen **standardmaessig
200 Zeilen**, fragen aber vorher nach einer anderen Anzahl:

```text
Choice: 2
Wie viele Logzeilen anzeigen? (Enter = 200): 500
--- Letzte 500 Zeilen: /var/log/wps/heizungssteuerung.log ---
```

Verhalten der Eingabe:

| Eingabe | Wirkung |
|---|---|
| nur Enter | Standardwert (200) |
| Zahl | wird uebernommen |
| Text | Hinweis, Rueckfall auf den Standardwert |
| `0` oder negativ | Hinweis, Rueckfall auf den Standardwert |
| sehr gross | Begrenzung auf `WPS_MAX_LOG_LINES` (Default 20000) |
| EOF / nicht-interaktiv | Standardwert, kein Leerwert |

Dieselbe Eingabepruefung wird auch fuer die Stundenabfrage in Option `12`
und die Zyklenzahl in Option `17` verwendet (`frage_zahl`). Es gibt damit
nur eine Stelle mit Zahlenvalidierung im Manager.

Optional:

```sh
WPS_MAX_LOG_LINES=5000    # Deckel fuer die angeforderte Zeilenzahl
```

### Neue Optionen: Health und Analyse-Qualitaet

| Option | Zweck |
|---|---|
| `22` | Steuerungs-Health ueber den API-Endpunkt `/health` (Loop-Alter, Regelfehler, GPIO, Snapshot-Persistenz) |
| `23` | Analyse-Qualitaet aus `quality_report.json` (Schema, unbekannte Codes, Szenarien, Probleme) |

Beide Ausgaben kommen aus `manager_health.py` (nur Standardbibliothek). Der Bericht
wird unter `WPS_QUALITY_REPORT` erwartet; fehlt die Variable, sucht der Manager
den neuesten `quality_report.json` unter `logs/`.

### Option 24: API-Schluessel

| Option | Zweck |
|---|---|
| `24` | `WPS_API_KEY` erzeugen, anzeigen, rotieren oder entfernen |

`api.py` liest den Schluessel **einmal beim Import**. Fehlt er, ist `API_KEY`
leer und `_check_api_key` beantwortet jede Schreibroute mit **503** – auch
`/control`, also auch den Not-Aus. Telegram funktioniert trotzdem, weil der
Handler die API umgeht. Genau deshalb kann das so lange unbemerkt bleiben.

Das Untermenue schreibt `WPS_API_KEY` nach `/etc/wpssteuerung/api.env`
(Override via `WPS_API_ENV_FILE`):

- **2 (erzeugen)** schreibt atomar: erst in eine temporaere Datei im selben
  Verzeichnis, dann `mv`. Ein abgebrochener Vorgang hinterlaesst weder eine
  leere noch eine halbe Datei. Ein vorhandener Schluessel wird nach Rueckfrage
  nach `api.env.bak` gesichert. Rechte: Verzeichnis `750`, Datei `640`,
  `root:root`. Generiert wird ueber `python3 -c "import secrets"` – das ist auf
  dem Pi ohnehin vorhanden, `openssl` nicht garantiert.
- **Jede Aenderung fragt den Neustart ab.** Ohne ihn laeuft der Dienst mit dem
  alten Stand weiter; der Schluessel wirkt erst danach.
- **Die Pruefung geht an den laufenden Dienst**, nicht nur an die Datei:
  `GET /config/export` mit `X-API-Key` ist eine harmlose Route mit
  Key-Pflicht. `200` = angenommen, `401` = Schluessel passt nicht (Neustart
  fehlt), `503` = im Dienst gar kein Schluessel. Bewusst **keine** Probe auf
  `/control` – die wuerde beim ersten erfolgreichen Test einen Not-Aus
  ausloesen.
- **4 (entfernen)** ist der dokumentierte Rueckweg in den gesperrten Zustand:
  danach 503 auf allen Schreibrouten, die Heizung laeuft aber weiter.

Der Schluessel gehoert in die WebApp ueber
`localStorage.setItem('wp_api_key', '<key>')` und in die Android-App ueber
Dashboard -> Karte *Verbindung*.

**Korrektur in Option 22:** `show_control_health` las `WPS_API_KEY` aus der
Menue-Umgebung. Die ist nie gesetzt – die Anzeige meldete deshalb dauerhaft
*nicht gesetzt*, auch wenn die Datei existierte. Jetzt wird die Datei gelesen.

### Option 25: Steuerungs-Unit installieren

| Option | Zweck |
|---|---|
| `25` | `Steuerung/wpsteuerung.service` nach `/etc/systemd/system/` installieren |

**Warum das nötig ist:** Kein Skript hat diese Unit je installiert – die
Deployment-Doku sagte „selbst anlegen". Auf dem Pi lief deshalb eine
handgebaute, ältere Unit. Der Schaden war doppelt und in beiden Fällen
still:

| Fehlende Zeile | Wirkung |
|---|---|
| `EnvironmentFile=-/etc/wpssteuerung/api.env` | `WPS_API_KEY` wird nie gelesen, **jede** Schreibroute antwortet 503 |
| `RestartPreventExitStatus=42` | `Restart=always` startet den Dienst **10 s nach einem Not-Aus** wieder |
| `StartLimitIntervalSec=0` | nach 5 Fehlstarts bleibt der Dienst aus – die beobachtete Lücke am 15.09. |

Bei der zweiten Zeile bleibt das Heizen zwar aus, weil `notaus.lock` den
Zustand lädt – der Dienst läuft aber trotzdem wieder und meldet sich
gesund. Genau das darf bei einem Not-Aus nicht passieren.

Die Option zeigt zuerst den Ist-Stand der **installierten** Unit (nicht der
im Repo), macht bei Abweichung ein `diff -u`, sichert nach `.bak`, kopiert
mit `install -m 0644`, führt `daemon-reload` aus und startet verifiziert neu.
Option 24 weist bei fehlender `EnvironmentFile`-Zeile aktiv auf Option 25
hin – eine gesetzte `api.env` hilft nichts, wenn sie keiner liest.

`wp-analyse.service` und `wp-analyse.timer` wurden schon länger nach
`install_auto_analysis` (Option 20.3) auf dieselbe Weise installiert.

Exit-Codes von `manager_health.py`:

```text
0 = gesund / Qualitaet gruen
1 = nicht erreichbar bzw. nicht lesbar
2 = Daten vorhanden, aber nicht gruen (degraded / Warnung)
```

Konfiguration optional:

```bash
export WPS_API_BASE=http://127.0.0.1:8000
export WPS_API_KEY=...
export WPS_QUALITY_REPORT=/pfad/quality_report.json
```

### Korrektheits- und Haertungsdetails

- **KPI-Anzeige (Option 15)**: Die KPI-Funktion benoetigt WP-Leistung und
  Strompreis. Der parameterlose Aufruf war ein `TypeError` und schlug immer fehl.
  Beide Werte werden jetzt aus `wp_steuerung_parameter.json` gelesen.
- **Kein Shell-Quoting-Risiko**: Python-Code bekommt Pfade und Parameter ueber
  Umgebungsvariablen (`WPS_STEER_DIR`). Ein Pfad mit Anfuehrungszeichen kann den
  Aufruf nicht mehr zerstoeren. Der Startbericht liegt in `startup_report.py`.
- **Detached-HEAD-Schutz**: Option 10 verweigert das Update, wenn HEAD keinem
  Branch zugeordnet ist, und weist auf fehlendes Upstream hin.
- **Backup umfasst untracked Dateien**: zusaetzlich `untracked-files.tar.gz`,
  weil `git diff HEAD` neue Dateien nicht enthaelt.
- **Preflight**: `python3` und `git` sind Pflicht; fehlen sie, gibt es eine
  klare Meldung statt eines Syntaxfehlers. `curl`/`gzip` werden nur fuer Uploads
  gemeldet.
- **Ctrl+C**: bricht mit sauberer Zeile und Exitcode 130 ab.
- **Service-Stopp**: verlangt eine Bestaetigung, weil der Dienst Heizung, Solar
  und Legionellenprophylaxe steuert.
- **Pager**: `more` wird ueber `zeige` aufgerufen, damit die Ausgabe auch ohne
  Pager funktioniert.
- **BOM-Toleranz**: `quality_report.json` wird mit `utf-8-sig` gelesen.

### Datenschutz beim Upload

Optionen 9, 14, 16, 18 und 21 nutzen dieselbe Upload-Routine. Vor jeder Übertragung
muss die öffentliche, nicht authentifizierte Catbox-URL ausdrücklich bestätigt
werden. Standardlimit sind 200 MiB (`WPS_UPLOAD_MAX_BYTES`), Verbindung 15 s und
Gesamtlaufzeit 300 s. Temporäre Dateien werden nach dem Versuch gelöscht.

## 🛠️ Einrichtung auf dem RPi

```bash
git clone [https://github.com/SomTron/RPI_updater.git](https://github.com/SomTron/RPI_updater.git)
chmod +x RPI_updater/*.sh