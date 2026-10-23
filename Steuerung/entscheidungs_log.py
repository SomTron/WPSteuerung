# -*- coding: utf-8 -*-
"""Entscheidungs-Log und Energiebilanz (KPIs) der WP-Steuerung.

Schreibt pro Regelzyklus eine Zeile als JSON Lines (entscheidungs_log.jsonl):
    {"ts": "...", "schema": 2, "gewinner": "...", "grund": "...",
     "soll_einschalten": bool, "kompressor_laeuft": bool, "feedin_w": float,
     "batpower_w": float, "soc": float, "t_unten": float, "t_oben": float}

`schema` ist die Version des Eintrags-Formats (siehe SCHEMA_VERSION). Auswertungen
erkennen daran, welche Felder erwartbar sind; die ersten Generationen des
Bestands (vor dem 25.09.2026) tragen das Feld noch nicht.

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

Historie-Fenster (Anzeige Option 15 im Manager, /history/regeln in der API):
- Die Detail-Ansicht reicht 8 Tage zurueck. Damit dieses Fenster vollstaendig
  verfuegbar ist, muessen sowohl die Rotations-Schwelle (MAX_BYTES) als auch
  der Lese-Deckel (MAX_EINTRAEGE_LESEN) mitspielen; MAX_GENERATIONEN begrenzt
  die Gesamthistorie. Aeltere Zeilen fallen bei der Rotation endgueltig weg.

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

import code_revision
from constants import (
    BATTERIE_LADUNG_MIN_W,
    DEFAULT_TIMEZONE,
    NETZKAUF_GRENZE_W,
    PV_UEBERSCHUSS_MIN_W,
    QUELLE_UNKLAR_MAX_W,
)

LOG_DATEI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "entscheidungs_log.jsonl")
# Rotations-Schwelle je Generation. Die Detail-Anzeige (Manager-Option 15)
# reicht 8 Tage zurueck. Bei ~650 Byte/Zeile und bis zu ~1.150 Zeilen/Tag
# (Herzschlag alle 75 s bei laufender WP) sind das rund 7,5 MB je 8 Tage.
# 8 MB decken das mit Reserve ab, sodass ein 8-Tage-Fenster in ein bis zwei
# Generationen passt. (Zuvor 2 MB - das deckte nur noch wenige Tage ab, seit
# der Herzschlag den Log-Volumen deutlich erhoeht hat.)
MAX_BYTES = 8_000_000          # Rotation bei ~8 MB -> naechste Generation
MAX_GENERATIONEN = 7           # .old.1 ... .old.7
# Lese-Cap: muss das 8-Tage-Fenster komplett abdecken. Worst Case rund
# 1.150 Zeilen/Tag => ~9.200 Zeilen; 20.000 lassen Luft fuer Zeilen aus
# Handlungswechseln, ohne dass ein vollstaendiges Pi-Einlesen zu teuer wird.
MAX_EINTRAEGE_LESEN = 20_000
HEARTBEAT_SEKUNDEN = 75.0      # Zwangsschreibintervall laufender WP (< dt-Cap 120 s)
# Lebenszeichen bei Stillstand. Von 30 auf 15 min gesenkt: ein Dienst-Ausfall
# im Stillstand war bis zu 30 min unsichtbar (Bestandslog 24.08.-11.10.2026
# zeigte mehrere 12-14 h-Luecken). Die Mehrkosten sind minimal (~+48 Zeilen/Tag
# = ~31 KB bei ~650 Byte/Zeile), die Ausfallerkennung dafuer doppelt so schnell.
HEARTBEAT_STILLSTAND_SEKUNDEN = 900.0
LOG_TIMEZONE = pytz.timezone(DEFAULT_TIMEZONE)
IDLE_GEWINNER = "Idle"         # keine Regel fordert Heizbetrieb
# Maximale Laenge des Gewinner-Grundes im Log (Freitext der Gewinner-Regel).
GEWINNER_GRUND_MAX = 200
#: Version des Eintrags-Schemas. Erlaubt Auswertungen, eindeutig zu erkennen,
#: welche Felder erwartbar sind - statt "Feld fehlt" mit "gab es damals noch
#: nicht" zu verwechseln (die ersten ~5 Wochen des Bestands sind "arm").
SCHEMA_VERSION = 2

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


def _handlung_override(
    regel_empfehlung: Optional[bool],
    soll_einschalten: bool,
    diagnostics: Optional[Dict],
) -> Optional[str]:
    """Kurzer Hinweis, wenn die Handlung von der Regel-Empfehlung abweicht.

    ``grund`` beschreibt die Entscheidung der GEWINNER-Regel, ``soll_einschalten``
    dagegen die tatsaechlich an die Hardware gegebene Empfehlung (nach
    Mindestlaufzeit/-pause, Debounce und Sicherheits-Overrides). Im Log
    24.08.-11.10.2026 widersprachen sich beide in 79 Zeilen ("-> AUS" bei
    ``soll_einschalten=true`` und umgekehrt); ohne ``diagnostics`` war im
    Alt-Format kein Grund der Abweichung erkennbar. Dieser Hinweis macht den
    Override explizit - als eigenes Feld UND als Marker im Grundtext.

    ``regel_empfehlung=None`` (Regel trifft keine EIN/AUS-Entscheidung) liefert
    bewusst keinen Hinweis: dann liegt gar kein Widerspruch vor.

    Der Detailtext kommt aus ``diagnostics.blocking_reason`` (Hardware-Sperre,
    z. B. Mindestlaufzeit). Fehlt sie - etwa bei einem reinen Debounce oder der
    Gewinner-Mindesthaltezeit (D(c)) -, nennt ``diagnostics.regelwechsel_grund``
    die Ursache.
    """
    if regel_empfehlung is None:
        return None
    if bool(regel_empfehlung) == bool(soll_einschalten):
        return None
    regel_richtung = "EIN" if regel_empfehlung else "AUS"
    handlung = "EIN" if soll_einschalten else "AUS"
    detail = None
    if isinstance(diagnostics, dict):
        # Reihenfolge: die konkrete Hardware-Sperre (blocking_reason) ist am
        # aussagekraeftigsten; fehlt sie (z. B. reiner Debounce/Haltezeit),
        # nennt regelwechsel_grund die Ursache.
        detail = (
            diagnostics.get("blocking_reason")
            or diagnostics.get("blocking_code")
            or diagnostics.get("regelwechsel_grund")
        )
    basis = f"Handlung {handlung} trotz Regel-{regel_richtung}"
    return f"{basis}: {str(detail)[:80]}" if detail else basis


# Cache der zuletzt geschriebenen Zeile (pfad-gebunden, damit Tests mit
# umgeleitetem LOG_DATEI nicht gegenseitig stoeren).
_cache_pfad: Optional[str] = None
_cache_zeile: Optional[Dict] = None
# Gesammelte Gewinner-Wechsel OHNE Handlungsaenderung, bis zur naechsten
# geschriebenen Zeile (pfad-gebunden wie der Cache darueber).
_pending_pfad: Optional[str] = None
_pending_regelwechsel: List[Dict] = []
MAX_PENDING_REGELWECHSEL = 20  # verhindert unbegrenztes Wachstum im Stillstand
_schreibfehlerzaehler = 0  # Schreibfehler zaehlen, aber das Journal nicht fluten
# Zeitstempel des letzten TATSACHLICH geschriebenen Eintrags. Massgeblich fuer
# den Herzschlag - bewusst NICHT der Cache: unterdrueckte Takte duerfen die
# Frist nicht verlaengern, sonst schiebt sich der Heartbeat endlos hinaus
# (im Test: nach 1800 s immer noch nicht faellig).
_letzte_schreib_ts = None

# Cache der vollstaendig gelesenen Logzeilen. `_lies_zeilen()` wird pro
# Statusabfrage zweimal aufgerufen (Entscheidungen + KPIs), und die WebApp
# pollt /status alle 5 s. Ohne Cache wuerde jede Abfrage saemtliche
# Generationen neu von der SD-Karte lesen und JSON-parsen - nach der
# Vergroesserung von MAX_BYTES/MAX_EINTRAEGE_LESEN waere das auf dem Pi
# spuerbar. Der Schluessel aus (Pfad, Groesse, mtime) invalidiert automatisch,
# sobald geschrieben oder rotiert wird (zusaetzlich explizit in
# `schreibe_eintrag`).
_lies_cache_pfad: Optional[str] = None
_lies_cache_key = None
_lies_cache_zeilen: List[Dict] = []

# BUGFIX (23.10.2026): Der Cache wurde nach JEDEM Schreibvorgang verworfen
# (siehe `schreibe_eintrag`). Im laufenden Betrieb wird das Log aber alle
# 75 s geschrieben (Herzschlag) - und /status fragt alle 5 s. Damit wurde
# alle 75 s die komplette Historie neu von der SD-Karte gelesen und
# JSON-geparst: gemessen 40 MB Spitze und +45 MB RSS je Durchlauf bei
# 20.000 Eintraegen. Das war der groesste wiederkehrende Speicher-Ausschlag
# des Dienstes und hat am 23.10.2026 mit zum OOM-Kill gefuehrt.
#
# Jetzt wird der Cache FORTGESCHRIEBEN: nur die neuen Bytes der juengsten
# Datei werden gelesen. `offset` ist der Byte-Stand, bis zu dem die
# juengste Datei bereits eingelesen ist (immer auf einer Zeilengrenze),
# `aeltere` die Signatur aller aelteren Generationen - aendert sie sich
# (Rotation), wird vollstaendig gelesen.
_lies_cache_stand: Dict = {"offset": 0, "aeltere": ()}

#: Felder, die der Dienst aus dem Entscheidungslog liest. Alles andere
#: (Schema, Revision, Roh-Temperaturen, Diagnose-Ballast) wird beim
#: Einlesen verworfen - es macht den Grossteil der 40 MB aus und wird hier
#: nie gebraucht. Die DATEI bleibt vollstaendig; wer alle Felder braucht,
#: liest sie direkt (Updater/manager_status.py, Analyse/*).
_LESE_FELDER = (
    "ts", "gewinner", "grund", "reason_code", "soll_einschalten",
    "kompressor_laeuft", "regel_empfehlung", "handlung_override",
    "feedin_w", "batpower_w",
)


def _reduziere(eintrag: Dict) -> Dict:
    """Behaelt nur die Felder, die der Dienst tatsaechlich auswertet."""
    reduziert = {feld: eintrag[feld] for feld in _LESE_FELDER if feld in eintrag}
    # Aus den Diagnosedaten wird nur der Grund-Code gebraucht (API-Anzeige).
    diag = eintrag.get("diagnostics")
    if isinstance(diag, dict) and "reason_code" in diag:
        reduziert["diagnostics"] = {"reason_code": diag["reason_code"]}
    return reduziert


def _lies_zeilen_ab(pfad: str, offset: int):
    """Liest JSONL-Zeilen ab ``offset`` bis zum letzten vollstaendigen Ende.

    Rueckgabe: (zeilen, neuer_offset). Ein angebrochener letzter Eintrag
    (Schreibvorgang laeuft gerade) wird NICHT uebernommen und der Offset
    bleibt vor dieser Zeile stehen - so geht sie beim naechsten Lesen
    nicht verloren.
    """
    zeilen: List[Dict] = []
    try:
        with open(pfad, "rb") as f:
            f.seek(offset)
            roh = f.read()
    except OSError:
        return zeilen, offset
    if not roh:
        return zeilen, offset
    ende = roh.rfind(b"\n")
    if ende < 0:
        return zeilen, offset
    for zeile in roh[: ende + 1].split(b"\n"):
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            eintrag = json.loads(zeile.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            continue  # abgebrochene Zeile tolerieren
        if isinstance(eintrag, dict):
            zeilen.append(_reduziere(eintrag))
    return zeilen, offset + ende + 1


def _letzte_logzeile() -> Optional[Dict]:
    """Letzte geschriebene Logzeile oder None (liest nur das Dateiende)."""
    # BUGFIX (22.10.2026): "global _cache_zeile" entfernt. Die Variable
    # wird hier NUR GELESEN - ohne Zuweisung bindet Python den Namen
    # nicht lokal, die Deklaration war also wirkungslos. Sie suggerierte
    # aber einen Schreibzugriff, den es nicht gibt.
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
    if not handlung_unveraendert:
        return True
    # Der Herzschlag wird VOR dem Vergleich der Gewinner-Regel geprueft.
    # Andernfalls verriegelt sich das Log selbst: Heisst die aktuelle Regel
    # anders als die letzte Dateizeile, gilt jeder Eintrag als "nur
    # Regelwechsel" und wird unterdrueckt. Ohne Schreibvorgang bleibt der
    # Cache leer, `_letzte_logzeile()` liest erneut aus der Datei, der
    # Vergleich bleibt falsch - und der faellige Herzschlag wird nie
    # erreicht. Beobachtet am 03./04.10.2026: 19 Stunden Betrieb ohne eine
    # einzige Logzeile bei unveraenderter Gewinner-Regel.
    if _herzschlag_due(vorher, eintrag):
        return True
    if vorher.get("gewinner") != eintrag.get("gewinner"):
        return False  # nur Regel-Wechsel -> an naechste Zeile haengen
    # Zustand stabil und Herzschlag noch nicht faellig.
    return False


def _herzschlag_due(vorher: Dict, eintrag: Dict) -> bool:
    """True, wenn das Herzschlag-Intervall seit dem LETZTEN SCHREIBVORGANG abgelaufen ist.

    Referenz ist bewusst der letzte geschriebene Eintrag und nicht der zuletzt
    bewertete: unterdrueckte Takte (nur ein Regelwechsel bei gleicher Handlung)
    duerfen die Frist nicht verlaengern, sonst wird sie nie faellig.
    """
    grenze = (
        HEARTBEAT_SEKUNDEN
        if eintrag.get("kompressor_laeuft")
        else HEARTBEAT_STILLSTAND_SEKUNDEN
    )
    try:
        aktuell = _parse_log_datetime(eintrag.get("ts"))
        if aktuell is None:
            return True
        referenz = _letzte_schreib_ts
        if referenz is None:
            # Kein bekannter Schreibvorgang: auf die letzte Dateizeile ausweichen.
            referenz = _parse_log_datetime(vorher.get("ts"))
            if referenz is None:
                return True
        return (aktuell - referenz).total_seconds() >= grenze
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
    regel_empfehlung: Optional[bool] = None,
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

    `regel_empfehlung` ist die EIN/AUS-Empfehlung der GEWINNER-Regel, also
    das, was der `grund`-Text beschreibt. Sie kann von `soll_einschalten`
    (der tatsaechlich an die Hardware gegebenen Empfehlung) abweichen, wenn
    Mindestlaufzeit/-pause, Debounce oder Sicherheit eingreifen. Dann traegt
    `handlung_override` einen kurzen Hinweis auf den Override und der
    `grund`-Text bekommt denselben Hinweis als Marker angehaengt.
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
    # Abweichung zwischen Regel-Empfehlung und tatsaechlicher Handlung
    # explizit machen: sonst liest sich eine Zeile "-> AUS" bei
    # soll_einschalten=true wie ein Fehler (Log 24.08.-11.10.2026: 79 Zeilen).
    override = _handlung_override(regel_empfehlung, soll_einschalten, diagnostics)
    if override:
        marker = f" [{override}]"
        gewinner_grund = (
            gewinner_grund[: max(GEWINNER_GRUND_MAX - len(marker), 0)] + marker
        )
    empfehlung_wert = None if regel_empfehlung is None else bool(regel_empfehlung)
    diag = dict(diagnostics) if isinstance(diagnostics, dict) else {}
    diag["regel_empfehlung"] = empfehlung_wert
    diag["handlung_override"] = override
    eintrag = {
        "ts": (ts or _log_now()).isoformat(timespec="seconds"),
        "schema": SCHEMA_VERSION,
        # Code-Stand der Zeile (Nutzerwunsch 18.10.2026). Die Datei wird
        # monatlich rotiert und mischt daher mehrere Code-Staende; ohne diese
        # Angabe ist spaeter nicht entscheidbar, welcher Fix schon lief.
        # "hash+" = lokale python-Aenderungen, "unbekannt" = kein Repo/git.
        "rev": code_revision.kennung(),
        "gewinner": gewinner_name,
        "grund": gewinner_grund[:GEWINNER_GRUND_MAX],
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
        # EIN/AUS-Empfehlung der Gewinner-Regel vs. tatsaechliche Handlung:
        # weichen sie ab, nennt handlung_override den Grund (Mindestlaufzeit,
        # Mindestpause, Debounce, Sicherheit). Damit ist der frueher
        # widerspruechliche Grundtext ("-> AUS" bei soll_einschalten=true)
        # eindeutig aufloesbar.
        "regel_empfehlung": empfehlung_wert,
        "handlung_override": override,
        "diagnostics": diag,
    }
    try:
        global _cache_pfad, _cache_zeile, _pending_pfad, _pending_regelwechsel
        global _letzte_schreib_ts, _lies_cache_key, _lies_cache_stand
        if _pending_pfad != LOG_DATEI:
            _pending_pfad = LOG_DATEI
            _pending_regelwechsel = []
        vorher = _letzte_logzeile()
        # Erste Bewertung dieses Prozesses: die Herzschlagsfrist laeuft ab dem
        # letzten Eintrag der DATEI. Ab dann nur noch ab echten Schreibvorgaengen.
        if _letzte_schreib_ts is None and vorher is not None:
            _letzte_schreib_ts = _parse_log_datetime(vorher.get("ts"))
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
            # Der Cache MUSS den gerade bewerteten Zustand fuehren - auch dann,
            # wenn im Prozess noch nie geschrieben wurde. Vorher stand hier
            # `if _cache_pfad == LOG_DATEI and _cache_zeile is not None`:
            # Nach einem Neustart sind beide leer, der Guard griff nie, und
            # derselbe Regelwechsel wiederholte sich in jedem Takt.
            _cache_zeile = dict(eintrag)
            _cache_pfad = LOG_DATEI
            return False
        # Gesammelte Regelwechsel an diese Zeile haengen.
        if _pending_regelwechsel:
            eintrag["diagnostics"]["bei_laueufigem_regelwechsel"] = list(_pending_regelwechsel)
            _pending_regelwechsel = []
        if os.path.exists(LOG_DATEI) and os.path.getsize(LOG_DATEI) > MAX_BYTES:
            _rotiere()
            _cache_zeile = None
            # Nach der Rotation hat sich der Dateisatz geaendert -> Lese-Cache
            # verwerfen, damit die naechste Abfrage die neue Aufteilung sieht.
            _lies_cache_key = None
            _lies_cache_stand = {"offset": 0, "aeltere": ()}
        with open(LOG_DATEI, "a", encoding="utf-8") as f:
            f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
        _cache_pfad = LOG_DATEI
        _cache_zeile = eintrag
        _letzte_schreib_ts = _parse_log_datetime(eintrag.get("ts"))
        # Die angehaengte Zeile muss die naechste Abfrage erreichen. Der
        # BUGFIX (23.10.2026): Hier wurde der Lese-Cache bisher VERWORFEN.
        # Damit wurde nach jedem Herzschlag (alle 75 s) die komplette
        # Historie neu gelesen und geparst (~40 MB Spitze). Der Cache wird
        # jetzt fortgeschrieben: `_lies_zeilen()` liest nur die Bytes ab
        # `offset` nach. Die eben geschriebene Zeile ist damit sofort
        # sichtbar, ohne dass etwas neu aufgebaut wird.
        return True
    except OSError as e:
        # Logging darf den Regelbetrieb niemals stoeren. Ein Schreibfehler
        # darf aber nicht lautlos bleiben: dieses Log ist das einzige
        # Diagnosewerkzeug fuer die Regelentscheidungen. Mit `logging.debug`
        # verschwand die Meldung bei Journal-Level INFO vollstaendig - am
        # 03./04.10.2026 blieben 19 Stunden Ausfall ohne jede Spur.
        global _schreibfehlerzaehler
        _schreibfehlerzaehler += 1
        if _schreibfehlerzaehler == 1 or _schreibfehlerzaehler % 50 == 0:
            logging.warning(
                "Entscheidungslog nicht schreibbar (%d. Fehler): %s",
                _schreibfehlerzaehler,
                e,
            )
        return False


def _rotiere() -> None:
    """Schiebt das Log eine Generation weiter: .old.N-1 -> .old.N.

    Vorher gab es nur EINE Generation (.old), die bei jeder Rotation ueberschrieben
    wurde: bei ~227 Zeilen/Tag und 2 MB war das Historie von maximal ~16 Tagen -
    zu wenig fuer Saison- und Wettervergleiche. MAX_GENERATIONEN aelterer
    Dateien ergeben stattdessen (je nach Log-Volumen) mehrere Monate.
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


def _lese_schluessel(pfade: List[str]):
    """Identitaet der Logdateien fuer den Lese-Cache (Pfad + Groesse + mtime)."""
    schluessel = []
    for pfad in pfade:
        try:
            st = os.stat(pfad)
            schluessel.append((pfad, st.st_size, st.st_mtime))
        except OSError:
            schluessel.append((pfad, None, None))
    return tuple(schluessel)


def _lies_zeilen() -> List[Dict]:
    """Liest alle Log-Generationen, aelteste zuerst (mit Lese-Cache).

    Liest die Generationen von der JUENGSTEN zur aeltesten und bricht ab,
    sobald MAX_EINTRAEGE_LESEN Eintraege zusammen sind. Ein 8-Tage-Fenster
    umfasst im Worst Case ~9.200 Zeilen - der Deckel schuetzt davor, alle
    Generationen vollstaendig einzulesen.

    BUGFIX (23.10.2026): Wurde die juengste Datei nur FORTGESCHRIEBEN
    (Normalfall: jeder Herzschlag), werden ausschliesslich die neuen Bytes
    gelesen und an den Cache angehaengt. Vorher wurde die komplette
    Historie neu geparst - alle 75 s, gemessen 40 MB Spitze. Vollstaendig
    gelesen wird nur noch bei Rotation, bei einer gekuerzten Datei und
    beim ersten Aufruf.
    """
    global _lies_cache_pfad, _lies_cache_key, _lies_cache_zeilen, _lies_cache_stand
    pfade = [p for p in _alle_logpfade() if os.path.exists(p)]
    schluessel = _lese_schluessel(pfade)

    juengste_ist_aktuell = bool(pfade) and pfade[-1] == LOG_DATEI
    stand = _lies_cache_stand
    offset = stand.get("offset", 0) if isinstance(stand, dict) else 0
    aeltere = stand.get("aeltere", ()) if isinstance(stand, dict) else ()
    if (
        juengste_ist_aktuell
        and _lies_cache_pfad == LOG_DATEI
        and offset > 0
        and aeltere == tuple(schluessel[:-1])
        and schluessel[-1][1] is not None
        and schluessel[-1][1] >= offset
    ):
        neue, neuer_offset = _lies_zeilen_ab(LOG_DATEI, offset)
        if neuer_offset != offset:
            _lies_cache_zeilen = (_lies_cache_zeilen + neue)[-MAX_EINTRAEGE_LESEN:]
            _lies_cache_stand = {
                "offset": neuer_offset,
                "aeltere": tuple(schluessel[:-1]),
            }
        _lies_cache_key = schluessel
        return list(_lies_cache_zeilen)

    bloecke: List[List[Dict]] = []
    aktueller_offset = 0
    for pfad in reversed(pfade):  # juengste Datei zuerst
        if pfad == LOG_DATEI:
            # Byte-Stand merken, damit der naechste Aufruf inkrementell
            # weiterlesen kann.
            zeilen, aktueller_offset = _lies_zeilen_ab(pfad, 0)
        else:
            zeilen = []
            try:
                with open(pfad, "rb") as f:
                    for roh in f:
                        roh = roh.strip()
                        if not roh:
                            continue
                        try:
                            eintrag = json.loads(roh.decode("utf-8", errors="replace"))
                        except json.JSONDecodeError:
                            continue  # abgebrochene letzte Zeile tolerieren
                        if isinstance(eintrag, dict):
                            zeilen.append(_reduziere(eintrag))
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
    ergebnis = zeilen_gesamt[-MAX_EINTRAEGE_LESEN:]
    _lies_cache_pfad = LOG_DATEI
    _lies_cache_key = schluessel
    _lies_cache_zeilen = ergebnis
    # Byte-Stand nur merken, wenn die juengste Datei zuerst gelesen wurde
    # (sonst ist der Deckel schon von den Generationen erreicht worden und
    # ein inkrementelles Weiterlesen wuerde Zeilen ueberspringen).
    if juengste_ist_aktuell and bloecke:
        _lies_cache_stand = {
            "offset": aktueller_offset,
            "aeltere": tuple(schluessel[:-1]),
        }
    else:
        _lies_cache_stand = {"offset": 0, "aeltere": ()}
    return list(ergebnis)


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
