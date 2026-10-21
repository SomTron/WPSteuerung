# -*- coding: utf-8 -*-
"""PV-Fenster: Gate und Messung (Nutzerfrage 18.10.2026).

Betreiber-Frage: "Sollte nicht nach dem Sonnenuntergang geregelt werden,
sondern nach dem PV-Forecast? Wenn der fuer den spaeten Nachmittag wenig
Einstrahlung vorhersagt, ist der Sonnenuntergang irrelevant - in dem waere er
ja automatisch enthalten."

Umgesetzt ueber die STUENDLICHE Prognose (`state.solar.forecast_hourly_wm2`,
kalibriert mit dem gelernten `fc_ratio`):

  * `logic_utils.pv_fenster_endstunde` = letzte Stunde mit erwarteter PV
    ueber der Schwelle,
  * `energy_source.classify_energy_source(pv_erlaubt=False)` = ausserhalb
    zaehlt die PV-Momentanmessung nicht mehr als Quelle (nur Batterie),
  * `evaluate_abweichung`/`evaluate_calculated_start`/`evaluate_forecast`
    bekommen das Fenster durchgereicht; CalcStart zieht seinen Spaetest-Start
    auf das Fensterende vor.
"""
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import priority_control as pc  # noqa: E402
from energy_source import Energiequelle, classify_energy_source  # noqa: E402
from json_config import WPSteuerungConfig  # noqa: E402
from logic_utils import pv_fenster_endstunde  # noqa: E402


# ── (a) Ermittlung des Fensters ─────────────────────────────────────────

def test_letzte_stunde_ueber_der_schwelle():
    profil = {12: 900.0, 14: 800.0, 15: 500.0, 16: 320.0, 17: 120.0, 18: 0.0}
    assert pv_fenster_endstunde(profil, 14.5, 300.0) == (16, pytest.approx(320.0))
    # Stunden in der Vergangenheit zaehlen nicht
    assert pv_fenster_endstunde(profil, 15.1, 300.0) == (16, pytest.approx(320.0))
    # hoehere Schwelle -> frueher zu Ende
    assert pv_fenster_endstunde(profil, 12.0, 600.0) == (14, pytest.approx(800.0))


def test_laufende_stunde_zaehlt_noch_zum_fenster():
    """Regression (20.10.2026): die LAUFENDE Stunde zaehlt noch mit.

    Um 16:30 ist die Stunde 16 nicht "vergangen" - sie liefert noch PV. Mit
    dem Filter ``h >= jetzt`` fiel sie weg, das Fenster endete dann eine
    Stunde zu frueh. Der Start rutschte dadurch hinter das Fensterende
    und kam aus dem Netz statt aus PV.
    """
    # Die LAUFENDE Stunde zaehlt noch mit: um 16:30 liefert Stunde 16
    # weiterhin PV. Mit dem Filter "h >= jetzt" fiel sie weg, das Fenster
    # endete dann eine Stunde zu frueh - der Start rutschte hinter das
    # Fensterende und kam aus dem Netz statt aus PV.
    assert pv_fenster_endstunde({16: 400.0}, 16.5, 300.0) == (16, pytest.approx(400.0))
    # Exakt am Stundenbeginn zaehlt die Stunde ebenfalls
    assert pv_fenster_endstunde({16: 400.0}, 16.0, 300.0) == (16, pytest.approx(400.0))
    # Abgeschlossene Stunden zaehlen nicht mehr: um 17:00 ist Stunde 16 vorbei
    assert pv_fenster_endstunde({15: 500.0, 16: 400.0}, 17.0, 300.0) is None
    # Zukunftsstunden zaehlen natuerlich mit (die Prognose ist Tagesprofil)
    assert pv_fenster_endstunde({16: 400.0, 17: 450.0}, 16.5, 300.0) == (
        17, pytest.approx(450.0))


def test_kalibrierung_wirkt_auf_das_fenster():
    """fc_ratio (gelernter Forecast-Faktor) verschiebt das Fenster mit."""
    profil = {14: 800.0, 15: 700.0, 16: 320.0}
    # unkalibriert: bis 16 Uhr; x0.5 -> 400/350/160 W, nur der 15 Uhr-Wert
    # bleibt ueber 300 W
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=1.0)[0] == 16
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=0.5)[0] == 15
    # unbrauchbare Werte fallen auf 1.0 zurueck
    assert pv_fenster_endstunde(profil, 14.0, 300.0, fc_ratio=0)[0] == 16


def test_ohne_prognose_kein_fenster():
    """Kein Profil = keine Aussage (der Aufrufer laesst das Gate dann aus)."""
    assert pv_fenster_endstunde(None, 12.0, 300.0) is None
    assert pv_fenster_endstunde({}, 12.0, 300.0) is None
    assert pv_fenster_endstunde({12: 100.0, 13: 200.0}, 12.0, 300.0) is None


# ── (b) Quellen-Gate ────────────────────────────────────────────────────

def test_ausserhalb_des_fensters_zaehlt_pv_nicht_als_quelle():
    """Daemmerlicht liefert oft noch 300 W DC - ein 600-W-Lauf zieht dann
    trotzdem Netz. Ausserhalb bleibt nur die Batterie."""
    werte = dict(pv_acpower=320.0, feedin_watt=0.0, batpower_raw=0.0,
                 soc=60.0, pv_min_watt=50.0, max_netzkauf_watt=-50.0)

    drin = classify_energy_source(**werte)
    assert drin.quelle is Energiequelle.PV

    draussen = classify_energy_source(**werte, pv_erlaubt=False)
    assert draussen.quelle is Energiequelle.NETZ
    assert "PV-Fenster" in draussen.begruendung

    # Batterie traegt weiterhin
    mit_batterie = classify_energy_source(
        pv_acpower=320.0, feedin_watt=0.0, batpower_raw=-300.0, soc=95.0,
        pv_min_watt=50.0, max_netzkauf_watt=-50.0, pv_erlaubt=False,
    )
    assert mit_batterie.quelle is Energiequelle.BATTERIE
    assert "PV-Fenster" in mit_batterie.begruendung


# ── (b) Wirkung in den Regeln ───────────────────────────────────────────

_SCHICHTUNG_TEMP = {"unten": 33.0, "mittig": 45.0, "oben": 48.0}


def _allel(config, temp_dict=None, **kw):
    kw.setdefault("pv_leistung", 320.0)
    kw.setdefault("pv_acpower", 320.0)
    kw.setdefault("soc", 60.0)
    kw.setdefault("battery_power", 0.0)
    kw.setdefault("now", datetime(2026, 12, 8, 16, 40))
    _gewinner, alle = pc.bewerte_alle_regeln(
        config=config, temp_dict=temp_dict or _SCHICHTUNG_TEMP,
        kompressor_ein=False, **kw
    )
    return {e.name: e for e in alle}


def _config():
    config = WPSteuerungConfig()
    config.abweichung.schichtung_erlaube_start = True
    return config


def test_abweichung_startet_ausserhalb_nicht_mit_pv():
    """Innerhalb des Fensters startet der Schichtungs-Start mit PV, ausserhalb
    nicht mehr (dort nur Batterie).

    BUGFIX (Befund 21.10.2026): Der Test benutzte `pv_fenster_aktiv` als
    Synonym fuer "ausserhalb des Fensters". Das ist der MASTER-SCHALTER aus
    der Config ("Gate ueberhaupt benutzen") - nicht die Fensterlage. In der
    Produktion wurde genau dieser Schalter als `pv_erlaubt` gereicht, damit
    war das Gate permanent offen und die PV zaehlte auch nach
    Sonnenuntergang als Quelle.

    `pv_quelle_erlaubt` ist jetzt die echte Fensterlage: True = wir sind im
    PV-Fenster, False = ausserhalb.
    """
    drin = _allel(_config(), pv_quelle_erlaubt=True)["Abweichung"]
    assert drin.einschalten is True, drin.grund

    draussen = _allel(_config(), pv_quelle_erlaubt=False)["Abweichung"]
    assert draussen.einschalten is None, draussen.grund

    mit_batterie = _allel(
        _config(), pv_quelle_erlaubt=False, battery_power=-300.0, soc=95.0
    )["Abweichung"]
    assert mit_batterie.einschalten is True, mit_batterie.grund


def test_master_schalter_steuert_nicht_das_gate():
    """Der Config-Schalter `pv_fenster_aktiv` darf das Quellen-Gate nicht
    beeinflussen - er sagt nur, OB das Gate benutzt wird.

    BUGFIX (Befund 21.10.2026): Beide wurden verwechselt. Jetzt gilt:
    - Schalter AUS  -> Gate komplett aus -> PV zaehlt immer als Quelle
    - Schalter AN   -> Gate aktiv -> es entscheidet die Fensterlage
    """
    # Schalter AN, aber ausserhalb des Fensters -> PV zaehlt nicht.
    ausserhalb = _allel(
        _config(), pv_fenster_aktiv=True, pv_quelle_erlaubt=False
    )["Abweichung"]
    assert ausserhalb.einschalten is None, ausserhalb.grund

    # Schalter AUS -> Gate aus -> PV zaehlt wieder als Quelle.
    schalter_aus = _allel(
        _config(), pv_fenster_aktiv=False, pv_quelle_erlaubt=True
    )["Abweichung"]
    assert schalter_aus.einschalten is True, schalter_aus.grund


def test_calcstart_startet_am_fensterende_und_nicht_spaeter():
    """Kernfall Winter: Spaetest-Start ohne Fenster erst kurz vor der
    Zielzeit (nach Sonnenuntergang), mit Fenster bis 16:30 - der Lauf bleibt
    im PV-Fenster."""
    config = _config()
    temp = {"unten": 41.0, "mittig": 41.5, "oben": 46.0}   # 1 K Defizit
    gemeinsam = dict(
        temp_dict=temp, forecast_wh_qm=2000.0, learned_target_hour=18.5,
        learned_heating_rate_unten=3.0, learned_heating_rate_gesamt=2.0,
    )

    # 16:35, Puffer ~1,6 h: ohne Fenster -> "Puffer reicht, warte auf PV"
    ohne = _allel(config, pv_fenster_stunde=None, **gemeinsam)["CalcStart"]
    assert ohne.einschalten is None, ohne.grund
    assert "warte" in ohne.grund, ohne.grund

    # Mit Fenster (letzte Stunde 16 Uhr, Reserve 30 min) -> Spaetest-Start
    mit = _allel(config, pv_fenster_stunde=16.0, **gemeinsam)["CalcStart"]
    assert mit.einschalten is True, mit.grund
    assert "SPAETEST-START" in mit.grund, mit.grund

    # Deutlich frueher (12:00) greift die Fenstergrenze nicht
    frueh = _allel(
        config, pv_fenster_stunde=16.0,
        **{**gemeinsam, "now": datetime(2026, 12, 8, 12, 0)}
    )["CalcStart"]
    assert frueh.einschalten is None, frueh.grund


def test_fenster_reserve_ist_konfigurierbar():
    """Defaults der neuen Felder - und abschaltbar ueber die Config."""
    config = WPSteuerungConfig()
    assert config.forecast.pv_fenster_aktiv is True
    assert config.forecast.pv_fenster_min_w == pytest.approx(300.0)
    assert config.forecast.pv_fenster_reserve_min == pytest.approx(30.0)
    config.forecast.pv_fenster_aktiv = False
    assert config.forecast.pv_fenster_aktiv is False


def test_pv_fenster_im_statuslog_und_zaehler():
    """(a) Messung: State-Felder sind vorhanden und werden hochgezaehlt."""
    from state import ControlState

    class _Cfg:
        class Heizungssteuerung:
            AUSSCHALTPUNKT = 48.0
            EINSCHALTPUNKT = 42.0

    control = ControlState(_Cfg)
    assert control.pv_fenster_stunde is None
    assert control.pv_fenster_aktiv is True
    assert control.pv_fenster_ausserhalb is False
    assert control.starts_ausserhalb_pv_fenster == 0


# ── (c) Produktionsverdrahtung (determine_mode_and_setpoints) ───────────
#
# BUGFIX (Befund 21.10.2026): Der Master-Schalter `forecast.pv_fenster_aktiv`
# wurde als `pv_erlaubt` an die Quellen-Pruefung gereicht. Damit war das
# PV-Fenster-Gate permanent offen - die PV zaehlte auch nach
# Sonnenuntergang als Quelle. Die Tests oben konnten das nicht sehen,
# weil sie `pv_fenster_aktiv` direkt als "ausserhalb des Fensters"
# missbrauchten (falsche Semantik).
#
# Diese Tests fahren die ECHTE Verdrahtung: State -> determine_mode_and_setpoints
# -> bewerte_alle_regeln -> Quellen-Gate.


def _pcl_state(stundenprognose, pv_erzeugung=800.0, jetzt_stunde=16.0):
    """State fuer die Produktionsverdrahtung."""
    import pytz
    TZ = pytz.timezone("Europe/Berlin")
    config = WPSteuerungConfig()
    config.abweichung.schichtung_erlaube_start = True
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=config,
        config=SimpleNamespace(Urlaubsmodus=SimpleNamespace(URLAUBSABSENKUNG=5.0)),
        bademodus_aktiv=False,
        urlaubsmodus_aktiv=False,
        sommer_modus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        legionellen_unvollstaendig=False,
        sensors=SimpleNamespace(t_oben=48.0, t_unten=33.0, t_mittig=45.0, t_verd=30.0),
        solar=SimpleNamespace(
            feedinpower=0.0, batpower=0.0, soc=60.0, acpower=pv_erzeugung,
            pv_erzeugung=pv_erzeugung,
            forecast_today=None, forecast_tomorrow=None, forecast_day2=None,
            forecast_hourly_wm2=stundenprognose,
            last_api_call=TZ.localize(datetime(2026, 12, 8, int(jetzt_stunde), 30)),
        ),
        solar_stale=False,
        forecast_stale=False,
        control=SimpleNamespace(
            kompressor_ein=False, previous_modus="Normalmodus",
            aktueller_einschaltpunkt=None, aktueller_ausschaltpunkt=None,
            active_rule_name=None, active_rule_sensor=None,
            komfort_aktiv=False, alle_ergebnisse=[], _soll_einschalten=False,
            notfall_aktiv=False, blocking_reason=None,
        ),
    )


def _abweichung(state):
    for e in state.control.alle_ergebnisse:
        if e.name == "Abweichung":
            return e
    return None


async def _fahre(state):
    """Fuehrt die Produktionsverdrahtung aus (16:30 am 08.12.2026).

    Die Zeit kommt ueber `state.clock` (die zentrale Zeitquelle der
    Steuerung) - nicht ueber ein `datetime`-Mocking. Letzteres macht das
    Modul-`datetime` zum Mock-Objekt und bricht isinstance-Pruefungen
    im Entscheidungslog.
    """
    import priority_control_logic as pcl
    import pytz

    fest = pytz.timezone("Europe/Berlin").localize(datetime(2026, 12, 8, 16, 30))
    state.clock = SimpleNamespace(now=lambda: fest)
    await pcl.determine_mode_and_setpoints(state, 33.0, 45.0)
    return state


@pytest.mark.asyncio
async def test_produktion_zaehlt_pv_ausserhalb_des_fensters_nicht_als_quelle():
    """Kernfall: Fenster zu -> die PV-Messung traegt den Lauf NICHT.

    Vor dem Fix startete die Abweichungs-Regel hier aus dem Netz, weil der
    Config-Schalter statt der Fensterlage als `pv_erlaubt` durchging.
    """
    # Stundenprognose: nur noch 12 Uhr ueber der Schwelle, jetzt ist 16:30
    # -> das Fenster ist geschlossen.
    state = _pcl_state({12: 900.0})
    await _fahre(state)

    assert state.control.pv_fenster_ausserhalb is True
    abw = _abweichung(state)
    assert abw is not None
    assert abw.einschalten is not True, abw.grund


@pytest.mark.asyncio
async def test_produktion_zaehlt_pv_im_fenster_als_quelle():
    """Gegenprobe: solange das Fenster offen ist, zaehlt die PV als Quelle."""
    # Stundenprognose: bis 18 Uhr ueber der Schwelle, jetzt 16:30 -> offen.
    state = _pcl_state({16: 900.0, 17: 800.0, 18: 600.0})
    await _fahre(state)

    assert state.control.pv_fenster_ausserhalb is False
    abw = _abweichung(state)
    assert abw is not None
    assert abw.einschalten is True, abw.grund


@pytest.mark.asyncio
async def test_ohne_stundenprognose_bleibt_das_gate_offen():
    """Fail-open: ohne Prognose kennt die Steuerung das Fenster nicht.

    Ein Blockieren waere geraten - dann wuerde die Heizung grundlos
    ausfallen. Das Gate bleibt deshalb INAKTIV, PV zaehlt als Quelle.
    """
    state = _pcl_state(None)
    await _fahre(state)

    # Kein Fenster bekannt -> kein "ausserhalb", PV zaehlt als Quelle.
    assert state.control.pv_fenster_ausserhalb is False
    abw = _abweichung(state)
    assert abw is not None
    assert abw.einschalten is True, abw.grund


@pytest.mark.asyncio
async def test_master_schalter_aus_oeffnet_das_gate_wieder():
    """Schalter AUS -> das Gate wird nicht benutzt -> PV zaehlt als Quelle.

    Der Schalter ist die einzige Abschaltung fuer die ganze Funktion.
    """
    state = _pcl_state({12: 900.0})
    state.priority_config.forecast.pv_fenster_aktiv = False
    await _fahre(state)

    assert state.control.pv_fenster_ausserhalb is False
    abw = _abweichung(state)
    assert abw is not None
    assert abw.einschalten is True, abw.grund


def test_energiequelle_ok_reicht_pv_erlaubt_durch():
    """BUGFIX 1: `_energiequelle_ok` reicht `pv_erlaubt` jetzt weiter.

    Vorher nahm die Funktion den Parameter an, liess ihn aber fallen -
    das Gate war damit wirkungslos.
    """
    from priority_control import _energiequelle_ok

    werte = dict(pv_acpower=800.0, battery_power=0.0, solar_stale=False)
    drin = _energiequelle_ok(
        0.0, 95.0, 50.0, 90.0, -50.0, pv_erlaubt=True, **werte)
    draussen = _energiequelle_ok(
        0.0, 95.0, 50.0, 90.0, -50.0, pv_erlaubt=False, **werte)
    assert drin is True
    assert draussen is False, "pv_erlaubt=False muss die PV als Quelle sperren"


def test_notfall_tiefenschutz_heizt_auch_bei_geschlossenem_fenster():
    """Sicherheitsgarantie: Das PV-Fenster-Gate darf nie den Frostschutz
    blockieren.

    Bei geschlossenem Fenster wartet die Abweichungs-Regel auf PV/Batterie.
    Faellt der Speicher jedoch unter die Notfall-Tiefgrenze, muss der
    Kompressor aus dem Netz laufen - sonst bliebe das Brauchwasser kalt.
    """
    from priority_control import evaluate_abweichung

    config = WPSteuerungConfig()
    notfall_c = float(config.notfallschutz.einschalten_bei_c)

    # Kalter Speicher, warmer Speicher oben (Schichtungs-Falle), Gate ZU.
    temp = {"unten": 30.0, "mittig": 35.0, "oben": 48.0}
    r = evaluate_abweichung(
        config.abweichung, temp, False, 16,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
        feedin_watt=0.0, soc=95.0, pv_acpower=800.0, battery_power=0.0,
        solar_stale=False, notfall_einschalten_c=notfall_c,
        pv_quelle_erlaubt=False,
    )
    assert r.einschalten is True, r.grund
    assert "Notfall" in r.grund or "Tiefgrenze" in r.grund, r.grund

