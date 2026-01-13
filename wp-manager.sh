#!/bin/sh
# wp-manager.sh - Interaktives Menü für die WPSteuerung
# POSIX-konform (läuft mit sh/dash auf dem Raspberry Pi)

# Farben definieren
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Funktion für farbige Ausgaben
print_header() {
    clear
    echo "${BLUE}========================================${NC}"
    echo "${BLUE}   WPSteuerung Manager v1.0   ${NC}"
    echo "${BLUE}========================================${NC}"
    echo ""
}

wait_for_key() {
    echo ""
    echo "${YELLOW}Drücke Enter um zurück zum Menü zu gelangen...${NC}"
    read dummy
}

# Hauptschleife
while true; do
    print_header
    echo "1) 📜 Live-Logs anzeigen (tail -f)"
    echo "2) 📄 Letzte 200 Zeilen Log (tail -n 200)"
    echo "3) 🚀 Update & Deploy (rpi-deploy.sh)"
    echo "4) 🔄 Service neustarten (systemctl restart)"
    echo "5) ⏹️  Service stoppen (systemctl stop)"
    echo "6) ▶️  Service starten (systemctl start)"
    echo "7) 📂 Dateien auflisten (ls -la)"
    echo "8) 🐍 Manuell starten (in Virtual Env)"
    echo "9) ❌ Beenden"
    echo ""
    printf "Deine Wahl: "
    read choice

    case $choice in
        1)
            echo "${GREEN}Starte Live-Log (STRG+C zum Beenden)...${NC}"
            tail -f heizungssteuerung.log
            ;;
        2)
            echo "${GREEN}Letzte 200 Zeilen:${NC}"
            tail -n 200 heizungssteuerung.log | more
            wait_for_key
            ;;
        3)
            echo "${GREEN}Starte Deployment-Script...${NC}"
            if [ -f "./rpi-deploy.sh" ]; then
                sh ./rpi-deploy.sh
            else
                echo "${RED}Fehler: rpi-deploy.sh nicht gefunden!${NC}"
                wait_for_key
            fi
            ;;
        4)
            echo "${YELLOW}Starte Service neu...${NC}"
            sudo systemctl restart wpsteuerung
            if [ $? -eq 0 ]; then
                echo "${GREEN}Erfolgreich neugestartet!${NC}"
            else
                echo "${RED}Fehler beim Neustart!${NC}"
            fi
            wait_for_key
            ;;
        5)
            echo "${YELLOW}Stoppe Service...${NC}"
            sudo systemctl stop wpsteuerung
            echo "${GREEN}Gestoppt.${NC}"
            wait_for_key
            ;;
        6)
            echo "${YELLOW}Starte Service...${NC}"
            sudo systemctl start wpsteuerung
            echo "${GREEN}Gestartet.${NC}"
            wait_for_key
            ;;
        7)
            echo "${GREEN}Dateiliste:${NC}"
            ls -la
            wait_for_key
            ;;
        8)
            echo "${YELLOW}Stoppe Hintergrund-Service...${NC}"
            sudo systemctl stop wpsteuerung
            echo "${GREEN}Aktiviere Umgebung und starte main.py...${NC}"
            echo "Drücke STRG+C zum Beenden (Service muss danach manuell gestartet werden!)"
            
            # Versuche verschiedene Venv-Pfade
            if [ -f "venv/bin/activate" ]; then
                . venv/bin/activate
            elif [ -f ".venv/bin/activate" ]; then
                . .venv/bin/activate
            elif [ -f "env/bin/activate" ]; then
                . env/bin/activate
            else
                echo "${RED}Kein Virtual Environment gefunden (venv, .venv, env)!${NC}"
                echo "Versuche es ohne Aktivierung..."
            fi
            
            python3 main.py
            wait_for_key
            ;;
        9)
            echo "Bis bald!"
            exit 0
            ;;
        *)
            echo "${RED}Ungültige Eingabe!${NC}"
            sleep 1
            ;;
    esac
done
