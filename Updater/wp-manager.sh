#!/bin/sh
# wp-manager.sh - Management script for WPSteuerung
# Located in Updater repo, targets ../Steuerung (relative to script location)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET_DIR="$(dirname "$SCRIPT_DIR")/Steuerung"
LOG_FILE="$TARGET_DIR/heizungssteuerung.log"
ERROR_LOG_FILE="$TARGET_DIR/error.log"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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
target = datetime.now() - timedelta(hours=${hours})
result, meta = query_logs(after=target, lines=${maxlines})
for line in result:
    sys.stdout.write(line)
if not result:
    print('Keine Logs in den letzten ${hours} Stunde(n) gefunden.')
" 2>&1 | more
    wait_for_key
}


wait_for_key() {
    printf "\n${YELLOW}Press Enter to return to menu...${NC}"
    read dummy
}

while true; do
    # Status Informationen abrufen
    CUR_BRANCH=$(cd "$TARGET_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "Unknown")
    CUR_COMMIT=$(cd "$TARGET_DIR" && git log -1 --oneline 2>/dev/null || echo "No commits")
    
        if systemctl is-active --quiet wpsteuerung; then
        SVC_STATUS="${GREEN}✓ AKTIV${NC}"
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

    clear
    printf "${BLUE}==============================================${NC}\n"
    printf "${BLUE}           WPSteuerung Manager v1.4           ${NC}\n"
    printf "${BLUE}==============================================${NC}\n"
    printf "Target:  $TARGET_DIR\n"
    printf "Branch:  ${YELLOW}$CUR_BRANCH${NC}\n"
    printf "Commit:  $CUR_COMMIT\n"
    printf "Service: $SVC_STATUS\n"
    printf "VPN:     $VPN_STATUS${VPN_INFO}\n"
    printf "${BLUE}----------------------------------------------${NC}\n\n"
    
    printf "1) 📜   Live-Logs (tail -f)\n"
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
    printf "0) ❌   Exit\n"
    echo ""
    printf "Choice: "
    read choice

    case $choice in
        1) tail -f "$LOG_FILE" ;;
        2) tail -n 200 "$LOG_FILE" | more; wait_for_key ;;
        3) tail -n 200 "$ERROR_LOG_FILE" | more; wait_for_key ;;
        4) sh "$SCRIPT_DIR/rpi-deploy.sh"; wait_for_key ;;
        5) sudo systemctl restart wpsteuerung; wait_for_key ;;
        6) sudo systemctl stop wpsteuerung; wait_for_key ;;
        7) sudo systemctl start wpsteuerung; wait_for_key ;;
        8) ls -la "$TARGET_DIR"; wait_for_key ;;
        9) 
            CSV_PATH="$TARGET_DIR/csv log/heizungsdaten.csv"
            if [ -f "$CSV_PATH" ]; then
                printf "${CYAN}Bereite heizungsdaten.csv für Upload vor...${NC}\n"
                cp "$CSV_PATH" "./heizungsdaten_upload.csv"
                gzip -f "./heizungsdaten_upload.csv"
                printf "${CYAN}Lade zu Catbox.moe hoch...${NC}\n"
                UPLOAD_URL=$(curl -F "reqtype=fileupload" -F "fileToUpload=@./heizungsdaten_upload.csv.gz" https://catbox.moe/user/api.php)
                if [ $? -eq 0 ] && [ -n "$UPLOAD_URL" ]; then
                    printf "${GREEN}Upload erfolgreich!${NC}\n"
                    printf "${YELLOW}URL: ${BLUE}$UPLOAD_URL${NC}\n"
                    # Optional: In die Zwischenablage kopieren oder in Log schreiben
                else
                    printf "${RED}Fehler beim Upload!${NC}\n"
                fi
                rm -f "./heizungsdaten_upload.csv.gz"
            else
                printf "${RED}Fehler: $CSV_PATH nicht gefunden!${NC}\n"
            fi
            wait_for_key
            ;;
        10)
            printf "${CYAN}Aktualisiere WP-Manager (RPI_updater)...${NC}\n"
            git pull
            printf "${GREEN}Update fertig. Starte Skript neu...${NC}\n"
            sleep 1
            exec sh "$0" "$@"
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
                query_logs_by_time "$log_target" "$log_lines"
            fi
            ;;
        12)
            printf "${CYAN}Letzte wieviele Stunden anzeigen? (z.B. 2, 4, 24):${NC} "
            read log_hours
            log_hours="${log_hours:-2}"
            printf "${CYAN}Wieviele Zeilen? (Default 200):${NC} "
            read log_lines
            log_lines="${log_lines:-200}"
            query_logs_by_duration "$log_hours" "$log_lines"
            ;;
        0) exit 0 ;;
        *) sleep 1 ;;
    esac
done
