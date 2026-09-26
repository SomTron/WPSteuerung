"""Simulations-Harness: treibt die ECHTE Regelungslogik ueber mehrere Tage.

Der Harness importiert ``priority_control_logic``/``priority_control``/
``learning_engine`` aus ``Steuerung/`` und rukt dieselben Funktionen auf,
die auch der 10-Sekunden-Hauptloop aufruft:

    check_safety_limits -> determine_mode_and_setpoints
                        -> handle_compressor_on / handle_compressor_off

Hardware, Telegram, Solax-API und Datei-Logging werden durch In-Memory-
Attrappen ersetzt. Zeit, Sensoren, PV und Batterie kommen aus dem
Speichermodell bzw. dem Szenario.
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import types
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz

_HERE = os.path.dirname(os.path.abspath(__file__))
_STEUERUNG = os.path.abspath(os.path.join(_HERE, "..", "..", "Steuerung"))
if _STEUERUNG not in sys.path:
    sys.path.insert(0, _STEUERUNG)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from thermal import Speicher, SpeicherParameter  # noqa: E402
from umgebung import (  # noqa: E402
    SEPTEMBER_PV,
    Batterie,
    BatterieParameter,
    Szenario,
    hauslast_w,
)

TZ = pytz.timezone("Europe/Berlin")

#: Zeitschritt des simulierten Hauptloops. 30 s statt 10 s: zehnmal schneller
#: als der Realbetrieb, aber fein genug, dass Debounce (2 Takte),
#: Mindestlaufzeit und PV-Weiterlauf-Band korrekt aufgeLoest werden.
SCHRITT_SEKUNDEN = 30.0


def lade_steuerung():
    """Importiert die Produktionsmodule mit RPi-/Telegram-Attrappen."""
    # Hardware-Module abfangen, bevor die Produktionsmodule sie importieren.
    for name in ("RPi", "RPi.GPIO", "smbus2", "RPLCD", "RPLCD.i2c",
                 "w1thermsensor", "RPi.w1thermsensor"):
        if name not in sys.modules:
            sys.modules[name] = types.ModuleType(name)
    sys.modules["RPi"].GPIO = sys.modules["RPi.GPIO"]
    sys.modules["RPi.GPIO"].BCM = "BCM"
    sys.modules["RPi.GPIO"].OUT = "OUT"
    sys.modules["RPi.GPIO"].IN = "IN"
    sys.modules["RPi.GPIO"].HIGH = 1
    sys.modules["RPi.GPIO"].LOW = 0

    import priority_control_logic as pcl  # noqa: E402
    import learning_engine as le_mod  # noqa: E402
    import entscheidungs_log  # noqa: E402
    import legionellen_plan  # noqa: E402

    # Simulations-Logging auf WARN stilllegen: die Regelung loggt im
    # 30-s-Takt, das wuerde die Ausgabe unbrauchbar machen. Warnungen
    # (Boiler-Max, Komfort) bleiben sichtbar.
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    for name in ("priority_control_logic", "priority_control", "learning_engine",
                 "safety_logic", "root"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.ERROR)
    return pcl, le_mod, entscheidungs_log, legionellen_plan


def _zahl(wert, standard: float = 0.0) -> float:
    """Robuste Zahl-Konvertierung (Sensoren koennen None liefern)."""
    if isinstance(wert, bool) or wert is None:
        return standard
    try:
        return float(wert)
    except (TypeError, ValueError):
        return standard


class SimClock:
    """Simulierte Uhr, die ``clock.now()`` liefert (``now_for``-Vertrag)."""

    def __init__(self, start: datetime):
        self._jetzt = start
        self._mono = 0.0

    def now(self) -> datetime:
        return self._jetzt

    def monotonic(self) -> float:
        return self._mono

    def setze(self, wert: datetime) -> None:
        self._jetzt = wert
        self._mono += 1.0


def baue_state(pcl, priority_config, clock: SimClock, lernpfad: str):
    """Baut einen State, der die von der Logik erwartete Struktur hat."""
    from state import ControlState, SensorsState, SolarState, StatsState

    class FakeConfig:
        """Minimaler INI-Ersatz (Werte wie im Projekt, siehe config.ini.example)."""

        class Heizungssteuerung:
            MIN_LAUFZEIT = 60
            MIN_PAUSE = 30
            VERDAMPFERTEMPERATUR = 6.0
            VERDAMPFER_RESTART_TEMP = 9.0
            SICHERHEITS_TEMP = 58.0
            AUSSCHALTPUNKT = 48.0
            EINSCHALTPUNKT = 42.0

        class Urlaubsmodus:
            URLAUBSABSENKUNG = 6.0

        class Telegram:
            BOT_TOKEN = ""
            CHAT_ID = ""

    state = types.SimpleNamespace()
    state.local_tz = TZ
    state.clock = clock
    state.config = FakeConfig
    state.priority_config = priority_config
    state.sensors = SensorsState()
    state.solar = SolarState()
    state.control = ControlState(FakeConfig)
    state.stats = StatsState(clock.now())
    state._cycle_log = None
    state.energy_source = "Netz"
    state.urlaubsmodus_aktiv = False
    state.bademodus_aktiv = False
    state.sommer_modus_aktiv = False
    state.sommer_modus_zaehler = 0
    state.sommer_letzter_bewertungstag = None
    state.legionellen_aktiv = False
    state.legionellen_last_done = None
    state.legionellen_started_at = None
    state.legionellen_target_reached_at = None
    state.legionellen_planned_date = None
    state.legionellen_planned_tag = None
    state.legionellen_temp_override = None
    state.legionellen_wochennummer = None
    state.forecast_stale = False
    state.forecast_age_s = 0
    state.last_forecast_update = clock.now()
    state.verdampfer_blocked = False
    state.verdampfer_shutdowns = []
    state.kompressor_verification_start_time = None
    state.kompressor_verification_start_t_verd = None
    state.kompressor_verification_start_t_unten = None
    state.kompressor_verification_failed = False
    state.kompressor_verification_error_count = 0
    state.kompressor_verification_last_check = None
    state.learning_engine = None
    return state


class Zyklus:
    """Ein abgeschlossener Kompressorlauf."""

    __slots__ = ("start", "ende", "regel", "quelle", "end_grund", "dauer_min",
                 "start_unten", "max_unten", "max_oben", "max_mitte",
                 "energie_wh", "pv_wh", "netz_wh")

    def __init__(self, start, regel, quelle):
        self.start = start
        self.ende = None
        self.regel = regel
        self.quelle = quelle
        self.end_grund = ""
        self.dauer_min = 0.0
        self.start_unten = 0.0
        self.max_unten = 0.0
        self.max_oben = 0.0
        self.max_mitte = 0.0
        self.energie_wh = 0.0
        self.pv_wh = 0.0
        self.netz_wh = 0.0


class Simulation:
    """Mehrtages-Simulation der Wärmepumpen-Regelung."""

    def __init__(self, szenario: Szenario, parameter: Optional[SpeicherParameter] = None,
                 schritt_sekunden: float = SCHRITT_SEKUNDEN, seed: Optional[int] = None,
                 lernpfad: Optional[str] = None, config_patch: Optional[dict] = None):
        self.sz = szenario
        self.dt = schritt_sekunden
        self.rnd = random.Random(seed if seed is not None else szenario.seed)
        self.pcl, self.le_mod, self.entscheidungs_log, self.legionellen_plan = lade_steuerung()

        p = parameter or SpeicherParameter()
        p.umgebung_c = szenario.umgebung_c
        self.speicher = Speicher(p, 41.0, 42.0, 43.5)
        self.batterie = Batterie(
            BatterieParameter(
                kapazitaet_wh=szenario.batterie_kapazitaet_wh,
                soc_start_prozent=szenario.soc_start_prozent,
                soc_max_prozent=szenario.soc_max_prozent,
                nacht_entladung_anteil=szenario.nacht_entladung_anteil,
            )
        ) if szenario.batterie else None
        self.start = TZ.localize(datetime.combine(szenario.start, datetime.min.time()))
        self.clock = SimClock(self.start)

        # Die echte JSON-Fachkonfiguration laden (wp_steuerung_parameter.json)
        from json_config import WPSteuerungConfigManager
        mgr = WPSteuerungConfigManager()
        mgr.load_config()
        self.priority_config = mgr.config
        self.config_patch = config_patch or {}
        self._wende_config_an()
        self.config_patch = config_patch or {}

        self.state = baue_state(self.pcl, self.priority_config, self.clock, lernpfad)
        self.lern_engine = self.le_mod.LearningEngine(
            data_path=lernpfad or os.path.join(tempfile.gettempdir(), "wpsim_lern.json")
        )
        self.state.learning_engine = self.lern_engine
        # Bestehender Betrieb: letzter Legionellen-Lauf liegt in der Vergangenheit
        if szenario.legionellen_vor_tagen > 0:
            self.state.legionellen_last_done = (
                self.start - timedelta(days=szenario.legionellen_vor_tagen)
            ).date()
        self.zyklen: List[Zyklus] = []
        self._lauf: Optional[Zyklus] = None
        self.zeitreihe: List[dict] = []
        self.legionellen_kw = None
        self.netz_je_tag: Dict = {}

        # Zapf-Budget: schrittgroessenunabhaengige Rate (7.5 Ereignisse/Tag,
        # gemessen im September-Log: 181 Cluster in 23 Tagen).
        self._zapfereignisse_je_tag = 7.5
        self._schritte_pro_tag = max(1.0, 86400.0 / self.dt)
        self._zapf_tag = None
        self._zapf_budget = 0.0

        # Diagnose-Zaehler
        self.diag_blockiert_h: Dict[str, float] = defaultdict(float)
        self.diag_regel_minuten: Dict[str, float] = {}
        self.diag_laufzeit_h = 0.0
        self.diag_netz_waermer_h = 0.0
        self.diag_pv_vergeblich_wh = 0.0
        self.diag_pv_vergeblich_ohne_wunsch_h = 0.0
        self._letzter_gewinner = None
        self._warte_grund = None
        self.legionellen_hinweis = ""

        # KPI-Zaehler
        self.pv_erzeugt_wh = 0.0
        self.pv_eigenverbraucht_wh = 0.0
        self.pv_eingespeist_wh = 0.0
        self.netzbezug_wh = 0.0
        self.wp_energie_wh = 0.0
        self.wp_laufzeit_s = 0.0
        self.komfortverletzungen = 0
        self.kalt_wasser_tage = 0
        self.boiler_max_ereignisse = 0
        self.legionellen_fertig = False
        self.waerme_zapf_wh = 0.0

    def _wende_config_an(self) -> None:
        """Wendet ``config_patch`` auf die geladene Fachkonfiguration an.

        Erlaubt ``pfad -> wert`` pro Sektion, z. B.
        ``{"abweichung": {"netz_notfall_offset_k": 4.0}}``. Die Aenderung
        betrifft nur diese Simulationsinstanz - die Projektdatei bleibt
        unberuehrt.
        """
        for sektion, werte in self.config_patch.items():
            ziel = getattr(self.priority_config, sektion, None)
            if ziel is None:
                continue
            for name, wert in werte.items():
                if hasattr(ziel, name):
                    setattr(ziel, name, wert)

    # ------------------------------------------------------------------
    # PV-Erzeugung
    # ------------------------------------------------------------------
    def pv_leistung(self, jetzt: datetime) -> float:
        """PV-Erzeugung in Watt fuer den Zeitpunkt."""
        if self.sz.pv_faktor <= 0:
            return 0.0
        stunde = jetzt.hour
        basis = SEPTEMBER_PV.get(stunde, 0.0) * self.sz.pv_faktor
        if self.sz.pv_streuung > 0 and basis > 0:
            # Wolkenmodulation: stundenweise, aber mit Tagesgang-Versatz,
            # damit die Einspeisung nicht kuenstlich glatt wirkt.
            versatz = jetzt.day % 7
            rauschen = 1.0 + self.sz.pv_streuung * (
                math.sin(jetzt.hour * 1.7 + versatz) * 0.6
                + self.rnd.uniform(-0.4, 0.4)
            )
            basis *= max(0.0, rauschen)
        return max(0.0, basis)

    # ------------------------------------------------------------------
    # Zapfungen
    # ------------------------------------------------------------------
    def _zapfplan(self, jetzt: datetime) -> float:
        """Warmwasser-Zapfvolumen in Litern fuer den Zeitpunkt.

        Nachgebildet am September-Log: 181 Zapfcluster in 23 Tagen
        (~7.5/Tag, Median 3.3 K Abfall am unteren Fuehler), mit einem
        klaren Abend-Peak zwischen 17 und 20 Uhr.
        """
        if jetzt.date() != self._zapf_tag:
            self._zapf_tag = jetzt.date()
            self._zapf_budget = 0.0
        self._zapf_budget += self._zapfereignisse_je_tag / self._schritte_pro_tag
        if self._zapf_budget < 1.0:
            return 0.0
        self._zapf_budget -= 1.0
        h = jetzt.hour
        # Abend-Peak staerker gewichten
        if 17 <= h <= 20:
            menge = self.rnd.gauss(7.5, 3.0)
        elif 6 <= h <= 9:
            menge = self.rnd.gauss(3.0, 1.5)
        elif 11 <= h <= 15:
            menge = self.rnd.gauss(4.0, 2.0)
        else:
            menge = self.rnd.gauss(2.0, 1.0)
        menge = max(0.5, min(menge, 18.0))
        # Ein Ereignis dauert typisch 2-6 Minuten -> ueber die Minuten verteilen
        return menge * (self.dt / 60.0) / 4.0

    # ------------------------------------------------------------------
    # Hardware-Attrappe
    # ------------------------------------------------------------------
    async def _set_kompressor(self, state, status: bool, force: bool = False,
                              t_boiler_oben=None, end_grund: str = None) -> bool:
        """Minimaler Ersatz fuer ``main.set_kompressor_status``.

        Signatur wie im Produktivcode (state zuerst), damit die echten
        Aufrufstellen in ``priority_control_logic`` unveraendert bleiben.
        Uebernimmt genau die State-Aenderungen, auf die sich die Regelung
        verlaesst (Zeitstempel, Zyklus-Id, Laufzeit, blocking_reason).
        """
        state = self.state
        jetzt = self.clock.now()
        war_ein = state.control.kompressor_ein
        if status:
            if war_ein:
                return True
            state.control.kompressor_ein = True
            state.control.zyklus_id = getattr(state.control, "zyklus_id", 0) + 1
            state.stats.last_compressor_on_time = jetzt
            state.kompressor_verification_start_time = jetzt
            state.kompressor_verification_start_t_verd = state.sensors.t_verd
            state.kompressor_verification_start_t_unten = state.sensors.t_unten
            regel = (getattr(state.control, "_pending_start_rule", None)
                     or getattr(state.control, "requested_rule_name", None)
                     or getattr(state.control, "previous_modus", None)
                     or "?")
            quelle = getattr(state.control, "_pending_start_source", None) or "Netz"
            state.control._pending_start_rule = None
            state.control._pending_start_source = None
            self._lauf = Zyklus(jetzt, regel, quelle)
            self._lauf.start_unten = _zahl(state.sensors.t_unten)
            self._lauf.max_unten = _zahl(state.sensors.t_unten)
            self._lauf.max_oben = _zahl(state.sensors.t_oben)
            self._lauf.max_mitte = _zahl(state.sensors.t_mittig)
            return True
        if not war_ein:
            return True
        state.control.kompressor_ein = False
        state.stats.last_compressor_off_time = jetzt
        state.control.effective_rule_name = None
        state.control.active_rule_name = None
        state.control.effective_source = None
        state.control._lauf_start_regel = None
        state.control.source_at_start = None
        lauf = self._lauf
        if lauf is not None:
            lauf.ende = jetzt
            lauf.dauer_min = (jetzt - lauf.start).total_seconds() / 60.0
            lauf.end_grund = end_grund or state.control.blocking_reason or "unbekannt"
            lauf.max_unten = max(lauf.max_unten, _zahl(state.sensors.t_unten))
            lauf.max_oben = max(lauf.max_oben, _zahl(state.sensors.t_oben))
            lauf.max_mitte = max(lauf.max_mitte, _zahl(state.sensors.t_mittig))
            self.zyklen.append(lauf)
            self._lauf = None
        if end_grund == "boiler_max":
            self.boiler_max_ereignisse += 1
        return True

    async def _sicherheitspruefung(self) -> bool:
        """Entspricht main.run_logic_step Schritt 3 (check_safety_limits)."""
        return await self.pcl.check_safety_limits(
            None, self.state, self.state.sensors.t_oben, self.state.sensors.t_unten,
            self.state.sensors.t_mittig, self.state.sensors.t_verd,
            self._set_kompressor,
        )

    async def _logikschritt(self) -> None:
        """Ein Durchlauf der Produktions-Regelung (Schritte 4 und 5)."""
        state = self.state
        result = await self.pcl.determine_mode_and_setpoints(
            state, state.sensors.t_unten, state.sensors.t_mittig,
            learning_engine=self.lern_engine,
        )
        state.control._soll_einschalten = bool(result.get("soll_einschalten", False))
        gewinner = result.get("gewinner_ergebnis")
        gewinner_name = gewinner.name if gewinner is not None else None
        self._letzter_gewinner = gewinner_name
        self._warte_grund = None
        for e in result.get("alle_ergebnisse", []):
            code = getattr(e, "reason_code", "")
            if e.aktiv and e.einschalten is None and code == "waiting_source":
                self._warte_grund = f"wartet_quelle:{e.name}"
                break
        regelfuehler = result["regelfuehler"]
        ausschaltpunkt = state.control.aktueller_ausschaltpunkt
        einschaltpunkt = state.control.aktueller_einschaltpunkt
        if state.control.kompressor_ein:
            await self.pcl.handle_compressor_off(
                state, None, regelfuehler, ausschaltpunkt,
                self.min_laufzeit, state.sensors.t_oben, self._set_kompressor,
                regel_name=gewinner_name,
            )
        else:
            await self.pcl.handle_compressor_on(
                state, None, regelfuehler, einschaltpunkt, ausschaltpunkt,
                self.min_laufzeit, self.min_pause, state.sensors.t_oben,
                state.sensors.t_mittig, self._set_kompressor,
            )
        await self._legionellen_lifecycle(result)

    async def _legionellen_lifecycle(self, result) -> None:
        """Spiegelt ``main._aktualisiere_legionellen_lifecycle`` ohne Telegram."""
        state = self.state
        cfg = state.priority_config.legionellen
        if not cfg.aktiv:
            return
        jetzt = self.clock.now()
        gewinner = result.get("gewinner_ergebnis")
        if not state.legionellen_aktiv and state.legionellen_temp_override is not None:
            state.legionellen_temp_override = None
        if gewinner is None or gewinner.name != "Legionellen":
            return
        if (gewinner.einschalten is True and not state.legionellen_aktiv
                and state.control.kompressor_ein
                and getattr(state.control, "_lauf_start_regel", None) == "Legionellen"):
            state.legionellen_aktiv = True
            state.legionellen_started_at = jetzt
            state.legionellen_target_reached_at = None
            state.legionellen_temp_override = cfg.legionellen_max_temp_c
            state.control.requested_rule_name = "Legionellen"
            state.control.effective_rule_name = "Legionellen"
            return
        if not state.legionellen_aktiv:
            return
        t_unten = state.sensors.t_unten
        if (isinstance(t_unten, (int, float)) and t_unten >= cfg.target_temp_c
                and state.legionellen_target_reached_at is None):
            state.legionellen_target_reached_at = jetzt
        if gewinner.einschalten is not False or state.legionellen_target_reached_at is None:
            return
        gehalten = jetzt - state.legionellen_target_reached_at
        if gehalten < timedelta(minutes=int(cfg.probezeit_minuten)):
            return
        state.legionellen_last_done = jetzt.date()
        state.legionellen_aktiv = False
        state.legionellen_temp_override = None
        state.legionellen_started_at = None
        state.legionellen_target_reached_at = None
        state._last_was_legionellen = True
        self.legionellen_fertig = True
        self.legionellen_kw = jetzt.isocalendar()[1]

    # ------------------------------------------------------------------
    # Sensoren und Solar-Daten in den State spielen
    # ------------------------------------------------------------------
    def _sensoren_setzen(self) -> None:
        s = self.speicher
        self.state.sensors.t_unten = round(s.unten.temperatur, 3)
        self.state.sensors.t_mitte = round(s.mitte.temperatur, 3)
        self.state.sensors.t_oben = round(s.oben.temperatur, 3)
        self.state.sensors.t_boiler = self.state.sensors.t_oben
        # Verdampfer: faellt bei Heizbetrieb, erholt sich im Leerlauf
        verd = getattr(self.state.sensors, "t_verd", None)
        if verd is None:
            verd = 18.0
        if self.state.control.kompressor_ein:
            verd -= 0.30
        else:
            verd += 0.12
        self.state.sensors.t_verd = max(2.0, min(45.0, verd))

    def _solar_setzen(self, jetzt: datetime, pv_w: float, einspeisung_w: float,
                      entladung_w: float, ladung_w: float) -> None:
        solar = self.state.solar
        solar.acpower = round(pv_w, 1)
        solar.feedinpower = round(einspeisung_w, 1)
        # Solax-Konvention: batpower > 0 = Entladung, < 0 = Ladung
        solar.batpower = round(entladung_w - ladung_w, 1)
        solar.battery_discharge_watt = round(entladung_w, 1)
        solar.battery_charge_watt = round(ladung_w, 1)
        solar.soc = round(self.batterie.soc, 1) if self.batterie else None
        solar.last_api_call = jetzt
        solar.energy_source = "Netz"
        # Forecast: konstant, aber "frisch" (Update wie im Betrieb)
        solar.forecast_today = self.sz.forecast_wh
        solar.forecast_tomorrow = self.sz.forecast_wh_morgen
        solar.forecast_day2 = self.sz.forecast_wh_uebermorgen
        solar.forecast_hourly_wm2 = {
            h: SEPTEMBER_PV.get(h, 0.0) * self.sz.pv_faktor * 0.19
            for h in range(24)
        }
        self.state.last_forecast_update = jetzt
        self.state.forecast_stale = False

    def _sommer_modus(self, jetzt: datetime) -> None:
        """Bewertet Sommer-Modus und Legionellen-Tagesplan (einmal pro Tag).

        Spiegelt ``main.check_periodic_tasks``: der Legionellen-Tag wird auf
        den Wochentag mit der besten *tatsaechlichen* Prognose geplant.
        """
        from logic_utils import evaluate_sommer_modus

        cfg = self.state.priority_config.sommer_modus
        if not cfg.aktiv:
            return
        zaehler, aktiv, tag, _ereignis = evaluate_sommer_modus(
            benoetigte_tage=cfg.benoetigte_tage,
            mindest_prognose_wh=cfg.mindest_prognose_wh,
            rad_today=self.sz.forecast_wh,
            rad_tomorrow=self.sz.forecast_wh_morgen,
            rad_day2=self.sz.forecast_wh_uebermorgen,
            heute=jetzt.date(),
            aktueller_zaehler=self.state.sommer_modus_zaehler,
            ist_aktiv=self.state.sommer_modus_aktiv,
            letzter_bewertungstag=self.state.sommer_letzter_bewertungstag,
        )
        self.state.sommer_modus_zaehler = zaehler
        self.state.sommer_modus_aktiv = aktiv
        self.state.sommer_letzter_bewertungstag = tag
        self._legionellen_planen(jetzt)

    def _legionellen_planen(self, jetzt: datetime) -> None:
        """Legionellen-Tagesplan (Spiegel von main.check_periodic_tasks)."""
        from priority_control import _wochentag_name

        state = self.state
        cfg = state.priority_config.legionellen
        if not cfg.aktiv:
            return
        aktuelle_kw = jetzt.isocalendar()[1]
        letzte_kw = (state.legionellen_last_done.isocalendar()[1]
                     if state.legionellen_last_done is not None else None)
        if letzte_kw == aktuelle_kw:
            return
        wochentag = jetzt.weekday()
        verfuegbar = [
            tag for tag in range(cfg.bevorzugter_tag, cfg.letzter_tag + 1)
            if tag >= wochentag
            and not (tag == wochentag and jetzt.hour > int(cfg.spaeteste_start_uhr))
        ]
        if not verfuegbar:
            self.legionellen_hinweis = "Kein weiterer Legionellen-Tag in dieser Woche"
            return
        tages_prognose = {
            wochentag: self.sz.forecast_wh,
            (wochentag + 1) % 7: self.sz.forecast_wh_morgen,
            (wochentag + 2) % 7: self.sz.forecast_wh_uebermorgen,
        }
        tages_prognose = {
            tag: p for tag, p in tages_prognose.items()
            if p is not None and tag in verfuegbar
            and p >= cfg.mindest_prognose_wh_qm
            and (tag != cfg.bevorzugter_tag or p >= cfg.erforderliche_wh_qm)
        }
        if not tages_prognose:
            self.legionellen_hinweis = (
                f"Kein belastbarer PV-Tag (mindestens "
                f"{cfg.mindest_prognose_wh_qm:.0f} Wh/m2)"
            )
            return
        bester = max(
            tages_prognose,
            key=lambda tag: (
                tages_prognose[tag],
                tages_prognose[tag] >= cfg.pv_prognose_schwelle_gut,
                tag == cfg.bevorzugter_tag,
            ),
        )
        preferred = tages_prognose.get(cfg.bevorzugter_tag)
        if preferred is not None and (tages_prognose[bester] - preferred
                                      < cfg.tagwechsel_ab_diff_wh_qm):
            bester = cfg.bevorzugter_tag
        delta = (bester - wochentag) % 7
        state.legionellen_planned_day = _wochentag_name(bester)
        state.legionellen_planned_tag = bester
        state.legionellen_planned_time = f"{cfg.start_uhr:02d}:00"
        state.legionellen_planned_date = jetzt.date() + timedelta(days=delta)
        state.legionellen_planned_forecast_wh = float(tages_prognose[bester])
        state.legionellen_planned_reason = (
            f"Bester PV-Tag: {tages_prognose[bester]:.0f} Wh/m2"
        )
        self.legionellen_hinweis = (
            f"geplant {_wochentag_name(bester)} "
            f"({state.legionellen_planned_date}, "
            f"{tages_prognose[bester]:.0f} Wh/m2)"
        )

    def _diagnose(self, jetzt: datetime, dt_h: float, pv_w: float, netz_w: float) -> None:
        """Sammelt, warum der Kompressor ein- oder ausgeschaltet ist.

        Drei Kennzahlen sind entscheidend fuer die Beurteilung der Regelung:
        * ``blockiert``  - Stunden, in denen eine Regel einschalten wollte,
          aber ein Sperrgrund (Mindestlaufzeit, Pause, Antizipation) griff.
        * ``vergebliche_pv`` - Stunden mit >= 1 kW Solarueberschuss, in denen
          die WP trotzdem aus war (verschenkte PV-Wh).
        * ``netzwaermer`` - Stunden, in denen die WP mit Netzstrom lief.
        """
        state = self.state
        ein = state.control.kompressor_ein
        if ein:
            self.diag_laufzeit_h += dt_h
            regel = getattr(state.control, "effective_rule_name", None) or self._letzter_gewinner
            if regel:
                self.diag_regel_minuten[regel] = self.diag_regel_minuten.get(regel, 0.0) + dt_h / 60.0
            if netz_w > 0:
                self.diag_netz_waermer_h += dt_h
            return

        grund = state.control.blocking_reason or ""
        if self._soll_wollte_ein() and grund:
            self.diag_blockiert_h[grund.split("(")[0].strip()] += dt_h
        # PV waere da, die WP liess sie liegen
        if pv_w >= 1000.0:
            self.diag_pv_vergeblich_wh += pv_w * dt_h
            if not self._soll_wollte_ein():
                self.diag_pv_vergeblich_ohne_wunsch_h += dt_h

    def _soll_wollte_ein(self) -> bool:
        return bool(getattr(self.state.control, "_soll_einschalten", False))

    # ------------------------------------------------------------------
    # Hauptschleife
    # ------------------------------------------------------------------
    async def simuliere(self, tage: Optional[int] = None) -> "Simulation":
        """Simuliert ``tage`` Kalendertage Schritt fuer Schritt."""
        zyklus_cfg = self.state.priority_config.zyklus
        self.min_laufzeit = timedelta(minutes=zyklus_cfg.mindestlaufzeit_minuten)
        self.min_pause = timedelta(minutes=zyklus_cfg.mindestpausenzeit_minuten)

        n_tage = tage if tage is not None else self.sz.tage
        end = self.clock.now() + timedelta(days=n_tage)
        dt_h = self.dt / 3600.0
        schritte = int((end - self.clock.now()).total_seconds() / self.dt)
        letzter_tag = None
        kalter_tag = False

        for i in range(schritte):
            jetzt = self.clock.now()
            self.clock.setze(jetzt + timedelta(seconds=self.dt))

            if letzter_tag is not None and jetzt.date() != letzter_tag:
                if kalter_tag:
                    self.kalt_wasser_tage += 1
                kalter_tag = False
                self.state.stats.last_day = jetzt.date()
                self.state.stats.total_runtime_today = timedelta()
                self._sommer_modus(jetzt)
            letzter_tag = jetzt.date()

            # --- Umgebung: PV, Hauslast, Batterie ---
            pv_w = self.pv_leistung(jetzt)
            last_w = hauslast_w(jetzt.hour, self.sz.hauslast_faktor)
            wp_w = self.sz.wp_leistung_w if self.state.control.kompressor_ein else 0.0
            if self.batterie is not None:
                entl_w, lad_w, einsp_w, netz_w = self.batterie.rechne(
                    dt_h, pv_w, last_w, wp_w
                )
            else:
                saldo = pv_w - last_w - wp_w
                entl_w, lad_w = 0.0, 0.0
                einsp_w, netz_w = max(0.0, saldo), max(0.0, -saldo)

            self._solar_setzen(jetzt, pv_w, einsp_w, entl_w, lad_w)
            self._sensoren_setzen()

            # --- Waermebilanz ---
            zapf_l = self._zapfplan(jetzt)
            if zapf_l > 0:
                self.waerme_zapf_wh += -self.speicher.zapfe(zapf_l)
            self.speicher.schritt(self.dt, self.state.control.kompressor_ein)

            # --- Energie-KPIs ---
            self.pv_erzeugt_wh += pv_w * dt_h
            self.pv_eingespeist_wh += max(0.0, einsp_w) * dt_h
            self.pv_eigenverbraucht_wh += max(
                0.0, min(pv_w, last_w + wp_w) - lad_w
            ) * dt_h
            self.netzbezug_wh += max(0.0, netz_w) * dt_h
            if self.state.control.kompressor_ein:
                self.wp_energie_wh += wp_w * dt_h
                self.wp_laufzeit_s += self.dt
                if self._lauf is not None:
                    self._lauf.energie_wh += wp_w * dt_h
                    if netz_w > 0:
                        self._lauf.netz_wh += netz_w * dt_h
                    else:
                        self._lauf.pv_wh += min(pv_w, wp_w) * dt_h

            if self.state.sensors.t_unten < 40.0:
                kalter_tag = True

            # --- Regelung (Produktionslogik) ---
            sicher = await self._sicherheitspruefung()
            if sicher:
                await self._logikschritt()

            # --- Diagnose: warum laeuft der Kompressor nicht? ---
            self._diagnose(jetzt, dt_h, pv_w, netz_w)

            if i % 4 == 0:
                self.zeitreihe.append(dict(
                    t=jetzt,
                    unten=self.state.sensors.t_unten,
                    mitte=self.state.sensors.t_mitte,
                    oben=self.state.sensors.t_oben,
                    k=self.state.control.kompressor_ein,
                    pv=pv_w, netz=netz_w, einspeisung=einsp_w,
                    soc=self.batterie.soc if self.batterie else None,
                    entladung=entl_w, ladung=lad_w,
                    regel=getattr(self.state.control, "active_rule_name", None),
                ))

        if self.state.control.kompressor_ein:
            await self._set_kompressor(self.state, False, end_grund="simulations_ende")
        if kalter_tag:
            self.kalt_wasser_tage += 1
        from report import kpi as _kpi
        self.kennzahlen_basis = _kpi(self)
        return self
