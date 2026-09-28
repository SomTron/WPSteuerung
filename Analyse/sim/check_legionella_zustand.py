"""Simuliert den ZUSTAND AUS DEM NUTZER-LOG vom 28.09.2026, 17:37.

    Oben=55.0 | Mittig=41.2 | Unten=22.8, PV 2697W, SOC 99%
    -> "Boiler-Max-Naehe (oben 55.0C, ... Einschalten erst < 45.0C)"

Der normale Szenario-Ablauf erreicht diesen Zustand nicht: nach der
Legionellenprophylaxe bleibt im Speichermodell auch `unten` warm
(konvektion_aus_w_k=12), waehrend die reale Anlage auf 22.8 C fiel.
Deshalb wird der Speicher hier direkt mit den Logwerten gestartet.

Geprueft wird die REALE Regelung (handle_compressor_on/-off) ueber einen
Tag, mit PV-Ueberschuss am Vormittag.
"""
import asyncio
import os
import sys
import tempfile
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "Steuerung"))

from harness import Simulation  # noqa: E402
from umgebung import SZENARIEN  # noqa: E402

# Zustand aus dem Nutzer-Log
LOG_UNTEN = 22.8
LOG_MITTE = 41.2
LOG_OBEN = 55.0


async def main() -> int:
    basis = SZENARIEN["september"]
    sz = type(basis)(
        **{
            **basis.__dict__,
            "tage": 1,
            "start": date(2026, 9, 28),
        }
    )
    sim = Simulation(
        sz, lernpfad=os.path.join(tempfile.gettempdir(), "wpsim_check.json")
    )

    # Speicher auf den Logzustand setzen
    sim.speicher.unten.temperatur = LOG_UNTEN
    sim.speicher.mitte.temperatur = LOG_MITTE
    sim.speicher.oben.temperatur = LOG_OBEN
    print(f"Startzustand (aus Nutzer-Log): unten={LOG_UNTEN} "
          f"mitte={LOG_MITTE} oben={LOG_OBEN}\n")

    await sim.simuliere()

    zeilen = sim.zeitreihe
    k_an = [z for z in zeilen if z.get("k")]
    regeln = {}
    for z in zeilen:
        r = z.get("regel")
        if r:
            regeln[r] = regeln.get(r, 0) + 1

    print(f"Schritte gesamt:            {len(zeilen)}")
    print(f"Schritte mit laufender WP:  {len(k_an)}")
    print(f"WP-Laufzeit:                {len(k_an) * 30 / 3600:.2f} h")
    print(f"min unten:                  {min(z['unten'] for z in zeilen):.1f} C")
    print(f"max unten:                  {max(z['unten'] for z in zeilen):.1f} C")
    print(f"max oben:                   {max(z['oben'] for z in zeilen):.1f} C")
    print(f"max mitte:                  {max(z['mitte'] for z in zeilen):.1f} C")
    print(f"ausloesende Regeln:         {regeln}")

    # Entscheidende Pruefungen
    fehler = []
    if not k_an:
        fehler.append("WP lief nie - das waere der gemeldete Fehler!")
    if max(z["unten"] for z in zeilen) < 35.0:
        fehler.append("unten blieb kalt - der Boiler wurde nicht aufgeheizt")
    if max(z["oben"] for z in zeilen) > 58.0:
        fehler.append("oben ueberschritt den Ueberhitzungsschutz (58 C)")

    print()
    if fehler:
        for f in fehler:
            print(f"FEHLER: {f}")
        return 1
    print("ERGEBNIS: Der Boiler heizt, der Oberfuehler bleibt unter 58 C.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
