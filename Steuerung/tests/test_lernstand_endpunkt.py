"""Test fuer den Lernstand-Endpunkt (BUGFIX 21.10.2026).

Der Endpunkt macht sichtbar, OB das Auto-Learning wirklich greift:
wie viele Stundenprofile, Tagesbilanzen und Prognoseabweichungen
vorliegen, und welche Korrekturfaktoren daraus gelernt wurden.
"""

import os
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


def test_lernstand_zeigt_den_fortschritt(monkeypatch, lern_engine):
    """BUGFIX (22.10.2026): Der Lernstand macht sichtbar, OB die
    Steuerung wirklich lernt. Ohne ihn ist nicht erkennbar, ob die
    Engine sammelt oder auf Defaults faehrt.
    """
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
                    pv_array_size_qm=10.0, hausverbrauch_watt=400.0,
                    hausverbrauch_stunden_profil=True,
                )
    info = lern_engine.get_info()
    stand = info["lernstand"]
    assert stand["stundenprofil_stunden"] == 24
    assert stand["stundenprofil_tage"] == 3
    assert stand["tagesbilanz_tage"] >= 1
    # Die Schluessel muessen zur WebApp passen
    for schluessel in ("stundenprofil_tage", "stundenprofil_stunden",
                        "tagesbilanz_tage", "tagesbilanz_wochentage",
                        "tagesbilanz_wochentage_saison", "saison",
                        "korrektur_stunden", "zyklen", "zapfungen"):
        assert schluessel in stand
    # Die aktuelle Jahreszeit wird genannt (fuer die Anzeige)
    assert stand["saison"] in ("winter", "fruehling", "sommer", "herbst")

    # Und die WebApp bekommt ihn (Whitelist!)
    from status_snapshot import _learning_summary
    assert "lernstand" in _learning_summary(lern_engine)


def test_saison_bilanz_zaehlt_nur_die_aktuelle_saison(tmp_path):
    """BUGFIX (22.10.2026): Die Tagesbilanz wird je Wochentag UND
    Jahreszeit gefuehrt. Fuer die Legionellenwahl zaehlt, wie viele
    Wochentage in der AKTUELLEN Saison schon einen Wert haben.
    """
    from learning_engine import LearningEngine
    from datetime import datetime, timedelta
    eng = LearningEngine(data_path=str(tmp_path / "lern.json"))
    # 3 Wochen Herbst (Oktober) - das ergibt 7 Wochentage in "herbst"
    start = datetime(2026, 10, 5, 0, 0)
    for woche in range(3):
        for tag_idx in range(7):
            tag = start + timedelta(days=woche * 7 + tag_idx)
            for h in range(24):
                for k in range(6):
                    eng.update(
                        now=tag + timedelta(hours=h, minutes=10 * k),
                        temp_dict={"unten": 40.0}, compressor_is_on=False,
                        feedin_watt=0.0, pv_acpower_watt=0.0,
                        pv_erzeugung_watt=0.0, soc=50.0,
                        forecast_today_wh_qm=None, forecast_hourly_wh=None,
                        pv_array_size_qm=10.0, hausverbrauch_watt=400.0,
                        hausverbrauch_stunden_profil=True,
                    )
    stand = eng.get_info()["lernstand"]
    # Der Test laeuft "heute" - die Saison ist die aktuelle, nicht
    # zwingend "herbst". Es muss aber ein gueltiger Wert sein.
    assert stand["saison"] in ("winter", "fruehling", "sommer", "herbst")
    # Beide Zaehler existieren; der saisonbezogene ist nicht groesser
    # als der overall-Zaehler.
    assert stand["tagesbilanz_wochentage_saison"] <= stand["tagesbilanz_wochentage"]
    assert stand["tagesbilanz_wochentage"] == 7


def test_lernstand_ist_leer_bei_neuer_installation(monkeypatch):
    """Ohne Daten darf die WebApp nicht abstuerzen."""
    from learning_engine import LearningEngine
    import tempfile
    eng = LearningEngine(
        data_path=os.path.join(tempfile.mkdtemp(), "lern.json"))
    stand = eng.get_info()["lernstand"]
    assert stand["stundenprofil_stunden"] == 0
    assert stand["tagesbilanz_tage"] == 0


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
