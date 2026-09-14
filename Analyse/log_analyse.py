# -*- coding: utf-8 -*-
"""Verbesserte Log-Analyse fuer heizungssteuerung_upload_*.txt.

Setzt die Empfehlungen aus der Log-Review um:
  1. Strukturiertes Parsen aller Regel-Bewertungen inkl. Kontext-Anreicherung
     (PV, SOC, Einspeisung, Forecast, Temperatur-Snapshot).
  2. Stabile Event-Codes statt Freitext (Zyklus, Overshoot, BOILERMAX, ...).
  3. Zyklus-Analyse: Dauer, Quelle (pv/batterie/netz), Endgrund, Kurzzyklen.
  4. Tages-KPIs inkl. Korrelation "morgendlicher Netzbezug vs. Tagesprognose".
  5. Warnungen/Fehler gruppiert + Log-Volumen-Analyse + Encoding-Check.

Nur Standardbibliothek -> laeuft ohne pip-Installation (kein pandas/noch nicht noetig).
"""
import csv
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #
LOG_PFAD = r"C:\Python\WPSteuerung\WPSteuerung\logs\heizungssteuerung_upload_6.9.2026-14.9.txt"
AUSGABE_DIR = r"C:\Python\WPSteuerung\WPSteuerung\logs\analyse_2026-09-08_14.09"

OVERSHOOT_SCHWELLE_C = 48.5      # unten >= x im Zyklus gilt als gefaehrlicher Zustand
KURZZYKLUS_MIN = 20.0            # < x Minuten = Kurzzyklus
NETZKAUF_GRENZE_W = -50.0        # feedin < x => Haus kauft Netzstrom
PV_VERPASST_AB_W = 500.0         # Einspeisung >= x waehrend Standby = "verschenkte PV"
MORGEN_FENSTER = (6, 12)         # Uhrzeiten fuer "Morgen-Netzbezug"-Analyse

# --------------------------------------------------------------------------- #
# Normalisierung des Logs (bekannte MojiBake-Folgen aus cp1252-Fehltranskodierung)
# --------------------------------------------------------------------------- #
_MOJIBAKE = [
    ("Ã‚Â°C", "°C"), ("Ã‚Â²", "²"), ("Â°C", "°C"),
    ("Ã¤", "ä"), ("Ã¶", "ö"), ("Ã¼", "ü"),
    ("Ã„", "Ä"), ("Ã–", "Ö"), ("Ãœ", "Ü"), ("ÃŸ", "ß"),
    ("âš ï¸", "⚠"), ("Ã¢Å Â Ã¯Â¸Â", "⚠"), ("ðŸ¦", "🦠"),
    ("Ã©", "é"),
]


def norm(line: str) -> str:
    for alt, fix in _MOJIBAKE:
        if alt in line:
            line = line.replace(alt, fix)
    return line


# --------------------------------------------------------------------------- #
# Regulaere Ausdruecke
# --------------------------------------------------------------------------- #
RE_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ([+-]\d{4}) (\w+) - (.*)$"
)
RE_RULE = re.compile(
    r"^\s*\[\s*(\d+)\]\s+(---|EIN|AUS)\s+([A-Za-zÄÖÜäöü0-9_-]+)"
    r"( \[INAKTIV\])?:\s?(.*)$"
)
RE_SENSOR = re.compile(
    r"Sensoren: Oben=([\d.]+)°?C \| Mittig=([\d.]+)°?C \| "
    r"Unten=([\d.]+)°?C \| Verd=([\d.]+)°?C"
)
RE_STATUS = re.compile(r"Status: (EIN|AUS)\s*(?:\|(.*))?$")
RE_COMP_EIN = re.compile(r"Kompressor EIN")
RE_COMP_EIN_VERIF = re.compile(r"t_verd=([\d.]+), t_unten=([\d.]+)")
RE_COMP_AUS = re.compile(r"Kompressor AUS(?: \(cycle=[^)]*\))?.*?\. Laufzeit: (\d+):(\d+):([\d.]+)")
RE_WECHSEL = re.compile(r"Wechsel zu Regel: (.+?)(?: \((EIN|AUS)\))?$")
RE_LEARN = re.compile(r"Learning: Heizzyklus - (\d+)min, unten ([\d.]+)->([\d.]+)C = ([\d.]+)C/h")
RE_FORECAST = re.compile(r"Today=([\d.]+) kWh/m²?.*?Tomorrow=([\d.]+) kWh/m²?")
RE_BOILERMAX = re.compile(r"BOILERMAX AUS(?: \(cycle=[^)]*\))? - (\w+) ([\d.]+)C")
RE_BOILERMAX_NAEHE = re.compile(r"Boiler-Max-Naehe")
RE_ZU_FRUEH = re.compile(r"ZU FRUEH geheizt")
RE_TAKTSCHUTZ = re.compile(r"Taktschutz aktiv")
RE_VERIF = re.compile(r"Kompressor-Verifizierung fehlgeschlagen")
RE_ABBRUCH_LEGIO = re.compile(r"Legionellenprophylaxe ABGEBROCHEN")
RE_REGEL_AUS = re.compile(r"Regel '([^']+)' sagt AUS")
RE_SENSO_FEHLER = re.compile(r"Zu wenige Zeilen|Sensor-Error|Sensorfehler", re.I)

# ---------------------------------------------------------------- helpers -->
def parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S %z")


def _zahl(werte):
    werte = [float(w) for w in werte if w is not None]
    return max(werte) if werte else None


def _code_klassifiziere(warnung: str) -> str:
    """Stabile Kategorien-Codes fuer Warnings/Errors statt Freitext."""
    if "BOILERMAX AUS" in warnung:
        return "WARN_BOILERMAX"
    if "Boiler-Max-Naehe" in warnung:
        return "WARN_BOILERMAX_NAEHE"
    if "Solar-Daten veraltet" in warnung:
        return "WARN_SOLAR_STALE"
    if "ZU FRUEH" in warnung:
        return "WARN_ZU_FRUEH"
    if "Taktschutz" in warnung:
        return "WARN_TAKTSCHUTZ"
    if "CalcStart-Hinweis" in warnung:
        return "WARN_CALCSTART_NACHSPERRE"
    if "Alle Versuche fehlgeschlagen" in warnung:
        return "ERR_NETZWERK_ALLE_VERSUCHE"
    if "Fehler bei der API-Anfrage" in warnung:
        return "ERR_SOLAX_API"
    if "Maximale Wiederholungen erreicht" in warnung:
        return "ERR_FALLBACK_DATEN"
    if "Hintergrund-Task" in warnung:
        return "ERR_HINTERGRUND_TASK"
    if "Fehler im Loop-Durchlauf" in warnung:
        return "ERR_LOOP_DURCHLAUF"
    if "Fehler beim Abrufen von Telegram-Updates" in warnung:
        return "ERR_TELEGRAM_UPDATES"
    if "Unhandled" in warnung or "Unbehandelt" in warnung:
        return "ERR_UNHANDLED"
    if "Unexpected error in get_solar_forecast" in warnung:
        return "ERR_FORECAST"
    if "Netzwerkfehler beim Senden" in warnung:
        return "ERR_TELEGRAM_SENDEN"
    if "Healthcheck-Ping Fehler" in warnung:
        return "ERR_HEALTHCHECK"
    if "LCD Schreibfehler" in warnung:
        return "WARN_LCD"
    if "Temperatur ueber Normalbereich" in warnung:
        return "WARN_TEMP_NORMALBEREICH"
    if RE_SENSO_FEHLER.search(warnung):
        return "ERR_SENSOR"
    if "Neustartsperre" in warnung:
        return "WARN_NEUSTARTSPERRE"
    if "Druckschalter" in warnung or "pressure" in warnung.lower():
        return "WARN_DRUCKSCHALTER"
    return "SONSTIGES"
# ------------------------------------------------------------- Parser ---- #
def parse_log(pfad):
    """Parst das Log und liefert strukturierte Daten fuer die Analyse."""
    pv_kontext, soc_kontext, feedin_kontext = [None], [None], [None]
    snapshots, zyklen, ereignisse = [], [], []
    forecast_pro_tag = {}
    gelernter_zyklus = []
    git_version = None
    stats = Counter()
    block = None
    last_sensoren = {}
    last_sensors_ts = None
    offener_zyklus = None
    letzte_zeilen = []

    def _finalisiere_block():
        nonlocal block
        if block is None:
            return
        grund_txt = " || ".join(r["grund"] for r in block["regeln"])
        block["pv_w"] = _zahl(re.findall(r"PV ([\d.]+)W", grund_txt))
        block["soc"] = _zahl(re.findall(r"SOC ([\d.]+)%", grund_txt))
        block["feedin_w"] = _zahl(re.findall(r"Einspeisung ([\d.]+)W", grund_txt))
        m_fc = re.search(r"Morgen ([\d.]+) Wh/qm", grund_txt)
        block["forecast_morgen_whqm"] = float(m_fc.group(1)) if m_fc else None
        m_soll = re.search(r"Soll ([\d.]+)C", grund_txt)
        block["soll_c"] = float(m_soll.group(1)) if m_soll else None
        for feld, ctx in (("pv_w", pv_kontext), ("soc", soc_kontext),
                          ("feedin_w", feedin_kontext)):
            if block.get(feld) is not None:
                ctx[0] = block[feld]
            else:
                block[feld] = ctx[0]
        snapshots.append(block)
        block = None

    with open(pfad, "r", encoding="utf-8", errors="replace") as f:
        for roh in f:
            stats["zeilen_gesamt"] += 1
            line = norm(roh.rstrip("\n"))

            # Regel-Detailzeilen (ohne Zeitstempel) direkt dem Block zuordnen
            if block is not None:
                rm = RE_RULE.match(line)
                if rm:
                    block["regeln"].append({
                        "prio": int(rm.group(1)),
                        "verdict": rm.group(2),
                        "name": rm.group(3),
                        "aktiv": rm.group(4) is None,
                        "grund": rm.group(5),
                    })
                    continue

            m = RE_LINE.match(line)
            if not m:
                continue
            ts = parse_ts(f"{m.group(1)} {m.group(2)}")
            level = m.group(3)
            msg = m.group(4)
            stats["zeilen_erkannt"] += 1
            stats["level_" + level] += 1

            # Versionsmarker: Startzeile nennt die GitHub-Revision des Standes
            if git_version is None and "GitHub-Rev:" in msg:
                m_gv = re.search(r"GitHub-Rev:\s*(.*)", msg)
                if m_gv:
                    git_version = m_gv.group(1).strip()

            if msg.startswith("Regel-Bewertung"):
                _finalisiere_block()
                block = {
                    "ts": ts, "level": level, "regeln": [],
                    "sensoren": dict(last_sensoren), "sensoren_ts": last_sensors_ts,
                    "status_ein": None, "regel": None, "modus": None, "blocking": None,
                    "pv_w": None, "soc": None, "feedin_w": None,
                    "forecast_morgen_whqm": None, "soll_c": None,
                }
                continue

            s = RE_SENSOR.search(msg)
            if s:
                last_sensoren = {
                    "oben": float(s.group(1)), "mittig": float(s.group(2)),
                    "unten": float(s.group(3)), "verd": float(s.group(4)),
                }
                last_sensors_ts = ts
                if block is not None:
                    block["sensoren"] = dict(last_sensoren)
                    block["sensoren_ts"] = ts
                continue

            st = RE_STATUS.search(msg)
            if st:
                ein = st.group(1) == "EIN"
                regel = modus = blocking = None
                for part in (st.group(2) or "").split("|"):
                    part = part.strip()
                    if part.startswith("EP=") or part.startswith("AP="):
                        continue
                    if part.startswith("Blocking:"):
                        blocking = part[len("Blocking:"):].strip()
                    elif part.startswith("Regel:"):
                        regel = part[len("Regel:"):].strip()
                    elif part.startswith("Modus:"):
                        modus = part[len("Modus:"):].strip()
                if block is not None:
                    block["status_ein"] = ein
                    block["regel"] = regel
                    block["modus"] = modus
                    block["blocking"] = blocking
                continue

            # ---- Kompressor-Zyklen ----
            if RE_COMP_EIN.match(msg):
                if offener_zyklus is None:
                    mvf = RE_COMP_EIN_VERIF.search(msg)
                    offener_zyklus = {
                        "start_ts": ts, "start_regel": None, "start_t_verd": None,
                        "start_t_unten": None, "end_ts": None, "dauer_min": None,
                        "end_grund": None, "end_regel": None,
                        "end_sensoren": None, "max_unten": None, "quelle": None,
                    }
                    if mvf:
                        offener_zyklus["start_t_verd"] = float(mvf.group(1))
                        offener_zyklus["start_t_unten"] = float(mvf.group(2))
                    for zts, zregel in reversed(ereignisse):
                        if zts <= ts and zregel[0] in ("CYCLE_START_WE", "WECHSEL"):
                            offener_zyklus["start_regel"] = zregel[1]
                            break
                continue

            if "Kompressor AUS. Laufzeit" in msg:
                mm = RE_COMP_AUS.search(msg)
                if offener_zyklus is not None and mm is not None:
                    offener_zyklus["end_ts"] = ts
                    d = int(mm.group(1)) * 60 + int(mm.group(2)) + int(float(mm.group(3))) / 60.0
                    offener_zyklus["dauer_min"] = round(d, 1)
                    offener_zyklus["end_sensoren"] = dict(last_sensoren)
                    grund = None
                    if "BOILERMAX AUS" in msg:
                        grund = "boiler_max"
                    ra = RE_REGEL_AUS.search(msg)
                    if grund is None and ra:
                        grund = "regel:" + ra.group(1)
                    if grund is None and "Keine Regel aktiv" in msg:
                        grund = "keine_regel"
                    if grund is None:
                        for hts, hmsg in reversed(letzte_zeilen):
                            if hts >= ts - timedelta(seconds=120) and hts <= ts:
                                if "BOILERMAX AUS" in hmsg:
                                    grund = "boiler_max"
                                elif "Legionellenprophylaxe ABGEBROCHEN" in hmsg:
                                    grund = "legionellen_timeout"
                                elif "Kompressor-Verifizierung fehlgeschlagen" in hmsg:
                                    grund = "verifizierung_fehler"
                                elif RE_REGEL_AUS.search(hmsg):
                                    grund = "regel:" + RE_REGEL_AUS.search(hmsg).group(1)
                                if grund:
                                    break
                    if grund is None:
                        # Letzter 'Wechsel zu Regel' nahe am AUS als Endgrund nutzen
                        for zts, (code, name, _, _) in reversed(ereignisse):
                            if zts >= ts - timedelta(seconds=120) and zts <= ts \
                                    and code in ("CYCLE_END_WE", "WECHSEL"):
                                grund = "keine_regel" if name == "Keine Regel aktiv" \
                                    else "wechsel:" + name
                                break
                    offener_zyklus["end_grund"] = grund or "unbekannt"
                    zyklen.append(offener_zyklus)
                    offener_zyklus = None
                continue

            w = RE_WECHSEL.search(msg)
            if w:
                code = ("CYCLE_START_WE" if w.group(2) == "EIN"
                        else "CYCLE_END_WE" if w.group(2) == "AUS" else "WECHSEL")
                ereignisse.append((ts, (code, w.group(1), None, None)))
                continue

            lm = RE_LEARN.search(msg)
            if lm:
                gelernter_zyklus.append({
                    "ts": ts, "dauer_min": int(lm.group(1)),
                    "unten_start": float(lm.group(2)), "unten_end": float(lm.group(3)),
                    "rate": float(lm.group(4)),
                })
                continue

            fm = RE_FORECAST.search(msg)
            if fm:
                tag = ts.strftime("%Y-%m-%d")
                if tag not in forecast_pro_tag:  # erste Prognose des Tages behalten
                    forecast_pro_tag[tag] = {
                        "today": float(fm.group(1)), "tomorrow": float(fm.group(2)), "ts": ts,
                    }
                ereignisse.append((ts, ("FORECAST", tag,
                                        f"{fm.group(1)}/{fm.group(2)}", None)))
                continue

            if level in ("WARNING", "ERROR", "CRITICAL"):
                code = _code_klassifiziere(msg)
                ereignisse.append((ts, (code, level, msg[:300], None)))
                stats[code] += 1
                if code == "WARN_BOILERMAX":
                    # BOILERMAX-Warnung kommt unmittelbar NACH der AUS-Zeile:
                    # letzten beendeten Zyklus nachtraeglich als boiler_max markieren
                    for z in reversed(zyklen):
                        if z.get("end_ts") and abs((z["end_ts"] - ts).total_seconds()) <= 60 \
                                and z["end_grund"] in (None, "unbekannt"):
                            z["end_grund"] = "boiler_max"
                            break
                if level in ("WARNING", "ERROR"):
                    letzte_zeilen.append((ts, msg))
                    if len(letzte_zeilen) > 40:
                        letzte_zeilen.pop(0)
                continue

            if level in ("INFO", "WARNING"):
                letzte_zeilen.append((ts, msg))
                if len(letzte_zeilen) > 40:
                    letzte_zeilen.pop(0)

    _finalisiere_block()
    stats["zyklen"] = len(zyklen)
    stats["snapshots"] = len(snapshots)
    stats["regelzeilen"] = sum(len(b["regeln"]) for b in snapshots)

    # Zyklus-Anreicherung: Quelle, max_unten, T-Ueberschreitung
    for z in zyklen:
        if z["start_regel"] in ("AdaptivePV", "Einspeisung", "PV_mitte", "PV_unten"):
            z["quelle"] = "pv"
        elif z["start_regel"] == "Batterie":
            z["quelle"] = "batterie"
        else:
            z["quelle"] = "netz"
        fenster_ende = z["end_ts"] + timedelta(minutes=8) if z["end_ts"] else None
        for snap in snapshots:
            if z["start_ts"] <= snap["ts"] <= (fenster_ende or snap["ts"]):
                u = (snap.get("sensoren") or {}).get("unten")
                if u is not None and (z["max_unten"] is None or u > z["max_unten"]):
                    z["max_unten"] = u
        if z["end_sensoren"]:
            u = z["end_sensoren"].get("unten")
            if u is not None and (z["max_unten"] is None or u > z["max_unten"]):
                z["max_unten"] = u
        if z["start_t_unten"] is not None and z["max_unten"] is not None:
            z["ueberschreitung_k"] = round(max(z["max_unten"] - OVERSHOOT_SCHWELLE_C, 0.0), 1)
        else:
            z["ueberschreitung_k"] = None

    return {"snapshots": snapshots, "zyklen": zyklen, "ereignisse": ereignisse,
            "forecast_pro_tag": forecast_pro_tag, "gelernter_zyklus": gelernter_zyklus,
            "stats": stats, "git_version": git_version}


# ------------------------------------------------------- Auswertungen ---- #
def analysiere(parsed):
    snapshots, zyklen, ereignisse = parsed["snapshots"], parsed["zyklen"], parsed["ereignisse"]
    forecast_pro_tag = parsed["forecast_pro_tag"]
    out = {}

    # --- Tages-KPIs ---
    tage = defaultdict(lambda: {
        "starts": 0, "laufzeit_min": 0.0, "netz_min": 0.0, "pv_bat_min": 0.0,
        "kurzzylen": 0, "boiler_max": 0, "overshoot": 0,
    })
    for z in zyklen:
        tag = z["start_ts"].strftime("%Y-%m-%d")
        t = tage[tag]
        t["starts"] += 1
        t["laufzeit_min"] += z["dauer_min"] or 0.0
        if z["quelle"] in ("pv", "batterie"):
            t["pv_bat_min"] += z["dauer_min"] or 0.0
        else:
            t["netz_min"] += z["dauer_min"] or 0.0
        if (z["dauer_min"] or 0) < KURZZYKLUS_MIN:
            t["kurzzylen"] += 1
        if z.get("ueberschreitung_k"):
            t["overshoot"] += 1
    # BoilerMax direkt aus den Warnereignissen je Tag zaehlen
    for e in ereignisse:
        if e[1][0] == "WARN_BOILERMAX":
            tag = e[0].strftime("%Y-%m-%d")
            tage[tag]["boiler_max"] += 1
    for tag, t in tage.items():
        fc = forecast_pro_tag.get(tag, {})
        t["forecast_today"] = fc.get("today")
        t["forecast_tomorrow"] = fc.get("tomorrow")
    out["tage"] = tage

    # --- Morgen-Netzbezug je Tag (Quelle) ---
    morgen = []
    for z in zyklen:
        if MORGEN_FENSTER[0] <= z["start_ts"].hour < MORGEN_FENSTER[1]:
            morgen.append(z)
    out["morgen_zyklen"] = morgen

    # --- verpasste PV nach BOILERMAX (Kuehlphase) ---
    boiler_max_ts = [e[0] for e in ereignisse if e[1][0] == "WARN_BOILERMAX"]
    verpasst = []
    for bts in boiler_max_ts:
        gap_end = None
        for z in zyklen:
            if z["start_ts"] > bts:
                gap_end = z["start_ts"]
                break
        if gap_end is None or (gap_end - bts) > timedelta(hours=12):
            gap_end = bts + timedelta(hours=12)  # Analyse auf 12 h begrenzen
        pv_min = 0.0
        for i, s in enumerate(snapshots):
            if s["ts"] < bts or s["ts"] > gap_end or s["status_ein"]:
                continue
            if (s.get("feedin_w") or 0) >= PV_VERPASST_AB_W:
                nxt = (snapshots[i + 1]["ts"] if i + 1 < len(snapshots) and
                       snapshots[i + 1]["ts"] <= gap_end else gap_end)
                dt = min((nxt - s["ts"]).total_seconds(), 330.0)
                pv_min += max(dt, 0) / 60.0
        verpasst.append({
            "von": bts, "bis": gap_end,
            "dauer_min": round((gap_end - bts).total_seconds() / 60.0, 1),
            "pv_min_geschaeetzt": round(pv_min, 1),
        })
    out["verpasst_pv"] = verpasst

    # --- Kurzzyklen-Liste ---
    out["kurzzyklen"] = [z for z in zyklen if (z["dauer_min"] or 0) < KURZZYKLUS_MIN]

    # --- Ueberschreitungen (max_unten >= Schwelle) ---
    out["overshoot_zyklen"] = [z for z in zyklen
                               if z.get("max_unten") and z["max_unten"] >= OVERSHOOT_SCHWELLE_C]

    # --- ZU_FRUEH-Events mit Kontext ---
    zu_frueh = []
    for e in ereignisse:
        if e[1][0] == "WARN_ZU_FRUEH":
            zu_frueh.append({"ts": e[0], "msg": e[1][2]})
    out["zu_frueh"] = zu_frueh

    # --- Regeln: EIN-Verteilungen (aus allen Snapshot-Bloecken) ---
    regel_ein = Counter()
    regel_aus = Counter()
    for s in snapshots:
        for r in s["regeln"]:
            if r["verdict"] == "EIN":
                regel_ein[r["name"]] += 1
            elif r["verdict"] == "AUS":
                regel_aus[r["name"]] += 1
    out["regel_ein"] = regel_ein
    out["regel_aus"] = regel_aus

    # --- Level-/Volumen-Statistik ---
    level = Counter()
    for s in snapshots:
        level[s["level"]] += 1
    for e in ereignisse:
        level[e[1][1]] += 1
    out["level"] = level
    out["stat_ereignis_codes"] = Counter(e[1][0] for e in ereignisse)

    # --- Stale-Daten-Phasen (Empfehlung 3.5): Zeitraeume, in denen auf
    # veralteten Solax-Daten entschieden wurde (WARN "Solar-Daten veraltet",
    # wird waehrend der Stillstand-Phase alle 5 min getaktet). ---
    stale_events = [e for e in ereignisse if e[1][0] == "WARN_SOLAR_STALE"]
    stale_pro_tag = Counter(e[0].strftime("%Y-%m-%d") for e in stale_events)
    out["stale_pro_tag"] = stale_pro_tag
    out["stale_gesamt"] = len(stale_events)
    return out


def als_dict_ohne_ts(obj):
    """CSV-freundliche Aufbereitung eines Zyklus (datetime -> str)."""
    if isinstance(obj, dict):
        return {k: als_dict_ohne_ts(v) for k, v in obj.items() if k not in ("sensoren",)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ------------------------------------------------------- Ausgabe/CSV ---- #
def _schreibe_csv(pfad, zeilen, felder):
    with open(pfad, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=felder, delimiter=";")
        w.writeheader()
        for z in zeilen:
            w.writerow({k: z.get(k) for k in felder})


def export_csve(parsed, out, outdir):
    os.makedirs(outdir, exist_ok=True)

    # 1) Zyklus-Tabelle
    felder = ["start", "ende", "dauer_min", "quelle", "start_regel", "end_grund",
              "start_unten", "max_unten", "ueberschreitung_k", "start_verd"]
    zyklen = []
    for z in parsed["zyklen"]:
        s0 = z["start_t_unten"]
        zyklen.append({
            "start": z["start_ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "ende": (z["end_ts"].strftime("%Y-%m-%d %H:%M:%S") if z["end_ts"] else ""),
            "dauer_min": z["dauer_min"], "quelle": z["quelle"],
            "start_regel": z["start_regel"], "end_grund": z["end_grund"],
            "start_unten": s0, "max_unten": z["max_unten"],
            "ueberschreitung_k": z["ueberschreitung_k"], "start_verd": z["start_t_verd"],
        })
    _schreibe_csv(os.path.join(outdir, "zyklen.csv"), zyklen, felder)

    # 2) Entscheidungs-Snapshots (Kontext-Anreicherung)
    felder = ["ts", "status_ein", "regel", "modus", "blocking", "pv_w", "soc",
              "feedin_w", "forecast_morgen_whqm", "soll_c", "unten", "oben", "mittig"]
    snaps = []
    for s in parsed["snapshots"]:
        sens = s.get("sensoren") or {}
        snaps.append({
            "ts": s["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "status_ein": "EIN" if s["status_ein"] else "AUS",
            "regel": s["regel"], "modus": s["modus"], "blocking": (s["blocking"] or "")[:60],
            "pv_w": s["pv_w"], "soc": s["soc"], "feedin_w": s["feedin_w"],
            "forecast_morgen_whqm": s["forecast_morgen_whqm"], "soll_c": s["soll_c"],
            "unten": sens.get("unten"), "oben": sens.get("oben"), "mittig": sens.get("mittig"),
        })
    _schreibe_csv(os.path.join(outdir, "entscheidungen_kontext.csv"), snaps, felder)

    # 3) Tages-KPIs
    felder = ["tag", "starts", "laufzeit_min", "netz_min", "pv_bat_min",
              "kurzzylen", "boiler_max", "overshoot", "forecast_today", "forecast_tomorrow"]
    tage_sort = sorted(out["tage"].items())
    _schreibe_csv(os.path.join(outdir, "tagesuebersicht.csv"),
                  [{"tag": k, **v} for k, v in tage_sort], felder)

    # 4) Morgen-Netzbezug vs. Prognose
    felder = ["tag", "start", "regel", "quelle", "netzbezug_w", "forecast_today"]
    morgen = []
    for z in out["morgen_zyklen"]:
        tag = z["start_ts"].strftime("%Y-%m-%d")
        morgen.append({
            "tag": tag, "start": z["start_ts"].strftime("%H:%M"),
            "regel": z["start_regel"], "quelle": z["quelle"], "netzbezug_w": "",
            "forecast_today": (parsed["forecast_pro_tag"].get(tag) or {}).get("today"),
        })
    _schreibe_csv(os.path.join(outdir, "morgen_netzbezug.csv"), morgen, felder)

    # 5) Ereignisse (stabile Codes)
    felder = ["ts", "code", "level", "detail"]
    ev = [{
        "ts": e[0].strftime("%Y-%m-%d %H:%M:%S"),
        "code": e[1][0], "level": e[1][1], "detail": (e[1][2] or "")[:150],
    } for e in parsed["ereignisse"]]
    _schreibe_csv(os.path.join(outdir, "ereignisse.csv"), ev, felder)
    return os.listdir(outdir)


# ------------------------------------------------------------- Report ---- #
def erzeuge_bericht(parsed, out, outdir, stats_roh):
    l = []
    ap = l.append
    ap("# Verbesserter Log-Analysereport\n")
    ap(f"**Quelle:** `{os.path.basename(LOG_PFAD)}`  ")
    if parsed.get("git_version"):
        ap(f"**Git-Version (Start):** `{parsed['git_version']}`  ")
    ap(f"**Ausgabe:** `{outdir}`\n")

    ap("## 1. Log-Volumen & Verarbeitung\n")
    gesamt = stats_roh.get("zeilen_gesamt", 0)
    regelzeilen = parsed["stats"].get("regelzeilen", 0)
    ap(f"- Zeilen gesamt (Rohdatei): **{gesamt}**  ")
    ap(f"- Davon Regel-Detailzeilen (15er-Bloecke): **{regelzeilen}** = "
       f"**{100.0 * regelzeilen / gesamt:.1f} %** (Redundanz)")
    ap(f"- Regel-Bewertungs-Bloecke: **{parsed['stats'].get('snapshots')}**  ")
    ap(f"- Level: INFO {stats_roh.get('level_INFO', 0)} / "
       f"DEBUG {stats_roh.get('level_DEBUG', 0)} / "
       f"WARNING {stats_roh.get('level_WARNING', 0)} / "
       f"ERROR {stats_roh.get('level_ERROR', 0)}")

    ap("\n## 2. Tages-KPIs\n")
    ap("| Tag | Starts | Laufzeit (min) | davon Netz | davon PV/Bat | Kurzzyklen (<20m) | "
       "BoilerMax | Overshoot | Prognose heute |")
    ap("|---|---|---|---|---|---|---|---|---|")
    for tag, t in sorted(out["tage"].items()):
        fc = t.get("forecast_today")
        fc_s = (f"{fc:.2f}" if fc is not None else "–")
        ap(f"| {tag} | {t['starts']} | {t['laufzeit_min']:.0f} | {t['netz_min']:.0f} | "
           f"{t['pv_bat_min']:.0f} | {t['kurzzylen']} | {t['boiler_max']} | "
           f"{t['overshoot']} | {fc_s} |")

    ap("\n## 3. Zyklen-Analyse\n")
    zyklen = parsed["zyklen"]
    ap(f"- Zyklen gesamt: **{len(zyklen)}**")
    dauren = [z["dauer_min"] or 0 for z in zyklen]
    if dauren:
        ap(f"- Gesamtlaufzeit: **{sum(dauren):.0f} min** "
           f"(= {sum(dauren) / 60:.1f} h)", )
        ap(f"- Median: **{sorted(dauren)[len(dauren) // 2]:.0f} min**, "
           f"min {min(dauren):.0f}, max {max(dauren):.0f}")
    q = Counter(z.get("quelle") for z in zyklen)
    ap(f"- Quelle: {dict(q)}")
    g = Counter(z.get("end_grund") for z in zyklen)
    ap(f"- Abschaltgruende: {dict(g)}")
    kz = out["kurzzyklen"]
    ap(f"- **Kurzzyklen (< {KURZZYKLUS_MIN:.0f} min): {len(kz)}** "
       f"({100.0 * len(kz) / max(len(zyklen), 1):.0f} %)")
    for z in kz[:12]:
        ap(f"    - {z['start_ts'].strftime('%d.%m %H:%M')} | {z['dauer_min']:.0f} min | "
           f"Regel {z['start_regel']} | Ende: {z['end_grund']}")

    ap("\n## 4. Overshoot / BOILERMAX (Kritischste Stelle)\n")
    osz = out["overshoot_zyklen"]
    ap(f"- Zyklen mit `unten max >= {OVERSHOOT_SCHWELLE_C}°C`: **{len(osz)}**")
    for z in sorted(osz, key=lambda x: x.get("ueberschreitung_k") or 0, reverse=True):
        ap(f"    - {z['start_ts'].strftime('%d.%m %H:%M')} | Dauer {z['dauer_min']} min | "
           f"max unten {z['max_unten']}°C | Regeln {z['start_regel']} -> {z['end_grund']}")
    vp = out["verpasst_pv"]
    if vp:
        ap("- **Geschaetzte verschenkte PV-Nutzung nach BOILERMAX:**")
        for v in vp:
            ap(f"    - {v['von'].strftime('%d.%m %H:%M')}–{v['bis'].strftime('%H:%M')}: "
               f"Kuehlphase {v['dauer_min']} min, davon schaetzbar "
               f"**{v['pv_min_geschaeetzt']} min** mit >= {PV_VERPASST_AB_W:.0f} W Einspeisung")

    ap("\n## 5. Morgendlicher Netzbezug vs. Prognose\n")
    ap("| Tag | Start | Start-Regel | Quelle | Prognose heute (kWh/m²) |")
    ap("|---|---|---|---|---|")
    for z in out["morgen_zyklen"]:
        tag = z["start_ts"].strftime("%Y-%m-%d")
        fc = (parsed["forecast_pro_tag"].get(tag) or {}).get("today")
        fc_s = (f"{fc:.2f}" if fc is not None else "–")
        ap(f"| {tag} | {z['start_ts'].strftime('%H:%M')} | {z['start_regel']} | "
           f"{z['quelle']} | {fc_s} |")

    ap("\n## 6. ZU-FRUEH-Ereignisse (Lernsignal)\n")
    for z in out["zu_frueh"]:
        ap(f"- {z['ts'].strftime('%d.%m %H:%M:%S')}: {z['msg'][:130]}")

    ap("\n## 7. Warnungen/Fehler (stabile Codes)\n")
    ap("| Code | Anzahl | | Code | Anzahl |")
    ap("|---|---|---|---|---|")
    codes = sorted(out["stat_ereignis_codes"].items(), key=lambda x: -x[1])
    mid = (len(codes) + 1) // 2
    for i in range(mid):
        c1, n1 = codes[i]
        row = f"| {c1} | {n1} |"
        if i + mid < len(codes):
            c2, n2 = codes[i + mid]
            row += f" {c2} | {n2} |"
        ap(row)

    ap("\n### 7b. Stale-Daten-Phasen (Entscheidungen auf veralteten PV-Daten)\n")
    gesamt_stale = out.get("stale_gesamt", 0)
    ap(f"- Warnungen 'Solar-Daten veraltet' (waehrend Ausfall alle 5 min): "
       f"**{gesamt_stale}**")
    if out.get("stale_pro_tag"):
        ap("| Tag | Stale-Warnungen |")
        ap("|---|---|")
        for tag, n in sorted(out["stale_pro_tag"].items()):
            ap(f"| {tag} | {n} |")
    else:
        ap("- Keine Stale-Phasen im analysierten Zeitraum.")

    ap("\n## 8. Regel-Einschalt-Haeufigkeit (alle Bewertungen)\n")
    ap("| Regel | EIN | AUS |")
    ap("|---|---|---|")
    for name in sorted(set(out["regel_ein"]) | set(out["regel_aus"]),
                       key=lambda n: -out["regel_ein"][n]):
        ap(f"| {name} | {out['regel_ein'][name]} | {out['regel_aus'][name]} |")

    md = "\n".join(l) + "\n"
    with open(os.path.join(outdir, "analyse_bericht.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return md


def main():
    print(f"Parsee {LOG_PFAD} ...")
    parsed = parse_log(LOG_PFAD)
    out = analysiere(parsed)
    os.makedirs(AUSGABE_DIR, exist_ok=True)
    export_csve(parsed, out, AUSGABE_DIR)
    bericht = erzeuge_bericht(parsed, out, AUSGABE_DIR, parsed["stats"])
    print(bericht)
    print(f"\n-> Fertig: {AUSGABE_DIR}")


if __name__ == "__main__":
    main()