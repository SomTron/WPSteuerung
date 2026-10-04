#!/usr/bin/env python3
"""Auswertung des Entscheidungslogs fuer einen Tag.

Auf dem Pi starten:
    python3 ~/<dieses Skript>

Zeigt, warum der Kompressor ein- oder ausgeschaltet wurde und wie lange
er lief. Beantwortet die Frage "warum keine Solarzeit?" belegt statt
vermutet.
"""
import collections
import glob
import json
import os
import sys
from datetime import datetime

TAG = sys.argv[1] if len(sys.argv) > 1 else "2026-10-03"
PFAD = "/home/patrik/WPSteuerung/Steuerung/entscheidungs_log.jsonl"


def _liefere_zeilen() -> tuple[list[dict], str]:
    """Liest den Log EINCHL fuer den gewaehlten Tag.

    `entscheidungs_log` rotiert bei 2 MB nach `.old.1` ... `.old.7`. Die
    Datei faellt also mit der Zeit aus dem Blick. Beim Auswerten eines
    Tages, der in einer rotierten Generation liegt, lieferte die alte
    Variante `Keine Eintraege` - und liess damit genau das Falsche
    schliessen, was man wissen wollte ("es wurde nicht geheizt").

    Deshalb: Hauptdatei und alle Generationen gemeinsam lesen und nach
    `ts` filtern. Sortiert wird anhand des Zeitstempels, nicht der
    Dateireihenfolge.
    """
    muster = PFAD + "*"
    pfade = [p for p in glob.glob(muster)
             if not p.endswith(".tmp") and os.path.isfile(p)]
    if PFAD not in pfade:
        pfade.append(PFAD)  # Fehlermeldung der Regex-Suche ist brauchbarer
    zeilen = []
    for p in pfade:
        try:
            with open(p, encoding="utf-8") as fh:
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
        except OSError as exc:
            print(f"Warnung: {p} nicht lesbar ({exc})")
    zeilen.sort(key=lambda d: str(d.get("ts", "")))
    quelle = PFAD if len(pfade) == 1 else f"{len(pfade)} Datei(en)"
    return zeilen, quelle


def main() -> int:
    zeilen, quelle = _liefere_zeilen()
    if not zeilen:
        print(f"Keine Eintraege fuer {TAG} in {quelle}")
        print("Hinweis: Tagformat YYYY-MM-DD. Bei_rotationierten Bestand "
              "werden alle Generationen gelesen.")
        return 1

    print(f"=== {TAG}: {len(zeilen)} Eintraege ===")
    print(f"Zeitraum: {zeilen[0]['ts']}  bis  {zeilen[-1]['ts']}")
    print(f"Quelle:   {quelle}")

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
    # BEFUND 04.10.2026: "verschenkt" war als `nicht kompressor_laeuft`
    # definiert. Das ist falsch: determine_mode_and_setpoints schreibt den
    # Logeintrag, BEVOR der Hardware-Start bestaetigt ist. Ein erfolgreicher
    # Start erscheint deshalb zuerst als `soll_einschalten=true` bei
    # `kompressor_laeuft=false`. Solche Zyklen wurden als "verschenkt"
    # gezaehlt, obwohl die Regel genau richtig gehandelt hat.
    #
    # Beispiel vom 04.10.: alle 4 "verschenkten" Zyklen trugen den Grund
    # "Legionellenprophylaxe faellig: Starte mit PV auf 60C" - die Regel
    # FORDERTE also das Einschalten. Der Zaehler hat die Regel fuer das
    # Gegenteil gehalten.
    #
    # Korrekt ist: verschenkt sind Zyklen mit Ueberschuss, bei denen die
    # Regel keinen Start verlangt hat UND der Kompressor aus blieb.
    verschenkt = [d for d in hoch
                  if not d.get("kompressor_laeuft")
                  and not d.get("soll_einschalten")]
    # Getrennt ausweisen: Start verlangt, Hardware aber noch nicht bestaetigt.
    start_verlangt = [d for d in hoch
                      if not d.get("kompressor_laeuft")
                      and d.get("soll_einschalten")]
    print(f"Ueberschuss>0 : {len(hoch)} Zyklen")
    print(f"  davon genutzt: {len(genutzt)}")
    print(f"  Start verlangt, HW-Bestaetigung folgt im naechsten Eintrag:"
          f" {len(start_verlangt)}")
    print(f"  wirklich verschenkt: {len(verschenkt)}")
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
        print("\nBeispiele mit allen Rohwerten:")
        for d in verschenkt[:8]:
            print(f"  {spitzen_uhr(d)}")
            print(f"      pv_ac={_w(d,'pv_acpower_w')}  einspeis={_w(d,'feedin_w')}"
                  f"  batt={_w(d,'batpower_w')}  haus={_w(d,'hausverbrauch_w')}"
                  f"  ueb={_w(d,'pv_ueberschuss_w')}  soc={d.get('soc')}")
            print(f"      t_oben={d.get('t_oben')}  t_unten={d.get('t_unten')}"
                  f"  -> {(d.get('grund') or '')[:60]}")

        # Plausibilitaet: Ueberschuss kann nicht groesser sein als das, was
        # PV undSpeicher wirklich liefern. Ein Ueberschuss deutlich ueber der
        # PV-Leistung bei gleichzeitig steigendem SOC deutet darauf hin, dass
        # das Ladeverhalten des Speichers als Ueberschuss gewertet wird.
        print("\n--- Plausibilitaet Ueberschuss")
        print(f"{'Zeit':<9}{'ueb':>8}{'pv':>8}{'einsp':>8}{'batt':>8}{'soc':>7}  auffaellig")
        auffaellig = 0
        for d in zeilen:
            ueb = d.get("pv_ueberschuss_w") or 0
            pv = d.get("pv_acpower_w") or 0
            bat = d.get("batpower_w")
            soc = d.get("soc")
            if ueb <= 0:
                continue
            # Lädt der Speicher (bat > 0), darf sein Ladestrom nicht als
            # Ueberschuss gelten - er fliesst ins Speicher, nicht in die WP.
            problem = (bat is not None and bat > 200
                       and ueb > pv + 200)
            if problem:
                auffaellig += 1
            if problem and auffaellig <= 10:
                print(f"{spitzen_uhr(d):<9}{ueb:>8.0f}{pv:>8.0f}"
                      f"{(d.get('feedin_w') or 0):>8.0f}{bat:>8.0f}"
                      f"{soc:>7.0f}  ja")
        gesamt = sum(1 for d in zeilen
                     if (d.get("pv_ueberschuss_w") or 0) > 0)
        print(f"\nauffaellig: {auffaellig} von {gesamt} Zyklen mit Ueberschuss")
        if auffaellig:
            print("Hinweis: Speicher laedt, sein Strom wird aber als "
                  "Ueberschuss gezaehlt.")

    print("\n--- Temperaturverlauf oben (Stichproben)")
    probe = zeilen[::max(1, len(zeilen) // 12)]
    for d in probe:
        print(f"  {spitzen_uhr(d)}  t_oben={d.get('t_oben')}"
              f"  t_mitte={d.get('t_mitte')}  t_unten={d.get('t_unten')}"
              f"  komp={'AN' if d.get('kompressor_laeuft') else 'aus'}")

    return 0


def _w(d, feld) -> str:
    """Wert formatieren, fehlende Werte kenntlich machen."""
    v = d.get(feld)
    return "-" if v is None else f"{v:.0f}"


def spitzen_uhr(d) -> str:
    try:
        return datetime.fromisoformat(str(d.get("ts"))).strftime("%H:%M:%S")
    except ValueError:
        return str(d.get("ts"))[-8:]


if __name__ == "__main__":
    sys.exit(main())