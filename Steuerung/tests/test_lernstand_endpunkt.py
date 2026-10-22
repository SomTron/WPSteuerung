"""Test fuer den Lernstand-Endpunkt (BUGFIX 21.10.2026).

Der Endpunkt macht sichtbar, OB das Auto-Learning wirklich greift:
wie viele Stundenprofile, Tagesbilanzen und Prognoseabweichungen
vorliegen, und welche Korrekturfaktoren daraus gelernt wurden.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import api  # noqa: E402
from learning_engine import LearningEngine  # noqa: E402


@pytest.fixture
def lern_engine():
    return LearningEngine(data_path="")


def test_endpunkt_ohne_engine_meldet_verfuegbar_false(monkeypatch):
    monkeypatch.setattr(api, "shared_state", SimpleNamespace(
        learning_engine=None))
    antwort = api.debug_lernstand()
    assert antwort["verfuegbar"] is False
    assert "grund" in antwort


def test_endpunkt_zeigt_die_gelernten_aggregate(monkeypatch, lern_engine):
    monkeypatch.setattr(api, "shared_state", SimpleNamespace(
        learning_engine=lern_engine))
    antwort = api.debug_lernstand()
    assert antwort["verfuegbar"] is True
    # Die Zaehler der Rohdaten muessen vorhanden sein
    assert "eintraege" in antwort
    for schluessel in ("surplus_by_hour", "tages_haushalt",
                       "forecast_hourly_deviations"):
        assert schluessel in antwort["eintraege"]
    # Die Kennzahlen der Lernengine
    assert "forecast_ratio" in antwort["kennzahlen"]
    assert "heat_rates" in antwort["kennzahlen"]
    # Und die abgeleiteten Groessen
    assert "stunden_korrekturfaktor" in antwort
    assert "haushalt_je_wochentag" in antwort


def test_endpunkt_zaehlt_die_echten_eintraege(monkeypatch, lern_engine):
    """Nach dem Lernen muessen die Zaehler stimmen."""
    from datetime import datetime, timedelta
    for d in range(3):
        tag = datetime(2026, 10, 21, 0, 0) + timedelta(days=d)
        for h in range(24):
            for k in range(6):
                lern_engine.update(
                    now=tag + timedelta(hours=h, minutes=10 * k),
                    temp_dict={"unten": 40.0}, compressor_is_on=False,
                    feedin_watt=0.0, pv_acpower_watt=0.0,
                    pv_erzeugung_watt=0.0, soc=50.0,
                    forecast_today_wh_qm=None, forecast_hourly_wh=None,
                    pv_array_size_qm=10.0,
                    hausverbrauch_watt=400.0,
                    hausverbrauch_stunden_profil=True,
                )
    monkeypatch.setattr(api, "shared_state", SimpleNamespace(
        learning_engine=lern_engine))
    antwort = api.debug_lernstand()
    assert antwort["eintraege"]["surplus_by_hour"] == 24 * 3
    assert antwort["eintraege"]["tages_haushalt"] >= 1
