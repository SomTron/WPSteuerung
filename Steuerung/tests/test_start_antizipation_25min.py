# -*- coding: utf-8 -*-
"""Regression Oktober 2026: Die Start-Antizipation braucht einen 25-Minuten-Hub.

Befund (Logs 05./12.10.2026): Fuer die Start-ERLAUBNIS rechnete
`handle_compressor_on` gegen `pv_min_laufzeit_minuten` (10 min). Bei ~20 C/h
Heizrate reichten schon ~3,5 K Hub - der Start wurde freigegeben und 9-14 min
spaeter vom Boiler-Maximum (48 C) abgewuergt, gefolgt von 30 min Pause und
demselben Spiel (32 boiler_max-Abbrueche).

Loesung: Fuer die Start-Erlaubnis gilt eine eigene Mindestlaufzeit
(`sicherheit.start_vorhersage_mindestlaufzeit_min`, Default 25 min). Netzlauefe
sind davon nicht betroffen, weil ihre echte Mindestlaufzeit (60 min) bereits
darueber liegt - die Aenderung wirkt real nur auf PV-Laeufe.
"""
import os
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control_logic as pcl  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def _state(rate_c_h, t_unten):
    """Baut einen minimalen Aus-Zustand mit frischer Heizraten-Messung."""
    now = datetime.now(TZ)
    cfg = WPSteuerungConfig()
    # Nachtabschaltung ausschalten: Der Test darf nicht von der Wanduhrzeit
    # abhaengen (sonst waere er zwischen 21:30 und 22:00 flaky).
    cfg.notfallschutz.bis_uhr = 0
    # Rate als frische Messung: delta = rate * 0.1 h.
    mess = {"ts": now - timedelta(minutes=6), "unten": t_unten - rate_c_h * 0.1}
    return SimpleNamespace(
        local_tz=TZ,
        sensors=SimpleNamespace(
            t_unten=t_unten, t_mittig=30.0, t_oben=45.0, t_verd=15.0
        ),
        priority_config=cfg,
        control=SimpleNamespace(
            kompressor_ein=False,
            blocking_reason=None,
            _soll_einschalten=True,
            restart_lockout_until=None,
            requested_rule_name="AdaptivePV",
            active_rule_name=None,
            previous_modus="AdaptivePV",
            _pending_start_rule=None,
            _pending_start_source=None,
            _rate_messung=mess,
            _rate_messungen=deque(maxlen=5),
            _rate_confidence=0.0,
            schichtung_oben_max=None,
            schichtung_oben_start=None,
            boiler_max_blockiert=None,
            notfall_aktiv=False,
        ),
        legionellen_aktiv=False,
        legionellen_temp_override=None,
        stats=SimpleNamespace(
            last_compressor_on_time=now,
            last_compressor_off_time=now - timedelta(hours=2),
        ),
    )


async def _starte(state, regelname="AdaptivePV"):
    async def set_status(st, ein, **kw):
        st.control.kompressor_ein = ein
        return True

    return await pcl.handle_compressor_on(
        state, None,
        regelfuehler=state.sensors.t_unten, einschaltpunkt=43.0,
        ausschaltpunkt=48.0, min_laufzeit=timedelta(minutes=60),
        min_pause=timedelta(minutes=0),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=set_status, regel_name=regelname,
    )


@pytest.mark.asyncio
async def test_ohne_startwunsch_kein_antizipations_grund(caplog):
    """Befund 18.10.2026: Die Start-Antizipation setzte ihren Sperrgrund auch
    dann, wenn gar keine Regel heizen wollte (Gewinner sagt AUS) - im
    Betriebslog standen 51 solcher "Info: Start-Antizipation"-Zeilen, waehrend
    der echte Grund (AdaptivePV: "Speicher oben voll") fehlte."""
    import logging as _logging

    state = _state(rate_c_h=20.0, t_unten=45.9)   # Hub 2,1 K -> sonst blockiert
    state.control._soll_einschalten = False       # keine Regel will heizen

    with caplog.at_level(_logging.INFO):
        ergebnis = await _starte(state, "AdaptivePV")

    assert ergebnis is False
    assert state.control.kompressor_ein is False
    assert "Start-Antizipation" not in (state.control.blocking_reason or "")
    assert state.control.blocking_reason is None
    assert "Start blockiert reason=start_vorhersage" not in caplog.text


@pytest.mark.asyncio
async def test_pv_start_mit_kurzem_hub_wird_blockiert():
    """unten 45,9 C -> Hub 2,1 K; bei 20 C/h nur ~6 min.

    Nach der alten Logik (10 min) war der Start erlaubt und riss sofort am
    Boiler-Maximum ab. Jetzt (25 min) wird er verweigert.
    """
    state = _state(rate_c_h=20.0, t_unten=45.9)
    ergebnis = await _starte(state, "AdaptivePV")
    assert ergebnis is False
    assert state.control.kompressor_ein is False
    assert "Start-Antizipation" in (state.control.blocking_reason or "")
    # Der erwartete Hub lag deutlich unter der 25-Minuten-Messlatte.
    vorhersage = state.control._last_start_anticipation
    assert vorhersage["effective_min"] == 25.0


@pytest.mark.asyncio
async def test_pv_start_mit_langem_hub_wird_erlaubt():
    """unten 40,0 C -> Hub 8,0 K; bei 20 C/h = 24 min + 1 min Reserve = 25 min."""
    state = _state(rate_c_h=20.0, t_unten=40.0)
    ergebnis = await _starte(state, "AdaptivePV")
    assert ergebnis is True
    assert state.control.kompressor_ein is True


# --- Ausnahme fuer die Mindest-Temperatur-Garantien (20.10.2026) ---
#
# Nutzerentscheidung: "der Abendkomfort geht vor". Die Antizipation
# blockierte die Garantie-Regeln immer: bei ~4,3 K/h Heizrate und 1,0 K
# freiem Hub rechnet sie 15 min gegen 60 min Mindestlaufzeit.
# Simulation (Analyse/sim, 14 Tage Dezember): das Abendminimum (20-22 Uhr)
# stieg dadurch von 36,6 C auf 41,9 C.
#
# Die Ausnahme gilt NUR fuer die Garantie-Regeln - die PV-Regeln bleiben
# geschuetzt, sonst wuerde die Einspeisung nachts starten.


@pytest.mark.asyncio
async def test_garantie_regel_ist_von_der_antizipation_ausgenommen():
    """Die Mindest-Temperatur-Garantie startet auch bei kurzem Hub."""
    state = _state(rate_c_h=4.3, t_unten=47.0)   # Hub 1,0 K -> 14 min
    state.priority_config.sicherheit.start_vorhersage_ausnahme_regeln = [
        "MinTemp-Abend-Mitte",
    ]
    ergebnis = await _starte(state, "MinTemp-Abend-Mitte")
    assert ergebnis is True
    assert state.control.kompressor_ein is True
    assert "Start-Antizipation" not in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_pv_regel_bleibt_von_der_antizipation_gesperrt():
    """Die Ausnahme darf NICHT auf die PV-Regeln durchschlagen."""
    state = _state(rate_c_h=20.0, t_unten=45.9)   # Hub 2,1 K -> blockiert
    state.priority_config.sicherheit.start_vorhersage_ausnahme_regeln = [
        "MinTemp-Abend-Mitte",
    ]
    ergebnis = await _starte(state, "AdaptivePV")
    assert ergebnis is False
    assert "Start-Antizipation" in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_ohne_ausnahme_gilt_die_antizipation_auch_fuer_die_garantie():
    """Ohne Ausnahmeliste bleibt das Verhalten unveraendert."""
    state = _state(rate_c_h=4.3, t_unten=47.0)
    ergebnis = await _starte(state, "MinTemp-Abend-Mitte")
    assert ergebnis is False
    assert "Start-Antizipation" in (state.control.blocking_reason or "")


def test_projektkonfiguration_entscheidet_abendkomfort_vor():
    """Die Projektkonfiguration muss die Ausnahme aktiviert haben."""
    import json

    pfad = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "wp_steuerung_parameter.json")
    with open(pfad, "r", encoding="utf-8") as f:
        daten = json.load(f)
    ausnahme = daten["sicherheit"].get("start_vorhersage_ausnahme_regeln", [])
    assert "MinTemp-Abend-Mitte" in ausnahme, (
        "Die Abendgarantie braucht die Ausnahme von der Start-Antizipation "
        "(Nutzerentscheidung 20.10.2026)")
