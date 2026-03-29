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
    printf "11) 🔙  Rollback (Ältere Version einspielen)\n"
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
            printf "${BLUE}Verfügbare CSV-Dateien zum Upload:${NC}\n"
            
            # Sammle Dateien sauber mit Zeilenumbruch als Trenner
            # (sh-kompatibel, Leerzeichen-sicher)
            TMP_FILES_LIST="/tmp/wp_files_$$.txt"
            echo "$TARGET_DIR/csv log/heizungsdaten.csv" > "$TMP_FILES_LIST"
            if [ -d "$TARGET_DIR/backup" ]; then
                ls -1t "$TARGET_DIR/backup"/backup_*.csv 2>/dev/null >> "$TMP_FILES_LIST"
            fi
            
            # Zähle verfügbare Dateien
            NUM_FILES=0
            i=1
            while IFS= read -r f; do
                if [ -n "$f" ] && [ -f "$f" ]; then
                    printf "${YELLOW}%2d)${NC} %s\n" $i "$(basename "$f")"
                    i=$((i+1))
                    NUM_FILES=$((NUM_FILES+1))
                fi
            done < "$TMP_FILES_LIST"
            
            if [ "$NUM_FILES" -eq 0 ]; then
                printf "${RED}Keine CSV-Dateien gefunden!${NC}\n"
            else
                printf "\nWähle Datei(en) (z.B. '1', '1 2 4' oder '1,3', 0 zum Abbrechen): "
                read csv_choices
                
                # Wenn 0 oder leer -> Abbruch
                if [ -z "$csv_choices" ] || [ "$csv_choices" = "0" ]; then
                    printf "${YELLOW}Abgebrochen.${NC}\n"
                else
                    # Verarbeite Auswahl (Kommas durch Leerzeichen ersetzen für die Loop)
                    csv_choices=$(echo "$csv_choices" | tr ',' ' ')
                    
                    # Prüfen ob mehrere Dateien gewählt wurden
                    CHOICE_COUNT=$(echo "$csv_choices" | wc -w)
                    MERGE_ALL="n"
                    if [ "$CHOICE_COUNT" -gt 1 ]; then
                        printf "${YELLOW}Möchtest du alle $CHOICE_COUNT Dateien in eine Datei zusammenführen? (j/n): ${NC}"
                        read merge_prompt
                        if [ "$merge_prompt" = "j" ] || [ "$merge_prompt" = "y" ]; then
                            MERGE_ALL="j"
                        fi
                    fi

                    if [ "$MERGE_ALL" = "j" ]; then
                        # --- MODUS: ZUSAMMENGEFÜHRTER UPLOAD ---
                        MERGED_FILE="./upload_merged.csv"
                        rm -f "$MERGED_FILE" "$MERGED_FILE.gz"
                        printf "${CYAN}Führe $CHOICE_COUNT Dateien zusammen...${NC}\n"
                        
                        processed_count=0
                        for choice in $csv_choices; do
                            if [ "$choice" -gt 0 ] 2>/dev/null && [ "$choice" -le "$NUM_FILES" ]; then
                                CSV_PATH=$(sed -n "${choice}p" "$TMP_FILES_LIST")
                                if [ "$processed_count" -eq 0 ]; then
                                    # Erste Datei mit Header kopieren
                                    cat "$CSV_PATH" > "$MERGED_FILE"
                                else
                                    # Weitere Dateien ohne Header anhängen
                                    tail -n +2 "$CSV_PATH" >> "$MERGED_FILE"
                                fi
                                processed_count=$((processed_count+1))
                            fi
                        done
                        
                        if [ "$processed_count" -gt 0 ]; then
                            printf "${CYAN}Komprimiere und lade Bundle hoch...${NC}\n"
                            gzip -f "$MERGED_FILE"
                            UPLOAD_URL=$(curl -s -F "reqtype=fileupload" -F "fileToUpload=@${MERGED_FILE}.gz" https://catbox.moe/user/api.php)
                            
                            if [ $? -eq 0 ] && [ -n "$UPLOAD_URL" ] && echo "$UPLOAD_URL" | grep -q "http"; then
                                printf "${GREEN}Zusammengeführter Upload erfolgreich!${NC}\n"
                                printf "${YELLOW}URL: ${BLUE}$UPLOAD_URL${NC}\n"
                                echo "$(date): [MERGED] ($processed_count Dateien) -> $UPLOAD_URL" >> "$TARGET_DIR/upload_history.log"
                            else
                                printf "${RED}Fehler beim Upload des Bundles!${NC}\n"
                                [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: $UPLOAD_URL${NC}\n"
                            fi
                            rm -f "${MERGED_FILE}.gz"
                        fi
                    else
                        # --- MODUS: EINZELNER UPLOAD ---
                        for choice in $csv_choices; do
                            # Validierung der Wahl
                            if [ "$choice" -gt 0 ] 2>/dev/null && [ "$choice" -le "$NUM_FILES" ]; then
                                CSV_PATH=$(sed -n "${choice}p" "$TMP_FILES_LIST")
                                FNAME=$(basename "$CSV_PATH")
                                
                                printf "${CYAN}--- Upload: $FNAME ---${NC}\n"
                                rm -f "./upload_tmp.csv" "./upload_tmp.csv.gz"
                                cp "$CSV_PATH" "./upload_tmp.csv"
                                gzip -f "./upload_tmp.csv"
                                
                                printf "${CYAN}Sende zu Catbox.moe...${NC}\n"
                                UPLOAD_URL=$(curl -s -F "reqtype=fileupload" -F "fileToUpload=@./upload_tmp.csv.gz" https://catbox.moe/user/api.php)
                                
                                if [ $? -eq 0 ] && [ -n "$UPLOAD_URL" ] && echo "$UPLOAD_URL" | grep -q "http"; then
                                    printf "${GREEN}Erfolgreich!${NC}\n"
                                    printf "${YELLOW}URL: ${BLUE}$UPLOAD_URL${NC}\n"
                                    echo "$(date): [MULTI] Upload $FNAME -> $UPLOAD_URL" >> "$TARGET_DIR/upload_history.log"
                                else
                                    printf "${RED}Fehler beim Upload von $FNAME!${NC}\n"
                                    [ -n "$UPLOAD_URL" ] && printf "${RED}Antwort: $UPLOAD_URL${NC}\n"
                                fi
                                rm -f "./upload_tmp.csv.gz"
                            else
                                printf "${RED}Überspringe ungültige Auswahl: $choice${NC}\n"
                            fi
                        done
                    fi
                fi
            fi
            rm -f "$TMP_FILES_LIST"
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
            printf "${CYAN}Starte Deployment-Menü für Rollback... Bitte drücke gleich '7'.${NC}\n"
            sleep 1
            sh ./rpi-deploy.sh; wait_for_key 
            ;;
        0) exit 0 ;;
        *) sleep 1 ;;
    esac
done
