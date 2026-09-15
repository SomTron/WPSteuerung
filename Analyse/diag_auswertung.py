# -*- coding: utf-8 -*-
"""Auswertung eines Diagnose-Pakets (wp-manager: journal/kernel/system/logs).

Wertet die Rohdaten aus, die `logs/analyse_*/` nicht abdeckt:
  * heizungsdaten.csv       -> Zyklen, Kurzzyklen, Overshoot, verschenkte PV,
                               morgendlicher Netzbezug, Taktwechsel
  * entscheidungs_log.jsonl -> Regelverteilung, Stromquelle, AUS-Gruende
  * error.log               -> Fehlerprofil (zeitlich gefiltert!)
  * journal.txt             -> Restarts/SIGKILL/OOM auf systemd-Ebene
  * learning_data.json      -> Lernstand (Heizraten, Zapf-Fenster, Forecast)

Nur Standardbibliothek. Aufruf:
    python3 Analyse/diag_auswertung.py <verzeichnis-mit-den-dateien>
"""
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Schwellen analog Analyse/log_analyse.py
KURZZYKLUS_MIN = 20.0
OVERSHOOT_C = 48.5
PV_VERPASST_W = 500.0
NETZKAUF_W = -50.0
MORGEN = (6, 12)
TRENNER = "=" * 78
TAKT_SEK = 13          # CSV-Takt (Sekunden) fuer Minuten-Hochrechnung


def _f(wert, default=None):
    """CSV-Wert robust in float (deutsche und englische Notation)."""
    try:
        return float(str(wert).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default


def _ts(wert):
    try:
        return datetime.strptime(str(wert).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _an(r):
    return str(r.get("Kompressor", "")).strip() in ("1", "EIN", "ein", "True")


def zyklus_analyse(zeilen):
    """Findet Kompressor-Zyklen -> (zyklen, takt_wechsel, verschenkte_pv_wh)."""
    zyklen, lauf = [], None
    verpasste_pv_wh = 0.0
    takt_wechsel = 0
    letzte_t = None
    vorher_an = None

    for t, r in zeilen:
        an = _an(r)
        dt = (t - letzte_t).total_seconds() / 3600 if letzte_t else 0
        if dt > 1:            # Luecke (Neustart) nicht als Laufzeit zaehlen
            dt = 0
        unten = _f(r.get("T_Unten"))
        feedin = _f(r.get("FeedinPower"), 0.0)

        if an and lauf is None:
            lauf = {"start": t, "quelle": r.get("PowerSource") or "?",
                    "unten_start": unten, "unten_max": unten}
            if vorher_an is False:
                takt_wechsel += 1
        elif an and lauf is not None:
            if unten is not None:
                lauf["unten_max"] = max(lauf["unten_max"] or unten, unten)
        elif not an and lauf is not None:
            lauf["dauer_min"] = round((t - lauf["start"]).total_seconds() / 60, 1)
            zyklen.append(lauf)
            lauf = None

        if not an and feedin is not None and feedin >= PV_VERPASST_W:
            verpasste_pv_wh += feedin * dt

        letzte_t = t
        vorher_an = an

    return zyklen, takt_wechsel, verpasste_pv_wh


def analyse_csv(basis):
    pfad = os.path.join(basis, "heizungsdaten.csv")
    if not os.path.exists(pfad):
        return
    zeilen = []
    with open(pfad, "r", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            t = _ts(r.get("Zeitstempel"))
            if t:
                zeilen.append((t, r))
    zeilen.sort(key=lambda x: x[0])
    if not zeilen:
        return

    print(TRENNER)
    print(f"A) heizungsdaten.csv: {len(zeilen)} Zeilen")
    print(f"   Zeitraum: {zeilen[0][0]} .. {zeilen[-1][0]}")
    deltas = [(zeilen[i + 1][0] - zeilen[i][0]).total_seconds()
              for i in range(len(zeilen) - 1)]
    deltas = [d for d in deltas if 0 < d < 3600]
    if deltas:
        print(f"   Takt: Median {statistics.median(deltas):.0f} s, max {max(deltas):.0f} s")

    zyklen, takt_wechsel, pv_wh = zyklus_analyse(zeilen)
    if not zyklen:
        print("   Keine abgeschlossenen Zyklen gefunden.")
        return

    dauern = [z["dauer_min"] for z in zyklen]
    kurz = [z for z in zyklen if z["dauer_min"] < KURZZYKLUS_MIN]
    uebersch = [z for z in zyklen if (z["unten_max"] or 0) >= OVERSHOOT_C]
    print(f"\n   Zyklen: {len(zyklen)} | Median {statistics.median(dauern):.0f} min | "
          f"min {min(dauern):.0f} | max {max(dauern):.0f} | Summe {sum(dauern) / 60:.1f} h")
    print(f"   Kurzzyklen <{KURZZYKLUS_MIN:.0f} min: {len(kurz)} "
          f"({100 * len(kurz) / len(zyklen):.0f} %)")
    print(f"   Zyklen mit T_unten >= {OVERSHOOT_C} C: {len(uebersch)}")
    print(f"   Quellen beim Einschalten: {Counter(z['quelle'] for z in zyklen).most_common()}")
    print(f"   Verschenkte PV (Einspeisung >= {PV_VERPASST_W:.0f} W bei WP AUS): "
          f"{pv_wh / 1000:.1f} kWh")
    print(f"   Taktwechsel (AUS->EIN): {takt_wechsel}")
    un = [z["unten_start"] for z in zyklen if z["unten_start"] is not None]
    if un:
        print(f"   T_unten beim Einschalten: Median {statistics.median(un):.1f} C")

    print("\n   Pro Tag: Datum | Starts | Laufzeit min | Kurzzyklen | Overshoot | Quelle")
    pro_tag = defaultdict(lambda: {"n": 0, "min": 0.0, "kurz": 0, "over": 0, "q": Counter()})
    for z in zyklen:
        e = pro_tag[z["start"].date().isoformat()]
        e["n"] += 1
        e["min"] += z["dauer_min"]
        e["kurz"] += 1 if z["dauer_min"] < KURZZYKLUS_MIN else 0
        e["over"] += 1 if (z["unten_max"] or 0) >= OVERSHOOT_C else 0
        e["q"][z["quelle"]] += 1
    for d in sorted(pro_tag):
        e = pro_tag[d]
        print(f"   {d} | {e['n']:3d} | {e['min']:7.0f} | {e['kurz']:3d} | "
              f"{e['over']:3d} | {dict(e['q'])}")

    morgen = defaultdict(lambda: [0, 0])
    for t, r in zeilen:
        if not (MORGEN[0] <= t.hour < MORGEN[1]):
            continue
        feedin = _f(r.get("FeedinPower"), 0.0)
        if feedin is not None and feedin < NETZKAUF_W:
            morgen[t.date().isoformat()][0] += 1
            if _an(r):
                morgen[t.date().isoformat()][1] += 1
    print("\n   Morgendlicher Netzbezug (6-12h): Datum | Minuten | davon WP an")
    for d in sorted(morgen)[-10:]:
        m, wp = morgen[d]
        print(f"   {d} | {m * TAKT_SEK / 60:6.0f} min | {wp * TAKT_SEK / 60:6.0f} min")


def analyse_entscheidungen(basis):
    pfad = os.path.join(basis, "entscheidungs_log.jsonl")
    if not os.path.exists(pfad):
        return
    eintraege = []
    with open(pfad, encoding="utf-8") as fh:
        for zeile in fh:
            zeile = zeile.strip()
            if not zeile:
                continue
            try:
                eintraege.append(json.loads(zeile))
            except json.JSONDecodeError:
                continue
    print("\n" + TRENNER)
    print(f"B) entscheidungs_log.jsonl: {len(eintraege)} Eintraege")
    if not eintraege:
        return
    print(f"   Zeitraum: {eintraege[0].get('ts')} .. {eintraege[-1].get('ts')}")
    print(f"   Gewinner: {Counter(e.get('gewinner') or '-' for e in eintraege).most_common(10)}")
    an = [e for e in eintraege if e.get("kompressor_laeuft")]
    print(f"   WP laeuft: {len(an)} Eintraege ({100 * len(an) / len(eintraege):.0f} %)")
    klasse = Counter()
    for e in an:
        feedin = e.get("feedin_w")
        if isinstance(feedin, (int, float)):
            klasse["pv_batterie" if feedin >= NETZKAUF_W else "netz"] += 1
    print(f"   Stromquelle waehrend Lauf: {klasse.most_common()}")
    print("   Top-AUS-Gruende:")
    for g, n in Counter((e.get("grund") or "")[:50] for e in eintraege
                        if not e.get("kompressor_laeuft")).most_common(8):
        print(f"     {n:4d}x {g}")


def analyse_errorlog(basis, tage_zurueck=8):
    pfad = os.path.join(basis, "error.log")
    if not os.path.exists(pfad):
        return
    pro_tag = Counter()
    typen = Counter()
    grenze = (datetime.now() - timedelta(days=tage_zurueck)).date()
    with open(pfad, encoding="utf-8", errors="replace") as fh:
        for zeile in fh:
            if not zeile[:4].isdigit():
                continue
            tag = zeile[:10]
            pro_tag[tag] += 1
            try:
                if datetime.strptime(tag, "%Y-%m-%d").date() >= grenze:
                    typen[zeile.split(" - ", 1)[-1][:60].rstrip()] += 1
            except ValueError:
                continue
    print("\n" + TRENNER)
    print("C) error.log")
    if pro_tag:
        print(f"   Tage: {len(pro_tag)} | aeltester {min(pro_tag)} | neuester {max(pro_tag)}")
        print("   Zeilen pro Tag:")
        for d in sorted(pro_tag)[-tage_zurueck:]:
            print(f"     {d}: {pro_tag[d]}")
    print(f"   Top-Typen (letzte {tage_zurueck} Tage):")
    for t, n in typen.most_common(10):
        print(f"     {n:5d}x {t}")


def analyse_journal(basis):
    pfad = os.path.join(basis, "journal.txt")
    if not os.path.exists(pfad):
        return
    text = open(pfad, encoding="utf-8", errors="replace").read()
    print("\n" + TRENNER)
    print("D) journal.txt (systemd-Sicht)")
    muster = {
        "Service gestartet": "Started ",
        "Service gestoppt": "Stopping ",
        "Fehlgeschlagen": "Failed with result",
        "Killed (SIGKILL)": "code=killed",
        "Restart geplant": "Scheduled restart",
        "OOM-Meldung": "out of memory",
    }
    for name, m in muster.items():
        print(f"   {name:20s}: {text.count(m)}")
    relevant = [z for z in text.splitlines()
                if any(m in z for m in ("Started ", "Stopping ", "Failed with result",
                                        "code=killed", "Scheduled restart"))]
    print("   Letzte Zeilen:")
    for z in relevant[-8:]:
        print(f"     {z[:130]}")


def analyse_lernen(basis):
    pfad = os.path.join(basis, "learning_data.json")
    if not os.path.exists(pfad):
        return
    d = json.load(open(pfad, encoding="utf-8"))
    print("\n" + TRENNER)
    print("E) learning_data.json")
    print(f"   Schluessel: {sorted(d.keys())}")
    for k in ("total_cycles", "total_usage_events", "target_hour_samples",
              "morning_target_hour_samples", "forecast_ratio_samples"):
        if k in d:
            print(f"   {k}: {d[k]}")
    for saison, v in (d.get("heat_rates") or {}).items():
        print(f"   heat_rate {saison}: {v}")
    for k in ("learned_evening_window", "learned_morning_window",
              "komfort_verletzungen_7d", "zu_frueh_14d", "zu_frueh_events_gesamt"):
        if k in d:
            print(f"   {k}: {d[k]}")


def analyse_zyklus_enden(basis):
    """Welche Regel beendet die Zyklen? -> Ursache der Kurzzyklen.

    Nutzt entscheidungs_log.jsonl: Wechsel von kompressor_laeuft true->false
    ergeben ein Zyklus-Ende mit der dann gewinnenden Regel und dem Grund.
    """
    pfad = os.path.join(basis, "entscheidungs_log.jsonl")
    if not os.path.exists(pfad):
        return
    eintraege = []
    with open(pfad, encoding="utf-8") as fh:
        for zeile in fh:
            try:
                e = json.loads(zeile)
            except json.JSONDecodeError:
                continue
            t = _ts((e.get("ts") or "").replace("T", " ")[:19])
            if t:
                eintraege.append((t, e))
    eintraege.sort(key=lambda x: x[0])
    if not eintraege:
        return

    enden = []
    start_ts = None
    vorher = False
    for t, e in eintraege:
        laeuft = bool(e.get("kompressor_laeuft"))
        if laeuft and not vorher:
            start_ts = t
        elif not laeuft and vorher and start_ts:
            enden.append({"dauer_min": (t - start_ts).total_seconds() / 60,
                          "regel": e.get("gewinner") or "-",
                          "grund": (e.get("grund") or "")[:60]})
            start_ts = None
        vorher = laeuft
    if not enden:
        return

    print("\n" + TRENNER)
    print(f"F) Zyklus-Enden aus dem Entscheidungslog: {len(enden)} Zyklen")
    print("   Regel am Ende | Zyklen | Median min | davon Kurzzyklen (<20 min)")
    pro_regel = defaultdict(list)
    for e in enden:
        pro_regel[e["regel"]].append(e["dauer_min"])
    for regel, dauern in sorted(pro_regel.items(), key=lambda kv: -len(kv[1])):
        kurz = sum(1 for d in dauern if d < KURZZYKLUS_MIN)
        print(f"   {regel:16s} | {len(dauern):6d} | {statistics.median(dauern):10.0f} | "
              f"{kurz:5d} ({100 * kurz / len(dauern):.0f} %)")

    kurz = [e for e in enden if e["dauer_min"] < KURZZYKLUS_MIN]
    print(f"\n   Top-Gruende bei Kurzzyklen ({len(kurz)}):")
    for g, n in Counter(e["grund"] for e in kurz).most_common(8):
        print(f"     {n:4d}x {g}")


def main():
    basis = sys.argv[1] if len(sys.argv) > 1 else "."
    if not os.path.isdir(basis):
        print(f"FEHLER: Verzeichnis nicht gefunden: {basis}")
        return 1
    analyse_csv(basis)
    analyse_entscheidungen(basis)
    analyse_zyklus_enden(basis)
    analyse_errorlog(basis)
    analyse_journal(basis)
    analyse_lernen(basis)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())