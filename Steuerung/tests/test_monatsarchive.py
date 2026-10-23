# -*- coding: utf-8 -*-
"""Monatsarchive: EINE Definition fuer Rotieren, Aufraeumen und Auswerten.

BUGFIX (23.10.2026): Auf die Frage "ist das ein Monatsarchiv?" gab es drei
Antworten - `rotiere_csv_monatlich` erzeugt bei einer Kollision
"heizungsdaten_YYYY-MM_HHMMSS.csv", waehrend `raeume_csv_archiv_auf` nur
Namen der Laenge 7 akzeptierte und `api._berechne_hist_wh_qm` "%Y-%m"
parste. Eine doppelt rotierte Datei war damit fuer jede Auswertung
unsichtbar und wurde nie aufgeraeumt.

Diese Tests sichern die gemeinsame Definition und ihre Folgen:
  * Kollisionsdateien werden gefunden (Auswertung, Charts, Aufraeumen),
  * Altlasten wie "heizungsdaten_alt.csv" werden ignoriert,
  * das Wh/m2-Mittel laeuft ueber eine Monatsgrenze (bisher ungetestet).
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402
from utils import monatsarchive, raeume_csv_archiv_auf, relevante_csv_dateien  # noqa: E402

KOPF = "Zeitstempel,FeedinPower\n"


def _csv(pfad, zeilen):
    """Schreibt eine CSV mit Kopfzeile; zeilen = [(ISO-Zeitstempel, Watt)]."""
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write(KOPF)
        for ts, watt in zeilen:
            f.write(f"{ts},{watt}\n")
    return str(pfad)


# --- Erkennung ---------------------------------------------------------------

def test_normale_archive_werden_erkannt(tmp_path):
    _csv(tmp_path / "heizungsdaten_2026-08.csv", [])
    _csv(tmp_path / "heizungsdaten_2026-09.csv", [])
    gefunden = monatsarchive(str(tmp_path), "heizungsdaten")
    assert sorted(gefunden) == ["2026-08", "2026-09"]
    assert gefunden["2026-08"][-1].endswith("heizungsdaten_2026-08.csv")


def test_kollisionsdatei_wird_erkannt_und_ist_die_neueste(tmp_path):
    """Bei doppelter Rotation entsteht "..._YYYY-MM_HHMMSS.csv"."""
    _csv(tmp_path / "heizungsdaten_2026-07.csv", [])
    _csv(tmp_path / "heizungsdaten_2026-07_143000.csv", [])
    gefunden = monatsarchive(str(tmp_path), "heizungsdaten")
    assert len(gefunden["2026-07"]) == 2, "Kollisionsdatei fehlt"
    assert gefunden["2026-07"][-1].endswith("heizungsdaten_2026-07_143000.csv"), (
        "die spaeter rotierte Datei muss als neueste gelten"
    )


def test_altlasten_und_ungueltige_monate_werden_ignoriert(tmp_path):
    """Regression: "heizungsdaten_alt.csv" (44 MB) wurde bei jedem /status
    geglobbt und verworfen - sie ist kein Monatsarchiv."""
    _csv(tmp_path / "heizungsdaten_alt.csv", [])
    _csv(tmp_path / "heizungsdaten_2026-13.csv", [])      # Monat 13 gibt es nicht
    _csv(tmp_path / "heizungsdaten_2026-08_abc.csv", [])  # kein HHMMSS
    _csv(tmp_path / "heizungsdaten_backup.csv", [])
    assert monatsarchive(str(tmp_path), "heizungsdaten") == {}


def test_fremde_basis_wird_nicht_gemischt(tmp_path):
    _csv(tmp_path / "zyklen_2026-08.csv", [])
    assert monatsarchive(str(tmp_path), "heizungsdaten") == {}


def test_fehlendes_verzeichnis_ist_leer(tmp_path):
    assert monatsarchive(str(tmp_path / "gibtsnicht"), "heizungsdaten") == {}


# --- Folgen: Aufraeumen und Charts ------------------------------------------

def test_aufraeumen_loescht_auch_kollisionsdateien(tmp_path):
    alt = _csv(tmp_path / "heizungsdaten_2020-01.csv", [])
    alt_kollision = _csv(tmp_path / "heizungsdaten_2020-01_120000.csv", [])
    neu = _csv(tmp_path / "heizungsdaten_2026-09.csv", [])

    geloescht = raeume_csv_archiv_auf(
        str(tmp_path), "heizungsdaten", max_monate=24, heute=datetime(2026, 10, 23)
    )

    assert alt in geloescht
    assert alt_kollision in geloescht, "Kollisionsdatei blieb als Waise liegen"
    assert os.path.exists(neu)
    assert not os.path.exists(alt)


def test_charts_finden_kollisionsdatei_des_vormonats(tmp_path):
    archiv = _csv(tmp_path / "heizungsdaten_2026-09_153000.csv", [])
    aktuell = _csv(tmp_path / "heizungsdaten.csv", [])
    dateien = relevante_csv_dateien(
        str(tmp_path / "heizungsdaten.csv"), jetzt=datetime(2026, 10, 1, 0, 10)
    )
    assert dateien == [archiv, aktuell]


# --- Wh/m2-Mittel ueber die Monatsgrenze ------------------------------------

def _monatswechsel_csvs(verzeichnis, archiv_name):
    _csv(verzeichnis / archiv_name, [
        ("2026-08-31 23:50:00", 1000),
        ("2026-08-31 23:59:00", 1000),
    ])
    return _csv(verzeichnis / "heizungsdaten.csv", [
        ("2026-09-01 00:00:10", 1000),
        ("2026-09-01 00:01:10", 1000),
    ])


#: 08-31: 540 s + 70 s (ueber die Monatsgrenze) = 610 s * 1000 W
#: 09-01: 60 s + 60 s (Schaetzung des letzten Intervalls)
ERWARTET_MONATSWECHSEL = ((610 + 120) * 1000 / 3600) / 2


def test_wh_qm_mittel_liest_das_vormonatsarchiv(tmp_path):
    """14-Tage-Fenster ueber den Monatswechsel: Archiv + aktuelle Datei.

    Bisher fehlte ein Test fuer genau diesen Uebergang. Erwartung: Die
    Energie des 31.08. (aus dem Archiv) und des 01.09. (aktuelle Datei)
    gehen beide ein, und die Monatsgrenze erzeugt KEINE Riesenluecke
    (23:59 -> 00:00:10 ist ein regulaeres 70-s-Intervall).
    """
    verzeichnis = tmp_path / "csv log"
    verzeichnis.mkdir()
    aktuell = _monatswechsel_csvs(verzeichnis, "heizungsdaten_2026-08.csv")

    wert = api._berechne_hist_wh_qm(aktuell, tage=14, jetzt=datetime(2026, 9, 1, 2, 0))

    assert wert == pytest.approx(ERWARTET_MONATSWECHSEL, abs=0.5), f"{wert} != {ERWARTET_MONATSWECHSEL}"


def test_wh_qm_mittel_liest_kollisionsarchiv(tmp_path):
    """Regression: Ein doppelt rotierter Vormonat fiel still aus dem Mittel."""
    verzeichnis = tmp_path / "csv log"
    verzeichnis.mkdir()
    aktuell = _monatswechsel_csvs(verzeichnis, "heizungsdaten_2026-08_143000.csv")

    wert = api._berechne_hist_wh_qm(aktuell, tage=14, jetzt=datetime(2026, 9, 1, 2, 0))

    assert wert == pytest.approx(ERWARTET_MONATSWECHSEL, abs=0.5), (
        "das Kollisionsarchiv wurde nicht gelesen - der Vormonat fehlt"
    )
