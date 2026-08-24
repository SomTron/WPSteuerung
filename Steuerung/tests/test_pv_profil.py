# -*- coding: utf-8 -*-
"""Tests fuer das gelernte PV-Profil (Punkt E)."""
import csv
import sys
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")

from pv_profil import berechne_profil, get_peak_leistung, get_erwartete_pv_watt  # noqa: E402


def schreibe_test_csv(pfad, eintraege):
    """eintraege: Liste von (datetime, feedin_watt)."""
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Zeitstempel", "FeedinPower"])
        for ts, watt in eintraege:
            w.writerow([ts.isoformat(), watt])


class TestBerechneProfil:
    def test_leere_oder_fehlende_datei_liefert_null_profil(self, tmp_path):
        # Fehlende Datei -> 24-Stunden-Nullprofil (konsistentes Format)
        profil = berechne_profil(csv_path=str(tmp_path / "gibtsnicht.csv"))
        assert len(profil) == 24
        assert all(v == 0.0 for v in profil.values())

        leer = tmp_path / "leer.csv"
        leer.write_text("Zeitstempel,FeedinPower\n", encoding="utf-8")
        profil = berechne_profil(csv_path=str(leer))
        assert all(v == 0.0 for v in profil.values())
        assert len(profil) == 24

    def test_stundenscharfe_mittelung(self, tmp_path):
        # Basis: vor 2 Tagen um Mitternacht (immer innerhalb des 14-Tage-Fensters)
        basis = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        zeilen = []
        # 12 Uhr: 1000 W und 2000 W -> Mittel 1500
        for d in range(2):
            zeilen.append((basis + timedelta(days=d, hours=12), 1000.0 + d * 1000))
        # 13 Uhr: 500 W -> Mittel 500
        zeilen.append((basis + timedelta(hours=13), 500.0))
        pfad = str(tmp_path / "daten.csv")
        schreibe_test_csv(pfad, zeilen)

        profil = berechne_profil(csv_path=pfad)
        assert profil[12] == 1500.0
        assert profil[13] == 500.0
        assert profil[3] == 0.0

    def test_alte_zeilen_werden_ignoriert(self, tmp_path):
        jetzt = datetime.now()
        alt = jetzt - timedelta(days=30)  # ausserhalb 14-Tage-Fenster
        frisch = jetzt - timedelta(days=1)
        pfad = str(tmp_path / "daten.csv")
        schreibe_test_csv(pfad, [(alt, 9999.0), (frisch, 800.0)])

        profil = berechne_profil(csv_path=pfad, tage=14)
        stunde = frisch.hour
        assert profil[stunde] == 800.0
        if alt.hour != stunde:
            assert profil[alt.hour] == 0.0

    def test_kaputte_zeilen_werden_toleriert(self, tmp_path):
        pfad = tmp_path / "kaputt.csv"
        with open(pfad, "w", encoding="utf-8") as f:
            f.write("Zeitstempel,FeedinPower\n")
            f.write("KEIN-DATUM,100\n")
            f.write(f"{datetime.now().isoformat()},nicht-eine-zahl\n")
            f.write(f"{datetime.now().isoformat()},700\n")
        profil = berechne_profil(csv_path=str(pfad))
        assert profil.get(datetime.now().hour) == 700.0

    def test_negative_feedin_zaehlen_nicht(self, tmp_path):
        jetzt = datetime.now()
        pfad = str(tmp_path / "neg.csv")
        schreibe_test_csv(pfad, [(jetzt, -500.0), (jetzt + timedelta(hours=1), 300.0)])
        profil = berechne_profil(csv_path=pfad)
        assert profil[jetzt.hour] == 0.0
        assert profil[(jetzt + timedelta(hours=1)).hour] == 300.0


class TestPeakUndErwartung:
    def test_peak(self, tmp_path):
        basis = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        pfad = str(tmp_path / "p.csv")
        schreibe_test_csv(pfad, [
            (basis + timedelta(hours=11), 400.0),
            (basis + timedelta(hours=12), 2500.0),
            (basis + timedelta(hours=15), 900.0),
        ])
        profil = berechne_profil(csv_path=pfad)
        assert get_peak_leistung(profil) == 2500.0
        assert get_peak_leistung({}) == 0.0

    def test_get_erwartete_pv_watt(self, tmp_path):
        basis = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
        pfad = str(tmp_path / "e.csv")
        schreibe_test_csv(pfad, [(basis + timedelta(hours=12), 1800.0)])
        profil = berechne_profil(csv_path=pfad)
        assert get_erwartete_pv_watt(12, profil=profil) == 1800.0
        assert get_erwartete_pv_watt(23, profil=profil) == 0.0
