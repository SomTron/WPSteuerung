#!/bin/sh
# Raspberry Pi WPSteuerung Deployment Script (POSIX-sh kompatibel)
# Verwendung: ./rpi-deploy.sh  (oder: bash rpi-deploy.sh)

set -e

# Farben fuer Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Konfiguration
REPO_DIR="/home/patrik/WPSteuerung"
SERVICE_NAME="wpsteuerung"

# Absoluter Pfad zu diesem Skript (fuer Neustart nach cd)
SCRIPT_PATH=$(readlink -f "$0" 2>/dev/null || realpath "$0" 2>/dev/null || echo "$0")

# Hilfsfunktion fuer farbigen Output (POSIX-konform)
# Verwendung: color_print $COLOR "Nachricht"
color_print() {
    printf "%b%s%b\n" "$1" "$2" "$NC"
}

# --- Hilfsfunktionen für robusteres Deployment ---

save_fallback() {
    local max_fallbacks=5
    local fallback_file="$REPO_DIR/.git/fallback_commits"
    local current_commit=$(git rev-parse HEAD)
    local current_date=$(date '+%Y-%m-%d %H:%M:%S')
    local commit_msg=$(git log -1 --format=%s)
    
    # Speichere die aktuellen Abhängigkeiten vor dem Update zum Vergleichen
    if [ -f "$REPO_DIR/requirements.txt" ]; then
        cp "$REPO_DIR/requirements.txt" "$REPO_DIR/.git/requirements.txt.bak" 2>/dev/null || true
    fi
    
    # Commit in Historiendatei eintragen (nur wenn er nicht schon der neueste Eintrag ist)
    if [ ! -f "$fallback_file" ] || ! head -n 1 "$fallback_file" | grep -q "$current_commit"; then
        echo "$current_commit | $current_date | $commit_msg" | cat - "$fallback_file" 2>/dev/null | head -n $max_fallbacks > "$fallback_file.tmp"
        mv "$fallback_file.tmp" "$fallback_file"
    fi
}

check_dependencies() {
    printf "\n${CYAN}Prüfe auf neue Python-Abhängigkeiten...${NC}\n"
    if [ -f "$REPO_DIR/requirements.txt" ] && [ -f "$REPO_DIR/.git/requirements.txt.bak" ]; then
        if ! cmp -s "$REPO_DIR/requirements.txt" "$REPO_DIR/.git/requirements.txt.bak"; then
            printf "${YELLOW}requirements.txt hat sich geändert! Installiere neue Abhängigkeiten...${NC}\n"
            # Versuche global oder im venv zu installieren (hier globale fallback-annahme basierend auf typischen RPi setups)
            # Da wir nicht wissen, ob ein venv aktiv ist, versuchen wir pip3 install -r mit --break-system-packages (ab Python 3.11 auf RPi nötig für system-wide)
            # Eine sicherere Methode ist, den systemctl service zu checken, aber wir verwenden den Standard pip
            if command -v pip3 >/dev/null 2>&1; then
                # Nutze sudo um Berechtigungsprobleme beim globalen Install auf RPi zu vermeiden, falls nötig.
                sudo pip3 install -r "$REPO_DIR/requirements.txt" || sudo pip3 install --break-system-packages -r "$REPO_DIR/requirements.txt" || true
                printf "${GREEN}Abhängigkeiten aktualisiert.${NC}\n"
            else
                printf "${RED}pip3 nicht gefunden! Bitte manuell prüfen.${NC}\n"
            fi
        else
            printf "${GREEN}Keine Änderungen an den Abhängigkeiten.${NC}\n"
        fi
    fi
}

check_health_and_rollback() {
    local target_commit=$1
    printf "\n${CYAN}Warte 5 Sekunden auf Service-Start...${NC}\n"
    sleep 5
    
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        printf "\n${RED}================================================${NC}\n"
        printf "${RED}WARNUNG: Service ist nach dem Neustart fehlgeschlagen!${NC}\n"
        printf "${RED}================================================${NC}\n"
        printf "Fehler-Log (letzte 10 Zeilen):\n"
        sudo journalctl -u "$SERVICE_NAME" -n 10 --no-pager | grep -i error || true
        
        if [ -n "$target_commit" ]; then
            printf "\nSoll das System automatisch auf den vorherigen Zustand (Commit %s) zurückgesetzt werden? (j/n): " "$target_commit"
            read rb_reply
            if [ "$rb_reply" = "j" ] || [ "$rb_reply" = "J" ]; then
                printf "${CYAN}Setze zurück auf %s...${NC}\n" "$target_commit"
                git reset --hard "$target_commit"
                sudo systemctl restart "$SERVICE_NAME"
                printf "${GREEN}Zurückgesetzt und neu gestartet!${NC}\n"
            fi
        fi
    else
        printf "${GREEN}Healthcheck bestanden: Service ist aktiv.${NC}\n"
    fi
}

color_print "$CYAN" "========================================="
color_print "$CYAN" "  WPSteuerung Deployment auf Raspberry Pi"
color_print "$CYAN" "========================================="

# Pruefe ob Repository existiert
if [ ! -d "$REPO_DIR" ]; then
    color_print "$RED" "Fehler: Repository nicht gefunden in $REPO_DIR"
    color_print "$YELLOW" "Fuehre erst die Ersteinrichtung durch!"
    exit 1
fi

cd "$REPO_DIR"
# Verhindere "fatal: Need to specify how to reconcile divergent branches"
git config pull.rebase false

# Zeige aktuellen Branch
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" = "HEAD" ]; then
    printf "\n"
    color_print "$RED" "WARNUNG: Du befindest dich im 'detached HEAD' Zustand!"
    color_print "$YELLOW" "Deine Commits koennten verloren gehen. Empfohlen: Zu einem Branch wechseln."
else
    printf "\n"
    color_print "$YELLOW" "Aktueller Branch: $CURRENT_BRANCH"
fi

# Zeige Git Status (ohne untracked files)
printf "\n"
color_print "$CYAN" "Git Status (ohne untracked files, ignoriere sonnen_prognose.csv):"
git status -uno --short | grep -v "sonnen_prognose.csv" || true

# Warne nur bei getrackten Aenderungen
MODIFIED_FILES=$(git status -uno --porcelain | grep -v "sonnen_prognose.csv" || true)
if [ -n "$MODIFIED_FILES" ]; then
    color_print "$RED" "WARNUNG: Es gibt lokale Aenderungen an getrackten Dateien!"
    printf "Moechtest du diese verwerfen? (j/n): "
    read reply
    case "$reply" in
        [Jj]*)
            git reset --hard
            color_print "$GREEN" "Lokale Aenderungen verworfen."
            color_print "$CYAN" "Starte Skript neu..."
            exec sh "$SCRIPT_PATH" "$@"
            ;;
        *)
            color_print "$YELLOW" "Abgebrochen."
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
printf "7. Auf ältere Version zurücksetzen (Rollback)\n"
printf "0. Abbrechen\n"

printf "Waehle (0-7): "
read choice

case "$choice" in
    1)
        printf "\n${CYAN}Hole Informationen von GitHub...${NC}\n"
        git fetch --all > /dev/null 2>&1

        # Informationen über aktuellen Stand
        CUR_COMMIT=$(git rev-parse --short HEAD)
        CUR_DATE=$(git log -1 --format=%cd --date=format:'%d.%m.%Y %H:%M')
        CUR_MSG=$(git log -1 --format=%s)

        # Informationen über Remote Stand
        REM_COMMIT=$(git rev-parse --short "origin/$CURRENT_BRANCH")
        REM_DATE=$(git log -1 --format=%cd --date=format:'%d.%m.%Y %H:%M' "origin/$CURRENT_BRANCH")
        REM_MSG=$(git log -1 --format=%s "origin/$CURRENT_BRANCH")

        color_print "$YELLOW" "Aktueller Code (Lokal):"
        printf "  Commit: %s\n" "$CUR_COMMIT"
        printf "  Datum:  %s\n" "$CUR_DATE"
        printf "  Info:   %s\n" "$CUR_MSG"

        printf "\n"
        color_print "$CYAN" "Neuer Code (GitHub):"
        printf "  Commit: %s\n" "$REM_COMMIT"
        printf "  Datum:  %s\n" "$REM_DATE"
        printf "  Info:   %s\n" "$REM_MSG"

        printf "\nUpdate durchfuehren? (j/n): "
        read confirm
        if [ "$confirm" = "j" ] || [ "$confirm" = "J" ]; then
            save_fallback
            printf "\n${CYAN}Aktualisiere Branch '%s'...${NC}\n" "$CURRENT_BRANCH"
            git pull origin "$CURRENT_BRANCH"
            printf "${GREEN}Code aktualisiert!${NC}\n"
            check_dependencies
            sudo systemctl restart "$SERVICE_NAME"
            check_health_and_rollback "$CUR_COMMIT"
            printf "${CYAN}Starte Skript neu um Aenderungen zu laden...${NC}\n"
            sleep 1
            exec sh "$SCRIPT_PATH" "$@"
        else
            color_print "$YELLOW" "Update abgebrochen."
            exec sh "$SCRIPT_PATH" "$@"
        fi
        ;;

    2)
        printf "\n${CYAN}Verfuegbare Branches:${NC}\n"
        git branch -a | grep -v HEAD
        printf "Zu welchem Branch wechseln? (z.B. master/refactoring-wip): "
        read raw_branch

        # Bereinige Branch-Namen (entferne remotes/origin/ oder origin/)
        target_branch=$(echo "$raw_branch" | sed -e 's|^remotes/origin/||' -e 's|^origin/||')

        printf "${CYAN}Wechsle zu Branch '%s'...${NC}\n" "$target_branch"
        git fetch --all

        # Pruefe ob Branch lokal existiert, sonst tracke remote
        if git show-ref --verify --quiet "refs/heads/$target_branch"; then
            git checkout "$target_branch"
        else
            printf "${YELLOW}Branch '%s' lokal nicht gefunden. Versuche Tracking von origin/%s...${NC}\n" "$target_branch" "$target_branch"
            git checkout -b "$target_branch" "origin/$target_branch" || git checkout "$target_branch"
        fi

        printf "${GREEN}Zu Branch '%s' gewechselt!${NC}\n" "$target_branch"
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
        printf "Zu welchem Branch wechseln? (z.B. master/refactoring-wip): "
        read raw_branch

        # Bereinige Branch-Namen
        target_branch=$(echo "$raw_branch" | sed -e 's|^remotes/origin/||' -e 's|^origin/||')

        printf "\n${CYAN}Hole Informationen von GitHub...${NC}\n"
        git fetch --all > /dev/null 2>&1

        # Informationen über aktuellen Stand
        CUR_COMMIT=$(git rev-parse --short HEAD)
        CUR_DATE=$(git log -1 --format=%cd --date=format:'%d.%m.%Y %H:%M')
        CUR_MSG=$(git log -1 --format=%s)

        # Informationen über Ziel-Branch Stand
        REM_COMMIT=$(git rev-parse --short "origin/$target_branch")
        REM_DATE=$(git log -1 --format=%cd --date=format:'%d.%m.%Y %H:%M' "origin/$target_branch")
        REM_MSG=$(git log -1 --format=%s "origin/$target_branch")

        color_print "$YELLOW" "Aktueller Code (Lokal):"
        printf "  Branch: %s\n" "$CURRENT_BRANCH"
        printf "  Commit: %s\n" "$CUR_COMMIT"
        printf "  Datum:  %s\n" "$CUR_DATE"
        printf "  Info:   %s\n" "$CUR_MSG"

        printf "\n"
        color_print "$CYAN" "Ziel-Branch (GitHub):"
        printf "  Branch: %s\n" "$target_branch"
        printf "  Commit: %s\n" "$REM_COMMIT"
        printf "  Datum:  %s\n" "$REM_DATE"
        printf "  Info:   %s\n" "$REM_MSG"

        printf "\nWechsel und Update durchfuehren? (j/n): "
        read confirm
        if [ "$confirm" = "j" ] || [ "$confirm" = "J" ]; then
            printf "${CYAN}Wechsle zu Branch '%s'...${NC}\n" "$target_branch"
            
            # Pruefe ob Branch lokal existiert, sonst tracke remote
            if git show-ref --verify --quiet "refs/heads/$target_branch"; then
                git checkout "$target_branch"
            else
                printf "${YELLOW}Branch '%s' lokal nicht gefunden. Versuche Tracking von origin/%s...${NC}\n" "$target_branch" "$target_branch"
                git checkout -b "$target_branch" "origin/$target_branch" || git checkout "$target_branch"
            fi

            save_fallback
            git pull origin "$target_branch"
            printf "${GREEN}Branch gewechselt und aktualisiert!${NC}\n"
            check_dependencies
            sudo systemctl restart "$SERVICE_NAME"
            check_health_and_rollback "$CUR_COMMIT"
            printf "${CYAN}Starte Skript neu um Aenderungen zu laden...${NC}\n"
            sleep 1
            exec sh "$SCRIPT_PATH" "$@"
        else
            color_print "$YELLOW" "Abgebrochen."
            exec sh "$SCRIPT_PATH" "$@"
        fi
        ;;

    4)
        printf "${CYAN}Starte Service neu...${NC}\n"
        sudo systemctl restart "$SERVICE_NAME"
        printf "${GREEN}Service neu gestartet!${NC}\n"
        ;;

    5)
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
        printf "\n"
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
            printf "Du musst die Konfiguration manuell erstellen oder Schl\344ssel generieren.\n"
            printf "Beispiel:\n  wg genkey | tee privatekey | wg pubkey > publickey\n  sudo nano /etc/wireguard/wg0.conf\n"
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

    7)
        printf "\n${CYAN}=== Rollback auf ältere Version ===${NC}\n"
        printf "Letzte Commits dieses Branches:\n"
        git log -10 --oneline --decorate
        printf "\nLetzte durch Updates gespeicherte funktionierende Commits:\n"
        if [ -f "$REPO_DIR/.git/fallback_commits" ]; then
            cat "$REPO_DIR/.git/fallback_commits"
        else
            printf "Keine Update-Historie gefunden.\n"
        fi
        
        printf "\nWelchen Commit (Hash) möchtest du wiederherstellen? (Leer = Abbrechen): "
        read fallback_commit
        
        if [ -n "$fallback_commit" ]; then
            printf "${YELLOW}Achtung: Dies versetzt das Repository in einen 'Detached HEAD' Zustand.${NC}\n"
            printf "Du kannst später mit Auswahl 2 (Branch wechseln) wieder auf den Branch wechseln.\n"
            printf "Lokale nicht-committete Änderungen werden verworfen.\n"
            printf "Fortfahren? (j/n): "
            read rb_confirm
            if [ "$rb_confirm" = "j" ] || [ "$rb_confirm" = "J" ]; then
                printf "${CYAN}Führe checkout auf %s aus...${NC}\n" "$fallback_commit"
                git reset --hard HEAD > /dev/null 2>&1 || true
                git checkout "$fallback_commit"
                printf "${GREEN}Code auf Version %s zurückgesetzt!${NC}\n" "$fallback_commit"
                sudo systemctl restart "$SERVICE_NAME"
                printf "${GREEN}Service neu gestartet!${NC}\n"
            else
                printf "${YELLOW}Abgebrochen.${NC}\n"
            fi
        else
            printf "${YELLOW}Abgebrochen.${NC}\n"
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

printf "\n${GREEN}=== Aktueller Status ===${NC}\n"
printf "Branch: %s\n" "$(git rev-parse --abbrev-ref HEAD)"
printf "Letzter Commit: %s\n" "$(git log -1 --oneline)"

if systemctl is-active --quiet "$SERVICE_NAME"; then
    printf "Service: ${GREEN}AKTIV${NC}\n"
else
    printf "Service: ${RED}INAKTIV${NC}\n"
fi

printf "\n${CYAN}Fertig!${NC}\n"
