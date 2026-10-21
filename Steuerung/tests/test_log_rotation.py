"""Tests fuer die tagesbasierte Log-Rotation und das Lesen aller Generationen.

Hintergrund (das eigentliche Problem): Das Betriebslog rotiert, weil das
tmpfs auf dem Pi begrenzt ist (log2ram, SIZE=128M). Bisher rotierte es
GROESSENBASIERT (`.1`, `.2`, ...) - zwei Probleme daraus:

  1. Nach einer Rotation sah der Leser nur noch die aktive (winzige)
     Datei. Die aeltere Historie lag unangetastet auf der Platte, war
     aber unsichtbar. Genau das war die gemeldete Symptomatik: "wenn der
     Log gerade neu geschrieben wurde, sehe ich nur einen kleinen Teil".
  2. Die Dateinamen sagten nichts ueber den Zeitraum aus. Bei `.1` muss
     man raten, welcher Tag darin steht.

Jetzt: Rotation tagesbasiert (Datum im Dateinamen) und die Leser ziehen
alle Generationen heran.

Diese Tests halten beide Eigenschaften fest - insbesondere (1), weil genau
dort der Fehler zurueckkommen kann.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from pathlib import Path

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from log_files import (  # noqa: E402
    archiv_name,
    generationen,
    tail_bytes_ueber_alle,
    tail_ueber_alle,
    verwalte_archiv,
)
from log_query import query_logs, tail_log  # noqa: E402
from logging_config import TagesRotationsHandler  # noqa: E402

LOGZEILE = "{ts} +0200 {level} - {text}\n"


def schreibe_log(pfad: Path, eintraege) -> str:
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        for ts, level, text in eintraege:
            f.write(LOGZEILE.format(ts=ts, level=level, text=text))
    return str(pfad)


def minuten_eintraege(start, anzahl, level="INFO", text="Eintrag"):
    eintraege = []
    for i in range(anzahl):
        ts = (start + dt.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        eintraege.append((ts, level, f"{text} {i}"))
    return eintraege


# --------------------------------------------------------------- Generationen


def test_generationen_liefert_alle_dateien_aelteste_zuerst(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("neu\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.2026-10-18").write_text("a\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.2026-10-19").write_text("b\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in generationen(str(log))]
    assert namen == [
        "heizungssteuerung.log.2026-10-18",
        "heizungssteuerung.log.2026-10-19",
        "heizungssteuerung.log",
    ]


def test_generationen_erkennt_auch_numerischen_altbestand(tmp_path):
    """Der Pi kann Altbestand (.1) und neue Tagesdateien nebeneinander haben."""
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("neu\n", encoding="utf-8")
    alt = tmp_path / "heizungssteuerung.log.1"
    alt.write_text("alt\n", encoding="utf-8")
    # mtime bewusst in die Vergangenheit legen (Zeitgrenze liegt am mtime).
    os.utime(alt, (1000000, 1000000))

    namen = [os.path.basename(p) for p in generationen(str(log))]
    assert namen[0] == "heizungssteuerung.log.1"
    assert namen[-1] == "heizungssteuerung.log"


def test_generationen_ignoriert_fremde_dateien(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("x\n", encoding="utf-8")
    (tmp_path / "error.log").write_text("y\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.bak").write_text("z\n", encoding="utf-8")

    namen = [os.path.basename(p) for p in generationen(str(log))]
    assert namen == ["heizungssteuerung.log"]


# -------------------------------------------------- Der eigentliche Fehler


def test_tail_log_sieht_die_rotierte_historie(tmp_path):
    """REGRESSION: nach einer Rotation war die Historie unsichtbar.

    Vorher las `tail_log` nur die aktive Datei. Der Eintrag aus der
    Generation `.1` (also der Zeit vor der Rotation) fehlte komplett.
    """
    log = tmp_path / "heizungssteuerung.log"
    schreibe_log(log, [("2026-09-01 10:00:00", "INFO", "AKTIV")])
    (tmp_path / "heizungssteuerung.log.1").write_text(
        "2026-09-01 09:00:00 +0200 INFO - ROTIERT\n", encoding="utf-8"
    )

    zeilen, meta = tail_log(lines=10, log_path=str(log))

    assert len(zeilen) == 2, "die rotierte Historie fehlt"
    assert any("ROTIERT" in z for z in zeilen)
    assert any("AKTIV" in z for z in zeilen)
    # Chronologische Reihenfolge: aelter zuerst.
    assert "ROTIERT" in zeilen[0]
    assert "AKTIV" in zeilen[1]
    assert meta["total_found"] == 2


def test_query_logs_findet_eintraege_in_der_historie(tmp_path):
    """REGRESSION: die Zeitbereichsabfrage sah nur die aktive Datei."""
    log = tmp_path / "heizungssteuerung.log"
    schreibe_log(log, [("2026-09-01 10:00:00", "INFO", "AKTIV")])
    (tmp_path / "heizungssteuerung.log.1").write_text(
        "2026-09-01 09:00:00 +0200 INFO - ROTIERT\n", encoding="utf-8"
    )

    zeilen, meta = query_logs(
        after=dt.datetime(2026, 9, 1, 8, 0), lines=10, log_path=str(log)
    )

    assert len(zeilen) == 2
    assert any("ROTIERT" in z for z in zeilen)
    assert len(meta["generationen"]) == 2


def test_query_logs_ueber_ihre_zeilengrenze_hinaus(tmp_path):
    """REGRESSION: die Historie darf nicht am Zeilenlimit abgeschnitten werden.

    `lines` ist das Limit ueber das GESAMTE Log (so war es auch vorher).
    Der entscheidende Punkt: die Generation wird mitgelesen, nicht
    uebersprungen - und sie steht chronologisch VOR der aktiven Datei.
    """
    log = tmp_path / "heizungssteuerung.log"
    schreibe_log(log, minuten_eintraege(dt.datetime(2026, 9, 2, 12, 0), 5, text="neu"))
    (tmp_path / "heizungssteuerung.log.1").write_text(
        "2026-09-01 09:00:00 +0200 INFO - ROTIERT\n", encoding="utf-8"
    )

    zeilen, _ = query_logs(lines=10, log_path=str(log))

    # 5 Zeilen aus der aktiven Datei + 1 Zeile aus der Generation.
    assert len(zeilen) == 6
    assert "ROTIERT" in zeilen[0], "die aeltere Generation muss vorne stehen"
    assert "neu 4" in zeilen[-1]


def test_query_logs_zeilenlimit_gilt_ueber_alle_generationen(tmp_path):
    """Das Limit bleibt global - es werden nicht je Datei `lines` geliefert."""
    log = tmp_path / "heizungssteuerung.log"
    schreibe_log(log, minuten_eintraege(dt.datetime(2026, 9, 2, 12, 0), 5, text="neu"))
    (tmp_path / "heizungssteuerung.log.1").write_text(
        "2026-09-01 09:00:00 +0200 INFO - ROTIERT\n", encoding="utf-8"
    )

    zeilen, _ = query_logs(lines=3, log_path=str(log))

    assert len(zeilen) == 3
    assert any("ROTIERT" in z for z in zeilen), "die Generation fehlt im Ergebnis"


def test_tail_ueber_alle_liefert_chronologische_reihenfolge(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("2026-09-02 10:00:00 +0200 INFO - ZWEI\n", encoding="utf-8")
    (tmp_path / "heizungssteuerung.log.2026-09-01").write_text(
        "2026-09-01 10:00:00 +0200 INFO - EINS\n", encoding="utf-8"
    )

    zeilen = tail_ueber_alle(str(log), 10)

    assert len(zeilen) == 2
    assert "EINS" in zeilen[0]
    assert "ZWEI" in zeilen[1]


def test_tail_bytes_ueber_alle_ist_chronologisch(tmp_path):
    log = tmp_path / "error.log"
    log.write_text("2026-09-02 10:00:00 +0200 ERROR - ZWEI\n", encoding="utf-8")
    (tmp_path / "error.log.2026-09-01").write_text(
        "2026-09-01 10:00:00 +0200 ERROR - EINS\n", encoding="utf-8"
    )

    text = tail_bytes_ueber_alle(str(log), 100_000)

    assert "EINS" in text
    assert "ZWEI" in text
    assert text.index("EINS") < text.index("ZWEI")


def test_tail_log_sieht_die_generation_ohne_aktive_datei(tmp_path):
    """REGRESSION: die Historie war unsichtbar, wenn die aktive Datei fehlt.

    Die aktive Logdatei entsteht erst beim ersten Eintrag des Tages. Fehlt
    sie, liegen die Daten von gestern aber in der Generation - mit der
    alten Pruefung ("aktive Datei existiert?") waren sie weg.
    """
    log = tmp_path / "heizungssteuerung.log"
    (tmp_path / "heizungssteuerung.log.2026-10-20").write_text(
        "2026-10-20 09:00:00 +0200 INFO - GESTERN\n", encoding="utf-8"
    )

    zeilen, meta = tail_log(lines=10, log_path=str(log))

    assert len(zeilen) == 1, "die Historie fehlt"
    assert "GESTERN" in zeilen[0]
    assert meta["log_exists"] is True


def test_query_logs_sieht_die_generation_ohne_aktive_datei(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    (tmp_path / "heizungssteuerung.log.2026-10-20").write_text(
        "2026-10-20 09:00:00 +0200 INFO - GESTERN\n", encoding="utf-8"
    )

    zeilen, meta = query_logs(lines=10, log_path=str(log))

    assert len(zeilen) == 1
    assert "GESTERN" in zeilen[0]
    assert meta["log_exists"] is True


# ------------------------------------------------------- Tagesrotation


def _handler(pfad, **kwargs):
    h = TagesRotationsHandler(str(pfad), **kwargs)
    h.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s - %(message)s",
                          datefmt="%Y-%m-%d %H:%M:%S %z")
    )
    return h


def _emit(h, text):
    h.emit(logging.LogRecord("test", logging.INFO, __file__, 1, text, None, None))


def test_rotation_bei_tageswechsel_legt_datierten_namen_an(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    h = _handler(log)
    try:
        _emit(h, "Zeile 1")
        # Tageswechsel simulieren
        h._tag = dt.date.today() - dt.timedelta(days=1)
        _emit(h, "Zeile 2")
    finally:
        h.close()

    gestern = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    archiv = tmp_path / f"heizungssteuerung.log.{gestern}"
    assert archiv.exists(), "die Rotation haette kein datiertes Archiv anlegen muessen"
    assert "Zeile 1" in archiv.read_text(encoding="utf-8")
    # Die aktive Datei enthaelt nur den neuen Inhalt.
    assert "Zeile 2" in log.read_text(encoding="utf-8")
    assert "Zeile 1" not in log.read_text(encoding="utf-8")


def test_rotation_bei_groessengrenze_teilt_den_tag(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    h = _handler(log, max_bytes=200)
    try:
        for i in range(20):
            _emit(h, f"Zeile {i} " + "x" * 50)
    finally:
        h.close()

    namen = sorted(p.name for p in tmp_path.glob("heizungssteuerung.log.*"))
    assert namen, "die Groessengrenze haette rotieren muessen"
    # Der Tag muss erhalten bleiben, auch wenn er zerschnitten wurde.
    assert all(n.startswith("heizungssteuerung.log.2") for n in namen)


def test_nachtuebergang_rotiert_und_je_tag_hat_eine_datei(tmp_path, monkeypatch):
    """Der realistische Ablauf: die Nacht kommt, der Handler rotiert.

    `date.today()` wird in logging_config gepatcht, damit der Uebergang
    ohne Warten auf Mitternacht simulierbar ist. Jeder Tag muss danach
    seine EIGENE Datei haben - das ist der ganze Zweck der Umstellung
    von der Groessen- auf die Tagesrotation.
    """
    import logging_config

    class _Datum:
        """Ersetzt datetime.date in logging_config (nur fuer today())."""

        def __init__(self, start):
            self.aktuell = start

        def today(self):
            return self.aktuell

    fake = _Datum(dt.date(2026, 10, 19))
    monkeypatch.setattr(logging_config, "date", fake)

    log = tmp_path / "heizungssteuerung.log"
    h = _handler(log)
    try:
        _emit(h, "Tag 19")
        fake.aktuell = dt.date(2026, 10, 20)   # Mitternacht
        _emit(h, "Tag 20")
        fake.aktuell = dt.date(2026, 10, 21)
        _emit(h, "Tag 21")
    finally:
        h.close()

    tag19 = tmp_path / "heizungssteuerung.log.2026-10-19"
    tag20 = tmp_path / "heizungssteuerung.log.2026-10-20"
    assert tag19.exists(), "der Log vom 19.10. fehlt"
    assert tag20.exists(), "der Log vom 20.10. fehlt"
    assert "Tag 19" in tag19.read_text(encoding="utf-8")
    assert "Tag 20" in tag20.read_text(encoding="utf-8")
    # Der laufende Tag landet in der aktiven Datei.
    assert "Tag 21" in log.read_text(encoding="utf-8")
    assert "Tag 20" not in log.read_text(encoding="utf-8")


def test_rotation_bei_neustart_behaelt_den_tag(tmp_path):
    """Ein Neustart mitten in der Nacht darf den gestrigen Log nicht
    als "heute" archivieren - sonst geht der Tag im Dateinamen verloren."""
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("gestern\n", encoding="utf-8")
    gestern = dt.datetime.now() - dt.timedelta(days=1)
    os.utime(log, (gestern.timestamp(), gestern.timestamp()))

    h = _handler(log)
    try:
        _emit(h, "heute")
    finally:
        h.close()

    archiv = tmp_path / f"heizungssteuerung.log.{gestern.date().isoformat()}"
    assert archiv.exists(), "der Log des gestrigen Tages ging verloren"
    assert "gestern" in archiv.read_text(encoding="utf-8")


def test_archiv_name_hat_datum_und_teilnummer(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    name = archiv_name(str(log), dt.date(2026, 10, 19))
    assert os.path.basename(name) == "heizungssteuerung.log.2026-10-19"
    name2 = archiv_name(str(log), dt.date(2026, 10, 19), 2)
    assert os.path.basename(name2) == "heizungssteuerung.log.2026-10-19.2"


# --------------------------------------------------------- Archivverwaltung


def test_verwalte_archiv_loescht_zu_alte_dateien(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("x\n", encoding="utf-8")
    alt = tmp_path / "heizungssteuerung.log.2020-01-01"
    alt.write_text("uralt\n", encoding="utf-8")
    neu = tmp_path / f"heizungssteuerung.log.{dt.date.today().isoformat()}"
    neu.write_text("heute\n", encoding="utf-8")

    geloescht = verwalte_archiv(str(log), keep_tage=14, max_gesamt_bytes=10_000_000)

    assert str(alt) in geloescht
    assert not alt.exists()
    assert neu.exists(), "ein aktuelles Log darf nicht geloescht werden"


def test_verwalte_archiv_haelt_das_byte_budget_ein(tmp_path):
    """Ohne Byte-Budget koennte das log2ram-tmpfs (128M) auf dem Pi
    gesprengt werden - die Zeitgrenze allein reicht dafuer nicht."""
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("x\n", encoding="utf-8")
    for i in range(5):
        p = tmp_path / f"heizungssteuerung.log.2026-10-{10 + i:02d}"
        p.write_text("y" * 1000, encoding="utf-8")

    verwalte_archiv(str(log), keep_tage=3650, max_gesamt_bytes=2500)

    uebrig = sorted(tmp_path.glob("heizungssteuerung.log.2026-*"))
    gesamt = sum(p.stat().st_size for p in uebrig)
    assert gesamt <= 2500, f"Byte-Budget verletzt: {gesamt} Bytes"
    # Die aeltesten sind geopfert, die juengsten bleiben.
    assert not (tmp_path / "heizungssteuerung.log.2026-10-10").exists()
    assert (tmp_path / "heizungssteuerung.log.2026-10-14").exists()


def test_verwalte_archiv_behaelt_die_aktive_datei(tmp_path):
    log = tmp_path / "heizungssteuerung.log"
    log.write_text("aktiv\n", encoding="utf-8")
    verwalte_archiv(str(log), keep_tage=0, max_gesamt_bytes=0)
    assert log.exists(), "die aktive Logdatei darf nie geloescht werden"
