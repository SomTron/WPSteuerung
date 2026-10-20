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
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from thermal import Speicher, SpeicherParameter  # noqa: E402

WURZEL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSV = os.path.join(WURZEL, "logs", "24.9", "heizungsdaten.csv")
#: Winterdaten: derselbe Messpunkt, aber aus Analyse/merged_data.csv
#: (Dezember 2025 + Januar 2026, vollstaendige Tagesabdeckung).
CSV_WINTER = os.path.join(WURZEL, "Analyse", "merged_data.csv")
#: Monate, die als Winterdatensatz gelten (Dezember 2025 und Januar 2026).
WINTER_MONATE = ("12", "01")

#: Ausgabedatei je Datenquelle.
PARAMETER_DATEI = {
    "september": "parameter.json",
    "winter": "parameter_winter.json",
}


def lade_realdaten(pfad: str = CSV, monate=None):
    """Liest die Roh-CSV robust (die Logdatei enthaelt Null-Byte-Padding).

    ``monate`` filtert auf Monate (z. B. ``("12", "01")`` fuer den Winter).
    Das ``Kompressor``-Flag wird in beiden Kodierungen gelesen: ``0``/``1``
    im September-Log, ``AUS``/``EIN`` im Winterdatensatz.
    """
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
            if monate and t.strftime("%m") not in monate:
                continue

            def zahl(k):
                v = re.sub(r"\x00+", "", (r.get(k) or "")).strip()
                try:
                    return float(v)
                except ValueError:
                    return None

            k = zahl("Kompressor")
            if k is None:
                # Kodierung 'AUS'/'EIN' (Winterdatensatz)
                roh = re.sub(r"\x00+", "", (r.get("Kompressor") or "")).strip().upper()
                k = 1.0 if roh in ("1", "EIN", "AN") else 0.0

            zeilen.append(dict(
                t=t, oben=zahl("T_Oben"), mitte=zahl("T_Mittig"),
                unten=zahl("T_Unten"), k=k,
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


def tages_replay(zeilen, p: SpeicherParameter):
    """Nachfaehrt einen ganzen Tag mit dem gemessenen Verlauf.

    Der Tagesverlauf ist das entscheidende Kriterium fuer den Winter:
    der Speicher steht am Morgen kalt (Dezember real ~13 C) und wird
    tagsueber auf ~42 C geheizt. Der Zyklus-fuer-Zyklus-Nachweis deckt
    genau diese Luecke nicht ab - zwischen zwei Laeufen liegen Stunden,
    in denen der Speicher auskuehlt.

    Rueckgabe: Fehler je Sensor (Bias, Streuung) und die Extremwerte des
    Modells (Minimum am Morgen, Maximum am Nachmittag).
    """
    speicher = Speicher(p)
    fehler = collections.defaultdict(list)
    if not zeilen or zeilen[0]["unten"] is None:
        return None
    speicher.setze_temperaturen(
        zeilen[0]["unten"], zeilen[0]["mitte"] or zeilen[0]["unten"],
        zeilen[0]["oben"] or zeilen[0]["unten"],
    )
    tageskurve = []
    for i in range(1, len(zeilen)):
        a, b = zeilen[i - 1], zeilen[i]
        dt = (b["t"] - a["t"]).total_seconds()
        if dt <= 0 or dt > 900:
            continue
        # Zapfung: der untere Fuehler faellt RELATIV zu den anderen. Ein
        # Abfall aller Fuehler (der Speicher kuehlt aus) ist keine Zapfung -
        # im Winter faellt der Speicher nach dem Abschalten der WP um
        # ueber 10 K in einer Stunde. Ohne diese Unterscheidung schuettet
        # das Modell Kaltwasser in den Speicher und kühlt ihn kuenstlich
        # auf 13 C - genau der Effekt, den die Kalibrierung nicht finden
        # kann.
        if (a["k"] or 0) < 0.5 and a["unten"] is not None and b["unten"] is not None:
            d_unten = a["unten"] - b["unten"]
            d_mitte = (a["mitte"] - b["mitte"]) if (a["mitte"] is not None and b["mitte"] is not None) else 0.0
            if d_unten - d_mitte > 1.0:
                speicher.zapfe((d_unten - d_mitte) * 0.8)
        speicher.schritt(dt, bool(a["k"] and a["k"] > 0.5))
        for name in ("unten", "mitte", "oben"):
            soll = b[name]
            if soll is not None:
                fehler[name].append(getattr(speicher, name).temperatur - soll)
        tageskurve.append((b["t"], b["k"], speicher.unten.temperatur))
    if not fehler:
        return None
    morgen = [x[2] for x in tageskurve if x[0].hour < 9]
    nachmittag = [x[2] for x in tageskurve if 12 <= x[0].hour < 17]
    return {
        "fehler": {k: (st.mean(v), st.pstdev(v)) for k, v in fehler.items() if v},
        "min_frueh": min(morgen) if morgen else None,
        "max_mittag": max(nachmittag) if nachmittag else None,
    }


def guete_tag(tage, p: SpeicherParameter, ziel: dict) -> float:
    """Zielfunktion: Tagesverlauf nachfahren + Raten als Randbedingung.

    ``tage`` ist eine Liste von Tages-Serien (je Tag eine Liste aus
    ``_fenster``).
    """
    quad = []
    fehler_min = []
    fehler_max = []
    for ser in tage:
        r = tages_replay(ser, p)
        if not r:
            continue
        for name, (bias, sigma) in r["fehler"].items():
            quad.append(bias * bias)
            quad.append(sigma * sigma)
        if r["min_frueh"] is not None and ziel.get("min_frueh") is not None:
            fehler_min.append(r["min_frueh"] - ziel["min_frueh"])
        if r["max_mittag"] is not None and ziel.get("max_mittag") is not None:
            fehler_max.append(r["max_mittag"] - ziel["max_mittag"])
    if not quad:
        return 1e9
    replay = math.sqrt(sum(quad) / len(quad))
    m = modellraten(p)
    # Im Winter sind die September-Ratenziele NICHT anwendbar:
    #  - die Heizrate wird dort an einem gleichmaessig warmen Speicher
    #    gemessen, im Winter startet die Heizung an einem KALTEN Speicher
    #    (13 C) - das sind voellig verschiedene Situationen;
    #  - die Schichtung im Leerlauf entsteht aus der Historie des
    #    Tagesverlaufs, nicht aus einer 150-min-Modellphase.
    # Deshalb optimiert der Winter-Fit ausschliesslich ueber den
    # Tagesverlauf (inkl. Morgenminimum und Nachmittagsmaximum).
    if ziel.get("nur_tagesverlauf"):
        strafen = 0.0
    else:
        strafen = (
            ((m["heiz"] - ziel["heiz"]) / 1.5) ** 2
            + ((m["kuehl"] - ziel["kuehl"]) / 0.15) ** 2
            + ((m["mitte_an"] - ziel["mitte_an"]) / 2.0) ** 2
            + ((m["oben_an"] - ziel["oben_an"]) / 3.0) ** 2
        )
    extra = 0.0
    if fehler_min:
        extra += (st.mean(fehler_min) / 2.0) ** 2
    if fehler_max:
        extra += (st.mean(fehler_max) / 3.0) ** 2
    return replay + 0.8 * math.sqrt(strafen) + extra


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


def nacht_replay(zeilen, p: SpeicherParameter):
    """Nachfaehrt die Nachtphase (WP aus): Abkuehlen des Speichers.

    Der Winter liefert das entscheidende, sauber abgrenzbare Teilstueck:
    der Speicher steht abends bei ~42 C und ist am Morgen auf ~13 C
    abgekuehlt. Der Weg dazwischen bestimmt die Verlustparameter - ohne
    ihn behauptet das Modell im Winter einen warmen Speicher und die WP
    laeuft die halbe Zeit nicht.
    """
    speicher = Speicher(p)
    fehler = collections.defaultdict(list)
    if not zeilen or zeilen[0]["unten"] is None:
        return None
    speicher.setze_temperaturen(
        zeilen[0]["unten"], zeilen[0]["mitte"] or zeilen[0]["unten"],
        zeilen[0]["oben"] or zeilen[0]["unten"],
    )
    for i in range(1, len(zeilen)):
        a, b = zeilen[i - 1], zeilen[i]
        dt = (b["t"] - a["t"]).total_seconds()
        if dt <= 0 or dt > 900:
            continue
        speicher.schritt(dt, False)
        for name in ("unten", "mitte", "oben"):
            soll = b[name]
            if soll is not None:
                fehler[name].append(getattr(speicher, name).temperatur - soll)
    if not fehler:
        return None
    return {k: (st.mean(v), st.pstdev(v)) for k, v in fehler.items() if v}


def guete_nacht(nachten, p: SpeicherParameter) -> float:
    """Zielfunktion fuer die Verlustparameter (nur Nachtphase)."""
    quad = []
    for ser in nachten:
        r = nacht_replay(ser, p)
        if not r:
            continue
        for name, (bias, sigma) in r.items():
            quad.append(bias * bias)
            quad.append(sigma * sigma)
    if not quad:
        return 1e9
    return math.sqrt(sum(quad) / len(quad))


def fitze_parameter_nacht(nachten, start: SpeicherParameter,
                          runden=10) -> SpeicherParameter:
    """Koordinaten-Suche nur fuer die Verlust-/Kopplungsparameter."""
    felder = [
        ("verlust_w_k", 1.30, 0.2, 60.0),
        ("konvektion_aus_w_k", 1.40, 0.05, 400.0),
        ("konvektion_oben_aus_w_k", 1.40, 0.02, 400.0),
        ("volumen_l", 1.12, 150.0, 1200.0),
    ]

    def _suche(p0):
        best = p0.kopie()
        best_q = guete_nacht(nachten, best)
        for _ in range(runden):
            verbessert = False
            for feld, faktor, minv, maxv in felder:
                for richtung in (faktor, 1.0 / faktor):
                    kand = best.kopie()
                    neu = min(max(getattr(kand, feld) * richtung, minv), maxv)
                    if abs(neu - getattr(best, feld)) < 1e-9:
                        continue
                    setattr(kand, feld, neu)
                    q = guete_nacht(nachten, kand)
                    if q < best_q - 1e-4:
                        best, best_q, verbessert = kand, q, True
            if not verbessert:
                break
        return best, best_q

    best, best_q = _suche(start)
    print(f"  Start A: Fehler={best_q:.3f}")
    for i in range(2):
        p0 = start.kopie()
        p0.verlust_w_k = 2.0 * (0.5 + 2.0 * ((i + 1) / 3.0))
        p0.volumen_l = 300.0 * (0.6 + 1.2 * ((i + 1) / 3.0))
        kand, q = _suche(p0)
        print(f"  Start {chr(66 + i)}: Fehler={q:.3f}")
        if q < best_q:
            best, best_q = kand, q
    print(f"  BEST Fehler={best_q:.3f}")
    print(f"    Verlust={best.verlust_w_k:.2f}W/K  Volumen={best.volumen_l:.0f}L  "
          f"kAus={best.konvektion_aus_w_k:.2f}  kObenAus={best.konvektion_oben_aus_w_k:.2f}")
    return best


def kalibriere_winter_kpi(zeilen, basis: SpeicherParameter, ziel: dict,
                          tage: int = 7):
    """Kalibriert die Winterparameter auf die GEMESSENEN Kennzahlen.

    Der Winter laesst sich nicht ueber die Raten kalibrieren (siehe
    ``fitze_parameter_tag``): der gemessene ``T_Unten`` verhaelt sich
    im Winter nicht wie ein passiver Speicherknoten - er faellt nach
    dem Abschalten der WP in ~1 h um 10-15 K, waehrend ``T_Mitte`` und
    ``T_Oben`` warm bleiben. Diese Bilanz ist mit einem
    Speichermodell nicht schliessbar (das Volumen, das den unteren
    Knoten auf 13 C abkuehlen laesst, muesste den oberen Knoten
    mitheizen).

    Kalibriert wird deshalb auf das, was am Ende zaehlt: die
    Simulations-Kennzahlen gegen die gemessenen Werte (Netzbezug je
    Tag, WP-Laufzeit je Tag, tiefste Temperatur).
    """
    import asyncio

    from harness import Simulation
    from umgebung import SZENARIEN

    ser = _fenster(zeilen)
    pro_tag = collections.OrderedDict()
    for s in ser:
        pro_tag.setdefault(s["t"].date(), []).append(s)
    tage_liste = [v for v in pro_tag.values() if len(v) >= 200]
    if not tage_liste:
        return None
    # Starttemperaturen: Median der gemessenen Werte um 00:00
    start = []
    for i in range(3):
        werte = [x[("unten", "mitte", "oben")[i]] for tag in tage_liste
                 for x in tag if x["t"].hour == 0 and x[("unten", "mitte", "oben")[i]] is not None]
        start.append(st.median(werte) if werte else (12.9, 32.4, 44.5)[i])
    print(f"  Starttemperaturen (Median 00:00): unten {start[0]:.1f} C, "
          f"mitte {start[1]:.1f} C, oben {start[2]:.1f} C")

    sz = SZENARIEN["winter_wenig_pv"]
    # Die Parameterdatei des Szenarios muss hier weg: das Harness laedt
    # sie sonst und wuerde die getesteten Parameter ueberschreiben - der
    # Sweep waere wirkungslos.
    sz = type(sz)(**{**sz.__dict__, "tage": tage, "parameter_datei": None})

    async def _bewerte(p):
        lernpfad = os.path.join(tempfile.gettempdir(), "wpsim_kalibrierung.json")
        if os.path.exists(lernpfad):
            os.remove(lernpfad)
        sim = Simulation(sz, parameter=p, lernpfad=lernpfad, schritt_sekunden=60.0)
        sim.speicher.setze_temperaturen(*start)
        await sim.simuliere()
        k = sim.kennzahlen_basis
        zr = sim.zeitreihe
        min_unten = min(r["unten"] for r in zr)
        min_oben = min(r["oben"] for r in zr)
        return {
            "netz": k["netz_pro_tag_kwh"],
            "wp_h": k["wp_laufzeit_h"] / tage,
            "min_unten": min_unten,
            "min_oben": min_oben,
        }

    best = None
    for p_w in (1900.0, 2100.0, 2300.0):
        for v_w in (4.5, 5.5, 6.5):
            for v_oben in (0.05, 0.10, 0.20):
                kand = basis.kopie()
                kand.heizleistung_w = p_w
                kand.verlust_w_k = v_w
                kand.verlust_oben_anteil = v_oben
                r = asyncio.run(_bewerte(kand))
                fehler = (
                    ((r["netz"] - ziel["netz"]) / 1.0) ** 2
                    + ((r["wp_h"] - ziel["wp_h"]) / 1.0) ** 2
                    + ((r["min_unten"] - ziel["min_unten"]) / 3.0) ** 2
                    + ((r["min_oben"] - ziel["min_oben"]) / 3.0) ** 2
                )
                if best is None or fehler < best[0]:
                    best = (fehler, kand, r)
    fehler, kand, r = best
    print(f"  BEST Fehler={fehler:.3f}  P={kand.heizleistung_w:.0f}W  "
          f"Verlust={kand.verlust_w_k:.2f}W/K  "
          f"VerlustOben={kand.verlust_oben_anteil:.2f}")
    print(f"    Netz {r['netz']:.2f} kWh/Tag (Ziel {ziel['netz']:.2f})  "
          f"WP {r['wp_h']:.1f} h/Tag (Ziel {ziel['wp_h']:.1f})")
    print(f"    min unten {r['min_unten']:.1f} C (Ziel {ziel['min_unten']:.1f})  "
          f"min oben {r['min_oben']:.1f} C (Ziel {ziel['min_oben']:.1f})")
    return kand, start


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


def fitze_parameter_tag(tage, start: SpeicherParameter, ziel: dict,
                        runden=8) -> SpeicherParameter:
    """Koordinaten-Suche gegen den Tagesverlauf (inkl. Speichervolumen)."""
    felder = [
        ("heizleistung_w", 1.20, 300.0, 6000.0),
        ("verlust_w_k", 1.35, 0.2, 80.0),
        ("konvektion_an_w_k", 1.40, 50.0, 30000.0),
        ("konvektion_oben_an_w_k", 1.40, 10.0, 15000.0),
        ("einspeisung_anteil_unten", 1.25, 0.0, 1.0),
        ("konvektion_aus_w_k", 1.35, 0.1, 300.0),
        ("konvektion_oben_aus_w_k", 1.35, 0.05, 300.0),
        ("volumen_l", 1.15, 150.0, 2000.0),
    ]

    def _suche(p0):
        best = p0.kopie()
        best_q = guete_tag(tage, best, ziel)
        for _ in range(runden):
            verbessert = False
            for feld, faktor, minv, maxv in felder:
                for richtung in (faktor, 1.0 / faktor):
                    kand = best.kopie()
                    neu = min(max(getattr(kand, feld) * richtung, minv), maxv)
                    if abs(neu - getattr(best, feld)) < 1e-9:
                        continue
                    setattr(kand, feld, neu)
                    q = guete_tag(tage, kand, ziel)
                    if q < best_q - 1e-4:
                        best, best_q, verbessert = kand, q, True
            if not verbessert:
                break
        return best, best_q

    best, best_q = _suche(start)
    print(f"  Start A: Fehler={best_q:.3f}")
    for i in range(2):
        p0 = start.kopie()
        p0.heizleistung_w = 800.0 * (0.6 + 0.8 * ((i + 1) / 3.0))
        p0.konvektion_an_w_k = 400.0 * (0.5 + 1.4 * ((i + 1) / 3.0))
        p0.volumen_l = 300.0 * (0.6 + 1.2 * ((i + 1) / 3.0))
        kand, q = _suche(p0)
        print(f"  Start {chr(66 + i)}: Fehler={q:.3f}")
        if q < best_q:
            best, best_q = kand, q
    m = modellraten(best)
    print(f"  BEST Fehler={best_q:.3f}")
    print(f"    P={best.heizleistung_w:.0f}W  Volumen={best.volumen_l:.0f}L  "
          f"Anteil unten={best.einspeisung_anteil_unten:.2f}")
    print(f"    kAn={best.konvektion_an_w_k:.0f}  kObenAn={best.konvektion_oben_an_w_k:.0f}")
    print(f"    Verlust={best.verlust_w_k:.2f}W/K  kAus={best.konvektion_aus_w_k:.2f}"
          f"  kObenAus={best.konvektion_oben_aus_w_k:.2f}")
    for k in ("heiz", "kuehl", "mitte_an", "mitte_aus", "oben_an", "oben_aus"):
        print(f"    {k:10s} Modell {m[k]:+7.2f}   Messung {ziel[k]:+7.2f}")
    return best


def main() -> int:
    quelle = sys.argv[1] if len(sys.argv) > 1 else "september"
    if quelle == "winter":
        zeilen = lade_realdaten(CSV_WINTER, monate=WINTER_MONATE)
        start = SpeicherParameter(umgebung_c=13.0)
        ausgabe = PARAMETER_DATEI["winter"]
    else:
        zeilen = lade_realdaten()
        start = SpeicherParameter()
        ausgabe = PARAMETER_DATEI["september"]
    if not zeilen:
        print("Keine Realdaten gefunden.")
        return 1
    print(f"Datenquelle: {quelle}  ({len(zeilen)} Zeilen, "
          f"{zeilen[0]['t']} .. {zeilen[-1]['t']})\n")

    laeufe = finde_laeufe(zeilen, min_minuten=25)
    dauer = [x[2] for x in laeufe]
    if not laeufe:
        print("Keine Kompressorlaeufe >= 25 min gefunden.")
        return 1
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
    if quelle == "winter":
        # Der Winter laesst sich nicht ueber die September-Raten kalibrieren
        # (siehe kalibriere_winter_kpi). Stattdessen: die Speicher-
        # parameter so waehlen, dass die SIMULATION die gemessenen
        # Winter-Kennzahlen trifft (Netzbezug, WP-Laufzeit, Tiefpunkt).
        ziel_kpi = {
            "netz": 9.4,        # kWh/Tag (Dezember 2025, gemessen)
            "wp_h": 5.2,         # h/Tag (gemessen)
            "min_unten": 13.0,   # C (gemessen)
            "min_oben": 42.4,    # C (gemessen: der Deckel bleibt warm)
        }
        print("  Zielwerte (Dezember 2025): Netz 9,4 kWh/Tag, WP 5,2 h/Tag, "
              "min unten 13 C, min oben 42,4 C")
        basis = start.kopie()
        basis.umgebung_c = 13.0           # Keller (unten und mitte)
        basis.umgebung_oben_c = 20.0      # Aufstellraum (Deckel, oberer Fuehler)
        basis.konvektion_an_w_k = 100.0    # Durchmischung beim Heizen
        basis.konvektion_oben_an_w_k = 50.0
        basis.konvektion_aus_w_k = 0.4      # stabile Schichtung im Leerlauf
        basis.konvektion_oben_aus_w_k = 2.0
        basis.einspeisung_anteil_unten = 0.6
        basis.volumen_l = 300.0
        basis.waerme_verteilt = True       # der WP-Tauscher durchmischt den Speicher
        basis.einspeisung_anteil_oben = 0.30
        ergebnis = kalibriere_winter_kpi(zeilen, basis, ziel_kpi)
        if not ergebnis:
            print("Kalibrierung fehlgeschlagen.")
            return 1
        p, start_temperaturen = ergebnis
        pfad_start = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "start_temperaturen_winter.json")
        with open(pfad_start, "w", encoding="utf-8") as f:
            json.dump({"unten": start_temperaturen[0],
                       "mitte": start_temperaturen[1],
                       "oben": start_temperaturen[2]}, f, indent=2)
    else:
        fit_laeufe = sorted(laeufe, key=lambda x: x[2])[-18:]
        p = fitze_parameter(zeilen, fit_laeufe, start, ziel)

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

    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), ausgabe)
    with open(pfad, "w", encoding="utf-8") as f:
        json.dump(p.__dict__, f, indent=2)
    print(f"\n-> {pfad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
