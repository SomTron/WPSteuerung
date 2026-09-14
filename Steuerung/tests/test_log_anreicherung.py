# -*- coding: utf-8 -*-
"""Tests fuer die umgesetzten Log-Empfehlungen (Teil 1/2).

Abgedeckt: Kompakt-Log, Zyklus-ID/Reason-Kodierung, BOILERMAX-Kontext, Kontext
in der Status-Zeile, Sensor-Degradation.
"""
import logging
import sys
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from priority_control import RegelErgebnis  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ---------------- 1) Kompakt-Log ---------------- #
class TestKompaktLog:
    def _state(self):
        return SimpleNamespace(local_tz=TZ)

    def test_verdikt_aenderung_loggt(self):
        state = self._state()
        erg = [RegelErgebnis(name="Einspeisung", prioritaet=85,
                             aktiv=True, einschalten=True, grund="")]
        assert pcl._soll_priority_loggen(state, erg) is True

    def test_gleiches_verdikt_loggt_nicht_erneut_innerhalb_60min(self):
        state = self._state()
        erg = [RegelErgebnis(name="Einspeisung", prioritaet=85,
                             aktiv=True, einschalten=True, grund="")]
        assert pcl._soll_priority_loggen(state, erg) is True
        assert pcl._soll_priority_loggen(state, erg) is False

    def test_verdikt_wechsel_in_aus_loggt_wieder(self):
        state = self._state()
        ein = [RegelErgebnis(name="Einspeisung", prioritaet=85,
                             aktiv=True, einschalten=True, grund="")]
        aus = [RegelErgebnis(name="Einspeisung", prioritaet=85,
                             aktiv=True, einschalten=False, grund="")]
        assert pcl._soll_priority_loggen(state, ein) is True
        assert pcl._soll_priority_loggen(state, ein) is False
        assert pcl._soll_priority_loggen(state, aus) is True

    def test_nach_60min_snapshot_auch_ohne_aenderung(self):
        state = self._state()
        state._last_priority_log = datetime.now(TZ) - timedelta(minutes=70)
        erg = [RegelErgebnis(name="Einspeisung", prioritaet=85,
                             aktiv=True, einschalten=True, grund="")]
        assert pcl._soll_priority_loggen(state, erg) is True


# ---------------- 2) Zyklus-ID / Reason-Kodierung ---------------- #
def baue_off_state(lauf_minuten=30):
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=SimpleNamespace(
            sicherheit=SimpleNamespace(ueberhitzung_c=58.0)),
        control=SimpleNamespace(
            kompressor_ein=True, blocking_reason=None,
            _soll_einschalten=False, zyklus_id=42),
        stats=SimpleNamespace(
            last_compressor_on_time=datetime.now(TZ) - timedelta(minutes=lauf_minuten)),
    )


@pytest.mark.asyncio
async def test_abschaltmeldung_enthaelt_zyklus_id_und_reason(caplog):
    state = baue_off_state()
    with caplog.at_level(logging.INFO):
        result = await pcl.handle_compressor_off(
            state=state, session=None, regelfuehler=26.5, ausschaltpunkt=48.0,
            min_laufzeit=timedelta(minutes=15), t_oben=40.0,
            set_kompressor_status_func=AsyncMock(return_value=True),
        )
    assert result is True
    meldungen = [r.message for r in caplog.records]
    assert any("Keine Regel aktiv: Kompressor AUS (cycle=42)" in m for m in meldungen)
    assert any("reason=regel_aus" in m for m in meldungen)


def test_zyklus_id_robust():
    assert pcl._zyklus_id(baue_off_state()) == "42"
    assert pcl._zyklus_id(SimpleNamespace(control=SimpleNamespace())) == "?"


# ---------------- 3) BOILERMAX-Kontext ---------------- #
def test_boiler_max_kontext_format():
    state = SimpleNamespace(
        control=SimpleNamespace(_lauf_start_regel="Einspeisung",
                                active_rule_name=None),
        solar=SimpleNamespace(feedinpower=7563.0, soc=90.0),
        learning_engine=SimpleNamespace(
            data=SimpleNamespace(cycles=[{"rate_unten_c_h": 19.59}])),
    )
    txt = pcl._boiler_max_kontext(state)
    assert "Regel=Einspeisung" in txt
    assert "PV=7563W" in txt
    assert "SOC=90%" in txt
    assert "Rate(letzter Lauf)=19.6C/h" in txt


def test_boiler_max_kontext_robust_gegen_mocks():
    txt = pcl._boiler_max_kontext(SimpleNamespace(control=SimpleNamespace()))
    assert "Regel=unbekannt" in txt
    assert "PV=n/a" in txt
    assert "SOC=n/a" in txt


# ---------------- 4) Status-Zeile mit Kontext ---------------- #
@pytest.mark.asyncio
async def test_statuszeile_enthaelt_pv_einspeis_soc_alter(monkeypatch, caplog,
                                                           tmp_path):
    import main

    state = SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(t_oben=48.0, t_mittig=43.0, t_unten=46.0,
                                t_verd=12.0),
        control=SimpleNamespace(
            kompressor_ein=False,
            aktueller_einschaltpunkt=42.0, aktueller_ausschaltpunkt=48.0,
            blocking_reason=None, active_rule_name="Einspeisung",
            previous_modus="Einspeisung"),
        solar=SimpleNamespace(
            acpower=5000.0, feedinpower=4000.0, soc=80.0,
            last_api_call=datetime.now(TZ) - timedelta(seconds=30)),
    )
    monkeypatch.setattr(main, "check_log_throttle", lambda *a, **k: True)
    monkeypatch.setattr(main, "hardware_manager",
                        SimpleNamespace(write_lcd=lambda *a, **k: None))
    monkeypatch.setattr(main, "HEIZUNGSDATEN_CSV", str(tmp_path / "heiz.csv"))
    monkeypatch.setattr(main, "build_heizungsdaten_zeile", lambda s: ["1", "2"])
    monkeypatch.setattr(main, "write_last_state_snapshot", lambda s: None)

    with caplog.at_level(logging.INFO):
        await main.log_system_state(state)

    meldungen = [r.message for r in caplog.records if r.message.startswith("Status:")]
    assert meldungen
    zeile = meldungen[0]
    assert "PV=5000W" in zeile
    assert "Einspeis=4000W" in zeile
    assert "SOC=80%" in zeile
    assert "Alter=30s" in zeile
    assert "Regel: Einspeisung" in zeile


# ---------------- 5) Sensor-Degradation ---------------- #
def test_sensor_degradation_warnt_auf_logarithmischen_schwellen(caplog):
    from sensors import SensorManager

    sm = SensorManager()
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            sm._track_sensor_error("28-abc")
    warns = [r.message for r in caplog.records if "Sensor-Degradation" in r.message]
    # Warn-Schwellen bei 1, 2, 4 (Zweierpotenzen)
    assert len(warns) == 3
    assert "28-abc" in warns[0]