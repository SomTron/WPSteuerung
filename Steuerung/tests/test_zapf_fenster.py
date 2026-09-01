"""Tests: Lernen aus Zapfverhalten -> dynamische Regelzeiten.

Betriebsvorgabe: "die Steuerung soll auch aus dem Zapfverhalten lernen und
die Zeiten anpassen."

1. LearningEngine.get_learned_evening_window(): frueheste/spaeteste
   Abend-Zapfung der letzten 14 Tage inkl. Vor-/Nachlauf.
2. MindestTemp-Eintrag mit fenster_aus_lernen=True nutzt das gelernte
   Fenster (mit Klemmung: max 2h frueher, max 1h spaeter als konfiguriert).
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytest  # noqa: E402

from json_config import WPSteuerungConfig  # noqa: E402
from learning_engine import LearningEngine  # noqa: E402
import priority_control as pc  # noqa: E402


def baue_engine_mit_zapfungen(stunden_liste, basis=None):
    """Erzeugt eine Engine mit synthetischen usage_events."""
    engine = LearningEngine(data_path=":memory:")  # wird nie gespeichert (kein update)
    if basis is None:
        basis = datetime(2026, 1, 15, 12, 0)
    events = []
    for i, stunde in enumerate(stunden_liste):
        ts = basis - timedelta(days=i % 10) + timedelta(hours=stunde)
        events.append({
            "timestamp": ts.isoformat(),
            "temp_before": 45.0,
            "temp_after": 43.0,
            "drop_k": 2.0,
        })
    engine.data.usage_events = events
    return engine


# ── get_learned_evening_window ──

def test_fenster_aus_vollen_daten():
    now = datetime(2026, 1, 15, 12, 0)
    # Zapfungen zwischen 19.0 und 20.5 an verschiedenen Tagen
    stunden = []
    for tag in range(7):
        stunden.extend([19.0 + tag * 0.0, 20.5])
    engine = LearningEngine.__new__(LearningEngine)  # ohne _load
    from learning_engine import LearningData
    engine.data = LearningData(
        cycles=[], heat_rates={}, usage_events=[],
        learned_target_hour=19.0, target_hour_samples=10, version=2,
    )
    events = []
    for tag in range(7):
        d = now - timedelta(days=tag)
        for stunde in (19.25, 20.75):
            ts = d.replace(hour=int(stunde), minute=int((stunde % 1) * 60))
            events.append({
                "timestamp": ts.isoformat(),
                "temp_before": 45.0, "temp_after": 43.0, "drop_k": 2.0,
            })
    engine.data.usage_events = events

    fenster = engine.get_learned_evening_window(now=now)
    assert fenster is not None
    start, ende = fenster
    # frueheste Zapfung 19:15 - 1.5h Vorlauf = 17:45
    assert abs(start - 17.75) < 0.01
    # spaeteste Zapfung 20:45 + 0.75h Nachlauf = 21:30
    assert abs(ende - 21.5) < 0.01


def test_zu_wenige_samples_liefert_none():
    now = datetime(2026, 1, 15, 12, 0)
    engine = baue_engine_mit_zapfungen([19.5])  # nur 1 Event
    assert engine.get_learned_evening_window(now=now) is None


def test_alte_events_zaehlen_nicht():
    now = datetime(2026, 1, 15, 12, 0)
    engine = LearningEngine.__new__(LearningEngine)
    from learning_engine import LearningData
    engine.data = LearningData(
        cycles=[], heat_rates={}, usage_events=[],
        learned_target_hour=19.0, target_hour_samples=5, version=2,
    )
    alt = now - timedelta(days=30)
    events = []
    for tag in range(6):
        ts = alt - timedelta(days=tag)
        events.append({"timestamp": ts.isoformat(), "temp_before": 45.0,
                       "temp_after": 43.0, "drop_k": 2.0})
    engine.data.usage_events = events
    assert engine.get_learned_evening_window(now=now, tage=14) is None


# ── MindestTemp mit gelerntem Fenster ──

def bewerte_mindest(temp_dict, now_hour, learned=None):
    config = WPSteuerungConfig()
    for eintrag in config.mindest_temp.eintraege:
        if eintrag.name == "Abend-Mitte":
            eintrag.fenster_aus_lernen = True
    return pc.evaluate_mindesttemp(
        config.mindest_temp, temp_dict, now_hour,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
        learned_evening_window=learned,
    ), config


def test_gelerntes_fenster_erweitert_abendfenster():
    """Gelernt: Zapfungen ab 18.5h -> Fenster darf schon um 16 Uhr starten."""
    ergebnisse, config = bewerte_mindest(
        {"mittig": 39.0}, now_hour=16,
        learned=(16.5, 22.3),
    )
    e = next(x for x in ergebnisse if "Abend-Mitte" in x.name)
    assert e.einschalten is True
    assert "gelernt" in e.grund


def test_statisch_ohne_gelerntes_fenster():
    """Ohne Lernwert gilt das konfigurierte Fenster (17-22)."""
    ergebnisse, _ = bewerte_mindest({"mittig": 39.0}, now_hour=16, learned=None)
    e = next(x for x in ergebnisse if "Abend-Mitte" in x.name)
    assert not e.aktiv
    assert "17-22" in e.grund


def test_klemmung_max_2h_frueher():
    """Sehr fruehe Zapfungen (13.9h): Start wird auf 15 Uhr geklemmt
    (max. 2h vor dem konfigurierten Start 17 Uhr), nicht weiter vorgezogen."""
    ergebnisse, _ = bewerte_mindest({"mittig": 39.0}, now_hour=15, learned=(13.9, 21.0))
    e = next(x for x in ergebnisse if "Abend-Mitte" in x.name)
    # Fenster 15-21 -> um 15 Uhr aktiv, Garantie greift
    assert e.aktiv and e.einschalten is True
    assert "15-" in e.grund and "gelernt" in e.grund


def test_klemmung_max_1h_spaeter():
    """Sehr spaete Zapfungen (23.4h) verlaengern Ende nur bis 23."""
    ergebnisse, _ = bewerte_mindest({"mittig": 39.0}, now_hour=22, learned=(18.0, 23.4))
    e = next(x for x in ergebnisse if "Abend-Mitte" in x.name)
    assert e.einschalten is True  # 22 < 23 -> im Fenster


# ── Integration: pcl reicht gelerntes Fenster durch ──

@pytest.mark.asyncio
async def test_pcl_uebergibt_gelerntes_abendfenster():
    from types import SimpleNamespace
    import pytz
    from unittest.mock import patch as mock_patch
    import priority_control_logic as pcl

    TZ = pytz.timezone("Europe/Berlin")
    config = WPSteuerungConfig()
    config.calculated_start.aktiv = False
    # Abend-Mitte auf Lernen stellen
    for eintrag in config.mindest_temp.eintraege:
        if eintrag.name == "Abend-Mitte":
            eintrag.fenster_aus_lernen = True

    state = SimpleNamespace(
        local_tz=TZ,
        priority_config=config,
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=43.0, t_unten=41.0, t_mittig=42.0, t_verd=30.0),
        solar=SimpleNamespace(feedinpower=0.0, batpower=1200.0, soc=95.0,
                              forecast_today=None, forecast_tomorrow=None,
                              forecast_day2=None),
        control=SimpleNamespace(
            kompressor_ein=False, previous_modus="Normalmodus",
            aktueller_einschaltpunkt=None, aktueller_ausschaltpunkt=None,
            active_rule_name=None, active_rule_sensor=None,
            komfort_aktiv=False, alle_ergebnisse=[], _soll_einschalten=False,
        ),
    )

    class _MockEngine:
        def update(self, now, temp_dict, compressor_is_on, **kwargs):
            pass

        def get_learned_heating_rate(self, month, sensor):
            return None

        def get_learned_target_hour(self):
            return None

        def get_learned_evening_window(self):
            return (16.5, 22.3)

    with mock_patch.object(pcl, 'datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(datetime(2026, 1, 15, 12, 0))
        result = await pcl.determine_mode_and_setpoints(
            state, 41.0, 42.0, learning_engine=_MockEngine(),
        )

    abend = next(e for e in result["alle_ergebnisse"] if "Abend-Mitte" in e.name)
    # Um 12 Uhr inaktiv, ABER mit dem GELERNTEN Fenster 16-23 im Grundtext:
    assert "gelernt" in abend.grund
    assert "16-23" in abend.grund
