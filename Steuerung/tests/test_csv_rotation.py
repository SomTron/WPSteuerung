"""Tests fuer die CSV-Monatsrotation (utils.rotiere_csv_monatlich).

Hintergrund: heizungsdaten.csv waechst seit Jahren ungebremst und wird von
den Telegram-Charts bei jeder Anfrage via pandas gelesen. Die Rotation
archiviert nach Monatswechsel zu heizungsdaten_YYYY-MM.csv; relevante_csv_dateien()
liefert Charts aktuelle Datei + Vormonats-Archiv, damit 24h-Verlaeufe ueber
die Monatsgrenze funktionieren.
"""
import os
from datetime import datetime

import pytest

from utils import (
    EXPECTED_CSV_HEADER,
    relevante_csv_dateien,
    rotiere_csv_monatlich,
)

HEADER = ",".join(EXPECTED_CSV_HEADER)


def schreibe_csv(pfad, zeitstempel_liste):
    """Minimal-gueltige CSV schreiben (nur Zeitstempel-Spalte gefuellt)."""
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(HEADER + "\n")
        for ts in zeitstempel_liste:
            f.write(f"{ts},1,2,3,4,5,0,0,0,0,0,0,0,0,40,45,100,0,Netz,2.75\n")


@pytest.fixture
def csv_pfad(tmp_path):
    return str(tmp_path / "csv log" / "heizungsdaten.csv")


# ── rotiere_csv_monatlich ─────────────────────────────────────────────

class TestRotation:
    def test_fehlende_datei_ist_kein_fehler(self, tmp_path):
        pfad = str(tmp_path / "gibts" / "nicht.csv")
        assert rotiere_csv_monatlich(pfad) is None

    def test_header_only_datei_wird_nicht_rotiert(self, csv_pfad, tmp_path):
        os.makedirs(os.path.dirname(csv_pfad))
        schreibe_csv(csv_pfad, [])
        assert rotiere_csv_monatlich(csv_pfad) is None
        assert os.path.exists(csv_pfad)

    def test_aktueller_monat_wird_nicht_rotiert(self, csv_pfad):
        os.makedirs(os.path.dirname(csv_pfad))
        heute = datetime(2026, 8, 15)
        schreibe_csv(csv_pfad, ["2026-08-01 10:00:00", "2026-08-14 22:30:00"])
        assert rotiere_csv_monatlich(csv_pfad, heute=heute) is None
        assert os.path.exists(csv_pfad)

    def test_vormonat_wird_archiviert(self, csv_pfad, tmp_path):
        os.makedirs(os.path.dirname(csv_pfad))
        inhalt = ["2026-07-01 00:05:00", "2026-07-31 23:55:00"]
        schreibe_csv(csv_pfad, inhalt)

        archiv = rotiere_csv_monatlich(csv_pfad, heute=datetime(2026, 8, 1, 0, 10))

        assert archiv == str(tmp_path / "csv log" / "heizungsdaten_2026-07.csv")
        assert not os.path.exists(csv_pfad), "Alte Datei muss verschwunden sein"
        assert os.path.exists(archiv), "Archiv muss existieren"
        # Inhalt vollstaendig erhalten
        with open(archiv, encoding="utf-8") as f:
            zeilen = [z for z in f.read().splitlines() if z.strip()]
        assert len(zeilen) == 3  # Header + 2 Datenzeilen

    def test_legacy_mehrmonats_datei_benannt_sich_nach_letztem_eintrag(self, csv_pfad, tmp_path):
        """Nie rotierte Alt-Datei (Jan-Jul): Archivname folgt dem LETZTEM Eintrag."""
        os.makedirs(os.path.dirname(csv_pfad))
        schreibe_csv(csv_pfad, [
            "2026-01-05 12:00:00",
            "2026-04-05 12:00:00",
            "2026-07-20 08:00:00",
        ])
        archiv = rotiere_csv_monatlich(csv_pfad, heute=datetime(2026, 8, 2))
        assert archiv.endswith("heizungsdaten_2026-07.csv")

    def test_doppel_rotation_bekommt_eindeutigen_namen(self, csv_pfad, tmp_path):
        os.makedirs(os.path.dirname(csv_pfad))
        schreibe_csv(csv_pfad, ["2026-07-02 06:00:00"])
        erster = rotiere_csv_monatlich(csv_pfad, heute=datetime(2026, 8, 1))
        # Zweite Datei mit gleichem Vormonat -> Kollision mit erstem Archiv
        schreibe_csv(csv_pfad, ["2026-07-03 06:00:00"])
        zweiter = rotiere_csv_monatlich(csv_pfad, heute=datetime(2026, 8, 1))

        assert erster != zweiter
        assert os.path.exists(erster) and os.path.exists(zweiter)

    def test_unparsebare_zeilen_sind_kein_crash(self, csv_pfad):
        os.makedirs(os.path.dirname(csv_pfad))
        with open(csv_pfad, "w", encoding="utf-8") as f:
            f.write(HEADER + "\n")
            f.write("kein,zeitstempel,hier\n")
        assert rotiere_csv_monatlich(csv_pfad, heute=datetime(2026, 8, 1)) is None


# ── relevante_csv_dateien ─────────────────────────────────────────────

class TestRelevanteDateien:
    def test_liefert_aktuelle_und_vormonats_archiv(self, csv_pfad, tmp_path):
        verzeichnis = os.path.join(str(tmp_path), "csv log")
        os.makedirs(verzeichnis)
        archiv = os.path.join(verzeichnis, "heizungsdaten_2026-07.csv")
        schreibe_csv(archiv, ["2026-07-31 23:59:00"])
        schreibe_csv(csv_pfad, ["2026-08-01 00:05:00"])

        dateien = relevante_csv_dateien(csv_pfad, jetzt=datetime(2026, 8, 1, 12, 0))

        assert dateien == [archiv, csv_pfad], "Chronologisch: Archiv zuerst"

    def test_ohne_archiv_nur_aktuelle_datei(self, csv_pfad):
        os.makedirs(os.path.dirname(csv_pfad))
        schreibe_csv(csv_pfad, ["2026-08-01 00:05:00"])

        dateien = relevante_csv_dateien(csv_pfad, jetzt=datetime(2026, 8, 15))

        assert dateien == [csv_pfad]

    def test_ohne_irgendeine_datei_leer(self, tmp_path):
        pfad = str(tmp_path / "leer" / "heizungsdaten.csv")
        assert relevante_csv_dateien(pfad, jetzt=datetime(2026, 8, 15)) == []

    def test_jahreswechsel_januar_findet_dezember(self, csv_pfad, tmp_path):
        verzeichnis = os.path.join(str(tmp_path), "csv log")
        os.makedirs(verzeichnis)
        archiv = os.path.join(verzeichnis, "heizungsdaten_2025-12.csv")
        schreibe_csv(archiv, ["2025-12-31 23:59:00"])
        schreibe_csv(csv_pfad, ["2026-01-01 00:05:00"])

        dateien = relevante_csv_dateien(csv_pfad, jetzt=datetime(2026, 1, 1, 8, 0))

        assert dateien == [archiv, csv_pfad]
