"""Tests: Magic Numbers wanderten in die JSON-Config (Design-Fix #6).

- Bademodus-Erhoehung (+3K) -> bademodus.solltemperatur_erhoehung_c
- AdaptivePV-Prognose-Schwellen (4000/1000 Wh/qm) -> adaptive_pv.fc_schwelle_*
- Wochenende-Prioritaet (100) -> wochenende.prioritaet
"""
import os
import sys
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytz
import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import (  # noqa: E402
    WPSteuerungConfig,
    AdaptivePVConfig,
    WochenendeConfig,
    KomfortConfig,
    CalculatedStartConfig,
)
from priority_control import evaluate_adaptive_pv, evaluate_wochenende  # noqa: E402
import priority_control as pc  # noqa: E402
import priority_control_logic as pcl  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


# ── Wochenende-Prioritaet ──

def test_wochenende_laufender_zyklus_wird_nicht_durch_wochenendspur_re_befohlen():
    config = WPSteuerungConfig()
    samstag = TZ.localize(datetime(2025, 6, 14, 8, 0))
    ergebnis = evaluate_wochenende(config.wochenende, samstag, kompressor_ein=True)
    assert ergebnis.einschalten is None
    assert "Mindestlaufzeit" in ergebnis.grund


def test_wochenende_ohne_lauf_behindert_start():
    config = WPSteuerungConfig()
    samstag = TZ.localize(datetime(2025, 6, 14, 8, 0))
    ergebnis = evaluate_wochenende(config.wochenende, samstag, kompressor_ein=False)
    assert ergebnis.einschalten is False


def test_wochenende_prio_default_100():
    config = WPSteuerungConfig()
    assert config.wochenende.prioritaet == 100

    samstag_frueh = TZ.localize(datetime(2025, 6, 14, 8, 0))
    ergebnis = evaluate_wochenende(config.wochenende, samstag_frueh)
    assert ergebnis.prioritaet == 100
    assert ergebnis.einschalten is False  # vor 9 Uhr blockiert


def test_wochenende_prio_aus_config():
    config = SimpleNamespace(wochenende=WochenendeConfig(
        aktiv=True, fruehestens_uhr=9, prioritaet=77,
    ))
    samstag_frueh = TZ.localize(datetime(2025, 6, 14, 8, 0))

    block = evaluate_wochenende(config.wochenende, samstag_frueh)
    assert block.prioritaet == 77
    assert block.einschalten is False

    erlaubt = evaluate_wochenende(config.wochenende, TZ.localize(datetime(2025, 6, 14, 10, 0)))
    assert erlaubt.prioritaet == 77

    inaktiv = evaluate_wochenende(
        WochenendeConfig(aktiv=False, fruehestens_uhr=9, prioritaet=77),
        samstag_frueh,
    )
    assert inaktiv.prioritaet == 77

    kein_wochenende = evaluate_wochenende(config.wochenende, TZ.localize(datetime(2025, 6, 11, 8, 0)))
    assert kein_wochenende.prioritaet == 77


# ── AdaptivePV-Prognose-Schwellen ──

def _adaptive_grund(forecast, adaptive_cfg=None):
    cfg = adaptive_cfg or AdaptivePVConfig()
    ergebnis = evaluate_adaptive_pv(
        cfg,
        # 40C: ueber beiden Kalt-Schwellen (35/38) -> kein Temperatur-Faktor,
        # die Grund-Schwelle 300W bleibt direkt sichtbar
        temp_dict={"unten": 40.0, "mitte": 42.0, "oben": 44.0},
        pv_leistung=0.0,  # unter jeder Schwelle -> Grund enthaelt die Schwelle
        pv_acpower=200.0,
        forecast_wh_qm=forecast,
        kompressor_ein=False,
        now_hour=12,
    )
    return ergebnis.grund


def test_adaptive_pv_schwellen_default():
    assert AdaptivePVConfig().fc_schwelle_gut_wh == 4000.0
    assert AdaptivePVConfig().fc_schwelle_schlecht_wh == 1000.0

    # 4001 Wh/qm = "guter Tag" -> Schwelle x1.5 (300 -> 450 W)
    assert "< 450W" in _adaptive_grund(4001)
    # 3999 Wh/qm = neutral -> Basis-Schwelle 300 W
    assert "< 300W" in _adaptive_grund(3999)
    # 1001 Wh/qm = neutral -> Basis-Schwelle 300 W
    assert "< 300W" in _adaptive_grund(1001)


# --- AdaptivePV: begrenzte Klammern (Punkte 2 der Regelanalyse) -----------
def test_adaptive_pv_senkt_nicht_unter_die_kombinationsgrenze():
    """Kalt (x0.5) UND bewoelkt (x0.5) ergab gestapelt x0.175 = 52 W.

    Jetzt begrenzt `max_kombinationsfaktor` (0.5) die gemeinsame Senkung:
    300 x 0.5 = 150 W, danach greift min_start_watt (300 W). Die
    Temperaturinformation geht nicht mehr in der Senkung verloren.
    """
    ergebnis = evaluate_adaptive_pv(
        AdaptivePVConfig(), {"unten": 30.0}, 200.0, 900.0,
        False, now_hour=12, forecast_today_wh_qm=900.0,
    )
    assert ergebnis.einschalten is None
    assert "Mindestwert 300W" in ergebnis.grund


def test_adaptive_pv_schwelle_wird_nach_oben_begrenzt():
    """x0.7 (leicht kalt) mal x1.5 (guter Tag) ergab 315 W.

    Bei 7 kB PV ist 315 W eine Ausloesung durch jede Wolke. Die neue
    `max_start_watt`-Obergrenze behebt die vorher offene Seite des Bandes.
    """
    cfg = AdaptivePVConfig(base_threshold_watt=2000.0)
    ergebnis = evaluate_adaptive_pv(
        cfg, {"unten": 37.0}, 400.0, 5000.0,
        False, now_hour=12, forecast_today_wh_qm=5000.0,
    )
    # 2000 x 0.7 x 1.5 = 2100 -> auf 1500 W begrenzt
    assert "Hoechstwert 1500W" in ergebnis.grund
    assert ergebnis.einschalten is None, "400 W darf unter 1500 W nicht starten"


def test_adaptive_pv_obergrenze_laest_echten_ueberschuss_durch():
    """Gegenprobe: mit 2500 W PV muss die Regel trotz guter Prognose starten."""
    ergebnis = evaluate_adaptive_pv(
        AdaptivePVConfig(), {"unten": 37.0}, 2500.0, 5000.0,
        False, now_hour=12, forecast_today_wh_qm=5000.0,
    )
    assert ergebnis.einschalten is True, ergebnis.grund


def test_adaptive_pv_grund_nennt_beide_faktoren():
    """Die Begruendung muss die Faktoren ausweisen - sonst ist die
    Schwelle im Log nicht nachvollziehbar. Geprueft wird der EIN-Fall,
    weil nur dort beide Faktoren ohne Klammerung sichtbar sind."""
    ergebnis = evaluate_adaptive_pv(
        AdaptivePVConfig(),
        temp_dict={"unten": 37.0, "mitte": 39.0, "oben": 44.0},
        pv_leistung=2000.0,
        pv_acpower=2000.0,
        forecast_wh_qm=5000.0,
        kompressor_ein=False,
        now_hour=12,
        forecast_today_wh_qm=5000.0,
    )
    assert ergebnis.einschalten is True, ergebnis.grund
    assert "Temp x0.7" in ergebnis.grund
    assert "Prognose x1.5" in ergebnis.grund


def test_adaptive_pv_klammern_konsistent():
    """min darf nicht groesser als max sein - sonst waere die Regel leer."""
    with pytest.raises(ValueError, match="min_start_watt darf nicht groesser"):
        AdaptivePVConfig(min_start_watt=2000.0, max_start_watt=1500.0)


# --- Prognose-Schwellen duerfen nicht im Code liegen (Befund 04.10.2026) ---
def test_calcstart_prognoseschwellen_kommen_aus_der_config():
    """BEFORE (Befund Regelanalyse 04.10.2026): evaluate_calculated_start
    pruefte fest gegen 3000/1500/500 Wh/qm. Drei Regel-Familien benutzten
    drei verschiedene Saetze: Forecast 3000/800, AdaptivePV 4000/1000,
    CalcStart 3000/1500/500. Nachgerechnet: `forecast.fc_schwelle_hoch_wh`
    auf 9999 gesetzt - das CalcStart-Ergebnis blieb IDENTISCH.

    Wer die Prognoseschwelle tunen will, tat bisher so, als aendere sich
    etwas, obwohl es nicht passierte.
    """
    from priority_control import evaluate_calculated_start

    temps = {"unten": 40.0, "mittig": 40.0, "oben": 46.0}
    basis = CalculatedStartConfig()

    a = evaluate_calculated_start(
        basis, temps, 10, 0, forecast_wh_qm=2500.0,
        feedin_watt=2000.0, pv_acpower=3000.0,
    )
    # Schwellwert so verschieben, dass 2500 jetzt als "sehr sonnig" gilt
    verschoben = CalculatedStartConfig(fc_schwelle_sehr_sonnig_wh=2000.0)
    b = evaluate_calculated_start(
        verschoben, temps, 10, 0, forecast_wh_qm=2500.0,
        feedin_watt=2000.0, pv_acpower=3000.0,
    )

    assert a.grund != b.grund, (
        "Die Config-Aenderung muss die CalcStart-Bewertung wirklich "
        "veraendern - sonst liegt die Schwelle immer noch im Code"
    )
    assert "sonnig" in a.grund and "sehr sonnig" in b.grund


def test_calcstart_defaults_entsprechen_den_bisherigen_codewerten():
    """Die Defaults muessen exakt den Werten entsprechen, die vorher
    fest im Code standen - sonst aendert die Herausnahme das Verhalten."""
    cfg = CalculatedStartConfig()
    assert cfg.fc_schwelle_sehr_sonnig_wh == 3000.0
    assert cfg.fc_schwelle_sonnig_wh == 1500.0
    assert cfg.fc_schwelle_bewoelkt_wh == 500.0


def test_batterie_entlastungsschwelle_kommt_aus_der_config():
    """Auch evaluate_batterie hatte mit 2000.0 eine feste Zahl im Code."""
    from json_config import BatterieConfig
    assert BatterieConfig().entlastung_ab_wh == 2000.0


def test_adaptive_pv_schlechte_prognose_wird_auf_mindestwert_angehoben():
    """Die x0.5-Stufe (150 W) wird durch min_start_watt (300 W) geboden.

    Befund aus dem Laufzeitlog 18.09.-01.10.2026: die Multiplikatoren
    stapelten sich bis auf 75 W (300 x 0.5 x 0.5). Bei 75 W Ueberschuss
    zieht die WP ~600 W - der Start kam zu ueber 80 % aus dem Netz und war
    damit kein PV-Start. min_start_watt begrenzt das nach unten.
    """
    # 999 Wh/qm = "schlechter Tag" -> x0.5 = 150 W, aber auf 300 W gehoben
    grund = _adaptive_grund(999)
    assert "< 300W" in grund
    assert "< 150W" not in grund

    # Bei 200 W PV (ueber 150, aber unter 300) wird NICHT eingeschaltet
    ergebnis = evaluate_adaptive_pv(
        AdaptivePVConfig(), {"unten": 40.0}, 200.0, 999.0,
        False, now_hour=12, forecast_today_wh_qm=999.0,
    )
    assert ergebnis.einschalten is None, f"200 W darf nicht starten: {ergebnis.grund}"

    # Oberhalb der Mindestgrenze startet die Regel wieder
    ergebnis = evaluate_adaptive_pv(
        AdaptivePVConfig(), {"unten": 40.0}, 320.0, 999.0,
        False, now_hour=12, forecast_today_wh_qm=999.0,
    )
    assert ergebnis.einschalten is True, f"320 W muss starten: {ergebnis.grund}"


def test_adaptive_pv_mindestwert_ist_abschaltbar():
    """min_start_watt=0 stellt das alte Verhalten wieder her."""
    cfg = AdaptivePVConfig(min_start_watt=0.0)
    # Ohne Untergrenze greift wieder x0.5 -> 150 W
    assert "< 150W" in _adaptive_grund(999, cfg)
    # Validierung: negative Werte sind ungueltig
    with pytest.raises(ValueError):
        AdaptivePVConfig(min_start_watt=-1.0)


def test_adaptive_pv_schwellen_konfigurierbar():
    cfg = AdaptivePVConfig(fc_schwelle_gut_wh=2000.0, fc_schwelle_schlecht_wh=500.0)
    # 2500 liegt jetzt im "guten" Bereich -> x1.5 (450 W, ueber der Grenze)
    assert "< 450W" in _adaptive_grund(2500, cfg)
    # 400 waere jetzt "schlecht" -> x0.5 = 150 W, auf min_start_watt gehoben
    assert "< 300W" in _adaptive_grund(400, cfg)


# ── Bademodus-Erhoehung ──

def _baue_state(bademodus, erhoehung):
    config = WPSteuerungConfig()
    config.bademodus.solltemperatur_erhoehung_c = erhoehung
    return SimpleNamespace(
        local_tz=TZ,
        priority_config=config,
        bademodus_aktiv=bademodus,
        urlaubsmodus_aktiv=False,
        legionellen_aktiv=False,
        legionellen_last_done=None,
        legionellen_started_at=None,
        sensors=SimpleNamespace(t_oben=40.0, t_unten=41.0, t_mittig=42.0, t_verd=30.0),
        solar=SimpleNamespace(
            # PV als Quelle fuer das Abweichungs-Gate, aber SOC niedrig, damit
            # die Batterie-Regel (Prio 75) nicht dazwischen gewinnt
            acpower=100, feedinpower=100, batpower=0, soc=0, forecast_today=None, forecast_tomorrow=None,
            last_api_call=TZ.localize(datetime(2025, 6, 11, 17, 55)),
        ),
        control=SimpleNamespace(
            kompressor_ein=False,
            previous_modus="Normalmodus",
            aktueller_einschaltpunkt=None,
            aktueller_ausschaltpunkt=None,
            active_rule_name=None,
            active_rule_sensor=None,
            komfort_aktiv=False,
            alle_ergebnisse=[],
        ),
    )


@pytest.mark.asyncio
async def test_bademodus_erhoehung_aus_config():
    """Mit Erhoehung +5K gewinnt Abweichung bei t_unten=41 (Soll 40+5=45, -3K Hysterese)."""
    state = _baue_state(bademodus=True, erhoehung=5.0)

    with patch('priority_control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(datetime(2025, 6, 11, 18, 0))  # Mi, ausserhalb Fenster
        result = await pcl.determine_mode_and_setpoints(state, t_unten=41.0, t_mittig=42.0)

    gewinner = result["gewinner_ergebnis"]
    assert gewinner is not None and gewinner.name == "Abweichung"
    # einschaltpunkt = (42 + 5) - 3.0 = 44.0; ohne Bademodus waere Basis 42.
    assert result["einschaltpunkt"] == 44.0


@pytest.mark.asyncio
async def test_ohne_bademodus_keine_erhoehung():
    """Ohne Bademodus gilt exakt das Basisziel 42C.

    Der Test prueft das ZIEL, nicht die Regel: Komfort ist seit der
    Log-Auswertung abgeschaltet (aktiv=false, siehe KomfortConfig), die
    42-C-Abschaltung uebernimmt jetzt die Abweichungsregel.
    """
    state = _baue_state(bademodus=False, erhoehung=5.0)
    state.sensors.t_unten = 42.0

    with patch('priority_control_logic.datetime') as mock_dt:
        mock_dt.now.return_value = TZ.localize(datetime(2025, 6, 11, 18, 0))
        result = await pcl.determine_mode_and_setpoints(state, t_unten=42.0, t_mittig=42.0)

    gewinner = result["gewinner_ergebnis"]
    assert gewinner is not None
    assert gewinner.einschalten is False
    assert result["ausschaltpunkt"] == 42.0


@pytest.mark.asyncio
async def test_komfort_ist_abgeschaltet():
    """Komfort startet nicht mehr allein wegen ein paar Watt PV.

    Log-Beleg 18.09.-01.10.2026: die Regel leitete bei 81 W PV ein, bei
    einem Bedarf von 600 W elektrisch (87 % Netzanteil). Sie war die
    einzige Regel im Spalt zwischen 50 W und der AdaptivePV-Schwelle.
    """
    cfg = KomfortConfig()
    assert cfg.aktiv is False
    erg = pc.evaluate_komfort(
        cfg, {"unten": 23.7, "mitte": 40.0, "oben": 57.8},
        81.0, 19, 8, 9,
    )
    assert erg.aktiv is False
    assert "abgeschaltet" in erg.grund


def _deployment_cfg():
    pfad = os.path.join(os.path.dirname(__file__), "..", "wp_steuerung_parameter.json")
    with open(pfad, encoding="utf-8") as f:
        return json.load(f)


def test_mittag_fenster_deckt_den_vormittag():
    """Von 08:00 bis 11:00 gab es zeitlich keine Komfortregel.

    Log 18.09.-01.10.2026: Komfort lieferte dort 2 EIN-Zeilen bei 81 W PV
    (87 % Netzanteil bei 600 W Bedarf). CalcStart zielt auf die Zapfzeit
    (16-17 Uhr), MinTemp-Mittag-Mitte startete erst um 11:00. Das Fenster
    beginnt jetzt um 09:00 - zeitgesteuert statt temperatur+PV-gesteuert.
    """
    eintraege = _deployment_cfg()["mindest_temp"]["eintraege"]
    mittag = next(e for e in eintraege if e["name"] == "Mittag-Mitte")
    assert mittag["start_uhr"] == 9
    assert mittag["ende_uhr"] == 16


def test_deployment_komfort_abgeschaltet():
    """Komfort ist aus - CalcStart und MinTemp uebernehmen die Aufgabe."""
    cfg = _deployment_cfg()
    assert cfg["komfort"]["aktiv"] is False
    # Die zeitbezogenen Regeln bleiben aktiv.
    assert cfg["mindest_temp"]["aktiv"] is True
    assert cfg["calculated_start"]["aktiv"] is True


def test_solar_deckt_das_retry_budget_ab():
    """Die Hintergrund-Deadline muss zum Retry-Budget passen.

    Betriebslog 02.10.2026: mit 15 s schnitt die Deadline die Wiederholungen
    nach Versuch 2 ab; der innere Fallback ("verwende Fallback-Daten") wurde
    nie erreicht und es lief ein voller TimeoutError-Traceback auf.
    """
    import constants
    budget = (
        constants.SOLAX_MAX_RETRIES * constants.SOLAR_API_TIMEOUT_SEC
        + (constants.SOLAX_MAX_RETRIES - 1) * constants.SOLAX_RETRY_DELAY_SEC
    )
    assert constants.SOLAR_REFRESH_DEADLINE_SEC >= budget, (
        f"Deadline {constants.SOLAR_REFRESH_DEADLINE_SEC}s deckt das "
        f"Retry-Budget von {budget}s nicht"
    )
