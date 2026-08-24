"""Tests: Hartes Boiler-Maximum (bricht die Mindestlaufzeit).

Anforderung (2026-08-24): "der Boiler soll nicht mehr als 48 Grad haben.
wenn er oben 48,7 hat unten aber nur noch 30 kann er schon einschalten
aber nicht wenn er unten 48,8 Grad hat."

Vorher: Unten 48.8 >= Limit 48 -> alle Regeln sagten AUS, aber die
Mindestlaufzeit hielt den Kompressor noch ~10 min fest und heizte oben
auf 51.7 C weiter. Neu: Der BoilerMax-Abschalter bricht die Laufzeit.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import pytz  # noqa: E402

import priority_control_logic as pcl  # noqa: E402
from json_config import SicherheitConfig, WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def baue_state(t_unten, t_oben=51.7, t_mittig=50.0, config=None):
    cfg = config or WPSteuerungConfig()
    now = datetime.now(TZ)
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(t_unten=t_unten, t_mittig=t_mittig,
                                t_oben=t_oben, t_verd=12.0),
        priority_config=cfg,
        control=SimpleNamespace(
            kompressor_ein=True,
            blocking_reason=None,
            _soll_einschalten=False,
            restart_lockout_until=None,
        ),
        stats=SimpleNamespace(
            last_compressor_on_time=now - timedelta(minutes=2),
            last_compressor_off_time=now - timedelta(hours=2),
        ),
    )


def _set_status_sammler(calls):
    async def set_status(state, ein, **kwargs):
        calls.append((ein, kwargs))
        return True
    return set_status


# ---------- handle_compressor_off ----------

@pytest.mark.asyncio
async def test_off_bricht_mindestlaufzeit_beim_limit():
    """unten 48.8 >= 48 -> sofort AUS trotz 2 min statt 15 min Laufzeit."""
    state = baue_state(t_unten=48.8)
    calls = []
    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=48.8, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="Einspeisung",
    )
    assert erg is True
    assert calls and calls[0][0] is False
    assert "Boiler-Maximum" in (state.control.blocking_reason or "")
    # Kuehlphase aktiviert: Freigabe erst bei limit - hysterese
    assert state.control.boiler_max_blockiert == pytest.approx(46.0)


@pytest.mark.asyncio
async def test_off_wartet_weiterhin_unterhalb_des_limits(monkeypatch):
    """unten 47.5 < 48 -> altes Verhalten: Mindestlaufzeit abwarten."""
    state = baue_state(t_unten=47.5)
    monkeypatch.setattr(pcl, "check_log_throttle", lambda *a, **k: True)
    calls = []
    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=47.5, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="Einspeisung",
    )
    assert erg is False
    assert calls == []  # nicht ausgeschaltet
    assert "Boiler-Maximum" not in (state.control.blocking_reason or "")
    assert "Mindestlaufzeit" in (state.control.blocking_reason or "")
    assert getattr(state.control, "boiler_max_blockiert", None) is None


@pytest.mark.asyncio
async def test_off_gilt_auch_wenn_eine_regel_ein_will():
    """Sicherheit schlaegt Regel: unten am Limit -> aus, egal was die Regel sagt."""
    state = baue_state(t_unten=48.3)
    state.control._soll_einschalten = True  # Regel will eigentlich heizen
    calls = []
    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=44.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="PV_unten",
    )
    assert erg is True
    assert calls and calls[0][0] is False


@pytest.mark.asyncio
async def test_fuehler_ohne_attribut_faehrt_ohne_boilermax(monkeypatch):
    """Fehlender Bezugsfuehler -> kein BoilerMax-Eingriff (Rueckwaertskompat.)."""
    state = baue_state(t_unten=49.9)
    del state.sensors.t_unten  # simuliert fehlenden Sensor
    monkeypatch.setattr(pcl, "check_log_throttle", lambda *a, **k: True)
    calls = []
    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=None, ausschaltpunkt=None,
        min_laufzeit=timedelta(minutes=15), t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="X",
    )
    assert erg is False
    assert calls == []


# ---------- handle_compressor_on ----------

@pytest.mark.asyncio
async def test_on_blockiert_in_der_kuehlphase():
    """Nach Limit-Abschalten: Einschalten erst wieder <= 46 C."""
    state = baue_state(t_unten=47.9)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control.boiler_max_blockiert = 46.0
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.0, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler([]),
    )
    assert erg is False
    assert "Kuehlphase" in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_on_freigabe_nach_abkuehlung_hebt_flag_auf():
    """unten unter der Kuehlschwelle -> Flag wird entfernt, EIN moeglich."""
    state = baue_state(t_unten=45.5)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control.boiler_max_blockiert = 46.0
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.0, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler([]),
    )
    assert erg is True
    assert state.control.boiler_max_blockiert is None


@pytest.mark.asyncio
async def test_userfall_oben_warm_unten_kalt_darf_einschalten():
    """User-Fall: oben 48.7 / unten 30 -> EIN erlaubt (keine pauschale Sperre)."""
    state = baue_state(t_unten=30.0, t_oben=48.7)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=30.0, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is True
    assert calls and calls[0][0] is True


@pytest.mark.asyncio
async def test_on_ohne_flag_keine_sperre_trotz_hoher_temp():
    """Kein Limit-Ereignis passiert -> EIN bei unten 47 bleibt unangetastet."""
    state = baue_state(t_unten=47.0)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.0, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is True


# ---------- Konfiguration ----------

def test_default_werte_matchen_anforderung():
    cfg = WPSteuerungConfig()
    s = cfg.sicherheit
    assert s.max_temp_c == 48.0
    assert s.boiler_max_fuehler == "unten"
    assert s.boiler_max_hysterese_k == 2.0


def test_validatoren():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SicherheitConfig(boiler_max_fuehler="seitlich")
    with pytest.raises(ValidationError):
        SicherheitConfig(boiler_max_hysterese_k=-1.0)
    with pytest.raises(ValidationError):
        SicherheitConfig(max_temp_c=60.0, ueberhitzung_c=55.0)
    with pytest.raises(ValidationError):
        SicherheitConfig(max_temp_c=10.0)
