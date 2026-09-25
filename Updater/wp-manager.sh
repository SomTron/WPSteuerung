#!/bin/sh
# wp-manager.sh - stabiler Launcher fuer die WPSteuerung-Verwaltung
#
# Das Menue liegt bewusst in wp-manager-menu.sh. Diese Datei bleibt klein und
# stabil, damit ein fehlerhaftes Menue-Update nicht auch den einzigen Startweg
# beschaedigt. Der Launcher fuehrt einen Syntax-/Startcheck aus und faellt bei
# Bedarf auf die letzte erfolgreich gestartete Kopie im State-Verzeichnis zurueck.
#
# Reihenfolge eines Starts:
#   1. aktuelles Menue pruefen
#   2. bei Fehler: Fehlermeldung + letzte gute Kopie starten
#   3. nach erfolgreichem Ende: aktuelles Menue als gute Kopie merken
#
# WPS_MANAGER_STATE_DIR kann den State-Pfad fuer Tests/Testsysteme ueberschreiben.

SCRIPT_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || exit 1
MENU_SCRIPT="$SCRIPT_DIR/wp-manager-menu.sh"
PROJECT_ROOT="${WPS_MANAGER_PROJECT_ROOT:-$(dirname "$SCRIPT_DIR")}"
STATE_DIR="${WPS_MANAGER_STATE_DIR:-${XDG_STATE_HOME:-${HOME:-/tmp}/.local/state}/wps-manager}"
LAST_GOOD="$STATE_DIR/wp-manager-menu.last-good.sh"
LAST_GOOD_META="$STATE_DIR/last-good-commit.txt"
LAUNCHER_PID=$$

umask 077
mkdir -p "$STATE_DIR" 2>/dev/null || {
    printf '%s\n' "WP-Manager: State-Verzeichnis nicht beschreibbar: $STATE_DIR" >&2
    exit 1
}

print_error() {
    printf '\n%s\n' "=========================================================" >&2
    printf '%s\n' "WP-Manager FEHLER" >&2
    printf '%s\n' "$1" >&2
    printf '%s\n' "=========================================================" >&2
}

preflight_menu() {
    candidate="$1"
    [ -f "$candidate" ] || return 1
    [ -r "$candidate" ] || return 1
    command -v python3 >/dev/null 2>&1 || return 1
    sh -n "$candidate" >/dev/null 2>&1 || return 1
    return 0
}

promote_last_good() {
    candidate="$1"
    tmp="$LAST_GOOD.tmp.$$"
    if cp "$candidate" "$tmp" 2>/dev/null && mv -f "$tmp" "$LAST_GOOD" 2>/dev/null; then
        if command -v git >/dev/null 2>&1; then
            git -C "$SCRIPT_DIR" rev-parse HEAD > "$LAST_GOOD_META" 2>/dev/null || :
        fi
        return 0
    fi
    rm -f "$tmp" 2>/dev/null || :
    return 1
}

run_fallback() {
    # 127 = kein nutzbarer Ersatzstand (aendert sich nie durch den Menue-Exitcode)
    [ -f "$LAST_GOOD" ] || return 127
    preflight_menu "$LAST_GOOD" || return 127
    if [ -f "$LAST_GOOD_META" ]; then
        print_error "Letzter funktionierender Manager-Stand wird verwendet: $(cat "$LAST_GOOD_META" 2>/dev/null)"
    else
        print_error "Letzter funktionierender Manager-Stand wird verwendet."
    fi
    WPS_MANAGER_SCRIPT_DIR="$SCRIPT_DIR" WPS_MANAGER_PROJECT_ROOT="$PROJECT_ROOT" \
        sh "$LAST_GOOD" "$@"
}

# Expliziter Downgrade-Weg: startet die letzte funktionierende Kopie,
# auch wenn das aktuelle Menue selbst nicht startbar ist.
if [ "${1:-}" = "--downgrade" ] || [ "${1:-}" = "--fallback" ]; then
    run_fallback
    downgrade_rc=$?
    if [ "$downgrade_rc" -eq 127 ]; then
        print_error "Downgrade nicht moeglich: keine funktionierende Ersatzkopie."
        exit 1
    fi
    exit "$downgrade_rc"
fi

if ! preflight_menu "$MENU_SCRIPT"; then
    print_error "Das aktuelle Manager-Menue ist nicht startbar (Syntax-, Lese- oder Python-Abhaengigkeitsfehler): $MENU_SCRIPT"
    run_fallback "$@"
    fallback_rc=$?
    if [ "$fallback_rc" -eq 127 ]; then
        print_error "Es ist keine funktionierende Ersatzkopie vorhanden. Bitte Manager-Repository pruefen."
        exit 1
    fi
    # Die Ersatzkopie ist bereits gelaufen; sie darf nicht durch das defekte
    # aktuelle Menue als 'gut' markiert werden.
    exit "$fallback_rc"
fi

WPS_MANAGER_SCRIPT_DIR="$SCRIPT_DIR" WPS_MANAGER_PROJECT_ROOT="$PROJECT_ROOT" \
    sh "$MENU_SCRIPT" "$@"
menu_rc=$?
if [ "$menu_rc" -eq 0 ]; then
    promote_last_good "$MENU_SCRIPT" || {
        print_error "Letzter guter Stand konnte nicht gesichert werden: $LAST_GOOD"
    }
    exit 0
fi

print_error "Manager-Menue wurde mit Exitcode $menu_rc beendet. Es erfolgt kein erfolgreicher Stand-Wechsel."
run_fallback "$@"
fallback_rc=$?
if [ "$fallback_rc" -eq 127 ]; then
    print_error "Kein funktionierender Ersatzstand verfuegbar."
    exit "$menu_rc"
fi
exit "$fallback_rc"
