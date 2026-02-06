#!/bin/bash
# Install Cloudflare Tunnel on Raspberry Pi

set -e

# Farben
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

printf "${GREEN}=== Cloudflare Tunnel Setup ===${NC}\n"

# 1. Architektur prüfen
ARCH=$(dpkg --print-architecture)
printf "Erkannte Architektur: ${GREEN}$ARCH${NC}\n"

if [ "$ARCH" != "armhf" ] && [ "$ARCH" != "arm64" ]; then
    printf "${RED}Warnung: Dieses Skript ist primär für Raspberry Pi (armhf/arm64) gedacht.${NC}\n"
fi

# 2. Token abfragen
printf "\nBitte gib deinen Cloudflare Tunnel Token ein.\n"
printf "(Diesen erhältst du im Cloudflare Zero Trust Dashboard unter Access > Tunnels)\n"
printf "Token: "
read -r TUNNEL_TOKEN

if [ -z "$TUNNEL_TOKEN" ]; then
    printf "${RED}Fehler: Kein Token eingegeben.${NC}\n"
    exit 1
fi

# 3. Cloudflared herunterladen und installieren
printf "\n${GREEN}Lade cloudflared herunter...${NC}\n"

if [ "$ARCH" = "arm64" ]; then
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O cloudflared
elif [ "$ARCH" = "armhf" ]; then
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm -O cloudflared
else
    # Fallback für amd64 (falls auf PC getestet)
    wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O cloudflared
fi

chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/

# 4. Service installieren
printf "\n${GREEN}Installiere Service...${NC}\n"
sudo cloudflared service uninstall 2>/dev/null || true
sudo cloudflared service install "$TUNNEL_TOKEN"

printf "\n${GREEN}Fertig! Der Tunnel sollte jetzt aktiv sein.${NC}\n"
printf "Bitte konfiguriere jetzt im Cloudflare Dashboard den 'Public Hostname':\n"
printf "  Service: HTTP\n"
printf "  URL:     localhost:8000\n"
