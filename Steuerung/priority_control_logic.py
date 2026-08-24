"""
Prioritaetenbasierte Steuerungslogik (Pareto-optimiert).

Ersetzt die modusbasierte Logik durch eine Regelengine mit Prioritaeten.
Jede Regel bewertet unabhaengig die aktuelle Situation.
Die Regel hoechster Prioritaet bestimmt das Schaltverhalten.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable

from utils import safe_timedelta
from constants import (
    CONFIG_CHECK_INTERVAL_SEC,
)
try:
    from constants import SOLAR_DATA_STALE_THRESHOLD_MIN
except ImportError:
    SOLAR_DATA_STALE_THRESHOLD_MIN = 15
try:
    import entscheidungs_log
except ImportError:
    entscheidungs_log = None
from logic_utils import check_log_throttle
from safety_logic import (
    handle_critical_compressor_error,
)
from json_config import WPSteuerungConfig
from priority_control import (
    RegelErgebnis,
    bewerte_alle_regeln,
    calcstart_nachtsperre_konflikt,
    formatiere_ergebnisse,
)

try:
    from telegram_api import send_telegram_message  # noqa: F401 - Verfuegbarkeits-Probe
except ImportError:
    pass

from collections import deque


def set_last_compressor_off_time(state, time_val):
    """Setzt den Zeitpunkt des letzten Kompressor-Ausschaltens."""
    state.stats.last_compressor_off_time = time_val


def setze_neustartsperre(state, minuten: int = 10) -> None:
    """Setzt eine explizite Neustartsperre fuer den Kompressor.

    Ersetzt den alten Trick, last_compressor_off_time in die Zukunft zu
    setzen: Die Sperre ist jetzt ein eigenes Feld mit klar lesbarem
    Blocking-Reason, statt eine Mindestpausen-Rechnung zu verfaelschen."""
    state.control.restart_lockout_until = (
        datetime.now(state.local_tz) + timedelta(minutes=minuten)
    )


async def check_pressure_and_config(
    session, state, handle_pressure_check_func: Callable,
    set_kompressor_status_func: Callable, only_pressure: bool = False
):
    """Prueft Druckschalter und aktualisiert Konfiguration bei Bedarf."""
    pressure_ok = await handle_pressure_check_func(session, state)
    if state.control.last_pressure_state != pressure_ok:
        logging.info(f"Druckschalter: {'OK' if pressure_ok else 'Fehler'}")
        state.control.last_pressure_state = pressure_ok
    if not pressure_ok:
        state.control.ausschluss_grund = "Druckschalterfehler"
        state.control.blocking_reason = "Druckschalter-Fehler"
        if state.control.kompressor_ein:
            await set_kompressor_status_func(state, False, force=True)
        return False

    if not only_pressure:
        if safe_timedelta(datetime.now(state.local_tz), state._last_config_check, state.local_tz) > timedelta(seconds=CONFIG_CHECK_INTERVAL_SEC):
            state.update_config()
            state._last_config_check = datetime.now(state.local_tz)
    return True


def _solar_daten_veraltet(state) -> bool:
    """True, wenn die Solax-Daten aelter als der Stale-Schwellwert sind.

    Die PV-abhaengigen Regeln werden dann pausiert (siehe priority_control);
    main.py setzt zusaetzlich die Werte selbst auf 0."""
    last_api_call = getattr(state.solar, "last_api_call", None)
    if last_api_call is None:
        return True  # nie geliefert -> nicht bewertbar -> konservativ pausieren
    try:
        jetzt = datetime.now(getattr(last_api_call, "tzinfo", None))
        alter_min = (jetzt - last_api_call).total_seconds() / 60.0
    except (TypeError, ValueError):
        return False
    return alter_min > SOLAR_DATA_STALE_THRESHOLD_MIN


def _gelerntes_morgenfenster(learning_engine):
    """Gelerntes Morgen-Zapffenster defensiv abfragen (alte Fakes fehlt es)."""
    if not learning_engine or not hasattr(learning_engine, "get_learned_morning_window"):
        return None
    try:
        return learning_engine.get_learned_morning_window()
    except Exception as e:  # pragma: no cover - Lernen darf nie blockieren
        logging.debug(f"Morgenfenster nicht ermittelbar: {e}")
        return None


async def determine_mode_and_setpoints(state, t_unten, t_mittig, learning_engine=None):
    """
    Bestimmt den Betriebsmodus basierend auf den Prioritaeten-Regeln.
    
    Statt fester Modus-Logik wird die Prioritaeten-Engine befragt.
    """
    temp_dict = {
        "oben": state.sensors.t_oben,
        "unten": t_unten,
        "mittig": t_mittig,
        "verd": state.sensors.t_verd,
    }
    
    pv_leistung = state.solar.feedinpower if state.solar.feedinpower else 0.0
    if pv_leistung < 0:
        pv_leistung = 0.0
    
    # --- Bademodus/Urlaubsmodus-Kopplung ---
    # Wir arbeiten mit einer Kopie der Config, um die original-Config nicht zu aendern.
    # Falls Bademodus/Urlaub aktiv, passen wir die Solltemperatur der Abweichungs-Regel an.
    import copy
    effektive_config = copy.deepcopy(state.priority_config)
    
    if state.bademodus_aktiv:
        # Bademodus: Zieltemperatur-Erhoehung aus der Config (fuer warmes Wasser)
        erhoehung = effektive_config.bademodus.solltemperatur_erhoehung_c
        effektive_config.abweichung.solltemperatur_c += erhoehung
        logging.debug(f"Bademodus aktiv: Solltemperatur +{erhoehung}C auf {effektive_config.abweichung.solltemperatur_c}C")
    
    if state.urlaubsmodus_aktiv:
        # Urlaubsmodus: Solltemperatur senken (Sparmodus)
        absenkung = float(state.config.Urlaubsmodus.URLAUBSABSENKUNG) if hasattr(state.config, 'Urlaubsmodus') else 5.0
        effektive_config.abweichung.solltemperatur_c -= absenkung
        logging.debug(f"Urlaubsmodus aktiv: Solltemperatur -{absenkung}C auf {effektive_config.abweichung.solltemperatur_c}C")

    # Sommer-Modus: Solltemperatur senken bei mehrtÃ¤gig guter PV-Prognose.
    # Im Sommer scheint fast jeden Tag die Sonne, daher braucht der Boiler nicht
    # jeden Tag auf 44Â°C+ hochgeheizt zu werden - morgen kommt ja wieder PV-Strom.
    # Der Offset (default -3Â°C) reduziert die Zieltemperatur der Abweichungs-Regel.
    if hasattr(state, 'sommer_modus_aktiv') and state.sommer_modus_aktiv:
        wende_sommer_offset_an(effektive_config)
        logging.debug(
            f"Sommer-Modus aktiv: Abweichungs-Soll {effektive_config.abweichung.solltemperatur_c:.1f}C, "
            f"PV-Ausschaltpunkte "
            f"{', '.join(f'{r.name}:{r.ausschalten_bei_c:.0f}C' for r in effektive_config.pv_regeln)}"
        )

    # Forecast-Daten aus State holen
    # Forecast + AdaptivePV brauchen die MORGEN-Prognose (Vorheizen/Sparen)
    forecast_wh_qm = getattr(state.solar, 'forecast_tomorrow', None)
    if forecast_wh_qm is not None:
        try:
            forecast_wh_qm = float(forecast_wh_qm)
        except (TypeError, ValueError):
            forecast_wh_qm = None
    # CalcStart braucht die HEUTE-Prognose (PV-Erwartung zum Warten/Heizen)
    forecast_today_wh = getattr(state.solar, 'forecast_today', None)
    if forecast_today_wh is not None:
        try:
            forecast_today_wh = float(forecast_today_wh)
        except (TypeError, ValueError):
            forecast_today_wh = None
    
    # Alle Regeln bewerten (mit effektiver Config)
    # Learning Engine aktualisieren (Heizzyklen + Zapfprofil)
    if learning_engine is not None:
        learning_engine.update(
            now=datetime.now(state.local_tz),
            temp_dict=temp_dict,
            compressor_is_on=state.control.kompressor_ein,
        )
        gelernte_rate_unten = learning_engine.get_learned_heating_rate(
            datetime.now(state.local_tz).month, 'unten'
        )
        gelernte_rate_gesamt = learning_engine.get_learned_heating_rate(
            datetime.now(state.local_tz).month, 'gesamt'
        )
        gelernte_zielzeit = learning_engine.get_learned_target_hour()
        # Defensiv: aeltere Lern-Engines/Fakes kennen die Methode evtl. nicht
        _get_fenster = getattr(learning_engine, 'get_learned_evening_window', None)
        gelerntes_abendfenster = _get_fenster() if callable(_get_fenster) else None
    else:
        gelernte_rate_unten = None
        gelernte_rate_gesamt = None
        gelernte_zielzeit = None
        gelerntes_abendfenster = None

    # Konfigurations-Guard: CalcStart-Zielzeit vs. Nachtsperre.
    # Eine Zielzeit innerhalb/hinter der Sperre macht die Regel stumm -
    # einmal pro Konfigurations-/Lernstand-Aenderung warnen, damit der
    # Betreiber den Konflikt sieht statt sich ueber kaltes Wasser zu wundern.
    try:
        ziel_pruefung = (
            gelernte_zielzeit
            if gelernte_zielzeit is not None
            else float(state.priority_config.calculated_start.target_uhr)
        )
        calc_tot, _, calc_hinweis = calcstart_nachtsperre_konflikt(
            float(ziel_pruefung),
            state.priority_config.sicherheit.nachtsperre_start,
            state.priority_config.sicherheit.nachtsperre_ende,
        )
        signatur = (calc_tot, round(float(ziel_pruefung), 2))
        if (calc_tot or calc_hinweis) and getattr(
            state, '_calcstart_warn_signatur', None
        ) != signatur:
            state._calcstart_warn_signatur = signatur
            if calc_tot:
                logging.error(f"CalcStart-Konflikt: {calc_hinweis}")
            else:
                logging.warning(f"CalcStart-Hinweis: {calc_hinweis}")
    except (TypeError, ValueError):
        pass  # ungueltige Lernwerte: Regel-Engine faengt das selbst ab

    gewinner, alle_ergebnisse = bewerte_alle_regeln(
        config=effektive_config,
        temp_dict=temp_dict,
        pv_leistung=pv_leistung,
        kompressor_ein=state.control.kompressor_ein,
        now=datetime.now(state.local_tz),
        forecast_wh_qm=forecast_wh_qm,
        forecast_today_wh_qm=forecast_today_wh,
        soc=getattr(state.solar, 'soc', None),
        battery_power=getattr(state.solar, 'batpower', None),
        learned_evening_window=gelerntes_abendfenster,
        learned_morning_window=_gelerntes_morgenfenster_mit_bonus(state, learning_engine),
        solar_stale=_solar_daten_veraltet(state),
        learned_heating_rate_unten=gelernte_rate_unten,
        learned_heating_rate_gesamt=gelernte_rate_gesamt,
        learned_target_hour=gelernte_zielzeit,
    )
    
    # Ergebnisse loggen (gethrottelt)
    if check_log_throttle(state, "_last_priority_log", interval_minutes=5.0):
        logging.info(f"Regel-Bewertung ({len(alle_ergebnisse)} Regeln):\n{formatiere_ergebnisse(alle_ergebnisse)}")
    
    # Gewinner-Regel in State speichern fuer Anzeige
    state.control.active_rule_sensor = None
    state.control.active_rule_name = None
    
    if gewinner is not None:
        state.control.active_rule_name = gewinner.name
        
        # Sensor-Name aus Regel ermitteln
        if "mitte" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Mittig"
        elif "unten" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Unten"
        elif "oben" in gewinner.grund.lower():
            state.control.active_rule_sensor = "Oben"
        
        # Ein/Ausschaltpunkte aus Regel ermitteln.
        # WICHTIG: effektive_config (mit Bademodus-/Urlaubs-Offsets) verwenden,
        # nicht state.priority_config - sonst melden die Setpoints einen
        # anderen Sollwert, als die Regeln tatsaechlich ausgewertet haben.
        if gewinner.einschalten is True:
            eps = _extract_einschaltpunkt(gewinner, effektive_config)
            ausp = _extract_ausschaltpunkt(gewinner, effektive_config)
            state.control.aktueller_einschaltpunkt = eps
            state.control.aktueller_ausschaltpunkt = ausp
        else:
            # Regel sagt AUS: korrekte Setpoints aus der Regel extrahieren,
            # damit handle_compressor_off() den Kompressor auch abschalten kann.
            # Wenn wir hier max_temp_c setzen, wuerde der Kompressor nie ausschalten,
            # weil z.B. t_unten=43.2C < max_temp_c=48C.
            eps = _extract_einschaltpunkt(gewinner, effektive_config)
            ausp = _extract_ausschaltpunkt(gewinner, effektive_config)
            state.control.aktueller_einschaltpunkt = max(eps, ausp)  # hoch, damit kein Neueinschalten
            state.control.aktueller_ausschaltpunkt = ausp            # korrekt, damit Abschaltung funktioniert
    else:
        # Keine Regel will einschalten: Standard = ausschalten
        state.control.aktueller_einschaltpunkt = state.priority_config.sicherheit.max_temp_c
        state.control.aktueller_ausschaltpunkt = state.priority_config.sicherheit.max_temp_c
    
    # Komfort-Einschaltstatus (fuer Komfort-Regel)
    state.control.komfort_aktiv = any(
        e.name == "Komfort" and e.aktiv and e.einschalten is True
        for e in alle_ergebnisse
    )
    
    # Alle Ergebnisse im State speichern fuer API/HTML-Anzeige
    state.control.alle_ergebnisse = alle_ergebnisse
    
    # Ergebnis aufbereiten
    should_on = gewinner is not None and gewinner.einschalten is True
    
    # Regelfuehler dynamisch aus der aktiven Regel ermitteln, nicht hartcodiert t_unten
    regelfuehler = t_unten  # Fallback
    if gewinner is not None:
        if "unten" in gewinner.grund.lower() or "unten" in (gewinner.name or "").lower():
            regelfuehler = t_unten
        elif "mitte" in gewinner.grund.lower() or "mitte" in (gewinner.name or "").lower():
            regelfuehler = t_mittig
        elif "oben" in gewinner.grund.lower() or "oben" in (gewinner.name or "").lower():
            regelfuehler = state.sensors.t_oben
    
    # Solarueberschuss aktiv wenn PV-Leistung >= niedrigste Schwelle aller PV-Regeln
    if state.priority_config.pv_regeln:
        min_pv_schwelle = min(r.pv_schwelle_watt for r in state.priority_config.pv_regeln)
        solar_ueberschuss_aktiv = pv_leistung >= min_pv_schwelle
    else:
        solar_ueberschuss_aktiv = False
    
    res = {
        "modus": gewinner.name if gewinner else "Keine Regel aktiv",
        "einschaltpunkt": state.control.aktueller_einschaltpunkt,
        "ausschaltpunkt": state.control.aktueller_ausschaltpunkt,
        "regelfuehler": regelfuehler,
        "solar_ueberschuss_aktiv": solar_ueberschuss_aktiv,
        "soll_einschalten": should_on,
        "gewinner_ergebnis": gewinner,
        "alle_ergebnisse": alle_ergebnisse,
    }
    
    # Moduswechsel-Logging
    if state.control.previous_modus != res["modus"]:
        logging.info(f"Wechsel zu Regel: {res['modus']} ({'EIN' if should_on else 'AUS'})")
        state.control.previous_modus = res["modus"]

    # Taktschutz (Punkt D): Wechsel tracken
    _track_wechsel(state, res["modus"])

    # Entscheidungs-Historie (JSONL) fuer Webapp/KPIs - darf nie blockieren
    if entscheidungs_log is not None:
        try:
            entscheidungs_log.schreibe_eintrag(
                gewinner_name=gewinner.name if gewinner else None,
                gewinner_grund=gewinner.grund if gewinner else "",
                soll_einschalten=bool(should_on),
                kompressor_laeuft=bool(state.control.kompressor_ein),
                feedin_watt=pv_leistung,
                batpower_watt=getattr(state.solar, 'batpower', None),
                soc=getattr(state.solar, 'soc', None),
                t_unten=t_unten,
                t_oben=getattr(state.sensors, 't_oben', None),
            )
        except Exception as e:  # pragma: no cover
            logging.debug(f"Entscheidungslog-Fehler: {e}")

    return res


def wende_sommer_offset_an(config) -> None:
    """Wendet die Sommermodus-Absenkungen auf eine (Kopie-)Config an.

    Hintergrund (Nutzeranforderung): Bei mehrtaetig gutem PV-Wetter soll der
    Boiler-Buffer bewusst NICHT voll aufgebaut werden - die maximale
    Temperatur wird minimiert, da morgen wieder genug PV-Strom kommt.

    - abweichung.solltemperatur_c += temperatur_offset_c (bisheriges Verhalten)
    - NEU: PV-Regeln + AdaptivePV: ausschalten_bei_c += pv_ausschalt_offset_c
      (PV-Shaping laeuft bei Dauer-Sonne z.B. nur bis 46 statt 48C),
      geklemmt, damit die Einschalthysterese erhalten bleibt.
    """
    offset = config.sommer_modus.temperatur_offset_c
    config.abweichung.solltemperatur_c += offset

    pv_offset = config.sommer_modus.pv_ausschalt_offset_c
    for pv in config.pv_regeln:
        neuer_aus = pv.ausschalten_bei_c + pv_offset
        # Klemme: Ausschaltpunkt bleibt mind. 2K ueber dem Einschaltpunkt
        pv.ausschalten_bei_c = max(neuer_aus, pv.einschalten_bei_c + 2.0)

    apv = config.adaptive_pv
    # Absoluter Boden 42C, damit die Regel nicht wirkungslos/kippelig wird
    apv.tmax_c = max(apv.tmax_c + pv_offset, 42.0)


def _extract_einschaltpunkt(ergebnis: RegelErgebnis, config: WPSteuerungConfig) -> float:
    """Extrahiert den Einschaltpunkt aus dem Regel-Ergebnis fuer Statusanzeige."""
    name = ergebnis.name
    
    if name.startswith("PV_"):
        # PV-Regel: Finde die passende
        for pv in config.pv_regeln:
            if pv.name == name:
                return pv.einschalten_bei_c
    elif name == "Komfort":
        return config.komfort.komfort_einschalten_bei_c
    elif name == "Zeitfenster":
        return config.zeitfenster.max_temp_fuer_einschalten_c
    elif name == "Abweichung":
        return config.abweichung.solltemperatur_c - config.abweichung.einschalten_bei_abweichung_k
    elif name == "Forecast":
        return config.forecast.t_vorheiz_ab_c
    elif name == "AdaptivePV":
        return config.adaptive_pv.base_threshold_watt
    elif name == "CalcStart":
        return config.calculated_start.solltemperatur_c
    elif name.startswith("MinTemp-"):
        for eintrag in config.mindest_temp.eintraege:
            if name == f"MinTemp-{eintrag.name}":
                return eintrag.min_temp_c
    elif name == "Batterie":
        return config.batterie.einschalten_bei_c
    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert

    return config.sicherheit.max_temp_c


def _extract_ausschaltpunkt(ergebnis: RegelErgebnis, config: WPSteuerungConfig) -> float:
    """Extrahiert den Ausschaltpunkt aus dem Regel-Ergebnis fuer Statusanzeige."""
    name = ergebnis.name
    
    if name.startswith("PV_"):
        for pv in config.pv_regeln:
            if pv.name == name:
                return pv.ausschalten_bei_c
    elif name == "Komfort":
        return config.komfort.ausschalten_bei_c
    elif name == "Zeitfenster":
        return config.zeitfenster.max_temp_fuer_einschalten_c
    elif name == "Abweichung":
        return config.abweichung.solltemperatur_c - config.abweichung.ausschalten_bei_abweichung_k
    elif name == "Forecast":
        return config.forecast.tmax_c
    elif name == "AdaptivePV":
        return config.adaptive_pv.tmax_c
    elif name == "CalcStart":
        return config.calculated_start.tmax_c
    elif name.startswith("MinTemp-"):
        for eintrag in config.mindest_temp.eintraege:
            if name == f"MinTemp-{eintrag.name}":
                return eintrag.min_temp_c + eintrag.hysterese_k
    elif name == "Batterie":
        return config.batterie.ausschalten_bei_c
    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c

    return config.sicherheit.max_temp_c



def _gelerntes_morgenfenster_mit_bonus(state, learning_engine):
    """Morgenfenster + Komfort-Bonus (Punkt B): Bei Verletzungen 0.5h Vorlauf."""
    fenster = _gelerntes_morgenfenster(learning_engine)
    if fenster is None:
        return None
    bonus = 0.0
    if learning_engine is not None:
        bonus = getattr(learning_engine, "get_komfort_bonus_vorlauf", lambda: 0.0)()
    if bonus > 0:
        fruehe, spaete = fenster
        fenster = (max(fruehe - bonus, 4.5), spaete)
    return fenster

def _track_wechsel(state, gewinner_name):
    """Trackt echte Entscheidungswechsel (Punkt D) im letzten 60min-Fenster.

    Nur tatsaechliche Wechsel des Gewinners werden erfasst, nicht jede
    Bewertung (siehe Kommentar unten).
    """
    now = datetime.now(state.local_tz)
    hist = getattr(state.control, "_wechsel_historie", None)
    if not isinstance(hist, deque):
        hist = deque()
        try:
            state.control._wechsel_historie = hist
        except Exception:
            pass
    # Alte Eintraege immer entfernen - auch ohne neuen Wechsel
    grenze = now - timedelta(hours=1)
    while hist and hist[0][0] < grenze:
        hist.popleft()
    # Nur echte Wechsel erfassen: erster Eintrag oder anderer Gewinner als
    # zuvor. Der Loop laeuft alle ~13 s - ohne diesen Vergleich waechst der
    # Zaehler mit jedem Durchlauf und der Taktschutz feuert dauerhaft,
    # obwohl der Kompressor gar nicht getaktet hat.
    if hist and hist[-1][1] == gewinner_name:
        return
    hist.append((now, gewinner_name))

def _taktschutz_blockiert(state, cfg) -> float:
    """Prueft Taktschutz (Punkt D): zu viele Wechsel/h -> zusaetzliche Pause.
    Returns: zusaetzliche Pause in Sekunden (0 = keine Blockade)."""
    ts_cfg = getattr(cfg, "taktschutz", None)
    if ts_cfg is None or not getattr(ts_cfg, "aktiv", False):
        return 0.0
    hist = getattr(state.control, "_wechsel_historie", deque())
    if len(hist) < ts_cfg.max_wechsel_pro_stunde:
        return 0.0
    # Sind die Wechsel innerhalb der letzten Stunde?
    now = datetime.now(state.local_tz)
    grenze = now - timedelta(hours=1)
    aktuelle = sum(1 for ts, _ in hist if ts >= grenze)
    if aktuelle >= ts_cfg.max_wechsel_pro_stunde:
        logging.warning(
            f"Taktschutz aktiv: {aktuelle} Wechsel/h >= {cfg.taktschutz.max_wechsel_pro_stunde}, "
            f"zusaetzliche Pause {ts_cfg.zusatz_pause_minuten} min"
        )
        return ts_cfg.zusatz_pause_minuten * 60.0
    return 0.0

def _boiler_max_info(state):
    """Infos zum harten Boiler-Maximum: (temp, limit, wiederein, fuehler).

    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
    """
    cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if cfg is None:
        return None, None, None, "unten"
    fuehler = getattr(cfg, "boiler_max_fuehler", None) or "unten"
    temp = getattr(getattr(state, "sensors", None), f"t_{fuehler}", None)
    if temp is None or not isinstance(temp, (int, float)):
        return None, None, None, fuehler
    limit = float(getattr(cfg, "max_temp_c", 48.0))
    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
    return temp, limit, wiederein, fuehler


async def handle_compressor_off(
    state, session, regelfuehler, ausschaltpunkt, min_laufzeit,
    t_oben, set_kompressor_status_func: Callable, regel_name=None
):
    """Prueft Abschaltbedingungen und schaltet aus.

    regel_name: Name der Gewinner-Regel, die explizit AUS entschieden hat.
    None heisst: gar keine Regel aktiv (z.B. wegen Nachtsperre). Dient der
    sauberen Trennung in Log und Blocking-Reason - frueher lief beides unter
    "Keine Regel aktiv" und verschleierte die eigentliche Entscheidung."""
    if not state.control.kompressor_ein:
        return False

    # Absolute Sicherheitsgrenze
    if t_oben is not None and t_oben >= state.priority_config.sicherheit.ueberhitzung_c:
        if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
            state.control.blocking_reason = f"Ueberhitzungsschutz ({t_oben:.1f}C >= {state.priority_config.sicherheit.ueberhitzung_c}C)"
            logging.warning(f"SICHERHEIT AUS: Ueberhitzung ({t_oben:.1f}C)")
            return True
        await handle_critical_compressor_error(session, state, "bei Ueberhitzung")
        return False

    # Hartes Boiler-Maximum am Bezugsfuehler (Standard: unten).
    # Bricht die Mindestlaufzeit - Schutz geht vor Taktschutz. Ohne diesen
    # Bruch heizt der Kompressor nach Erreichen des Limits weiter und treibt
    # v.a. die obere Schicht unnoetig weiter hoch.
    t_max, limit, wiederein, fuehler = _boiler_max_info(state)
    if t_max is not None and t_max >= limit:
        if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
            state.control.boiler_max_blockiert = wiederein
            state.control.blocking_reason = (
                f"Boiler-Maximum ({fuehler} {t_max:.1f}C >= {limit:.1f}C)"
            )
            logging.warning(
                f"BOILERMAX AUS: {fuehler} {t_max:.1f}C >= {limit:.1f}C - "
                f"Mindestlaufzeit gebrochen, Freigabe erst <= {wiederein:.1f}C"
            )
            return True
        await handle_critical_compressor_error(session, state, "bei Boiler-Maximum")
        return False

    # --- NEU: Keine Regel aktiv -> Kompressor ausschalten ---
    # Wenn keine Regel den Kompressor einschalten will (z.B. wegen Nachtsperre),
    # muss der Kompressor ausgeschaltet werden, auch wenn der regelfuehler
    # noch unter dem ausschaltpunkt liegt.
    should_on = getattr(state.control, '_soll_einschalten', False)
    if not should_on:
        # Zwei Faelle, die hier sauber getrennt werden: Eine Regel hat explizit
        # AUS entschieden (regel_name gesetzt) ODER gar keine Regel ist aktiv
        # (regel_name None, z.B. Nachtsperre). Verhalten identisch, Text ehrlich.
        if regel_name is not None:
            kontext = f"Regel '{regel_name}' sagt AUS"
        else:
            kontext = "Keine Regel aktiv"

        # Pruefe ob wir schon laenger als die Mindestlaufzeit laufen
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(f"{kontext}: Kompressor AUS. Laufzeit: {elapsed}")
                return True
        else:
            remaining_min = int((min_laufzeit - elapsed).total_seconds() // 60)
            state.control.blocking_reason = (
                f"{kontext}, warte auf Mindestlaufzeit (noch {remaining_min}m)"
            )
            throttle_key = ("log_min_laufzeit_regel_aus" if regel_name is not None
                            else "log_min_laufzeit_keine_regel")
            if check_log_throttle(state, throttle_key, interval_minutes=5):
                logging.info(
                    f"{kontext}, aber Mindestlaufzeit noch nicht erreicht. "
                    f"Laufzeit: {elapsed}"
                )
        return False

    # Regel-basiertes Ausschalten
    if regelfuehler is not None and regelfuehler >= ausschaltpunkt:
        elapsed = safe_timedelta(datetime.now(state.local_tz), state.stats.last_compressor_on_time, state.local_tz)
        if elapsed >= min_laufzeit:
            if await set_kompressor_status_func(state, False, force=True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                logging.info(
                    f"Regel AUS: Regelfuehler ({regelfuehler:.1f}) >= Ziel ({ausschaltpunkt:.1f}). "
                    f"Laufzeit: {elapsed}"
                )
                return True
            await handle_critical_compressor_error(session, state, "")
        else:
            remaining_min = int((min_laufzeit - elapsed).total_seconds() // 60)
            state.control.blocking_reason = f"Warte auf Mindestlaufzeit (noch {remaining_min}m)"
            if check_log_throttle(state, "log_min_laufzeit_off", interval_minutes=5):
                logging.info(
                    f"Abschaltwunsch unterdrueckt: Mindestlaufzeit noch nicht erreicht. "
                    f"Laufzeit: {elapsed}"
                )
    return False


async def handle_compressor_on(
    state, session, regelfuehler, einschaltpunkt, ausschaltpunkt,
    min_laufzeit, min_pause, t_oben,
    set_kompressor_status_func: Callable
):
    """Prueft Einschaltbedingungen und schaltet ein."""
    now = datetime.now(state.local_tz)

    # Boiler-Maximum-Kuehlphase: Nur nach einem tatsaechlichen Limit-Abschalten
    # aktiv (Flag boiler_max_blockiert). Der normale EIN-Bereich unterhalb des
    # Limits bleibt unangetastet - bewusst KEINE pauschale Hysterese, damit
    # z.B. PV-Heizen bei unten 47 C weiter moeglich bleibt.
    schwelle = getattr(state.control, "boiler_max_blockiert", None)
    if schwelle is not None:
        t_max, _limit, _wiederein, fuehler = _boiler_max_info(state)
        if t_max is not None and t_max > schwelle:
            state.control.blocking_reason = (
                f"Boiler-Maximum-Kuehlphase ({fuehler} {t_max:.1f}C, "
                f"Einschalten erst <= {schwelle:.1f}C)"
            )
            return False
        state.control.boiler_max_blockiert = None  # abgekuehlt -> freigegeben
    # Taktschutz (Punkt D): Bei zu vielen Wechseln zusaetzliche Pause
    _ts_cfg = getattr(state, "priority_config", None)
    takt_pause = _taktschutz_blockiert(state, _ts_cfg)
    if takt_pause > 0 and min_pause.total_seconds() < takt_pause:
        min_pause_orig = min_pause
        min_pause = timedelta(seconds=takt_pause)
        logging.info(
            f"Taktschutz verlaengert Pause von {min_pause_orig.total_seconds()/60:.0f} "
            f"auf {takt_pause/60:.0f} min"
        )

    # Explizite Neustartsperre (z.B. nach Verifizierungsfehler): blockiert
    # VOR der Mindestpausen-Pruefung, damit der Grund eindeutig im Log steht.
    lockout_until = getattr(state.control, 'restart_lockout_until', None)
    if lockout_until is not None and now < lockout_until:
        rest = lockout_until - now
        mins = int(rest.total_seconds() // 60)
        secs = int(rest.total_seconds() % 60)
        state.control.blocking_reason = f"Neustartsperre (noch {mins}m {secs}s)"
        return False

    # Die Prioritaeten-Engine hat bereits entschieden
    # Wir muessen nur noch Mindestlaufzeit/-pause und Basis-Sicherheit pruefen
    
    pause_ok = True
    pause_remaining = None
    if state.stats.last_compressor_off_time:
        elapsed_pause = safe_timedelta(now, state.stats.last_compressor_off_time, state.local_tz)
        if elapsed_pause < min_pause:
            pause_ok = False
            pause_remaining = min_pause - elapsed_pause

    # Nur pruefen ob der Regelfuehler der aktiven Regel ueber Ausschaltpunkt liegt.
    # t_oben wird hier NICHT geprueft, da:
    #   1. check_safety_limits() bereits t_oben >= max_temp_c / ueberhitzung_c abfaengt
    #   2. Der Boiler stratifiziert ist - t_oben kann hoch sein waehrend unten noch kalt ist.
    stop_condition = (
        regelfuehler is not None and regelfuehler >= ausschaltpunkt
    )
    
    if not state.control.kompressor_ein:
        # Pruefe ob die Regel einschalten will (ueber state oder Aufruf-Parameter)
        should_on = getattr(state.control, '_soll_einschalten', False)
        
        if should_on and pause_ok:
            if stop_condition:
                logging.info(
                    f"Einschalten unterdrueckt: Regelfuehler ({regelfuehler:.1f}) >= "
                    f"Ausschaltpunkt ({ausschaltpunkt:.1f})"
                )
                state.control.blocking_reason = "Zieltemp erreicht"
                return False
            
            if await set_kompressor_status_func(state, True, t_boiler_oben=t_oben):
                state.control.blocking_reason = None
                state.control.restart_lockout_until = None  # Sperre erledigt
                logging.info(
                    f"Eingeschaltet um {now}. "
                    f"Grund: Regel-Einschalt (Regelfuehler={regelfuehler}, Ziel={einschaltpunkt})"
                )
                return True
    
    # Blocking-Reason setzen wenn Bedingungen nicht erfuellt
        # Nur setzen, wenn ueberhaupt eine Regel einschalten will (sonst sinnlose Warnung)
        if not state.control.kompressor_ein:
            should_on = getattr(state.control, '_soll_einschalten', False)
            if should_on and not pause_ok and pause_remaining:
                minutes = int(pause_remaining.total_seconds() // 60)
                seconds = int(pause_remaining.total_seconds() % 60)
                state.control.blocking_reason = f"Min. Pause (noch {minutes}m {seconds}s)"
            elif not should_on:
                state.control.blocking_reason = None
    
    return False


async def handle_mode_switch(
    state, session, t_oben, t_mittig,
    set_kompressor_status_func: Callable
):
    """
    Prueft ob bei Regelwechsel der Kompressor ausgeschaltet werden sollte.
    Wird nur bei Wechsel von EIN->EIN (andere Regel) relevant.
    """
    if not state.control.kompressor_ein:
        return False
    
    # Bei Prioritaeten-System: Der neue Regel-Zyklus bestimmt automatisch
    # ob weiterlaufen soll. Kein aktives Ausschalten noetig bei Moduswechsel.
    # Das handle_compressor_off uebernimmt die Temp-Logik.
    return False


async def check_safety_limits(session, state, t_oben, t_unten, t_mittig, t_verd, set_kompressor_status_func: Callable):
    """
    Erweiterte Sicherheitspruefungen basierend auf der JSON-Config.
    Prueft nur Ueberhitzung.

    Das harte Boiler-Maximum (max_temp_c am Bezugsfuehler boiler_max_fuehler,
    Standard unten) wird separat in handle_compressor_off/-on erzwungen und
    bricht dort die Mindestlaufzeit. Hier bleibt es bei der Warnung fuer
    t_oben: Die obere Schichtung darf das Limit naturgemaeß uebersteigen,
    solange der Bezugsfuehler darunter liegt.
    """
    cfg = state.priority_config.sicherheit
    
    # 1. Ueberhitzungsschutz (einzige harte Abschaltung)
    if t_oben is not None and t_oben >= cfg.ueberhitzung_c:
        state.control.blocking_reason = f"UEBERHITZUNG ({t_oben:.1f}C >= {cfg.ueberhitzung_c}C)"
        if state.control.kompressor_ein:
            logging.critical(f"UEBERHITZUNG: {t_oben:.1f}C - Sofort-Abschaltung!")
            await set_kompressor_status_func(state, False, force=True)
        return False
    
    # 2. Max-Temperatur nur als Warnung (kein Abschalten, Schwellwert: max_temp_c + 2)
    max_temp_warn = cfg.max_temp_c + 2.0
    if t_oben is not None and t_oben >= max_temp_warn:
        if check_log_throttle(state, "log_max_temp_warn", interval_minutes=5):
            logging.warning(f"Temperatur ueber Normalbereich: {t_oben:.1f}C >= {max_temp_warn}C (kein Abschalten)")
    
    return True


def get_priority_control_status(state) -> dict:
    """Gibt einen Status-Report der Prioritaeten-Steuerung zurueck."""
    cfg = state.priority_config
    
    pv_leistung = state.solar.feedinpower if state.solar.feedinpower else 0.0
    
    return {
        "wp_leistung_watt": cfg.wp.leistung_watt,
        "wp_typ": cfg.wp.typ,
        "pv_leistung_watt": pv_leistung,
        "aktive_regel": getattr(state.control, 'active_rule_name', None),
        "sensoren": state.control.active_rule_sensor,
        "nachtsperre_aktiv": _is_nachtsperre_aktiv(cfg, datetime.now(state.local_tz)),
        "komfort_aktiv": getattr(state.control, 'komfort_aktiv', False),
        "anzahl_regeln": len(cfg.pv_regeln) + 4 + 3,  # +Wochenende+PV+Komfort+Zeitfenster+Abweichung+Forecast+AdaptivePV+CalcStart
    }


def _is_nachtsperre_aktiv(cfg: WPSteuerungConfig, now: datetime) -> bool:
    """Prueft ob die Nachtsperre aktiv ist."""
    start = cfg.sicherheit.nachtsperre_start
    ende = cfg.sicherheit.nachtsperre_ende
    h = now.hour
    if start <= ende:
        return start <= h < ende
    return h >= start or h < ende

