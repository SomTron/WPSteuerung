# -*- coding: utf-8 -*-
"""Befund 05.11.2026: `sicherheit.notfall_c` steuert KEINE Regel.

Der Notfallschutz (Prio 110) entscheidet ausschliesslich ueber
`notfallschutz.einschalten_bei_c` (Strategie 'auto': oben -> mittig -> unten).
Die Status-API lieferte dagegen `sicherheit.notfall_c` - einen Wert, den kein
Code liest. Weil beide zufaellig 36 waren, fiel es nie auf; ein Eintrag von
z. B. 40 in `sicherheit.notfall_c` haette gar nichts bewirkt.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from json_config import WPSteuerungConfig  # noqa: E402
from priority_control import evaluate_notfallschutz  # noqa: E402

TEMPS = {"oben": 34.0, "mittig": 34.0, "unten": 34.0}


def test_wirksame_schwelle_ist_die_notfallschutz_config():
    """34 C liegt unter 36 C -> EIN, unabhaengig von `sicherheit.notfall_c`."""
    assert evaluate_notfallschutz(WPSteuerungConfig().notfallschutz, TEMPS).einschalten is True

    toter_wert = WPSteuerungConfig()
    toter_wert.sicherheit.notfall_c = 20.0   # wuerde 34 C als Notfall deuten
    assert evaluate_notfallschutz(toter_wert.notfallschutz, TEMPS).einschalten is True

    scharf = WPSteuerungConfig()
    scharf.notfallschutz.einschalten_bei_c = 33.0
    assert evaluate_notfallschutz(scharf.notfallschutz, TEMPS).einschalten is not True


def test_api_liefert_die_wirksame_schwelle():
    """Quelltext-Guard: die Statusanzeige darf nicht den toten Wert senden."""
    pfad = os.path.join(os.path.dirname(__file__), "..", "api.py")
    with open(pfad, encoding="utf-8") as fh:
        quelle = fh.read()
    assert '"sicherheit_notfall": pc.notfallschutz.einschalten_bei_c' in quelle
    assert '"sicherheit_notfall": pc.sicherheit.notfall_c' not in quelle
