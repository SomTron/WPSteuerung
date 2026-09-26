"""Vergleicht Konfigurationsvarianten ueber alle Szenarien.

Aufruf:
    py -3 Analyse/sim/varianten.py
    py -3 Analyse/sim/varianten.py --tage 14 --nur basis,abkühlung_kalt

Jede Variante ist eine pure Aenderung an der JSON-Fachkonfiguration
(``wp_steuerung_parameter.json``). Die Simulation faehrt damit dieselbe
Regelungslogik, die auch produktiv laeuft.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Dict

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from harness import Simulation  # noqa: E402
from thermal import SpeicherParameter  # noqa: E402
from umgebung import SZENARIEN  # noqa: E402


def lade_parameter() -> SpeicherParameter:
    pfad = os.path.join(_HERE, "parameter.json")
    if not os.path.exists(pfad):
        return SpeicherParameter()
    with open(pfad, "r", encoding="utf-8") as f:
        daten = json.load(f)
    felder = set(SpeicherParameter.__dataclass_fields__)
    return SpeicherParameter(**{k: v for k, v in daten.items() if k in felder})


#: Die untersuchten Varianten. Jeder Eintrag patcht die Fachkonfiguration.
#: ``basis`` stellt bewusst den ZUSTAND VOR den Verbesserungen wieder her,
#: damit der Vergleich auch nach dem Aendern der Projektdatei aussagekraeftig
#: bleibt.
VARIANTEN: Dict[str, dict] = {
    "basis": {
        "beschreibung": "Ausgangszustand vor den Verbesserungen",
        "aenderungen": {
            "notfallschutz": {"ausschalten_bei_c": 38.0,
                              "temperaturfuehler": "oben"},
            "abweichung": {"quelle_warten_min_forecast_wh_qm": 0.0},
            "legionellen": {"max_tage_ohne_lauf": 0},
        },
    },
    "verbessert": {
        "beschreibung": (
            "kein PV-Warten bei trueber Wetterlage + Legionella-Hygienefrist"
            " (Notfallschutz und Batterie wie Nutzerwunsch belassen)"
        ),
        "aenderungen": {},
    },

    "netz_offset_6": {
        "beschreibung": "Netz-Notfall der Abweichung bei 36C statt 30C",
        "aenderungen": {"abweichung": {"netz_notfall_offset_k": 6.0}},
    },
    "netz_offset_4": {
        "beschreibung": "Netz-Notfall der Abweichung bei 38C",
        "aenderungen": {"abweichung": {"netz_notfall_offset_k": 4.0}},
    },
    "hysterese_2": {
        "beschreibung": "Abweichung springt bereits bei 2.0K statt 4.9K an",
        "aenderungen": {"abweichung": {"einschalten_bei_abweichung_k": 2.0}},
    },
    "kombi": {
        "beschreibung": "Notfall kaeltester + Netz-Offset 4K + Hysterese 2K",
        "aenderungen": {
            "notfallschutz": {"temperaturfuehler": "alle"},
            "abweichung": {"netz_notfall_offset_k": 4.0,
                           "einschalten_bei_abweichung_k": 2.0},
        },
    },
    "kombi_eng": {
        "beschreibung": "kombi + Mindestlaufzeit 45 min statt 60 min",
        "aenderungen": {
            "notfallschutz": {"temperaturfuehler": "alle"},
            "abweichung": {"netz_notfall_offset_k": 4.0,
                           "einschalten_bei_abweichung_k": 2.0},
            "zyklus": {"mindestlaufzeit_minuten": 45},
        },
    },
    "unwetter1500": {
        "beschreibung": "bei truer Wetterlage nicht auf PV warten (<1500 Wh/m2)",
        "aenderungen": {"abweichung": {"quelle_warten_min_forecast_wh_qm": 1500.0}},
    },
    "unwetter_kombi": {
        "beschreibung": "Unwetter-Ausnahme + Notfallschutz am kaeltesten Fuehler",
        "aenderungen": {
            "abweichung": {"quelle_warten_min_forecast_wh_qm": 1500.0},
            "notfallschutz": {"temperaturfuehler": "alle"},
        },
    },
    "unwetter_kombi_2500": {
        "beschreibung": "Unwetter-Ausnahme (<2500) + kaeltester Notfallfuehler",
        "aenderungen": {
            "abweichung": {"quelle_warten_min_forecast_wh_qm": 2500.0},
            "notfallschutz": {"temperaturfuehler": "alle"},
        },
    },
    # --- Der eigentliche Hebel: der Notfallschutz gibt im Winter die
    # Solltemperatur vor. Mit ausschalten_bei_c=38 beendet er jeden Lauf bei
    # 38 C, bevor die Abweichungs-Regel ihr 42-C-Ziel erreichen kann.
    "notfall_aus_42": {
        "beschreibung": "Notfallschutz schaltet erst bei 42C ab (statt 38C)",
        "aenderungen": {"notfallschutz": {"ausschalten_bei_c": 42.0}},
    },

    "notfall_42_kalt_unwetter": {
        "beschreibung": "42C-Abschaltung + kaeltester Fuehler + Unwetter-Ausnahme",
        "aenderungen": {
            "notfallschutz": {"ausschalten_bei_c": 42.0,
                              "temperaturfuehler": "alle"},
            "abweichung": {"quelle_warten_min_forecast_wh_qm": 1500.0},
        },
    },
    "notfall_42_kalt_unwetter_hyst": {
        "beschreibung": "wie vor, aber Abweichung mit 2.5K Hysterese",
        "aenderungen": {
            "notfallschutz": {"ausschalten_bei_c": 42.0,
                              "temperaturfuehler": "alle"},
            "abweichung": {"quelle_warten_min_forecast_wh_qm": 1500.0,
                           "einschalten_bei_abweichung_k": 2.5},
        },
    },
}

#: Kennzahlen, die in der Vergleichstabelle erscheinen.
METRIKEN = [
    ("netz_pro_tag_kwh", "Netz kWh/Tag", "{:.2f}"),
    ("min_mitte_global", "min mitte", "{:.1f}"),
    ("stunden_mitte_unter_40", "h mitte<40C", "{}"),
    ("min_unten_global", "min unten", "{:.1f}"),
    ("stunden_unten_unter_35", "h unten<35C", "{}"),
    ("pv_eigenverbrauch_prozent", "PV-Eigenverbr %", "{:.0f}"),
    ("max_unten_global", "max unten", "{:.1f}"),
    ("stunden_unten_ueber_45", "h unten>45C", "{}"),
    ("wp_laufzeit_h", "WP-Laufzeit h", "{:.1f}"),
    ("zyklen", "Zyklen", "{}"),
    ("kurzlaeufe", "Kurzlaeufe", "{}"),
    ("boiler_max_ereignisse", "Boiler-Max", "{}"),
    ("legionellen_fertig", "Legionella ok", "{}"),
    ("batterie_regel_h", "Batt-Regel h", "{:.1f}"),
    ("soc_voll_prozent", "Zeit SOC>=90 %", "{:.0f}"),
]


def kennzahlen(sim) -> dict:
    """Kennzahlen inklusive der Komfort-Kennzahlen aus der Zeitreihe."""
    aus = dict(sim.kennzahlen_basis)
    werte = sim.zeitreihe
    if not werte:
        return aus
    dt_h = (werte[1]["t"] - werte[0]["t"]).total_seconds() / 3600.0
    aus["min_mitte_global"] = min(r["mitte"] for r in werte)
    aus["max_oben_global"] = max(r["oben"] for r in werte)
    aus["max_unten_global"] = max(r["unten"] for r in werte)
    aus["min_unten_global"] = min(r["unten"] for r in werte)
    aus["stunden_mitte_unter_40"] = round(
        sum(dt_h for r in werte if r["mitte"] < 40.0), 1)
    aus["stunden_mitte_unter_42"] = round(
        sum(dt_h for r in werte if r["mitte"] < 42.0), 1)
    aus["stunden_unten_unter_35"] = round(
        sum(dt_h for r in werte if r["unten"] < 35.0), 1)
    aus["stunden_unten_unter_38"] = round(
        sum(dt_h for r in werte if r["unten"] < 38.0), 1)
    aus["stunden_unten_ueber_45"] = round(
        sum(dt_h for r in werte if r["unten"] > 45.0), 1)
    pro_tag: Dict = {}
    for r in werte:
        pro_tag.setdefault(r["t"].date(), []).append(r["unten"])
    aus["tage_unten_unter_40"] = sum(1 for v in pro_tag.values() if min(v) < 40.0)
    aus["tage_unten_unter_35"] = sum(1 for v in pro_tag.values() if min(v) < 35.0)
    aus["legionellen_fertig"] = int(sim.legionellen_fertig)
    aus["pv_vergeblich_kwh"] = sim.diag_pv_vergeblich_wh / 1000.0
    aus["netz_waermer_h"] = sim.diag_netz_waermer_h
    aus["regel_minuten"] = {
        name: round(min * 60) for name, min in
        sorted(sim.diag_regel_minuten.items(), key=lambda kv: -kv[1])[:6]
    }
    # Batterie-Sichtbarkeiten
    aus["batterie_regel_h"] = round(
        sim.diag_regel_minuten.get("Batterie", 0.0) / 60.0, 2)
    soc_werte = [r["soc"] for r in werte if r.get("soc") is not None]
    aus["soc_voll_prozent"] = (
        sum(1 for v in soc_werte if v >= 90.0) / len(soc_werte) * 100.0
        if soc_werte else 0.0
    )
    aus["soc_min"] = min(soc_werte) if soc_werte else 0.0
    aus["soc_max"] = max(soc_werte) if soc_werte else 0.0
    return aus


async def main_async(args) -> int:
    parameter = lade_parameter()
    szenarien = args.szenarien or list(SZENARIEN)
    varianten = args.varianten or list(VARIANTEN)
    ergebnisse: Dict[str, Dict[str, dict]] = {}
    t0 = time.time()

    for vname in varianten:
        if vname not in VARIANTEN:
            print(f"Unbekannte Variante: {vname}")
            return 1
        v = VARIANTEN[vname]
        ergebnisse[vname] = {}
        for sname in szenarien:
            sz = SZENARIEN[sname]
            if args.tage:
                sz = type(sz)(**{**sz.__dict__, "tage": args.tage})
            lernpfad = os.path.join(
                tempfile.gettempdir(), f"wpsim_lern_{vname}_{sname}.json"
            )
            if os.path.exists(lernpfad):
                os.remove(lernpfad)
            sim = Simulation(sz, parameter=parameter, lernpfad=lernpfad,
                             schritt_sekunden=args.schritt,
                             config_patch=v["aenderungen"])
            await sim.simuliere()
            ergebnisse[vname][sname] = kennzahlen(sim)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(ergebnisse, f, indent=2, ensure_ascii=False)

    # --- Vergleichstabelle ---
    linie = "=" * 100
    print(linie)
    print(f"Konfigurationsvergleich  ({args.tage or SZENARIEN[szenarien[0]].tage} Tage je Szenario, "
          f"Schritt {args.schritt:g} s)")
    print(linie)
    for sname in szenarien:
        print(f"\n### Szenario: {SZENARIEN[sname].name}\n")
        kopf = f"{'Variante':<20}" + "".join(f"{lbl:>16}" for _, lbl, _ in METRIKEN)
        print(kopf)
        print("-" * len(kopf))
        for vname in varianten:
            k = ergebnisse[vname][sname]
            zeile = f"{vname:<20}"
            for key, _lbl, fmt in METRIKEN:
                zeile += f"{fmt.format(k.get(key, 0)):>16}"
            print(zeile)
        print()
        for vname in varianten:
            if vname == "basis":
                continue
            print(f"  {vname}: {VARIANTEN[vname]['beschreibung']}")
    print(f"\n{linie}")
    print(f"Rechenzeit: {time.time() - t0:.0f} s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Konfigurationsvergleich der WP-Regelung")
    ap.add_argument("--tage", type=int, default=10)
    ap.add_argument("--schritt", type=float, default=60.0)
    ap.add_argument("--szenarien", nargs="*", default=None)
    ap.add_argument("--varianten", nargs="*", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    if args.szenarien and len(args.szenarien) == 1 and "," in args.szenarien[0]:
        args.szenarien = args.szenarien[0].split(",")
    if args.varianten and len(args.varianten) == 1 and "," in args.varianten[0]:
        args.varianten = args.varianten[0].split(",")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
