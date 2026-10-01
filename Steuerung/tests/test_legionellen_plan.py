"""Regressionen fuer persistente Legionellen-Tagesplanung."""

from datetime import datetime
from types import SimpleNamespace

import pytz

import legionellen_plan
from json_config import LegionellenConfig
from main import (
    _aktualisiere_legionellen_lifecycle,
    _beende_legionellenlauf_if_needed,
    _complete_legionellen_lifecycle,
)
from priority_control import RegelErgebnis, evaluate_legionellen


TZ = pytz.timezone("Europe/Berlin")


def _state():
    return SimpleNamespace(
        local_tz=TZ,
        legionellen_planned_day=None,
        legionellen_planned_tag=None,
        legionellen_planned_time=None,
        legionellen_planned_date=None,
        legionellen_planned_forecast_wh=None,
        legionellen_plan_revision=0,
        legionellen_plan_created_at=None,
        legionellen_planned_reason=None,
        legionellen_aktiv=False,
        legionellen_target_reached_at=None,
        legionellen_started_at=None,
        legionellen_temp_override=None,
        legionellen_end_time=None,
        legionellen_last_done=None,
        legionellen_unvollstaendig=False,
        legionellen_telegram_start_sent=True,
        legionellen_telegram_done_sent=True,
        control=SimpleNamespace(
            kompressor_ein=True,
            _lauf_start_regel="Legionellen",
            requested_rule_name=None,
            effective_rule_name=None,
            effective_source=None,
        ),
        sensors=SimpleNamespace(t_unten=35.0),
        solar=SimpleNamespace(acpower=600.0, batpower=0.0),
        priority_config=SimpleNamespace(
            legionellen=SimpleNamespace(
                aktiv=True,
                target_temp_c=60.0,
                legionellen_max_temp_c=65.0,
                max_duration_hours=8,
                probezeit_minuten=0,
            )
        ),
        _last_was_legionellen=False,
    )


def test_plan_roundtrip_und_clear_ist_idempotent(tmp_path, monkeypatch):
    path = str(tmp_path / "legionellen_plan.json")
    monkeypatch.setattr(legionellen_plan, "PLAN_FILE", path)
    state = _state()
    created = datetime.now(TZ)
    state.legionellen_planned_day = "Freitag"
    state.legionellen_planned_tag = 4
    state.legionellen_planned_time = "08:00"
    state.legionellen_planned_date = created.date()
    state.legionellen_planned_forecast_wh = 2500.0
    state.legionellen_plan_created_at = created
    state.legionellen_planned_reason = "Test"

    assert legionellen_plan.save_plan(state, path=path) is True
    restored = _state()
    assert legionellen_plan.load_plan(restored, path=path) is True
    assert restored.legionellen_planned_day == "Freitag"
    assert restored.legionellen_planned_date == created.date()

    legionellen_plan.clear_plan(restored, persist=True)
    assert restored.legionellen_planned_date is None
    assert restored.legionellen_plan_revision == 1
    legionellen_plan.clear_plan(restored, persist=True)
    assert restored.legionellen_plan_revision == 1


def test_verstrichener_plan_wird_wirklich_geloescht(tmp_path, monkeypatch):
    path = str(tmp_path / "legionellen_plan.json")
    monkeypatch.setattr(legionellen_plan, "PLAN_FILE", path)
    state = _state()
    state.legionellen_planned_day = "Freitag"
    state.legionellen_planned_tag = 4
    state.legionellen_planned_time = "08:00"
    state.legionellen_planned_date = datetime(2025, 1, 1).date()
    state.legionellen_planned_forecast_wh = 2500.0
    state.legionellen_plan_created_at = datetime(2025, 1, 1)
    assert legionellen_plan.save_plan(state, path=path) is True

    restored = _state()
    assert legionellen_plan.load_plan(restored, path=path) is False
    assert restored.legionellen_planned_date is None
    assert restored.legionellen_planned_tag is None



def test_legionellen_start_und_probezeit_werden_nach_hardware_start_gepflegt():
    import asyncio

    async def run():
        state = _state()
        result = {"gewinner_ergebnis": RegelErgebnis(
            name="Legionellen", prioritaet=90, aktiv=True, einschalten=True
        )}
        await _aktualisiere_legionellen_lifecycle(None, state, result)
        assert state.legionellen_aktiv is True
        assert state.legionellen_started_at is not None

        state.sensors.t_unten = 60.0
        result = {"gewinner_ergebnis": RegelErgebnis(
            name="Legionellen", prioritaet=90, aktiv=True, einschalten=True
        )}
        await _aktualisiere_legionellen_lifecycle(None, state, result)
        assert state.legionellen_target_reached_at is not None

        result = {"gewinner_ergebnis": RegelErgebnis(
            name="Legionellen", prioritaet=90, aktiv=True, einschalten=False
        )}
        await _aktualisiere_legionellen_lifecycle(None, state, result)
        assert state.legionellen_aktiv is False
        assert state.legionellen_last_done is not None

    asyncio.run(run())


# ===========================================================
# Abbruch vor dem Ziel: kein stiller Ausfall der Prophylaxe
# ===========================================================

class TestAbbruchVorZiel:
    """Regression aus dem Laufzeitlog 18.09.-01.10.2026.

    Am 25.09.2026 wurde die Legionellenfahrt dreimal abgebrochen und erreichte
    nur 53,0 C (Ziel 60 C). Vorher raeumte jeder Abbruch Plan und Flag
    restlos ab - die Prophylaxe fiel damit fuer diesen Zyklus still aus und
    startete erst am Folgetag kalt bei 23,7 C neu (7,5 h statt 2 h).
    """

    def test_abbruch_vor_ziel_merkt_sich_als_unvollstaendig(self):
        from datetime import date as _date
        st = _state()
        # Einen echten Plan setzen: genau dieser darf beim Abbruch NICHT
        # geraeumt werden (vorher rief der Abbruchpfad clear_plan() auf).
        st.legionellen_planned_date = _date(2026, 9, 25)
        st.legionellen_planned_tag = 4
        st.legionellen_plan_revision = 1
        st.legionellen_aktiv = True
        st.legionellen_started_at = datetime(2026, 9, 25, 12, 0, tzinfo=TZ)
        # Ziel NICHT erreicht -> target_reached_at bleibt None
        st.legionellen_target_reached_at = None
        _beende_legionellenlauf_if_needed(st, datetime(2026, 9, 25, 15, 25, tzinfo=TZ), "AUS")
        assert st.legionellen_aktiv is False
        assert st.legionellen_unvollstaendig is True, (
            "Abbruch vor dem Ziel muss als unvollstaendig vermerkt werden"
        )
        # Der Plan bleibt stehen - sonst waere kein Nachversuch moeglich.
        assert st.legionellen_planned_date == _date(2026, 9, 25)
        assert st.legionellen_planned_tag == 4

    def test_erfolgreicher_abschluss_raeumt_den_plan(self):
        """Gegenprobe: nach erfuellter Probezeit wird sehr wohl geraeumt."""
        from datetime import date as _date
        st = _state()
        st.legionellen_planned_date = _date(2026, 9, 26)
        st.legionellen_aktiv = True
        st.legionellen_started_at = datetime(2026, 9, 26, 7, 8, tzinfo=TZ)
        st.legionellen_target_reached_at = datetime(2026, 9, 26, 14, 30, tzinfo=TZ)
        _beende_legionellenlauf_if_needed(st, datetime(2026, 9, 26, 15, 0, tzinfo=TZ), "AUS")
        assert st._legionellen_completion_pending is True
        assert st.legionellen_planned_date is None, (
            "Nach erfolgreichem Abschluss muss der Plan geraeumt sein"
        )

    def test_erfolgreicher_abschluss_setzt_flag_zurueck(self):
        st = _state()
        st.legionellen_aktiv = True
        st.legionellen_unvollstaendig = True
        _complete_legionellen_lifecycle(st, datetime(2026, 9, 26, 14, 40, tzinfo=TZ))
        assert st.legionellen_unvollstaendig is False
        assert st.legionellen_last_done is not None

    def test_nachversuch_umgeht_wochentags_und_zeitfenster(self):
        """Der Nachversuch darf nicht am Wochentags-Gate scheitern.

        Wichtig fuer den Realfall: haette der Abbruch am Donnerstag
        stattgefunden, waere Freitag/Samstag/Sonntag der einzige Rest der
        KW gewesen - jetzt greift das Gate nicht mehr.
        """
        cfg = LegionellenConfig(
            aktiv=True, prioritaet=90, target_temp_c=60.0,
            legionellen_max_temp_c=65.0, probezeit_minuten=15,
            bevorzugter_tag=4, letzter_tag=6, start_uhr=8.0,
            spaeteste_start_uhr=12, pv_start_min_watt=500.0,
            batterie_start_min_watt=50.0, batterie_start_min_soc_prozent=90.0,
        )
        # Mittwoch, 21 Uhr: ausserhalb Wochentagsfenster UND Startfenster
        now = datetime(2026, 9, 23, 21, 0)
        temps = {"unten": 40.0, "oben": 45.0, "mittig": 42.0}

        # Ohne Nachversuch: kein Start (Wochentag + spaetester Start verpasst)
        ohne = evaluate_legionellen(cfg, temps, now, pv_leistung=3000.0,
                                   pv_acpower=3000.0, soc=80.0)
        assert ohne.einschalten is None, ohne.grund

        # Mit Nachversuch: Start trotzdem zulaessig
        mit = evaluate_legionellen(cfg, temps, now, pv_leistung=3000.0,
                                  pv_acpower=3000.0, soc=80.0,
                                  legionellen_unvollstaendig=True)
        assert mit.einschalten is True, (
            f"Nachversuch wurde durch das Wochentags-Gate blockiert: {mit.grund}"
        )

    def test_nachversuch_respektiert_erst_erfolg_im_kw(self):
        """Ohne offene Fahrt bleibt 'Bereits in KW erledigt' bestehen."""
        cfg = LegionellenConfig(
            aktiv=True, prioritaet=90, target_temp_c=60.0,
            legionellen_max_temp_c=65.0, probezeit_minuten=15,
            bevorzugter_tag=4, letzter_tag=6, start_uhr=8.0,
        )
        now = datetime(2026, 9, 25, 9, 0)  # Freitag
        temps = {"unten": 40.0, "oben": 45.0, "mittig": 42.0}
        ergebnis = evaluate_legionellen(
            cfg, temps, now, pv_leistung=3000.0, pv_acpower=3000.0,
            soc=80.0, legionellen_last_done=datetime(2026, 9, 24).date(),
            legionellen_unvollstaendig=False,
        )
        assert ergebnis.aktiv is False
        assert "Bereits in KW" in ergebnis.grund
