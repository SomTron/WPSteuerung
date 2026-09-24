#!/bin/sh
# wp-manager.sh - Management script for WPSteuerung
# Located in Updater repo, targets ../Steuerung (relative to script location)
#
# v1.12: Sicheres Fast-Forward ohne Datenverlust, verifizierte Serviceaktionen,
#       Produktionsstatus mit Kompressor/Zyklen/Datenalter, 24-h-Fehler-/OOM-
#       Diagnose, Analyse-Untermenue und zentraler Datenschutz-Upload.
# v1.10: Neue Option 19 (Lauf-Status/letzte Abstuerze aus letzter_lauf.json) und
#       Status-Zeile "Letzter Lauf" im Kopf - macht OOM-Kills/Crashes sichtbar,
#       die vorher nur im Kernel-Journal standen. Banner-Version nachgezogen.
# v1.9: Neue Optionen 17 (zyklen.csv anzeigen) und 18 (Upload der
#       Analyse-Zylen-CSV) - Kompressor-Zyklen direkt vom PI abrufen/teilen.
# v1.8: Neue Optionen 15 (Entscheidungs-Log anzeigen) und 16 (Upload
#       entscheidungs_log.jsonl) - Detailansicht der Regelentscheidungen.
# v1.7: Option 10 behandelte blockierende Runtime-Dateien; v1.12 ersetzte das
#       Verwerfen durch Snapshot + kontrollierten Abbruch.
# v1.6: Farb-Fix (%b statt %s bei Service/VPN-Status - zeigte vorher rohe \033-Codes),
#       lokale-Aenderungen-Zaehler nur noch getrackte Dateien (-uno).
# v1.5: CYAN-Farbe ergänzt (war vorher nicht definiert), informativer Status-Header
#       (Uptime, RAM, Git-Diff, Temperatur, Disk, letzter Fehler), Eingabevalidierung,
#       Fehlerbehandlung bei Upload / Self-Update / Service-Steuerung, neue Option 13.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TARGET_DIR="$REPO_ROOT/Steuerung"
# Log-/CSV-Pfade: produktiv unter /var/log/wps, CSV direkt im Steuerungs-CWD.
LOG_FILE="${WPS_LOG_FILE:-/var/log/wps/heizungssteuerung.log}"
ERROR_LOG_FILE="${WPS_ERROR_LOG_FILE:-/var/log/wps/error.log}"
HEATING_CSV="${WPS_HEATING_CSV:-$TARGET_DIR/csv log/heizungsdaten.csv}"
CYCLE_CSV="${WPS_CYCLE_CSV:-$TARGET_DIR/csv log/zyklen.csv}"
LEARNING_JSON="${WPS_LEARNING_JSON:-$TARGET_DIR/learning_data.json}"
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

status_value() {
    printf '%s\n' "$MANAGER_STATUS" | sed -n "s/^$1=//p" | head -n 1
}

# Legt lokale, versionierte Aenderungen als Patch + Statusliste ab. Es wird
# bewusst weder gestasht noch verworfen: Der Benutzer entscheidet selbst ueber
# Commit, stash oder manuelle Zusammenfuehrung.
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
    printf '%s\n' "$backup_dir"
}

# Fuehrt nur Fast-Forward-Updates aus. Lokale Aenderungen werden gesichert und
# blockieren den Updateversuch; so kann kein Update still Daten vernichten.
safe_manager_update() {
    repo="$1"
    if [ -n "$(git -C "$repo" status --porcelain -uno 2>/dev/null)" ]; then
        backup_dir=$(backup_local_changes "$repo") || {
            printf "${RED}✗ Lokale Änderungen konnten nicht gesichert werden.${NC}\n"
            return 1
        }
        printf "${YELLOW}Update blockiert: lokale getrackte Änderungen vorhanden.${NC}\n"
        git -C "$repo" status --short -uno | head -n 10 | sed 's/^/   /'
        printf "Snapshot: %s\n" "$backup_dir"
        printf "Bitte Änderungen committen/stashen oder manuell sichern.\n"
        return 1
    fi
    old_commit=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null) || return 1
    git -C "$repo" pull --ff-only || return 1
    new_commit=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null) || return 1
    if [ "$old_commit" != "$new_commit" ]; then
        printf "${GREEN}✓ Update installiert: %s → %s.${NC}\n" "$old_commit" "$new_commit"
        return 10
    fi
    printf "${GREEN}✓ Bereits aktuell (%s).${NC}\n" "$new_commit"
    return 0
}

# Fuehrt systemctl aus und prueft danach den tatsaechlichen Endzustand. Fuer
# Start/Restart werden MainPID und NRestarts geprueft, damit ein sofortiger
# Crash-Loop nicht als Erfolg gemeldet wird.
verify_service_action() {
    action="$1"
    service_name="$2"
    old_pid=$(systemctl show "$service_name" -p MainPID --value 2>/dev/null)
    old_restarts=$(systemctl show "$service_name" -p NRestarts --value 2>/dev/null)
    case "$old_pid" in ''|*[!0-9]*) old_pid=0 ;; esac
    case "$old_restarts" in ''|*[!0-9]*) old_restarts=0 ;; esac

    if ! sudo systemctl "$action" "$service_name"; then
        printf "${RED}✗ systemctl %s ist fehlgeschlagen.${NC}\n" "$action"
        return 1
    fi
    case "$action" in
        start|restart) expected=active ;;
        stop) expected=inactive ;;
        *) return 2 ;;
    esac
    reached=1
    tries=0
    while [ "$tries" -lt 10 ]; do
        if systemctl is-active --quiet "$service_name"; then actual=active; else actual=inactive; fi
        if [ "$actual" = "$expected" ]; then reached=0; break; fi
        tries=$((tries + 1))
        sleep 1
    done
    if [ "$reached" -ne 0 ]; then
        printf "${RED}✗ Endzustand nach %s ist '%s', erwartet '%s'.${NC}\n" "$action" "$actual" "$expected"
        systemctl status "$service_name" --no-pager -l 2>/dev/null || true
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    fi

    sleep 2
    new_pid=$(systemctl show "$service_name" -p MainPID --value 2>/dev/null)
    new_restarts=$(systemctl show "$service_name" -p NRestarts --value 2>/dev/null)
    case "$new_pid" in ''|*[!0-9]*) new_pid=0 ;; esac
    case "$new_restarts" in ''|*[!0-9]*) new_restarts=0 ;; esac
    if [ "$action" = "stop" ] && [ "$new_pid" -ne 0 ]; then
        printf "${RED}✗ Service ist gestoppt, aber MainPID ist noch %s.${NC}\n" "$new_pid"
        return 1
    elif [ "$action" = "restart" ] && [ "$old_pid" -gt 0 ] && [ "$new_pid" -eq "$old_pid" ]; then
        printf "${RED}✗ Neustart hat die MainPID nicht gewechselt (%s).${NC}\n" "$new_pid"
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    elif [ "$action" != "stop" ] && [ "$new_pid" -le 0 ]; then
        printf "${RED}✗ Service ist aktiv, aber MainPID ist ungueltig (%s).${NC}\n" "$new_pid"
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    elif [ "$new_restarts" -gt "$old_restarts" ]; then
        printf "${RED}✗ Service ist in einen Neustart-Crash gelaufen (%s → %s).${NC}\n" "$old_restarts" "$new_restarts"
        journalctl -u "$service_name" -n 30 --no-pager 2>/dev/null || true
        return 1
    fi
    printf "${GREEN}✓ %s verifiziert: PID %s, NRestarts %s.${NC}\n" "$action" "$new_pid" "$new_restarts"
    return 0
}

# Einheitlicher, bewusst bestaetigungspflichtiger Catbox-Upload. Originaldaten
# werden nur in ein privates temporaeres Verzeichnis kopiert und danach geloescht.
upload_file() {
    source_file="$1"
    label="$2"
    max_bytes="${WPS_UPLOAD_MAX_BYTES:-209715200}"
    case "$max_bytes" in
        ''|*[!0-9]*)
            printf "${RED}✗ WPS_UPLOAD_MAX_BYTES muss eine positive Ganzzahl sein.${NC}\n"
            return 1
            ;;
    esac
    if [ "$max_bytes" -le 0 ]; then
        printf "${RED}✗ WPS_UPLOAD_MAX_BYTES muss groesser als 0 sein.${NC}\n"
        return 1
    fi
    if [ ! -f "$source_file" ] || [ ! -r "$source_file" ]; then
        printf "${RED}✗ Datei nicht lesbar: %s${NC}\n" "$source_file"
        return 1
    fi
    file_size=$(wc -c < "$source_file" 2>/dev/null | tr -d ' ')
    case "$file_size" in
        ''|*[!0-9]*)
            printf "${RED}✗ Dateigröße konnte nicht ermittelt werden.${NC}\n"
            return 1
            ;;
    esac
    if [ "$file_size" -eq 0 ]; then
        printf "${RED}✗ Datei ist leer.${NC}\n"
        return 1
    fi
    if [ "$file_size" -gt "$max_bytes" ]; then
        printf "${RED}✗ Upload zu gross: %s Bytes (Limit %s).${NC}\n" "$file_size" "$max_bytes"
        return 1
    fi
    if ! command -v gzip >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
        printf "${RED}✗ Upload benoetigt curl und gzip.${NC}\n"
        return 1
    fi
    printf "${YELLOW}ACHTUNG: '%s' wird oeffentlich auf catbox.moe hochgeladen.${NC}\n" "$label"
    printf "Die Datei kann Betriebs-, Temperatur- und Entscheidungsdaten enthalten.\n"
    printf "URL ist ohne Login abrufbar. Upload jetzt ausfuehren? (j/N): "
    read upload_reply
    case "$upload_reply" in
        [Jj]*) ;;
        *) printf "Upload abgebrochen.\n"; return 0 ;;
    esac

    temp_dir=$(mktemp -d 2>/dev/null) || {
        printf "${RED}✗ Temporaeres Verzeichnis konnte nicht erstellt werden.${NC}\n"
        return 1
    }
    trap 'rm -rf "$temp_dir"' 0 1 2 3 15
    base_name=$(basename "$source_file")
    upload_name="${base_name}.gz"
    if ! gzip -c "$source_file" > "$temp_dir/$upload_name"; then
        rm -rf "$temp_dir"
        printf "${RED}✗ Komprimierung fehlgeschlagen.${NC}\n"
        return 1
    fi
    compressed_size=$(wc -c < "$temp_dir/$upload_name" 2>/dev/null | tr -d ' ')
    case "$compressed_size" in
        ''|*[!0-9]*)
            rm -rf "$temp_dir"
            printf "${RED}✗ Komprimierte Dateigröße konnte nicht ermittelt werden.${NC}\n"
            return 1
            ;;
    esac
    if [ "$compressed_size" -gt "$max_bytes" ]; then
        rm -rf "$temp_dir"
        printf "${RED}✗ Komprimierte Datei überschreitet das Limit (%s > %s Bytes).${NC}\n" \
            "$compressed_size" "$max_bytes"
        return 1
    fi
    printf "Lade %s (%s komprimiert) ...\n" "$label" "$(du -h "$temp_dir/$upload_name" | cut -f1)"
    upload_url=$(curl --connect-timeout 15 --max-time 300 -fsS \
        -F 'reqtype=fileupload' -F "fileToUpload=@$temp_dir/$upload_name" \
        https://catbox.moe/user/api.php)
    curl_rc=$?
    rm -rf "$temp_dir"
    if [ "$curl_rc" -ne 0 ]; then
        printf "${RED}✗ Upload fehlgeschlagen (curl RC=%s).${NC}\n" "$curl_rc"
        return 1
    fi
    if ! printf '%s' "$upload_url" | grep -Eq '^https://files\.catbox\.moe/[A-Za-z0-9_-]+(\.[A-Za-z0-9]+)?$'; then
        printf "${RED}✗ Unerwartete Catbox-Antwort.${NC}\n"
        return 1
    fi
    printf "${GREEN}✓ Upload erfolgreich.${NC}\nURL: ${BLUE}%s${NC}\n" "$upload_url"
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


# Bevorzugt die laufende, direkt bei jedem Kompressorlauf fortgeschriebene
# Historie. Fallback: neueste zyklen.csv aus einem manuellen Analyse-Lauf.
finde_zyklen_csv() {
    if [ -s "$CYCLE_CSV" ] && [ "$(wc -l < "$CYCLE_CSV" 2>/dev/null | tr -d ' ')" -gt 1 ]; then
        printf '%s\n' "$CYCLE_CSV"
        return 0
    fi
    ls -1t "$REPO_ROOT"/logs/analyse_*/zyklen.csv 2>/dev/null | head -n1
}


wait_for_key() {
    printf "\n${YELLOW}Drücke Enter, um ins Menü zurückzukehren...${NC}"
    read dummy
}

analysis_status() {
    timer_enabled=$(systemctl is-enabled wp-analyse.timer 2>/dev/null || true)
    timer_active=$(systemctl is-active wp-analyse.timer 2>/dev/null || true)
    [ -n "$timer_enabled" ] || timer_enabled=unbekannt
    [ -n "$timer_active" ] || timer_active=inaktiv
    printf "${CYAN}=== Automatische Analyse ===${NC}\n"
    printf "Timer enabled: %s\n" "$timer_enabled"
    printf "Timer active:  %s\n" "$timer_active"
    printf "Service:       %s\n" "$(systemctl show wp-analyse.service -p LoadState --value 2>/dev/null || echo unbekannt)"
    printf "Letztes Resultat: %s (ExecMainStatus %s)\n" \
        "$(systemctl show wp-analyse.service -p Result --value 2>/dev/null || echo n/a)" \
        "$(systemctl show wp-analyse.service -p ExecMainStatus --value 2>/dev/null || echo n/a)"
    systemctl list-timers wp-analyse.timer --all --no-pager 2>/dev/null || true
}

install_auto_analysis() {
    service_unit="$REPO_ROOT/Steuerung/wp-analyse.service"
    timer_unit="$REPO_ROOT/Steuerung/wp-analyse.timer"
    if [ ! -f "$service_unit" ] || [ ! -f "$timer_unit" ]; then
        printf "${RED}Timer-Dateien fehlen. Bitte zuerst den aktuellen Code deployen.${NC}\n"
        return 1
    fi
    printf "${CYAN}Installiere und aktiviere taegliche WP-Analyse ...${NC}\n"
    sudo install -m 0644 "$service_unit" /etc/systemd/system/wp-analyse.service || return 1
    sudo install -m 0644 "$timer_unit" /etc/systemd/system/wp-analyse.timer || return 1
    sudo systemctl daemon-reload || return 1
    sudo systemctl enable --now wp-analyse.timer || return 1
    if systemctl is-enabled --quiet wp-analyse.timer && systemctl is-active --quiet wp-analyse.timer; then
        printf "${GREEN}✓ wp-analyse.timer installiert, aktiviert und aktiv.${NC}\n"
        systemctl list-timers wp-analyse.timer --all --no-pager 2>/dev/null || true
        return 0
    fi
    printf "${RED}✗ Timer ist nach der Installation nicht enabled/active.${NC}\n"
    systemctl status wp-analyse.timer --no-pager -l 2>/dev/null || true
    return 1
}

run_auto_analysis() {
    if [ ! -f "$REPO_ROOT/Steuerung/wp-analyse.service" ]; then
        printf "${RED}Analyse-Unit fehlt. Bitte zuerst Option 20.3 ausfuehren.${NC}\n"
        return 1
    fi
    printf "${CYAN}Starte wp-analyse.service manuell ...${NC}\n"
    if ! sudo systemctl start wp-analyse.service; then
        printf "${RED}✗ Analyse-Service konnte nicht erfolgreich beendet werden.${NC}\n"
        journalctl -u wp-analyse.service -n 40 --no-pager 2>/dev/null || true
        return 1
    fi
    result=$(systemctl show wp-analyse.service -p Result --value 2>/dev/null)
    exit_status=$(systemctl show wp-analyse.service -p ExecMainStatus --value 2>/dev/null)
    if [ "$result" = "success" ] && [ "$exit_status" = "0" ]; then
        printf "${GREEN}✓ Analyse-Testlauf erfolgreich.${NC}\n"
        find "$REPO_ROOT/logs/analyse_auto" -maxdepth 1 -type f -printf '%TY-%Tm-%Td %TH:%TM  %p\n' 2>/dev/null | sort | tail -n 8
        return 0
    fi
    printf "${RED}✗ Analyse-Testlauf fehlgeschlagen: Result=%s ExecMainStatus=%s.${NC}\n" "$result" "$exit_status"
    journalctl -u wp-analyse.service -n 40 --no-pager 2>/dev/null || true
    return 1
}

analysis_menu() {
    while :; do
        clear
        printf "${BLUE}=========================================================${NC}\n"
        printf "                WP-Analyse-Verwaltung\n"
        printf "${BLUE}=========================================================${NC}\n"
        printf "1) Status und naechster Timer-Lauf\n"
        printf "2) Manuellen Testlauf starten\n"
        printf "3) Timer installieren / reparieren / aktivieren\n"
        printf "4) Timer deaktivieren\n"
        printf "5) Journal der Analyse (letzte 80 Zeilen)\n"
        printf "0) Zurueck zum Hauptmenue\n"
        printf "Choice: "
        read analysis_choice
        case "$analysis_choice" in
            1) analysis_status ;;
            2) run_auto_analysis ;;
            3) install_auto_analysis ;;
            4)
                if sudo systemctl disable --now wp-analyse.timer; then
                    printf "${GREEN}✓ wp-analyse.timer deaktiviert.${NC}\n"
                else
                    printf "${RED}✗ Timer konnte nicht deaktiviert werden.${NC}\n"
                fi
                ;;
            5) journalctl -u wp-analyse.service -n 80 --no-pager 2>/dev/null || true ;;
            0) return 0 ;;
            *) printf "${RED}Ungültige Auswahl: %s${NC}\n" "$analysis_choice" ;;
        esac
        wait_for_key
    done
}

while true; do
    # Status Informationen abrufen
    CUR_BRANCH=$(cd "$TARGET_DIR" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "Unknown")
    CUR_COMMIT=$(cd "$TARGET_DIR" && git log -1 --oneline 2>/dev/null || echo "No commits")
    
    # ---- Git: lokale Änderungen & Abstand zum Remote ----
    GIT_DIRTY=$(cd "$TARGET_DIR" && git status --porcelain -uno 2>/dev/null | wc -l | tr -d ' ')
    GIT_BEHIND=$(cd "$TARGET_DIR" && git rev-list --count "HEAD..@{u}" 2>/dev/null)
    [ -z "$GIT_BEHIND" ] && GIT_BEHIND="?"

    # ---- Produktionsdaten: nur CSV-Ende/letzter Snapshot, keine Vollfiles ----
    MANAGER_STATUS=$(python3 "$SCRIPT_DIR/manager_status.py" \
        "$HEATING_CSV" "$CYCLE_CSV" "$TARGET_DIR/last_state.txt" 2>/dev/null || true)
    COMP_VALUE=$(status_value compressor); [ -n "$COMP_VALUE" ] || COMP_VALUE="UNBEKANNT"
    COMP_SOURCE=$(status_value compressor_source); [ -n "$COMP_SOURCE" ] || COMP_SOURCE="n/a"
    COMP_AGE=$(status_value compressor_age); [ -n "$COMP_AGE" ] || COMP_AGE="n/a"
    HEATING_AGE=$(status_value heating_age); [ -n "$HEATING_AGE" ] || HEATING_AGE="n/a"
    CYCLE_COUNT=$(status_value cycle_count); [ -n "$CYCLE_COUNT" ] || CYCLE_COUNT="n/a"
    CYCLE_LAST=$(status_value cycle_last_end); [ -n "$CYCLE_LAST" ] || CYCLE_LAST="n/a"
    CYCLE_AGE=$(status_value cycle_age); [ -n "$CYCLE_AGE" ] || CYCLE_AGE="n/a"
    CYCLE_REASON=$(status_value cycle_last_reason); [ -n "$CYCLE_REASON" ] || CYCLE_REASON="n/a"

    # ---- Service-Status mit Details ----
    SVC_ENABLED=$(systemctl is-enabled wpsteuerung 2>/dev/null)
    [ -z "$SVC_ENABLED" ] && SVC_ENABLED="unbekannt"
    SVC_PID=""
    SVC_MEM=""
    SVC_SINCE=""
    SVC_RESTARTS=$(systemctl show wpsteuerung -p NRestarts --value 2>/dev/null)
    [ -z "$SVC_RESTARTS" ] && SVC_RESTARTS="n/a"
    if systemctl is-active --quiet wpsteuerung; then
        SVC_STATUS="${GREEN}✓ AKTIV${NC}"
        SVC_PID=$(systemctl show wpsteuerung -p MainPID --value 2>/dev/null)
        SVC_SINCE=$(systemctl show wpsteuerung -p ActiveEnterTimestamp --value 2>/dev/null)
        SVC_MEM=$(fmt_bytes "$(systemctl show wpsteuerung -p MemoryCurrent --value 2>/dev/null)")
    else
        SVC_STATUS="${RED}✗ INAKTIV${NC}"
    fi

    ANALYSIS_ENABLED=$(systemctl is-enabled wp-analyse.timer 2>/dev/null || true)
    ANALYSIS_ACTIVE=$(systemctl is-active wp-analyse.timer 2>/dev/null || true)
    [ -n "$ANALYSIS_ENABLED" ] || ANALYSIS_ENABLED="unbekannt"
    [ -n "$ANALYSIS_ACTIVE" ] || ANALYSIS_ACTIVE="inaktiv"
    ANALYSIS_NEXT=$(systemctl list-timers wp-analyse.timer --all --no-pager --no-legend 2>/dev/null | awk 'NR==1 {print $1 " " $2 " " $3}')
    [ -z "$ANALYSIS_NEXT" ] && ANALYSIS_NEXT="kein Lauf geplant"

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

    # ---- 24-h-Diagnose: bewusst Journal statt lebenslanger error.log-Zeilen ----
    LOG_SIZE=""
    [ -f "$LOG_FILE" ] && LOG_SIZE=$(du -h "$LOG_FILE" 2>/dev/null | cut -f1)
    RECENT_ERROR_COUNT="n/a"
    LAST_ERR=""
    if command -v journalctl >/dev/null 2>&1; then
        RECENT_ERROR_COUNT=$(journalctl -u wpsteuerung --since '24 hours ago' --no-pager 2>/dev/null \
            | grep -Eic ' ERROR | CRITICAL |Traceback|Exception' || true)
        LAST_ERR=$(journalctl -u wpsteuerung --since '24 hours ago' --no-pager 2>/dev/null \
            | grep -E ' ERROR | CRITICAL |Traceback|Exception' | tail -n 1 | cut -c1-100)
    fi
    RECENT_OOM_COUNT="n/a"
    if command -v journalctl >/dev/null 2>&1; then
        RECENT_OOM_COUNT=$(journalctl -k --since '24 hours ago' --no-pager 2>/dev/null \
            | grep -Eic 'Out of memory|Killed process' || true)
    fi

    clear
    printf "${BLUE}=========================================================${NC}\n"
    printf "${BLUE}               WPSteuerung Manager v1.12                 ${NC}\n"
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
        printf "          ${DIM}läuft seit %s | NRestarts %s${NC}\n" "$SVC_SINCE" "$SVC_RESTARTS"
    else
        printf "          ${DIM}NRestarts %s${NC}\n" "$SVC_RESTARTS"
    fi
    case "$COMP_VALUE" in
        EIN) COMP_COLOR="$GREEN" ;;
        AUS) COMP_COLOR="$CYAN" ;;
        *) COMP_COLOR="$YELLOW" ;;
    esac
    printf "WP:        ${COMP_COLOR}%s${NC}  ${DIM}letzter bekannter Stand: %s (vor %s, %s)${NC}\n" \
        "$COMP_VALUE" "$COMP_VALUE" "$COMP_AGE" "$COMP_SOURCE"
    printf "CSV:       Temperaturdaten vor %s | Zyklen: %s, letzter vor %s (%s)\n" \
        "$HEATING_AGE" "$CYCLE_COUNT" "$CYCLE_AGE" "$CYCLE_LAST"
    printf "           letzter Zyklus: %s\n" "$CYCLE_REASON"
    printf "Analyse:   Timer %s/%s, Autostart %s | nächst: %s\n" \
        "$ANALYSIS_ACTIVE" "$ANALYSIS_ENABLED" "$ANALYSIS_ENABLED" "$ANALYSIS_NEXT"
    printf "VPN:       %b%s\n" "$VPN_STATUS" "$VPN_INFO"
    printf "Lauf:      %b\n" "$LAUF_STATUS"
    SYS_LINE="System:   ${HOST_UP:-unbekannt}"
    [ -n "$CPU_TEMP" ] && SYS_LINE="$SYS_LINE | CPU $CPU_TEMP"
    if [ -n "$DISK_AVAIL" ]; then
        SYS_LINE="$SYS_LINE | Disk: $DISK_AVAIL frei ($DISK_USEPCT belegt)"
    fi
    printf "%s\n" "$SYS_LINE"
    # printf --  : dash (Dash) meldet sonst "Illegal option --", wenn die
    # Farbvariable leer ist (> kein TTY) und das Format mit '-' beginnt.
    printf -- "${BLUE}---------------------------------------------------------${NC}\n"
    if [ "$RECENT_ERROR_COUNT" != "n/a" ] && [ "$RECENT_ERROR_COUNT" != "0" ]; then
        printf "⚠ 24 h:     ${RED}%s Fehler/Traceback${NC}, OOM ${RED}%s${NC}\n" \
            "$RECENT_ERROR_COUNT" "$RECENT_OOM_COUNT"
        [ -n "$LAST_ERR" ] && printf "  ${RED}➜ %s${NC}\n" "$LAST_ERR"
    elif [ "$RECENT_ERROR_COUNT" = "0" ]; then
        printf "Diagnose:   24 h: ${GREEN}keine Fehler/Tracebacks${NC}, OOM: %s\n" "$RECENT_OOM_COUNT"
    else
        printf "Diagnose:   24-h-Journal nicht verfügbar\n"
    fi
    [ -n "$LOG_SIZE" ] && printf "Log:        heizungssteuerung.log (%s)\n" "$LOG_SIZE"
    printf -- "${BLUE}---------------------------------------------------------${NC}\n\n"
    
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
    printf "20) ⏰  Auto-Analyse (Status, Testlauf, Timer, Journal)\n"
    printf "21) 🧠  Upload learning_data.json to Catbox\n"
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
            printf "${CYAN}Starte Service neu und verifiziere...${NC}\n"
            verify_service_action restart wpsteuerung
            wait_for_key
            ;;
        6)
            printf "${CYAN}Stoppe Service und verifiziere...${NC}\n"
            verify_service_action stop wpsteuerung
            wait_for_key
            ;;
        7)
            printf "${CYAN}Starte Service und verifiziere...${NC}\n"
            verify_service_action start wpsteuerung
            wait_for_key
            ;;
        8) ls -la "$TARGET_DIR"; wait_for_key ;;
        9)
            upload_file "$HEATING_CSV" "heizungsdaten.csv"
            wait_for_key
            ;;
        10)
            printf "${CYAN}Aktualisiere WP-Manager (nur Fast-Forward)...${NC}\n"
            safe_manager_update "$REPO_ROOT"
            update_rc=$?
            if [ "$update_rc" -eq 10 ]; then
                sleep 1
                exec sh "$0" "$@"
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
            upload_file "$LOG_FILE" "heizungssteuerung.log"
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
            upload_file "$ENT_LOG_FILE" "entscheidungs_log.jsonl"
            wait_for_key
            ;;
        17)
            ZYKLEN_CSV=$(finde_zyklen_csv)
            if [ -z "$ZYKLEN_CSV" ]; then
                printf "${RED}Keine zyklen.csv gefunden. Der erste Eintrag entsteht nach"
                printf " einem abgeschlossenen Kompressorzyklus;${NC}\\n"
                printf "                alternativ Option 20 (Auto-Analyse) ausfuehren.\\n"
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
                printf "${CYAN}Zyklus-Historie: %s${NC}\\n" "$ZYKLEN_CSV"
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
                upload_file "$ZYKLEN_CSV" "zyklen.csv"
            else
                printf "${RED}Fehler: keine zyklen.csv gefunden.${NC}\\n"
            fi
            wait_for_key
            ;;
        20) analysis_menu ;;
        21)
            upload_file "$LEARNING_JSON" "learning_data.json"
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

            printf "${CYAN}--- Bewertung (startup_diagnose) ---${NC}\n"
            (
                cd "$TARGET_DIR" || exit 1
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
            )

            printf "\\n${CYAN}--- Service / System ---${NC}\\n"
            printf "Neustarts durch systemd: %s\\n" "$(systemctl show wpsteuerung -p NRestarts --value 2>/dev/null)"
            free -m 2>/dev/null | awk 'NR==1 || NR==2'
            if command -v vcgencmd >/dev/null 2>&1; then
                printf "Throttling (Strom/Temp): %s  ${DIM}(0x0 = unauffaellig)${NC}\\n" "$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
            fi
            OOM_COUNT=$(journalctl -k --since '24 hours ago' --no-pager 2>/dev/null \
                | grep -Eic 'Out of memory|Killed process' || true)
            [ -z "$OOM_COUNT" ] && OOM_COUNT="n/a"
            printf "OOM-Ereignisse im Kernel-Journal (letzte 24 h): %s\\n" "$OOM_COUNT"
            printf "${DIM}Bei fehlender Journal-Berechtigung oder leerem Journal ist die Anzahl nur ein Hinweis.${NC}\\n"
            wait_for_key
            ;;
        0) exit 0 ;;
        *)
            printf "${RED}Ungültige Auswahl: '%s'${NC}\n" "$choice"
            sleep 1
            ;;
    esac
done
