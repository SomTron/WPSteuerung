#!/bin/bash
# Raspberry Pi WPSteuerung Deployment Script
# Verwendung: ./rpi-deploy.sh

set -e  # Exit bei Fehler

# Farben fuer Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Konfiguration
REPO_DIR="/home/pi/WPSteuerung/WPSteuerung"
SERVICE_NAME="wpsteuerung"  # Anpassen an deinen Service-Namen

echo -e "${CYAN}=========================================${NC}"
echo -e "${CYAN}  WPSteuerung Deployment auf Raspberry Pi${NC}"
echo -e "${CYAN}=========================================${NC}"

# Pruefe ob Repository existiert
if [ ! -d "$REPO_DIR" ]; then
    echo -e "${RED}Fehler: Repository nicht gefunden in $REPO_DIR${NC}"
    echo -e "${YELLOW}Fuehre erst die Ersteinrichtung durch!${NC}"
    exit 1
fi

cd "$REPO_DIR"

# Zeige aktuellen Branch
CURRENT_BRANCH=$(git branch --show-current)
echo -e "\n${YELLOW}Aktueller Branch: $CURRENT_BRANCH${NC}"

# Zeige Git Status
echo -e "\n${CYAN}Git Status:${NC}"
git status --short

# Warne bei lokalen Aenderungen
if [ -n "$(git status --porcelain)" ]; then
    echo -e "${RED}WARNUNG: Es gibt lokale Aenderungen!${NC}"
    read -p "Moechtest du diese verwerfen? (j/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Jj]$ ]]; then
        git reset --hard
        echo -e "${GREEN}Lokale Aenderungen verworfen.${NC}"
    else
        echo -e "${YELLOW}Abgebrochen.${NC}"
        exit 1
    fi
fi

# Hauptmenue
echo -e "\n${CYAN}Was moechtest du tun?${NC}"
echo "1. Code aktualisieren (aktuellen Branch pullen)"
echo "2. Branch wechseln"
echo "3. Branch wechseln UND aktualisieren"
echo "4. Nur Service neu starten"
echo "5. Status anzeigen"
echo "0. Abbrechen"

read -p "Waehle (0-5): " choice

case $choice in
    1)
        # Pull current branch
        echo -e "\n${CYAN}Aktualisiere Branch '$CURRENT_BRANCH'...${NC}"
        git fetch --all
        git pull origin $CURRENT_BRANCH
        
        echo -e "${GREEN}Code aktualisiert!${NC}"
        
        read -p "Service neu starten? (j/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Jj]$ ]]; then
            sudo systemctl restart $SERVICE_NAME
            echo -e "${GREEN}Service neu gestartet!${NC}"
        fi
        ;;
        
    2)
        # Switch branch
        echo -e "\n${CYAN}Verfuegbare Branches:${NC}"
        git branch -a | grep -v HEAD
        
        read -p "Zu welchem Branch wechseln? (master/android-api/funktioniert): " target_branch
        
        echo -e "${CYAN}Wechsle zu Branch '$target_branch'...${NC}"
        git fetch --all
        git checkout $target_branch
        
        echo -e "${GREEN}Zu Branch '$target_branch' gewechselt!${NC}"
        
        read -p "Service neu starten? (j/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Jj]$ ]]; then
            sudo systemctl restart $SERVICE_NAME
            echo -e "${GREEN}Service neu gestartet!${NC}"
        fi
        ;;
        
    3)
        # Switch and pull
        echo -e "\n${CYAN}Verfuegbare Branches:${NC}"
        git branch -a | grep -v HEAD
        
        read -p "Zu welchem Branch wechseln? (master/android-api/funktioniert): " target_branch
        
        echo -e "${CYAN}Wechsle zu Branch '$target_branch'...${NC}"
        git fetch --all
        git checkout $target_branch
        git pull origin $target_branch
        
        echo -e "${GREEN}Branch gewechselt und aktualisiert!${NC}"
        
        read -p "Service neu starten? (j/n): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Jj]$ ]]; then
            sudo systemctl restart $SERVICE_NAME
            echo -e "${GREEN}Service neu gestartet!${NC}"
        fi
        ;;
        
    4)
        # Restart service only
        echo -e "${CYAN}Starte Service neu...${NC}"
        sudo systemctl restart $SERVICE_NAME
        echo -e "${GREEN}Service neu gestartet!${NC}"
        ;;
        
    5)
        # Show status
        echo -e "\n${CYAN}=== Git Status ===${NC}"
        echo "Branch: $(git branch --show-current)"
        echo "Letzter Commit: $(git log -1 --oneline)"
        
        echo -e "\n${CYAN}=== Service Status ===${NC}"
        sudo systemctl status $SERVICE_NAME --no-pager -l
        
        echo -e "\n${CYAN}=== Letzte 20 Log-Zeilen ===${NC}"
        sudo journalctl -u $SERVICE_NAME -n 20 --no-pager
        ;;
        
    0)
        echo -e "${YELLOW}Abgebrochen.${NC}"
        exit 0
        ;;
        
    *)
        echo -e "${RED}Ungueltige Auswahl!${NC}"
        exit 1
        ;;
esac

echo -e "\n${GREEN}=== Aktueller Status ===${NC}"
echo "Branch: $(git branch --show-current)"
echo "Letzter Commit: $(git log -1 --oneline)"

# Zeige Service-Status
if systemctl is-active --quiet $SERVICE_NAME; then
    echo -e "Service: ${GREEN}AKTIV${NC}"
else
    echo -e "Service: ${RED}INAKTIV${NC}"
fi

echo -e "\n${CYAN}Fertig!${NC}"
