"""Regression: CalcStart benutzte zwei verschiedene Zielzeiten.

Befund aus der statischen Regelanalyse 04.10.2026.

`evaluate_calculated_start` bestimmte die Zielzeit an zwei Stellen
getrennt:

  * fuer die Nachtsperre, die Restzeit und alle Meldungen: `ziel_uhr`
    = die GELERNTE Abend-Zielzeit (EWMA der ersten Abendzapfung),
  * fuer den Abbruch "Nach Zielzeit": `calc_cfg.target_uhr`, die
    KONFIGURIERTE Zielzeit (17:00).

Nachgerechnet mit gelernter Zielzeit 18:30, konfiguriert 17:00 - also
ganz normalem Abendduschverhalten:

  16:45  "1.1h Puffer reicht -> warte auf PV"
  17:00  "Nach Zielzeit"     <- Regel ab hier TOT
  18:20  "Nach Zielzeit"

Die Zapfgarantie fiel also genau fuer die Zeit aus, fuer die sie
existiert: zwischen 17:00 und der echten Dusche um 18:30 konnte keine
Regel mehr dafuer sorgen, dass der Speicher heiss ist.

Umgekehrt erzwingt eine zu FRUEH gelernte Zielzeit (14:00) von 11:00
bis 17:00 durchgehend "ZU SPAET! -> EIN (Notfall)" - sechs Stunden
Dauerstart, obwohl `target_uhr: 17` konfiguriert ist.

Nebenbefund: `f"{ziel_uhr:.0f}"` rundete 18.5 auf "18" - im Log las sich
"bis 18:00", gemeint war 18:30.
"""
import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control as pc
from json_config import WPSteuerungConfig

_CFG = WPSteuerungConfig(
    **json.load(
        open(
            os.path.join(os.path.dirname(pc.__file__), "wp_steuerung_parameter.json"),
            encoding="utf-8",
        )
    )
)
CALC = _CFG.calculated_start

# 40 C: unter Ziel, aber weit vom tmax entfernt - reine Pufferentscheidung
TEMPS = {"unten": 40.0, "mittig": 40.0, "oben": 46.0}
MIT_PV = {"feedin_watt": 4000.0, "pv_acpower": 5000.0, "forecast_wh_qm": 2000.0}


def _calc(h, m, gelernt=None, **kw):
    kw.pop("learned_target_hour", None)
    return pc.evaluate_calculated_start(
        CALC, TEMPS, h, m, learned_target_hour=gelernt, **kw
    )


def test_gelernte_zielzeit_haengt_den_abbruch_hinaus():
    """Kern-Regression: Der Abbruch muss der GELERNTEN Zielzeit folgen."""
    assert CALC.target_uhr == 17, "Ausgangslage des Befunds"

    # 17:30 liegt NACH der Konfiguration (17:00), aber VOR der gelernten
    # Zielzeit (18:30). Die Regel muss hier noch arbeiten.
    e = _calc(17, 30, gelernt=18.5, **MIT_PV)
    assert "Nach Zielzeit" not in e.grund, (
        f"Regel war vorzeitig tot: {e.grund}"
    )
    assert e.einschalten is True, e.grund


def test_abbruch_erst_nach_der_gelernten_zielzeit():
    e = _calc(18, 40, gelernt=18.5, **MIT_PV)
    assert "Nach Zielzeit" in e.grund, e.grund
    # Und die Meldung nennt die gelernte Zielzeit, nicht die konfigurierte.
    assert "18:30" in e.grund, e.grund


def test_zu_frueh_gelernte_zielzeit_erzwingt_keine_dauerstarts():
    """Gegenrichtung: gelernt 14:00 darf nicht bis 17:00 durchdruecken."""
    starts = []
    for h in range(9, 18):
        e = _calc(h, 0, gelernt=14.0, **MIT_PV)
        if e.einschalten is True and "ZU SPAET" in e.grund:
            starts.append(h)
    assert not starts, (
        f"Erzwungener Notfall-Start in den Stunden {starts} bei gelernter "
        f"Zielzeit 14:00"
    )


def test_ohne_gelernte_zielzeit_gilt_die_konfigurierte():
    """Ohne Lernwert bleibt das Verhalten unveraendert."""
    e = _calc(17, 30, gelernt=None, **MIT_PV)
    assert "Nach Zielzeit" in e.grund, e.grund


def test_zielzeit_ausgabe_rundet_nicht_falsch():
    """18.5 darf nicht als 18:00 erscheinen - das war im Log irrefuehrend."""
    assert pc._uhr_text(18.5) == "18:30"
    assert pc._uhr_text(18.0) == "18:00"
    assert pc._uhr_text(17.0) == "17:00"
    assert pc._uhr_text(7.25) == "7:15"

    e = _calc(16, 45, gelernt=18.5, **MIT_PV)
    assert "18:30" in e.grund, e.grund
    assert "18:00" not in e.grund, e.grund


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])