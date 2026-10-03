#!/usr/bin/env python3
"""Auswertung des Entscheidungslogs fuer einen Tag.

Auf dem Pi starten:
    python3 ~/<dieses Skript>

Zeigt, warum der Kompressor ein- oder ausgeschaltet wurde und wie lange
er lief. Beantwortet die Frage "warum keine Solarzeit?" belegt statt
vermutet.
"""
import collections
import json
import sys
from datetime import datetime

TAG = sys.argv[1] if len(sys.argv) > 1 else "2026-10-03"
PFAD = "/home/patrik/WPSteuerung/Steuerung/entscheidungs_log.jsonl"


def main() -> int:
    zeilen = []
    with open(PFAD, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if str(d.get("ts", "")).startswith(TAG):
                zeilen.append(d)

    if not zeilen:
        print(f"Keine Eintraege fuer {TAG} in {PFAD}")
        return 1

    print(f"=== {TAG}: {len(zeilen)} Eintraege ===")
    print(f"Zeitraum: {zeilen[0]['ts']}  bis  {zeilen[-1]['ts']}")

    lauf = sum(1 for d in zeilen if d.get("kompressor_laeuft"))
    print(f"Kompressor an: {lauf}/{len(zeilen)} Zyklen "
          f"({100.0 * lauf / len(zeilen):.0f} %)")

    print("\n--- Haeufigste Gruende fuer 'soll_einschalten = false'")
    gr = collections.Counter()
    for d in zeilen:
        if not d.get("soll_einschalten"):
            gr[(d.get("grund") or "?")[:70]] += 1
    for k, v in gr.most_common(10):
        print(f"{v:5d}  {k}")

    print("\n--- Zyklen mit Ueberschuss (sollte Solar sein)")
    hoch = [d for d in zeilen
            if (d.get("pv_ueberschuss_w") or 0) > 0]
    genutzt = [d for d in hoch if d.get("kompressor_laeuft")]
    verschenkt = [d for d in hoch if not d.get("kompressor_laeuft")]
    print(f"Ueberschuss>0 : {len(hoch)} Zyklen")
    print(f"  davon genutzt: {len(genutzt)}")
    print(f"  verschenkt  : {len(verschenkt)}")
    if hoch:
        spitze = max(hoch, key=lambda d: d.get("pv_ueberschuss_w") or 0)
        print(f"  max. Ueberschuss: {spitzen_uhr(spitze)}"
              f"  {spitze.get('pv_ueberschuss_w')} W")

    if verschenkt:
        print("\n--- Warum wurde der Ueberschuss nicht genutzt?")
        vg = collections.Counter()
        for d in verschenkt:
            vg[(d.get("grund") or "?")[:70]] += 1
        for k, v in vg.most_common(8):
            print(f"{v:5d}  {k}")
        print("\nBeispiele:")
        for d in verschenkt[:6]:
            print(f"  {spitzen_uhr(d)}  ueb={d.get('pv_ueberschuss_w')}W"
                  f"  pv={d.get('pv_acpower_w')}W  soc={d.get('soc')}%"
                  f"  t_oben={d.get('t_oben')}  -> {(d.get('grund') or '')[:50]}")

    print("\n--- Temperaturverlauf oben (Stichproben)")
    probe = zeilen[::max(1, len(zeilen) // 12)]
    for d in probe:
        print(f"  {spitzen_uhr(d)}  t_oben={d.get('t_oben')}"
              f"  t_mitte={d.get('t_mitte')}  t_unten={d.get('t_unten')}"
              f"  komp={'AN' if d.get('kompressor_laeuft') else 'aus'}")

    return 0


def spitzen_uhr(d) -> str:
    try:
        return datetime.fromisoformat(str(d.get("ts"))).strftime("%H:%M:%S")
    except ValueError:
        return str(d.get("ts"))[-8:]


if __name__ == "__main__":
    sys.exit(main())