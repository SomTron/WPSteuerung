"""Tests fuer die Daempfung von Neustart-Schleifen.

Hintergrund: Die Unit startet den Dienst mit Restart=always/RestartSec=10
und BEWUSST ohne StartLimit - eine Heizungssteuerung darf nach einem
Absturz nicht aufgeben. Startet der Dienst aber dauerhaft fehl, entstehen
8.640 Neustarts pro Tag mit Stacktrace im Journal.

Die Daempfung: ab dem dritten Fehlstart in 10 Minuten wartet der Prozess
in-place, statt sich beenden zu lassen. Der Dienst versucht es weiter -
denn der Fehler kann weggehen (z. B. ein Sensor kommt zurueck).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

from start_daempfung import (  # noqa: E402
    fehlstart_verzeichnen,
    fehlstart_zuruecksetzen,
    fehlstarts_im_fenster,
    warte_bei_fehlerschleife,
)


@pytest.fixture
def historie(tmp_path):
    return str(tmp_path / "startfehler.json")


class TestHistorie:
    def test_fehlstart_wird_gezählt(self, historie):
        anzahl = fehlstart_verzeichnen(historie, jetzt=datetime(2026, 10, 21, 10, 0))
        assert anzahl == 1
        assert fehlstarts_im_fenster(historie, jetzt=datetime(2026, 10, 21, 10, 1)) == 1

    def test_alte_fehlstarts_zaehlen_nicht_mehr(self, historie):
        fehlstart_verzeichnen(historie, jetzt=datetime(2026, 10, 21, 10, 0))
        # 30 Minuten spaeter: das alte liegt ausserhalb des Fensters.
        assert fehlstarts_im_fenster(
            historie, jetzt=datetime(2026, 10, 21, 10, 30)
        ) == 0

    def test_kaputte_datei_ist_kein_fehler(self, historie):
        with open(historie, "w", encoding="utf-8") as f:
            f.write("kein json")
        assert fehlstarts_im_fenster(historie) == 0
        # Und ein neuer Eintrag funktioniert trotzdem.
        assert fehlstart_verzeichnen(historie) == 1

    def test_zuruecksetzen_entfernt_die_historie(self, historie):
        fehlstart_verzeichnen(historie)
        assert os.path.exists(historie)
        fehlstart_zuruecksetzen(historie)
        assert not os.path.exists(historie)

    def test_zuruecksetzen_ohne_datei_ist_kein_fehler(self, historie):
        fehlstart_zuruecksetzen(historie)   # darf nicht werfen


class TestDaempfung:
    def test_bei_den_ersten_starts_keine_wartezeit(self, historie):
        schlaefzeiten = []
        for _ in range(2):
            warte = warte_bei_fehlerschleife(
                historie, schlaf=schlaefzeiten.append,
                jetzt=datetime(2026, 10, 21, 10, 0),
            )
            assert warte == 0.0
        assert schlaefzeiten == []

    def test_ab_drittem_start_wird_gewartet(self, historie):
        schlaefzeiten = []
        for _ in range(3):
            warte = warte_bei_fehlerschleife(
                historie, schlaf=schlaefzeiten.append,
                jetzt=datetime(2026, 10, 21, 10, 0),
            )
        assert warte > 0, "nach dem dritten Fehlstart muss gewartet werden"
        assert len(schlaefzeiten) == 1

    def test_die_wartezeit_ist_der_fehler_abstand(self, historie):
        schlaefzeiten = []
        for _ in range(3):
            warte_bei_fehlerschleife(
                historie, schlaf=schlaefzeiten.append,
                jetzt=datetime(2026, 10, 21, 10, 0),
                max_start=3, wartezeit_min=5,
            )
        assert schlaefzeiten == [300.0]

    def test_nach_erfolgreichem_start_ist_die_historie_weg(self, historie):
        fehlstart_verzeichnen(historie)
        fehlstart_zuruecksetzen(historie)
        # Danach zaehlt ein neuer Fehlstart wieder ab eins.
        assert fehlstart_verzeichnen(historie) == 1
