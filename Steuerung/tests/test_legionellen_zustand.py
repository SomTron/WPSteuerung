# -*- coding: utf-8 -*-
"""Regression 31.10.2026: Prophylaxe startete nach einem Neustart erneut.

Betriebslog:

  * 30.10.2026, 14:47 - Ziel 60 C unten erreicht, Halteprobe, Abschluss
    (`legionellen_aktiv=false`, `legionellen_unvollstaendig=false`).
  * 31.10.2026, 01:49 - Neustart der Steuerung (`rev` e9e60d1 -> 67adf23).
  * 31.10.2026, 11:00 - erneut "Legionellenprophylaxe faellig" - dieselbe
    Kalenderwoche (KW 44) ein zweites Mal aufgeheizt.

Ursache: `legionellen_last_done` war reiner RAM-Zustand; das Wochen-Gate in
`evaluate_legionellen` verglich nach dem Neustart gegen `None`. Der Abschluss
wird jetzt in `legionellen_zustand.json` persistiert und beim Start geladen.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytz

import legionellen_zustand
from json_config import LegionellenConfig
from priority_control import evaluate_legionellen

TZ = pytz.timezone("Europe/Berlin")

#: Freitag/Samstag 2026 liegen in KW 44 - genau der Fall aus dem Log.
ABSCHLUSS_TAG = date(2026, 10, 30)                     # Freitag, KW 44
FOLGETAG = datetime(2026, 10, 31, 11, 0, tzinfo=TZ)    # Samstag, KW 44
NAECHSTE_KW = datetime(2026, 11, 6, 11, 0, tzinfo=TZ)  # Freitag, KW 45

TEMPS = {"oben": 55.7, "mittig": 40.2, "unten": 33.0}


def _cfg() -> LegionellenConfig:
    return LegionellenConfig(
        aktiv=True,
        target_temp_c=60.0,
        legionellen_max_temp_c=65.0,
        probezeit_minuten=10,
        bevorzugter_tag=4,   # Freitag
        letzter_tag=6,       # Sonntag
        start_uhr=10.0,
        pv_prognose_schwelle_gut=2000.0,
        erforderliche_wh_qm=500.0,
    )


def _leerer_state():
    """Frischer State wie nach einem Service-Neustart."""
    return SimpleNamespace(
        local_tz=TZ,
        legionellen_last_done=None,
        legionellen_wochennummer=None,
        legionellen_end_time=None,
    )


def _beende_state():
    """Minimaler State fuer `_complete_legionellen_lifecycle`."""
    return SimpleNamespace(
        local_tz=TZ,
        legionellen_last_done=None,
        legionellen_wochennummer=None,
        legionellen_end_time=None,
        legionellen_aktiv=True,
        legionellen_temp_override=65.0,
        legionellen_started_at=FOLGETAG - timedelta(hours=3),
        legionellen_target_reached_at=FOLGETAG,
        legionellen_unvollstaendig=True,
        control=SimpleNamespace(
            requested_rule_name=None,
            effective_rule_name="Legionellen",
            active_rule_name="Legionellen",
            effective_source="PV",
            source_at_start="PV",
            source_current="PV",
            _lauf_start_regel="Legionellen",
            _pending_start_rule=None,
            _pending_start_source=None,
        ),
        priority_config=SimpleNamespace(
            legionellen=SimpleNamespace(
                aktiv=True,
                target_temp_c=60.0,
                legionellen_max_temp_c=65.0,
                probezeit_minuten=10,
                max_duration_hours=0,
            )
        ),
        solar=SimpleNamespace(acpower=800.0, batpower=0.0),
        sensors=SimpleNamespace(t_unten=60.0),
        _last_was_legionellen=False,
        _legionellen_completion_pending=True,
    )


def test_abschluss_ueberlebt_den_neustart():
    """Datum, KW und Endzeit stehen nach dem Neustart wieder im State."""
    vorher = _leerer_state()
    vorher.legionellen_last_done = ABSCHLUSS_TAG
    vorher.legionellen_wochennummer = ABSCHLUSS_TAG.isocalendar()[1]
    vorher.legionellen_end_time = FOLGETAG - timedelta(hours=12)
    assert legionellen_zustand.save_abschluss(vorher) is True

    nachher = _leerer_state()
    assert legionellen_zustand.load_abschluss(nachher) is True
    assert nachher.legionellen_last_done == ABSCHLUSS_TAG
    assert nachher.legionellen_wochennummer == 44
    assert nachher.legionellen_end_time == vorher.legionellen_end_time


def test_folgetag_startet_nicht_erneut_in_derselben_kw():
    """Der eigentliche Logfall: Am Samstag muss das Wochen-Gate greifen."""
    vorher = _leerer_state()
    vorher.legionellen_last_done = ABSCHLUSS_TAG
    vorher.legionellen_wochennummer = 44
    assert legionellen_zustand.save_abschluss(vorher) is True

    neustart = _leerer_state()
    assert legionellen_zustand.load_abschluss(neustart) is True

    erg = evaluate_legionellen(
        _cfg(), dict(TEMPS), FOLGETAG,
        legionellen_last_done=neustart.legionellen_last_done,
        legionellen_unvollstaendig=False,
        pv_leistung=800.0, pv_acpower=800.0,
    )
    assert erg.aktiv is False, erg.grund
    assert erg.einschalten is None
    assert "Bereits in KW 44" in erg.grund, erg.grund


def test_ohne_persistenz_ist_die_prophylaxe_faellig():
    """Gegenprobe: Ohne gespeicherten Abschluss (Altverhalten) startet sie."""
    erg = evaluate_legionellen(
        _cfg(), dict(TEMPS), FOLGETAG,
        legionellen_last_done=None,
        legionellen_unvollstaendig=False,
        pv_leistung=800.0, pv_acpower=800.0,
    )
    assert "Bereits in KW" not in (erg.grund or "")


def test_neue_kalenderwoche_blockiert_nicht():
    """In der Folgewoche ist das Gate offen - der Abschluss darf nicht haengen."""
    vorher = _leerer_state()
    vorher.legionellen_last_done = ABSCHLUSS_TAG
    vorher.legionellen_wochennummer = 44
    assert legionellen_zustand.save_abschluss(vorher) is True

    neustart = _leerer_state()
    assert legionellen_zustand.load_abschluss(neustart) is True

    erg = evaluate_legionellen(
        _cfg(), dict(TEMPS), NAECHSTE_KW,
        legionellen_last_done=neustart.legionellen_last_done,
        legionellen_unvollstaendig=False,
        pv_leistung=800.0, pv_acpower=800.0,
    )
    assert "Bereits in KW" not in (erg.grund or "")


def test_kaputte_datei_wird_verworfen():
    """Unlesbares JSON darf den Start nicht stoppen und wird entfernt."""
    with open(legionellen_zustand.ZUSTAND_FILE, "w", encoding="utf-8") as fh:
        fh.write("{kein json")

    zustand = _leerer_state()
    assert legionellen_zustand.load_abschluss(zustand) is False
    assert zustand.legionellen_last_done is None


def test_zukunftsdatum_wird_verworfen():
    """Eine verdrehte Uhr darf das Gate nicht dauerhaft schliessen."""
    with open(legionellen_zustand.ZUSTAND_FILE, "w", encoding="utf-8") as fh:
        fh.write('{"schema_version": 1, "legionellen_last_done": "2099-01-01"}')

    zustand = _leerer_state()
    assert legionellen_zustand.load_abschluss(zustand) is False
    assert zustand.legionellen_last_done is None


def test_abschluss_ohne_datum_entfernt_die_datei():
    """`save_abschluss` ohne `last_done` (kein Lauf) raeumt eine Altdatei weg."""
    vorher = _leerer_state()
    vorher.legionellen_last_done = ABSCHLUSS_TAG
    assert legionellen_zustand.save_abschluss(vorher) is True

    leer = _leerer_state()
    assert legionellen_zustand.save_abschluss(leer) is False
    assert legionellen_zustand.load_abschluss(_leerer_state()) is False


def test_lifecycle_abschluss_wird_persistiert():
    """`_complete_legionellen_lifecycle` muss den Abschluss schreiben."""
    from legionellen_lifecycle import _complete_legionellen_lifecycle

    state = _beende_state()
    _complete_legionellen_lifecycle(state, FOLGETAG)

    assert state.legionellen_last_done == FOLGETAG.date()
    wieder = _leerer_state()
    assert legionellen_zustand.load_abschluss(wieder) is True
    assert wieder.legionellen_last_done == FOLGETAG.date()
    assert wieder.legionellen_wochennummer == 44
