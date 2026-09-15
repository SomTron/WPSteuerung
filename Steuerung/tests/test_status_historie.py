"""Tests fuer den /status-Hotspot: historisches 14-Tage-PV-Mittel (Wh).

Hintergrund: /status wird vom Dashboard alle 5 s abgefragt und las frueher bei
jedem Aufruf die KOMPLETTE heizungsdaten.csv mit pd.read_csv() (alle 20
Spalten, >120k Zeilen) - auf dem Pi ein RAM-Spike, der am 15.09. zum OOM-Kill
fuehrte (journal: status=9/KILL). Jetzt: stdlib csv, nur Kopf + Tail-Teil,
nur die zwei benoetigten Spalten und ein 30-Minuten-Cache.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402


@pytest.fixture(autouse=True)
def _cache_leeren():
    """Cache vor/nach jedem Test zuruecksetzen (Modulzustand!)."""
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None
    yield
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None


def _csv_schreiben(pfad, tage=3, feedin=1000.0):
    """Schreibt pro Tag 4 Viertelstunden-Zeilen (Summe je Tag = 1000 Wh)."""
    jetzt = datetime.now()
    zeilen = ["Zeitstempel,FeedinPower,BatPower,ACPower"]
    for t in range(tage):
        tag = (jetzt - timedelta(days=t)).strftime("%Y-%m-%d")
        for minute in (0, 15, 30, 45):
            stunde = 12 if minute < 60 else 12
            zeilen.append(f"{tag} {stunde:02d}:{minute:02d}:00,{feedin},0.0,500.0")
    with open(pfad, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")


def test_berechnet_mittel_ohne_pandas(tmp_path):
    """Korrektes Ergebnis - und zwar ohne pandas (stdlib csv)."""
    p = tmp_path / "hist.csv"
    _csv_schreiben(str(p))

    wert = api._historisches_wh_qm(str(p))

    # 4x 1000 W * 15/60 h = 1000 Wh pro Tag
    assert wert == pytest.approx(1000.0)


def test_excel_seriennummer_wird_erkannt():
    """Die CSV kann Excel-Seriennummern enthalten (Datum als Zahl)."""
    # 45000 Tage seit 1899-12-30
    assert api._parse_zeitstempel("45000") is not None
    assert api._parse_zeitstempel("2026-09-15 12:00:00") == datetime(2026, 9, 15, 12, 0, 0)
    assert api._parse_zeitstempel("") is None
    assert api._parse_zeitstempel("keinDatum") is None


def test_zweiter_aufruf_kommt_aus_dem_cache(tmp_path, monkeypatch):
    p = tmp_path / "hist.csv"
    _csv_schreiben(str(p))

    aufrufe = {"n": 0}
    echt = api._berechne_hist_wh_qm

    def spion(csv_path):
        aufrufe["n"] += 1
        return echt(csv_path)

    monkeypatch.setattr(api, "_berechne_hist_wh_qm", spion)

    erst = api._historisches_wh_qm(str(p))
    zweit = api._historisches_wh_qm(str(p))

    assert erst == zweit
    assert aufrufe["n"] == 1, "Der 5-s-Poll darf die CSV nicht jedes Mal neu lesen"


def test_fehlende_datei_liefert_none(tmp_path):
    fehlt = tmp_path / "gibts_nicht.csv"
    assert api._historisches_wh_qm(str(fehlt)) is None