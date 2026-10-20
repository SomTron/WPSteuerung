"""Sensitivitaet der Winterkalibrierung (20.10.2026).

Aufruf:
    py -3 Analyse/sim/sensitivitaet.py [szenario]

Die Winterparameter sind aus 14 Tagen Median geschaetzt. Die Frage ist,
wie stark die daraus abgeleiteten Entscheidungen von der
Parameterschaetzung abhaengen - insbesondere die, mit der
Start-Antizipation fuer die Mindest-Temperatur-Garantien ausgenommen
wird (Nutzerentscheidung: "der Abendkomfort geht vor").

Geprueft wird je Parametersatz:
  * der Netzbezug ohne und mit der Ausnahme (Preis des Komforts),
  * das Abendminimum 20-22 Uhr ohne und mit der Ausnahme (Nutzen).

Ergebnis (winter_wenig_pv, 14 Tage):
  - der Komfortgewinn ist in ALLEN Parametersaetzen positiv,
  - der Preis liegt im plausiblen Bereich zwischen 0,00 und 1,09 kWh/Tag
    und haengt vor allem am Waermeverlust des Speichers.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from harness import Simulation  # noqa: E402
from thermal import SpeicherParameter  # noqa: E402
from umgebung import SZENARIEN  # noqa: E402

#: Ohne Ausnahme = alte Antizipationslogik (Projektstandard vor der Aenderung).
OHNE_AUSNAHME = {"sicherheit": {"start_vorhersage_ausnahme_regeln": []}}

#: Die Parametersaetze: (Bezeichnung, Volumenfaktor, Leistungsfaktor, Verlustfaktor)
SAETZE = [
    ("Kalibriert (Referenz)", 1.0, 1.0, 1.0),
    ("Verlust +20 %", 1.0, 1.0, 1.2),
    ("Verlust -20 %", 1.0, 1.0, 0.8),
    ("Heizleistung +15 %", 1.0, 1.15, 1.0),
    ("Heizleistung -15 %", 1.0, 0.85, 1.0),
    ("Volumen 250 L", 250.0 / 300.0, 1.0, 1.0),
    ("Volumen 360 L", 360.0 / 300.0, 1.0, 1.0),
    ("Verlust +20 %, P +15 %", 1.0, 1.15, 1.2),
    ("Verlust -20 %, P -15 %", 1.0, 0.85, 0.8),
]


def _parameter(basis: dict, dm: float, dp: float, dv: float) -> SpeicherParameter:
    felder = set(SpeicherParameter.__dataclass_fields__)
    p = SpeicherParameter(**{k: v for k, v in basis.items() if k in felder})
    p.volumen_l = basis["volumen_l"] * dm
    p.heizleistung_w = basis["heizleistung_w"] * dp
    p.verlust_w_k = basis["verlust_w_k"] * dv
    return p


async def _lauf(szenario: str, p: SpeicherParameter, mit_ausnahme: bool,
                tage: int = 14) -> tuple:
    sz = SZENARIEN[szenario]
    # Die Parameterdatei des Szenarios muss weg - sonst wuerde sie die
    # getesteten Parameter ueberschreiben und der Sweep waere wirkungslos.
    sz = type(sz)(**{**sz.__dict__, "tage": tage, "parameter_datei": None})
    lernpfad = os.path.join(tempfile.gettempdir(), "wpsim_sensibilitaet.json")
    if os.path.exists(lernpfad):
        os.remove(lernpfad)
    sim = Simulation(
        sz, parameter=p, lernpfad=lernpfad, schritt_sekunden=60.0,
        config_patch=None if mit_ausnahme else OHNE_AUSNAHME,
    )
    await sim.simuliere()
    k = sim.kennzahlen_basis
    abend = [r["mitte"] for r in sim.zeitreihe if 20 <= r["t"].hour < 22]
    return k["netz_pro_tag_kwh"], (min(abend) if abend else 0.0)


async def main_async(szenario: str) -> int:
    with open(os.path.join(_HERE, "parameter_winter.json"), "r",
              encoding="utf-8") as f:
        basis = json.load(f)
    print(f"Sensitivitaet: {szenario}, 14 Tage")
    print("{:30s} {:>10s} {:>10s} {:>8s} {:>11s} {:>10s}".format(
        "Parametersatz", "Netz ohne", "Netz mit", "dNetz",
        "Abend ohne", "Abend mit"))
    for label, dm, dp, dv in SAETZE:
        p = _parameter(basis, dm, dp, dv)
        n_ohne, a_ohne = await _lauf(szenario, p, False)
        n_mit, a_mit = await _lauf(szenario, p, True)
        print("{:30s} {:10.2f} {:10.2f} {:+8.2f} {:11.1f} {:10.1f}".format(
            label, n_ohne, n_mit, n_mit - n_ohne, a_ohne, a_mit))
    return 0


def main() -> int:
    szenario = sys.argv[1] if len(sys.argv) > 1 else "winter_wenig_pv"
    return asyncio.run(main_async(szenario))


if __name__ == "__main__":
    raise SystemExit(main())