import asyncio
import logging
import sys

async def check_vpn_status(state):
    """
    Prüft, ob das WireGuard-Interface (wg0) aktiv ist und extrahiert die IP-Adresse.
    Aktualisiert state.vpn_ip.
    """
    # Auf Windows-Systemen (Entwicklung) überspringen wir den echten Check
    if sys.platform == "win32":
        # Zu Testzwecken auf Windows setzen wir nichts, oder einen Dummy-Wert falls gewünscht
        # state.vpn_ip = "10.0.0.1 (Simulated)" 
        return

    try:
        # Prüfe ob wg0 existiert und hole IP
        # Wir nutzen ip addr show wg0 und filtern nach der ersten IPv4 Adresse
        cmd = "ip addr show wg0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1"
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            ip = stdout.decode().strip()
            if ip:
                if state.vpn_ip != ip:
                    logging.info(f"VPN Status: Verbunden ({ip})")
                state.vpn_ip = ip
            else:
                if state.vpn_ip is not None:
                    logging.warning("VPN Status: Verbindung getrennt (wg0 hat keine IP)")
                state.vpn_ip = None
        else:
            if state.vpn_ip is not None:
                logging.warning(f"VPN Status: wg0 Interface nicht gefunden oder Fehler")
            state.vpn_ip = None
            
    except Exception as e:
        logging.error(f"Fehler beim Prüfen des VPN-Status: {e}")
        state.vpn_ip = None
