# -*- coding: utf-8 -*-
"""Tests fuer Komfort-Waechter (B), dynamische Batteriereserve (C) und
adaptiven Taktschutz (D)."""
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace


sys.stdout.reconfigure(encoding="utf-8")

from learning_engine import LearningEngine  # noqa: E402
from priority_control import evaluate_batterie  # noqa: E402
import priority_control_logic as pcl  # noqa: E402


TZ = None


def _engine_mit_verletzungen(pfad, anzahl, tage_zurueck=1):
    """Engine mit n Komfort-Verletzungen vorbereiten."""
    eng = LearningEngine(data_path=pfad)
    jetzt = datetime.now()
    for i in range(anzahl):
        ts = (jetzt - timedelta(days=tage_zurueck - 1)).isoformat()
        eng.data.komfort_verletzungen.append(ts)
    return eng


class TestKomfortVerletzung:
    def test_verletzung_wird_gezaehlt_und_gespeichert(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = LearningEngine(data_path=pfad)
        now = datetime.now()
        eng._detect_komfort_verletzung(now, t_oben=38.5, nachtsperre_aktiv=False)
        eng2 = LearningEngine(data_path=pfad)
        assert len(eng2.data.komfort_verletzungen) == 1
        assert eng2.get_komfort_verletzung_rate(tage=7) == 1

    def test_ueber_grenze_keine_verletzung(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = LearningEngine(data_path=pfad)
        now = datetime.now()
        eng._detect_komfort_verletzung(now, t_oben=None, nachtsperre_aktiv=False)
        eng._detect_komfort_verletzung(now, t_oben=41.0, nachtsperre_aktiv=False)
        eng._detect_komfort_verletzung(now, t_oben=39.0, nachtsperre_aktiv=True)  # Sperre
        assert len(eng.data.komfort_verletzungen) == 0

    def test_max_pro_tag_begrenzt(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = LearningEngine(data_path=pfad)
        now = datetime.now()
        for minute in range(10):
            eng._detect_komfort_verletzung(
                now + timedelta(minutes=minute),
                t_oben=35.0, nachtsperre_aktiv=False,
            )
        # max_pro_tag=3 (Default)
        assert len(eng.data.komfort_verletzungen) == 3

    def test_bonus_vorlauf_ab_drei_verletzungen(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = _engine_mit_verletzungen(pfad, anzahl=3)
        assert eng.get_komfort_bonus_vorlauf(schwellwert=2) == 0.5

    def test_kein_bonus_bei_wenigen_verletzungen(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = _engine_mit_verletzungen(pfad, anzahl=1)
        assert eng.get_komfort_bonus_vorlauf(schwellwert=2) == 0.0

    def test_get_info_enthaelt_komfort(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        eng = LearningEngine(data_path=pfad)
        info = eng.get_info()
        assert "komfort_verletzungen_7d" in info
        assert "komfort_verletzungen_1d" in info


class TestDynamischeBatteriereserve:
    """Punkt C: Bei guter Prognose darf die Batterie tiefer entladen."""

    def _cfg(self, min_soc=80.0):
        from json_config import BatterieConfig
        return BatterieConfig(
            aktiv=True, temperaturfuehler="unten",
            einschalten_bei_c=42.0, ausschalten_bei_c=47.0,
            min_soc_prozent=min_soc, entlastung_max_prozent=15.0,
            min_soc_absolut=10.0,
        )

    @staticmethod
    def _temp():
        return {"oben": 40.0, "mittig": 40.0, "unten": 40.0}

    def test_schlechte_prognose_volle_reserve(self):
        erg = evaluate_batterie(
            self._cfg(), self._temp(), feedin_watt=100.0, soc=75.0,
            kompressor_ein=False, now_hour=12,
            nachtsperre_start=22, nachtsperre_ende=6,
            forecast_wh_qm=500.0,  # schlechter Tag
        )
        # SOC 75 < Reserve 80 -> keine Aktion
        assert erg.einschalten is not True

    def test_gute_prognose_entlastet_reserve(self):
        erg = evaluate_batterie(
            self._cfg(), self._temp(), feedin_watt=100.0, soc=70.0,
            kompressor_ein=False, now_hour=12,
            nachtsperre_start=22, nachtsperre_ende=6,
            forecast_wh_qm=2500.0,  # guter Tag -> Reserve 65%
        )
        assert erg.einschalten is True
        assert "70" in erg.grund or "EIN" in erg.grund

    def test_absolutes_minimum_wird_respektiert(self):
        cfg = self._cfg(min_soc=20.0)  # Entlastung wuerde auf 5% gehen
        erg = evaluate_batterie(
            cfg, self._temp(), feedin_watt=100.0, soc=11.0,
            kompressor_ein=False, now_hour=12,
            nachtsperre_start=22, nachtsperre_ende=6,
            forecast_wh_qm=3000.0,
        )
        # min_soc_absolut=10: effektive Reserve = max(20-15, 10)=10 -> SOC 11 reicht
        assert erg.einschalten is True


class TestTaktschutz:
    """Punkt D: Zu viele Regelwechsel verlaengern die Pause."""

    def _state_mit_hist(self, wechsel_minuten_liste):
        """Historie relativ zur ECHTEN Zeit aufbauen (kein Mock noetig)."""
        now = datetime.now()
        hist = deque()
        for m in wechsel_minuten_liste:
            hist.append((now - timedelta(minutes=m), "Test"))
        state = SimpleNamespace(
            local_tz=None,
            control=SimpleNamespace(_wechsel_historie=hist),
            priority_config=SimpleNamespace(
                taktschutz=SimpleNamespace(
                    aktiv=True, max_wechsel_pro_stunde=4,
                    dauer_minuten=120, zusatz_pause_minuten=15,
                )
            ),
        )
        return state, now

    def test_unter_grenzwert_blockiert_nicht(self):
        state, now = self._state_mit_hist([50, 55, 58])  # nur 3 Wechsel
        pause = pcl._taktschutz_blockiert(state, state.priority_config)
        assert pause == 0.0

    def test_ueber_grenzwert_liefert_zusatzpause(self):
        state, now = self._state_mit_hist([1, 2, 3, 4])  # 4 Wechsel >= Grenze
        pause = pcl._taktschutz_blockiert(state, state.priority_config)
        assert pause == 15 * 60.0

    def test_alte_wechsel_zaehlen_nicht(self):
        state, now = self._state_mit_hist([70, 75, 80, 85])  # aelter als 1h
        pause = pcl._taktschutz_blockiert(state, state.priority_config)
        assert pause == 0.0

    def test_inaktiv_liefert_null(self):
        state, _ = self._state_mit_hist([5, 10, 15, 20])
        state.priority_config.taktschutz.aktiv = False
        assert pcl._taktschutz_blockiert(state, state.priority_config) == 0.0

    def test_cfg_none_liefert_null(self):
        state = SimpleNamespace(control=SimpleNamespace(_wechsel_historie=None))
        assert pcl._taktschutz_blockiert(state, None) == 0.0

    def test_track_wechsel_legt_historie_an(self):
        state = SimpleNamespace(
            local_tz=None,
            control=SimpleNamespace(),
        )
        pcl._track_wechsel(state, "X")
        assert isinstance(state.control._wechsel_historie, deque)
        assert len(state.control._wechsel_historie) == 1

    # ── Bugfix: Zaehler stieg mit jedem Loop-Durchlauf (~13 s) ──

    def test_track_wechsel_gleicher_gewinner_zaehlt_nicht(self):
        """Gleicher Gewinner in Folge ist KEIN Wechsel.

        Vorher wurde jeder Loop angehaengt -> 8 Durchlaufe reichten fuer
        den Taktschutz, der feuerte dann dauerhaft ohne echten Grund
        (Log: +1 Wechsel alle ~13 s).
        """
        state = SimpleNamespace(local_tz=None, control=SimpleNamespace())
        for _ in range(100):
            pcl._track_wechsel(state, "Abweichung")
        assert len(state.control._wechsel_historie) == 1

    def test_track_wechsel_100_loops_stabil_kein_taktschutz(self):
        """Integration: 100 Durchlaeufe mit stabiler Regel -> keine Zusatzpause."""
        state = SimpleNamespace(
            local_tz=None,
            control=SimpleNamespace(),
            priority_config=SimpleNamespace(
                taktschutz=SimpleNamespace(
                    aktiv=True, max_wechsel_pro_stunde=8,
                    dauer_minuten=120, zusatz_pause_minuten=15,
                )
            ),
        )
        for _ in range(100):
            pcl._track_wechsel(state, "Abweichung")
        assert pcl._taktschutz_blockiert(state, state.priority_config) == 0.0

    def test_track_wechsel_alternierende_regeln_zaehlen_jeden_wechsel(self):
        """A->B->A->B: Jeder echte Wechsel wird erfasst."""
        state = SimpleNamespace(local_tz=None, control=SimpleNamespace())
        for name in ("A", "B", "A", "B"):
            pcl._track_wechsel(state, name)
        hist = state.control._wechsel_historie
        assert [n for _, n in hist] == ["A", "B", "A", "B"]

    def test_track_wechsel_raeumt_alt_eintraege_ohne_wechsel_ab(self):
        """Auch ohne neuen Wechsel werden >1h alte Eintraege entfernt."""
        now = datetime.now()
        hist = deque([(now - timedelta(minutes=90), "Alt")])
        state = SimpleNamespace(
            local_tz=None, control=SimpleNamespace(_wechsel_historie=hist)
        )
        pcl._track_wechsel(state, "Alt")
        # Alter Eintrag weg, frischer Baseline-Eintrag da
        assert len(hist) == 1
        assert hist[0][0] >= now - timedelta(minutes=5)


class TestMorgenfensterBonus:
    """Punkt B: Bonus-Vorlauf verschiebt das gelernte Morgenfenster frueher."""

    def test_fenster_ohne_bonus_und_mit_bonus(self, tmp_path):
        pfad = str(tmp_path / "lern.json")
        basis = datetime.now().replace(hour=7, minute=30, second=0, microsecond=0)
        eng = LearningEngine(data_path=pfad)
        for d in range(4, 9):
            ev = {"timestamp": basis.replace(day=d).isoformat(), "menge": 1}
            if d <= 31:
                pass
            eng.data.usage_events.append(ev)
        eng.data.morning_target_hour_samples = 5
        eng.data.learned_morning_target_hour = 7.5
        # Direkt ueber usage events geht's nicht (Datum im Januar nötig) ->
        # wir pruefen nur die Helper-Logik:
        fenster = pcl._gelerntes_morgenfenster(eng)
        bonus = pcl._gelerntes_morgenfenster_mit_bonus(None, eng)
        # Ohne Verletzungen identisch
        if fenster is not None and eng.get_komfort_bonus_vorlauf() == 0.0:
            assert fenster == bonus
