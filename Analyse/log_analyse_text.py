# -*- coding: utf-8 -*-
"""Auswertung des Text-Betriebslogs (heizungssteuerung_log_*.txt).

Warum ein eigenes Skript: `log_analyse.py` liest das JSONL-Entscheidungslog
(strukturierte Felder). Dieses hier liest das Klartext-Journal des Pi und
beantwortet zwei Fragen, die nur dort sichtbar sind:

  1. **Rate der Entscheidungspfade** - Start-Antizipation und
     Overshoot-Vorhersage arbeiten mit `_rate_fuer_entscheidung`; im Log stehen
     ihre Raten. Die Auswertung zaehlt die Rate-Klassen und rechnet nach, wie
     viele Sperren/Abschaltungen mit der Kappung auf
     `learning_engine.heating_rate_max_c_h` (6 C/h) entfallen waeren.
  2. **Gewinner-AUS gegen rangniedrigere EIN-Regeln** - der Gewinner ist die
     hoechste Prioritaet mit EIN/AUS. Eine AUS-Regel verdeckt damit jede
     rangniedrigere Regel; das Skript zaehlt diese Konstellationen (z. B.
     AdaptivePV 78 gegen Abweichung 47, Notfallschutz 110 gegen Legionellen 90).

Belegte Befunde (Log 14.09.-18.10.2026, 1460 Regel-Bewertungen):
    * 42 Schatten-Ereignisse (2,9 %): 29x Komfort->Forecast (alte Config),
      10x Notfallschutz-Hysterese (Sept., durch `legionellen_angekuendigt`
      inzwischen behoben), 2x Wochenende (gewollt), 1x AdaptivePV->Abweichung
      (Befund 18.10.2026, behoben: `oben_max_start_c` liefert im Aus-Zustand
      `None` statt `False`).
    * Start-Antizipation: 163 Sperrmeldungen, davon 143 mit Rate > 12 C/h;
      144 (88 %) waeren mit der Kappung auf 6 C/h nicht entstanden.
    * Overshoot-Vorhersage: 9 Abschaltungen, 5 davon nur wegen der ungekappten
      Rate - u. a. eine Legionellenfahrt, die bei `unten` 41,7 C mit der
      Prognose "68,5 C" abgewuergt wurde (25.09.2026, 12:53).

Nur Standardbibliothek, liest ausschliesslich.

Aufruf:
    py -3 Analyse/log_analyse_text.py <logdatei> [bericht.txt]
"""
import re
import sys
from collections import Counter

REGEL = re.compile(r"^\s*\[\s*(\d+)\]\s+(EIN|AUS|---)\s+([^:]+):\s*(.*)$")
BLOCK = re.compile(r"Regel-Bewertung \((\d+) Regeln\):")
ZEIT = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}):\d{2} ")
# Aktuelle Fassung: "... (Rate 36C/h, Confidence 40% -> 21min < 25min
# Mindestlaufzeit + 1min Reserve, Konfidenz mittel)"
STARTV = re.compile(
    r"Start-Antizipation: hub zur Obergrenze nur ([\d.]+)K "
    r"\(Rate ([\d.]+)C/h, Confidence (\d+)% -> ([\d.]+)min < ([\d.]+)min "
    r"Mindestlaufzeit \+ ([\d.]+)min Reserve")
# Aeltere Fassung (vor der 25-min-Messlatte): "... (Rate 19C/h -> 13min
# < 60min Mindestlaufzeit)"
STARTV_ALT = re.compile(
    r"Start-Antizipation: hub zur Obergrenze nur ([\d.]+)K "
    r"\(Rate ([\d.]+)C/h -> ([\d.]+)min < ([\d.]+)min Mindestlaufzeit")
OVER = re.compile(
    r"OVERSHOOT-VORHERSAGE AUS \(cycle=(\d+)\) reason=overshoot_vorhersage: "
    r"(\w+) ([\d.]+)C steigt mit ([\d.]+)C/h auf ~([\d.]+)C "
    r"\(Limit ([\d.]+)C, Reserve ([\d.]+)K\)")
LERN = re.compile(
    r"Learning: Heizzyklus - (\d+)min, unten ([\d.]+)->([\d.]+)C = ([\d.]+)C/h "
    r"\((\w+), MW=([\d.]+), n=(\d+)(, zu kurz fuer die Rate)?\)")
HEIZPLAN = re.compile(r"noch ([\d.]+)K bei ([\d.]+) C/h -> erreicht ca\. (\d{2}:\d{2})")
# Startup-Marker: "Start WPSteuerung | GitHub-Rev: <hash> [branch] Betreff"
REVISION = re.compile(r"GitHub-Rev: (\S+)(?: \[([^\]]*)\])?")

#: Kappung der Planungsrate (Default `LearningConfig.heating_rate_max_c_h`).
RATE_MAX_C_H = 6.0


def _rate_klasse(rate):
    if rate <= 6.0:
        return "<=6 C/h"
    if rate <= 12.0:
        return "6-12 C/h"
    if rate <= 20.0:
        return "12-20 C/h"
    if rate <= 30.0:
        return "20-30 C/h"
    return ">30 C/h"


def _prognose_mit_kappe(t, rest_min):
    return t + RATE_MAX_C_H * (rest_min / 60.0)


def _werte_block(block, schatten, persist, oben_voll):
    """Gewinner = hoechste Prioritaet mit EIN/AUS (nicht '---')."""
    regeln = block["regeln"]
    mit_meinung = [r for r in regeln if r[1] in ("EIN", "AUS")]
    if not mit_meinung:
        return
    gewinner = max(mit_meinung, key=lambda r: r[0])
    if gewinner[1] == "AUS":
        verdeckt = [f"{r[2]}({r[0]})" for r in regeln
                    if r[1] == "EIN" and r[0] < gewinner[0]]
        if verdeckt:
            schatten.append((block["zeit"], gewinner[2], gewinner[3], verdeckt))
            persist[(gewinner[2], tuple(sorted(verdeckt)))] += 1
    for r in regeln:
        if "Speicher oben voll" in r[3]:
            oben_voll.append((block["zeit"], r[3]))


def sammle(pfad):
    """Liest das Log und liefert die Rohdaten fuer den Bericht."""
    daten = {
        "bloecke": 0, "schatten": [], "persist": Counter(), "oben_voll": [],
        "start": [], "overshoot": [], "lern": [], "heizplan": [],
        "zaehler": Counter(), "unbekannt": [], "revisionen": [],
    }
    now = ""
    block = None
    with open(pfad, "r", encoding="utf-8", errors="replace") as f:
        for zeile in f:
            m = ZEIT.match(zeile)
            if m:
                now = f"{m.group(1)} {m.group(2)}"

            m = BLOCK.search(zeile)
            if m:
                block = {"zeit": now, "regeln": []}
                daten["bloecke"] += 1
                continue
            if block is not None:
                m = REGEL.match(zeile)
                if m:
                    block["regeln"].append(
                        (int(m.group(1)), m.group(2), m.group(3).strip(), m.group(4)))
                    continue
                _werte_block(block, daten["schatten"], daten["persist"],
                             daten["oben_voll"])
                block = None

            if "OVERSHOOT-VORHERSAGE AUS" in zeile:
                daten["zaehler"]["overshoot_aus"] += 1
                m = OVER.search(zeile)
                if m:
                    daten["overshoot"].append(
                        (now, float(m.group(4)), float(m.group(3)), float(m.group(5)),
                         float(m.group(6)), float(m.group(7))))
                continue
            if "reason=start_vorhersage" in zeile:
                m = STARTV.search(zeile)
                if m:
                    # (hub, rate, minz, reserve) - aktuelle Fassung
                    daten["start"].append((now, float(m.group(1)), float(m.group(2)),
                                           float(m.group(5)), float(m.group(6))))
                else:
                    m = STARTV_ALT.search(zeile)
                    if m:
                        # aeltere Fassung ohne Reserve-Angabe
                        daten["start"].append((now, float(m.group(1)),
                                               float(m.group(2)), float(m.group(4)),
                                               0.0))
                    else:
                        daten["unbekannt"].append(zeile.strip()[-140:])
                continue
            m = LERN.search(zeile)
            if m:
                daten["lern"].append((float(m.group(1)), float(m.group(4)),
                                      bool(m.group(8))))
            m = HEIZPLAN.search(zeile)
            if m:
                daten["heizplan"].append(float(m.group(2)))
            m = REVISION.search(zeile)
            if m:
                daten["revisionen"].append(
                    (now, m.group(1), m.group(2) or "", zeile.strip()[-80:]))
            for wort in ("Mindestlaufzeit noch nicht erreicht", "Zyklus gespeichert",
                         "Kompressor EIN", "Kompressor AUS"):
                if wort in zeile:
                    daten["zaehler"][wort] += 1
    if block is not None:
        _werte_block(block, daten["schatten"], daten["persist"], daten["oben_voll"])
    return daten


def bericht(pfad, d):
    """Erzeugt den Textbericht als Liste von Zeilen."""
    aus = []

    def p(*a):
        aus.append(" ".join(str(x) for x in a))

    tage = sorted({s[0][:10] for s in d["schatten"]})
    p("=" * 78)
    p(f"Auswertung Text-Betriebslog: {pfad}")
    p(f"{d['bloecke']} Regel-Bewertungen, {len(d['schatten'])} "
      f"Schatten-Ereignisse auf {len(tage)} Tagen")
    p("=" * 78)

    p("\n### A) Gewinner sagt AUS, rangniedrigere Regel wollte EIN")
    schatten = d["schatten"]
    p(f"Ereignisse: {len(schatten)} von {d['bloecke']} Bewertungen = "
      f"{100.0*len(schatten)/max(d['bloecke'], 1):.1f} %")
    p(f"'Speicher oben voll'-Meldungen: {len(d['oben_voll'])} auf "
      f"{len({o[0][:10] for o in d['oben_voll']})} Tagen")
    p("\n  Haeufigste Kombinationen (Anzahl Bewertungen):")
    for (g, v), n in d["persist"].most_common(12):
        p(f"    {n:5d} x  {g}  verdeckt  {', '.join(v)}")
    p("\n  Verdeckte Regeln:")
    for g, n in Counter(r for s in schatten for r in s[3]).most_common():
        p(f"    {n:5d} x  {g}")
    p("\n  Alle Ereignisse (Zeit | Gewinner | verdeckt | Grund):")
    for s in schatten:
        p(f"    {s[0]}  {s[1]:<14} | {', '.join(s[3]):<44} | {s[2][:58]}")

    p("\n### B) Start-Antizipation: Sperren und Rate")
    start = d["start"]
    p(f"Sperrmeldungen: {len(start)} (+{len(d['unbekannt'])} unbekanntes Format)")
    p("  Rate-Klassen: " + ", ".join(
        f"{k}={v}" for k, v in
        sorted(Counter(_rate_klasse(r[2]) for r in start).items())))
    frei_tag = frei_nacht = 0
    for ts, hub, rate, minz, res in start:
        erwartet = hub / max(rate, 1.0) * 60.0
        if erwartet + res < minz and hub / RATE_MAX_C_H * 60.0 + res >= minz:
            try:
                stunde = int(ts[11:13])
            except ValueError:
                stunde = 12
            if 8 <= stunde < 19:
                frei_tag += 1
            else:
                frei_nacht += 1
    p(f"  Mit Kappung auf {RATE_MAX_C_H:.0f} C/h NICHT mehr gesperrt: "
      f"{frei_tag + frei_nacht} (davon 8-19 Uhr: {frei_tag}; ausserhalb, dort "
      f"greift zusaetzlich die Nachtabschaltung: {frei_nacht})")
    p("  Beispiele mit Rate > 12 C/h:")
    gezeigt = 0
    for ts, hub, rate, minz, res in start:
        if rate > 12.0 and gezeigt < 6:
            p(f"    {ts}  Rate {rate}C/h, hub {hub}K -> mit "
              f"{RATE_MAX_C_H:.0f} C/h {hub/RATE_MAX_C_H*60.0:.0f} min statt "
              f"{hub/max(rate, 1.0)*60.0:.0f} min (Grenze {minz:.0f}+{res:.0f})")
            gezeigt += 1
    for z in d["unbekannt"][:3]:
        p(f"    ...{z}")

    p("\n### C) Overshoot-Vorhersage (AUS)")
    over = d["overshoot"]
    weg = 0
    for _ts, rate, t, prog, limit, res in over:
        rest = (prog - t) / max(rate, 0.1) * 60.0
        if _prognose_mit_kappe(t, rest) < limit - res:
            weg += 1
    p(f"Ereignisse: {len(over)}; mit Kappung auf {RATE_MAX_C_H:.0f} C/h nicht "
      f"ausgeloest: {weg} ({100.0*weg/max(len(over), 1):.0f} %)")
    for ts, rate, t, prog, limit, res in over:
        rest = (prog - t) / max(rate, 0.1) * 60.0
        p(f"    {ts}  {rate:5.1f}C/h  {t:.1f}C -> {prog:5.1f}C (Limit "
          f"{limit:.1f}, Rest {rest:.0f} min, mit {RATE_MAX_C_H:.0f} C/h: "
          f"{_prognose_mit_kappe(t, rest):.1f}C)")

    p("\n### D) Lernzyklen und Planungsraten")
    lern = d["lern"]
    kurz = [z for z in lern if z[2]]
    lang = [z for z in lern if not z[2]]
    p(f"Lernzyklen: {len(lern)}, davon 'zu kurz fuer die Rate': {len(kurz)}")
    for label, menge in (("Kurzzyklen-Raten", kurz), ("Gezaehlte Raten", lang)):
        if menge:
            p(f"  {label}: {min(z[1] for z in menge):.1f} - "
              f"{max(z[1] for z in menge):.1f} C/h (Mittel "
              f"{sum(z[1] for z in menge)/len(menge):.1f})")
    if d["heizplan"]:
        p(f"Heizplan-Zeilen: {len(d['heizplan'])}, Raten "
          f"{min(d['heizplan']):.1f}-{max(d['heizplan']):.1f} C/h (Mittel "
          f"{sum(d['heizplan'])/len(d['heizplan']):.1f})")

    p("\n### E) Sonstige Zaehler")
    for k, v in sorted(d["zaehler"].items(), key=lambda kv: -kv[1]):
        p(f"    {v:6d} x  {k}")

    p("\n### F) Code-Staende im Log (GitHub-Rev)")
    revs = d["revisionen"]
    p(f"Startup-Marker: {len(revs)}, verschiedene Revisionen: "
      f"{len({r[1] for r in revs})}")
    gesehen = {}
    for ts, hash_wert, branch, _zeile in revs:
        info = gesehen.setdefault(
            hash_wert, {"von": ts, "bis": ts, "branch": branch, "n": 0})
        info["bis"] = ts
        info["n"] += 1
    for hash_wert, info in gesehen.items():
        p(f"    {hash_wert:<12} [{info['branch']:<28}] "
          f"{info['von']} - {info['bis']}  ({info['n']} Starts)")
    if not revs:
        p("    (kein GitHub-Rev-Marker im Log - aeltere Programmversion)")
    return aus


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    daten = sammle(sys.argv[1])
    zeilen = bericht(sys.argv[1], daten)
    text = "\n".join(zeilen)
    print(text)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

