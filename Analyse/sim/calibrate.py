"""Kalibriert das Speichermodell gegen logs/24.9 und validiert es.

Aufruf:  py -3 Analyse/sim/calibrate.py

Zielfunktion ist das *Nachfahren echter Zyklen* (das Modell muss die
gemessenen Temperaturverlaeufe aus den tatsaechlichen Starttemperaturen und
mit den tatsaechlichen Zapfungen reproduzieren). Ergaenzend werden die
gemessenen Heiz-/Kuehlraten und die Schichtung als Randbedingungen
gewichtet, damit das Modell nicht in ein physikalisch unmoegliches
Minimum laeuft.

Gewichtete Ziele (aus dem September-Log, 15-min-Fenster):
    Heizrate unten (AN)          +8.0 K/h
    Kuehlrate unten (AUS)        -0.42 K/h
    Schichtung mitte-unten AN    +0.36 K   (Durchmischung)
    Schichtung mitte-unten AUS   +15.8 K   (stabile Schichtung)
    Schichtung oben-unten AN     +1.9 K
    Schichtung oben-unten AUS    +21.2 K
"""
from __future__ import annotations

import collections
import csv as _csv
import json
import math
import os
import re
import statistics as st
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from thermal import Speicher, SpeicherParameter  # noqa: E402

WURZEL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSV = os.path.join(WURZEL, "logs", "24.9", "heizungsdaten.csv")


def lade_realdaten(pfad: str = CSV):
    """Liest die Roh-CSV robust (die Logdatei enthaelt Null-Byte-Padding)."""
    zeilen = []
    with open(pfad, "r", encoding="utf-8", errors="replace", newline="") as f:
        for r in _csv.DictReader(f):
            ts = re.sub(r"\x00+", "", (r.get("Zeitstempel") or "")).strip()
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue

            def zahl(k):
                v = re.sub(r"\x00+", "", (r.get(k) or "")).strip()
                try:
                    return float(v)
                except ValueError:
                    return None

            zeilen.append(dict(
                t=t, oben=zahl("T_Oben"), mitte=zahl("T_Mittig"),
                unten=zahl("T_Unten"), k=zahl("Kompressor"),
            ))
    zeilen.sort(key=lambda r: r["t"])
    return zeilen


def _fenster(zeilen, minuten=5):
    """Mittelt die Rohzeitreihe in ``minuten``-Fenster."""
    out = collections.OrderedDict()
    for r in zeilen:
        k = r["t"].replace(second=0, microsecond=0)
        k = k.replace(minute=(k.minute // minuten) * minuten)
        out.setdefault(k, []).append(r)
    ser = []
    for k, c in out.items():
        def m(f):
            v = [x[f] for x in c if x[f] is not None]
            return sum(v) / len(v) if v else None
        ser.append(dict(
            t=k, k=sum(1 for x in c if (x["k"] or 0) > 0.5) / len(c),
            unten=m("unten"), mitte=m("mitte"), oben=m("oben"),
        ))
    return ser


def zielraten(zeilen):
    """Messraten aus dem Log, an denen das Modell kalibriert wird.

    Das sind genau die Groessen, auf die die Regelung reagiert: die Heizrate
    des unteren Fuehlers, die Abkuehlrate im Leerlauf und die Schichtung
    (beim Heizen Durchmischung, im Leerlauf stabile Schichtung).
    """
    ser = _fenster(zeilen)
    namen = ("heiz", "kuehl", "mitte_an", "mitte_aus", "oben_an", "oben_aus")
    sammel = {n: [] for n in namen}
    for i in range(len(ser) - 3):
        a, b = ser[i], ser[i + 3]
        dt = (b["t"] - a["t"]).total_seconds() / 3600
        if dt <= 0 or a["unten"] is None or b["unten"] is None:
            continue
        an = (a["k"] or 0) > 0.5 and (b["k"] or 0) > 0.5
        aus = (a["k"] or 0) < 0.5 and (b["k"] or 0) < 0.5
        d_unten = (b["unten"] - a["unten"]) / dt
        if an and d_unten > 0.2:        # echter Heizanstieg, kein Zapf
            sammel["heiz"].append(d_unten)
        elif aus and -1.5 < d_unten < 0:   # ruhige Kuehlung, kein Zapf
            sammel["kuehl"].append(d_unten)
        if an or aus:
            if a["mitte"] is not None and b["mitte"] is not None:
                sammel["mitte_an" if an else "mitte_aus"].append(b["mitte"] - b["unten"])
            if a["oben"] is not None and b["oben"] is not None:
                sammel["oben_an" if an else "oben_aus"].append(b["oben"] - b["unten"])
    return {n: (st.median(v) if v else 0.0) for n, v in sammel.items()}


def modellraten(p: SpeicherParameter):
    """Dieselben Kennzahlen aus dem Modell (reine Heiz-/Kuehlphase, ohne Zapf)."""
    s = Speicher(p, start_unten=41.0, start_mitte=41.0, start_oben=42.0)
    dt = 60.0
    h = dt / 3600.0
    heiz, mitte_an, oben_an = [], [], []
    for _ in range(150):          # 150 min Heizen
        vorher = s.unten.temperatur
        s.schritt(dt, True)
        heiz.append((s.unten.temperatur - vorher) / h)
        mitte_an.append(s.mitte.temperatur - s.unten.temperatur)
        oben_an.append(s.oben.temperatur - s.unten.temperatur)
    kuehl, mitte_aus, oben_aus = [], [], []
    for _ in range(150):          # 150 min Leerlauf
        vorher = s.unten.temperatur
        s.schritt(dt, False)
        kuehl.append((s.unten.temperatur - vorher) / h)
        mitte_aus.append(s.mitte.temperatur - s.unten.temperatur)
        oben_aus.append(s.oben.temperatur - s.unten.temperatur)
    n = 40
    return dict(
        heiz=st.median(heiz[-n:]), kuehl=st.median(kuehl[-n:]),
        mitte_an=st.median(mitte_an[-n:]), mitte_aus=st.median(mitte_aus[-n:]),
        oben_an=st.median(oben_an[-n:]), oben_aus=st.median(oben_aus[-n:]),
    )


def validiere(zeilen, t_start, t_ende, p: SpeicherParameter):
    """Faellt das Modell mit dem GEMESSENEN Kompressor-/Zapf-Verlauf nach."""
    speicher = Speicher(p)
    fehler = collections.defaultdict(list)
    zyklus = [r for r in zeilen if t_start <= r["t"] <= t_ende]
    if not zyklus:
        return None
    speicher.setze_temperaturen(
        zyklus[0]["unten"], zyklus[0]["mitte"], zyklus[0]["oben"]
    )
    for i in range(1, len(zyklus)):
        a, b = zyklus[i - 1], zyklus[i]
        dt = (b["t"] - a["t"]).total_seconds()
        if dt <= 0 or dt > 900:
            continue
        # Zapfung: Kuelldrop des unteren Fuehlers ueber 0.5 K im Fenster
        if (a["k"] or 0) < 0.5 and a["unten"] is not None and b["unten"] is not None:
            drop = a["unten"] - b["unten"]
            if drop > 0.5:
                speicher.zapfe(drop * 0.8)
        speicher.schritt(dt, bool(a["k"] and a["k"] > 0.5))
        for name in ("unten", "mitte", "oben"):
            soll = b[name]
            if soll is not None:
                fehler[name].append(getattr(speicher, name).temperatur - soll)
    return {k: (st.mean(v), st.pstdev(v)) for k, v in fehler.items() if v}


def finde_laeufe(zeilen, min_minuten=25):
    """Kompressorlaeufe aus der Zeitreihe (5-min-Fenster)."""
    ser = _fenster(zeilen)
    laeufe = []
    start = None
    for s in ser:
        an = (s["k"] or 0) > 0.5
        if an and start is None:
            start = s["t"]
        elif not an and start is not None:
            laeufe.append((start, s["t"], (s["t"] - start).total_seconds() / 60))
            start = None
    return [x for x in laeufe if x[2] >= min_minuten]


def guete(zeilen, laeufe, p: SpeicherParameter, ziel: dict) -> float:
    """Nachfahren echter Zyklen + physikalische Randbedingungen als Strafe."""
    quad = []
    for a, b, _ in laeufe:
        r = validiere(zeilen, a, b, p)
        if not r:
            continue
        for name, (bias, sigma) in r.items():
            quad.append(bias * bias)
            quad.append(sigma * sigma)
    if not quad:
        return 1e9
    replay = math.sqrt(sum(quad) / len(quad))

    m = modellraten(p)
    # Toleranzen: Heiz-/Kuehlrate eng, Schichtung mittel.
    strafen = (
        ((m["heiz"] - ziel["heiz"]) / 1.5) ** 2
        + ((m["kuehl"] - ziel["kuehl"]) / 0.15) ** 2
        + ((m["mitte_an"] - ziel["mitte_an"]) / 2.0) ** 2
        + ((m["oben_an"] - ziel["oben_an"]) / 3.0) ** 2
    )
    return replay + 0.8 * math.sqrt(strafen)


def fitze_parameter(zeilen, laeufe, start: SpeicherParameter, ziel: dict,
                    runden=8) -> SpeicherParameter:
    """Koordinaten-Suche mit mehreren Startpunkten."""
    felder = [
        ("heizleistung_w", 1.20, 700.0, 5000.0),
        ("verlust_w_k", 1.45, 0.2, 15.0),
        ("konvektion_an_w_k", 1.50, 100.0, 30000.0),
        ("konvektion_oben_an_w_k", 1.50, 20.0, 15000.0),
        ("einspeisung_anteil_unten", 1.25, 0.0, 1.0),
        ("konvektion_aus_w_k", 1.45, 0.1, 150.0),
        ("konvektion_oben_aus_w_k", 1.45, 0.05, 150.0),
    ]

    def _suche(p0):
        best = p0.kopie()
        best_q = guete(zeilen, laeufe, best, ziel)
        for _ in range(runden):
            verbessert = False
            for feld, faktor, minv, maxv in felder:
                for richtung in (faktor, 1.0 / faktor):
                    kand = best.kopie()
                    neu = min(max(getattr(kand, feld) * richtung, minv), maxv)
                    if abs(neu - getattr(kand, feld)) < 1e-9:
                        continue
                    setattr(kand, feld, neu)
                    q = guete(zeilen, laeufe, kand, ziel)
                    if q < best_q - 1e-4:
                        best, best_q, verbessert = kand, q, True
            if not verbessert:
                break
        return best, best_q

    best, best_q = _suche(start)
    print(f"  Start A: Fehler={best_q:.3f}")
    for i in range(2):
        p0 = SpeicherParameter(
            heizleistung_w=start.heizleistung_w * (0.65 + 0.5 * ((i + 1) / 3.0)),
            konvektion_an_w_k=400.0 * (0.5 + 1.4 * ((i + 1) / 3.0)),
            konvektion_oben_an_w_k=150.0 * (0.5 + 1.4 * ((i + 1) / 3.0)),
            einspeisung_anteil_unten=0.55 + 0.15 * i,
        )
        kand, q = _suche(p0)
        print(f"  Start {chr(66 + i)}: Fehler={q:.3f}")
        if q < best_q:
            best, best_q = kand, q

    m = modellraten(best)
    print(f"  BEST Fehler={best_q:.3f}")
    print(f"    P={best.heizleistung_w:.0f}W  Anteil unten={best.einspeisung_anteil_unten:.2f}"
          f"  kAn={best.konvektion_an_w_k:.0f}  kObenAn={best.konvektion_oben_an_w_k:.0f}")
    print(f"    Verlust={best.verlust_w_k:.2f}W/K  kAus={best.konvektion_aus_w_k:.2f}"
          f"  kObenAus={best.konvektion_oben_aus_w_k:.2f}")
    for k in ("heiz", "kuehl", "mitte_an", "mitte_aus", "oben_an", "oben_aus"):
        print(f"    {k:10s} Modell {m[k]:+7.2f}   Messung {ziel[k]:+7.2f}")
    return best


def main() -> int:
    zeilen = lade_realdaten()
    if not zeilen:
        print("Keine Realdaten gefunden.")
        return 1
    print(f"Realdaten: {len(zeilen)} Zeilen, "
          f"{zeilen[0]['t']} .. {zeilen[-1]['t']}\n")

    laeufe = finde_laeufe(zeilen, min_minuten=25)
    dauer = [x[2] for x in laeufe]
    print(f"Kompressorlaeufe >= 25 min: {len(laeufe)}, "
          f"Median {st.median(dauer):.0f} min, max {max(dauer):.0f} min")

    ziel = zielraten(zeilen)
    print("\n=== Messraten aus dem Log (Zielwerte) ===")
    beschriftung = {
        "heiz": "Heizrate unten (AN)", "kuehl": "Kuehlrate unten (AUS)",
        "mitte_an": "Schichtung mitte-unten AN", "mitte_aus": "Schichtung mitte-unten AUS",
        "oben_an": "Schichtung oben-unten AN", "oben_aus": "Schichtung oben-unten AUS",
    }
    for k, txt in beschriftung.items():
        print(f"  {txt:<30} {ziel[k]:+7.2f}")

    print("\n=== Parameter-Fit ===")
    fit_laeufe = sorted(laeufe, key=lambda x: x[2])[-18:]
    p = fitze_parameter(zeilen, fit_laeufe, SpeicherParameter(), ziel)

    print("\n=== Gegenprobe: Nachfahren aller echten Zyklen (Modell minus Messung) ===")
    alle = collections.defaultdict(list)
    for a, b, _ in laeufe:
        r = validiere(zeilen, a, b, p)
        for name, (bias, sigma) in (r or {}).items():
            alle[name].append((bias, sigma))
    for name in ("unten", "mitte", "oben"):
        v = alle[name]
        print(f"  {name:6s}: Bias {st.mean([x[0] for x in v]):+6.2f} K, "
              f"Streuung {st.mean([x[1] for x in v]):5.2f} K ({len(v)} Zyklen)")

    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parameter.json")
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(p.__dict__, f, indent=2)
    print(f"\n-> {pfad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
