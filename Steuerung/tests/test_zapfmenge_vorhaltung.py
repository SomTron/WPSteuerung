"""Zapfungsmenge und Vorhaltung (BUGFIX 22.10.2026).

Aus der Live-Auswertung (Entscheidungslog 09.-15.10.2026): nach einer
Zapfung fiel der untere Fuehler von 43,2 auf 24,3 C, waehrend Mitte
(48 C) und Oben (48 C) unveraendert blieben. Das ist keine Abkuehlung,
sondern entnommenes Wasser - der Speicher war danach von unten kalt.

Die WP kann das nicht rueckgaengig machen: sie fuelt den Speicher von
oben, und oben ist bereits voll (Boiler-Max 48 C). Also muss die
Reserve VOR der Zapfung im Speicher liegen.

Diese Tests decken ab:
  1) Die Zapfungsmenge wird aus dem Temperaturabfall geschaetzt.
  2) Sie wird gelernt (Mittel + Peak).
  3) Die Vorhaltung erhoeht den SOLLwert (nicht die Zielzeit) und
     verschiebt damit den Start sinnvoll frueher.
  4) Die Verdrahtung von der Lernengine bis zum CalcStart.
"""
from datetime import datetime, timedelta

import pytest

from learning_engine import LearningEngine
from priority_control import evaluate_calculated_start
from json_config import CalculatedStartConfig


def _engine(tmp_path):
    return LearningEngine(data_path=str(tmp_path / "lern.json"))


def _zapfe(engine, start, unten_vor, unten_nach, mitte_vor=47.0, mitte_nach=46.5,
          kompressor=False):
    """Ein steiler Abfall + Ruhe-Sample: erzeugt genau EIN Ereignis."""
    engine.update(start, {"unten": unten_vor, "mittig": mitte_vor, "oben": 47.5},
                  kompressor)
    engine.update(start + timedelta(minutes=1),
                  {"unten": unten_nach, "mittig": mitte_nach, "oben": 46.5},
                  kompressor)
    engine.update(start + timedelta(minutes=8),
                  {"unten": unten_nach, "mittig": mitte_nach, "oben": 46.5},
                  kompressor)


class TestZapfmenge:
    """Die Menge wird aus dem Temperaturabfall geschaetzt."""

    def test_menge_wird_geschaetzt(self, tmp_path):
        """16 K Abfall bei 300 l -> eine plausible Menge."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        assert len(eng.data.usage_events) == 1
        menge = eng.data.usage_events[0]["zapfmenge_l"]
        assert menge > 0.0, "Die Zapfungsmenge wurde nicht berechnet"
        # 16 K Abfall, halber Speicher betroffen, 300 l:
        # Q = 16 * 300 * 1.163 * 0.5 / (1.163 * 45) ~ 53 l
        assert 20.0 < menge < 120.0, f"Unerwartete Menge: {menge} l"

    def test_menge_ist_ohne_volumen_null(self, tmp_path):
        """Ohne Speichervolumen ist keine Menge berechenbar."""
        eng = _engine(tmp_path)
        eng.data.config.speicher_volumen_l = 0.0
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        assert eng.data.usage_events[0]["zapfmenge_l"] == 0.0

    def test_menge_wird_gelernt(self, tmp_path):
        """Nach zwei Zapfungen ist die Menge verfuegbar."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)
        _zapfe(eng, datetime(2026, 10, 16, 18, 30), 43.0, 27.0)
        assert eng.data.zapfmenge_samples == 2
        assert eng.get_learned_zapfmenge_l() > 0.0
        assert eng.get_learned_zapfmenge_l(peak=True) > 0.0

    def test_peak_ist_der_groesste_wert(self, tmp_path):
        """Der Peak ist der schlimmste Fall - fuer die Vorhaltung relevant."""
        eng = _engine(tmp_path)
        _zapfe(eng, datetime(2026, 10, 15, 18, 30), 43.0, 27.0)   # gross
        _zapfe(eng, datetime(2026, 10, 16, 18, 30), 43.0, 35.0)   # klein
        _zapfe(eng, datetime(2026, 10, 17, 18, 30), 43.0, 35.0)
        assert eng.get_learned_zapfmenge_l(peak=True) >= \
            eng.get_learned_zapfmenge_l()

    def test_ohne_daten_keine_menge(self, tmp_path):
        """Ohne Zapfungen gibt es keine Menge - die Vorhaltung bleibt 0."""
        eng = _engine(tmp_path)
        assert eng.get_learned_zapfmenge_l() == 0.0
        assert eng.get_learned_zapfmenge_l(peak=True) == 0.0


class TestVorhaltungImCalcStart:
    """Die Vorhaltung erhoeht den SOLLwert und verschiebt den Start."""

    def _start(self, vorhaltung_k):
        cfg = CalculatedStartConfig()
        r = evaluate_calculated_start(
            calc_cfg=cfg,
            temp_dict={"unten": 30.0, "mittig": 35.0, "oben": 40.0},
            now_hour=10, now_minute=0,
            nachtsperre_start=19, nachtsperre_ende=8,
            forecast_wh_qm=2000.0,
            learned_heating_rate_unten=3.0,
            learned_heating_rate_gesamt=2.0,
            learned_target_hour=17.0,
            feedin_watt=0.0, soc=50.0, fc_ratio=1.0,
            surplus_profile=None,
            pv_acpower=0.0, battery_power=0.0, solar_stale=False,
            zapf_vorhaltung_k=vorhaltung_k,
        )
        return r.regel_dict or {}

    def test_ohne_vorhaltung_kein_effekt(self):
        d0 = self._start(0.0)
        assert d0["hours_needed"] == pytest.approx(4.0, abs=0.1)

    def test_vorhaltung_erhoeht_den_bedarf(self):
        """6 K Vorhaltung -> 2 K mehr Bedarf (bei 3 K/h Heizrate)."""
        d0 = self._start(0.0)
        d6 = self._start(6.0)
        assert d6["hours_needed"] > d0["hours_needed"] + 1.5, (
            f"Vorhaltung wirkt nicht auf den Bedarf: {d0} -> {d6}")

    def test_vorhaltung_verschiebt_den_start_frueher(self):
        """Der Start muss FRUEHER liegen, nicht nur der Puffer kleiner."""
        d0 = self._start(0.0)
        d6 = self._start(6.0)
        assert d6["planned_start_hour"] < d0["planned_start_hour"], (
            f"Start nicht frueher: {d0['planned_start_hour']} -> "
            f"{d6['planned_start_hour']}")

    def test_vorhaltung_ist_im_grundtext_nachvollziehbar(self):
        """Der Grund muss die Vorhaltung nennen."""
        cfg = CalculatedStartConfig()
        r = evaluate_calculated_start(
            calc_cfg=cfg,
            temp_dict={"unten": 30.0, "mittig": 35.0, "oben": 40.0},
            now_hour=10, now_minute=0,
            nachtsperre_start=19, nachtsperre_ende=8,
            forecast_wh_qm=2000.0,
            learned_heating_rate_unten=3.0,
            learned_heating_rate_gesamt=2.0,
            learned_target_hour=17.0,
            feedin_watt=0.0, soc=50.0, fc_ratio=1.0,
            surplus_profile=None,
            pv_acpower=0.0, battery_power=0.0, solar_stale=False,
            zapf_vorhaltung_k=4.0,
        )
        assert "Vorhaltung" in str(r.grund) or "4.0K" in str(r.grund), (
            "Die Vorhaltung muss im Grundtext stehen: " + str(r.grund))


class TestWochentag:
    """Sonntag wird durch Baden mehr Warmwasser gebraucht (BUGFIX 22.10.2026).

    Live-Messung (Entscheidungslog 09.-15.10.2026), groesster steiler
    Temperaturabfall am unteren Fuehler:

        Sa 5,8 K   Fr 4,2 K   Do 3,2 K   Mi 2,7 K   Mo 2,1 K

    Die Menge muss deshalb je Wochentag gelernt werden - mit einem
    globalen Mittelwert wuerde die WP auch montags auf dem
    Sonntags-Niveau vorheizen.
    """

    def _lern_menge(self, eng, wochentag, menge_abfall_k):
        """Eine Zapfung an einem bestimmten Wochentag erzeugen."""
        tag = datetime(2026, 10, 12 + wochentag, 18, 30)  # 12.10.=Mo
        _zapfe(eng, tag, 43.0, 43.0 - menge_abfall_k)

    def test_je_wochentag_wird_gelernt(self, tmp_path):
        eng = _engine(tmp_path)
        # Sa groesser als Mo
        self._lern_menge(eng, 5, 16.0)   # Samstag
        self._lern_menge(eng, 0, 8.0)    # Montag
        assert "5" in eng.data.learned_zapfmenge_je_wochentag
        assert "0" in eng.data.learned_zapfmenge_je_wochentag
        sa = eng.data.learned_zapfmenge_je_wochentag["5"]
        mo = eng.data.learned_zapfmenge_je_wochentag["0"]
        assert sa > mo, f"Samstag muert mehr als Montag: {sa} vs {mo}"

    def test_wochentags_getter_unterscheidet(self, tmp_path):
        """Mit je 2 Beobachtungen je Wochentag greift der
        wochentagsspezifische Wert."""
        eng = _engine(tmp_path)
        self._lern_menge(eng, 5, 16.0)   # Sa
        self._lern_menge(eng, 5, 16.0)
        self._lern_menge(eng, 0, 8.0)    # Mo
        self._lern_menge(eng, 0, 8.0)
        sa = eng.get_learned_zapfmenge_je_wochentag(5)
        mo = eng.get_learned_zapfmenge_je_wochentag(0)
        assert sa > mo, f"Samstag muert mehr als Montag: {sa} vs {mo}"

    def test_zu_wenig_daten_nimmt_den_globalen_wert(self, tmp_path):
        """Mit nur einer Beobachtung fuer Sonntag gilt der globale Wert -
        lieber ein Mittelwert als gar keiner."""
        eng = _engine(tmp_path)
        self._lern_menge(eng, 5, 16.0)
        self._lern_menge(eng, 0, 8.0)
        self._lern_menge(eng, 0, 8.0)
        # Sonntag hat nur 1 Sample -> globaler Rueckfall
        assert eng.get_learned_zapfmenge_je_wochentag(5) == \
            eng.get_learned_zapfmenge_l()

    def test_ungueltiger_wochentag_nimmt_den_globalen_wert(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng.get_learned_zapfmenge_je_wochentag(99) == \
            eng.get_learned_zapfmenge_l()
        assert eng.get_learned_zapfmenge_je_wochentag(-1) == \
            eng.get_learned_zapfmenge_l()


class TestVerdrahtung:
    """Von der Lernengine bis zum CalcStart - die Leitung muss existieren."""

    def test_priority_control_logic_reicht_die_vorhaltung_weiter(self):
        import pathlib
        quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        assert "zapf_vorhaltung_k=zapf_vorhaltung_k" in text, (
            "Die Vorhaltung wird nicht an bewerte_alle_regeln gereicht")

    def test_bewerte_alle_regeln_hat_die_parameter(self):
        import inspect
        from priority_control import bewerte_alle_regeln
        params = inspect.signature(bewerte_alle_regeln).parameters
        assert "zapf_vorhaltung_k" in params
        assert "zapfmenge_l" in params

    def test_evaluate_calculated_start_hat_die_parameter(self):
        import inspect
        from priority_control import evaluate_calculated_start
        params = inspect.signature(evaluate_calculated_start).parameters
        assert "zapf_vorhaltung_k" in params

    def test_die_menge_wird_durchgereicht(self):
        """Die gelernte Menge muss bis zum CalcStart kommen - sie ist
        die Grundlage der Vorhaltung (und der Anzeige)."""
        import pathlib
        quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        assert "zapfmenge_l=gelernte_zapfmenge_l" in text, (
            "Die Zapfungsmenge wird nicht durchgereicht")

    def test_der_wochentag_wird_verwendet(self):
        """Die Mengen-Berechnung muss den Wochentag der ZAPFUNG
        verwenden - nicht den heutigen."""
        import pathlib
        quelle = pathlib.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        assert "get_learned_zapfmenge_je_wochentag" in text, (
            "Der wochentagsspezifische Wert wird nicht verwendet")
        assert "_zapf_wochentag" in text, (
            "Der Wochentag der Zapfung wird nicht bestimmt")
        # Der Wochentag muss aus dem GEPLANTEN Termin stammen, wenn
        # einer steht - sonst wuerde die WP fuer den falschen Tag
        # vorheizen.
        assert "planned_date.weekday()" in text, (
            "Der Wochentag des geplanten Termins wird nicht verwendet")
        # Die Zuweisung muss den GEPLANTEN Termin bevorzugen. Wird
        # stattdessen immer der heutige Wochentag genommen, faellt die
        # Mutation "fest auf Montag" nicht auf.
        import re
        block = re.search(
            r"_zapf_wochentag = now_local\.weekday\(\).*?"
            r"(?=_get_menge)",
            text, re.S)
        assert block, "Die Wochentag-Bestimmung wurde nicht gefunden"
        # Geprueft wird die ZUWEISUNG, nicht nur das Vorkommen des
        # Wortes: eine Mutation, die den geplanten Termin UEBERGAENGT
        # (if False:), muss auffallen.
        zuweisung = re.search(
            r"if planned_date is not None:.*?"
            r"_zapf_wochentag = int\(planned_date\.weekday\(\)\)",
            block.group(0), re.S)
        assert zuweisung, (
            "Der geplante Termin wird nicht fuer den Wochentag genutzt - "
            "der Wochentag wird immer vom HEUTIGEN Tag genommen")

    def test_ohne_lernengine_keine_vorhaltung(self, tmp_path):
        """Ohne Lernengine (oder mit zu wenig Daten) bleibt alles neutral."""
        eng = _engine(tmp_path)
        assert eng.get_learned_zapfmenge_l() == 0.0

    @pytest.mark.asyncio
    async def test_zukuenftiger_termin_ueberschreibt_heutigen_wochentag_nicht(self, tmp_path):
        """BUGFIX (24.10.2026): Ein geplanter Termin in der Zukunft (z. B. Sonntag)
        darf an einem Montag nicht dazu fuehren, dass mit dem Sonntags-Verbrauch gerechnet wird.
        """
        import pytz
        from unittest.mock import MagicMock, patch
        import priority_control_logic as pcl
        from json_config import WPSteuerungConfig
        from types import SimpleNamespace

        eng = _engine(tmp_path)
        eng.get_learned_zapfmenge_je_wochentag = MagicMock(return_value=50.0)

        # Montag, 19.10.2026
        jetzt = pytz.timezone("Europe/Berlin").localize(datetime(2026, 10, 19, 12, 0))
        cfg = WPSteuerungConfig()

        state = SimpleNamespace(
            local_tz=pytz.timezone("Europe/Berlin"),
            sensors=SimpleNamespace(t_oben=45.0, t_mittig=40.0, t_unten=35.0, t_verd=15.0, t_boiler=40.0),
            solar=SimpleNamespace(feedinpower=0.0, forecast_tomorrow=None, forecast_today=None,
                                  forecast_hourly_wm2=None, acpower=None, pv_erzeugung=None,
                                  soc=None, hausverbrauch=None),
            control=SimpleNamespace(kompressor_ein=False, alle_ergebnisse=[],
                                    schichtung_oben_start=None, schichtung_oben_max=None),
            priority_config=cfg,
            bademodus_aktiv=False,
            urlaubsmodus_aktiv=False,
            # Termin liegt am Sonntag (Zukunft!)
            legionellen_planned_date=datetime(2026, 10, 25).date(),
            legionellen_planned_tag=6,
            legionellen_last_done=None,
            legionellen_started_at=None,
            legionellen_aktiv=False,
        )

        with patch("priority_control_logic._now_for_state", return_value=jetzt):
            await pcl.determine_mode_and_setpoints(state, 35.0, 40.0, learning_engine=eng)

        # Es muss Wochentag 0 (Montag) abgefragt worden sein, NICHT 6 (Sonntag)!
        eng.get_learned_zapfmenge_je_wochentag.assert_called_with(0)

