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

### Automatische Datenanalyse

`wp-manager.sh` Option 17/18 verwendet bevorzugt die laufende, monatlich
rotierte Historie `Steuerung/csv log/zyklen.csv`. Sie wird nach jedem
abgeschlossenen Kompressorlauf fortgeschrieben.

Option 20 öffnet die Analyse-Verwaltung: Status/next run, manueller Testlauf,
Timer installieren/reparieren/aktivieren oder deaktivieren sowie Analyse-Journal.
Der Timer aktualisiert die Standardbibliothek-Analyse täglich um 04:30 Uhr (plus
bis zu 5 Minuten randomized delay) unter `logs/analyse_auto/`.

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

### Datenschutz beim Upload

Optionen 9, 14, 16, 18 und 21 nutzen dieselbe Upload-Routine. Vor jeder Übertragung
muss die öffentliche, nicht authentifizierte Catbox-URL ausdrücklich bestätigt
werden. Standardlimit sind 200 MiB (`WPS_UPLOAD_MAX_BYTES`), Verbindung 15 s und
Gesamtlaufzeit 300 s. Temporäre Dateien werden nach dem Versuch gelöscht.

## 🛠️ Einrichtung auf dem RPi

```bash
git clone [https://github.com/SomTron/RPI_updater.git](https://github.com/SomTron/RPI_updater.git)
chmod +x RPI_updater/*.sh