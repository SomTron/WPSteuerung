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

# Bewahrt lokale getrackte Aenderungen als Patch + Statusliste auf. Es wird
# nichts gestasht, ueberschrieben oder verworfen.
backup_local_changes() {
    repo="$1"
    stamp=$(date '+%Y%m%d-%H%M%S')-$$
    git_dir=$(cd "$repo" && git rev-parse --git-dir 2>/dev/null) || return 1
    case "$git_dir" in
        /*) ;;
        *) git_dir="$repo/$git_dir" ;;
    esac
    backup_dir="$git_dir/wp-manager-backups/$stamp"
    mkdir -p "$backup_dir" || return 1
    git -C "$repo" status --porcelain=v1 > "$backup_dir/status.txt" || return 1
    git -C "$repo" diff --binary HEAD -- > "$backup_dir/tracked-changes.patch" || return 1
    git -C "$repo" rev-parse HEAD > "$backup_dir/base-commit.txt" || return 1
    printf '%s\n' "$backup_dir"
}

verify_service_restart() {
    service_name="$1"
    old_pid=$(systemctl show "$service_name" -p MainPID --value 2>/dev/null || true)
    old_restarts=$(systemctl show "$service_name" -p NRestarts --value 2>/dev/null || true)
    case "$old_pid" in ''|*[!0-9]*) old_pid=0 ;; esac
    case "$old_restarts" in ''|*[!0-9]*) old_restarts=0 ;; esac
    if ! sudo systemctl restart "$service_name"; then
        color_print "$RED" "FEHLER: systemctl restart ist fehlgeschlagen."
        return 1
    fi
    sleep 2
    new_pid=$(systemctl show "$service_name" -p MainPID --value 2>/dev/null || true)
    new_restarts=$(systemctl show "$service_name" -p NRestarts --value 2>/dev/null || true)
    case "$new_pid" in ''|*[!0-9]*) new_pid=0 ;; esac
    case "$new_restarts" in ''|*[!0-9]*) new_restarts=0 ;; esac
    if ! systemctl is-active --quiet "$service_name" || [ "$new_pid" -le 0 ]; then
        color_print "$RED" "FEHLER: Service ist nicht stabil aktiv (MainPID=$new_pid)."
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    fi
    if [ "$old_pid" -gt 0 ] && [ "$new_pid" -eq "$old_pid" ]; then
        color_print "$RED" "FEHLER: Neustart hat die MainPID nicht gewechselt ($new_pid)."
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    fi
    if [ "$new_restarts" -gt "$old_restarts" ]; then
        color_print "$RED" "FEHLER: Service ist direkt in einen Crash gelaufen (NRestarts $old_restarts -> $new_restarts)."
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    fi
    color_print "$GREEN" "Service-Neustart verifiziert: MainPID=$new_pid, NRestarts=$new_restarts."
}

# Holt Remote-Infos fuer die ANZEIGE. Bewusst nicht-interaktiv
# (BatchMode=yes): ssh fragt dann KEINE Passphrase ab, sondern meldet sofort
# den Fehler. Sonst wartet der Aufruf im 15-s-Timeout auf eine Passphrase,
# die der Nutzer an dieser Stelle nicht erwartet - der Fetch laeuft dann in
# den Timeout und es erscheint die FALSCHE Meldung "offline", obwohl das
# Netz in Ordnung war und nur der SSH-Key nicht freigeschaltet ist.
#
# FETCH_STATUS: 0 = ok, 1 = Netzwerk/Host, 2 = SSH-Authentifizierung,
#               3 = sonstiges
FETCH_STATUS=0
FETCH_OUTPUT=""
fetch_remote_info() {
    FETCH_STATUS=0
    FETCH_OUTPUT=""
    _fetch_rc=0
    # Vorhandene GIT_SSH_COMMAND erhalten und BatchMode nur ergaenzen.
    _ssh_cmd="${GIT_SSH_COMMAND:+$GIT_SSH_COMMAND }-o BatchMode=yes"
    if command -v timeout >/dev/null 2>&1; then
        FETCH_OUTPUT=$(GIT_SSH_COMMAND="$_ssh_cmd" timeout 15 git fetch --all 2>&1) || _fetch_rc=1
    else
        FETCH_OUTPUT=$(GIT_SSH_COMMAND="$_ssh_cmd" git fetch --all 2>&1) || _fetch_rc=1
    fi
    if [ "$_fetch_rc" = "0" ]; then
        return 0
    fi
    case "$FETCH_OUTPUT" in
        *"Permission denied"*|*"publickey"*|*"Host key verification failed"*|\
        *"Too many authentication failures"*|*"Bad configuration option"*|\
        *"Could not open a connection to your authentication agent"*|\
        *"Load key "*|*"sign_and_send_pubkey"*|*"no matching host key"*|\
        *"agent refused"*)
            FETCH_STATUS=2 ;;
        *"Could not resolve hostname"*|*"Couldn't resolve hostname"*|\
        *"Temporary failure in name resolution"*|*"Connection refused"*|\
        *"Connection timed out"*|*"Operation timed out"*|\
        *"No route to host"*|*"Network is unreachable"*|\
        *"Connection reset"*|*"Could not read from remote repository"*)
            FETCH_STATUS=1 ;;
        *)
            FETCH_STATUS=3 ;;
    esac
    return 0
}

# Erklaert das Ergebnis von fetch_remote_info knapp und faktisch korrekt.
print_fetch_hint() {
    case "$1" in
        0) : ;;
        1)
            color_print "$YELLOW" "Hinweis: GitHub nicht erreichbar (Netzwerk/Host)."
            color_print "$YELLOW" "        Angaben basieren auf dem letzten erfolgreichen Fetch."
            ;;
        2)
            color_print "$YELLOW" "Hinweis: SSH-Key nicht freigeschaltet - KEIN Netzproblem."
            color_print "$YELLOW" "        Der Key hat eine Passphrase, die hier nicht abgefragt werden kann."
            color_print "$YELLOW" "        Rueckstand daher ungeprueft. Option 1 fragt die Passphrase interaktiv ab."
            color_print "$YELLOW" "        Dauerhaft abhilfreich:  ssh-add ~/.ssh/id_ed25519  (vor dem Menue starten)"
            ;;
        *)
            color_print "$YELLOW" "Hinweis: Remote-Abfrage fehlgeschlagen."
            color_print "$YELLOW" "        Angaben basieren auf dem letzten erfolgreichen Fetch."
            color_print "$YELLOW" "        Ursache: $2"
            ;;
    esac
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
# Remote-Refs aktualisieren, damit "wie viele Commits hinten"-Anzeigen
# (hier und im wp-manager.sh Header) aktuell sind. Der Fehlerfall wird
# klassifiziert, statt alles als "offline" zu melden.
fetch_remote_info
FETCH_FAILED=$FETCH_STATUS

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
color_print "$CYAN" "Git Status (ohne untracked files):"
git status -uno --short

# Blockiere jeden Git-Schritt bei lokalen getrackten Aenderungen. Der Snapshot
# ersetzt bewusst keine benutzerkontrollierte Commit-/Stash-Aufloesung.
if [ -n "$(git status -uno --porcelain)" ]; then
    color_print "$RED" "ABBRUCH: Lokale Aenderungen an getrackten Dateien!"
    git status -uno --short | head -n 10
    BACKUP_DIR=$(backup_local_changes "$REPO_DIR") || {
        color_print "$RED" "FEHLER: Lokale Aenderungen konnten nicht gesichert werden."
        exit 1
    }
    color_print "$YELLOW" "Kein Pull/Checkout ausgefuehrt. Snapshot: $BACKUP_DIR"
    color_print "$YELLOW" "Bitte Aenderungen committen/stashen oder manuell zusammenfuehren."
    exit 1
fi

# Zeige Abstand zum Remote-Branch (Entscheidungshilfe fuer Option 1)
UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || true)
[ -z "$UPSTREAM" ] && UPSTREAM="origin/$CURRENT_BRANCH"
BEHIND=$(git rev-list --count "HEAD..$UPSTREAM" 2>/dev/null || echo "?")

printf "\n"
if [ "$FETCH_FAILED" != "0" ]; then
    print_fetch_hint "$FETCH_FAILED" "$FETCH_OUTPUT"
fi
if [ "$CURRENT_BRANCH" = "HEAD" ]; then
    : # detached HEAD: keine sinnvolle Vergleichsmoeglichkeit
elif [ "$BEHIND" = "?" ]; then
    color_print "$YELLOW" "Kein Upstream-Branch konfiguriert (kein Vergleich moeglich)."
elif [ "$FETCH_FAILED" != "0" ]; then
    # "auf dem neuesten Stand" waere hier eine unbelegte Behauptung: der
    # Vergleich beruht auf dem letzten erfolgreichen Fetch, nicht auf dem
    # aktuellen Remote-Stand.
    color_print "$YELLOW" "Vergleich mit '$UPSTREAM' ungeprueft (laut letztem Fetch: $BEHIND Commit(s) Rueckstand)."
else
    if [ "$BEHIND" = "0" ]; then
        color_print "$GREEN" "Lokal ist auf dem neuesten Stand von '$UPSTREAM'."
    else
        color_print "$YELLOW" "Lokal haengt $BEHIND Commit(s) hinter '$UPSTREAM' zurueck – Option 1 holt sie."
    fi
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
        if [ "$CURRENT_BRANCH" = "HEAD" ]; then
            color_print "$RED" "FEHLER: Update im detached HEAD ist nicht erlaubt. Bitte zuerst einen Branch waehlen."
            exit 1
        fi
        printf "\n${CYAN}Hole Informationen von GitHub...${NC}\n"
        # OHNE BatchMode: hier darf (und soll) ssh nach der Passphrase fragen.
        # stderr wird deshalb NICHT unterdrueckt - der Passphrase-Hinweis geht
        # ueber /dev/tty und muss fuer den Nutzer sichtbar bleiben.
        if ! git fetch --all > /dev/null; then
            color_print "$RED" "FEHLER: 'git fetch' fehlgeschlagen - GitHub nicht erreichbar?"
            color_print "$YELLOW" "Bitte Netz/WireGuard pruefen und erneut versuchen."
            print_fetch_hint 1 ""
            exit 1
        fi

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
            printf "\n${CYAN}Aktualisiere Branch '%s' (nur Fast-Forward)...${NC}\n" "$CURRENT_BRANCH"
            if git pull --ff-only origin "$CURRENT_BRANCH"; then
                printf "${GREEN}Code aktualisiert!${NC}\n"
            else
                # Unter `set -e` wuerde das Skript hier still beenden. Stattdessen
                # klar benennen - und der Service bleibt unveraendert laufen.
                color_print "$RED" "FEHLER: 'git pull' fehlgeschlagen - Code nicht aktualisiert."
                color_print "$YELLOW" "Der Service wurde NICHT neu gestartet."
                exit 1
            fi
            if systemctl is-active --quiet "$SERVICE_NAME"; then
                printf "${CYAN}Starte Service neu und verifiziere...${NC}\n"
                verify_service_restart "$SERVICE_NAME"
            else
                printf "${YELLOW}Service ist nicht aktiv, ueberspringe Neustart.${NC}\n"
            fi
            printf "${CYAN}Starte Skript neu um Aenderungen zu laden...${NC}\n"
            sleep 1
            exec sh "$SCRIPT_PATH" "$@"
        else
            color_print "$YELLOW" "Update abgebrochen."
            exec sh "$SCRIPT_PATH" "$@"
        fi
        ;;

        2)
        printf "\n${CYAN}Hole neueste Branch-Informationen...${NC}\n"
        # Passphrase darf hier interaktiv abgefragt werden (kein BatchMode).
        if ! git fetch --all > /dev/null; then
            color_print "$RED" "FEHLER: 'git fetch' fehlgeschlagen - GitHub nicht erreichbar?"
            print_fetch_hint 1 ""
            exit 1
        fi
        printf "\n${CYAN}Verfuegbare Branches:${NC}\n"
        git branch -a | grep -v HEAD
        printf "Zu welchem Branch wechseln? (z.B. master/refactoring-wip): "
        read raw_branch

        # Bereinige Branch-Namen und validiere sie als echte Git-Referenz.
        target_branch=$(printf '%s\n' "$raw_branch" | sed -e 's|^remotes/origin/||' -e 's|^origin/||')
        case "$target_branch" in
            -*|'')
                color_print "$RED" "FEHLER: Ungueltiger Branch-Name: $target_branch"
                exit 1
                ;;
        esac
        if ! git check-ref-format "refs/heads/$target_branch" >/dev/null 2>&1; then
            color_print "$RED" "FEHLER: Ungueltiger Branch-Name: $target_branch"
            exit 1
        fi

        printf "${CYAN}Wechsle zu Branch '%s'...${NC}\n" "$target_branch"

        if git show-ref --verify --quiet "refs/heads/$target_branch"; then
            git checkout "$target_branch"
        elif git show-ref --verify --quiet "refs/remotes/origin/$target_branch"; then
            printf "${YELLOW}Branch '%s' lokal nicht gefunden. Erzeuge Tracking-Branch...${NC}\n" "$target_branch"
            git checkout -b "$target_branch" "origin/$target_branch"
        else
            color_print "$RED" "FEHLER: Branch existiert weder lokal noch auf origin: $target_branch"
            exit 1
        fi

        printf "${GREEN}Zu Branch '%s' gewechselt!${NC}\n" "$target_branch"
        printf "Service neu starten? (j/n): "
        read reply
        case "$reply" in
            [Jj]*)
                verify_service_restart "$SERVICE_NAME"
                ;;
        esac
        ;;

        3)
        printf "\n${CYAN}Hole neueste Branch-Informationen...${NC}\n"
        # Passphrase darf hier interaktiv abgefragt werden (kein BatchMode).
        if ! git fetch --all > /dev/null; then
            color_print "$RED" "FEHLER: 'git fetch' fehlgeschlagen - GitHub nicht erreichbar?"
            print_fetch_hint 1 ""
            exit 1
        fi
        printf "\n${CYAN}Verfuegbare Branches:${NC}\n"
        git branch -a | grep -v HEAD
        printf "Zu welchem Branch wechseln? (z.B. master/refactoring-wip): "
        read raw_branch

        # Bereinige Branch-Namen und validiere sie als echte Git-Referenz.
        target_branch=$(printf '%s\n' "$raw_branch" | sed -e 's|^remotes/origin/||' -e 's|^origin/||')
        case "$target_branch" in
            -*|'')
                color_print "$RED" "FEHLER: Ungueltiger Branch-Name: $target_branch"
                exit 1
                ;;
        esac
        if ! git check-ref-format "refs/heads/$target_branch" >/dev/null 2>&1; then
            color_print "$RED" "FEHLER: Ungueltiger Branch-Name: $target_branch"
            exit 1
        fi
        if ! git show-ref --verify --quiet "refs/remotes/origin/$target_branch"; then
            color_print "$RED" "FEHLER: Ziel-Branch existiert auf origin nicht: $target_branch"
            exit 1
        fi

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

            # Pruefe ob Branch lokal existiert, sonst erstelle Tracking-Branch.
            if git show-ref --verify --quiet "refs/heads/$target_branch"; then
                git checkout "$target_branch"
            else
                git checkout -b "$target_branch" "origin/$target_branch"
            fi

            if git pull --ff-only origin "$target_branch"; then
                printf "${GREEN}Branch gewechselt und aktualisiert!${NC}\n"
            else
                # Klar benennen statt unter `set -e` still zu beenden.
                color_print "$RED" "FEHLER: 'git pull' fehlgeschlagen - Branch nicht aktualisiert."
                color_print "$YELLOW" "Der Service wurde NICHT neu gestartet."
                exit 1
            fi
            if systemctl is-active --quiet "$SERVICE_NAME"; then
                printf "${CYAN}Starte Service neu und verifiziere...${NC}\n"
                verify_service_restart "$SERVICE_NAME"
            else
                printf "${YELLOW}Service ist nicht aktiv, ueberspringe Neustart.${NC}\n"
            fi
            printf "${CYAN}Starte Skript neu um Aenderungen zu laden...${NC}\n"
            sleep 1
            exec sh "$SCRIPT_PATH" "$@"
        else
            color_print "$YELLOW" "Abgebrochen."
            exec sh "$SCRIPT_PATH" "$@"
        fi
        ;;

    4)
        printf "${CYAN}Starte Service neu und verifiziere...${NC}\n"
        verify_service_restart "$SERVICE_NAME"
        ;;

        5)
        printf "\n${CYAN}=========================================${NC}\n"
        printf "${GREEN}=== Aktueller Status ===${NC}\n"
        printf "${CYAN}=========================================${NC}\n"
        printf "  Branch:        ${YELLOW}%s${NC}\n" "$(git rev-parse --abbrev-ref HEAD)"
        printf "  Letzter Commit: %s\n" "$(git log -1 --oneline)"

        # Remote-Infos frisch holen (Fehlerursache wird klassifiziert)
        fetch_remote_info
        STATUS_UPSTREAM=$(git rev-parse --abbrev-ref --symbolic-full-name "@{u}" 2>/dev/null || true)
        [ -z "$STATUS_UPSTREAM" ] && STATUS_UPSTREAM="origin/$(git rev-parse --abbrev-ref HEAD)"
        STATUS_BEHIND=$(git rev-list --count "HEAD..$STATUS_UPSTREAM" 2>/dev/null || echo "?")
        STATUS_DIRTY=$(git status --porcelain | wc -l | tr -d ' ')

        printf "  Upstream:      %s\n" "$STATUS_UPSTREAM"
        if [ "$FETCH_STATUS" = "0" ]; then
            printf "  Rueckstand:    %s Commit(s) hinter '%s'\n" "$STATUS_BEHIND" "$STATUS_UPSTREAM"
        else
            printf "  Rueckstand:    ${YELLOW}ungeprueft (Remote-Abfrage fehlgeschlagen)${NC}\n"
        fi
        printf "  Lokale Aend.:  %s Datei(en)\n" "$STATUS_DIRTY"
        if [ "$FETCH_STATUS" != "0" ]; then
            print_fetch_hint "$FETCH_STATUS" "$FETCH_OUTPUT"
        fi

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
