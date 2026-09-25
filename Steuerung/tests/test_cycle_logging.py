"""Tests fuer die persistente Kompressorzyklus-Historie."""
import csv
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cycle_logging
from cycle_logging import CYCLE_CSV_HEADER, begin_cycle, finish_cycle, update_cycle_maxima


def _state(start_regel="Einspeisung"):
    return SimpleNamespace(
        sensors=SimpleNamespace(t_unten=30.0, t_mittig=40.0, t_oben=50.0, t_verd=5.0),
        control=SimpleNamespace(
            kompressor_ein=False, active_rule_name=start_regel, blocking_reason=None
        ),
    )


def test_schema_entspricht_der_bestehenden_loganalyse():
    assert CYCLE_CSV_HEADER == [
        "start", "ende", "dauer_min", "quelle", "source_at_start", "start_regel", "end_grund",
        "start_unten", "start_mittig", "start_oben",
        "max_unten", "max_mittig", "max_oben",
        "ueberschreitung_k", "start_verd",
    ]


def test_altes_10_spalten_schema_wird_verlustfrei_migriert(tmp_path):
    pfad = tmp_path / "zyklen.csv"
    with open(pfad, "w", encoding="utf-8", newline="") as f:
        f.write("start;ende;dauer_min;quelle;start_regel;end_grund;start_unten;max_unten;ueberschreitung_k;start_verd\n")
        f.write("2026-09-01 10:00:00;2026-09-01 10:10:00;10.0;pv;AdaptivePV;regel_aus;40.0;44.0;0.0;18.0\n")

    assert cycle_logging.ensure_cycle_csv(str(pfad), jetzt=datetime(2026, 9, 24)) is True
    with open(pfad, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert list(rows[0]) == CYCLE_CSV_HEADER
    assert rows[0]["start_regel"] == "AdaptivePV"
    assert rows[0]["max_unten"] == "44.0"
    assert rows[0]["start_mittig"] == ""
    assert rows[0]["start_oben"] == ""


def test_abschluss_speichert_start_maxima_und_endgrund(tmp_path):
    state = _state("Batterie")
    state.sensors.t_oben = 48.0
    start = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    ende = start + timedelta(minutes=42, seconds=12)
    pfad = str(tmp_path / "csv log" / "zyklen.csv")

    begin_cycle(state, start, 7)
    state.control.kompressor_ein = True
    state.sensors.t_unten = 42.0
    state.sensors.t_mittig = 48.0
    state.sensors.t_oben = 49.5
    assert update_cycle_maxima(state) is True
    state.sensors.t_unten = 41.0  # niedriger darf Maximum nicht verkleinern
    assert finish_cycle(state, ende, end_grund="regel_aus", csv_path=pfad) is True

    with open(pfad, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert len(rows) == 1
    row = rows[0]
    assert row["start"] == "2026-09-23 10:00:00"
    assert row["ende"] == "2026-09-23 10:42:12"
    assert row["dauer_min"] == "42.2"
    assert row["quelle"] == "batterie"
    assert row["start_regel"] == "Batterie"
    assert row["end_grund"] == "regel_aus"
    assert row["start_unten"] == "30.0"
    assert row["max_unten"] == "42.0"
    assert row["max_mittig"] == "48.0"
    assert row["max_oben"] == "49.5"
    assert row["ueberschreitung_k"] == "0.0"
    assert state._cycle_log is None


def test_unbekannte_quelle_bleibt_unbekannt(tmp_path):
    state = _state("SonstigeRegel")
    start = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    begin_cycle(state, start, 1)
    state.control.kompressor_ein = True
    assert finish_cycle(state, start, csv_path=str(tmp_path / "zyklen.csv")) is True
    with open(tmp_path / "zyklen.csv", encoding="utf-8", newline="") as f:
        row = next(csv.DictReader(f, delimiter=";"))
    assert row["quelle"] == "unbekannt"
    assert row["source_at_start"] == "unbekannt"


    state = _state()
    state.sensors.t_unten = "30.0"
    state.sensors.t_oben = float("nan")
    start = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    begin_cycle(state, start, 1)
    state.control.kompressor_ein = True
    assert update_cycle_maxima(state) is True
    assert finish_cycle(state, start, csv_path=str(tmp_path / "zyklen.csv")) is True
    with open(tmp_path / "zyklen.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    row = rows[-1]
    assert row["start_unten"] == ""
    assert row["start_oben"] == ""
    assert row["max_unten"] == ""


def test_abschluss_ohne_lauf_ist_noop():
    state = _state()
    assert finish_cycle(state, datetime.now(timezone.utc)) is False


def test_schreibfehler_wirft_nicht(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle_logging, "ensure_cycle_csv", lambda *a, **k: False)
    state = _state()
    now = datetime.now(timezone.utc)
    begin_cycle(state, now, 1)
    assert finish_cycle(state, now, csv_path=str(tmp_path / "zyklen.csv")) is False


@pytest.mark.asyncio
async def test_set_kompressor_status_schreibt_einen_zyklus(monkeypatch, tmp_path, caplog):
    import main

    pfad = str(tmp_path / "zyklen.csv")
    monkeypatch.setattr(cycle_logging, "CYCLE_CSV", pfad)
    monkeypatch.setattr(
        main, "hardware_manager",
        SimpleNamespace(set_compressor_state=lambda value: True),
    )
    state = SimpleNamespace(
        local_tz=timezone.utc,
        sensors=SimpleNamespace(t_unten=30.0, t_mittig=40.0, t_oben=50.0, t_verd=5.0),
        control=SimpleNamespace(
            kompressor_ein=False, zyklus_id=0, active_rule_name="PV_unten",
            source_at_start="PV", blocking_reason=None,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=None, last_compressor_off_time=None,
            total_runtime_today=timedelta(), last_completed_cycle=None,
        ),
    )

    assert await main.set_kompressor_status(state, True) is True
    start_snapshot = dict(state._cycle_log)
    # Ein redundantes force_on darf keinen zweiten Lauf erzeugen.
    assert await main.set_kompressor_status(state, True, force=True) is True
    assert state._cycle_log == start_snapshot
    assert state.control.zyklus_id == 1

    with caplog.at_level(logging.INFO):
        assert await main.set_kompressor_status(
            state, False, force=True, end_grund="boiler_max"
        ) is True
    assert any(
        "Kompressor AUS (cycle=1)" in record.getMessage()
        and "reason=boiler_max" in record.getMessage()
        for record in caplog.records
    )
    with open(pfad, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["quelle"] == "pv"
    assert rows[0]["end_grund"] == "boiler_max"
    assert len(state.control._hardware_wechsel_historie) == 2
    assert [entry[1] for entry in state.control._hardware_wechsel_historie] == [True, False]



@pytest.mark.asyncio
async def test_gpio_fehler_laesst_state_aus(monkeypatch):
    import main

    monkeypatch.setattr(
        main, "hardware_manager",
        SimpleNamespace(set_compressor_state=lambda value: False),
    )
    state = SimpleNamespace(
        local_tz=timezone.utc,
        control=SimpleNamespace(kompressor_ein=False, blocking_reason=None),
    )

    assert await main.set_kompressor_status(state, True) is False
    assert state.control.kompressor_ein is False
    assert "fehlgeschlagen" in state.control.blocking_reason


@pytest.mark.asyncio
async def test_redundantes_force_off_veraendert_pause_nicht(monkeypatch):
    import main

    off_time = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        main, "hardware_manager",
        SimpleNamespace(set_compressor_state=lambda value: True),
    )
    state = SimpleNamespace(
        local_tz=timezone.utc,
        control=SimpleNamespace(kompressor_ein=False, blocking_reason=None),
        stats=SimpleNamespace(last_compressor_off_time=off_time),
    )

    assert await main.set_kompressor_status(
        state, False, force=True, end_grund="api_manuell"
    ) is True
    assert state.stats.last_compressor_off_time == off_time
    assert state.control.kompressor_ein is False
