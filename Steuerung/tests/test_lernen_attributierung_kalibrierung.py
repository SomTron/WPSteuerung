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


def _schritt(e, now, kompressor, feedin=None, soc=None, pv_array_size_qm=None):
    e.update(
        now,
        {"oben": 45.0, "mittig": 43.0, "unten": 40.0},
        kompressor, feedin_watt=feedin, soc=soc,
        pv_array_size_qm=pv_array_size_qm,
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
        # Faktor = Einspeisung (Wh) / (Prognose Wh/m2 x Arrayflaeche m2).
        # Bei 10 m2 und 5000 Wh/m2 sind das 50000 Wh erwartet.
        self._tag(engine, datetime(2026, 8, 24), 20000.0)   # ratio 0.40
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.get_forecast_ratio() == 1.0           # n<3 -> neutral
        self._tag(engine, datetime(2026, 8, 25), 6000.0)    # ratio 0.12
        _kalibriere_am_abend(engine, datetime(2026, 8, 25), 5000.0)
        self._tag(engine, datetime(2026, 8, 26), 45000.0)   # ratio 0.90
        _kalibriere_am_abend(engine, datetime(2026, 8, 26), 5000.0)
        assert engine.data.forecast_ratio_samples == 3
        # Gemessen: 0.400 -> 0.370 -> 0.529 (EWMA alpha=0.3)
        assert abs(engine.get_forecast_ratio() - 0.529) < 0.01

    def test_prognose_in_wh_pro_m2_wird_nicht_erneut_umgerechnet(self, engine):
        """Die Integrationsgrenze liefert bereits Wh/m2.

        Ein frueherer Doppel-Normalisierungsschritt las 5000 Wh/m2 als
        "5.0 Wh/m2" undwarf die Prognose immer als unbrauchbar - die
        Kalibrierung lief nie an (Symptom: "samples 0/3" in der WebApp).
        """
        self._tag(engine, datetime(2026, 8, 24), 50000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.data.forecast_ratio_samples == 1
        # 50000 Wh Einspeisung bei 5000 Wh/m2 x 10 m2 = Faktor 1.0
        assert engine.data.forecast_ratio == pytest.approx(1.0)

    def test_arrayflaeche_statt_doppelter_normalisierung(self, engine):
        """Regression: Ohne Arrayflaeche klemmte der Faktor am Maximum.

        Realistischer Septembertag: 5000 Wh/m2 Prognose, 10 m2 Array, ca.
        20000 Wh Eigenverbrauch -> Faktor 0.4. Ohne die Flaeche ergaebe sich
        20000/5000 = 4.0 und damit dauerhaft der Maximalwert 2.0 - die
        Kalibrierung waere wirkungslos und wuerde systematisch zu
        pessimistischen Prognosen fuehren.
        """
        self._tag(engine, datetime(2026, 8, 24), 20000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.data.forecast_ratio_samples == 1
        assert engine.data.forecast_ratio == pytest.approx(0.4, abs=0.01)

    def test_flaeche_kommt_aus_der_wp_config(self, engine):
        """Die Flaeche wird aus `wp.pv_array_size_qm` uebernommen.

        Sonst wuerde die Kalibrierung dauerhaft mit dem Default (10 m2)
        rechnen, waehrend die Stundensprognose im selben Zyklus die echte
        Anlagengrösse verwendet - zwei Werte fuer dieselbe Groesse.
        """
        _schritt(engine, datetime(2026, 8, 24, 8, 0), False, feedin=100.0,
                 pv_array_size_qm=25.0)
        assert engine.data.config.pv_array_size_qm == 25.0
        # 25 m2 x 2000 Wh/m2 = 50000 Wh; 20000 Wh Einspeisung -> Faktor 0.4
        self._tag(engine, datetime(2026, 8, 24), 20000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 2000.0)
        assert engine.data.forecast_ratio == pytest.approx(0.4, abs=0.01)

    def test_ungueltige_flaeche_ueberschreibt_den_default_nicht(self, engine):
        """0, None oder NaN duerfen die Konfiguration nicht zerstoeren."""
        for wert in (0.0, None, float("nan")):
            _schritt(engine, datetime(2026, 8, 24, 8, 0), False, feedin=50.0,
                     pv_array_size_qm=wert)
            assert engine.data.config.pv_array_size_qm == 10.0

    def test_ohne_flaeche_wird_klar_verworfen(self, engine):
        """Bei Flaeche 0 gibt es kein Ertragspotential -> kein Sample,
        statt durch eine Division durch 0 zu stuerzen."""
        engine.data.config.pv_array_size_qm = 0.0
        self._tag(engine, datetime(2026, 8, 24), 20000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.data.forecast_ratio_samples == 0

    def test_clamps_und_leere_tage(self, engine):
        self._tag(engine, datetime(2026, 8, 24), 40.0)      # unter Mindest-Daten
        _kalibriere_am_abend(engine, datetime(2026, 8, 24), 5000.0)
        assert engine.data.forecast_ratio_samples == 0      # uebersprungen
        # 300000 Wh bei 5000 Wh/m2 x 10 m2 = 6.0 -> am Max geklemmt
        self._tag(engine, datetime(2026, 8, 25), 300000.0)
        _kalibriere_am_abend(engine, datetime(2026, 8, 25), 5000.0)
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
            _calc_cfg(netz_fallback_erlaubt=True), TEMPS, 14, 45,
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
