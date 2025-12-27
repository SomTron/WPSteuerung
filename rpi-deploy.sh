#!/bin/sh
# Raspberry Pi WPSteuerung Deployment Script
# Verwendung: ./rpi-deploy.sh oder sh rpi-deploy.sh

set -e  # Exit bei Fehler

# Farben fuer Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Konfiguration
REPO_DIR="/home/patrik/WPSteuerung"
SERVICE_NAME="wpsteuerung"

printf "${CYAN}=========================================${NC}\n"
printf "${CYAN}  WPSteuerung Deployment auf Raspberry Pi${NC}\n"
printf "${CYAN}=========================================${NC}\n"

# Pruefe ob Repository existiert
if [ ! -d "$REPO_DIR" ]; then
    printf "${RED}Fehler: Repository nicht gefunden in $REPO_DIR${NC}\n"
    printf "${YELLOW}Fuehre erst die Ersteinrichtung durch!${NC}\n"
    exit 1
fi

cd "$REPO_DIR"

# Zeige aktuellen Branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
printf "\n${YELLOW}Aktueller Branch: $CURRENT_BRANCH${NC}\n"

# Zeige Git Status (ohne untracked files)
printf "\n${CYAN}Git Status (ohne untracked files):${NC}\n"
git status -uno --short

# Warne nur bei tatsaechlichen Aenderungen (ignore untracked files)
if [ -n "$(git status -uno --porcelain)" ]; then
    printf "${RED}WARNUNG: Es gibt lokale Aenderungen an getrackten Dateien!${NC}\n"
    printf "Moechtest du diese verwerfen? (j/n): "
    read reply
    case "$reply" in
        [Jj]*)
            git reset --hard
            printf "${GREEN}Lokale Aenderungen verworfen.${NC}\n"
            ;;
        *)
            printf "${YELLOW}Abgebrochen.${NC}\n"
            exit 1
            ;;
    esac
fi

# Hauptmenue
printf "\n${CYAN}Was moechtest du tun?${NC}\n"
printf "1. Code aktualisieren (aktuellen Branch pullen)\n"
printf "2. Branch wechseln\n"
printf "3. Branch wechseln UND aktualisieren\n"
printf "4. Nur Service neu starten\n"
printf "5. Status anzeigen\n"
printf "6. WireGuard installieren/prüfen\n"
printf "0. Abbrechen\n"

printf "Waehle (0-6): "
read choice

case "$choice" in
    1)
        printf "\n${CYAN}Aktualisiere Branch '$CURRENT_BRANCH'...${NC}\n"
        git fetch --all
        git pull origin "$CURRENT_BRANCH"
        printf "${GREEN}Code aktualisiert!${NC}\n"
        
        printf "Service neu starten? (j/n): "
        read reply
        case "$reply" in
            [Jj]*)
                sudo systemctl restart "$SERVICE_NAME"
                printf "${GREEN}Service neu gestartet!${NC}\n"
                ;;
        esac
        ;;

    2)
        printf "\n${CYAN}Verfuegbare Branches:${NC}\n"
        git branch -a | grep -v HEAD

        printf "Zu welchem Branch wechseln? (master/android-api/funktioniert): "
        read target_branch

        printf "${CYAN}Wechsle zu Branch '$target_branch'...${NC}\n"
        git fetch --all
        git checkout "$target_branch"
        printf "${GREEN}Zu Branch '$target_branch' gewechselt!${NC}\n"
        
        printf "Service neu starten? (j/n): "
        read reply
        case "$reply" in
            [Jj]*)
                sudo systemctl restart "$SERVICE_NAME"
                printf "${GREEN}Service neu gestartet!${NC}\n"
                ;;
        esac
        ;;

    3)
        printf "\n${CYAN}Verfuegbare Branches:${NC}\n"
        git branch -a | grep -v HEAD

        printf "Zu welchem Branch wechseln? (master/android-api/funktioniert): "
        read target_branch
        
        printf "${CYAN}Wechsle zu Branch '$target_branch'...${NC}\n"
        git fetch --all
        git checkout "$target_branch"
        git pull origin "$target_branch"
        printf "${GREEN}Branch gewechselt und aktualisiert!${NC}\n"
        
        printf "Service neu starten? (j/n): "
        read reply
        case "$reply" in
            [Jj]*)
                sudo systemctl restart "$SERVICE_NAME"
                printf "${GREEN}Service neu gestartet!${NC}\n"
                ;;
        esac
        ;;

    4)
        printf "${CYAN}Starte Service neu...${NC}\n"
        sudo systemctl restart "$SERVICE_NAME"
        printf "${GREEN}Service neu gestartet!${NC}\n"
        ;;

    5)
        printf "\n${CYAN}=== Git Status ===${NC}\n"
        printf "Branch: %s\n" "$(git rev-parse --abbrev-ref HEAD)"
        printf "Letzter Commit: %s\n" "$(git log -1 --oneline)"

        printf "\n${CYAN}=== Service Status ===${NC}\n"
        sudo systemctl status "$SERVICE_NAME" --no-pager -l

        printf "\n${CYAN}=== Letzte 20 Log-Zeilen ===${NC}\n"
        sudo journalctl -u "$SERVICE_NAME" -n 20 --no-pager
        ;;

    6)
        printf "\n${CYAN}=== WireGuard Setup ===${NC}\n"
        if ! command -v wg > /dev/null 2>&1; then
             printf "${YELLOW}WireGuard ist nicht installiert. Installiere...${NC}\n"
             sudo apt-get update
             sudo apt-get install -y wireguard
             printf "${GREEN}WireGuard installiert.${NC}\n"
        else
             printf "${GREEN}WireGuard ist bereits installiert.${NC}\n"
        fi

        if [ ! -f "/etc/wireguard/wg0.conf" ]; then
            printf "${YELLOW}Konfiguration /etc/wireguard/wg0.conf nicht gefunden.${NC}\n"
            printf "Du musst die Konfiguration manuell erstellen oder Schlussel generieren.\n"
            printf "Beispiel:\n"
            printf "  wg genkey | tee privatekey | wg pubkey > publickey\n"
            printf "  sudo nano /etc/wireguard/wg0.conf\n"
        else
            printf "${GREEN}Konfiguration gefunden.${NC}\n"
            printf "WireGuard Service (re)starten? (j/n): "
            read reply
            case "$reply" in
                [Jj]*)
                    sudo systemctl enable wg-quick@wg0
                    sudo systemctl restart wg-quick@wg0
                    printf "${GREEN}WireGuard Service neu gestartet.${NC}\n"
                    ;;
            esac
        fi

        if command -v wg > /dev/null 2>&1; then
             printf "\n${CYAN}WireGuard Status:${NC}\n"
             sudo wg show
             printf "\n${CYAN}IP-Adressen:${NC}\n"
             ip -4 a show wg0 | grep inet || true
        fi
        ;;

    0)
        printf "${YELLOW}Abgebrochen.${NC}\n"
        exit 0
        ;;

    *)
        printf "${RED}Ungueltige Auswahl!${NC}\n"
        exit 1
        ;;
esac

printf "\n${CYAN}=========================================${NC}\n"
printf "${GREEN}=== Aktueller Status ===${NC}\n"
printf "${CYAN}=========================================${NC}\n"
printf "  Branch:        ${YELLOW}%s${NC}\n" "$(git rev-parse --abbrev-ref HEAD)"
printf "  Letzter Commit: %s\n" "$(git log -1 --oneline)"

if systemctl is-active --quiet "$SERVICE_NAME"; then
    printf "  Service:       ${GREEN}✓ AKTIV${NC}\n"
else
    printf "  Service:       ${RED}✗ INAKTIV${NC}\n"
fi

printf "${CYAN}=========================================${NC}\n"
printf "\n${GREEN}✓ Fertig!${NC}\n\n"
