#!/bin/sh
# wg-endpoint-check.sh - WireGuard-Endpunkt auf dem Pi pruefen und
#                       eine Client-Adresse fuer IPv4 ausgeben.
#
# Anlass (Befund 05.10.2026): Die Windows-/Android-Tunnel zeigten "aktiv",
# aber der Pi-Peer war nicht erreichbar. Ursache war kein Schluessel- oder
# Dienstproblem, sondern der Endpoint der Clients:
#
#   Endpoint = [2a0d:5dc1:1000:501:5846:ec2b:d68f:a41f]:51820
#
# Das ist eine IPv6-Adresse. Das WLAN der Clients hatte keine globale
# IPv6-Adresse und keine IPv6-Default-Route, dort konnte also gar kein
# Handshake aufgebaut werden. Zusaetzlich ist die Interface-ID
# (5846:ec2b:d68f:a41f, ohne "ff:fe") keine EUI-64, sondern eine temporaere
# Privacy-Adresse - sie wechselt bei jedem Neuverbinden und taugt damit
# auch mit echtem IPv6 nicht als fester Endpoint.
#
# Dieses Skript aendert NICHTS und legt NICHTS an. Es prueft nur und gibt
# am Ende eine fertige Endpoint-Zeile zum Uebernehmen aus.
#
# Aufruf:
#   ./wg-endpoint-check.sh                     # Diagnose + Endpoint-Zeile
#   ./wg-endpoint-check.sh --name wg.example.dyndns.org
#                                             # DynDNS-Namen statt IP
#
# WGS_WG_CONF  Pfad auf wg0.conf (Default /etc/wireguard/wg0.conf)

set -e

WG_CONF="${WGS_WG_CONF:-/etc/wireguard/wg0.conf}"
WG_IF="wg0"
DYNDNS_NAME=""

if [ -t 1 ]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    CYAN='\033[0;36m'
    NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; NC=''
fi

say()  { printf '%s\n' "$1"; }
ok()   { printf "  ${GREEN}[ ok ]${NC} %s\n" "$1"; }
bad()  { printf "  ${RED}[FEHL]${NC} %s\n" "$1"; }
warn() { printf "  ${YELLOW}[WARN]${NC} %s\n" "$1"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --name)
            DYNDNS_NAME="${2:-}"
            shift 2 || shift
            ;;
        -h|--help)
            printf 'Aufruf: %s [--name DYN-DNS-NAME]\n' "$0"
            exit 0
            ;;
        *)
            printf 'Unbekanntes Argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

printf '\n%s=== WireGuard-Endpoint-Diagnose auf dem Pi ===%s\n' "$CYAN" "$NC"

# --- 1. Dienst ------------------------------------------------------------
printf '\n%s-- 1. Dienst wg-quick@%s --%s\n' "$CYAN" "$WG_IF" "$NC"
if ! command -v wg >/dev/null 2>&1; then
    bad "wg ist nicht installiert (sudo apt-get install -y wireguard)"
else
    ok "wg installiert: $(wg --version 2>/dev/null | head -1)"
fi

if command -v systemctl >/dev/null 2>&1; then
    if systemctl is-active --quiet "wg-quick@$WG_IF"; then
        ok "wg-quick@$WG_IF laeuft"
    else
        bad "wg-quick@$WG_IF laeuft NICHT - sudo systemctl enable --now wg-quick@$WG_IF"
    fi
    if systemctl is-enabled --quiet "wg-quick@$WG_IF" 2>/dev/null; then
        ok "wg-quick@$WG_IF startet beim Boot automatisch"
    else
        warn "wg-quick@$WG_IF startet nach einem Reboot nicht automatisch"
    fi
else
    warn "systemctl nicht gefunden - Dienststatus nicht pruefbar"
fi

# --- 2. Konfiguration -----------------------------------------------------
printf '\n%s-- 2. Konfiguration %s --%s\n' "$CYAN" "$WG_CONF" "$NC"
LISTEN_PORT=""
if [ -f "$WG_CONF" ]; then
    ok "Konfiguration vorhanden"
    if [ -r "$WG_CONF" ]; then
        LISTEN_PORT=$(grep -E '^[[:space:]]*ListenPort[[:space:]]*=' "$WG_CONF" 2>/dev/null \
                      | head -1 | cut -d= -f2 | tr -d '[:space:]')
        ADDRESS_LINE=$(grep -E '^[[:space:]]*Address[[:space:]]*=' "$WG_CONF" 2>/dev/null \
                       | head -1 | cut -d= -f2- | sed 's/^[[:space:]]*//')
        if [ -n "$LISTEN_PORT" ]; then
            ok "ListenPort: $LISTEN_PORT"
        else
            bad "kein ListenPort gesetzt - UDP-Port nicht eindeutig"
        fi
        if [ -n "$ADDRESS_LINE" ]; then
            ok "Address: $ADDRESS_LINE"
        else
            warn "kein Address-Eintrag fuer $WG_IF"
        fi
        PEER_COUNT=$(grep -cE '^\[Peer\]' "$WG_CONF" 2>/dev/null || echo 0)
        ok "Peer-Eintraege: $PEER_COUNT"
    else
        warn "Konfiguration nicht lesbar (sudo noetig) - ohne sudo blind"
    fi
else
    bad "Konfiguration fehlt: $WG_CONF"
fi

# --- 3. Interface ---------------------------------------------------------
printf '\n%s-- 3. Interface --%s\n' "$CYAN" "$NC"
WG4=$(ip -4 -o addr show dev "$WG_IF" 2>/dev/null \
      | awk '{for(i=1;i<=NF;i++) if($i=="inet") print $(i+1)}' | head -1)
if [ -n "$WG4" ]; then
    ok "$WG_IF hat IPv4: $WG4"
else
    bad "$WG_IF hat keine IPv4-Adresse"
fi

# --- 4. Weiterleitung -----------------------------------------------------
printf '\n%s-- 4. Weiterleitung --%s\n' "$CYAN" "$NC"
if [ -r /proc/sys/net/ipv4/ip_forward ]; then
    FORWARD=$(cat /proc/sys/net/ipv4/ip_forward)
    if [ "$FORWARD" = "1" ]; then
        ok "net.ipv4.ip_forward = 1"
    else
        bad "net.ipv4.ip_forward = $FORWARD - setze: sudo sysctl -w net.ipv4.ip_forward=1"
    fi
else
    warn "ip_forward nicht lesbar (sudo noetig)"
fi

if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q '^Status: active'; then
        warn "ufw ist aktiv - offene Ports pruefen: sudo ufw status"
    else
        ok "ufw ist nicht aktiv"
    fi
else
    ok "kein ufw installiert"
fi

# --- 5. Oeffentliche Adresse ----------------------------------------------
printf '\n%s-- 5. Oeffentliche Adresse --%s\n' "$CYAN" "$NC"
PUBLIC_V4=""
for url in "https://api.ipify.org" "https://ipv4.icanhazip.com" "https://ifconfig.me/ip"; do
    PUBLIC_V4=$(curl -4 -s -m 10 "$url" 2>/dev/null | tr -d '[:space:]' || true)
    case "$PUBLIC_V4" in
        *[!0-9.]*|'') PUBLIC_V4="" ;;
        *) break ;;
    esac
done

if [ -n "$PUBLIC_V4" ]; then
    ok "oeffentliche IPv4: $PUBLIC_V4"
else
    warn "oeffentliche IPv4 nicht ermittelbar (Internet oder DNS nicht erreichbar)"
fi

PUBLIC_V6=""
for url in "https://api64.ipify.org" "https://ifconfig.co/ip"; do
    PUBLIC_V6=$(curl -6 -s -m 10 "$url" 2>/dev/null | tr -d '[:space:]' || true)
    case "$PUBLIC_V6" in
        *:*) break ;;
        *) PUBLIC_V6="" ;;
    esac
done

if [ -n "$PUBLIC_V6" ]; then
    warn "oeffentliche IPv6: $PUBLIC_V6"
    warn "IPv6-Endpoint bleibt ungeeignet, solange Clients kein natives IPv6 haben"
else
    ok "keine eigene globale IPv6 - IPv4 ist der richtige Weg"
fi

# --- 6. Endpoint-Zeile ----------------------------------------------------
printf '\n%s-- 6. Endpoint fuer die Clients --%s\n' "$CYAN" "$NC"
PORT="${LISTEN_PORT:-51820}"
ENDPOINT_HOST=""

if [ -n "$DYNDNS_NAME" ]; then
    ENDPOINT_HOST="$DYNDNS_NAME"
    printf '  %sEmpfohlen (DynDNS, bleibt stabil):%s\n' "$GREEN" "$NC"
    say "    Endpoint = $DYNDNS_NAME:$PORT"
    say ""
    say "  Der DynDNS-A-Record muss auf $PUBLIC_V4 zeigen."
elif [ -n "$PUBLIC_V4" ]; then
    ENDPOINT_HOST="$PUBLIC_V4"
    printf '  %sEndpoint (direkte IPv4):%s\n' "$GREEN" "$NC"
    say "    Endpoint = $PUBLIC_V4:$PORT"
    say ""
    say "  ${YELLOW}Wichtig:${NC} Diese IP aendert sich beim Neuverbinden des Anschlusses."
    say "  Danach muss die Zeile in jedem Client erneut angepasst werden."
    say "  ${CYAN}Besser:${NC} DynDNS-Namen eintragen (siehe --name)."
else
    say "  ${YELLOW}Keine oeffentliche IPv4 ermittelt - Endpoint manuell aus dem Router uebernehmen.${NC}"
fi

# --- 7. CGNAT-Check im Router --------------------------------------------
printf '\n%s-- 7. CGNAT-Check im Router --%s\n' "$CYAN" "$NC"
say "  So wird CGNAT erkannt - es entscheidet, ob die Weiterleitung ueberhaupt wirkt:"
say "    1. Im Router die WAN-IPv4 ablesen"
say "       (Fritz!Box: Internet > Online-Monitor > IPv4-Adresse)"
if [ -n "$PUBLIC_V4" ]; then
    say "    2. Vergleich mit der oeffentlichen IPv4 dieses Skripts: $PUBLIC_V4"
    say ""
    say "    ${YELLOW}Unterschiedliche Werte = CGNAT.${NC} Eine Portweiterleitung"
    say "    erreicht den Pi dann nicht. In dem Fall hilft nur ein DynDNS-Anbieter"
    say "    mit IPv4-Portmapping oder der bereits vorhandene Cloudflare-Tunnel."
else
    say "    2. Vergleich mit der oeffentlichen IPv4 (dieses Skript: unbekannt)"
fi

# --- 7b. IPv6 im Zielnetz (Befund 05.10.2026, Mobilfunk) -----------------
# Ein IPv6-Endpoint funktioniert nur, wenn das Netz, aus dem der Client
# verbindet, selbst IPv6 mitbringt. Im deutschen Mobilfunk ist das nicht
# die Regel (Befund: Mobilfunk ohne IPv6 -> kein Handshake). Waehrend im
# Heimnetz dasselbe Kabel problemlos laeuft, weil beide Geraete im selben
# Praefix liegen und der Traffic nicht ueber das Internet laeuft.
# Deshalb ist ein Test aus dem Heimnetz kein Nachweis fuer den Mobilfunk.
printf '\n%s-- 7b. IPv6-Voraussetzung im Client-Netz --%s\n' "$CYAN" "$NC"
say "  Ein IPv6-Endpoint braucht IPv6 im Netz des Clients."
say "  Ohne IPv6 dort ist kein Handshake moeglich - unabhaengig von"
say "  Portfreigabe, Schluessel und korrekter Adresse."
say ""
say "  ${YELLOW}Handy im Mobilfunk pruefen:${NC} test-ipv6.com oder ipv6-test.com"
say "  ${YELLOW}PC an einen Handyhotspot:${NC}   ping -6 2001:4860:4860::8888"
say "  Keine Antwort = das Netz hat kein IPv6."

# Stabilitaet der globalen IPv6 pruefen. Eine Interface-ID aus zufaelligen
# Hexwerten wechselt, sobald der Anschluss neu verbindet - dann zeigt ein
# fest eingetragener Endpoint ins Leere, ohne dass ein Fehler sichtbar wird.
PI6_GUA=$(ip -6 -o addr show dev wlan0 2>/dev/null \
          | awk '{for(i=1;i<=NF;i++) if($i=="inet6" && $i+1 !~ /^fd/) print $(i+1)}' | head -1 \
          | cut -d'/' -f1)
if [ -n "$PI6_GUA" ]; then
    # EUI-64 aus der MAC: in der Interface-ID steht die Gruppe "fffe" - das
    # ist das ueber die Gruppengrenze hinweg zusammengezogene ff:fe.
    # 2c:cf:67:a7:11:3e  ->  02ec:f67a:fffe:a713:003e
    # Zufaellig vergebene IDs (wie 5846:ec2b:d68f:a41f) enthalten das nicht.
    PI6_IF=$(printf '%s' "$PI6_GUA" | awk -F: '{print $5":"$6":"$7":"$8":"$9}')
    case "$PI6_IF" in
        *fffe*)
            ok "Pi-GUA: $PI6_GUA"
            ok "Interface-ID ist EUI-64 aus der MAC - stabile Adresse"
            ;;
        *)
            warn "Pi-GUA: $PI6_GUA"
            warn "Interface-ID '$PI6_IF' ist zufaellig (kein EUI-64 aus der MAC)."
            warn "Sie wechselt beim Neuverbinden - ein fest eingetragener Endpoint"
            warn "zeigt dann ins Leere, ohne dass ein Fehler sichtbar wird."
            warn "Stabilisieren: in der Fritz!Box beim Geraet die IPv6-Interface-ID"
            warn "fest eintragen. Aus der MAC 2c:cf:67:a7:11:3e ergaebe sich:"
            warn "  -> 02ec:f67a:fffe:a713:003e"
            ;;
    esac
else
    warn "keine globale IPv6 auf wlan0 gefunden"
fi

# --- 8. Naechste Schritte -------------------------------------------------
printf '\n%s-- 8. Naechste Schritte --%s\n' "$CYAN" "$NC"
say "  ${CYAN}IPv6 (dein aktuelles Setup) - gut fuer:${NC}"
say "     Zugriff im Heimnetz und in Netzen mit IPv6. Kein offener Port noetig."
say ""
say "  ${YELLOW}IPv6 ist im Mobilfunk nicht verfuegbar (Befund 05.10.2026).${NC}"
say "  Fuer das Handy im Mobilfunk brauchst du einen zweiten Weg:"
say ""
say "     Option 1 - IPv4 + DynDNS (bevorzugt, wenn kein CGNAT):"
say "       1. In der Fritz!Box: Heimnetz > Netzwerk > Geraet 'IPv4-Adresse"
say "          dauerhaft zuweisen' aktivieren (Pi haelt 192.168.178.29)"
say "       2. Portfreigabe: UDP 51820 extern -> 192.168.178.29, IPv4"
say "       3. DynDNS beim Router-Anbieter einrichten"
say "       4. In beiden Clients: Endpoint = <DynDNS-Name>:51820"
if [ -n "$PUBLIC_V4" ]; then
    say "          aktuell oeffentliche IPv4: $PUBLIC_V4"
fi
say ""
say "     Option 2 - Cloudflare Tunnel (funktioniert auch hinter CGNAT):"
say "       Das Projekt bringt Steuerung/setup_cloudflare.sh mit."
say "       Einmalig auf dem Pi ausfuehren, danach ist die Steuerung"
say "       ohne offene Ports und ohne IPv6 erreichbar."
say "  1. Im Router eine Portweiterleitung einrichten:"
if [ -n "$WG4" ]; then
    say "     UDP $PORT von aussen auf ${WG4} ($WG_IF)"
else
    say "     UDP $PORT von aussen auf die IPv4-Adresse des Pi ($WG_IF)"
fi
say "  2. In jedem Client die Endpoint-Zeile aus Schritt 6 uebernehmen."
say "     Schluessel und AllowedIPs bleiben unveraendert."
say "  3. Tunnel deaktivieren, Config importieren, wieder aktivieren."
say "  4. Gegenprobe: Aufruf der WebApp ueber die Tunnel-IP des Pi."

printf '\n'
say "  ${CYAN}Tunnel auf dem PC pruefen (PowerShell, als Administrator):${NC}"
say '    & "$env:ProgramFiles\WireGuard\wg.exe" show'
say "    Erwartet: eine Zeile 'latest handshake: ...' mit Abstand < 2 Minuten."
say "    Bleibt sie aus, antwortet der Endpoint nicht - Port oder Adresse stimmt nicht."
printf '\n'

printf '%s\n' "$YELLOW"
printf '%s\n' "Dieses Skript hat nichts geaendert und nichts angelegt."
printf '%s\n' "Es zeigt nur den Ist-Zustand."
printf '%s\n' "$NC"