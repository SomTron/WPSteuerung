"""Tests: Hartes Boiler-Maximum (bricht die Mindestlaufzeit).

Anforderung (2026-08-24): "der Boiler soll nicht mehr als 48 Grad haben.
wenn er oben 48,7 hat unten aber nur noch 30 kann er schon einschalten
aber nicht wenn er unten 48,8 Grad hat."

Vorher: Unten 48.8 >= Limit 48 -> alle Regeln sagten AUS, aber die
Mindestlaufzeit hielt den Kompressor noch ~10 min fest und heizte oben
auf 51.7 C weiter. Neu: Der BoilerMax-Abschalter bricht die Laufzeit.

Nachtrag (Nutzerentscheidung 28.09.2026): `boiler_max_fuehler` ist jetzt
`"max"` - es zaehlt der HEISSESTE Fuehler, nicht mehr fest "unten". Der
User-Fall oben ("oben 48,7 / unten 30 -> einschalten") ist damit bewusst
UMKEHRT: bei geschichtetem Boiler stand oben am Limit, waehrend unten kalt
war - die WP wurde in den am Limit stehenden Boiler hinein geheizt
(Log 28.09.: oben 57.8 C / unten 23.7 C nach Legionellenprophylaxe).
Siehe `test_userfall_oben_warm_unten_kalt_kein_einschalten`.
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
    """Testfixture.

    Der Bezugsfuehler des Boiler-Maximums ist `unten` (Deployment). Die
    Standardwerte fuer oben/mittig sind bewusst HEISS - wie im echten Betrieb
    nach einer Schichtung bzw. der Legionellenprophylaxe. Sie duerfen das
    Maximum nicht beeinflussen, weil sie nicht der Bezugsfuehler sind.
    Tests zum `max`-Modus setzen `boiler_max_fuehler` explizit.
    """
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
            requested_rule_name=None,
        ),
        legionellen_aktiv=False,
        legionellen_temp_override=None,
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
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
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
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler([]),
    )
    assert erg is True
    assert state.control.boiler_max_blockiert is None


@pytest.mark.asyncio
async def test_legionellenrueckstand_sperrt_die_heizung_nicht():
    """Kernfall aus dem Nutzer-Log vom 28.09.2026, 17:37:

        Oben=55.0 | Mittig=41.2 | Unten=22.8
        Status: AUS | Info: Boiler-Max-Naehe (oben 55.0C, mittig 41.2C,
        Einschalten erst < 45.0C) | Modus: AdaptivePV

    Mit `boiler_max_fuehler = "max"` zaehlte der OBERE Fuehler gegen das
    48-C-Limit. Nach der Legionellenprophylaxe (Ziel 60 C) steht er aber
    stundenlang ueber 45 C - die Ein-Sperre hat damit jede Heizung blockiert,
    obwohl `unten` bei 22.8 C nach Wärmete verlangte. Zurueck auf die
    geregelte Groesse `unten`: das Sperre-Naehe-Verhalten greift wieder am
    kalten Boiler, der Schutz fuer oben bleibt ueberhitzung_c (58 C)."""
    state = baue_state(t_unten=22.8, t_oben=55.0, t_mittig=41.2)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=22.8, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is True, (
        "Ein kalter Boiler (unten 22.8 C) muss heizen duerfen, auch wenn der "
        "Oberfuehler nach Legionellenprophylaxe noch heiss ist"
    )
    assert calls and calls[0][0] is True


def test_deployment_referenziert_die_geregelte_groesse():
    """Regression: 'max' als Deployment-Default blockierte nach jedem
    Legionellenlauf stundenlang jede Heizung. Das Limit gehoert auf die
    Groesse, die die Regeln tatsaechlich steuern."""
    cfg = WPSteuerungConfig()
    assert cfg.sicherheit.boiler_max_fuehler == "unten"


@pytest.mark.asyncio
async def test_max_modus_sperrt_bei_heissem_oberfuehler():
    """'max' bleibt als Option erhalten (fuer gleichmaessig durchmischte
    Boiler) - und sperrt dann erwartungsgemaess. Der Unterschied zum
    Deployment ist genau der Unterschied, der oben zum Fehlverhalten
    fuehrte: der Oberfuehler zaehlt."""
    cfg = WPSteuerungConfig()
    cfg.sicherheit.boiler_max_fuehler = "max"
    state = baue_state(t_unten=22.8, t_oben=55.0, t_mittig=41.2, config=cfg)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=22.8, einschaltpunkt=45.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is False
    assert calls == []
    assert "oben 55.0" in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_on_blockiert_in_der_limitnaehe_ohne_vorheriges_limit():
    """Incident 26.08. 14:38: unten 47.9C am Limit -> EIN wird verhindert.

    Vorher startete der Kompressor bei 47.94C und erreichte nach 2 min das
    harte Maximum (BOILERMAX-AUS, Mindestlaufzeit gebrochen). Neu greift die
    Ein-Sperre schon im Naehbereich - ganz ohne vorheriges Limit-Ereignis."""
    state = baue_state(t_unten=47.9)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=47.9, einschaltpunkt=42.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is False
    assert calls == []
    assert "Boiler-Max-Naehe" in (state.control.blocking_reason or "")
    # Kein Kuehlphase-Flag gesetzt worden - es war ja kein Limit-Ereignis
    assert getattr(state.control, "boiler_max_blockiert", None) is None


@pytest.mark.asyncio
async def test_on_erlaubt_mit_zwei_kelvin_luft():
    """unten 45.9C (< 48 - 2K): EIN weiterhin erlaubt, sobald der freie Hub
    mit der realen (langsamen) Rate die Mindestlaufzeit fuellt. Hier 6 C/h
    -> 2.1K/6*60min = 21min >= 15min Mindestlaufzeit."""
    state = baue_state(t_unten=45.9)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control._rate_messung = {
        "ts": datetime.now(TZ) - timedelta(minutes=6),
        "unten": 45.3,  # +0.6K in 0.1h = 6 C/h
    }
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.9, einschaltpunkt=42.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is True
    assert calls and calls[0][0] is True


@pytest.mark.asyncio
async def test_on_blockiert_wenn_hub_nicht_fuer_mindestlaufzeit_reicht():
    """Empfehlung 3.1: unten 45.9 mit schneller Rate (24 C/h) -> Hub reicht
    nur ~5min < 15min Mindestlaufzeit -> Start wird abgelehnt, statt in den
    erzwungenen Laufzeit-Bruch/Kuehlphase zu laufen."""
    state = baue_state(t_unten=45.9)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control._rate_messung = {
        "ts": datetime.now(TZ) - timedelta(minutes=6),
        "unten": 43.5,  # +2.4K in 0.1h = 24 C/h
    }
    calls = []
    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.9, einschaltpunkt=42.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),
    )
    assert erg is False
    assert calls == []
    assert "Start-Antizipation" in (state.control.blocking_reason or "")


def test_start_antizipation_niedrige_konfidenz_verlaengert_puffer():
    state = baue_state(t_unten=45.9)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    # Keine Messung: Fallback-Rate 12 C/h, confidence 0.1.
    calls = []
    async def set_status(state, ein, **kwargs):
        calls.append((ein, kwargs))
        return True

    import asyncio
    erg = asyncio.run(pcl.handle_compressor_on(
        state, None, regelfuehler=45.9, einschaltpunkt=42.0, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), min_pause=timedelta(minutes=30),
        t_oben=state.sensors.t_oben, t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=set_status,
    ))
    assert erg is False
    assert calls == []
    info = state.control._last_start_anticipation
    assert info["confidence_category"] == "niedrig"
    assert info["extra_buffer_min"] == 2.0
    assert info["effective_buffer_min"] == 3.0


@pytest.mark.asyncio
async def test_legionellen_wunsch_benutzt_boilerlimit_65_vor_start():
    """45C sind im normalen 48C-Guard naehe, fuer Legionellen aber sicher."""
    state = baue_state(t_unten=45.0, t_oben=50.0, t_mittig=45.0)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control.requested_rule_name = "Legionellen"
    calls = []

    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.0, einschaltpunkt=55.0,
        ausschaltpunkt=60.0, min_laufzeit=timedelta(minutes=15),
        min_pause=timedelta(minutes=30), t_oben=50.0, t_mittig=45.0,
        set_kompressor_status_func=_set_status_sammler(calls),
    )

    assert erg is True
    assert calls and calls[0][0] is True
    assert "Boiler-Max-Naehe" not in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_legionellen_mindestpause_bleibt_voll_aktiv():
    """Legionellen hebt die normale Pause nicht auf."""
    state = baue_state(t_unten=45.0, t_oben=50.0, t_mittig=45.0)
    state.control.kompressor_ein = False
    state.control._soll_einschalten = True
    state.control.requested_rule_name = "Legionellen"
    state.stats.last_compressor_off_time = datetime.now(TZ) - timedelta(minutes=5)
    calls = []

    erg = await pcl.handle_compressor_on(
        state, None, regelfuehler=45.0, einschaltpunkt=55.0,
        ausschaltpunkt=60.0, min_laufzeit=timedelta(minutes=15),
        min_pause=timedelta(minutes=30), t_oben=50.0, t_mittig=45.0,
        set_kompressor_status_func=_set_status_sammler(calls),
    )

    assert erg is False
    assert calls == []
    assert "Min. Pause" in (state.control.blocking_reason or "")


@pytest.mark.asyncio
async def test_laufende_legionellen_haertet_bei_65c_ab():
    """Der Schutz bleibt aktiv: bei 65C muss die Legionellenfahrt enden."""
    state = baue_state(t_unten=65.0, t_oben=63.0, t_mittig=64.0)
    state.legionellen_aktiv = True
    state.legionellen_temp_override = 65.0
    calls = []

    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=65.0, ausschaltpunkt=60.0,
        min_laufzeit=timedelta(minutes=15), t_oben=63.0,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="Legionellen",
    )

    assert erg is True
    assert calls and calls[0][0] is False
    assert calls[0][1]["end_grund"] == "boiler_max"
    assert state.control.boiler_max_blockiert == pytest.approx(63.0)


def test_nach_legionellen_gilt_wieder_normaler_boilerschutz():
    state = baue_state(t_unten=48.5)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.legionellen_end_time = None
    _temp, limit, wiederein, _sensor = pcl._boiler_max_info(state)
    assert limit == 48.0
    assert wiederein == 46.0


# ---------- Bezugsfuehler "max" + Legionellen-Nachlauf (28.09.2026) ----------

def test_max_waehlt_den_heissesten_fuehler():
    """'max' bleibt als Option fuer gleichmaessig durchmischte Boiler."""
    cfg = WPSteuerungConfig()
    cfg.sicherheit.boiler_max_fuehler = "max"
    state = baue_state(t_unten=23.7, t_oben=57.8, t_mittig=40.0, config=cfg)
    state.legionellen_end_time = None
    temp, _limit, _wie, fuehler = pcl._boiler_max_info(state)
    assert fuehler == "oben"
    assert temp == pytest.approx(57.8)


def test_max_meldet_mittig_wenn_oben_ausgefallen_ist():
    cfg = WPSteuerungConfig()
    cfg.sicherheit.boiler_max_fuehler = "max"
    state = baue_state(t_unten=23.7, t_oben=None, t_mittig=41.0, config=cfg)
    state.legionellen_end_time = None
    temp, _limit, _wie, fuehler = pcl._boiler_max_info(state)
    assert fuehler == "mittig"
    assert temp == pytest.approx(41.0)


def test_max_ohne_sensoren_kein_eingriff():
    cfg = WPSteuerungConfig()
    cfg.sicherheit.boiler_max_fuehler = "max"
    state = baue_state(t_unten=None, t_oben=None, t_mittig=None, config=cfg)
    state.legionellen_end_time = None
    assert pcl._boiler_max_info(state) == (None, None, None, "max")


def test_legionellen_nachlauf_hebt_limit_auf_65():
    """Direkt nach der Prophylaxe: Limit 65C statt 48C, damit der noch
    sehr heisse Boiler (oben 57.8C) nicht sofort abgeschaltet wird und
    die WP ihn wieder auf Solltemperatur bringen kann."""
    state = baue_state(t_unten=48.5, t_oben=57.8, t_mittig=50.0)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.legionellen_end_time = datetime.now(TZ) - timedelta(minutes=30)
    _temp, limit, wiederein, _f = pcl._boiler_max_info(state)
    assert limit == 65.0
    assert wiederein == pytest.approx(63.0)


def test_legionellen_nachlauf_laeft_ab():
    """Nach 180 min ist das normale 48C-Limit wieder aktiv."""
    state = baue_state(t_unten=48.5, t_oben=57.8, t_mittig=50.0)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.legionellen_end_time = datetime.now(TZ) - timedelta(minutes=181)
    _temp, limit, _wie, _f = pcl._boiler_max_info(state)
    assert limit == 48.0


def test_legionellen_nachlauf_0_deaktiviert():
    state = baue_state(t_unten=48.5, t_oben=57.8, t_mittig=50.0)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.legionellen_end_time = datetime.now(TZ) - timedelta(minutes=1)
    state.priority_config.sicherheit.legionellen_nachlauf_min = 0.0
    _temp, limit, _wie, _f = pcl._boiler_max_info(state)
    assert limit == 48.0


@pytest.mark.asyncio
async def test_nachlauf_verhindert_abwuergen_des_noch_heissen_boilers():
    """End-to-End: Nach der Prophylaxe darf `handle_compressor_off` bei
    oben 57.8C / unten 48.5C NICHT abschalten - ohne Nachlauf wuerde der
    Boiler hier dauerhaft blockiert bleiben."""
    state = baue_state(t_unten=48.5, t_oben=57.8, t_mittig=50.0)
    state.legionellen_aktiv = False
    state.legionellen_temp_override = None
    state.control.requested_rule_name = None
    state.legionellen_end_time = datetime.now(TZ) - timedelta(minutes=20)
    calls = []
    erg = await pcl.handle_compressor_off(
        state, None, regelfuehler=48.5, ausschaltpunkt=48.0,
        min_laufzeit=timedelta(minutes=15), t_oben=57.8,
        set_kompressor_status_func=_set_status_sammler(calls),
        regel_name="Abweichung",
    )
    assert calls == []          # kein Abschalten
    assert erg is False
    assert "Boiler-Maximum" not in (state.control.blocking_reason or "")


# ---------- Konfiguration ----------

def test_default_werte_matchen_anforderung():
    cfg = WPSteuerungConfig()
    s = cfg.sicherheit
    assert s.max_temp_c == 48.0
    # Nutzerentscheidung 28.09.2026, korrigiert: Bezugsfuehler ist die
    # GERE GELTE Groesse 'unten'. 'max' sperrte nach jeder Legionellen-
    # prophylaxe stundenlang jede Heizung (siehe
    # test_legionellenrueckstand_sperrt_die_heizung_nicht).
    assert s.boiler_max_fuehler == "unten"
    assert s.boiler_max_hysterese_k == 2.0
    assert s.boiler_max_ein_abstand_k == 2.0
    assert s.legionellen_nachlauf_min == 180.0


def test_validatoren():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SicherheitConfig(boiler_max_fuehler="seitlich")
    with pytest.raises(ValidationError):
        SicherheitConfig(boiler_max_hysterese_k=-1.0)
    with pytest.raises(ValidationError):
        SicherheitConfig(boiler_max_ein_abstand_k=-0.5)
    with pytest.raises(ValidationError):
        SicherheitConfig(max_temp_c=60.0, ueberhitzung_c=55.0)
    with pytest.raises(ValidationError):
        SicherheitConfig(max_temp_c=10.0)
