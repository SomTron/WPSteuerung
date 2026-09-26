"""CLI: Mehrtages-Simulation der Regelung.

Beispiele:
    py -3 Analyse/sim/run.py                          # alle Szenarien
    py -3 Analyse/sim/run.py september winter_ohne_pv  # Auswahl
    py -3 Analyse/sim/run.py --tage 14                # laenger simulieren
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from harness import Simulation  # noqa: E402
from report import bericht, kpi  # noqa: E402
from thermal import SpeicherParameter  # noqa: E402
from umgebung import SZENARIEN  # noqa: E402


def lade_parameter() -> SpeicherParameter:
    """Laedt die kalibrierten Speicherparameter (falls vorhanden)."""
    pfad = os.path.join(_HERE, "parameter.json")
    if not os.path.exists(pfad):
        return SpeicherParameter()
    with open(pfad, "r", encoding="utf-8") as f:
        daten = json.load(f)
    felder = set(SpeicherParameter.__dataclass_fields__)
    return SpeicherParameter(**{k: v for k, v in daten.items() if k in felder})


def lernpfad_fuer(name: str) -> str:
    """Lerndatei je Szenario - im Tempverzeichnis, nicht im Repository."""
    return os.path.join(tempfile.gettempdir(), f"wpsim_lern_{name}.json")


async def main_async(args) -> int:
    parameter = lade_parameter()
    ergebnisse = {}
    for name in args.szenarien:
        if name not in SZENARIEN:
            print(f"Unbekanntes Szenario: {name}. Verfuegbar: {', '.join(SZENARIEN)}")
            return 1
        sz = SZENARIEN[name]
        if args.tage:
            sz = type(sz)(**{**sz.__dict__, "tage": args.tage})
        lernpfad = lernpfad_fuer(name)
        if os.path.exists(lernpfad):
            os.remove(lernpfad)
        sim = Simulation(sz, parameter=parameter, lernpfad=lernpfad,
                         schritt_sekunden=args.schritt)
        t0 = time.time()
        await sim.simuliere()
        dauer = time.time() - t0
        print(bericht(sim))
        if args.trace:
            tages_trace(sim, args.trace)
        print(f"(Simulationszeit: {dauer:.1f} s fuer {sz.tage} Tage)\n")
        ergebnisse[name] = kpi(sim)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ergebnisse, f, indent=2, ensure_ascii=False)
        print(f"-> {args.json}")
    return 0


def tages_trace(sim, tag: str) -> None:
    """Stundenweise Aufschluesselung eines Tages (Regel + Temperaturen)."""
    von = sim.zeitreihe[0]["t"].date() + timedelta(days=tag - 1)
    bis = von + timedelta(days=1)
    zeilen = [r for r in sim.zeitreihe if von <= r["t"].date() < bis]
    if not zeilen:
        return
    print(f"\nStundenprotokoll {von} (K=1 Kompressor an):")
    print("  Zeit     unten mitte  oben    k   PV    Netz  SOC  Regel")
    for r in zeilen[::3]:      # alle 15 min
        print(f"  {r['t']:%H:%M}  {r['unten']:5.1f} {r['mitte']:5.1f} "
              f"{r['oben']:5.1f}  {int(r['k'])}  {r['pv']:5.0f} "
              f"{r['netz']:5.0f}  {(r['soc'] or 0):4.0f}  {r['regel'] or '-'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Mehrtages-Simulation der WP-Regelung")
    ap.add_argument("szenarien", nargs="*", default=list(SZENARIEN),
                    help="Namen der Szenarien (Default: alle)")
    ap.add_argument("--tage", type=int, default=None, help="Anzahl Tage je Szenario")
    ap.add_argument("--schritt", type=float, default=30.0, help="Simulationsschritt in Sekunden")
    ap.add_argument("--json", default=None, help="Ergebnisse als JSON schreiben")
    ap.add_argument("--trace", type=int, default=0,
                    help="Stundenprotokoll fuer den n-ten Tag ausgeben")
    args = ap.parse_args()
    if not args.szenarien:
        args.szenarien = list(SZENARIEN)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
