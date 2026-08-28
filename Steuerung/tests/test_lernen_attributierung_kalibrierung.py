# -*- coding: utf-8 -*-
"""Bausteine A+B + Verbrauchsbewusstsein der LearningEngine.

A: Quellen-Attribution je Heizzyklus (pv/batterie/gemischt/netz) +
   "Zu-frueh"-Erkennung (Nicht-PV-Zyklus, danach binnen 45 min volle Sonne).
B: Forecast-Kalibrierung (taeglicher Netzeinschuss / Prognose als EWMA).
C: Stundensurplus-Profil -> CalcStart beginnt vor gelernten Mittagstiefs.
"""
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace


sys.stdout.reconfigure(encoding="utf-8")

import pytest  # noqa: E402

from json_config import WPSteuerungConfig  # noqa: E402
from learning_engine import LearningEngine  # noqa: E402
from priority_control import (  # noqa: E402
    bewerte_alle_regeln,
    evaluate_adaptive_pv,
    evaluate_calculated_start,
)


@pytest.fixture
def engine(tmp_path):
    """Isolierte Engine (keine echte learning_data.json anfassen!)."""
    return LearningEngine(data_path=str(tmp_path / "lern.json"))


def _schritt(e, now, kompressor, feedin=None, soc=None):
    e.update(
        now,
        {"oben": 45.0, "mittig": 43.0, "unten": 40.0},
        kompressor, feedin_watt=feedin, soc=soc,
    )


def _zyklus(e, start, dauer_min=60, feedin=0.0, soc=None, schritt_min=5):
    """Simuliert einen Heizzyklus (Start->Ende) mit konstanter Quelle."""
    n = int(dauer_min / schritt_min)
    for i in range(n + 1):
        _schritt(e, start + timedelta(minutes=schritt_min * i),
                 True, feedin=feedin, soc=soc)
    _schritt(e, start + timedelta(minutes=dauer_min + schritt_min),
             False, feedin=feedin, soc=soc)


class TestQuellenAttribution:
    def test_pv_zyklus_wird_attribuiert(self, engine):
        _zyklus(engine, datetime(2026, 8, 26, 11, 0), feedin=900.0, soc=50.0)
        z = engine.data.cycles[-1]
        assert z["quelle"] == "pv"
        assert z["avg_feedin_watt"] == 900.0
        assert z["avg_soc"] == 50.0
        assert engine.data.runtime_by_quelle_sec["pv"] > 3000

    def test_batterie_zyklus(self, engine):
        _zyklus(engine, datetime(2026, 8, 26, 21, 0), feedin=-20.0, soc=93.0)
        assert engine.data.cycles[-1]["quelle"] == "batterie"

    def test_netz_zyklus(self, engine):
        _zyklus(engine, datetime(2026, 8, 26, 21, 0), feedin=-300.0, soc=40.0)
        assert engine.data.cycles[-1]["quelle"] == "netz"

    def test_gemischter_zyklus(self, engine):
        # Teils Sonne, teils Netzkauf, Batterie nicht voll -> gemischt
        t0 = datetime(2026, 8, 26, 16, 0)
        for i in range(7):
            _schritt(engine, t0 + timedelta(minutes=5 * i), True,
                     feedin=-80.0, soc=70.0)
        for i in range(5):
            _schritt(engine, t0 + timedelta(minutes=35 + 5 * i), True,
                     feedin=450.0, soc=70.0)
        _schritt(engine, t0 + timedelta(minutes=65), False,
                 feedin=450.0, soc=70.0)
        assert engine.data.cycles[-1]["quelle"] == "gemischt"


class TestZuFruehErkennung:
    def test_netz_zyklus_mit_pv_nachlauf_zaehlt_als_zu_frueh(self, engine):
        _zyklus(engine, datetime(2026, 8, 26, 14, 0), dauer_min=55,
                feedin=-300.0, soc=40.0)  # Ende ~15:00
        # 10 min spaeter noch dunkel...
        _schritt(engine, datetime(2026, 8, 26, 15, 10), False, feedin=100.0)
        # ...dann kommt die Sonne durch
        _schritt(engine, datetime(2026, 8, 26, 15, 50), False, feedin=1200.0)
        stat = engine.get_quellen_statistik()
        assert stat["zu_frueh_events_gesamt"] == 1
        assert stat["zu_frueh_14d"] == 1

    def test_kein_false_positive_ohne_pv_nachlauf(self, engine):
        _zyklus(engine, datetime(2026, 8, 26, 14, 0), dauer_min=55,
                feedin=-300.0, soc=40.0)
        _schritt(engine, datetime(2026, 8, 26, 15, 50), False, feedin=100.0)
        assert engine.get_quellen_statistik()["zu_frueh_events_gesamt"] == 0


class TestForecastKalibrierung:
    def _tag(self, e, datum, surplus_wh, stunden=10):
        """Integriert surplus_wh ueber den Vormittag (stuendliche Schritte,
        damit der Anti-Zeitprung-Cap von 1h im update() nicht greift)."""
        watt = surplus_wh / float(stunden)
        t0 = datetime(datum.year, datum.month, datum.day, 8, 0)
        for h in range(stunden + 1):
            _schritt(e, t0 + timedelta(hours=h), False, feedin=watt)

    def test_ratio_lernt_ab_drei_tagen(self, engine):
        self._tag(engine, datetime(2026, 8, 24), 20000.0)   # ratio 4.0 -> 2.0
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.get_forecast_ratio() == 1.0           # n<3 -> neutral
        self._tag(engine, datetime(2026, 8, 25), 6000.0)    # ratio 1.2
        _kalibriere_am_abend(engine, datetime(2026, 8, 25), 5000.0)
        self._tag(engine, datetime(2026, 8, 26), 4500.0)    # ratio 0.9
        _kalibriere_am_abend(engine, datetime(2026, 8, 26), 5000.0)
        assert engine.data.forecast_ratio_samples == 3
        # EWMA: 2.0 -> 1.76 -> 1.502
        assert abs(engine.get_forecast_ratio() - 1.502) < 0.01

    def test_clamps_und_leere_tage(self, engine):
        self._tag(engine, datetime(2026, 8, 24), 40.0)      # unter Mindest-Daten
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.data.forecast_ratio_samples == 0      # uebersprungen
        self._tag(engine, datetime(2026, 8, 25), 30000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 25), 5000.0)  # 6.0->2.0
        assert engine.data.forecast_ratio == 2.0
        # Keine Prognose -> kein Sample
        self._tag(engine, datetime(2026, 8, 26), 8000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 26), None)
        assert engine.data.forecast_ratio_samples == 1

    def test_nur_einmal_pro_tag(self, engine):
        self._tag(engine, datetime(2026, 8, 24), 20000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 9999.0)  # zweiter Versuch
        assert engine.data.forecast_ratio_samples == 1


def _kalibriere_am_abend(e, tag_datum, forecast):
    """Abendlicher update()-Aufruf MIT Prognose - loest die Kalibrierung."""
    e.update(
        tag_datum.replace(hour=20, minute=1),
        {"oben": 45.0, "mittig": 43.0, "unten": 40.0},
        False, feedin_watt=200.0, forecast_today_wh_qm=forecast,
    )


class TestSurplusProfil:
    def test_profil_lernt_mittagstief(self, engine):
        stunde_watt = {8: 800.0, 9: 700.0, 10: 600.0, 11: 500.0,
                       12: 150.0, 13: 180.0, 14: 600.0}
        for tag in range(7):
            for h, w in stunde_watt.items():
                _schritt(engine, datetime(2026, 8, 20 + tag, h, 30),
                         False, feedin=w)
        profil = engine.get_surplus_profile()
        assert profil is not None
        assert profil[12] < 250.0          # Kochtief gelernt
        assert profil[8] > 400.0           # Morgenueberschuss intakt

    def test_get_info_liefert_profil_fuer_ui(self, engine):
        info = engine.get_info()
        assert info["surplus_profil"] is None      # noch unbrauchbar
        assert info["forecast_ratio"] == 1.0
        assert info["quellen"]["zu_frueh_14d"] == 0
        # Nach dem Lernen steht das Profil {stunde: watt} im Payload:
        for i in range(6):
            _schritt(engine, datetime(2026, 8, 26, 12, i), False, feedin=120.0)
        for i in range(6):
            _schritt(engine, datetime(2026, 8, 26, 13, i), False, feedin=600.0)
        for i in range(6):
            _schritt(engine, datetime(2026, 8, 26, 14, i), False, feedin=500.0)
        for i in range(6):
            _schritt(engine, datetime(2026, 8, 26, 15, i), False, feedin=450.0)
        info = engine.get_info()
        assert set(info["surplus_profil"].keys()) == {"12", "13", "14", "15"}
        assert info["surplus_profil"]["12"] < 250

    def test_profil_bleibt_none_bei_zu_wenig_stunden(self, engine):
        for i in range(8):
            _schritt(engine, datetime(2026, 8, 26, 12, i), False, feedin=100.0)
        assert engine.get_surplus_profile() is None


# ─────────────── Wirkung auf CalcStart / AdaptivePV ───────────────

def _calc_cfg(**over):
    cfg = SimpleNamespace(
        aktiv=True, prioritaet=82, solltemperatur_c=44.0, target_uhr=17,
        heizrate_unten_c_h=3.0, heizrate_gesamt_c_h=2.0, tmax_c=48.0,
        pv_einspeisung_min_watt=50.0, soc_min_prozent=90.0,
        max_netzbezug_watt=-50.0, spaetstart_puffer_h=0.5,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


TEMPS = {"oben": 45.0, "mittig": 43.0, "unten": 41.0}   # braucht 1.0 h


class TestCalcstartMittagstief:
    def test_ohne_profil_wartet_noch_um_1445(self):
        erg = evaluate_calculated_start(_calc_cfg(), TEMPS, 14, 45)
        assert erg.einschalten is None

    def test_mittagstief_verfrueht_den_spaetest_start(self):
        """Tief um 15 Uhr -> 15-Uhr-Stunde zaehlt nur 75% -> frueher EIN."""
        erg = evaluate_calculated_start(
            _calc_cfg(), TEMPS, 14, 45,
            surplus_profile={"15": 100.0},
        )
        assert erg.einschalten is True
        assert "SPAETEST" in erg.grund
        assert "Mittagstief 15-16" in erg.grund

    def test_kalibrierung_faerbt_die_kategorie(self):
        # Mit PV-Quelle laeuft die Regel in den "warte auf PV"-Zweig, der
        # das pv_label (inkl. Kalibrierungs-Zusatz) enthaelt.
        erg = evaluate_calculated_start(
            _calc_cfg(), TEMPS, 14, 0,
            feedin_watt=100.0,
            forecast_wh_qm=1300.0, fc_ratio=0.38,   # 1300*0.38=494 -> bewoelkt
        )
        assert erg.einschalten is None
        assert "bewoelkt" in erg.grund
        assert "x0.38" in erg.grund


def _adaptive_cfg(**over):
    cfg = SimpleNamespace(
        aktiv=True, prioritaet=55, temperaturfuehler="unten",
        base_threshold_watt=300.0, fc_schwelle_gut_wh=4000.0,
        fc_schwelle_schlecht_wh=1000.0, tmax_c=48.0,
        t_aggressiv_kalt_c=30.0, t_normal_kalt_c=35.0,
        einschalten_bis_c=45.0,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class TestAdaptiveKalibrierung:
    def test_schlechte_kalibrierung_senkt_schwelle(self):
        # 2500 Wh/qm ist neutral; x0.4 -> 1000 <= schlecht-Schwelle -> x0.5
        erg = evaluate_adaptive_pv(
            _adaptive_cfg(), TEMPS, 200.0, 2500.0, False, 12,
            fc_ratio=0.4,
        )
        assert erg.einschalten is True
        assert ">= 150W" in erg.grund

    def test_neutral_ohne_ratio(self):
        erg = evaluate_adaptive_pv(
            _adaptive_cfg(), TEMPS, 200.0, 2500.0, False, 12,
        )
        assert erg.grund.endswith("< 300W")


class TestVerdrahtungBewerteAlleRegeln:
    def test_fc_ratio_und_profil_erreichen_calcstart(self):
        _gewinner, alle = bewerte_alle_regeln(
            config=WPSteuerungConfig(),
            temp_dict=dict(TEMPS),
            pv_leistung=100.0,   # PV-Quelle -> "warte auf PV"-Zweig mit Label
            kompressor_ein=False,
            now=datetime(2026, 8, 26, 14, 0),
            forecast_wh_qm=None,
            forecast_today_wh_qm=1300.0,
            fc_ratio=0.4,
            surplus_profile={"15": 100.0},
        )
        cs = [e for e in alle if e.name == "CalcStart"][0]
        assert "x0.40" in cs.grund
        assert "Mittagstief" in cs.grund

# ─────────────── CalcStart + Usage-Events (Punkt 4) ───────────────

def test_calcstart_usage_events_reduzieren_stundenbedarf():
    """Zapfung mit Temperaturabfall -> hours_needed sinkt -> frueher EIN."""
    usage_event = {
        "timestamp": "2026-08-26T14:30:00",
        "temp_before_unten": 47.0,
        "temp_before_mitte": 46.0,
        "temp_before_oben": 47.0,
        "temp_after_unten": 41.0,
        "temp_after_mitte": 43.0,
        "temp_after_oben": 45.0,
        "drop_unten_k": 6.0,
        "drop_mitte_k": 3.0,
        "drop_oben_k": 2.0,
        "drop_gesamt_k": 6.0,
    }

    erg_ohne = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 14, 45,
        forecast_wh_qm=None,
    )
    assert erg_ohne.einschalten is None, \
        f"Ohne Zapfung sollte CalcStart warten, war: {erg_ohne.grund}"

    erg_mit = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 14, 45,
        recent_usage_events=[usage_event],
        forecast_wh_qm=None,
    )
    assert erg_mit.einschalten is None, \
        f"Mit Zapfung bei 14:45 sollte CalcStart noch warten: {erg_mit.grund}"
    assert "Puffer" in erg_mit.grund, \
        f"Sollte Puffer anzeigen: {erg_mit.grund}"

    erg_spaet_ohne = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 16, 0,
        forecast_wh_qm=None,
    )
    assert erg_spaet_ohne.einschalten is True
    assert "SPAETEST" in erg_spaet_ohne.grund or "ZU SPAET" in erg_spaet_ohne.grund

    erg_spaet_mit = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 16, 0,
        recent_usage_events=[usage_event],
        forecast_wh_qm=None,
    )
    assert erg_spaet_mit.einschalten is None, \
        f"Mit Zapfung um 16:00 sollte CalcStart noch warten: {erg_spaet_mit.grund}"


def test_calcstart_usage_events_leere_liste_wirkt_nicht():
    """Leere Liste oder None -> kein Einfluss auf hours_needed."""
    erg_none = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 14, 45,
        recent_usage_events=None,
        forecast_wh_qm=None,
    )
    erg_leer = evaluate_calculated_start(
        _calc_cfg(), TEMPS, 14, 45,
        recent_usage_events=[],
        forecast_wh_qm=None,
    )
    assert erg_none.einschalten == erg_leer.einschalten
    assert erg_none.grund == erg_leer.grund


def test_calcstart_usage_events_verdrahtung_im_gesamtsystem():
    """recent_usage_events von der Engine ueber bewerte_alle_regeln bis
    zu evaluate_calculated_start."""
    from learning_engine import LearningEngine
    import tempfile
    import os
    td = tempfile.mkdtemp()
    eng = LearningEngine(data_path=os.path.join(td, "lern.json"))
    zapf_event = {
        "timestamp": "2026-08-26T15:55:00",
        "drop_gesamt_k": 8.0,
    }
    eng.data.usage_events.append(zapf_event)
    events = eng.get_recent_usage_events(hours=2, now=datetime(2026, 8, 26, 16, 0))

    assert len(events) == 1
    assert events[0]["drop_gesamt_k"] == 8.0

    config = WPSteuerungConfig()
    config.calculated_start.aktiv = True
    config.calculated_start.target_uhr = 17
    config.zeitfenster.start_uhr = 0
    config.zeitfenster.ende_uhr = 0
    config.forecast.aktiv = False
    config.adaptive_pv.aktiv = False
    config.komfort.notfall_einschalten_bei_c = 30.0
    config.komfort.komfort_einschalten_bei_c = 30.0
    config.komfort.min_pv_fuer_komfort_watt = 999999
    config.wochenende.aktiv = False

    temps = {"oben": 45.0, "mittig": 43.0, "unten": 41.0}
    _, alle = bewerte_alle_regeln(
        config=config,
        temp_dict=temps,
        pv_leistung=0.0,
        kompressor_ein=False,
        now=datetime(2026, 8, 26, 16, 0),
        recent_usage_events=events,
    )
    cs = [e for e in alle if e.name == "CalcStart"][0]
    assert cs.einschalten is None, \
        f"Mit frischer Zapfung um 16:00 sollte CalcStart warten: {cs.grund}"
    assert "Puffer" in cs.grund and "reicht" in cs.grund



# ─── Zeitachsen-Bug: timezone-aware now vs naive JSON-Timestamps ───

def test_timezone_aware_now_crasht_nicht(engine):
    """Regression: timezone-aware 'now' darf keinen TypeError

    'can't compare offset-naive and offset-aware datetimes' ausloesen.
    Wurde uebersehen, weil alle Tests naive datetimes verwenden.
    """
    # Timezone-aware now (wie in Produktion auf dem Pi)
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Berlin")
    except Exception:
        pytest.skip("zoneinfo nicht verfuegbar")
    
    aware = datetime(2026, 8, 26, 19, 30, tzinfo=tz)

    # Erstmal eine Zapfung registrieren (mit naive-timestamp, wie aus JSON)
    engine.data.usage_events.append({
        "timestamp": "2026-08-26T18:45:00",
        "drop_gesamt_k": 4.5, "drop_unten_k": 3.0,
        "drop_mitte_k": 1.0, "drop_oben_k": 0.5,
        "temp_before_unten": 45.0, "temp_after_unten": 42.0,
        "temp_before_mitte": 44.0, "temp_after_mitte": 43.0,
        "temp_before_oben": 46.0, "temp_after_oben": 45.5,
    })

    # Diese drei Aufrufe duerfen NICHT mit TypeError crashen
    result_morgen = engine.get_learned_morning_window(now=aware)
    result_abend = engine.get_learned_evening_window(now=aware, min_samples=1)
    result_recent = engine.get_recent_usage_events(now=aware)

    assert result_abend is not None  # Zapfung um 18:45 liegt im Abendfenster
    assert result_morgen is None     # 18:45 liegt NICHT im Morgenfenster
    assert len(result_recent) == 1   # liegt in den letzten 2h


def test_zu_frueh_pending_timezone_aware(engine):
    """Regression: auch die 'Zu frueh'-Erkennung muss mit aware now klappen."""
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo("Europe/Berlin")
    except Exception:
        pytest.skip("zoneinfo nicht verfuegbar")

    # Netz-Zyklus beenden (haengt pending_zu_frueh an)
    _zyklus(engine, datetime(2026, 8, 26, 14, 0), dauer_min=55,
            feedin=-300.0, soc=40.0)

    engine._pending_zu_frueh = ["2026-08-26T14:55:00"]

    # Timezone-aware weiter (45 min noch nicht um -> kein Event)
    aware = datetime(2026, 8, 26, 15, 20, tzinfo=tz)
    engine.update(
        aware,
        {"oben": 45.0, "mittig": 43.0, "unten": 40.0},
        False, feedin_watt=1200.0,
    )
    assert engine.get_quellen_statistik()["zu_frueh_events_gesamt"] == 0

    # Nach 45 min + Sonne -> Event
    engine._pending_zu_frueh = ["2026-08-26T14:55:00"]
    aware2 = datetime(2026, 8, 26, 16, 0, tzinfo=tz)
    engine.update(
        aware2,
        {"oben": 45.0, "mittig": 43.0, "unten": 40.0},
        False, feedin_watt=1200.0,
    )
    assert engine.get_quellen_statistik()["zu_frueh_events_gesamt"] == 1
