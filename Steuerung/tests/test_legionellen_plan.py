"""Regressionen fuer persistente Legionellen-Tagesplanung."""

from datetime import datetime
from types import SimpleNamespace

import pytz

import legionellen_plan
from main import _aktualisiere_legionellen_lifecycle
from priority_control import RegelErgebnis


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
