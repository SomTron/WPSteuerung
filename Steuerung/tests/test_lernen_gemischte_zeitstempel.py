# -*- coding: utf-8 -*-
"""Regression: gemischte Zeitstempel-Formate in learning_data.json.

Incident 15.09.: Im Log stand wiederholt
    'Learning-Engine-Info nicht verfuegbar:
     can't compare offset-naive and offset-aware datetimes'
Dadurch blieb die Learning-Karte im Dashboard leer (mein /status-Fix hat nur
den 500 in eine Warnung verwandelt). Ursache: aeltere Eintraege wurden MIT
Zeitzonen-Offset geschrieben ('2026-08-27T17:18:44+02:00'), neuere ohne.
"""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from learning_engine import LearningEngine  # noqa: E402


def _engine(tmp_path, zu_frueh=(), komfort=()):
    pfad = tmp_path / "learning_data.json"
    pfad.write_text(json.dumps({
        "zu_frueh_events": list(zu_frueh),
        "komfort_verletzungen": list(komfort),
    }), encoding="utf-8")
    return LearningEngine(data_path=str(pfad))


def test_parse_ts_normalisiert_und_toleriert_muell():
    assert LearningEngine._parse_ts("2026-09-15T12:00:00").tzinfo is None
    assert LearningEngine._parse_ts("2026-09-15T12:00:00+02:00").tzinfo is None
    assert LearningEngine._parse_ts("kaputt") is None
    assert LearningEngine._parse_ts(None) is None
    assert LearningEngine._parse_ts(12345) is None


def test_quellen_statistik_mit_gemischten_formaten(tmp_path):
    """Genau der Incident: aware + naiv in derselben Liste -> kein Crash."""
    jetzt = datetime.now()
    aware_alt = jetzt.astimezone().isoformat(timespec="seconds")           # mit Offset
    naiv_neu = (jetzt - timedelta(days=1)).isoformat(timespec="seconds")   # ohne

    engine = _engine(tmp_path, zu_frueh=[aware_alt, naiv_neu])

    stat = engine.get_quellen_statistik()      # darf keine TypeError werfen
    assert stat["zu_frueh_events_gesamt"] == 2
    assert stat["zu_frueh_14d"] == 2


def test_quellen_statistik_zaehlt_nur_das_14_tage_fenster(tmp_path):
    jetzt = datetime.now()
    zu_alt = (jetzt - timedelta(days=30)).isoformat(timespec="seconds")
    im_fenster = (jetzt - timedelta(days=2)).isoformat(timespec="seconds")
    im_fenster_aware = (jetzt - timedelta(days=3)).astimezone().isoformat(timespec="seconds")

    engine = _engine(tmp_path, zu_frueh=[zu_alt, im_fenster_aware, im_fenster])

    stat = engine.get_quellen_statistik()
    assert stat["zu_frueh_events_gesamt"] == 3
    assert stat["zu_frueh_14d"] == 2


def test_quellen_statistik_ohne_referenz_ist_null(tmp_path):
    """Unlesbarer letzter Eintrag -> 0 statt Exception."""
    engine = _engine(tmp_path, zu_frueh=["2026-09-14T10:00:00", "kaputt"])
    stat = engine.get_quellen_statistik()
    assert stat["zu_frueh_14d"] == 0
    assert stat["zu_frueh_events_gesamt"] == 2


def test_komfort_rate_mit_gemischten_formaten(tmp_path):
    jetzt = datetime.now()
    frisch_aware = jetzt.astimezone().isoformat(timespec="seconds")
    frisch_naiv = (jetzt - timedelta(days=1)).isoformat(timespec="seconds")
    alt = (jetzt - timedelta(days=30)).isoformat(timespec="seconds")

    engine = _engine(tmp_path, komfort=[alt, frisch_aware, frisch_naiv, "kaputt"])

    assert engine.get_komfort_verletzung_rate(tage=7) == 2


def test_get_info_wirft_mit_gemischten_daten_nicht(tmp_path):
    """Der eigentliche Zweck: die Learning-Karte im Dashboard bleibt gefuellt."""
    jetzt = datetime.now()
    engine = _engine(
        tmp_path,
        zu_frueh=[jetzt.astimezone().isoformat(timespec="seconds"),
                  (jetzt - timedelta(days=1)).isoformat(timespec="seconds")],
        komfort=[jetzt.astimezone().isoformat(timespec="seconds")],
    )
    info = engine.get_info()
    assert info["quellen"]["zu_frueh_events_gesamt"] == 2
    assert "runtime_sec" in info["quellen"]