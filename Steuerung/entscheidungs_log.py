# -*- coding: utf-8 -*-
"""Entscheidungs-Log und Energiebilanz (KPIs) der WP-Steuerung.

Schreibt pro Regelzyklus eine Zeile als JSON Lines (entscheidungs_log.jsonl):
    {"ts": "...", "gewinner": "...", "grund": "...", "soll_einschalten": bool,
     "kompressor_laeuft": bool, "feedin_w": float, "batpower_w": float,
     "soc": float, "t_unten": float, "t_oben": float}

Daraus leitet sich ab:
- die Entscheidungs-Historie fuer API/Webapp ("Warum lief die WP um 3 Uhr?")
- Tages-/Wochen-KPIs: WP-Energie und Anteil PV/Batterie vs. Netz

Schreibstrategie (gegen Spam bei identischen Zyklen):
- geschrieben wird nur bei AENDERUNG der Entscheidung (gewinner /
  soll_einschalten / kompressor_laeuft) oder als Herzschlag alle
  HEARTBEAT_SEKUNDEN, solange die WP laeuft (fuer exakte Energiebilanz).
- Im Webapp erscheinen dadurch echte Umschaltzeitpunkte statt Wiederholungen.

Klassifikation der Stromquelle (vereinfacht, dokumentiert):
- feedin >= NETZKAUF_GRENZE_W  -> "pv_batterie" (kein nennenswerter Netzkauf)
- feedin <  NETZKAUF_GRENZE_W  -> "netz"
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

LOG_DATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entscheidungs_log.jsonl")
MAX_BYTES = 2_000_000          # Rotation bei ~2 MB -> .old
MAX_EINTRAEGE_LESEN = 5000
NETZKAUF_GRENZE_W = -50.0      # darunter gilt: Haus kauft Netzstrom
HEARTBEAT_SEKUNDEN = 75.0      # Zwangsschreibintervall laufender WP (< dt-Cap 120 s)

# Cache der zuletzt geschriebenen Zeile (pfad-gebunden, damit Tests mit
# umgeleitetem LOG_DATEI nicht gegenseitig stoeren).
_cache_pfad: Optional[str] = None
_cache_zeile: Optional[Dict] = None


def _letzte_logzeile() -> Optional[Dict]:
    """Letzte geschriebene Logzeile oder None (liest nur das Dateiende)."""
    global _cache_zeile
    if _cache_pfad == LOG_DATEI and _cache_zeile is not None:
        return _cache_zeile
    try:
        if not os.path.exists(LOG_DATEI):
            return None
        with open(LOG_DATEI, "rb") as f:
            f.seek(0, os.SEEK_END)
            groesse = f.tell()
            block = min(groesse, 8192)
            f.seek(groesse - block)
            daten = f.read().decode("utf-8", errors="replace")
        zeilen = [z for z in daten.strip().splitlines() if z.strip()]
        if not zeilen:
            return None
        return json.loads(zeilen[-1])
    except (OSError, json.JSONDecodeError):
        return None


def _soll_schreiben(vorher: Optional[Dict], eintrag: Dict) -> bool:
    """True bei Entscheidungsaenderung oder Herzschlag laufender WP."""
    if vorher is None:
        return True
    identisch = (
        vorher.get("gewinner") == eintrag.get("gewinner")
        and vorher.get("soll_einschalten") == eintrag.get("soll_einschalten")
        and vorher.get("kompressor_laeuft") == eintrag.get("kompressor_laeuft")
    )
    if not identisch:
        return True
    if not eintrag.get("kompressor_laeuft"):
        return False  # Stillstand: identische Zyklen nicht weiterschreiben
    try:
        dt = (datetime.fromisoformat(eintrag["ts"])
              - datetime.fromisoformat(vorher["ts"])).total_seconds()
        return dt >= HEARTBEAT_SEKUNDEN
    except (KeyError, ValueError):
        return True  # im Zweifel lieber schreiben als Zustand verlieren


def schreibe_eintrag(
    gewinner_name: Optional[str],
    gewinner_grund: str,
    soll_einschalten: bool,
    kompressor_laeuft: bool,
    feedin_watt: Optional[float] = None,
    batpower_watt: Optional[float] = None,
    soc: Optional[float] = None,
    t_unten: Optional[float] = None,
    t_oben: Optional[float] = None,
) -> bool:
    """Haengt einen Zyklus-Eintrag ans JSONL-Log (nur bei Aenderung/Herzschlag).

    Rueckgabe: True, wenn geschrieben wurde; False bei unterdruecktem Duplikat.
    """
    eintrag = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "gewinner": gewinner_name or "",
        "grund": (gewinner_grund or "")[:200],
        "soll_einschalten": bool(soll_einschalten),
        "kompressor_laeuft": bool(kompressor_laeuft),
        "feedin_w": round(float(feedin_watt), 1) if feedin_watt is not None else None,
        "batpower_w": round(float(batpower_watt), 1) if batpower_watt is not None else None,
        "soc": round(float(soc), 1) if soc is not None else None,
        "t_unten": round(float(t_unten), 2) if t_unten is not None else None,
        "t_oben": round(float(t_oben), 2) if t_oben is not None else None,
    }
    try:
        global _cache_pfad, _cache_zeile
        vorher = _letzte_logzeile()
        if not _soll_schreiben(vorher, eintrag):
            return False
        if os.path.exists(LOG_DATEI) and os.path.getsize(LOG_DATEI) > MAX_BYTES:
            alt = LOG_DATEI + ".old"
            if os.path.exists(alt):
                os.remove(alt)
            os.replace(LOG_DATEI, alt)
            _cache_zeile = None
            logging.info("Entscheidungslog rotiert auf .old")
        with open(LOG_DATEI, "a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
        _cache_pfad = LOG_DATEI
        _cache_zeile = eintrag
        return True
    except OSError as e:
        # Logging darf den Regelbetrieb niemals stoeren
        logging.debug(f"Entscheidungslog nicht schreibbar: {e}")
        return False


def _lies_zeilen() -> List[Dict]:
    """Liest aktuelles + rotiertes Log, aelteste zuerst."""
    zeilen: List[Dict] = []
    for pfad in (LOG_DATEI + ".old", LOG_DATEI):
        if not os.path.exists(pfad):
            continue
        try:
            with open(pfad, encoding="utf-8") as f:
                for z in f:
                    z = z.strip()
                    if not z:
                        continue
                    try:
                        zeilen.append(json.loads(z))
                    except json.JSONDecodeError:
                        continue  # abgebrochene letzte Zeile tolerieren
        except OSError as e:
            logging.debug(f"Entscheidungslog nicht lesbar ({pfad}): {e}")
    return zeilen[-MAX_EINTRAEGE_LESEN:]


def historie(stunden: float = 24, limit: int = 100) -> List[Dict]:
    """Letzte Entscheidungen (neueste zuerst) fuer API/Webapp."""
    grenze = datetime.now() - timedelta(hours=stunden)
    auswahl = []
    for e in _lies_zeilen():
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            continue
        if ts >= grenze:
            auswahl.append(e)
    return list(reversed(auswahl[-limit:]))


def _aggregiere(eintraege: List[Dict], wp_leistung_watt: float,
                strompreis_eur_kwh: float) -> Dict:
    """Aggregiert Laufzeit/Energie/Quellenanteile ueber Logzeilen.

    Zeitspanne je Intervall = Abstand zur Vorgaengerzeile (max. 120 s
    angesetzt). Zugerechnet wird der Zustand der VORGAENGERZEILE, da dieser
    waehrend des gesamten Intervalls galt - wichtig bei aenderungsbasiertem
    Schreiben, damit der WP-Start keine Phantom-Minuten aus der Stillstandsluecke
    erzeugt.
    """
    laufzeit_s = {"pv_batterie": 0.0, "netz": 0.0}
    vorher_ts: Optional[datetime] = None
    vorher_laeuft = False
    vorher_feedin: Optional[float] = None
    for e in eintraege:
        try:
            ts = datetime.fromisoformat(e["ts"])
        except (KeyError, ValueError):
            vorher_ts = None
            vorher_laeuft = False
            continue
        dt_s = 0.0
        if vorher_ts is not None:
            dt_s = min(max((ts - vorher_ts).total_seconds(), 0.0), 120.0)
        if vorher_ts is not None and vorher_laeuft:
            quelle = ("netz" if vorher_feedin is not None
                      and vorher_feedin < NETZKAUF_GRENZE_W else "pv_batterie")
            laufzeit_s[quelle] += dt_s
        vorher_ts = ts
        vorher_laeuft = bool(e.get("kompressor_laeuft"))
        vorher_feedin = e.get("feedin_w")

    gesamt_min = sum(laufzeit_s.values()) / 60.0
    energie_kwh = {q: s / 3600.0 * wp_leistung_watt / 1000.0 for q, s in laufzeit_s.items()}
    netz_kwh = energie_kwh["netz"]
    pvb_kwh = energie_kwh["pv_batterie"]
    gesamt_kwh = pvb_kwh + netz_kwh
    anteil_pv = round(100.0 * pvb_kwh / gesamt_kwh, 1) if gesamt_kwh > 0 else None
    return {
        "laufzeit_min": round(gesamt_min, 1),
        "energie_kwh": round(gesamt_kwh, 2),
        "anteil_pv_batterie_prozent": anteil_pv,
        "netz_kwh": round(netz_kwh, 2),
        "kosten_netz_eur": round(netz_kwh * strompreis_eur_kwh, 2),
    }


def kpis(wp_leistung_watt: float = 600.0, strompreis_eur_kwh: float = 0.35) -> Dict:
    """KPIs fuer heute und die letzten 7 Tage."""
    alle = _lies_zeilen()
    heute_str = datetime.now().strftime("%Y-%m-%d")

    def _von(bis_stunden: float) -> List[Dict]:
        grenze = datetime.now() - timedelta(hours=bis_stunden)
        out = []
        for e in alle:
            try:
                if datetime.fromisoformat(e["ts"]) >= grenze:
                    out.append(e)
            except (KeyError, ValueError):
                continue
        return out

    heute = [e for e in alle if e.get("ts", "").startswith(heute_str)]
    return {
        "heute": _aggregiere(heute, wp_leistung_watt, strompreis_eur_kwh),
        "sieben_tage": _aggregiere(_von(24 * 7), wp_leistung_watt, strompreis_eur_kwh),
    }
