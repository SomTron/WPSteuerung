#!/usr/bin/env python3
"""dns-diag.py - DNS-Diagnose auf dem Pi. Read-only: aendert nichts.

Anlass (Befund 13.10.2026): Der Healthcheck der Steuerung meldete einen Fehler,
obwohl der Fernzugriff auf den Pi lief. Healthcheck, Telegram und die Solax-API
brauchen alle *ausgehendes* DNS - der Fernzugriff nicht. Faellt die
Namensaufloesung aus, sehen diese drei gleichzeitig alt aus, waehrend das
Geraet erreichbar bleibt.

Das Skript beantwortet fuenf Fragen:
  1. Welche Nameserver stehen in /etc/resolv.conf, und wer verwaltet die Datei?
  2. Antworten diese (und optional weitere) Server ueberhaupt auf UDP/53?
  3. Was macht der System-Resolver (getaddrinfo) daraus?
  4. Ist aiodns in DIESEM Interpreter vorhanden, und loest es auf?
  5. Passt die requirements.txt neben diesem Skript zum Repo (enthaelt aiodns)?

Aufruf:
  python3 Updater/dns-diag.py                          # nur konfigurierte Server
  python3 Updater/dns-diag.py 192.168.178.1 1.1.1.1    # zusaetzlich diese testen

Warum ohne "dig": auf dem Pi ist dig oft nicht installiert (Befund
13.10.2026: "dig: command not found"). Die Abfrage wird deshalb von Hand als
DNS-Paket gebaut - Standardbibliothek, keine Zusatzpakete.

Es wird nichts geaendert, nichts installiert und nichts angelegt.
"""

import asyncio
import os
import socket
import struct
import sys
import time

TESTNAME = "hc-ping.com"
TIMEOUT_S = 3.0


def dns_query(name):
    """Minimale DNS-Anfrage (A-Record, RD=1) als Bytes."""
    frage = b""
    for teil in name.split("."):
        frage += bytes([len(teil)]) + teil.encode("ascii")
    frage += b"\x00"
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    return header + frage + struct.pack(">HH", 1, 1)


def udp_dns_test(server):
    """UDP/53-Abfrage gegen einen Nameserver. Gibt (ok, dauer_ms, info) zurueck."""
    paket = dns_query(TESTNAME)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.settimeout(TIMEOUT_S)
    start = time.time()
    try:
        sock.sendto(paket, (server, 53))
        antwort, _ = sock.recvfrom(2048)
        dauer_ms = (time.time() - start) * 1000
        return True, dauer_ms, f"{dauer_ms:.0f} ms, {len(antwort)} Bytes Antwort"
    except socket.timeout:
        return False, None, f"Timeout nach {TIMEOUT_S:.0f}s (UDP/53 gesperrt oder Server tot)"
    except OSError as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def lies_resolv_conf():
    """(nameserver, text) aus /etc/resolv.conf lesen."""
    server = []
    text = ""
    try:
        with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"  [FEHL] /etc/resolv.conf nicht lesbar: {exc}")
        return server, text
    for zeile in text.splitlines():
        teile = zeile.split()
        if len(teile) >= 2 and teile[0] == "nameserver":
            server.append(teile[1])
    return server, text


def verwalter_von_resolv_conf(text):
    """Wer die Datei ueberschreibt - wichtig, weil Aenderungen dort verpuffen."""
    for marke, wer in (
        ("tailscale", "Tailscale"),
        ("systemd-resolved", "systemd-resolved"),
        ("NetworkManager", "NetworkManager"),
        ("dhcpcd", "dhcpcd"),
    ):
        if marke.lower() in text.lower():
            return wer
    return None


def pruefe_repo_requirements():
    """Enthaelt die requirements.txt des Repos aiodns? Deckt falsche Dateien auf."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pfad = os.path.join(repo, "requirements.txt")
    if not os.path.exists(pfad):
        return False, f"nicht gefunden: {pfad}"
    try:
        with open(pfad, encoding="utf-8-sig", errors="replace") as fh:
            zeilen = [z.strip() for z in fh if z.strip() and not z.strip().startswith("#")]
    except OSError as exc:
        return False, f"nicht lesbar ({exc})"
    return ("aiodns" in zeilen), f"{pfad} ({len(zeilen)} Eintraege)"


def main():
    print("\n=== DNS-Diagnose der WPSteuerung (read-only) ===")

    print("\n-- 1. /etc/resolv.conf --")
    server, text = lies_resolv_conf()
    if server:
        for eintrag in server:
            print(f"  [ ok ] nameserver {eintrag}")
    else:
        print("  [FEHL] keine nameserver-Zeile gefunden")
    verwalter = verwalter_von_resolv_conf(text)
    if verwalter:
        print(f"  [WARN] Datei wird von {verwalter} verwaltet - eigene Aenderungen")
        print("         darin werden ueberschrieben, der Zustand kommt von dort.")
        if verwalter == "Tailscale":
            print("         Faellt tailscaled aus, stockt die GESAMTE Namensaufloesung:")
            print("         Fernzugriff laeuft weiter, Healthcheck/Telegram/Solax nicht.")

    print(f"\n-- 2. UDP/53-Abfrage fuer {TESTNAME} --")
    geprueft = list(dict.fromkeys(server + sys.argv[1:]))
    if not geprueft:
        print("  [WARN] kein Server zum Testen - Adressen mitgeben, z.B.")
        print("         python3 Updater/dns-diag.py 192.168.178.1 100.100.100.100 1.1.1.1")
    ergebnisse = {s: udp_dns_test(s) for s in geprueft}
    for s, (ok, _dauer, info) in ergebnisse.items():
        print(f"  [{' ok ' if ok else 'FEHL'}] {s:<18} {info}")
    # Schnellster zuerst: ein lokaler Resolver (Router) antwortet typisch in
    # wenigen Millisekunden und ist bei einem WAN-Aussetzer noch erreichbar.
    brauchbar = sorted(
        (s for s, (ok, _dauer, _info) in ergebnisse.items() if ok),
        key=lambda s: ergebnisse[s][1],
    )

    print("\n-- 3. System-Resolver (getaddrinfo, wie Python selbst aufloest) --")
    try:
        adressen = socket.getaddrinfo(TESTNAME, 443, proto=socket.IPPROTO_TCP)
        ips = sorted({a[4][0] for a in adressen})
        print(f"  [ ok ] {TESTNAME} -> {', '.join(ips)}")
    except OSError as exc:
        print(f"  [FEHL] {type(exc).__name__}: {exc}")

    print("\n-- 4. aiodns in diesem Interpreter --")
    try:
        import aiodns
    except ImportError:
        print("  [WARN] aiodns fehlt - aiohttp faellt auf den ThreadedResolver zurueck.")
        print("         Der belegt Threads; stockt DNS, verzoegert das auch die")
        print("         Sensor-Lesung. Abhilfe: pip install -r requirements.txt")
    else:
        print(f"  [ ok ] aiodns vorhanden: {os.path.dirname(aiodns.__file__)}")
        for ziel in ([brauchbar[0]] if brauchbar else []) + [None]:
            label = ziel or "System-Nameserver"
            try:
                resolver = aiodns.DNSResolver(nameservers=[ziel]) if ziel else aiodns.DNSResolver()
                asyncio.run(resolver.gethostbyname(TESTNAME, "A"))
                print(f"  [ ok ] aiodns loest {TESTNAME} auf ueber {label}")
            except Exception as exc:
                print(f"  [FEHL] aiodns ueber {label}: {type(exc).__name__}: {exc}")
                if "Timeout" in str(exc):
                    print("         Genau das erzeugt 'Timeout while contacting DNS servers'.")

    print("\n-- 5. requirements.txt des Repos --")
    hat_aiodns, info = pruefe_repo_requirements()
    if hat_aiodns:
        print("  [ ok ] aiodns enthalten")
    else:
        print("  [WARN] aiodns NICHT enthalten -> Datei ist alt oder es ist die falsche Datei")
    print(f"         {info}")

    print("\n-- 6. Empfehlung --")
    if brauchbar:
        print("  In config.ini eintragen (schnellster zuerst = typisch der Router):")
        print("    [Netzwerk]")
        print(f"    DNS_SERVER = {', '.join(brauchbar[:2])}")
    else:
        print("  Keiner der getesteten Server hat geantwortet. Weitere Adressen testen:")
        print("    python3 Updater/dns-diag.py 192.168.178.1 100.100.100.100 1.1.1.1 8.8.8.8")
    print("\n  Es wurde nichts geaendert.\n")


if __name__ == "__main__":
    main()


