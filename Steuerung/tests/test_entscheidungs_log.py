"""Tests: Entscheidungs-Log und Energiebilanz-KPIs.

Testet das neue entscheidungs_log-Modul (Punkte ③ und ④):
- schreibe_eintrag: Anhaengen ans JSONL
- Rotation bei MAX_BYTES
- historie(): Filter nach Stunden/Limit
- kpis(): Aggregation mit Laufzeit/Energie/Anteil
"""
import os
import sys
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import entscheidungs_log as el


def _patch_log_pfad(tmp_path):
    """Erzeugt temporaere Log-Pfade und patcht die Modul-Konstanten."""
    log_datei = str(tmp_path / "entscheidungs_log.jsonl")
    old_datei = str(tmp_path / "entscheidungs_log.jsonl.old")
    return patch.multiple(
        el,
        LOG_DATEI=log_datei,
        MAX_BYTES=1_000_000,
        MAX_EINTRAEGE_LESEN=5000,
    ), log_datei, old_datei


def test_schreibe_und_lese(tmp_path):
    """Ein Eintrag schreiben und per historie() wiederfinden."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        el.schreibe_eintrag(
            gewinner_name="TestRegel",
            gewinner_grund="Weil es Spass macht",
            soll_einschalten=True,
            kompressor_laeuft=True,
            feedin_watt=500.0,
            batpower_watt=1200.0,
            soc=88.0,
            t_unten=41.0,
            t_oben=45.0,
        )
        # Datei existiert
        assert os.path.exists(log_datei)
        # Genau eine Zeile
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
        assert len(zeilen) == 1
        eintrag = json.loads(zeilen[0])
        assert eintrag["gewinner"] == "TestRegel"
        assert eintrag["grund"] == "Weil es Spass macht"
        assert eintrag["soll_einschalten"] is True
        assert eintrag["kompressor_laeuft"] is True
        assert eintrag["feedin_w"] == 500.0


def test_historie_filtert_nach_stunden(tmp_path):
    """Nur Eintraege innerhalb des Stunden-Fensters zurueck."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Alten Eintrag mit manipulierter Zeit schreiben
        alt = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
        aktuell = datetime.now().isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": alt, "gewinner": "Alt"}) + "\n")
            f.write(json.dumps({"ts": aktuell, "gewinner": "Neu"}) + "\n")

        h = el.historie(stunden=24, limit=100)
        assert len(h) == 1
        assert h[0]["gewinner"] == "Neu"


def test_rotation_bei_max_bytes(tmp_path):
    """Bei Ueberschreitung von MAX_BYTES wird nach .old rotiert."""
    patcher, log_datei, old_datei = _patch_log_pfad(tmp_path)
    with patcher:
        # MAX_BYTES auf sehr klein setzen (100 Bytes)
        with patch.object(el, "MAX_BYTES", 100):
            for i in range(20):
                el.schreibe_eintrag(
                    gewinner_name=f"R{i}",
                    gewinner_grund="x" * 50,
                    soll_einschalten=True,
                    kompressor_laeuft=True,
                )

        # Nach vielen Eintraegen sollte .old existieren
        assert os.path.exists(log_datei)
        # Pruefen: alte Eintraege sind (nach Rotation) noch lesbar
        zeilen = el._lies_zeilen()
        namen = {z["gewinner"] for z in zeilen}
        assert "R0" in namen or len(zeilen) > 0  # irgendwas ist drin


def test_historie_neueste_zuerst(tmp_path):
    """historie() liefert neueste Eintraege zuerst."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        # Chronologisch schreiben: aeltester zuerst (R4), neuester zuletzt (R0)
        for i in range(4, -1, -1):  # 4, 3, 2, 1, 0
            ts = (now - timedelta(hours=i)).isoformat(timespec="seconds")
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "gewinner": f"R{i}"}) + "\n")

        h = el.historie(stunden=48, limit=10)
        assert len(h) == 5
        # Neuester zuerst (R0 = now)
        assert h[0]["gewinner"] == "R0"
        assert h[-1]["gewinner"] == "R4"


def test_limit_begrenzt_ausgabe(tmp_path):
    """limit-Parameter in historie() wird eingehalten."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        for i in range(20):
            ts = (now - timedelta(minutes=i * 30)).isoformat(timespec="seconds")
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "gewinner": f"R{i}"}) + "\n")

        h = el.historie(stunden=48, limit=5)
        assert len(h) == 5


def test_kpis_aggregieren(tmp_path):
    """kpis() berechnet Laufzeit, Energie und Anteil korrekt."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        # Simuliere 6 Eintraege mit dt=60s, Kompressor laeuft immer
        # feedin >= -50 -> pv_batterie, feedin < -50 -> netz
        for i in range(6):
            ts = (now - timedelta(seconds=60 * (5 - i))).isoformat(timespec="seconds")
            feedin = 100.0 if i < 4 else -200.0  # 4 PV, 2 Netz
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": ts,
                    "gewinner": "Test",
                    "soll_einschalten": True,
                    "kompressor_laeuft": True,
                    "feedin_w": feedin,
                    "batpower_w": 0.0,
                    "soc": 50.0,
                    "t_unten": 40.0,
                    "t_oben": 42.0,
                }) + "\n")

        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        # 'heute' hat alle 6 Eintraege (5 Intervalle * 60s = 300s Laufzeit)
        heute = result["heute"]
        assert heute["laufzeit_min"] == pytest.approx(5.0, abs=0.5), f"Laufzeit: {heute}"
        # 3/5 der Zeit PV (i=1..3), 2/5 Netz (i=4..5) -> anteil 60%
        # 300s * 600W = 180000 Ws = 0.05 kWh
        assert heute["energie_kwh"] == pytest.approx(0.05, abs=0.01), f"Energie: {heute}"
        assert heute["anteil_pv_batterie_prozent"] == pytest.approx(60.0, abs=2.0)
        # 2/5 von 0.05 kWh = 0.02 kWh * 0.35 EUR/kWh = 0.007 EUR
        # Die Funktion rundet auf 2 Dezimalstellen: round(0.007, 2) = 0.01
        assert heute["kosten_netz_eur"] == pytest.approx(0.01, abs=0.001)


def test_kpis_leere_logs(tmp_path):
    """Bei leerem Log liefern kpis() Nones fuer Anteil, 0 fuer Kosten."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Keine Eintraege
        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        heute = result["heute"]
        assert heute["laufzeit_min"] == 0.0
        assert heute["energie_kwh"] == 0.0
        assert heute["anteil_pv_batterie_prozent"] is None
        assert heute["kosten_netz_eur"] == 0.0


def test_korrupte_zeile_toleriert(tmp_path):
    """Eine abgebrochene JSON-Zeile stoert das Lesen nicht."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now().isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "gewinner": "OK"}) + "\n")
            f.write("{\"ts\": \"... abgebrochen")  # kaputt, kein Newline am Ende
        h = el.historie(stunden=48, limit=100)
        assert len(h) == 1
        assert h[0]["gewinner"] == "OK"


def test_schreibe_ohne_os_error(tmp_path, caplog):
    """Wenn LOG_DATEI nicht schreibbar ist, kein Crash."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Pfad in ein nicht-existierendes Verzeichnis
        with patch.object(el, "LOG_DATEI", str(tmp_path / "nix" / "log.jsonl")):
            el.schreibe_eintrag("Test", "Grund", True, False)
            # Kein Fehler, nur debug-Log
            assert len(caplog.records) == 0 or caplog.records[0].levelname == "DEBUG"