#!/bin/sh
# setup_cloudflare_tunnel.sh - Fernzugriff auf die WP-Steuerung einrichten.
#
# Warum Cloudflare und nicht WireGuard (Befund 05.10.2026):
#
# Die Fritz!Box meldet als WAN-IPv4 100.64.32.237. Das liegt in
# 100.64.0.0/10 (RFC 6598, "Shared Address Space") - der Anschluss haengt
# hinter CGNAT. Damit hat der Router keine eigene oeffentliche IPv4 und
# eine Portweiterleitung erreicht das Netz dahinter nie. Ein IPv6-Weg
# scheitert zusaetzlich daran, dass der Mobilfunk-Zugang kein IPv6 hat
# (gleiche Adresse laeuft nur im Heimnetz, weil dort PC und Pi dasselbe
# Praefix nutzen). Damit ist WireGuard von unterwegs grundsaetzlich nicht
# nutzbar - unabhaengig von Schluessel und Portfreigabe.
#
# Ein Cloudflare Tunnel braucht keinen offenen Port und funktioniert
# hinter CGNAT und ohne IPv6. Der Pi baut eine ausgehende Verbindung zu
# Cloudflare auf; die Steuerung ist dann ueber eine HTTPS-Domain erreichbar.
#
# Das Skript ist read-only, bis du ausdruecklich "j" bestaetigst. Es
# aendert nichts an der Heizungssteuerung und keine Firewall-Regel.
#
# Aufruf:
#   ./Updater/setup_cloudflare_tunnel.sh            # nur Diagnose
#   ./Updater/setup_cloudflare_tunnel.sh --install  # Diagnose + Einrichtung
#
# WGS_API_URL  Erreichbarkeit des lokalen Dienstes pruefen (Default http://127.0.0.1:8000)

set -e

API_URL="${WGS_API_URL:-http://127.0.0.1:8000}"
TUNNEL_SERVICE="cloudflared"
INSTALL=0

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
head2() { printf '\n%s== %s ==%s\n' "$CYAN" "$1" "$NC"; }

for arg in "$@"; do
    case "$arg" in
        --install) INSTALL=1 ;;
        -h|--help)
            printf 'Aufruf: %s [--install]\n' "$0"
            printf '  (ohne --install wird nur diagnostiziert)\n'
            exit 0
            ;;
        *)
            printf 'Unbekanntes Argument: %s\n' "$arg" >&2
            exit 2
            ;;
    esac
done

printf '\n%s=== Cloudflare-Tunnel: Fernzugriff einrichten ===%s\n' "$CYAN" "$NC"
# --- 1. CGNAT: die eigentliche Ursache -----------------------------------
head2 "1. Anschlussart (CGNAT?)"
PUBLIC_V4=""
for url in "https://api.ipify.org" "https://ipv4.icanhazip.com"; do
    PUBLIC_V4=$(curl -4 -s -m 10 "$url" 2>/dev/null | tr -d '[:space:]' || true)
    case "$PUBLIC_V4" in
        *[!0-9.]*|'') PUBLIC_V4="" ;;
        *) break ;;
    esac
done

if [ -n "$PUBLIC_V4" ]; then
    ok "oeffentliche Exit-IP: $PUBLIC_V4"
else
    warn "oeffentliche IPv4 nicht ermittelbar"
fi

say ""
say "  ${CYAN}So liest du die WAN-IP ab (Fritz!Box):${NC}"
say "     Internet > Online-Monitor > 'IPv4-Adresse'"
say ""
say "  ${YELLOW}Beginnt sie mit 100.64. bis 100.127.${NC} Dann steht der"
say "  Anschluss hinter CGNAT: eine Portweiterleitung erreicht das Netz"
say "  dahinter nie, und WireGuard ist von unterwegs nicht nutzbar."
say "  Genau dann ist der Cloudflare Tunnel der richtige Weg - er braucht"
say "  keinen offenen Port und kein IPv6."

# --- 2. Lokaler Dienst ---------------------------------------------------
head2 "2. Erreichbarkeit des lokalen Dienstes"
API_PORT_NUM=$(printf '%s' "$API_URL" | sed 's#.*:##')
# curl liefert bei Verbindungsfehlern selbst "000". Das || echo "000" waere
# dann ein zweites "000" - deshalb nur die Auswertung von curl verwenden.
HEALTH_CODE=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "$API_URL/health" 2>/dev/null || printf '000')
[ -n "$HEALTH_CODE" ] || HEALTH_CODE="000"
if [ "$HEALTH_CODE" = "200" ]; then
    ok "GET $API_URL/health -> 200 (Steuerung laeuft)"
elif [ "$HEALTH_CODE" = "000" ]; then
    bad "GET $API_URL/health antwortet nicht"
    warn "Erst den Dienst pruefen: systemctl status wpsteuerung"
else
    warn "GET $API_URL/health -> HTTP $HEALTH_CODE"
fi
say ""
say "  Der Tunnel-Zielport ist $API_PORT_NUM. Der Public Hostname im"
say "  Cloudflare-Dashboard muss darauf zeigen - nicht auf 0.0.0.0."

# --- 3. Schreibschutz: Pflicht, sobald der Tunnel oeffentlich ist ---------
head2 "3. Schreibschutz (WPS_API_KEY)"
ENV_FILE="/etc/wpssteuerung/api.env"
if [ -r "$ENV_FILE" ] && grep -q '^WPS_API_KEY=' "$ENV_FILE" 2>/dev/null; then
    ok "WPS_API_KEY ist in $ENV_FILE gesetzt"
    if grep -q '^WPS_CORS_ORIGINS=' "$ENV_FILE" 2>/dev/null; then
        ok "WPS_CORS_ORIGINS ist gesetzt"
    else
        warn "WPS_CORS_ORIGINS fehlt - die WebApp laedt im Browser"
        warn "fremde Origins ab. Nach dem Tunnel unbedingt setzen:"
        warn "  WPS_CORS_ORIGINS=https://deine-domain.example"
    fi
else
    bad "kein WPS_API_KEY in $ENV_FILE"
    warn "${YELLOW}Ohne Schluessel antworten alle Schreibrouten mit 503.${NC}"
    warn "Das ist nach auen hin harmlos - aber es heisst auch: der"
    warn "Not-Aus-Knopf der WebApp waere wirkungslos."
    say ""
    say "  Im Updater-Menue Option 24 auf dem Pi waehlen und dort"
    say "  den Schluessel erzeugen. Danach den Dienst neu starten."
fi
say ""
say "  ${CYAN}Zusaetzlich dringend empfohlen:${NC} eine ${YELLOW}Cloudflare"
say "  Access-Policy${NC} (Zero Trust > Access > Applications) fuer die"
say "  Domain setzen. Dann kommt man ohne Anmeldung gar nicht erst"
say "  bis zur Heizungssteuerung."

# --- 4. Installieren ------------------------------------------------------
head2 "4. Tunnel einrichten"
if command -v cloudflared >/dev/null 2>&1; then
    ok "cloudflared ist installiert: $(cloudflared --version 2>&1 | head -1)"
else
    warn "cloudflared ist nicht installiert"
fi

if [ "$INSTALL" -ne 1 ]; then
    printf '\n%sNur Diagnose - es wurde nichts geaendert.%s\n' "$YELLOW" "$NC"
    say "Wirklich einrichten?  ./setup_cloudflare_tunnel.sh --install"
    printf '\n%s-- Naechste Schritte im Cloudflare-Dashboard --%s\n' "$CYAN" "$NC"
    say "  1. Zero Trust > Networks > Tunnels > Tunnel waehlen"
    say "  2. Public Hostname hinzufuegen:"
    say "       Subdomain : wp (deine gewaehlte Domain)"
    say "       Typ       : HTTP"
    say "       URL       : localhost:$API_PORT_NUM"
    say "  3. Unter Zero Trust > Access > Applications eine Policy fuer"
    say "     die Domain setzen (E-Mail-Anmeldung)."
    exit 0
fi

printf '\n%sHier wird auf dem Pi ein Dienst installiert.%s\n' "$YELLOW" "$NC"
say "Erreichbar wird danach: die WebApp und die API, ohne offenen Port."
say "Am Projekt selbst und an der Heizungslogik aendert sich nichts."
printf '\nTrotzdem ausfuehren? (j/N): '
read -r REPLY
case "$REPLY" in
    [Jj]*) ;;
    *) printf '%sAbgebrochen - nichts geaendert.%s\n' "$YELLOW" "$NC"; exit 0 ;;
esac

ARCH=$(dpkg --print-architecture 2>/dev/null || echo unknown)
case "$ARCH" in
    arm64) CF_ARCH="arm64" ;;
    armhf) CF_ARCH="arm" ;;
    *)     CF_ARCH="amd64" ;;
esac
say "  Architektur: $ARCH -> cloudflared-linux-$CF_ARCH"

TMP_BIN=$(mktemp)
if curl -fsSL -m 180 "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH" -o "$TMP_BIN"; then
    ok "cloudflared heruntergeladen"
    sudo install -m 0755 "$TMP_BIN" /usr/local/bin/cloudflared
    ok "installiert nach /usr/local/bin/cloudflared"
else
    rm -f "$TMP_BIN"
    bad "Download fehlgeschlagen - Internetverbindung noetig"
    exit 1
fi
rm -f "$TMP_BIN"

printf '\n%sTunnel-Token abfragen.%s\n' "$CYAN" "$NC"
say "Zero Trust > Networks > Tunnels > deinen Tunnel > Configure"
say "Der Token steht dort als 'cloudflared service install <TOKEN>'."
say "Token (Eingabe bleibt unsichtbar): "
if [ -t 0 ]; then
    stty -echo 2>/dev/null || true
fi
read -r TUNNEL_TOKEN
if [ -t 0 ]; then
    stty echo 2>/dev/null || true
fi
printf '\n'

if [ -z "$TUNNEL_TOKEN" ]; then
    bad "kein Token eingegeben - nur cloudflared installiert, kein Dienst"
    exit 1
fi

sudo systemctl stop "$TUNNEL_SERVICE" 2>/dev/null || true
sudo cloudflared service uninstall 2>/dev/null || true
sudo cloudflared service install "$TUNNEL_TOKEN"
ok "Dienst $TUNNEL_SERVICE installiert"

sleep 3
if systemctl is-active --quiet "$TUNNEL_SERVICE"; then
    ok "$TUNNEL_SERVICE laeuft"
else
    bad "$TUNNEL_SERVICE laeuft NICHT"
    journalctl -u "$TUNNEL_SERVICE" -n 20 --no-pager 2>/dev/null || true
    exit 1
fi

printf '\n%s=== Fertig ===%s\n' "$GREEN" "$NC"
say "Im Cloudflare-Dashboard noch Public Hostname auf"
say "  http://localhost:$API_PORT_NUM"
say "setzen und eine Access-Policy fuer die Domain anlegen."
say "Danach ist die WebApp unter https://<deine-subdomain> erreichbar."