# -*- coding: utf-8 -*-
"""RAM-Schutz: der OOM-Kill vom 23.10.2026 darf nicht wiederkommen.

Hintergrund: Der Kernel hat den Dienst mit
``Out of memory: Killed process ... (python) anon-rss:178428kB`` beendet
(416 MB Gesamt-RAM). Ursache waren nicht Datenmengen an sich, sondern
materialisierte CSV-Zeilen und ein Lese-Cache, der bei jedem Schreiben
neu aufgebaut wurde. Gemessen auf einer 13,5-MB-CSV mit 121.000 Zeilen
(tracemalloc-Spitze, vorher -> nachher):

  * /history 7 Tage:        113 MB -> 5,5 MB
  * PV-Profil:               30 MB -> 0,6 MB
  * Wh/m2-Mittel:            27 MB -> unter 10 MB
  * Entscheidungslog-Lesen:  40 MB -> 0,3 MB (Fortschreibung)

Die Tests hier sichern die RAM-Grenzen UND die Gleichwertigkeit der
Ergebnisse (gleiche Zeilen, gleiche Auswahl, gleiche Felder).
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402
import csv_tail  # noqa: E402
import entscheidungs_log as el  # noqa: E402
import pv_profil  # noqa: E402

KOPF = (
    "Zeitstempel,T_Oben,T_Mittig,T_Unten,T_Verd,Kompressor,ACPower,"
    "FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
    "Einschaltpunkt,Ausschaltpunkt,Solarueberschuss,Urlaubsmodus,"
    "PowerSource,Prognose_Morgen\n"
)


def _csv_mit_zeilen(pfad, anzahl, takt_sek=10, bis_jetzt=True):
    """Schreibt eine CSV im Produktionsformat (10-s-Takt bis jetzt)."""
    start = datetime.now() - timedelta(seconds=takt_sek * anzahl)
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write(KOPF)
        for i in range(anzahl):
            ts = start + timedelta(seconds=takt_sek * i)
            f.write(
                f"{ts.strftime('%Y-%m-%d %H:%M:%S')},42.0,41.0,40.0,10.0,1,"
                "700.0,600.0,0.0,55.0,350.0,350.0,1900.0,42.0,48.0,0,0,Netz,4.6\n"
            )


def _peak(func):
    """tracemalloc-Spitze eines Aufrufs in MB."""
    import tracemalloc

    tracemalloc.start()
    try:
        func()
        _aktuell, spitze = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return spitze / 1048576


# --- Byte-Offset: die letzten N Zeilen -------------------------------------

@pytest.mark.parametrize(
    "inhalt,max_rows,erwartet",
    [
        # mit abschliessendem Umbruch
        ("a\nb\nc\n", 1, "c\n"),
        ("a\nb\nc\n", 2, "b\nc\n"),
        ("a\nb\nc\n", 3, "a\nb\nc\n"),
        ("a\nb\nc\n", 10, "a\nb\nc\n"),
        # ohne abschliessenden Umbruch
        ("a\nb\nc", 1, "c"),
        ("a\nb\nc", 2, "b\nc"),
        ("a\nb\nc", 10, "a\nb\nc"),
        # max_rows <= 0 liest alles
        ("a\nb\nc\n", 0, "a\nb\nc\n"),
    ],
)
def test_offset_trifft_die_letzten_zeilen(tmp_path, inhalt, max_rows, erwartet):
    p = tmp_path / "k.csv"
    # Bytes schreiben: write_text() wuerde unter Windows CRLF erzeugen und
    # damit genau die Zeilenenden veraendern, um die es hier geht.
    p.write_bytes(("kopf\n" + inhalt).encode("utf-8"))
    with open(p, "rb") as roh:
        offset = csv_tail.offset_der_letzten_zeilen(roh, max_rows)
    with open(p, "rb") as roh:
        roh.seek(offset)
        assert roh.read().decode("utf-8") == erwartet


def test_offset_ueber_blockgrenzen(tmp_path):
    """Die Grenze liegt weit vor dem letzten Block (256 kB)."""
    p = tmp_path / "gross.csv"
    _csv_mit_zeilen(p, anzahl=8000)  # ~1,1 MB
    with open(p, "rb") as roh:
        offset = csv_tail.offset_der_letzten_zeilen(roh, 100)
        roh.seek(offset)
        rest = roh.read().decode("utf-8").strip().splitlines()
    assert len(rest) == 100
    assert rest[-1].startswith("20")  # juengste Zeile zuletzt


# --- Streaming statt Materialisierung ---------------------------------------

def test_streame_tail_haelt_nur_einen_block(tmp_path):
    """50.000 Zeilen streamen kostet fast keinen Speicher.

    Vorher las `_read_csv_tail` die KOMPLETTE Datei und hielt die letzten
    N Zeilen mit allen Spalten. Der Test ist die Regressionsbremse: mehr
    als ein Block (256 kB) Puffer ist nicht noetig.
    """
    p = tmp_path / "gross.csv"
    _csv_mit_zeilen(p, anzahl=50000)
    gezaehlt = {"n": 0}

    def zaehle(row):
        assert len(row) == 8
        gezaehlt["n"] += 1

    spitze = _peak(lambda: csv_tail.streame_tail(p, 50000, api.HISTORY_SPALTEN, zaehle))
    assert gezaehlt["n"] == 50000
    assert spitze < 6.0, f"{spitze:.1f} MB Spitze beim Streamen - Speicher waechst mit der Datei"


def test_read_csv_tail_liefert_dieselben_zeilen_wie_vorher(tmp_path):
    """Gleiche Zeilen, gleiche Reihenfolge, gleiche Qualitaetszaehler."""
    p = tmp_path / "k.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("Zeitstempel,T_Oben,T_Mittig,T_Unten,T_Verd,Kompressor,"
                "Einschaltpunkt,Ausschaltpunkt,Extra\n")
        f.write("2026-09-01 10:00:00,1,2,3,4,AUS,42,48,x\n")
        f.write("2026-09-01 10:00:10,1,2,3,4,EIN,42,48,x\n")
        f.write("00:00:20,1,2\n")                      # zu wenige Spalten
        f.write("2026-09-01 10:00:30,1,2,3,4,\x00,42,48,x\n")  # Null-Byte
        f.write("2026-09-01 10:00:40,5,6,7,8,AUS,42,48,x\n")

    rows, quality = api._read_csv_tail(str(p), max_rows=100)

    # Referenz: so hat die alte Fassung gelesen (deque ueber den Leser).
    import csv
    from collections import deque

    with open(p, "r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        index = [header.index(n) for n in api.HISTORY_SPALTEN]
        alt = [tuple(f[i] for i in index) for f in deque(reader, maxlen=100)
               if len(f) == len(header) and not any("\x00" in x for x in f)]
    assert rows == alt
    assert quality["invalid_rows"] == 2
    assert quality["null_bytes"] == 1
    assert quality["header"][0] == "Zeitstempel"


def test_read_csv_tail_ohne_datei_ist_leer(tmp_path):
    rows, quality = api._read_csv_tail(str(tmp_path / "fehlt.csv"))
    assert rows == []
    assert quality["header"] == []


# --- /history: Vorverdichtung auf Minuten ----------------------------------

def test_sammle_history_gedeckelt_und_gleichwertig(tmp_path):
    """/history verdichtet beim Lesen - die Auswahl bleibt dieselbe.

    Vorher wurden 100.000 Tailzeilen gehalten (113 MB), dann auf 5000
    Punkte reduziert. Jetzt wird beim Lesen auf Minutenwerte verdichtet;
    weil alle Sampling-Stufen Vielfache einer Minute sind, ist das
    Ergebnis der anschliessenden Auswahl identisch.
    """
    p = tmp_path / "woche.csv"
    _csv_mit_zeilen(p, anzahl=60000)  # ~7 Tage im 10-s-Takt
    cutoff = datetime.now() - timedelta(hours=168)

    zwischen, quality, anzahl, letzte = api._sammle_history(str(p), cutoff, 100000)
    assert anzahl > 50000
    assert len(zwischen) <= 60 * 24 * 7 + 5, "Minutenverdichtung greift nicht"
    assert letzte is not None

    # Referenzpfad (alte Fassung): alle Zeilen materialisieren und dann
    # herunterrechnen. Muss dieselben Zeilen liefern.
    alle, _ = api._read_csv_tail(str(p), max_rows=100000)
    alt, coverage_alt = api._downsample_history(alle, cutoff, max_points=5000)
    neu, coverage_neu = api._downsample_history(
        [row for _ts, row in zwischen], cutoff, max_points=5000
    )
    assert neu == alt
    assert coverage_neu["coverage_end"] == coverage_alt["coverage_end"]
    assert coverage_neu["sampling_seconds"] == coverage_alt["sampling_seconds"]


def test_sammle_history_bleibt_im_ram(tmp_path):
    p = tmp_path / "woche.csv"
    _csv_mit_zeilen(p, anzahl=60000)
    cutoff = datetime.now() - timedelta(hours=168)
    spitze = _peak(lambda: api._sammle_history(str(p), cutoff, 100000))
    assert spitze < 15.0, f"{spitze:.1f} MB Spitze - das ist OOM-Niveau auf dem Pi"


# --- PV-Profil: keine Zeilenliste mehr -------------------------------------

def test_pv_profil_haelt_keine_zeilen(tmp_path):
    p = tmp_path / "profil.csv"
    _csv_mit_zeilen(p, anzahl=40000)
    pv_profil._cache.clear()
    spitze = _peak(lambda: pv_profil.berechne_profil(str(p), force_refresh=True))
    assert spitze < 5.0, f"{spitze:.1f} MB Spitze - Profil darf nicht mitwachsen"


def test_pv_profil_cache_ist_gedeckelt(tmp_path):
    p = tmp_path / "profil.csv"
    _csv_mit_zeilen(p, anzahl=50)
    pv_profil._cache.clear()
    for faktor in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5):
        pv_profil.berechne_profil(str(p), forecast_scaling=faktor)
    assert len(pv_profil._cache) <= pv_profil.CACHE_MAX_EINTRAEGE


# --- Entscheidungslog: Cache wird fortgeschrieben --------------------------

def _eintrag(ts, gewinner="Abweichung"):
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "gewinner": gewinner,
        "grund": "Regel-Einschalt 'Abweichung'",
        "soll_einschalten": True,
        "kompressor_laeuft": True,
        "feedin_w": 0.0,
        "batpower_w": -120.0,
        "t_unten": 18.187,
        "diagnostics": {"reason_code": "start_abweichung", "blocking_reason": None},
    }


def test_entscheidungslog_liest_inkrementell(tmp_path, monkeypatch):
    """Ein neuer Eintrag kostet nur das Lesen der neuen Bytes.

    Vorher wurde der komplette Lese-Cache nach jedem Schreibvorgang
    verworfen (alle 75 s ein Neuaufbau von ~40 MB).
    """
    pfad = tmp_path / "entscheidungs_log.jsonl"
    monkeypatch.setattr(el, "LOG_DATEI", str(pfad))
    monkeypatch.setattr(el, "_lies_cache_pfad", None)
    monkeypatch.setattr(el, "_lies_cache_key", None)
    monkeypatch.setattr(el, "_lies_cache_zeilen", [])
    monkeypatch.setattr(el, "_lies_cache_stand", {"offset": 0, "aeltere": ()})

    jetzt = datetime.now()
    with open(pfad, "w", encoding="utf-8") as f:
        for i in range(500):
            f.write(json.dumps(_eintrag(jetzt - timedelta(minutes=500 - i))) + "\n")

    erste = el._lies_zeilen()
    assert len(erste) == 500
    stand_vorher = el._lies_cache_stand["offset"]
    assert stand_vorher == pfad.stat().st_size

    with open(pfad, "a", encoding="utf-8") as f:
        f.write(json.dumps(_eintrag(jetzt, "AdaptivePV")) + "\n")

    zweite = el._lies_zeilen()
    assert len(zweite) == 501
    assert zweite[-1]["gewinner"] == "AdaptivePV"
    # Nur die neuen Bytes wurden gelesen - und der Stand ist fortgeschrieben.
    assert el._lies_cache_stand["offset"] > stand_vorher
    spitze = _peak(el._lies_zeilen)
    assert spitze < 1.0, f"{spitze:.1f} MB fuer einen neuen Eintrag - Cache wird neu aufgebaut"


def test_entscheidungslog_wartet_auf_die_ganze_zeile(tmp_path, monkeypatch):
    """Ein angebrochener letzter Eintrag wird nicht verschluckt."""
    pfad = tmp_path / "entscheidungs_log.jsonl"
    monkeypatch.setattr(el, "LOG_DATEI", str(pfad))
    monkeypatch.setattr(el, "_lies_cache_pfad", None)
    monkeypatch.setattr(el, "_lies_cache_key", None)
    monkeypatch.setattr(el, "_lies_cache_zeilen", [])
    monkeypatch.setattr(el, "_lies_cache_stand", {"offset": 0, "aeltere": ()})

    jetzt = datetime.now()
    vollstaendig = json.dumps(_eintrag(jetzt - timedelta(minutes=1)))
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(vollstaendig + "\n")
    assert len(el._lies_zeilen()) == 1

    # Schreibvorgang laeuft gerade: halbe Zeile in der Datei.
    halbe = json.dumps(_eintrag(jetzt, "Komfort"))
    with open(pfad, "a", encoding="utf-8") as f:
        f.write(halbe[:20])
    assert len(el._lies_zeilen()) == 1, "angebrochene Zeile darf nicht als Eintrag zaehlen"

    # ... und nach dem Abschluss ist sie da (nicht verloren).
    with open(pfad, "a", encoding="utf-8") as f:
        f.write(halbe[20:] + "\n")
    zeilen = el._lies_zeilen()
    assert len(zeilen) == 2
    assert zeilen[-1]["gewinner"] == "Komfort"


def test_entscheidungslog_behaelt_die_genutzten_felder(tmp_path, monkeypatch):
    """Die Reduktion beim Lesen nimmt nur Ballast weg, keine genutzten Felder."""
    pfad = tmp_path / "entscheidungs_log.jsonl"
    monkeypatch.setattr(el, "LOG_DATEI", str(pfad))
    monkeypatch.setattr(el, "_lies_cache_pfad", None)
    monkeypatch.setattr(el, "_lies_cache_key", None)
    monkeypatch.setattr(el, "_lies_cache_zeilen", [])
    monkeypatch.setattr(el, "_lies_cache_stand", {"offset": 0, "aeltere": ()})
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(json.dumps(_eintrag(datetime.now())) + "\n")

    zeile = el._lies_zeilen()[0]
    for feld in ("ts", "gewinner", "grund", "soll_einschalten",
                 "kompressor_laeuft", "feedin_w", "batpower_w"):
        assert feld in zeile, feld
    assert zeile["diagnostics"] == {"reason_code": "start_abweichung"}
    # Ballast ist weg (macht den Grossteil der 40 MB aus):
    assert "t_unten" not in zeile


# --- RAM-Schutz in /status --------------------------------------------------

def test_ram_knapp_erkennt_knappen_speicher(monkeypatch):
    import startup_diagnose

    monkeypatch.setattr(
        startup_diagnose, "speicher_werte",
        lambda *a, **k: {"total_kb": 425984, "verfuegbar_kb": 40 * 1024, "rss_kb": 90000},
    )
    assert api._ram_knapp() is True

    monkeypatch.setattr(
        startup_diagnose, "speicher_werte",
        lambda *a, **k: {"total_kb": 425984, "verfuegbar_kb": 199 * 1024, "rss_kb": 66000},
    )
    assert api._ram_knapp() is False


def test_ram_knapp_ohne_proc_ist_false(monkeypatch):
    """Ohne /proc (Windows, Tests) darf der Schutz nichts abschalten."""
    import startup_diagnose

    monkeypatch.setattr(
        startup_diagnose, "speicher_werte",
        lambda *a, **k: {"total_kb": None, "verfuegbar_kb": None, "rss_kb": None},
    )
    assert api._ram_knapp() is False


# --- Speicher-Diagnose: Spitze (VmPeak) ------------------------------------

def test_vmpeak_wird_gelesen_und_formatiert(tmp_path):
    import startup_diagnose as sd

    status = tmp_path / "status"
    status.write_text(
        "Name:\tpython\nVmPeak:\t  194560 kB\nVmRSS:\t       66560 kB\n",
        encoding="utf-8",
    )
    assert sd.spitze_kb(str(status)) == 194560
    assert sd.formatiere_spitze(194560) == " | Spitze=190 MB"
    assert sd.spitze_kb(str(tmp_path / "fehlt")) is None
    assert sd.formatiere_spitze(None) == ""


def test_speicher_log_enthaelt_die_spitze(monkeypatch, caplog):
    """Das stuendliche Speicher-Log nennt jetzt auch VmPeak."""
    import logging

    import main as m

    monkeypatch.setattr(
        m.startup_diagnose, "speicher_werte",
        lambda *a, **k: {"total_kb": 425984, "verfuegbar_kb": 199 * 1024, "rss_kb": 66560},
    )
    monkeypatch.setattr(m.startup_diagnose, "spitze_kb", lambda *a, **k: 194560)
    monkeypatch.setattr(m, "now_for", lambda state: datetime.now())

    state = type("S", (), {"clock": None})()
    with caplog.at_level(logging.INFO):
        m._logge_speicher(state, None)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "RSS=65 MB" in text
    assert "Spitze=190 MB" in text


def test_ram_schwelle_warnt_einmal_je_episode(monkeypatch, caplog):
    """Unterschreiten der Grenze wird EINMAL gewarnt, Erholung EINMAL gemeldet."""
    import logging

    import main as m

    werte = {"verfuegbar_kb": 40 * 1024}
    monkeypatch.setattr(
        m.startup_diagnose, "speicher_werte", lambda *a, **k: dict(werte)
    )
    monkeypatch.setattr(m, "_state_monotonic", lambda state: 0.0)
    state = type("S", (), {})()

    with caplog.at_level(logging.INFO):
        m._pruefe_ram_schwelle(state)           # unterschreitet -> Warnung
        state._ram_schwelle_ts = None           # Drosselung aufheben (Test)
        m._pruefe_ram_schwelle(state)           # bleibt knapp -> keine 2. Warnung
        werte["verfuegbar_kb"] = 200 * 1024
        state._ram_schwelle_ts = None
        m._pruefe_ram_schwelle(state)           # erholt -> Info
    texte = [r.getMessage() for r in caplog.records]
    assert sum("RAM knapp" in t for t in texte) == 1
    assert sum("RAM wieder ausreichend" in t for t in texte) == 1



