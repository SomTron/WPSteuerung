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
- geschrieben wird bei AENDERUNG der HANDLUNG (soll_einschalten /
  kompressor_laeuft) oder als Herzschlag: alle HEARTBEAT_SEKUNDEN, solange
  die WP laeuft (fuer exakte Energiebilanz), bzw. alle
  HEARTBEAT_STILLSTAND_SEKUNDEN im Stillstand (Lebenszeichen).
- Ein blosser Wechsel der GEWINNER-Regel bei unveraenderter Handlung ist
  KEIN eigener Eintrag mehr, sondern wird als pending-Regelwechsel gesammelt
  und der naechsten Zeile in diagnostics.bei_laueufigem_regelwechsel
  beigefuegt. Im Log 18.09.-01.10.2026 waren das 209 von 2.950 Zeilen (7,1 %)
  reines Rauschen (Median-Abstand 119 s, Minimum 10 s).
- Im Webapp erscheinen dadurch echte Umschaltzeitpunkte statt Wiederholungen.

Lebenszeichen im Stillstand: ohne Eintrag ist ein langer Stillstand von einem
abgestuerzten Dienst nicht unterscheidbar - im Log 18.09.-01.10.2026 standen
12,9 h ohne Zeile, deren Vorzeile noch "kompressor_laeuft: true" behauptete.

Klassifikation der Stromquelle (Schwellen in constants.py). `feedin` ist
die Leistung INS NETZ; die Anlage ist wechselrichtergekoppelt, d. h. PV
speist erst Haus und WP, der Rest laedt die Batterie:
- "pv"        -> feedin >= PV_UEBERSCHUSS_MIN_W  (echter Export) ODER die
                 Batterie laedt (batpower > BATTERIE_LADUNG_MIN_W: die
                 Batterie kann nur aus PV-Ueberschuss speisen, also deckt
                 die PV auch den WP-Lauf - auch bei feedin = 0)
- "batterie"  -> kein Ueberschuss, aber batpower < 0 (Batterie speist)
- "netz"      -> feedin < NETZKAUF_GRENZE_W      (Haus kauft Netzstrom)
- "unklar"    -> weder PV noch Batterie nachweisbar. Bleibt ehrlicherweise
                 offen statt pauschal als PV verbucht zu werden.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz

from constants import (
    BATTERIE_LADUNG_MIN_W,
    DEFAULT_TIMEZONE,
    NETZKAUF_GRENZE_W,
    PV_UEBERSCHUSS_MIN_W,
    QUELLE_UNKLAR_MAX_W,
)

LOG_DATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entscheidungs_log.jsonl")
MAX_BYTES = 2_000_000          # Rotation bei ~2 MB -> naechste Generation
MAX_GENERATIONEN = 7           # .old.1 ... .old.7, ~4 Monate Historie
MAX_EINTRAEGE_LESEN = 5000
HEARTBEAT_SEKUNDEN = 75.0      # Zwangsschreibintervall laufender WP (< dt-Cap 120 s)
HEARTBEAT_STILLSTAND_SEKUNDEN = 1_800.0  # Lebenszeichen bei Stillstand (30 min)
LOG_TIMEZONE = pytz.timezone(DEFAULT_TIMEZONE)
IDLE_GEWINNER = "Idle"         # keine Regel fordert Heizbetrieb

# Quellnamen der Energiebilanz
QUELLE_PV = "pv"
QUELLE_BATTERIE = "batterie"
QUELLE_NETZ = "netz"
QUELLE_UNKLAR = "unklar"


def _parse_log_datetime(raw) -> Optional[datetime]:
    """Parst alte naive und neue ISO-Zeitstempel in dieselbe Zeitzone."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if value.tzinfo is None:
        return LOG_TIMEZONE.localize(value)
    return value.astimezone(LOG_TIMEZONE)


def _log_now() -> datetime:
    return datetime.now(LOG_TIMEZONE)


def _zahl(wert) -> str:
    """Formatiert einen Messwert fuer den Grundtext (ohne 'None')."""
    if wert is None:
        return "-"
    try:
        return f"{float(wert):g}"
    except (TypeError, ValueError):
        return "-"

# Cache der zuletzt geschriebenen Zeile (pfad-gebunden, damit Tests mit
# umgeleitetem LOG_DATEI nicht gegenseitig stoeren).
_cache_pfad: Optional[str] = None
_cache_zeile: Optional[Dict] = None
# Gesammelte Gewinner-Wechsel OHNE Handlungsaenderung, bis zur naechsten
# geschriebenen Zeile (pfad-gebunden wie der Cache darueber).
_pending_pfad: Optional[str] = None
_pending_regelwechsel: List[Dict] = []
MAX_PENDING_REGELWECHSEL = 20  # verhindert unbegrenztes Wachstum im Stillstand


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


def klassifiziere_quelle(feedin_w, batpower_w) -> str:
    """Ordnet einer laufenden WP die Stromquelle zu.

    `feedin_w` ist die Leistung INS NETZ (nicht die PV-Erzeugung). Die Anlage
    ist wechselrichtergekoppelt: PV speist erst Haus und WP, der Rest laedt die
    Batterie, erst der Rest davon wird eingespeist. Deshalb bedeutet
    feedin = 0 bei LADENDER Batterie einen solar gedeckten WP-Lauf - die
    haeufigste und energieguenstigste Betriebsart des Tages.

    Ohne diesen Zweig wurde genau dieser Fall als "unklar" ausgewiesen. Im Log
    vom 02.10.2026 (08:28-09:24, 56 min, SOC 65 -> 72 %) fielen dadurch 79,6 %
    der Tageslaufzeit aus dem Solarantial heraus, obwohl die PV den Lauf
    getragen hat.

    Rueckwaertskompatibel zur Zweiteilung, aber ehrlicher: die alte Regel
    ("feedin >= -50 -> pv_batterie") verbuchte auch 0 W ohne jede Batterie-
    Bewegung als Solarstrom und lieferte dadurch 100 % PV-Anteil an 13 von
    14 Tagen. Jetzt wird die vierte Stufe "unklar" ausgewiesen, statt sie als
    PV zu verbuchen. Alt-Datensaetze ohne Batterie-Feld werden nachgerechnet:
    batpower=None verhaelt sich wie 0 (kein Batterie-Nachweis).
    """
    if feedin_w is None:
        return QUELLE_UNKLAR
    if feedin_w >= PV_UEBERSCHUSS_MIN_W:
        return QUELLE_PV
    if feedin_w < NETZKAUF_GRENZE_W:
        return QUELLE_NETZ
    # Zwischen Netzkaufgrenze und echtem Ueberschuss: die Batterie entscheidet,
    # ob Solarstrom im Spiel war.
    if batpower_w is not None and batpower_w < 0:
        return QUELLE_BATTERIE
    if batpower_w is not None and batpower_w > BATTERIE_LADUNG_MIN_W:
        return QUELLE_PV
    if feedin_w <= QUELLE_UNKLAR_MAX_W:
        return QUELLE_UNKLAR
    return QUELLE_BATTERIE


def _soll_schreiben(vorher: Optional[Dict], eintrag: Dict) -> bool:
    """True bei Handlungsaenderung oder Herzschlag.

    Ein blosser Wechsel der Gewinner-Regel bei UNVERAENDERTER Handlung
    erzeugt keinen eigenen Eintrag mehr (Log-Rauschen, siehe Modul-Docstring).
    Der Aufrufer sammelt solche Wechsel in `_pending_regelwechsel` und haengt
    sie der naechsten geschriebenen Zeile an.
    """
    if vorher is None:
        return True
    handlung_unveraendert = (
        vorher.get("soll_einschalten") == eintrag.get("soll_einschalten")
        and vorher.get("kompressor_laeuft") == eintrag.get("kompressor_laeuft")
    )
    gewinner_unveraendert = vorher.get("gewinner") == eintrag.get("gewinner")
    if not handlung_unveraendert:
        return True
    if not gewinner_unveraendert:
        return False  # nur Regel-Wechsel -> an naechste Zeile haengen
    # Zustand stabil: nur nach Herzschlag-Intervall erneut schreiben.
    grenze = (
        HEARTBEAT_SEKUNDEN
        if eintrag.get("kompressor_laeuft")
        else HEARTBEAT_STILLSTAND_SEKUNDEN
    )
    try:
        aktuell = _parse_log_datetime(eintrag.get("ts"))
        vorher_ts = _parse_log_datetime(vorher.get("ts"))
        if aktuell is None or vorher_ts is None:
            return True
        dt = (aktuell - vorher_ts).total_seconds()
        return dt >= grenze
    except (TypeError, ValueError):
        return True  # im Zweifel lieber schreiben als Zustand verlieren


def schreibe_eintrag(
    gewinner_name: Optional[str],
    gewinner_grund: str,
    soll_einschalten: bool,
    kompressor_laeuft: bool,
    feedin_watt: Optional[float] = None,
    batpower_watt: Optional[float] = None,
    pv_acpower_watt: Optional[float] = None,
    pv_erzeugung_watt: Optional[float] = None,
    hausverbrauch_watt: Optional[float] = None,
    pv_ueberschuss_watt: Optional[float] = None,
    soc: Optional[float] = None,
    t_unten: Optional[float] = None,
    t_oben: Optional[float] = None,
    t_mitte: Optional[float] = None,
    t_verdampfer: Optional[float] = None,
    setpoint_ein: Optional[float] = None,
    setpoint_aus: Optional[float] = None,
    stale_s: Optional[int] = None,
    reason_code: Optional[str] = None,
    diagnostics: Optional[Dict] = None,
    ts: Optional[datetime] = None,
) -> bool:
    """Haengt einen Zyklus-Eintrag ans JSONL-Log (nur bei Aenderung/Herzschlag).

    stale_s: Alter der Solax-Daten in Sekunden (None = unbekannt/frisch).
             Ermoeglicht spaetere Analysen, Entscheidungen auf veralteten
             PV-Daten zu erkennen (Empfehlung 3.5).

    Rueckgabe: True, wenn geschrieben wurde; False bei unterdruecktem Duplikat.

    t_mitte/t_verdampfer und die beiden Setpoints ergaenzen die bislang
    fehlenden Sichtachsen: Die Schichtungslogik entscheidet an `mittig`
    (im Log 18.09.-01.10.2026 in 103 Zeilen als "Schichtungs-Start"
    genannt), ohne diesen Wert war sie aus dem Log nicht nachvollziehbar.
    Die Setpoints zeigen, welche Schwellen die Regel tatsaechlich angewandt
    hat - aus der 48-C-Pufferregel allein war das nicht ableitbar.

    `pv_acpower_watt` ist die PV-ERZEUGUNG und wird bewusst getrennt von
    `feedin_watt` (Einspeisung ins Netz) gefuehrt. Die Quellenpruefung der
    Schichtungsregel arbeitet mit der Erzeugung, nicht mit der Einspeisung;
    ohne dieses Feld war aus dem Log nicht erkennbar, ob eine Freigabe auf
    tatsaechlicher PV-Leistung oder nur auf einem Exporterfolg beruhte.

    `hausverbrauch_watt` und `pv_ueberschuss_watt` sind ABGELEITET aus der
    Wechselrichterbilanz (energy_source.hausverbrauch_watt). Der
    Ueberschuss sagt, wieviel PV-Leistung nach Deckung des Hauses fuer die
    Waermepumpe uebrig bleibt - die eigentliche Groesse fuer eine
    solarbetriebene Zuschaltung.
    """
    # Leerer Gewinner/Grund waere im Log nicht interpretierbar ("Dienst laeuft,
    # aber keine Regel will" vs. "Regel hat entschieden: AUS"). Deshalb wird
    # der Idle-Zustand explizit benannt - im Log 18.09.-01.10.2026 gab es 63
    # solche Zeilen, davon 39 bei laufender WP.
    if not gewinner_name:
        gewinner_name = IDLE_GEWINNER
    if not gewinner_grund:
        gewinner_grund = (
            "keine Regel fordert Heizbetrieb "
            f"(unten {_zahl(t_unten)}C, oben {_zahl(t_oben)}C, "
            f"PV-Erzeugung {_zahl(pv_acpower_watt)}W, "
            f"Ueberschuss {_zahl(pv_ueberschuss_watt)}W, "
            f"Haus {_zahl(hausverbrauch_watt)}W, SOC {_zahl(soc)}%)"
        )
    eintrag = {
        "ts": (ts or _log_now()).isoformat(timespec="seconds"),
        "gewinner": gewinner_name,
        "grund": gewinner_grund[:200],
        "soll_einschalten": bool(soll_einschalten),
        "kompressor_laeuft": bool(kompressor_laeuft),
        "feedin_w": round(float(feedin_watt), 1) if feedin_watt is not None else None,
        "batpower_w": round(float(batpower_watt), 1) if batpower_watt is not None else None,
        "pv_acpower_w": round(float(pv_acpower_watt), 1) if pv_acpower_watt is not None else None,
        # Die tatsaechliche Erzeugung (powerdc1 + powerdc2). `pv_acpower_w`
        # ist der Wechselstromausgang, also Haus plus Netz - nachts rund
        # 250 W, obwohl die Sonne nichts liefert. Beide Spalten bleiben
        # getrennt, sonst laesst sich der Unterschied spaeter nicht mehr
        # rekonstruieren.
        "pv_erzeugung_w": round(float(pv_erzeugung_watt), 1) if pv_erzeugung_watt is not None else None,
        "hausverbrauch_w": round(float(hausverbrauch_watt), 1) if hausverbrauch_watt is not None else None,
        "pv_ueberschuss_w": round(float(pv_ueberschuss_watt), 1) if pv_ueberschuss_watt is not None else None,
        "soc": round(float(soc), 1) if soc is not None else None,
        "t_unten": round(float(t_unten), 2) if t_unten is not None else None,
        "t_oben": round(float(t_oben), 2) if t_oben is not None else None,
        "t_mitte": round(float(t_mitte), 2) if t_mitte is not None else None,
        "t_verdampfer": round(float(t_verdampfer), 2) if t_verdampfer is not None else None,
        "setpoint_ein": round(float(setpoint_ein), 1) if setpoint_ein is not None else None,
        "setpoint_aus": round(float(setpoint_aus), 1) if setpoint_aus is not None else None,
        "stale_s": stale_s,
        "reason_code": reason_code,
        "diagnostics": dict(diagnostics) if isinstance(diagnostics, dict) else {},
    }
    try:
        global _cache_pfad, _cache_zeile, _pending_pfad, _pending_regelwechsel
        if _pending_pfad != LOG_DATEI:
            _pending_pfad = LOG_DATEI
            _pending_regelwechsel = []
        vorher = _letzte_logzeile()
        # Ist die Liste voller Regelwechsel ohne Handlungsaenderung, wird der
        # aktuelle Zustand erzwungen geschrieben. Sonst koennte das Log bei
        # dauerhaft alternierenden Regeln (z. B. PV-Schaping gegen Komfort)
        # vollstaendig verstummen - im Log 18.09.-01.10.2026 gab es 57 schnelle
        # Gegenwechsel, also genau diese Konstellation.
        erzwungen = len(_pending_regelwechsel) >= MAX_PENDING_REGELWECHSEL
        if not erzwungen and not _soll_schreiben(vorher, eintrag):
            # Regel-Wechsel ohne Handlungsaenderung: nicht als eigene Zeile
            # schreiben, sondern fuer die naechste Zeile vormerken.
            if vorher is not None and vorher.get("gewinner") != eintrag["gewinner"]:
                _pending_regelwechsel.append({
                    "ts": eintrag["ts"],
                    "von": vorher.get("gewinner") or IDLE_GEWINNER,
                    "nach": eintrag["gewinner"],
                })
                del _pending_regelwechsel[:-MAX_PENDING_REGELWECHSEL]
                # Der Cache muss den neuen Gewinner fuehren, sonst wiederholt
                # sich derselbe Wechsel bei jedem Takt.
                if _cache_pfad == LOG_DATEI and _cache_zeile is not None:
                    _cache_zeile = dict(_cache_zeile, gewinner=eintrag["gewinner"])
            return False
        # Gesammelte Regelwechsel an diese Zeile haengen.
        if _pending_regelwechsel:
            eintrag["diagnostics"]["bei_laueufigem_regelwechsel"] = list(_pending_regelwechsel)
            _pending_regelwechsel = []
        if os.path.exists(LOG_DATEI) and os.path.getsize(LOG_DATEI) > MAX_BYTES:
            _rotiere()
            _cache_zeile = None
        with open(LOG_DATEI, "a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
        _cache_pfad = LOG_DATEI
        _cache_zeile = eintrag
        return True
    except OSError as e:
        # Logging darf den Regelbetrieb niemals stoeren
        logging.debug(f"Entscheidungslog nicht schreibbar: {e}")
        return False


def _rotiere() -> None:
    """Schiebt das Log eine Generation weiter: .old.N-1 -> .old.N.

    Vorher gab es nur EINE Generation (.old), die bei jeder Rotation ueberschrieben
    wurde: bei ~227 Zeilen/Tag und 2 MB war das Historie von maximal ~16 Tagen -
    zu wenig fuer Saison- und Wettervergleiche. MAX_GENERATIONEN aelterer
    Dateien ergeben stattdessen rund vier Monate.
    """
    try:
        generationen = max(int(MAX_GENERATIONEN), 1)
        # aelteste Generation faellt raus
        veraltet = f"{LOG_DATEI}.old.{generationen}"
        if os.path.exists(veraltet):
            os.remove(veraltet)
        for n in range(generationen - 1, 0, -1):
            quelle = f"{LOG_DATEI}.old.{n}"
            if os.path.exists(quelle):
                os.replace(quelle, f"{LOG_DATEI}.old.{n + 1}")
        if os.path.exists(LOG_DATEI):
            os.replace(LOG_DATEI, f"{LOG_DATEI}.old.1")
        logging.info(f"Entscheidungslog rotiert auf .old.1 (max {generationen} Generationen)")
    except OSError as e:
        logging.debug(f"Entscheidungslog-Rotation nicht moeglich: {e}")


def _alle_logpfade() -> List[str]:
    """Alle Loggenerationen, aelteste zuerst (fuer _lies_zeilen)."""
    pfade: List[str] = []
    for n in range(MAX_GENERATIONEN, 0, -1):
        pfade.append(f"{LOG_DATEI}.old.{n}")
    pfade.append(LOG_DATEI)
    return pfade


def _lies_zeilen() -> List[Dict]:
    """Liest alle Log-Generationen, aelteste zuerst.

    Liest die Generationen von der JUENGSTEN zur aeltesten und bricht ab,
    sobald MAX_EINTRAEGE_LESEN Eintraege zusammen sind. Bei 7 Generationen
    x 2 MB waere ein vollstaendiges Einlesen auf dem Pi unnoetig teuer.
    """
    bloecke: List[List[Dict]] = []
    for pfad in reversed(_alle_logpfade()):  # juengste Datei zuerst
        if not os.path.exists(pfad):
            continue
        zeilen: List[Dict] = []
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
            continue
        if zeilen:
            # Die Datei wird von vorn gelesen, enthaelt also bereits
            # aelteres zuerst - die Reihenfolge bleibt unveraendert.
            bloecke.append(zeilen)
        if sum(len(b) for b in bloecke) >= MAX_EINTRAEGE_LESEN:
            break
    # Bloecke liegen juengste Datei zuerst -> fuer die Ausgabe umkehren.
    zeilen_gesamt = [z for b in reversed(bloecke) for z in b]
    return zeilen_gesamt[-MAX_EINTRAEGE_LESEN:]


def historie(stunden: float = 24, limit: int = 100) -> List[Dict]:
    """Letzte Entscheidungen (neueste zuerst) fuer API/Webapp."""
    grenze = _log_now() - timedelta(hours=stunden)
    auswahl = []
    for e in _lies_zeilen():
        ts = _parse_log_datetime(e.get("ts"))
        if ts is None:
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

    Quellen (siehe klassifiziere_quelle): "pv", "batterie", "netz", "unklar".
    Der Solaranteil wird ueber die GESAMTE Laufzeit berechnet: Laufzeit ohne
    PV- und ohne Batterie-Nachweis ("unklar") senkt ihn, statt als Solarstrom
    durchgewunken zu werden. Sonst stuende weiterhin jeden Tag "100 %"
    (historisch 13 von 14 Tage), obwohl real nur ~77 % Solarstrom waren.
    """
    laufzeit_s = {QUELLE_PV: 0.0, QUELLE_BATTERIE: 0.0,
                  QUELLE_NETZ: 0.0, QUELLE_UNKLAR: 0.0}
    vorher_ts: Optional[datetime] = None
    vorher_laeuft = False
    vorher_feedin: Optional[float] = None
    vorher_batpower: Optional[float] = None
    for e in eintraege:
        ts = _parse_log_datetime(e.get("ts"))
        if ts is None:
            vorher_ts = None
            vorher_laeuft = False
            continue
        dt_s = 0.0
        if vorher_ts is not None:
            dt_s = min(max((ts - vorher_ts).total_seconds(), 0.0), 120.0)
        if vorher_ts is not None and vorher_laeuft and dt_s > 0:
            laufzeit_s[klassifiziere_quelle(vorher_feedin, vorher_batpower)] += dt_s
        vorher_ts = ts
        vorher_laeuft = bool(e.get("kompressor_laeuft"))
        vorher_feedin = e.get("feedin_w")
        vorher_batpower = e.get("batpower_w")

    gesamt_min = sum(laufzeit_s.values()) / 60.0
    energie_kwh = {q: s / 3600.0 * wp_leistung_watt / 1000.0
                   for q, s in laufzeit_s.items()}
    netz_kwh = energie_kwh[QUELLE_NETZ]
    pv_kwh = energie_kwh[QUELLE_PV]
    batterie_kwh = energie_kwh[QUELLE_BATTERIE]
    unklar_kwh = energie_kwh[QUELLE_UNKLAR]
    solar_kwh = pv_kwh + batterie_kwh
    gesamt_kwh = solar_kwh + netz_kwh + unklar_kwh
    unklar_min = laufzeit_s[QUELLE_UNKLAR] / 60.0
    return {
        "laufzeit_min": round(gesamt_min, 1),
        "energie_kwh": round(gesamt_kwh, 2),
        # Der Nenner ist die GESAMTE Laufzeit - auch die unklaren Anteile.
        # Wuerde man sie herausrechnen, stuende bei jedem Tag "100 % PV" und
        # genau die Luecke verdeckt, die man aufdecken wollte (real ~77 %
        # Solar plus ~23 % ohne Nachweis statt pauschal 100 %).
        "anteil_pv_batterie_prozent": (
            round(100.0 * solar_kwh / gesamt_kwh, 1) if gesamt_kwh > 0 else None
        ),
        "pv_kwh": round(pv_kwh, 2),
        "batterie_kwh": round(batterie_kwh, 2),
        "netz_kwh": round(netz_kwh, 2),
        "unklar_kwh": round(unklar_kwh, 2),
        "unklar_laufzeit_min": round(unklar_min, 1),
        "anteil_unklar_prozent": (
            round(100.0 * unklar_kwh / gesamt_kwh, 1) if gesamt_kwh > 0 else None
        ),
        "kosten_netz_eur": round(netz_kwh * strompreis_eur_kwh, 2),
    }


def kpis(wp_leistung_watt: float = 600.0, strompreis_eur_kwh: float = 0.35) -> Dict:
    """KPIs fuer heute und die letzten 7 Tage."""
    alle = _lies_zeilen()
    jetzt = _log_now()
    heute_str = jetzt.strftime("%Y-%m-%d")

    def _von(bis_stunden: float) -> List[Dict]:
        grenze = jetzt - timedelta(hours=bis_stunden)
        out = []
        for e in alle:
            ts = _parse_log_datetime(e.get("ts"))
            if ts is not None and ts >= grenze:
                out.append(e)
        return out

    heute = [e for e in alle if e.get("ts", "").startswith(heute_str)]
    return {
        "heute": _aggregiere(heute, wp_leistung_watt, strompreis_eur_kwh),
        "sieben_tage": _aggregiere(_von(24 * 7), wp_leistung_watt, strompreis_eur_kwh),
    }
