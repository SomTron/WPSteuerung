"""Tests fuer die SD-Karten-Fruehwarnung.

Hintergrund: Auf dem Pi liegt die gesamte Historie auf der SD-Karte
(Heizungsdaten-CSV, Entscheidungslog, last_state.txt). Ein Ausfall kommt
nicht ueraschend: vorher meldet die Karte I/O-Fehler im Kernel-Log und
das Dateisystem zaehlt Fehler.

Der Check wertet vier Signale aus: Kernel-Meldungen, Dateisystem-
Fehlerzaehler, Schreibtest und CSV-Konsistenz.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from sd_health import (  # noqa: E402
    csv_konsistenz,
    kernel_fehler,
    pruefe,
    schreibtest,
)


# ------------------------------------------------------------ Kernel-Log


def test_kernel_fehler_werden_erkannt():
    zeilen = [
        "2026-10-21 10:00:00 kernel: EXT4-fs (mmcblk0p2): error 5",
        "2026-10-21 10:00:01 kernel: Buffer I/O error, dev mmcblk0p2",
        "2026-10-21 10:00:02 kernel: mmcblk0: error -84 while reading",
        "2026-10-21 10:00:03 kernel: Normaler Logeintrag ohne Fehler",
    ]
    treffer = kernel_fehler(zeilen)
    assert len(treffer) == 3
    assert not any("Normaler Logeintrag" in t for t in treffer)


def test_ohne_kernel_fehler_keine_treffer():
    zeilen = [
        "2026-10-21 10:00:00 kernel: Started WPSteuerung",
        "2026-10-21 10:00:01 kernel: usb 1-1: new device",
    ]
    assert kernel_fehler(zeilen) == []


def test_leeres_kernel_log_ist_kein_fehler():
    assert kernel_fehler([]) == []


# ------------------------------------------------------------ Schreibtest


def test_schreibtest_ist_ok(tmp_path):
    ergebnis = schreibtest(str(tmp_path))
    assert ergebnis["ok"] is True
    # Die Testdatei muss wieder weg sein.
    assert not os.path.exists(tmp_path / ".sd_health_test")


def test_schreibtest_meldet_fehlendes_verzeichnis(tmp_path):
    ergebnis = schreibtest(str(tmp_path / "gibtsnicht"))
    assert ergebnis["ok"] is False
    assert "existiert nicht" in ergebnis["fehler"]


# ------------------------------------------------------------ CSV-Check


def test_csv_konsistenz_ist_ok(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("Zeitstempel,a\n2026-10-21 10:00:00,1\n", encoding="utf-8")
    ergebnis = csv_konsistenz(str(csv))
    assert ergebnis["ok"] is True
    assert ergebnis["zeilen"] == 1


def test_leere_csv_ist_nicht_plausibel(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("", encoding="utf-8")
    ergebnis = csv_konsistenz(str(csv))
    assert ergebnis["ok"] is False
    assert "leer" in ergebnis["fehler"]


def test_csv_ohne_kopfzeile_ist_nicht_plausibel(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("2026-10-21 10:00:00,1\n", encoding="utf-8")
    ergebnis = csv_konsistenz(str(csv))
    assert ergebnis["ok"] is False


def test_fehlende_csv_ist_kein_fehler(tmp_path):
    """Die CSV entsteht erst beim ersten Lauf - das ist kein Fehler."""
    ergebnis = csv_konsistenz(str(tmp_path / "gibtsnicht.csv"))
    assert ergebnis["ok"] is True


# ------------------------------------------------------------ Gesamtcheck


def test_pruefe_ist_ok_ohne_befund(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("Zeitstempel,a\n2026-10-21 10:00:00,1\n", encoding="utf-8")

    status = pruefe(str(tmp_path), str(csv), dmesg_zeilen=[])

    assert status["sd_ok"] is True
    assert status["hinweise"] == []
    assert status["kernel_fehler_anzahl"] == 0


def test_pruefe_meldet_kernel_fehler(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("Zeitstempel,a\n2026-10-21 10:00:00,1\n", encoding="utf-8")

    status = pruefe(
        str(tmp_path), str(csv),
        dmesg_zeilen=["kernel: Buffer I/O error, dev mmcblk0p2"],
    )

    assert status["sd_ok"] is False
    assert status["kernel_fehler_anzahl"] == 1
    assert any("Kernel-Log" in h for h in status["hinweise"])


def test_pruefe_meldet_kaputte_csv(tmp_path):
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text("", encoding="utf-8")

    status = pruefe(str(tmp_path), str(csv), dmesg_zeilen=[])

    assert status["sd_ok"] is False
    assert any("CSV" in h for h in status["hinweise"])
