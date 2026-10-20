"""Vergleich von Kandidaten fuer die Start-Antizipation (Analyse, KEIN Produktivcode).

Hintergrund (Befund 14.10.2026, Nutzerprotokoll 18:05-19:44):
Die Start-Antizipation rechnete mit einer Heizrate von **21 C/h** und blockte
damit sinnvolle Starts (Hub 6,1 K -> "18 min < 60 min Mindestlaufzeit"),
waehrend Heizplan und CalcStart im **selben** Lauf **5,7 C/h** nannten.

Ursache im Code:
    priority_control_logic.py:808   rate = gelernte_rate_unten or _rate_fuer_entscheidung(...)
    priority_control_logic.py:2466  rate_var = _rate_fuer_entscheidung(state, ...)

Der Heizplan nimmt die Rate aus der Learning-Engine (gekappt), die
Start-Antizipation ruft `_rate_fuer_entscheidung` direkt auf und umgeht damit
die Kappung `heating_rate_max_c_h = 6.0`. Die Begruendung steht dort selbst:

    learning_engine.py:46-50
    "Physikalische Obergrenze fuer die Planungsrate: ... ~5,2 K/h fuer den
     GANZEN Speicher. Laeuft der untere Knoten sichtbar schneller, ist das ein
     Lokaleffekt und kein Mass fuer die Startplanung."

Zweiter Kandidat: `_ist_pv_gesteuerter_lauf` kennt nur "Einspeisung",
"AdaptivePV" und "PV_*". Ein CalcStart, der selbst quellengestuetzt startet
(Log: "PV: PV-Erzeugung 117W, ... kein Netzkauf"), wird gegen die volle
60-min-Mindestlaufzeit geprueft statt gegen `pv_min_laufzeit_minuten` (10).

Dieses Skript provisioniert beide Kandidaten als Monkeypatch und faehrt die
ECHTE Regelungslogik ueber identische Szenarien und Seeds. Es aendert nichts
am Produktionscode und nichts an der Projektkonfiguration.

Aufruf:
    py -3 Analyse/sim/kandidaten.py
    py -3 Analyse/sim/kandidaten.py --tage 14 --szenarien september winter_misch
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from harness import Simulation, lade_steuerung  # noqa: E402
from thermal import SpeicherParameter  # noqa: E402
from umgebung import SZENARIEN  # noqa: E402
from varianten import kennzahlen  # noqa: E402


def lade_parameter() -> SpeicherParameter:
    """Laedt die kalibrierten Speicherparameter (falls vorhanden)."""
    pfad = os.path.join(_HERE, "parameter.json")
    if not os.path.exists(pfad):
        return SpeicherParameter()
    with open(pfad, "r", encoding="utf-8") as f:
        daten = json.load(f)
    felder = set(SpeicherParameter.__dataclass_fields__)
    return SpeicherParameter(**{k: v for k, v in daten.items() if k in felder})


# --------------------------------------------------------------------------
# Kandidaten: jede Funktion patcht ein Modul und gibt eine Ruecknahme zurueck.
# --------------------------------------------------------------------------

def _rate_cap(pcl, _le_mod):
    """Kappt die Antizipations-Rate auf die dokumentierte Planungsgrenze."""
    original = pcl._rate_fuer_entscheidung

    def begrenzt(state, t_unten):
        rate = original(state, t_unten)
        cfg = getattr(
            getattr(getattr(state, "learning_engine", None), "data", None),
            "config", None,
        )
        grenze = float(getattr(cfg, "heating_rate_max_c_h", 0.0) or 0.0)
        return min(rate, grenze) if grenze > 0 else rate

    pcl._rate_fuer_entscheidung = begrenzt
    return lambda: setattr(pcl, "_rate_fuer_entscheidung", original)


def _rate_cap_12(pcl, _le_mod):
    """Wie _rate_cap, aber mit der Config-Untergrenze rate_fallback_c_h (12)."""
    original = pcl._rate_fuer_entscheidung

    def begrenzt(state, t_unten):
        return min(original(state, t_unten), 12.0)

    pcl._rate_fuer_entscheidung = begrenzt
    return lambda: setattr(pcl, "_rate_fuer_entscheidung", original)


def _calcstart_pv(pcl, _le_mod):
    """CalcStart zaehlt als quellengesteuerter Lauf (10 statt 60 min)."""
    original = pcl._ist_pv_gesteuerter_lauf
    pcl._ist_pv_gesteuerter_lauf = lambda name: original(name) or name == "CalcStart"
    return lambda: setattr(pcl, "_ist_pv_gesteuerter_lauf", original)
def _calcstart_pv(pcl, _le_mod):
    """CalcStart zaehlt als quellengesteuerter Lauf (10 statt 60 min)."""
    original = pcl._ist_pv_gesteuerter_lauf
    pcl._ist_pv_gesteuerter_lauf = lambda name: original(name) or name == "CalcStart"
    return lambda: setattr(pcl, "_ist_pv_gesteuerter_lauf", original)


#: Sonnenuntergang (Stunde, lokale Zeit) naeherungsweise fuer den Standort
#: (ca. 52,5 Grad Nord). Das Modell hat KEIN Sonnenmodell - die PV-Profile
#: sind skalierte Septemberprofile (siehe README) -, deshalb diese Tabelle.
SONNENUNTERGANG_H = {
    1: 16.5, 2: 17.5, 3: 18.4, 4: 19.8, 5: 20.9, 6: 21.4,
    7: 21.1, 8: 20.3, 9: 19.3, 10: 18.2, 11: 16.8, 12: 16.4,
}


def _spaetstart_su(pcl, _le_mod):
    """Spätest-Start nur bis (Sonnenuntergang - Reserve) aufschieben.

    Frage (Nutzer 18.10.2026): Wird mit CalcStart so lange gewartet, dass der
    Start im Winter nach Sonnenuntergang faellt - also ohne PV? Der
    Spätest-Start greift, sobald der freie Puffer unter `spaetstart_puffer_h`
    (0,5 h) faellt. Dieser Kandidat verkleinert den Puffer auf
    `Zielzeit - (Sonnenuntergang - Reserve)`, wenn das kleiner ist: der
    "letzte Moment" liegt dann noch im Tageslicht. Der Puffer wird nur
    ausgetauscht, wenn die Grenze wirklich frueher greift (Kopie der Config,
    kein Eingriff ins Original).
    """
    original = pcl.bewerte_alle_regeln
    reserve_h = 0.5

    def begrenzt(*args, **kw):
        if args:                       # Positional aufgerufen: unveraendert
            return original(*args, **kw)
        config = kw.get("config")
        now = kw.get("now")
        if config is not None and now is not None:
            su = SONNENUNTERGANG_H.get(now.month, 18.0) - reserve_h
            ziel = kw.get("learned_target_hour")
            if ziel is None:
                ziel = float(config.calculated_start.target_uhr)
            grenze = max(float(ziel) - su, 0.0)
            if grenze < config.calculated_start.spaetstart_puffer_h:
                kopie = config.model_copy(deep=True)
                kopie.calculated_start.spaetstart_puffer_h = round(grenze, 3)
                kw = dict(kw)
                kw["config"] = kopie
        return original(**kw)

    pcl.bewerte_alle_regeln = begrenzt
    return lambda: setattr(pcl, "bewerte_alle_regeln", original)



KANDIDATEN: Dict[str, tuple] = {
    "basis": ("unveraendert (Produktionsstand)", [], {}),
    "rate_cap": ("Antizipations-Rate auf heating_rate_max_c_h (6 C/h) begrenzt",
                 [_rate_cap], {}),
    "rate_cap_12": ("Antizipations-Rate auf 12 C/h begrenzt (mittlere Stufe)",
                    [_rate_cap_12], {}),
    "calcstart_pv": ("CalcStart als quellengesteuerter Lauf (10 min)",
                     [_calcstart_pv], {}),
    "spaetstart_su": ("Spaetest-Start hoechstens bis Sonnenuntergang - 0,5 h",
                      [_spaetstart_su], {}),
    "spaetstart_su_nf": ("wie spaetstart_su, zusaetzlich Netzfallback erlaubt",
                         [_spaetstart_su],
                         {"calculated_start": {"netz_fallback_erlaubt": True}}),
    "beides": ("rate_cap + calcstart_pv", [_rate_cap, _calcstart_pv], {}),
    "antizipation_aus": ("Start-Antizipation komplett abgeschaltet (Grenzwert)",
                         [], {"sicherheit": {"start_vorhersage_aktiv": False}}),
}

#: Kennzahlen der Vergleichstabelle: (Schluessel, Beschriftung, Format).
KPI_ZEILEN: List[tuple] = [
    ("netz_pro_tag_kwh", "Netz kWh/Tag", "{:.2f}"),
    ("netz_waermer_h", "WP m. Netz h", "{:.1f}"),
    ("pv_vergeblich_kwh", "vergebliche PV kWh", "{:.1f}"),
    ("pv_eigenverbrauch_prozent", "PV-Eigenverbr %", "{:.0f}"),
    ("wp_laufzeit_h", "WP-Laufzeit h", "{:.1f}"),
    ("zyklen", "Zyklen", "{}"),
    ("kurzlaeufe", "Kurzlaeufe", "{}"),
    ("zyklus_dauer_median", "Median min", "{:.0f}"),
    ("anti_block_h", "Antiz.-Sperre h", "{:.2f}"),
    ("min_unten_global", "min unten", "{:.1f}"),
    ("stunden_unten_unter_35", "h unten<35", "{}"),
    ("min_mitte_global", "min mitte", "{:.1f}"),
    ("boiler_max_ereignisse", "Boiler-Max", "{}"),
    # Direktmass fuer die Frage "Startet die WP nach Sonnenuntergang?":
    # Netzenergie aller Laeufe, die erst nach der Sonnenuntergangs-Stunde
    # begonnen haben (Tabelle SONNENUNTERGANG_H).
    ("wp_netz_nach_su_kwh", "Netz n. SU kWh", "{:.1f}"),
    ("wp_nach_su_prozent", "Anteil n. SU %", "{:.0f}"),
]


def kpi_start_nach_sonnenuntergang(sim) -> dict:
    """WP-Energie der Laeufe, die nach Sonnenuntergang begonnen haben.

    Antwort auf die Frage, ob der Spaetest-Start im Winter aus dem
    Tageslicht faellt: gezielt die Netzkilowattstunden dieser Laeufe (und ihr
    Anteil an der Energie dieser Laeufe). Bei Sonnenuntergang ist die
    PV-Erzeugung im Modell null - Netzenergie dort ist also "verlorene PV".
    """
    netz_wh = pv_wh = 0.0
    je_regel: Dict[str, float] = {}
    for z in sim.zyklen:
        start = getattr(z, "start", None)
        stunde = getattr(start, "hour", None)
        monat = getattr(start, "month", None)
        if stunde is None or monat is None:
            continue
        if stunde + getattr(start, "minute", 0) / 60.0 >= SONNENUNTERGANG_H.get(monat, 18.0):
            netz_w = float(getattr(z, "netz_wh", 0.0) or 0.0)
            netz_wh += netz_w
            pv_wh += float(getattr(z, "pv_wh", 0.0) or 0.0)
            # Welche Regel startet die Laeufe nach Sonnenuntergang? Das
            # entscheidet, WO ein PV-Fenster-Gate hingehoert.
            name = str(getattr(z, "regel", None) or "-")
            je_regel[name] = je_regel.get(name, 0.0) + netz_w / 1000.0
    gesamt = netz_wh + pv_wh
    return {
        "wp_netz_nach_su_kwh": round(netz_wh / 1000.0, 3),
        "wp_nach_su_prozent": (netz_wh / gesamt * 100.0) if gesamt > 1 else 0.0,
        "_nach_su_regeln": {k: round(v, 3) for k, v in je_regel.items()},
    }


def kennzahlen_erweitert(sim) -> dict:
    """Kennzahlen aus varianten.kennzahlen + Antizipations-Sperrstunden."""
    aus = kennzahlen(sim)
    aus["anti_block_h"] = round(
        sum(h for grund, h in sim.diag_blockiert_h.items()
            if grund.startswith("Start-Antizipation")), 2)
    zaehler: Dict[str, int] = {}
    for z in sim.zyklen:
        name = z.regel or "-"
        zaehler[name] = zaehler.get(name, 0) + 1
    aus["laeufe_je_regel"] = dict(sorted(zaehler.items(), key=lambda kv: -kv[1]))
    aus.update(kpi_start_nach_sonnenuntergang(sim))
    return aus


async def main_async(args) -> int:
    parameter = lade_parameter()
    pcl, le_mod, _el, _lp = lade_steuerung()
    szenarien = args.szenarien or ["september", "winter_misch"]
    varianten = args.varianten or list(KANDIDATEN)
    ergebnisse: Dict[str, Dict[str, dict]] = {}
    t0 = time.time()

    for vname in varianten:
        _beschreibung, patcher, patch = KANDIDATEN[vname]
        ruecknahmen = [p(pcl, le_mod) for p in patcher]
        ergebnisse[vname] = {}
        try:
            for sname in szenarien:
                sz = SZENARIEN[sname]
                if args.tage:
                    sz = type(sz)(**{**sz.__dict__, "tage": args.tage})
                lernpfad = os.path.join(
                    tempfile.gettempdir(), f"wpsim_kand_{vname}_{sname}.json")
                if os.path.exists(lernpfad):
                    os.remove(lernpfad)
                sim = Simulation(sz, parameter=parameter, lernpfad=lernpfad,
                                 schritt_sekunden=args.schritt,
                                 config_patch=patch)
                await sim.simuliere()
                ergebnisse[vname][sname] = kennzahlen_erweitert(sim)
        finally:
            for rueck in reversed(ruecknahmen):
                rueck()

    linie = "=" * 112
    tage = args.tage or SZENARIEN[szenarien[0]].tage
    print(linie)
    print(f"Kandidatenvergleich Start-Antizipation  ({tage} Tage, Schritt {args.schritt:g} s)")
    print(linie)
    for sname in szenarien:
        print(f"\n### Szenario: {SZENARIEN[sname].name}\n")
        kopf = f"{'Kandidat':<16}" + "".join(f"{lbl:>17}" for _, lbl, _ in KPI_ZEILEN)
        print(kopf)
        print("-" * len(kopf))
        for vname in varianten:
            k = ergebnisse[vname][sname]
            zeile = f"{vname:<16}"
            for key, _lbl, fmt in KPI_ZEILEN:
                zeile += f"{fmt.format(k.get(key, 0)):>17}"
            print(zeile)
        print()
        basis = ergebnisse["basis"][sname]
        regeln = basis.get("regel_minuten") or {}
        if regeln:
            print("  Gewinner-Regeln (Basis, Minuten): "
                  + ", ".join(f"{n}={s}" for n, s in regeln.items()))
        laeufe = basis.get("laeufe_je_regel") or {}
        print("  Laeufe je ausloesender Regel (Basis): "
              + ", ".join(f"{n}={c}" for n, c in laeufe.items()))
        su_regeln = basis.get("_nach_su_regeln") or {}
        if su_regeln:
            print("  Laeufe nach Sonnenuntergang je Regel (Netz-kWh): "
                  + ", ".join(f"{n}={v:.1f}"
                              for n, v in sorted(su_regeln.items(),
                                                 key=lambda kv: -kv[1])))
        print()
        for vname in varianten:
            if vname == "basis":
                continue
            k = ergebnisse[vname][sname]
            print(f"  {vname}: {KANDIDATEN[vname][0]}")
            print(f"     Netz/Tag {k['netz_pro_tag_kwh'] - basis['netz_pro_tag_kwh']:+.2f} kWh | "
                  f"vergebliche PV {k['pv_vergeblich_kwh'] - basis['pv_vergeblich_kwh']:+.1f} kWh | "
                  f"Kurzlaeufe {k['kurzlaeufe'] - basis['kurzlaeufe']:+d} | "
                  f"Antiz.-Sperre {k['anti_block_h'] - basis['anti_block_h']:+.2f} h | "
                  f"Zyklen {k['zyklen'] - basis['zyklen']:+d} | "
                  f"min unten {k['min_unten_global'] - basis['min_unten_global']:+.1f} K")
    print(f"\n{linie}")
    print(f"Rechenzeit: {time.time() - t0:.0f} s")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kandidatenvergleich Start-Antizipation")
    ap.add_argument("--tage", type=int, default=14)
    ap.add_argument("--schritt", type=float, default=60.0)
    ap.add_argument("--szenarien", nargs="*", default=None)
    ap.add_argument("--varianten", nargs="*", default=None)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
