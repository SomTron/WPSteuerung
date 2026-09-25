"""Tests fuer die Logabfrage (`log_query`).

Warum wichtig: Der WP-Manager ruft dieses Modul in den Optionen 11 und 12
auf ("Query Logs by Time" / "by Duration"). Es benutzt eine **binaere Suche
im Byte-Offset-Raum** ueber mehrere Log-Megabyte. Fehler dort fallen auf
dem Pi nur auf - in CI laeuft die Datei nie.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

import log_query  # noqa: E402
from log_query import (  # noqa: E402
    _binary_search_ts,
    _parse_ts,
    query_logs,
    tail_log,
)

# Format wie logging_config es schreibt: Datum Zeit +Zeitzone LEVEL - Text
LOGZEILE = "{ts} +0200 {level} - {text}\n"


def schreibe_log(pfad: Path, eintraege) -> str:
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        for ts, level, text in eintraege:
            f.write(LOGZEILE.format(ts=ts, level=level, text=text))
    return str(pfad)


def minuten_eintraege(start, anzahl, level="INFO", text="Eintrag"):
    eintraege = []
    for i in range(anzahl):
        ts = (start + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        eintraege.append((ts, level, f"{text} {i}"))
    return eintraege


@pytest.fixture
def logdatei(tmp_path):
    """Log mit 20 Minuten durchgehend, jede Minute eine Zeile."""
    eintraege = [
        (
            (datetime(2026, 9, 25, 14, 0) + timedelta(minutes=i)).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "ERROR" if i % 7 == 0 else "INFO",
            f"Eintrag {i}",
        )
        for i in range(20)
    ]
    return schreibe_log(tmp_path / "heizungssteuerung.log", eintraege)


# --------------------------------------------------------------- Parsing


def test_parse_ts_liest_datum_und_zeit():
    ts = _parse_ts("2026-09-25 14:03:22 +0200 INFO - Test\n")
    assert ts == datetime(2026, 9, 25, 14, 3, 22)


def test_parse_ts_ignoriert_zeitzone():
    """Die Zeitzone wird bewusst verworfen - Log und Abfrage sind lokal."""
    a = _parse_ts("2026-09-25 14:03:22 +0200 INFO - x")
    b = _parse_ts("2026-09-25 14:03:22 +0000 INFO - x")
    assert a == b, "Zeitzone darf das Parsen nicht veraendern"


def test_parse_ts_liefert_none_fuer_muell():
    assert _parse_ts("kein Zeitstempel hier\n") is None
    assert _parse_ts("2026-13-45 99:99:99 +0200 ERROR - kaputt\n") is None
    assert _parse_ts("") is None


def test_parse_ts_fordert_zeitstempel_am_zeilenanfang():
    """Ein Text mit Zeitstempel in der Mitte darf nicht zaehlen."""
    assert _parse_ts("vorher 2026-09-25 14:03:22 Text\n") is None


# ------------------------------------------------------------- query_logs


def test_query_logs_ohne_filter_liefert_alle(logdatei):
    zeilen, meta = query_logs(log_path=logdatei, lines=100)
    assert len(zeilen) == 20
    assert meta["total_found"] == 20
    assert meta["log_exists"] is True


def test_query_logs_respektiert_line_limit(logdatei):
    zeilen, meta = query_logs(log_path=logdatei, lines=5)
    assert len(zeilen) == 5
    assert meta["requested_lines"] == 5


def test_query_logs_begrenzt_auf_500(logdatei):
    """Ein grosser Wert darf die Speicherlast nicht ausuern."""
    _, meta = query_logs(log_path=logdatei, lines=100000)
    assert meta["requested_lines"] == 500


def test_query_logs_filtert_nach_level(logdatei):
    zeilen, meta = query_logs(log_path=logdatei, lines=100, level="ERROR")
    assert zeilen, "es gibt ERROR-Zeilen"
    assert all(" ERROR " in z for z in zeilen)
    assert meta["filter_level"] == "ERROR"
    assert len(zeilen) < 20, "Level-Filter muss reduzieren"


def test_query_logs_level_filter_ignoriert_gross_klein(tmp_path):
    """Die *Abfrage* darf klein geschrieben sein; das Log selbst schreibt
    laut logging_config immer gross (Levelnamen aus dem Log-Record)."""
    pfad = schreibe_log(
        tmp_path / "l.log",
        [
            ("2026-09-25 10:00:00", "WARNING", "erster"),
            ("2026-09-25 10:01:00", "ERROR", "zweiter"),
        ],
    )
    zeilen, _ = query_logs(log_path=pfad, lines=10, level="warning")
    assert len(zeilen) == 1 and "erster" in zeilen[0]

    zeilen2, _ = query_logs(log_path=pfad, lines=10, level="ERROR")
    assert len(zeilen2) == 1 and "zweiter" in zeilen2[0]


def test_query_logs_after_grenzt_aus(logdatei):
    zeilen, meta = query_logs(
        after=datetime(2026, 9, 25, 14, 10), lines=100, log_path=logdatei
    )
    assert zeilen
    assert meta["filter_after"] is not None
    assert all(_parse_ts(z) >= datetime(2026, 9, 25, 14, 10) for z in zeilen)


def test_query_logs_before_grenzt_aus(logdatei):
    zeilen, _ = query_logs(
        before=datetime(2026, 9, 25, 14, 5), lines=100, log_path=logdatei
    )
    assert zeilen
    assert all(_parse_ts(z) < datetime(2026, 9, 25, 14, 5) for z in zeilen)


def test_query_logs_zeitfenster_kombiniert(logdatei):
    zeilen, _ = query_logs(
        after=datetime(2026, 9, 25, 14, 5),
        before=datetime(2026, 9, 25, 14, 10),
        lines=100,
        log_path=logdatei,
    )
    assert zeilen
    for z in zeilen:
        ts = _parse_ts(z)
        assert datetime(2026, 9, 25, 14, 5) < ts < datetime(2026, 9, 25, 14, 10)


def test_query_logs_metadaten_enthalten_zeitraum(logdatei):
    _, meta = query_logs(log_path=logdatei, lines=100)
    assert meta["start_ts"] is not None
    assert meta["end_ts"] is not None
    assert meta["start_ts"] < meta["end_ts"]


def test_query_logs_fehlende_datei_ist_fehlerfrei(tmp_path):
    zeilen, meta = query_logs(log_path=str(tmp_path / "gibtsnicht.log"), lines=10)
    assert zeilen == []
    assert meta["log_exists"] is False
    assert meta["total_found"] == 0


def test_query_logs_leere_datei(tmp_path):
    leer = tmp_path / "leer.log"
    leer.write_text("", encoding="utf-8")
    zeilen, meta = query_logs(log_path=str(leer), lines=10)
    assert zeilen == []
    assert meta["log_exists"] is True
    assert meta["start_ts"] is None


# --------------------------------------------------------- binaere Suche


def test_binary_search_findet_erste_zeile_nach_ziel(logdatei):
    pos = _binary_search_ts(logdatei, datetime(2026, 9, 25, 14, 10))
    with open(logdatei, "r", encoding="utf-8") as f:
        f.seek(pos)
        erste = f.readline()
    ts = _parse_ts(erste)
    assert ts is not None
    assert ts >= datetime(2026, 9, 25, 14, 10), (
        f"erste Zeile steht auf {ts}, erwarte >= 14:10"
    )


def test_binary_search_vor_beginn_liefert_erste_zeile(logdatei):
    pos = _binary_search_ts(logdatei, datetime(2020, 1, 1, 0, 0))
    with open(logdatei, "r", encoding="utf-8") as f:
        f.seek(pos)
        erste = f.readline()
    assert _parse_ts(erste) is not None


def test_binary_search_nach_ende_liefert_dateiende(logdatei):
    pos = _binary_search_ts(logdatei, datetime(2099, 1, 1))
    assert pos <= os.path.getsize(logdatei)


def test_binary_search_position_ist_zeilenbeginn(logdatei):
    """Die Position muss auf einen Zeilenanfang zeigen, sonst zerbricht der
    nachfolgende Textlauf mitten in einer Zeile."""
    pos = _binary_search_ts(logdatei, datetime(2026, 9, 25, 14, 7))
    if pos > 0:
        with open(logdatei, "rb") as f:
            f.seek(pos - 1)
            assert f.read(1) == b"\n", "Position liegt mitten in einer Zeile"


def test_query_logs_nach_filter_verliert_erste_zeile_nicht(logdatei):
    """Regression: ein readline() nach dem Seek wuerde die erste passende
    Zeile verschlucken (Bug #1, wp-manager v1.7)."""
    zeilen, _ = query_logs(
        after=datetime(2026, 9, 25, 14, 5), lines=100, log_path=logdatei
    )
    assert zeilen, "Ergebnis darf nicht leer sein"
    assert "Eintrag 6" in zeilen[0], f"erste Zeile nach `after` fehlt: {zeilen[0]!r}"


def test_query_logs_after_ist_ausschliesslich(logdatei):
    """Dokumentierter Vertrag: Zeile exakt auf `after` wird ausgeschlossen."""
    zeilen, _ = query_logs(
        after=datetime(2026, 9, 25, 14, 0), lines=100, log_path=logdatei
    )
    assert zeilen[0].startswith("2026-09-25 14:01:00"), (
        "die Zeile auf `after` selbst darf nicht geliefert werden"
    )


# -------------------------------------------------------------- tail_log


def test_tail_log_liefert_die_letzten_zeilen(logdatei):
    zeilen, meta = tail_log(lines=3, log_path=logdatei)
    assert len(zeilen) == 3
    assert meta["total_found"] == 3
    assert "Eintrag 19" in zeilen[-1]


def test_tail_log_ueberschreitet_blockgrenze(tmp_path):
    """Der Block-Leser arbeitet in 4-KiB-Bloecken; eine kleine Datei
    wuerde die Grenzlogik nie ausloesen."""
    pfad = schreibe_log(
        tmp_path / "gross.log",
        minuten_eintraege(datetime(2026, 9, 25, 12, 0), 400, text="x" * 200),
    )
    assert os.path.getsize(pfad) > 4096
    zeilen, meta = tail_log(lines=5, log_path=pfad)
    assert len(zeilen) == 5
    assert "xxx" in zeilen[-1]


def test_tail_log_mehr_als_vorhanden(logdatei):
    zeilen, meta = tail_log(lines=500, log_path=logdatei)
    assert len(zeilen) == 20, "darf nicht mehr liefern als die Datei enthaelt"


def test_tail_log_fehlende_datei(tmp_path):
    zeilen, meta = tail_log(lines=5, log_path=str(tmp_path / "weg.log"))
    assert zeilen == []
    assert meta["log_exists"] is False


# ------------------------------------------------------------ Robustheit


def test_query_logs_ueberlebt_kaputte_zeilen(tmp_path):
    """Log kann mitten im Schreiben abgeschnitten sein - NUL und Müll
    duerfen keine Ausnahme erzeugen."""
    pfad = tmp_path / "kaputt.log"
    with open(pfad, "wb") as f:
        f.write(b"2026-09-25 10:00:00 +0200 INFO - ok\n")
        f.write(b"\x00\x00\x00Nullbytes\n")
        f.write(b"2026-09-25 10:02:00 +0200 ERROR - noch ok\n")
    zeilen, meta = query_logs(log_path=str(pfad), lines=10)
    assert meta["log_exists"] is True
    assert any("noch ok" in z for z in zeilen)


def test_default_logpfad_ist_deterministisch():
    """Egal ob /var/log/wps oder Entwicklungsverzeichnis - es muss ein
    absoluter Pfad auf die Logdatei sein."""
    pfad = log_query._find_default_log_path()
    assert pfad.endswith("heizungssteuerung.log")
    assert os.path.isabs(pfad)
