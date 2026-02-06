#!/bin/sh
# wp-manager.sh - Management script for WPSteuerung
# Located in RPI_updater repo, targets ../WPSteuerung

TARGET_DIR="../Steuerung"
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

    clear
    printf "${BLUE}==============================================${NC}\n"
    printf "${BLUE}           WPSteuerung Manager v1.3           ${NC}\n"
    printf "${BLUE}==============================================${NC}\n"
    printf "Target:  $TARGET_DIR\n"
    printf "Branch:  ${YELLOW}$CUR_BRANCH${NC}\n"
    printf "Commit:  $CUR_COMMIT\n"
    printf "Service: $SVC_STATUS\n"
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
    printf "0) ❌   Exit\n"
    echo ""
    printf "Choice: "
    read choice

    case $choice in
        1) tail -f "$LOG_FILE" ;;
        2) tail -n 200 "$LOG_FILE" | more; wait_for_key ;;
        3) tail -n 200 "$ERROR_LOG_FILE" | more; wait_for_key ;;
        4) sh ./rpi-deploy.sh; wait_for_key ;;
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
        0) exit 0 ;;
        *) sleep 1 ;;
    esac
done
