import asyncio
import ipaddress
import logging
import sys


VPN_INTERFACE = "wg0"
VPN_CHECK_TIMEOUT_SEC = 5.0


def _set_vpn_ip(state, ip):
    """Statusaktualisierung mit Logging nur bei tatsaechlicher Aenderung."""
    if ip and state.vpn_ip != ip:
        logging.info("VPN Status: Verbunden (%s)", ip)
    elif not ip and state.vpn_ip is not None:
        logging.warning("VPN Status: Verbindung getrennt (wg0 hat keine IP)")
    state.vpn_ip = ip


def _ipv4_from_ip_output(output):
    """Erste gueltige IPv4-Adresse aus der kompakten Ausgabe von ``ip -o`` lesen."""
    for line in output.splitlines():
        parts = line.split()
        try:
            address = parts[parts.index("inet") + 1].split("/", 1)[0]
            return str(ipaddress.IPv4Address(address))
        except (ValueError, IndexError, ipaddress.AddressValueError):
            continue
    return None


async def check_vpn_status(state):
    """
    Prueft, ob das WireGuard-Interface (wg0) aktiv ist und extrahiert die IP-Adresse.
    Aktualisiert state.vpn_ip. Der Prozess wird ohne Shell und mit Timeout ausgefuehrt,
    damit ein haengender Systemaufruf nicht die gesamte Hauptschleife blockiert.
    """
    if sys.platform == "win32":
        return

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "-4",
            "-o",
            "addr",
            "show",
            "dev",
            VPN_INTERFACE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=VPN_CHECK_TIMEOUT_SEC
        )

        if proc.returncode == 0:
            ip = _ipv4_from_ip_output(stdout.decode("utf-8", errors="replace"))
            _set_vpn_ip(state, ip)
        else:
            if state.vpn_ip is not None:
                logging.warning(
                    "VPN Status: %s Interface nicht gefunden oder ip-Aufruf fehlgeschlagen",
                    VPN_INTERFACE,
                )
            state.vpn_ip = None
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.communicate()
            except ProcessLookupError:
                pass
        logging.warning("VPN Status: Timeout nach %.1f Sekunden", VPN_CHECK_TIMEOUT_SEC)
        state.vpn_ip = None
    except Exception as exc:
        logging.error("Fehler beim Pruefen des VPN-Status: %s", exc)
        state.vpn_ip = None
