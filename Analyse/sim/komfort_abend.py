"""Simuliert die Komfortanforderung: mittags mittig warm, abends unten warm.

Nutzerwunsch (22.10.2026): "mittags mittig warmes Wasser und abends
unten auch".

Die Simulation misst fuer ein Szenario ueber mehrere Tage:
  * MITTAG (9-16 Uhr): war der FUEHLER "mitte" warm genug?
  * ABEND (17-22 Uhr): war der FUEHLER "unten" warm genug?

Verglichen werden Varianten der mindest_temp-Regel:
  * ""            - unveraendert (Basis)
  * "abend_unten" - zusaetzlicher Eintrag "Abend-Unten" (unten, 17-22 Uhr)
  * "abend_unten_40" - wie oben, aber 40C statt 42C

Aufruf:  python -m Analyse.sim.komfort_abend [szenario ...]
"""
import asyncio
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "Steuerung"))

from umgebung import SZENARIEN  # noqa: E402
from harness import Simulation  # noqa: E402

#: Komfortschwelle: der Fuehler muss so warm sein.
MITTAG_SOLL_C = 42.0
ABEND_SOLL_C = 42.0
#: Wie oft wird der Zustand geprueft (alle 5 Minuten).
SCHRITT = 300.0


def _messe(szenario_name: str, patch: dict) -> Dict:
    """Simuliert ein Szenario und misst die Komforterfuellung."""
    sz = SZENARIEN[szenario_name]
    sim = Simulation(sz, schritt_sekunden=SCHRITT, seed=42,
                     lernpfad=os.path.join(
                         os.environ.get("TEMP", "/tmp"),
                         f"wpsim_komfort_{szenario_name}.json"),
                     config_patch=patch)
    asyncio.run(sim.simuliere())

    mittag_ok = mittag_soll = 0
    abend_ok = abend_soll = 0
    mittag_temp: List[float] = []
    abend_temp: List[float] = []
    # Nutzerwunsch (22.10.2026): "ich moechte nicht dass die Waermepumpe
    # in der Nacht laeuft". Deshalb wird der Nachtlauf (19 Uhr bis 8 Uhr)
    # je Variante mitgemessen - er muss 0 bleiben.
    nachtlauf_minuten = 0.0
    schritte_je_tag = max(1.0, 86400.0 / 300.0)
    for eintrag in sim.zeitreihe:
        stunde = eintrag["t"].hour
        if eintrag.get("k") and (stunde >= 19 or stunde < 8):
            nachtlauf_minuten += 300.0 / 60.0
        zeit = eintrag.get("t")
        if zeit is None:
            continue
        stunde = zeit.hour
        mitte = eintrag.get("mitte")
        unten = eintrag.get("unten")
        if 9 <= stunde < 16 and mitte is not None:
            mittag_soll += 1
            mittag_temp.append(float(mitte))
            if float(mitte) >= MITTAG_SOLL_C:
                mittag_ok += 1
        if 17 <= stunde < 22 and unten is not None:
            abend_soll += 1
            abend_temp.append(float(unten))
            if float(unten) >= ABEND_SOLL_C:
                abend_ok += 1
    return {
        "mittag_quote": (mittag_ok / mittag_soll) if mittag_soll else 0.0,
        "mittag_min": min(mittag_temp) if mittag_temp else None,
        "mittag_mittel": (sum(mittag_temp) / len(mittag_temp)) if mittag_temp else None,
        "abend_quote": (abend_ok / abend_soll) if abend_soll else 0.0,
        "abend_min": min(abend_temp) if abend_temp else None,
        "abend_mittel": (sum(abend_temp) / len(abend_temp)) if abend_temp else None,
        "netz_wh": sim.netzbezug_wh,
        "pv_wh": sim.pv_erzeugt_wh,
        "laufzeit_h": sim.wp_laufzeit_s / 3600.0,
        "nachtlauf_min_je_tag": nachtlauf_minuten / max(
            1.0, len(sim.zeitreihe) / schritte_je_tag),
    }


def _patch_mit_abend_unten(soll_c: float) -> dict:
    """Patch: zusaetzlicher MindestTemp-Eintrag fuer "unten" am Abend."""
    return {
        "mindest_temp": {
            "__eintraege__": {
                "Abend-Unten": {
                    "temperaturfuehler": "unten",
                    "min_temp_c": soll_c,
                    "start_uhr": 17,
                    "ende_uhr": 22,
                    "nachtsperre_ueberschreiben": False,
                    "fenster_aus_lernen": False,
                },
            },
        },
    }


def main() -> int:
    namen = sys.argv[1:] or list(SZENARIEN)
    varianten = [
        ("Basis (unveraendert)", {}),
        ("+ Abend-Unten 42C", _patch_mit_abend_unten(42.0)),
        ("+ Abend-Unten 40C", _patch_mit_abend_unten(40.0)),
        ("+ Abend-Mitte 38C", {
            "mindest_temp": {"__eintraege__": {"Abend-Mitte": {"min_temp_c": 38.0}}}
        }),
        ("+ Mittel 38 + Unten 40", {
            "mindest_temp": {
                "__eintraege__": {
                    "Abend-Mitte": {"min_temp_c": 38.0},
                    "Abend-Unten": {
                        "temperaturfuehler": "unten",
                        "min_temp_c": 40.0,
                        "start_uhr": 17,
                        "ende_uhr": 22,
                        "nachtsperre_ueberschreiben": False,
                        "fenster_aus_lernen": False,
                    },
                },
            },
        }),
    ]
    for name in namen:
        if name not in SZENARIEN:
            print(f"  unbekanntes Szenario: {name}")
            continue
        print(f"\n=== Szenario: {name} ===")
        print(f"  {'Variante':26} {'mittags mitte>=42':>18} "
              f"{'abends unten>=42':>18} {'Netz kWh':>10} {'Laufz h':>9} "
              f"{'Nacht min/Tag':>14}")
        for label, patch in varianten:
            m = _messe(name, patch)
            print(f"  {label:26} {m['mittag_quote']*100:17.0f}% "
                  f"{m['abend_quote']*100:17.0f}% {m['netz_wh']/1000:10.1f} "
                  f"{m['laufzeit_h']:9.1f} {m['nachtlauf_min_je_tag']:14.0f}")
            if m["nachtlauf_min_je_tag"] > 0:
                print(f"  {'':26} ACHTUNG: {m['nachtlauf_min_je_tag']:.0f} min "
                      "Nachtlauf (Notfallschutz, Prio 110 - greift vor der "
                      "Nachtsperre)")
            if m["mittag_min"] is not None:
                print(f"  {'':26} min {m['mittag_min']:5.1f}C / Mittel "
                      f"{m['mittag_mittel']:5.1f}C   |   min {m['abend_min']:5.1f}C / "
                      f"Mittel {m['abend_mittel']:5.1f}C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
