#!/bin/sh
# wp-manager.sh - Management script for WPSteuerung
# Located in Updater repo, targets ../Steuerung (relative to script location)
#
# v1.10: Neue Option 19 (Lauf-Status/letzte Abstuerze aus letzter_lauf.json) und
#       Status-Zeile "Letzter Lauf" im Kopf - macht OOM-Kills/Crashes sichtbar,
#       die vorher nur im Kernel-Journal standen. Banner-Version nachgezogen.
# v1.9: Neue Optionen 17 (zyklen.csv anzeigen) und 18 (Upload der
#       Analyse-Zylen-CSV) - Kompressor-Zyklen direkt vom PI abrufen/teilen.
# v1.8: Neue Optionen 15 (Entscheidungs-Log anzeigen) und 16 (Upload
#       entscheidungs_log.jsonl) - Detailansicht der Regelentscheidungen.
# v1.7: Option 10 Raeumt blockierende lokale Aenderungen vorher weg (mit Ruefrage,
#       wie im Deploy-Skript) - Pull scheiterte sonst an Runtime-Dateien.
# v1.6: Farb-Fix (%b statt %s bei Service/VPN-Status - zeigte vorher rohe \033-Codes),
#       lokale-Aenderungen-Zaehler nur noch getrackte Dateien (-uno).
# v1.5: CYAN-Farbe ergänzt (war vorher nicht definiert), informativer Status-Header
#       (Uptime, RAM, Git-Diff, Temperatur, Disk, letzter Fehler), Eingabevalidierung,
#       Fehlerbehandlung bei Upload / Self-Update / Service-Steuerung, neue Option 13.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$(dirname "$SCRIPT_DIR")/Steuerung"
# Log-Pfade: produktiv unter /var/log/wps (log2ram-tmpfs), Fallback auf
# das Steuerungsverzeichnis (alte Installation / Entwicklung).
LOG_FILE="${WPS_LOG_FILE:-/var/log/wps/heizungssteuerung.log}"
ERROR_LOG_FILE="${WPS_ERROR_LOG_FILE:-/var/log/wps/error.log}"
[ -f "$LOG_FILE" ] || LOG_FILE="$TARGET_DIR/heizungssteuerung.log"
[ -f "$ERROR_LOG_FILE" ] || ERROR_LOG_FILE="$TARGET_DIR/error.log"

# Farben (leer, wenn Ausgabe kein Terminal ist, z.B. bei Piping)
if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    CYAN='\033[0;36m'
    DIM='\033[2m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; DIM=''; NC=''
fi

# Hilfsfunktionen -------------------------------------------------------------

# Bytes menschenlesbar formatieren (für RAM-Anzeige)
fmt_bytes() {
    b=$1
    case "$b" in
        ''|*[!0-9]*) echo "n/a"; return ;;
    esac
    if [ "$b" -ge 1073741824 ]; then
        echo "$((b / 1073741824)) GB"
    elif [ "$b" -ge 1048576 ]; then
        echo "$((b / 1048576)) MB"
    elif [ "$b" -ge 1024 ]; then
        echo "$((b / 1024)) KB"
    else
        echo "$b B"
    fi
}

# Prüft, ob die Eingabe eine positive Ganzzahl ist
is_number() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

if [ ! -d "$TARGET_DIR" ]; then
    printf "${RED}Error: $TARGET_DIR not found!${NC}\n"
    exit 1
fi


# Log-Abfrage-Hilfsfunktionen

query_logs_by_time() {
    target="$1"
    maxlines="${2:-50}"
    python3 "$TARGET_DIR/log_query.py" --after "$target" --lines "$maxlines"
    wait_for_key
}

query_logs_by_duration() {
    hours="$1"
    maxlines="${2:-200}"
    python3 -c "
from datetime import datetime, timedelta
import sys
sys.path.insert(0, '$TARGET_DIR')
from log_query import query_logs, tail_log
#timezone-awareive für Vergleich mit Log-Zeitstempeln
target = datetime.now().replace(tzinfo=None) - timedelta(hours=${hours})
result, meta = query_logs(after=target, lines=${maxlines})
for line in result:
    sys.stdout.write(line)
if not result:
    print('Keine Logs in den letzten ${hours} Stunde(n) gefunden.')
" 2>&1 | more
    wait_for_key
}


# Findet die neueste zyklen.csv aus einem Analyse-Lauf (Analyse/log_analyse.py).
# Pfad: <repo>/logs/analyse_*/zyklen.csv. Ausgabe: Pfad oder leer.
finde_zyklen_csv() {
    ROOT_DIR="$(dirname "$SCRIPT_DIR")/logs"
    ls -1t "$ROOT_DIR"/analyse_*/zyklen.csv 2>/dev/null | head -n1
}


wait_for_key() {
    printf "\n${YELLOW}Drücke Enter, um ins Menü zurückzukehren...${NC}"
    read dummy
}

while true; do
    # Status Informationen abrufen
    CUR_BRANCH=$(cd "$TARGET_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "Unknown")
    CUR_COMMIT=$(cd "$TARGET_DIR" && git log -1 --oneline 2>/dev/null || echo "No commits")
    
    # ---- Git: lokale Änderungen & Abstand zum Remote ----
    GIT_DIRTY=$(cd "$TARGET_DIR" && git status --porcelain -uno 2>/dev/null | wc -l | tr -d ' ')
    GIT_BEHIND=$(cd "$TARGET_DIR" && git rev-list --count "HEAD..@{u}" 2>/dev/null)
    [ -z "$GIT_BEHIND" ] && GIT_BEHIND="?"

    # ---- Service-Status mit Details ----
    SVC_ENABLED=$(systemctl is-enabled wpsteuerung 2>/dev/null)
    [ -z "$SVC_ENABLED" ] && SVC_ENABLED="unbekannt"
    SVC_PID=""
    SVC_MEM=""
    SVC_SINCE=""
    if systemctl is-active --quiet wpsteuerung; then
        SVC_STATUS="${GREEN}✓ AKTIV${NC}"
        SVC_PID=$(systemctl show wpsteuerung -p MainPID --value 2>/dev/null)
        SVC_SINCE=$(systemctl show wpsteuerung -p ActiveEnterTimestamp --value 2>/dev/null)
        SVC_MEM=$(fmt_bytes "$(systemctl show wpsteuerung -p MemoryCurrent --value 2>/dev/null)")
    else
        SVC_STATUS="${RED}✗ INAKTIV${NC}"
    fi

    # VPN / WireGuard Status
    if systemctl is-active --quiet wg-quick@wg0 2>/dev/null; then
        VPN_STATUS="${GREEN}✓ AKTIV${NC}"
        VPN_IP=$(wg show wg0 2>/dev/null | grep 'endpoint' | head -1 | awk '{print $2}' | cut -d: -f1)
        if [ -n "$VPN_IP" ]; then
            VPN_INFO=" ($VPN_IP)"
        else
            VPN_INFO=""
        fi
    else
        VPN_STATUS="${RED}✗ INAKTIV${NC}"
        VPN_INFO=""
    fi

    # ---- Startup-Diagnose: letzter Lauf sauber beendet? ----
    # Wichtig: Solange die Steuerung laeuft, steht in letzter_lauf.json
    # "sauber_beendet": false (wird erst im finally gesetzt). Deshalb wird die
    # PID aus der Datei mit der laufenden Service-PID verglichen - nur wenn sie
    # ABWEICHT, wurde der vorige Lauf wirklich abgebrochen (z. B. OOM-Kill).
    LAUF_JSON="$TARGET_DIR/letzter_lauf.json"
    LAUF_STATUS="${DIM}keine Info (Option 19)${NC}"
    if [ -f "$LAUF_JSON" ]; then
        LAUF_PID=$(grep -o '"pid"[[:space:]]*:[[:space:]]*[0-9]*' "$LAUF_JSON" 2>/dev/null | grep -o '[0-9]*')
        if grep -q '"sauber_beendet"[[:space:]]*:[[:space:]]*true' "$LAUF_JSON" 2>/dev/null; then
            LAUF_STATUS="${GREEN}✓ sauber beendet${NC}"
        elif [ -n "$LAUF_PID" ] && [ "$LAUF_PID" = "$SVC_PID" ]; then
            LAUF_STATUS="${GREEN}✓ läuft (aktiv)${NC}"
        else
            LAUF_STATUS="${RED}⚠ unsauber beendet -> Option 19${NC}"
        fi
    fi

    # ---- System-Infos ----
    HOST_UP=$(uptime -p 2>/dev/null | sed 's/^up //')
    DISK_LINE=$(df -P "$TARGET_DIR" 2>/dev/null | awk 'NR==2 {print $4 "|" $5}')
    DISK_AVAIL=${DISK_LINE%%|*}
    DISK_USEPCT=${DISK_LINE##*|}
    CPU_TEMP=""
    if command -v vcgencmd >/dev/null 2>&1; then
        CPU_TEMP=$(vcgencmd measure_temp 2>/dev/null | cut -d= -f2)
    elif [ -r /sys/class/thermal/thermal_zone0/temp ]; then
        CPU_TEMP="$(( $(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0) / 1000 ))°C"
    fi

    # ---- Log-Statistik ----
    LOG_SIZE=""
    [ -f "$LOG_FILE" ] && LOG_SIZE=$(du -h "$LOG_FILE" 2>/dev/null | cut -f1)
    ERR_COUNT=0
    ERR_TIME=""
    LAST_ERR=""
    if [ -f "$ERROR_LOG_FILE" ]; then
        ERR_COUNT=$(wc -l < "$ERROR_LOG_FILE" | tr -d ' ')
        ERR_TIME=$(stat -c %y "$ERROR_LOG_FILE" 2>/dev/null | cut -d. -f1)
        [ -s "$ERROR_LOG_FILE" ] && LAST_ERR=$(tail -n 1 "$ERROR_LOG_FILE" 2>/dev/null | cut -c1-70)
    fi

    clear
    printf "${BLUE}=========================================================${NC}\n"
    printf "${BLUE}               WPSteuerung Manager v1.10                 ${NC}\n"
    printf "${BLUE}=========================================================${NC}\n"
    printf "Target:   %s\n" "$TARGET_DIR"
    printf "Branch:   ${YELLOW}%s${NC}" "$CUR_BRANCH"
    if [ -n "$GIT_DIRTY" ] && [ "$GIT_DIRTY" != "0" ]; then
        printf "  ${YELLOW}⚠ %s uncommittete Änderungen${NC}" "$GIT_DIRTY"
    fi
    printf "\n"
    printf "Commit:   %s\n" "$CUR_COMMIT"
    printf "Remote:   "
    if [ "$GIT_BEHIND" = "0" ]; then
        printf "${GREEN}auf aktuellem Stand${NC}\n"
    elif [ "$GIT_BEHIND" = "?" ]; then
        printf "${DIM}kein Upstream-Branch konfiguriert${NC}\n"
    else
        printf "${YELLOW}%s Commit(s) hinterher – Update empfohlen (Option 4)${NC}\n" "$GIT_BEHIND"
    fi
    printf "Service:  %b" "$SVC_STATUS"
    if [ -n "$SVC_PID" ] && [ "$SVC_PID" != "0" ]; then
        printf "  ${DIM}PID %s${NC}" "$SVC_PID"
    fi
    if [ -n "$SVC_MEM" ] && [ "$SVC_MEM" != "n/a" ]; then
        printf "  ${DIM}RAM: %s${NC}" "$SVC_MEM"
    fi
    printf "  ${DIM}[autostart: %s]${NC}\n" "$SVC_ENABLED"
    if [ -n "$SVC_SINCE" ]; then
        printf "          ${DIM}läuft seit %s${NC}\n" "$SVC_SINCE"
    fi
    printf "VPN:      %b%s\n" "$VPN_STATUS" "$VPN_INFO"
    printf "Lauf:     %b\n" "$LAUF_STATUS"
    SYS_LINE="System:   ${HOST_UP:-unbekannt}"
    [ -n "$CPU_TEMP" ] && SYS_LINE="$SYS_LINE | CPU $CPU_TEMP"
    if [ -n "$DISK_AVAIL" ]; then
        SYS_LINE="$SYS_LINE | Disk: $DISK_AVAIL frei ($DISK_USEPCT belegt)"
    fi
    printf "%s\n" "$SYS_LINE"
    printf "${BLUE}---------------------------------------------------------${NC}\n"
    if [ -f "$ERROR_LOG_FILE" ] && [ "$ERR_COUNT" != "0" ]; then
        printf "⚠ Error-Log: ${RED}%s Einträge${NC}, letzte Änderung: %s\n" "$ERR_COUNT" "$ERR_TIME"
        [ -n "$LAST_ERR" ] && printf "  ${RED}➜ %s${NC}\n" "$LAST_ERR"
    fi
    [ -n "$LOG_SIZE" ] && printf "Log:      heizungssteuerung.log (%s)\n" "$LOG_SIZE"
    printf "${BLUE}---------------------------------------------------------${NC}\n\n"
    
    printf "1) 📜   Live-Logs (tail -f, Strg+C beendet)\n"
    printf "2) 📄   Last 200 log lines\n"
    printf "3) ⚠️    Error Log (Last 200 lines)\n"
    printf "4) 🚀   Update & Deploy (WPSteuerung)\n"
    printf "5) 🔄   Restart Service\n"
    printf "6) ⏹️    Stop Service\n"
    printf "7) ▶️    Start Service\n"
    printf "8) 📂   List Files\n"
    printf "9) ☁️    Upload CSV to Catbox\n"
    printf "10) 🆕  Update WP-Manager (this script)\n"
    printf "11) 🔍  Query Logs by Time (enter datetime)\n"
    printf "12) ⏱️  Query Logs by Duration (last N hours)\n"
    printf "13) 📊  Service-Details (systemctl status)\n"
    printf "14) ☁️  Upload Log to Catbox\n"
    printf "15) 📖  Entscheidungs-Log anzeigen (letzte Entscheidungen)\n"
    printf "16) ☁️  Upload entscheidungs_log.jsonl to Catbox\n"
    printf "17) 📊  Zyklen-Analyse anzeigen (zyklen.csv)\n"
    printf "18) ☁️  Upload zyklen.csv to Catbox\n"
    printf "19) 🩺  Lauf-Status / letzte Abstuerze (letzter_lauf.json)\n"
    printf "0) ❌   Exit\n"
    echo ""
    printf "Choice: "
    read choice

    case $choice in
        1)
            if [ ! -f "$LOG_FILE" ]; then
                printf "${RED}Logdatei nicht gefunden: %s${NC}\n" "$LOG_FILE"
                wait_for_key
            else
                printf "${YELLOW}Live-Logs (letzte 20 Zeilen) – Beenden mit Strg+C${NC}\n\n"
                tail -n 20 -f "$LOG_FILE"
            fi
            ;;
        2)
            if [ -f "$LOG_FILE" ]; then
                tail -n 200 "$LOG_FILE" | more
            else
                printf "${RED}Logdatei nicht gefunden: %s${NC}\n" "$LOG_FILE"
            fi
            wait_for_key
            ;;
        3)
            if [ -f "$ERROR_LOG_FILE" ] && [ -s "$ERROR_LOG_FILE" ]; then
                tail -n 200 "$ERROR_LOG_FILE" | more
            elif [ -f "$ERROR_LOG_FILE" ]; then
                printf "${GREEN}error.log ist leer – keine Fehler!${NC}\n"
            else
                printf "${RED}error.log nicht gefunden: %s${NC}\n" "$ERROR_LOG_FILE"
            fi
            wait_for_key
            ;;
        4) sh "$SCRIPT_DIR/rpi-deploy.sh"; wait_for_key ;;
        5)
            printf "${CYAN}Starte Service neu...${NC}\n"
            sudo systemctl restart wpsteuerung \
                && printf "${GREEN}✓ Service neu gestartet.${NC}\n" \
                || printf "${RED}✗ Neustart fehlgeschlagen!${NC}\n"
            wait_for_key
            ;;
        6)
            printf "${CYAN}Stoppe Service...${NC}\n"
            sudo systemctl stop wpsteuerung \
                && printf "${GREEN}✓ Service gestoppt.${NC}\n" \
                || printf "${RED}✗ Stop fehlgeschlagen!${NC}\n"
            wait_for_key
            ;;
        7)
            printf "${CYAN}Starte Service...${NC}\n"
            sudo systemctl start wpsteuerung \
                && printf "${GREEN}✓ Service gestartet.${NC}\n" \
                || printf "${RED}✗ Start fehlgeschlagen!${NC}\n"
            wait_for_key
            ;;
        8) ls -la "$TARGET_DIR"; wait_for_key ;;
        9) 
            CSV_PATH="$TARGET_DIR/csv log/heizungsdaten.csv"
            if [ -f "$CSV_PATH" ]; then
                TMP_DIR=$(mktemp -d 2>/dev/null || echo "/tmp/wp-manager.$$")
                mkdir -p "$TMP_DIR"
                printf "${CYAN}Bereite heizungsdaten.csv für Upload vor (%s)...${NC}\n" "$(du -h "$CSV_PATH" | cut -f1)"
                cp "$CSV_PATH" "$TMP_DIR/heizungsdaten_upload.csv"
                gzip -f "$TMP_DIR/heizungsdaten_upload.csv"
                printf "${CYAN}Lade zu Catbox.moe hoch...${NC}\n"
                UPLOAD_URL=$(curl -fsS -F "reqtype=fileupload" \
                    -F "fileToUpload=@$TMP_DIR/heizungsdaten_upload.csv.gz" \
                    https://catbox.moe/user/api.php)
                CURL_RC=$?
                if [ $CURL_RC -eq 0 ] && [ -n "$UPLOAD_URL" ] && printf '%s' "$UPLOAD_URL" | grep -q '^https://'; then
                    printf "${GREEN}✓ Upload erfolgreich! (${YELLOW}%s komprimiert${GREEN})${NC}\n" "$(du -h "$TMP_DIR/heizungsdaten_upload.csv.gz" | cut -f1)"
                    printf "URL: ${BLUE}%s${NC}\n" "$UPLOAD_URL"
                else
                    printf "${RED}✗ Fehler beim Upload (curl RC=%s)!${NC}\n" "$CURL_RC"
                    [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: %s${NC}\n" "$UPLOAD_URL"
                fi
                rm -rf "$TMP_DIR"
            else
                printf "${RED}Fehler: $CSV_PATH nicht gefunden!${NC}\n"
            fi
            wait_for_key
            ;;
        10)
            printf "${CYAN}Aktualisiere WP-Manager (Updater-Repo)...${NC}\n"
            # Vorab-Check: Lokale Aenderungen an getrackten Dateien blockieren
            # den Pull ("would be overwritten by merge"). Klassiker: Die vom
            # Service fortlaufend geschriebene sonnen_prognose.csv.
            REPO_ROOT=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)
            GIT_DIRTY_TRACKED=$(git -C "$REPO_ROOT" status --porcelain -uno 2>/dev/null | wc -l | tr -d ' ')
            if [ "$GIT_DIRTY_TRACKED" != "0" ] && [ -n "$GIT_DIRTY_TRACKED" ]; then
                printf "${YELLOW}⚠ %s lokale Aenderungen blockieren den Pull:${NC}\n" "$GIT_DIRTY_TRACKED"
                git -C "$REPO_ROOT" status --porcelain -uno 2>/dev/null | head -5 | sed 's/^/   /'
                printf "Diese Aenderungen verwerfen (git reset --hard)? (j/n): "
                read reply
                case "$reply" in
                    [Jj]*)
                        if git -C "$REPO_ROOT" reset --hard >/dev/null 2>&1; then
                            printf "${GREEN}✓ Lokale Aenderungen verworfen.${NC}\n"
                        else
                            printf "${RED}✗ Reset fehlgeschlagen – bitte manuell loesen.${NC}\n"
                            wait_for_key
                            continue
                        fi
                        ;;
                    *)
                        printf "${YELLOW}Abgebrochen. Alternativ: Option 4 (Deploy) nutzt denselben Ablauf.${NC}\n"
                        wait_for_key
                        continue
                        ;;
                esac
            fi
            OLD_COMMIT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null)
            if git -C "$SCRIPT_DIR" pull --ff-only; then
                NEW_COMMIT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null)
                if [ -n "$OLD_COMMIT" ] && [ "$OLD_COMMIT" != "$NEW_COMMIT" ]; then
                    printf "${GREEN}✓ Update installiert: %s → %s. Starte Skript neu...${NC}\n" "$OLD_COMMIT" "$NEW_COMMIT"
                    sleep 1
                    exec sh "$0" "$@"
                else
                    printf "${GREEN}✓ Bereits auf neuestem Stand (%s).${NC}\n" "${NEW_COMMIT:-?}"
                fi
            else
                printf "${RED}✗ Git pull fehlgeschlagen (offline / Konflikte / keine Änderungen möglich?)${NC}\n"
            fi
            wait_for_key
            ;;

        11)
            printf "${CYAN}Bis zu welchem Datum/Uhrzeit zurueck?${NC}\n"
            printf "Format: ${YELLOW}YYYY-MM-DD HH:MM:SS${NC} (z.B. ${GREEN}2026-08-08 16:00:00${NC})\n"
            printf "Eingabe: "
            read log_target
            if [ -z "$log_target" ]; then
                printf "${RED}Keine Eingabe, Abbruch.${NC}\n"
                wait_for_key
            else
                printf "${CYAN}Wieviele Zeilen? (Default 50):${NC} "
                read log_lines
                log_lines="${log_lines:-50}"
                if ! is_number "$log_lines"; then
                    printf "${RED}'%s' ist keine Zahl – verwende Standardwert 50.${NC}\n" "$log_lines"
                    log_lines=50
                    sleep 1
                fi
                query_logs_by_time "$log_target" "$log_lines"
            fi
            ;;
        12)
            printf "${CYAN}Letzte wieviele Stunden anzeigen? (z.B. 2, 4, 24):${NC} "
            read log_hours
            log_hours="${log_hours:-2}"
            if ! is_number "$log_hours"; then
                printf "${RED}'%s' ist keine Zahl – verwende Standardwert 2.${NC}\n" "$log_hours"
                log_hours=2
                sleep 1
            fi
            printf "${CYAN}Wieviele Zeilen? (Default 200):${NC} "
            read log_lines
            log_lines="${log_lines:-200}"
            if ! is_number "$log_lines"; then
                printf "${RED}'%s' ist keine Zahl – verwende Standardwert 200.${NC}\n" "$log_lines"
                log_lines=200
                sleep 1
            fi
            query_logs_by_duration "$log_hours" "$log_lines"
            ;;
        13)
            systemctl status wpsteuerung --no-pager -l 2>/dev/null \
                || journalctl -u wpsteuerung -n 50 --no-pager 2>/dev/null \
                || printf "${RED}Service 'wpsteuerung' nicht gefunden.${NC}\n"
            wait_for_key
            ;;
        14)
            if [ -f "$LOG_FILE" ]; then
                TMP_DIR=$(mktemp -d 2>/dev/null || echo "/tmp/wp-manager.$$")
                mkdir -p "$TMP_DIR"
                printf "${CYAN}Bereite heizungssteuerung.log für Upload vor (%s)...${NC}\n" "$(du -h "$LOG_FILE" | cut -f1)"
                cp "$LOG_FILE" "$TMP_DIR/heizungssteuerung_upload.log"
                gzip -f "$TMP_DIR/heizungssteuerung_upload.log"
                printf "${CYAN}Lade zu Catbox.moe hoch...${NC}\n"
                UPLOAD_URL=$(curl -fsS -F "reqtype=fileupload" \
                    -F "fileToUpload=@$TMP_DIR/heizungssteuerung_upload.log.gz" \
                    https://catbox.moe/user/api.php)
                CURL_RC=$?
                if [ $CURL_RC -eq 0 ] && [ -n "$UPLOAD_URL" ] && printf '%s' "$UPLOAD_URL" | grep -q '^https://'; then
                    printf "${GREEN}✓ Upload erfolgreich! (${YELLOW}%s komprimiert${GREEN})${NC}\n" "$(du -h "$TMP_DIR/heizungssteuerung_upload.log.gz" | cut -f1)"
                    printf "URL: ${BLUE}%s${NC}\n" "$UPLOAD_URL"
                else
                    printf "${RED}✗ Fehler beim Upload (curl RC=%s)!${NC}\n" "$CURL_RC"
                    [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: %s${NC}\n" "$UPLOAD_URL"
                fi
                rm -rf "$TMP_DIR"
            else
                printf "${RED}Fehler: $LOG_FILE nicht gefunden!${NC}\n"
            fi
            wait_for_key
            ;;
        15)
            ENT_LOG_FILE="${WPS_ENT_LOG_FILE:-$TARGET_DIR/entscheidungs_log.jsonl}"
            if [ ! -f "$ENT_LOG_FILE" ]; then
                printf "${RED}entscheidungs_log.jsonl nicht gefunden: %s${NC}\\n" "$ENT_LOG_FILE"
                wait_for_key
            else
                printf "${CYAN}Wie viele Entscheidungen anzeigen? (Default 30):${NC} "
                read ent_lines
                ent_lines="${ent_lines:-30}"
                if ! is_number "$ent_lines"; then
                    printf "${RED}'%s' ist keine Zahl – verwende Standardwert 30.${NC}\\n" "$ent_lines"
                    ent_lines=30
                    sleep 1
                fi
                python3 -c "
import sys
sys.path.insert(0, '$TARGET_DIR')
from entscheidungs_log import historie, kpis
rows = historie(72, limit=${ent_lines})
print('Letzte %d Entscheidungen (72 h):' % len(rows))
print('-' * 70)
for e in rows:
    laeuft = 'EIN' if e.get('kompressor_laeuft') else 'AUS'
    soll = 'EIN' if e.get('soll_einschalten') else 'AUS'
    f = e.get('feedin_w')
    f_txt = ('%.0fW' % f) if isinstance(f, (int, float)) else '-'
    print('%s | %s | soll=%s | WP=%s | feedin=%s | SOC=%s | unten=%sC | %s' % (
        e.get('ts', '?'), e.get('gewinner', '-'), soll, laeuft, f_txt,
        e.get('soc'), e.get('t_unten'), str(e.get('grund', ''))[:60]))
print('-' * 70)
try:
    k = kpis()
    print('KPIs heute: %s' % k.get('heute'))
    print('KPIs 7 Tage: %s' % k.get('sieben_tage'))
except Exception as ex:
    print('KPI-Abfrage fehlgeschlagen: %s' % ex)
" 2>&1 | more
                wait_for_key
            fi
            ;;
        16)
            ENT_LOG_FILE="${WPS_ENT_LOG_FILE:-$TARGET_DIR/entscheidungs_log.jsonl}"
            if [ -f "$ENT_LOG_FILE" ]; then
                TMP_DIR=$(mktemp -d 2>/dev/null || echo "/tmp/wp-manager.$$")
                mkdir -p "$TMP_DIR"
                printf "${CYAN}Bereite entscheidungs_log.jsonl fuer Upload vor (%s)...${NC}\\n" "$(du -h "$ENT_LOG_FILE" | cut -f1)"
                cp "$ENT_LOG_FILE" "$TMP_DIR/entscheidungs_log_upload.jsonl"
                gzip -f "$TMP_DIR/entscheidungs_log_upload.jsonl"
                printf "${CYAN}Lade zu Catbox.moe hoch...${NC}\\n"
                UPLOAD_URL=$(curl -fsS -F "reqtype=fileupload" \
                    -F "fileToUpload=@$TMP_DIR/entscheidungs_log_upload.jsonl.gz" \
                    https://catbox.moe/user/api.php)
                CURL_RC=$?
                if [ $CURL_RC -eq 0 ] && [ -n "$UPLOAD_URL" ] && printf '%s' "$UPLOAD_URL" | grep -q '^https://'; then
                    printf "${GREEN}✓ Upload erfolgreich! (${YELLOW}%s komprimiert${GREEN})${NC}\\n" "$(du -h "$TMP_DIR/entscheidungs_log_upload.jsonl.gz" | cut -f1)"
                    printf "URL: ${BLUE}%s${NC}\\n" "$UPLOAD_URL"
                else
                    printf "${RED}✗ Fehler beim Upload (curl RC=%s)!${NC}\\n" "$CURL_RC"
                    [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: %s${NC}\\n" "$UPLOAD_URL"
                fi
                rm -rf "$TMP_DIR"
            else
                printf "${RED}Fehler: entscheidungs_log.jsonl nicht gefunden!${NC}\\n"
            fi
            wait_for_key
            ;;
        17)
            ZYKLEN_CSV=$(finde_zyklen_csv)
            if [ -z "$ZYKLEN_CSV" ]; then
                printf "${RED}Keine zyklen.csv gefunden - fuehre zuerst Analyse/log_analyse.py\\n"
                printf "                aus (erzeugt logs/analyse_*/zyklen.csv).${NC}\\n"
                wait_for_key
            else
                printf "${CYAN}Wie viele Zyklen anzeigen? (Default 20):${NC} "
                read zyk_n
                zyk_n="${zyk_n:-20}"
                if ! is_number "$zyk_n"; then
                    printf "${RED}'%s' ist keine Zahl – verwende Standardwert 20.${NC}\\n" "$zyk_n"
                    zyk_n=20
                    sleep 1
                fi
                printf "${CYAN}Neueste Analyse: %s${NC}\\n" "$ZYKLEN_CSV"
                # BOM (UTF-8-Signatur) bleibt als Spalte 1 sichtbar
                if command -v column >/dev/null 2>&1; then
                    {
                        echo
                        head -n1 "$ZYKLEN_CSV"
                        tail -n "$zyk_n" "$ZYKLEN_CSV"
                    } | column -s ';' -t
                else
                    tail -n "$zyk_n" "$ZYKLEN_CSV"
                fi
                wait_for_key
            fi
            ;;
        18)
            ZYKLEN_CSV=$(finde_zyklen_csv)
            if [ -n "$ZYKLEN_CSV" ]; then
                TMP_DIR=$(mktemp -d 2>/dev/null || echo "/tmp/wp-manager.$$")
                mkdir -p "$TMP_DIR"
                printf "${CYAN}Bereite zyklen.csv fuer Upload vor (%s)...${NC}\\n" "$(du -h "$ZYKLEN_CSV" | cut -f1)"
                cp "$ZYKLEN_CSV" "$TMP_DIR/zyklen_upload.csv"
                gzip -f "$TMP_DIR/zyklen_upload.csv"
                printf "${CYAN}Lade zu Catbox.moe hoch...${NC}\\n"
                UPLOAD_URL=$(curl -fsS -F "reqtype=fileupload" \
                    -F "fileToUpload=@$TMP_DIR/zyklen_upload.csv.gz" \
                    https://catbox.moe/user/api.php)
                CURL_RC=$?
                if [ $CURL_RC -eq 0 ] && [ -n "$UPLOAD_URL" ] && printf '%s' "$UPLOAD_URL" | grep -q '^https://'; then
                    printf "${GREEN}✓ Upload erfolgreich! (${YELLOW}%s komprimiert${GREEN})${NC}\\n" "$(du -h "$TMP_DIR/zyklen_upload.csv.gz" | cut -f1)"
                    printf "URL: ${BLUE}%s${NC}\\n" "$UPLOAD_URL"
                else
                    printf "${RED}✗ Fehler beim Upload (curl RC=%s)!${NC}\\n" "$CURL_RC"
                    [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: %s${NC}\\n" "$UPLOAD_URL"
                fi
                rm -rf "$TMP_DIR"
            else
                printf "${RED}Fehler: keine zyklen.csv gefunden (Analyse-Lauf fehlt).${NC}\\n"
            fi
            wait_for_key
            ;;
        19)
            LAUF_JSON="$TARGET_DIR/letzter_lauf.json"
            printf "${CYAN}=== Lauf-Status / letzte Abstuerze ===${NC}\\n"
            if [ -f "$LAUF_JSON" ]; then
                printf "${DIM}Datei: %s${NC}\\n" "$LAUF_JSON"
                cat "$LAUF_JSON"
                printf "\\n\\n"
            else
                printf "${YELLOW}letzter_lauf.json existiert noch nicht.${NC}\\n"
                printf "${DIM}(wird beim naechsten Start der Steuerung angelegt)${NC}\\n\\n"
            fi

            printf "${CYAN}--- Bewertung (startup_diagnose) ---${NC}\\n"
            python3 -c "
import sys
sys.path.insert(0, '$TARGET_DIR')
try:
    import startup_diagnose as sd
except Exception as ex:
    print('startup_diagnose nicht verfuegbar: %s' % ex)
    raise SystemExit(0)

info = sd.pruefe_lauf_start_grund()
if sd.lauf_ist_aktiv():
    print('OK: Die Steuerung laeuft gerade - die Statusdatei gehoert zum aktiven Prozess.')
elif info.get('erster_start'):
    print('Kein Vorlauf verzeichnet (erster Start seit Einfuehrung der Diagnose).')
elif info.get('unsauber'):
    print('WARNUNG: Letzter Lauf wurde NICHT sauber beendet.')
    print('  Vorheriger Start:      %s' % (info.get('vorheriger_start') or '?'))
    print('  Letztes sauberes Ende: %s' % (info.get('vorheriges_ende') or 'kein Eintrag'))
    if info.get('oom_hinweis'):
        print('  Kernel-Log: %s' % info['oom_hinweis'])
        print('  => Verdacht: OOM-Kill (Speicher).')
    else:
        print('  Kein OOM-Hinweis im Kernel-Log lesbar -> Crash / harter Reset / Strom?')
else:
    print('OK: Letzter Lauf wurde sauber beendet.')
print()
print('Speicher jetzt: %s' % sd.formatiere_speicher(sd.speicher_werte()))
" 2>&1 | more

            printf "\\n${CYAN}--- Service / System ---${NC}\\n"
            printf "Neustarts durch systemd: %s\\n" "$(systemctl show wpsteuerung -p NRestarts --value 2>/dev/null)"
            free -m 2>/dev/null | awk 'NR==1 || NR==2'
            if command -v vcgencmd >/dev/null 2>&1; then
                printf "Throttling (Strom/Temp): %s  ${DIM}(0x0 = unauffaellig)${NC}\\n" "$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
            fi
            OOM_COUNT=$(dmesg 2>/dev/null | grep -c "Out of memory")
            [ -z "$OOM_COUNT" ] && OOM_COUNT="n/a"
            printf "OOM-Ereignisse im Kernel-Ringpuffer: %s\\n" "$OOM_COUNT"
            printf "${DIM}Hinweis: Der Ringpuffer ist begrenzt - 0 bedeutet nicht,\\n"
            printf "dass es nie einen OOM gab (siehe Journal/Historie).${NC}\\n"
            wait_for_key
            ;;
        0) exit 0 ;;
        *)
            printf "${RED}Ungültige Auswahl: '%s'${NC}\n" "$choice"
            sleep 1
            ;;
    esac
done
