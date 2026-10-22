"""
Self-Learning Engine für die WP-Steuerung.

Lernt aus dem Betrieb:
1. Heizrate (°C/h) – saisonal getrennt (Winter/Übergang/Sommer)
2. Optimale Zielzeit – wann wird abends Wasser gezapft?
3. Persistenz als JSON-Datei
"""
import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict

from utils import to_naive
from logic_utils import saison_von_datum


LEARNING_DATA_FILE = "learning_data.json"

# Geschaetzter Strompreis fuer die Ableitung der "verpassten Ersparnis" aus
# verschenkten PV-Wh nach einem ZU-FRUEH-Event (Empfehlung "WARNINGS anreichern").
ZU_FRUEH_STROMPREIS_EUR_KWH = 0.35


@dataclass
class LearningConfig:
    """Sämtliche konfigurierbaren Lernparameter.

    Wird als Unterfeld von LearningData persistiert, damit Aenderungen
    auch nach einem Neustart erhalten bleiben. Defaults sind die
    empirisch ermittelten Produktionswerte.
    """
    # ── Heizrate ──
    heating_rate_ewma_alpha: float = 0.10
    heating_rate_min_samples: int = 3
    # Mindestdauer eines Zyklus, damit er in die Planungsrate eingeht.
    # Befund aus dem Entscheidungslog 18.09.-01.10.2026: die ueber KURZYKLEN
    # gelernte Rate lag bei 13,86 K/h, die nachhaltige Rate (Kaltstart
    # 26.09., 92 % Laufzeit) aber nur bei 4,73 K/h. Faktor 2,9 - der
    # Startvorlauf des CalcStart fiel damit rund zwei Drittel zu kurz aus.
    # Kurze Zyklen messen die lokale Reaktion des unteren Knotens, nicht
    # das Durchheizen des Speichers.
    heating_rate_lern_min_minuten: float = 30.0
    # Physikalische Obergrenze fuer die Planungsrate: 300-L-Speicher
    # (1254 kJ/K) mit 600 W elektrisch und COP >= 3 ergibt ~5,2 K/h fuer
    # den GANZEN Speicher. Laeuft der untere Knoten sichtbar schneller,
    # ist das ein Lokaleffekt und kein Mass fuer die Startplanung.
    heating_rate_max_c_h: float = 6.0
    # Mindest-Anstieg, damit ein Zyklus in die Planungsrate eingeht. Ohne
    # diese Grenze zaehlten Zyklen, in denen der Fuehler GEFALLEN ist: die
    # Zyklus-Rate wird auf 0,1 K geklemmt und wurde so als "0,54 C/h" bzw.
    # "0,0 C/h" gelernt (Befund 18.10.2026: 11-min-Zyklus mit 0,54 C/h bei
    # fallender Temperatur, ein 1215-min-"Zyklus" mit 0,0 C/h). Bei alpha=0,1
    # senkt ein solcher Eintrag das Lernmittel um 10 %.
    heating_rate_min_anstieg_k: float = 0.5

    # ── Zielzeit (Zapfverhalten) ──
    target_hour_ewma_alpha: float = 0.15
    target_hour_min_samples: int = 3
    max_usage_events: int = 100
    # ── Zapfung: ein Abfallvorgang darf nicht bei jedem 10-s-Sample neu zählen ──
    usage_event_cooldown_min: int = 10
    # Zapfungs-Erkennung (Befund 18.10.2026): Das Ereignis wurde beim ERSTEN
    # Sample ueber der Schwelle abgeschlossen und mit diesem Ein-Sample-Delta
    # gespeichert. Im Betriebslog 14.09.-18.10.2026 hatten dadurch 26 von 27
    # Ereignissen einen gewichteten Abfall < 1 K (die meisten exakt 0,50 K =
    # Schwellwert) - und der 10-min-Cooldown verschluckte danach die ECHTE
    # Zapfung: Am 18.10. wurde um 18:28 "gew. 0,56K" gelernt, waehrend der
    # Speicher von 41,8 auf 22,2 C fiel. Der wert steuerte ausserdem die
    # gelernte Abend-Zielzeit (18,4 h) und damit den CalcStart-Fahrplan.
    #
    # Jetzt wird das Ereignis ueber ein Fenster verfolgt (maximaler Abfall ab
    # Beginn), und nur PLAUSIBLE Vorgaenge fliessen in die Zielzeit ein:
    # staerkster Abfall >= usage_event_min_drop_k UND Flanke >= min_slope.
    # Damit fallen sowohl das Sensorrauschen (kleiner Abfall) als auch das
    # langsame Abkuehlen des Speichers (grosse Summe, flache Flanke) heraus -
    # beide werden weiterhin protokolliert, aber nicht gelernt.
    usage_event_ruhe_min: float = 5.0
    usage_event_fenster_max_min: float = 20.0
    usage_event_min_drop_k: float = 2.0
    #: Ab dieser Flanke gilt ein Sample als "steile Phase". Der Speicher kuehlt
    #: im Leerlauf mit bis zu 0,5 K/min ab (Log 14.09.-18.10.2026), eine
    #: Zapfung bewegt den unteren Fuehler mit mehreren K/min - 1,0 trennt beides.
    usage_event_min_slope_k_min: float = 1.0


    max_usage_per_half_day: int = 2

    # ── Gelernte Fenster ──
    window_vorlauf_evening_h: float = 1.5
    window_nachlauf_evening_h: float = 0.75
    window_vorlauf_morning_h: float = 1.5
    window_nachlauf_morning_h: float = 0.75
    window_days: int = 14
    window_min_samples: int = 4

    # ── Komfort-Verletzungen ──
    comfort_grenz_c: float = 40.0
    comfort_max_pro_tag: int = 3
    comfort_max_entries: int = 200
    #: Eine anhaltende Kuehlphase ist EIN Ereignis, nicht drei. Ohne diese
    #: Sperre zaehlte und meldete der 10-s-Takt dieselbe Verletzung dreimal
    #: hintereinander (Nutzer-Log 28.09.2026, 18:43:18/28/37) und blaehte
    #: zusaetzlich die Statistik in api.py (`verletzungen_7d`) auf das
    #: Dreifache. Analog zur Zapfungs-Entprellung `usage_event_cooldown_min`.
    comfort_cooldown_min: int = 30
    comfort_bonus_schwellwert: int = 2
    comfort_bonus_vorlauf_h: float = 0.5

    # ── Quellen-Attribution / Zu-frueh ──
    zu_frueh_fenster_min: int = 45
    zu_frueh_threshold_w: float = 800.0
    zu_frueh_max_entries: int = 100

    # ── Surplus-Profil ──
    surplus_ewma_alpha: float = 0.08
    surplus_min_samples_pro_stunde: int = 5
    surplus_min_brauchbare_stunden: int = 4

    # ── Forecast-Kalibrierung ──
    forecast_ewma_alpha: float = 0.3
    forecast_min_samples: int = 3
    forecast_ratio_min: float = 0.3
    forecast_ratio_max: float = 2.0
    forecast_kalibrierung_ab_stunde: int = 20
    #: PV-Arrayflaeche in m2. Wird fuer die Forecast-Kalibrierung
    #: gebraucht, um die Prognose (Wh/m2) in Wh umzurechnen.
    #:
    #: ACHTUNG: Dieselbe Groesse liegt als `wp.pv_array_size_qm` in
    #: `wp_steuerung_parameter.json` (dort auch fuer die Stundensprognose
    #: verwendet). Der Wert hier ist nur der Rueckfall, falls die
    #: LearningEngine die WP-Config nicht bekommt - sonst wuerden zwei
    #: getrennte Werte auseinanderlaufen. `set_pv_array_size()` in
    #: `LearningEngine.update()` haelt beide synchron.
    pv_array_size_qm: float = 10.0

    # ── Allgemein ──
    max_cycles: int = 50
    config_version: int = 1


@dataclass
class HeatingCycle:
    """Ein abgeschlossener Aufheizvorgang."""
    start_time: str  # ISO-Format
    end_time: str
    start_temp_unten: float
    end_temp_unten: float
    start_temp_mitte: float
    end_temp_mitte: float
    duration_min: float
    rate_unten_c_h: float
    rate_gesamt_c_h: float
    season: str  # winter / transition / summer
    # Energie-Quellen-Attribution (optional, alte Datensaetze ohne diese Felder)
    avg_feedin_watt: Optional[float] = None
    avg_soc: Optional[float] = None
    quelle: str = ""  # pv / batterie / gemischt / netz


@dataclass
class UsageEvent:
    """Erkannte Warmwasser-Zapfung.
    
    Temperaturabfall an allen drei Fühler:
    unten kühlt zuerst, mitte folgt, oben zuletzt.
    """
    timestamp: str  # ISO-Format
    # Temperaturwerte VOR der Zapfung (Stand des vorherigen Zyklus)
    temp_before_unten: float
    temp_before_mitte: float
    temp_before_oben: float
    # Temperaturwerte NACH der Zapfung (aktueller Zyklus)
    temp_after_unten: float
    temp_after_mitte: float
    temp_after_oben: float
    # Temperaturabfall je Fühler in Kelvin
    drop_unten_k: float
    drop_mitte_k: float
    drop_oben_k: float
    # Gewichteter Gesamt-Abfall (gewichtet nach Sensorempfindlichkeit)
    drop_gesamt_k: float
    #: True, wenn der Vorgang die Plausibilitaetsgrenzen erfuellt
    #: (staerkster Abfall UND Flanke, siehe LearningConfig) und deshalb in
    #: die gelernte Zielzeit einfliesst. Altbestand kennt das Feld nicht und
    #: wird als plausibel gelesen.
    plausibel: bool = True


@dataclass
class LearningData:
    """Persistente Lerndaten."""
    # Heizraten (saisonal)
    cycles: List[Dict]
    heat_rates: Dict[str, Dict]
    
    # Zielzeit (Zapfverhalten)
    usage_events: List[Dict]
    learned_target_hour: float
    target_hour_samples: int

    # Gelernte MORGENliche Zapf-Zeit (Duschen frueh morgens)
    learned_morning_target_hour: float = 7.0
    morning_target_hour_samples: int = 0

    version: int = 4
    komfort_verletzungen: List[str] = field(default_factory=list)

    # ── Baustein A: Quellen-Attribution ──
    runtime_by_quelle_sec: Dict[str, float] = field(default_factory=lambda: {
        "pv": 0.0, "batterie": 0.0, "gemischt": 0.0, "netz": 0.0,
    })
    # Zyklen mit Nicht-PV-Quelle, nach denen binnen 45 min doch >800W kamen:
    zu_frueh_events: List[str] = field(default_factory=list)

    # ── Baustein B: Forecast-Kalibrierung ──
    # EWMA von (taeglicher Netzeinschuss Wh / Prognose Wh/m2), geklemmt 0.3-2.0
    forecast_ratio: float = 1.0
    forecast_ratio_samples: int = 0
    # Stundenscharfe Deviation: {stunde: {forecast, actual, deviation}}
    forecast_hourly_deviations: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── Verbrauchsbewusstsein: Stundensurplus-Profil ──
    # {"8": {"avg": 350.0, "n": 12}, ...}: gemitelte Netzeinspeisung je Stunde,
    # nur gesampelt bei AUSgeschaltetem Kompressor (= Haushaltsmuster pur).
    #
    # ACHTUNG (Befund 21.10.2026): Dieses Profil ist KEIN Tagesprofil. Es
    # unterscheidet weder Wochentag noch Saison, und der EWMA laeuft im
    # 10-Sekunden-Takt - der Wert ist nach 50 Sekunden "belastbar" und
    # friert dann auf dem aktuellen Tag ein. Fuer Aussagen ueber einen
    # ganzen TAG ist es deshalb unbrauchbar. Siehe dafuer
    # `tages_haushalt` (Tagesbilanz aus echten Tagessummen).
    surplus_by_hour: Dict[str, Dict[str, float]] = field(default_factory=dict)

    # ── Tages-Haushaltsbilanz (Befund 21.10.2026) ──
    # [{"ts": "2026-10-21", "verbrauch_wh": 1234.0}, ...]
    # Verbrauch des Haushalts je TAG, getrennt nach Wochentag ausgewertet.
    # Grundlage: erzeugte Energie (Wh) minus Einspeisung (Wh) des Tages.
    tages_haushalt: List[Dict] = field(default_factory=list)

    # ── Konfigurierbare Lernparameter (persistiert) ──
    config: LearningConfig = field(default_factory=LearningConfig)


def _get_season(month: int) -> str:
    """Bestimmt die Jahreszeit."""
    if month in (12, 1, 2):
        return "winter"
    elif month in (3, 4, 5, 9, 10, 11):
        return "transition"
    else:
        return "summer"


class LearningEngine:
    """Hauptklasse für das selbstlernende Verhalten."""

    def __init__(self, data_path: str = LEARNING_DATA_FILE):
        self.data_path = data_path
        self.data = self._load()
        self._last_compressor_state = False
        # Nach einem Neustart keine laufende Abkühlung doppelt als neue
        # Zapfung zählen. Die zuletzt persistierte Event-Zeit ist der Startwert.
        self._last_usage_event_time: Optional[datetime] = None
        for event in reversed(self.data.usage_events):
            parsed = self._parse_ts(event.get("timestamp"))
            if parsed is not None:
                self._last_usage_event_time = parsed
                break
        self._cycle_start_time: Optional[datetime] = None
        self._cycle_start_temps: Optional[Dict[str, float]] = None
        self._last_temps: Optional[Dict[str, Optional[float]]] = None
        self._last_temp_time: Optional[datetime] = None
        # Laufender Zapfungs-Vorgang (max. Abfall/Flanke werden verfolgt,
        # siehe _detect_usage/_verfolge_usage). Nur im RAM - ein Neustart
        # verwirft ihn, das Ereignis ist dann verloren (unkritisch).
        self._pending_usage: Optional[Dict] = None
        self._komfort_grenz_c: float = self.data.config.comfort_grenz_c
        # Solar-Tracking (Attribution/Kalibrierung/Surplus-Profil)
        self._cycle_feedin_ws: float = 0.0   # Zeitintegral Einspeisung [Ws]
        self._cycle_soc_sum_ws: float = 0.0
        self._cycle_secs: float = 0.0
        self._pending_zu_frueh: List[Dict] = []   # nur im RAM: {"ende": iso, "verpasste_wh": Wh}
        self._day_surplus_wh: float = 0.0
        self._day_pv_wh: float = 0.0
        # BUGFIX (21.10.2026): Tagesbilanz des echten Hausverbrauchs
        # (Wechselrichterbilanz: acpower - feedin - batPower).
        self._day_haus_wh: float = 0.0
        self._day_mess_secs: float = 0.0
        self._surplus_tag: str = ""
        self._kalibriert_datum: str = ""
        self._last_update_time: Optional[datetime] = None

    def _load(self) -> LearningData:
        """Lerndaten aus JSON laden oder Defaults erstellen."""
        defaults = LearningData(
            cycles=[], usage_events=[],
            heat_rates={"winter": {"avg": 3.0, "count": 0},
                       "transition": {"avg": 3.0, "count": 0},
                       "summer": {"avg": 3.0, "count": 0}},
            learned_target_hour=17.0, target_hour_samples=0, version=4
        )
        try:
            if os.path.exists(self.data_path):
                with open(self.data_path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                # Abwaertskompatibilitaet: alte Zapfungsereignisse
                # (temp_before/temp_after/drop_k) in neues Format
                # konvertieren
                usage_events = raw.get("usage_events", [])
                for ev in usage_events:
                    if "drop_unten_k" not in ev and "drop_k" in ev:
                        # Altes Format -> neues Format
                        ev["temp_before_unten"] = ev.get("temp_before", 0.0)
                        ev["temp_before_mitte"] = 0.0
                        ev["temp_before_oben"] = ev.get("temp_before", 0.0)
                        ev["temp_after_unten"] = ev.get("temp_after", 0.0)
                        ev["temp_after_mitte"] = 0.0
                        ev["temp_after_oben"] = ev.get("temp_after", 0.0)
                        ev["drop_unten_k"] = ev.get("drop_k", 0.0)
                        ev["drop_mitte_k"] = 0.0
                        ev["drop_oben_k"] = ev.get("drop_k", 0.0)
                        ev["drop_gesamt_k"] = ev.get("drop_k", 0.0)
                        # Alte Felder entfernen
                        ev.pop("temp_before", None)
                        ev.pop("temp_after", None)
                        ev.pop("drop_k", None)
                return LearningData(
                    cycles=raw.get("cycles", []),
                    heat_rates=raw.get("heat_rates", defaults.heat_rates),
                    usage_events=usage_events,
                    learned_target_hour=raw.get("learned_target_hour", 17.0),
                    target_hour_samples=raw.get("target_hour_samples", 0),
                    learned_morning_target_hour=raw.get("learned_morning_target_hour", 7.0),
                    morning_target_hour_samples=raw.get("morning_target_hour_samples", 0),
                    version=4,
                    komfort_verletzungen=raw.get("komfort_verletzungen", []),
                    runtime_by_quelle_sec=raw.get("runtime_by_quelle_sec", {
                        "pv": 0.0, "batterie": 0.0, "gemischt": 0.0, "netz": 0.0,
                    }),
                    zu_frueh_events=raw.get("zu_frueh_events", []),
                    forecast_ratio=raw.get("forecast_ratio", 1.0),
                    forecast_ratio_samples=raw.get("forecast_ratio_samples", 0),
                    forecast_hourly_deviations=raw.get("forecast_hourly_deviations", {}),
                    surplus_by_hour=raw.get("surplus_by_hour", {}),
    # BUGFIX (21.10.2026): Alte Eintraege OHNE das Feld "saison"
    # wurden mit der fehlerhaften Formel "Erzeugung - Einspeisung"
    # berechnet (nur der Sonnentag, ohne Nachtverbrauch). Sie werden
    # verworfen - sonst wuerden sie die Auswahl mit falschen Werten
    # verfaelschen.
    tages_haushalt=[
        dict(e) for e in (raw.get("tages_haushalt", []) or [])
        if isinstance(e, dict) and e.get("saison")
    ],
                    config=LearningConfig(**raw.get("config", {})),
                )
        except Exception as e:
            logging.warning(f"Konnte Lern-Daten nicht laden: {e}")
            self._sichere_korrupte_datei()
        return defaults

    def _sichere_korrupte_datei(self):
        """Bewahrt eine unlesbare Lerndatei auf, bevor Defaults sie ueberschreiben."""
        try:
            if os.path.exists(self.data_path):
                backup = f"{self.data_path}.korrupt-{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.copy2(self.data_path, backup)
                logging.warning(f"Korrupte Lern-Datei gesichert als {backup}")
        except Exception as e:
            logging.error(f"Konnte korrupte Lern-Datei nicht sichern: {e}")

    def _save(self):
        """Lerndaten atomar speichern: erst .tmp schreiben, dann os.replace().

        Schuetzt vor Datenverlust bei Stromausfall/Absturz mitten im Schreiben
        (SD-Karte des Pi): Die Zieldatei ist danach immer entweder die alte
        oder die vollstaendig neue Version -- niemals ein halbes JSON.
        """
        tmp_path = self.data_path + ".tmp"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(asdict(self.data), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.data_path)
        except Exception as e:
            logging.error(f"Fehler beim Speichern der Lern-Daten: {e}")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    # ── Öffentliche API ────────────────────────────────────

    def get_learned_heating_rate(self, month: int, sensor: str = "unten") -> float:
        """
        Gibt die gelernte Heizrate für die aktuelle Jahreszeit zurück.
        Default: 3.0 (unten) / 2.0 (gesamt).
        """
        season = _get_season(month)
        hr = self.data.heat_rates.get(season, {"avg": 3.0, "count": 0})
        if hr["count"] < self.data.config.heating_rate_min_samples:
            return 3.0 if sensor == "unten" else 2.0
        factor = 1.0 if sensor == "unten" else 0.67
        wert = hr["avg"] * factor
        # Auch BEREITS PERSISTIERTE Werte begrenzen. Die Kappung beim Lernen
        # greift fuer den Altbestand erst mit der EWMA-Zeitkonstante
        # (alpha=0.10), also erst nach Wochen. Ohne diese Begrenzung blieb
        # im Betriebslog vom 02.10.2026 die alte Rate stehen:
        #   "noch 13.8K bei 14.2 C/h -> erreicht ca. 09:34 (in 58 min)"
        rate_max = float(
            getattr(self.data.config, "heating_rate_max_c_h", 0.0) or 0.0)
        if rate_max > 0:
            wert = min(wert, rate_max)
        return round(wert, 2)

    def get_learned_target_hour(self) -> float:
        """Gelernte optimale ABEND-Zielzeit (Default 17:00 bei < config.target_hour_min_samples Samples)."""
        if self.data.target_hour_samples < self.data.config.target_hour_min_samples:
            return 17.0
        return self.data.learned_target_hour

    def get_learned_morning_target_hour(self) -> float:
        """Gelernte MORGEN-Zielzeit (Default 07:00 bei < config.target_hour_min_samples Samples)."""
        if self.data.morning_target_hour_samples < self.data.config.target_hour_min_samples:
            return 7.0
        return self.data.learned_morning_target_hour

    @classmethod
    def _parse_ts(cls, wert) -> Optional[datetime]:
        """ISO-Zeitstempel robust parsen und auf naiv normalisieren.

        Die Lern-Datei enthaelt historisch GEMISCHTE Formate: aeltere Eintraege
        wurden mit Offset geschrieben ('2026-08-27T17:18:44+02:00'), neuere ohne.
        Ein direkter Vergleich warf
        'TypeError: can't compare offset-naive and offset-aware datetimes' und
        liess die Learning-Info in /status ins Leere laufen (Incident 15.09.:
        'Learning-Engine-Info nicht verfuegbar: can't compare ...').
        Unlesbare Werte ergeben None statt einer Exception.
        """
        if not isinstance(wert, str):
            return None
        try:
            return to_naive(datetime.fromisoformat(wert))
        except (ValueError, TypeError):
            return None

    def get_learned_morning_window(
        self,
        vorlauf_h: float = 1.0,
        nachlauf_h: float = 0.75,
        now: Optional[datetime] = None,
        tage: int = 14,
        min_samples: int = 4,
    ) -> Optional[tuple]:
        """Gelerntes MORGEN-Zapffenster (05-12 Uhr) analog zum Abendfenster.

        Grundlage fuer die dynamische Frueh-Garantie der MindestTemp-Regel.
        Returns: (frueheste_h, spaeteste_h) inkl. Puffer, oder None bei
        weniger als min_samples Zapfungen im Zeitraum.
        """
        if now is None:
            now = datetime.now()
        grenze = to_naive(now - timedelta(days=tage))
        stunden = []
        for e in self.data.usage_events:
            # Nur plausible Zapfungen: Sonst verschieben Sensorrauschen und das
            # langsame Abkuehlen des Speichers das gelernte Fenster (Befund
            # 18.10.2026: 26 von 27 Ereignissen waren Kleinabfaelle < 1 K).
            if not e.get("plausibel", True):
                continue
            ts = self._parse_ts(e.get("timestamp"))
            if ts is None:
                continue
            if ts < grenze or not (5 <= ts.hour < 12):
                continue
            stunden.append(ts.hour + ts.minute / 60.0)
        if len(stunden) < min_samples:
            return None
        frueheste = max(min(stunden) - vorlauf_h, 4.5)
        spaeteste = min(max(stunden) + nachlauf_h, 11.5)
        return round(frueheste, 2), round(spaeteste, 2)

    def get_learned_evening_window(
            self,
            vorlauf_h: float = 1.5,
            nachlauf_h: float = 0.75,
            now: Optional[datetime] = None,
            tage: int = 14,
            min_samples: int = 4,
        ) -> Optional[tuple]:
            """Gelerntes Abend-Zapffenster aus den letzten `tage` Tagen.

            Aus ALLEN erkannten Zapfungen (16-24 Uhr) wird frueheste und spaeteste
            Zapfzeit bestimmt und mit Vor-/Nachlauf gepuffert. Die MindestTemp-Regel
            kann ihr Zeitfenster daraus dynamisch ableiten ("Zeiten anpassen"),
            statt starr auf die konfigurierten Stunden zu warten.

            Returns:
                (frueheste_uhrzeit_h, spaeteste_uhrzeit_h) oder None, wenn weniger
                als min_samples Zapfungen im Betrachtungszeitraum liegen.
            """
            if now is None:
                now = datetime.now()
            grenze = to_naive(now - timedelta(days=tage))
            stunden = []
            for e in self.data.usage_events:
                # Nur plausible Zapfungen (Befund 18.10.2026): Sensorrauschen
                # und langsames Abkuehlen wuerden das Fenster verbreitern.
                if not e.get("plausibel", True):
                    continue
                ts = self._parse_ts(e.get("timestamp"))
                if ts is None:
                    continue
                if ts < grenze or not (16 <= ts.hour < 24):
                    continue
                stunden.append(ts.hour + ts.minute / 60.0)
            if len(stunden) < min_samples:
                return None
            frueheste = max(min(stunden) - vorlauf_h, 16.0)
            spaeteste = min(max(stunden) + nachlauf_h, 23.75)
            return round(frueheste, 2), round(spaeteste, 2)

    def _detect_komfort_verletzung(self, now, t_oben, nachtsperre_aktiv, grenz_c=40.0, max_pro_tag=3):
        """Prueft ob t_oben unter die Komfort-Grenze gefallen ist und
        zaehlt die Verletzung (ausserhalb Nachtsperre, max. max_pro_tag).

        Entprellung: Eine anhaltende Kuehlphase ist EIN Ereignis. Ohne die
        Sperre zaehlte der 10-s-Takt dieselbe Verletzung dreimal in Folge
        (Nutzer-Log 28.09.2026, 18:43:18/28/37) - dreifache Warnung UND
        dreifache Verzerrung der Statistik (`verletzungen_7d` in api.py).
        """
        if t_oben is None or t_oben >= grenz_c or nachtsperre_aktiv:
            return
        heute_mitternacht = to_naive(
            now.replace(hour=0, minute=0, second=0, microsecond=0))
        heute_count = sum(
            1 for v in self.data.komfort_verletzungen
            if (ts := self._parse_ts(v)) is not None and ts >= heute_mitternacht
        )
        if heute_count >= self.data.config.comfort_max_pro_tag:
            return
        # Innerhalb des Cooldowns schon erfasst? Dann zaehlt und meldet
        # diese Kuehlphase nicht erneut.
        cooldown_s = max(0, int(self.data.config.comfort_cooldown_min)) * 60
        if cooldown_s and self.data.komfort_verletzungen:
            vor_ts = self._parse_ts(self.data.komfort_verletzungen[-1])
            if vor_ts is not None:
                try:
                    if 0 <= (to_naive(now) - vor_ts).total_seconds() < cooldown_s:
                        return
                except (TypeError, ValueError):
                    pass
        ts = now.isoformat(timespec="seconds")
        self.data.komfort_verletzungen.append(ts)
        # Auf letzte config.comfort_max_entries Eintraege begrenzen
        if len(self.data.komfort_verletzungen) > self.data.config.comfort_max_entries:
            self.data.komfort_verletzungen = self.data.komfort_verletzungen[-self.data.config.comfort_max_entries:]
        self._save()
        logging.warning(f"KOMFORT-VERLETZUNG: t_oben {t_oben:.1f}C < {grenz_c}C um {ts}")

    def get_komfort_verletzung_rate(self, tage=7) -> int:
        """Gibt Anzahl Komfort-Verletzungen der letzten tage zurueck."""
        if not self.data.komfort_verletzungen:
            return 0
        # Datumsvergleich statt String-Vergleich: die Eintraege koennen
        # historisch mit/ohne Zeitzonen-Offset vorliegen (s. _parse_ts).
        grenze = to_naive(datetime.now() - timedelta(days=tage))
        return sum(
            1 for v in self.data.komfort_verletzungen
            if (ts := self._parse_ts(v)) is not None and ts >= grenze
        )

    def get_komfort_bonus_vorlauf(self, schwellwert=2, tage=7) -> float:
        """Gibt zusaetzlichen Vorlauf fuer das Morgenfenster (0 oder 0.5 h),
        wenn in den letzten tage mehr als schwellwert Verletzungen auftraten."""
        rate = self.get_komfort_verletzung_rate(tage=tage)
        if rate > schwellwert:
            return 0.5
        return 0.0

    def get_forecast_ratio(self) -> float:
        """Kalibrierte Prognose (tatsaechlicher Netzeinschuss/Prognose).

        Neutral 1.0 bis mindestens 3 Tageswerte vorliegen."""
        if self.data.forecast_ratio_samples < 3:
            return 1.0
        return self.data.forecast_ratio

    @staticmethod
    def _saison_von_datum(tag: str) -> str:
        """Jahreszeit aus einem ISO-Datum ("2026-01-15").

        BUGFIX (21.10.2026): Der Hausverbrauch haengt nicht nur vom
        Wochentag ab, sondern auch von der Jahreszeit (Heizung, mehr
        Licht, mehr Kochen). Ohne Saisontrennung vermischte der Median
        Sommer (400 W/Tag) und Winter (1500 W/Tag) zu einem
        unbrauchbaren Zwischenwert (gemessen: 5200 Wh statt 9600
        bzw. 36000 Wh).

        Geteilt wird nach meteorologischen Jahreszeiten:
        Winter (12,1,2), Fruehling (3,4,5), Sommer (6,7,8), Herbst (9,10,11)
        """
        return saison_von_datum(tag)

    def get_haushaltsprofil_je_tag(self, tage: int = 28) -> Dict[str, float]:
        """Tages-Haushaltsbilanz: {Wochentag: geschätzter Verbrauch in Wh}.

        BUGFIX (21.10.2026): `surplus_by_hour` taugt NICHT als
        Haushaltsprofil. Es ist nachweislich kein Tagesprofil, sondern der
        aktuellste Wert der jeweiligen Stunde:

          * Der Schluessel ist NUR die Stunde - Wochentag und Saison werden
            nicht unterschieden (gemessen: Montag/Dienstag 2000 W und
            Wochenende 200 W ergeben fuer alle Stunden denselben Wert
            922,9 W).
          * Der EWMA laeuft im 10-Sekunden-Takt. Ein Tagesprofil braucht
            aber TAGE, nicht Takte - der Wert ist nach 50 Sekunden
            "belastbar" (n>=5 Takte) und friert dann auf dem Wert des
            aktuellen Tages ein.

        Fuer die Legionellen-Tageswahl brauchen wir aber genau das, was
        dieses Profil NICHT liefert: wie viel Energie der HAUSHALT an
        einem Tag verbraucht. Ein Tag mit 3000 Wh/m2 Ertrag ist schlecht,
        wenn der Haushalt an diesem Tag 3000 Wh zieht.

        Deshalb wird hier etwas anderes gemessen: die TAGESSUMME der
        tatsaechlichen Erzeugung (`_day_pv_wh`) minus die TAGESSUMME der
        Einspeisung (`_day_surplus_wh`) ergibt den Tagesverbrauch des
        Haushalts - inklusive der Batterie, genauer als jeder Stundenwert.

        Das Ergebnis wird je Wochentag (Mo..So) getrennt gehalten, weil
        der Verbrauch am Wochenende typisch anders ist als werktags.

        Returns: {wochentag_str: wh} - nur Tage mit ausreichend
        Messpunkten (mind. `min_tage_pro_wochentag` Tage).
        """
        min_tage_pro_wochentag = 2
        werte: Dict[str, list] = {}
        werte_saison: Dict[str, list] = {}
        for eintrag in self.data.tages_haushalt:
            try:
                ts = self._parse_ts(eintrag.get("ts"))
            except (AttributeError, TypeError):
                ts = None
            if ts is None:
                continue
            verbrauch = eintrag.get("verbrauch_wh")
            if not isinstance(verbrauch, (int, float)) or isinstance(verbrauch, bool):
                continue
            if verbrauch < 0 or verbrauch > 40000:
                continue
            # Wochentag 0=Mo..6=So
            key = str(ts.weekday())
            werte.setdefault(key, []).append(float(verbrauch))
            # Zusaetzlich je Wochentag+SAISON. Der Hausverbrauch haengt
            # stark von der Jahreszeit ab (Heizung, Licht, Kochen).
            # Ohne diese Trennung vermischte der Median Sommer und
            # Winter zu einem unbrauchbaren Zwischenwert.
            saison = eintrag.get("saison") or self._saison_von_datum(
                eintrag.get("ts", ""))
            werte_saison.setdefault(f"{key}-{saison}", []).append(
                float(verbrauch))

        def _median(liste: list) -> float:
            # Median statt Mittelwert: ein einzelner Ausreißer-Tag (Party,
            # Waschmaschine) soll den Plan nicht verschieben.
            liste = sorted(liste)
            mitte = len(liste) // 2
            if len(liste) % 2:
                return liste[mitte]
            return (liste[mitte - 1] + liste[mitte]) / 2.0

        profil: Dict[str, float] = {}
        for key, liste in werte.items():
            if len(liste) < min_tage_pro_wochentag:
                continue
            profil[key] = round(_median(liste), 1)
        # Saison-Werte zusaetzlich (Schluessel "wd-saison")
        for key, liste in werte_saison.items():
            if len(liste) < min_tage_pro_wochentag:
                continue
            profil[key] = round(_median(liste), 1)
        return profil

    def get_haushaltsverbrauch_fuer_wochentag(self, wochentag: int,
                                             datum: Optional[str] = None
                                             ) -> Optional[float]:
        """Geschaetzter Haushaltsverbrauch fuer einen Wochentag (in Wh).

        None, wenn fuer diesen Wochentag noch keine brauchbare Messung
        vorliegt - dann darf der Plan nicht darauf gestuetzt werden.

        BUGFIX (21.10.2026): Der Verbrauch haengt auch von der
        Jahreszeit ab. Deshalb wird zuerst der wochentags- UND
        saisonbezogene Wert gesucht (Schluessel "wd-saison"), und
        nur wenn der fehlt, auf den reinen Wochentagswert zurueckgefallen.
        Ein Sommerwert fuer einen Wintertag waere eine Falschaussage
        (gemessen: 9600 Wh/Tag im Sommer gegen 36000 Wh/Tag im
        Winter bei identischem Wochentag).
        """
        profil = self.get_haushaltsprofil_je_tag()
        wd = str(int(wochentag))
        if datum:
            saison = self._saison_von_datum(datum)
            wert = profil.get(f"{wd}-{saison}")
            if wert is not None:
                return float(wert)
        wert = profil.get(wd)
        if wert is None:
            return None
        return float(wert)

    def get_surplus_profile_with_forecast(self, forecast_today_wh_qm=None,
                                           forecast_hourly_wh=None,
                                           wochentag=None, saison=None):
        """Surplus-Profil je Stunde - Haushaltsprofil fuer den CalcStart.

        BUGFIX (21.10.2026): Diese Methode wurde am 04.09.2026 vom Aufrufer
        in priority_control_logic aufgerufen (mit `forecast_today_wh_qm` und
        `forecast_hourly_wh`), aber NIE implementiert. `getattr(...)` lieferte
        deshalb `None`, und `surplus_profil` war IMMER None - die
        Mittagstief-Erkennung (`_mittagstief_stunden`, die den Start bei
        wenig Netzeinschuss frueher ansetzt) lief nie.

        Das gelernte Profil `surplus_by_hour` misst die Netzeinspeisung bei
        AUSgeschaltetem Kompressor - also den Haushalt: in der Mittagstunde
        mit grossem Verbrauch ist die Einspeisung niedrig.

        Wichtig zur Semantik: Die Einspeisung ist bereits "Erzeugung minus
        Haushalt". Die Prognose wird deshalb NICHT abgezogen - das hiesse
        den Haushalt doppelt zu rechnen. Sie dient als Plausibilitaets-
        PRUEFUNG: eine Stunde ganz ohne Prognose (Nacht, oder die
        Stundensprognose fehlt) liefert keine verwertbare
        Haushaltsinformation und wird verworfen. So bleibt das Profil
        belastbar, ohne die Aussage zu erfinden.

        Rueckgabe: {stunde(str): watt} oder None (noch unbrauchbar).
        """
        # BUGFIX (21.10.2026): Optional nach Wochentag / Jahreszeit
        # filtern - der Hausverbrauch unterscheidet sich danach stark.
        profil = self.get_surplus_profile(wochentag=wochentag, saison=saison)
        if not profil:
            return None

        # Stunden ohne Prognose sind nicht verwertbar (Nachtstunden oder
        # fehlende Stundensprognose).
        nutzbar = dict(profil)
        if forecast_hourly_wh:
            mit_prognose = {}
            for stunde_s, wert in profil.items():
                try:
                    key = int(stunde_s)
                except (TypeError, ValueError):
                    continue
                wm2 = forecast_hourly_wh.get(key)
                if wm2 is None:
                    continue
                try:
                    wm2 = float(wm2)
                except (TypeError, ValueError):
                    continue
                if wm2 <= 0:
                    continue
                mit_prognose[stunde_s] = float(wert)
            if mit_prognose:
                nutzbar = mit_prognose

        return nutzbar if len(nutzbar) >= 4 else None

    def get_stunden_korrekturfaktor(self) -> Dict[int, float]:
        """Je Stunde: gelernter Korrekturfaktor auf die Prognose.

        BUGFIX (21.10.2026): Die gesammelten Stunden-Abweichungen
        ("zu welcher Tageszeit liegt die Prognose daneben?") wurden
        gespeichert, aber NIRGENDS gelesen. Sie sind genau die
        Information, die fuer die PV-Fenster-Ende-Berechnung fehlt:
        wenn die Prognose um 16 Uhr zu optimistisch ist, endet das
        PV-Fenster frueher als berechnet.

        Der Faktor ist der Quotient aus tatsaechlicher und prognostizier-
        ter Erzeugung, je Stunde ueber die letzten Tage (Median, damit ein
        einzelner Ausreisser-Tag nicht durchschlaegt). Begrenzt auf
        0,2 .. 2,0, damit ein Ausreisser die Steuerung nicht kippt.

        Returns: {Stunde: Faktor} - leer, wenn noch nichts gelernt ist.
        """
        werte: Dict[int, List[float]] = {}
        for key, e in self.data.forecast_hourly_deviations.items():
            try:
                stunde = int(str(key).rsplit("-", 1)[-1])
                prognose = float(e.get("forecast", 0.0))
                actual = float(e.get("actual", 0.0))
            except (TypeError, ValueError, KeyError):
                continue
            # Nur relative Abweichungen sind nutzbar - eine Prognose von
            # fast 0 W liefert keinen stabilen Quotienten.
            if prognose <= 1.0 or actual <= 0:
                continue
            werte.setdefault(stunde, []).append(actual / prognose)
        faktor: Dict[int, float] = {}
        for stunde, quotienten in werte.items():
            quotienten = sorted(quotienten)[-14:]
            if len(quotienten) < 3:
                continue
            mitte = len(quotienten) // 2
            if len(quotienten) % 2:
                median = quotienten[mitte]
            else:
                median = (quotienten[mitte - 1] + quotienten[mitte]) / 2.0
            faktor[stunde] = round(min(2.0, max(0.2, median)), 3)
        return faktor

    def get_forecast_hourly_deviations(self) -> Dict:
        """Stundenscharfe Forecast-Abweichungen {stunde: {forecast, actual, deviation}}.

        deviation = actual - forecast (positiv = Forecast zu niedrig, negativ = zu hoch)."""
        return dict(self.data.forecast_hourly_deviations)

    def get_surplus_profile(self, wochentag=None, saison=None):
        """Stundensurplus-Profil {stunde: watt} oder None (noch unbrauchbar).

        BUGFIX (21.10.2026): Der Hausverbrauch haengt stark vom
        Wochentag und von der Jahreszeit ab (Berufstagen vs. Wochenende,
        Heizung im Winter, mehr Kochen am Wochenende). Ohne Trennung
        mittelt das Profil ueber alles - gemessen: Werktag 1800 W gegen
        Wochenende 400 W in derselben Stunde wurden zu 1100 W
        gemittelt, und der CalcStart rechnete 6,7 h statt 2,9 h
        (Werktag) bzw. 0,7 h (Wochenende) Heizzeit.

        Mit `wochentag` (0=Mo..6=So) und/oder `saison` werden nur die
        passenden Tage ausgewertet. Fehlen passende Daten, wird auf das
        Gesamtprofil zurueckgefallen (lieber ein Mittelwert als gar
        keiner).

        Eine Stunde gilt als brauchbar ab n>=5 Samples; das Profil als
        Ganzes ab 4 brauchbaren Stunden (Tageslicht-Luecken erlaubt).

        BUGFIX (21.10.2026): Die Buckets sind jetzt tag+stunden-basiert
        ("2026-10-05-08"). Davor war der Key nur die Stunde, wodurch
        sich die Tage im EWMA vermischten (gemessen: Stunde 8 nach zwei
        Tagen 545,7 W statt 900 W - die WebApp-Anzeige war um Stunden
        verschoben).

        Fuer die Anzeige wird je Stunde der MITTELWERT der letzten
        Tage genommen: ein einzelner Tag (Regen, Party) soll das Profil
        nicht verschieben. Ohne das ist die Anzeige der letzte Tag -
        und der kann voellig untypisch sein.
        """
        # Buckets nach Stunde sammeln (Tag+Stunde -> Wert).
        #
        # Wichtig: Die Mindestzahl Samples (n>=5) wird ueber ALLE Tage
        # einer Stunde geprueft, nicht je Tag. Im 10-Minuten-Takt
        # fallen nur ~3 Samples auf die volle Stunde (die restlichen
        # laufen in die Folgestunde), ein einzelner Tag erreicht also
        # nie n>=5 - der Buckettest wuerde das Profil nie freigeben.
        #
        # BUGFIX (21.10.2026): Der Key enthaelt den TAG ("2026-10-05-08").
        # Damit lassen sich die Tage nach Wochentag und Jahreszeit
        # filtern - der Hausverbrauch unterscheidet sich danach stark.
        def _passt(key: str) -> bool:
            """Passt der Bucket zum gewuenschten Wochentag / zur Saison?"""
            if wochentag is None and saison is None:
                return True
            datum = str(key)[:10]
            if wochentag is not None:
                try:
                    ts = datetime.fromisoformat(datum)
                except (TypeError, ValueError):
                    return True
                if ts.weekday() != int(wochentag):
                    return False
            if saison is not None:
                if self._saison_von_datum(datum) != saison:
                    return False
            return True

        je_stunde = {}
        for key, e in self.data.surplus_by_hour.items():
            try:
                # Schluessel-Format: "YYYY-MM-DD-HH"
                stunde = int(str(key).rsplit("-", 1)[-1])
            except (TypeError, ValueError):
                continue
            if not _passt(key):
                continue
            je_stunde.setdefault(stunde, []).append(e)
        nutzbar = {}
        for stunde, eintraege in je_stunde.items():
            # Die letzten 7 Tage dieses Buckets (neueste zuerst gespeichert)
            eintraege = eintraege[-7:]
            n_gesamt = sum(int(e.get("n", 0)) for e in eintraege)
            if n_gesamt < 5:
                continue
            # Mittel der Tageswerte (jeder Tag zaehlt gleich - ein Tag
            # mit mehr Samples soll nicht staerker wiegen)
            werte = [float(e["avg"]) for e in eintraege]
            nutzbar[stunde] = round(sum(werte) / len(werte), 1)
        if len(nutzbar) < 4 and (wochentag is not None or saison is not None):
            # Zu wenige passende Daten (z.B. der Wochentag kam erst
            # zweimal vor): auf das Gesamtprofil zurueckfallen. Lieber
            # ein Mittelwert ueber alle Tage als gar kein Profil.
            return self.get_surplus_profile()
        return nutzbar if len(nutzbar) >= 4 else None

    def get_quellen_statistik(self) -> Dict:
        """Laufzeit-Split je Quelle + Zaehlung der Zu-frueh-Events."""
        z14 = 0
        if self.data.zu_frueh_events:
            # Neuester Event als Referenz (fuer Test-Simulationen); Vergleich
            # robust ueber _parse_ts, da Eintraege gemischte Formate haben.
            ref = self._parse_ts(self.data.zu_frueh_events[-1])
            if ref is not None:
                grenze = ref - timedelta(days=14)
                z14 = sum(
                    1 for v in self.data.zu_frueh_events
                    if (ts := self._parse_ts(v)) is not None and ts >= grenze
                )
        return {
            "runtime_sec": dict(self.data.runtime_by_quelle_sec),
            "zu_frueh_events_gesamt": len(self.data.zu_frueh_events),
            "zu_frueh_14d": z14,
        }

    def get_info(self) -> Dict:
        """Übersicht der gelernten Werte für API/UI."""
        fenster = self.get_learned_evening_window()
        # BUGFIX (22.10.2026): Aktuelle Jahreszeit fuer den
        # Lernstand (der Saison-Vergleich braucht sie).
        _saison_jetzt = self._saison_von_datum(
            datetime.now().strftime("%Y-%m-%d"))
        return {
            "heat_rates": self.data.heat_rates,
            "learned_target_hour": self.get_learned_target_hour(),
            "target_hour_samples": self.data.target_hour_samples,
            "total_cycles": len(self.data.cycles),
            "total_usage_events": len(self.data.usage_events),
            # Gelerntes Abend-Zapffenster [frueheste_h, spaeteste_h] oder None
            # (Grundlage fuer die dynamischen MindestTemp-Fenster)
            "learned_evening_window": list(fenster) if fenster else None,
            "learned_morning_target_hour": self.get_learned_morning_target_hour(),
            "morning_target_hour_samples": self.data.morning_target_hour_samples,
            "learned_morning_window": list(m_fenster) if (m_fenster := self.get_learned_morning_window()) else None,
            "komfort_verletzungen_7d": self.get_komfort_verletzung_rate(tage=7),
            "komfort_verletzungen_1d": self.get_komfort_verletzung_rate(tage=1),
            # Baustein A+B + Surplus-Profil
            "forecast_ratio": self.get_forecast_ratio(),
            "forecast_ratio_samples": self.data.forecast_ratio_samples,
            "forecast_hourly_deviations": self.get_forecast_hourly_deviations(),
            # BUGFIX (21.10.2026): Die Tages-Haushaltsbilanz fuer die
            # WebApp. Ausgewiesen wird sie je Wochentag (Median der
            # gemessenen Tage) - damit ist sichtbar, OB und WIE der
            # Hausverbrauch vom Wochentag abhaengt. Das ist die
            # Grundlage der Legionellen-Tageswahl (bester UEBERSCHUSS
            # statt hoechste Prognose).
            "haushalt_je_wochentag": self.get_haushaltsprofil_je_tag(),
            "haushalt_tage_erfasst": len(self.data.tages_haushalt),
            # BUGFIX (21.10.2026): Die gelernten Korrekturfaktoren je
            # Stunde sichtbar machen. Sie zeigen, OB und WIE stark die
            # Wetterprognose von der Realitaet abweicht - und ob die
            # Korrektur ueberhaupt greift.
            "stunden_korrekturfaktor": {
                str(h): f for h, f in self.get_stunden_korrekturfaktor().items()
            },
            "stunden_abweichung_anzahl": len(self.data.forecast_hourly_deviations),
            # BUGFIX (22.10.2026): Der LERNSTAND - wie viele Rohdaten
            # liegen vor und wie weit ist das Gelernte fortgeschritten.
            # Ohne das ist nicht erkennbar, ob die Steuerung wirklich
            # lernt oder nur auf Defaults faehrt.
            "lernstand": {
                "stundenprofil_tage": len({
                    str(k)[:10] for k in (self.data.surplus_by_hour or {})
                }),
                "stundenprofil_stunden": len(self.get_surplus_profile() or {}),
                "tagesbilanz_tage": len(self.data.tages_haushalt or []),
                "tagesbilanz_wochentage": len({
                    str(k) for k in self.get_haushaltsprofil_je_tag() or {}
                    if "-" not in str(k)
                }),
                # BUGFIX (22.10.2026): Die Tagesbilanz wird je Wochentag
                # UND Jahreszeit gefuehrt. Fuer die aktuelle Saison zaehlt,
                # wie viele Wochentage bereits einen saisonbezogenen
                # Wert haben - nur dann trifft die Legionellenwahl den
                # Verbrauch des jeweiligen Tages.
                "tagesbilanz_wochentage_saison": len({
                    str(k) for k in self.get_haushaltsprofil_je_tag() or {}
                    if str(k).endswith("-" + _saison_jetzt)
                }),
                "saison": _saison_jetzt,
                "korrektur_stunden": len(self.get_stunden_korrekturfaktor() or {}),
                "zyklen": len(self.data.cycles or []),
                "zapfungen": len(self.data.usage_events or []),
            },
            # Die aktuelle Jahreszeit - damit die WebApp die
            # saisonbezogenen Werte ("wd-saison") zuordnen kann.
            "saison": self._saison_von_datum(
                datetime.now().strftime("%Y-%m-%d")),
            "quellen": self.get_quellen_statistik(),
            "surplus_stunden": sorted(self.get_surplus_profile().keys())
                if self.get_surplus_profile() else [],
            # Volles Profil {stunde: watt} fuer das Balkendiagramm im UI
            # (None solange unbrauchbar, <4 brauchbare Stunden)
            "surplus_profil": (
                {str(h): round(w, 0)
                 for h, w in self.get_surplus_profile().items()}
                if self.get_surplus_profile() else None
            ),
            # ── Multisensor-Zapfungsdaten ──
            "usage_events": self.data.usage_events[-20:],   # Letzte 20 Ereignisse
            # Letzte erkannte Zapfung fuer CalcStart
            "letzte_zapfung": self.data.usage_events[-1] if self.data.usage_events else None,
        }

    # ── Zyklus-Update ──────────────────────────────────────

    def update(
        self,
        now: datetime,
        temp_dict: Dict[str, Optional[float]],
        compressor_is_on: bool,
        feedin_watt: Optional[float] = None,
        pv_acpower_watt: Optional[float] = None,
        pv_erzeugung_watt: Optional[float] = None,
        soc: Optional[float] = None,
        forecast_today_wh_qm: Optional[float] = None,
        legionellen_end_time: Optional[datetime] = None,
        forecast_hourly_wh: Optional[Dict[int, float]] = None,
        pv_array_size_qm: Optional[float] = None,
        hausverbrauch_watt: Optional[float] = None,
        hausverbrauch_stunden_profil: bool = False,
    ):
        """
        Wird jeden Regelzyklus aufgerufen.
        Erkennt Heizzykus-Start/Ende und Warmwasser-Zapfung sowie
        Quellen-Attribution, Forecast-Kalibrierung und Surplus-Profil.

        `pv_array_size_qm` ist die PV-Flaeche aus `wp.pv_array_size_qm`. Sie
        wird hier uebernommen, damit die Forecast-Kalibrierung mit exakt
        derselben Groesse rechnet wie die Stundensprognose in
        `priority_control_logic`. Ohne diesen Wert bliebe nur der Default
        (10 m2) - bei einer anderen Anlagengrösse waere der Faktor dann um
        den Flaechenquotienten daneben.
        """
        if isinstance(pv_array_size_qm, (int, float)) and not isinstance(
            pv_array_size_qm, bool
        ):
            if float(pv_array_size_qm) > 0:
                self.data.config.pv_array_size_qm = float(pv_array_size_qm)
        # ── Solar-Tracking ──
        # now immer auf naive-UTC reduzieren, damit interne Speicherung
        # (JSON naive-Timestamps) mit Produktions-now (timezone-aware)
        # konsistent bleibt.
        now = to_naive(now)

        dt_secs = 0.0
        if self._last_update_time is not None:
            dt_secs = (now - self._last_update_time).total_seconds()
            if dt_secs < 0 or dt_secs > 3600:  # Zeitprung/Neustart
                dt_secs = 0.0
        self._last_update_time = now

        # Tages-Energie integrieren und abends gegen die Prognose kalibrieren (B).
        heute = now.strftime("%Y-%m-%d")
        if self._surplus_tag != heute:
            # BUGFIX (21.10.2026): Beim Tageswechsel den VERGANGENEN Tag
            # bilanzieren, BEVOR die Zaehler zurueckgesetzt werden. Ohne
            # das ginge die Tagesbilanz verloren und die Haushalts-
            # schaetzung (fuer die Legionellen-Tageswahl) haette nie Daten.
            if self._surplus_tag is not None:
                self._bilanziere_vortag(self._surplus_tag)
            self._surplus_tag = heute
            self._day_surplus_wh = 0.0
            self._day_pv_wh = 0.0
            # BUGFIX (21.10.2026): Tagesbilanz des HAUSVERBRAUCHS. Die
            # Differenz Erzeugung - Einspeisung erfasst nur, was am Tag
            # mit Sonne floss; der Nachtverbrauch (Haus + Batterie) fehlte
            # komplett (gemessen: 5200 statt 9600 Wh/Tag). Deshalb wird der
            # echte Hausverbrauch aus der Wechselrichterbilanz akkumuliert:
            #   hausverbrauch = acpower - feedin - batPower
            self._day_haus_wh = 0.0
            self._day_mess_secs = 0.0
        if feedin_watt is not None and dt_secs > 0:
            self._day_surplus_wh += max(feedin_watt, 0.0) * dt_secs / 3600.0
        # Die Kalibrierung braucht die ERZEUGTE Energie, nicht die
        # exportierte. Vorher stand hier nur `_day_surplus_wh`, also reine
        # Netzeinspeisung; alles was das Haus verbrauchte und alles was die
        # Batterie aufnahm, fehlte im Zaehler. Der Quotient war damit
        # 1 - Hausanteil an der PV und lag dauerhaft unter 1 (beobachtet
        # x0,62): die Prognose wurde nicht kalibriert, sondern
        # systematisch zu pessimistisch bewertet.
        #
        # Ab hier kommt die Korrektur der naechsten Ebene: auch
        # `pv_acpower_watt` ist NICHT die Erzeugung, sondern der
        # Wechselstromausgang des Wechselrichter (Haus + Netz). Nachts
        # liefert die Gleichstromseite 0 W, die AC-Seite dagegen rund
        # 250 W. Ueber `pv_acpower_watt` wurde also weiterhin der
        # Hausverbrauch gezaehlt - nur eben mit Einspeisung statt mit
        # Erzeugung. Der Quotient blieb deshalb dauerhaft falsch und
        # wurde durch Nachjustieren des Faktors kaschiert.
        #
        # `pv_erzeugung_watt` kommt aus powerdc1 + powerdc2 und ist die
        # tatsaechliche Erzeugung. Ohne diesen Wert (aeltere Aufrufer)
        # wird auf das alte Verhalten zurueckgefallen, damit sich das
        # Verhalten nicht unbemerkt still aendert.
        erzeugung_w = (
            pv_erzeugung_watt if pv_erzeugung_watt is not None else pv_acpower_watt
        )
        if erzeugung_w is not None and dt_secs > 0:
            self._day_pv_wh += max(erzeugung_w, 0.0) * dt_secs / 3600.0

        # Tagesbilanz des echten Hausverbrauchs (inkl. Nacht und Batterie)
        if hausverbrauch_watt is not None and hausverbrauch_watt >= 0:
            self._day_haus_wh += hausverbrauch_watt * dt_secs / 3600.0
            self._day_mess_secs += dt_secs
        # Wichtig: Der Aufrufer liefert die HEUTE-Prognose bereits in Wh/m2
        # (priority_control_logic normalisiert dort mit kwh_m2). Deshalb hier
        # NUR die Gueltigkeit pruefen, nicht noch einmal umrechnen - eine
        # zweite Normalisierung mit value_unit="wh_m2" wuerde 5060 Wh/m2 als
        # "5.06 Wh/m2" lesen und die Tagesprognose immer als zu klein einstufen.
        # Symptom: "Kalibrierung uebersprungen (keine brauchbare Tagesprognose)"
        # und ein dauerhaftes "samples 0/3" in der WebApp.
        self._kalibriere_forecast(now, heute, forecast_today_wh_qm)

        # ── Stündliche Prognose-Abweichung (BUGFIX 21.10.2026) ──
        #
        # Die Stundensprognose war bis hierher ein toter Parameter: sie
        # wurde entgegengesetzt (`forecast_hourly_wh`) und NIE gelesen.
        # Ebenso blieb `data.forecast_hourly_deviations` leer - das Feld
        # existierte, wurde aber nie befuellt.
        #
        # Nutzen: Die Tageskalibrierung vergleicht die ERZEUGTE Tagesenergie
        # mit der Tagesprognose. Das ist ein Mittelwert ueber den ganzen Tag
        # und verwechselt "Prognose zu pessimistisch" mit "Prognose zu
        # optimistisch". Der stuendliche Vergleich zeigt dagegen, ZU WELCHER
        # TAGESZEIT die Prognose danebenliegt - das ist genau die Information,
        # die fuer die PV-Fenster-Berechnung (Ende des PV-Fensters) zaehlt.
        #
        # Ein hour_key- Eintrag bekommt:
        #   forecast: erwartete Erzeugung dieser Stunde in Wh (Prognose W/m2
        #              x Flaeche x 1 h)
        #   actual:   tatsaechlich erzeugte Wh in dieser Stunde
        #   deviation: actual - forecast (positiv = Prognose zu niedrig)
        if forecast_hourly_wh and erzeugung_w is not None and dt_secs > 0:
            vorher = len(self.data.forecast_hourly_deviations)
            self._erfasse_stunden_abweichung(now, forecast_hourly_wh, erzeugung_w, dt_secs)
            # BUGFIX (21.10.2026): Die Abweichungen wurden gesammelt,
            # aber erst beim naechsten Zyklusabschluss gespeichert - das
            # kann 12 Stunden dauern (nachts laeuft keine WP) oder bis
            # zum Neustart. Auf dem Pi stand das Feld dadurch dauerhaft
            # auf "0 Eintraege".
            #
            # Gespeichert wird nur, wenn sich der Bestand geaendert hat
            # (neue Stunde) - das ist selten und schreibt nicht bei
            # jedem 10-Sekunden-Takt auf die Platte.
            if len(self.data.forecast_hourly_deviations) != vorher:
                self._save()

        # Stundensurplus-Profil: Nur bei AUSgeschaltetem Kompressor sampeln,
        # sonst verfaelscht die WP-Leistung das Haushaltsmuster. Persistenz
        # opportunistisch ueber die _save()-Aufrufe der anderen Events.
        #
        # BUGFIX (21.10.2026): Der Key war nur die STUNDE ("8"). Beim
        # Tageswechsel wurde der Zaehler zurueckgesetzt, die
        # Stundenbuckets aber NICHT - der Bucket "8" enthielt danach einen
        # EWMA aus dem Vortag und dem heutigen Tag. Gemessen: der Wert
        # fuer Stunde 8 war nach zwei Tagen 545,7 W statt 900 W, und die
        # Anzeige in der WebApp war um Stunden verschoben.
        #
        # Der Key enthaelt jetzt den TAG: "2026-10-05-08". Damit ist der
        # Bucket wirklich "die Einspeisung am 05.10. um 8 Uhr".
        #
        # BUGFIX (21.10.2026): Der Wert muss der HAUSVERBRAUCH sein,
        # nicht die Einspeisung. Die Einspeisung ist tagsueber
        # "PV minus Haushalt" und nachts 0 - sie beschreibt den
        # PV-Ueberschuss, nicht das Haushaltsmuster. Auf dem Pi zeigte
        # das gelernte Profil consequently ~0 W fuer ALLE Stunden
        # (Werte -4 bis +1 W).
        #
        # Folge: jede Stunde galt als "Surplus-schwach" (< 250 W) und
        # wurde als Mittagstief gezaehlt. Gemessen auf dem Pi:
        #   ohne Profil: brauche 0.7h bis 17:00, Puffer 12.5h
        #   mit Profil:  brauche 6.7h bis 17:00, Puffer  3.5h
        # Der Start wurde also um Stunden zu spaet berechnet.
        #
        # Der Hausverbrauch (acpower - feedin) ist der echte
        # Haushalts-Wert: nachts 487 W, tagsueber je nach Last.
        # Ohne ihn (kein Messwert) wird nicht gesampelt - lieber kein
        # Profil als ein falsches.
        if hausverbrauch_stunden_profil and hausverbrauch_watt is not None \
                and not compressor_is_on:
            key = f"{heute}-{now.hour:02d}"
            wert = max(0.0, float(hausverbrauch_watt))
            alt_e = self.data.surplus_by_hour.get(
                key, {"avg": wert, "n": 0})
            alpha_surplus = self.data.config.surplus_ewma_alpha
            self.data.surplus_by_hour[key] = {
                "avg": round(alt_e["avg"] * (1.0 - alpha_surplus)
                             + wert * alpha_surplus, 1),
                "n": alt_e["n"] + 1}

        # Zyklus-Akkumulation fuer die Quellen-Attribution
        if compressor_is_on and dt_secs > 0:
            self._cycle_secs += dt_secs
            if feedin_watt is not None:
                self._cycle_feedin_ws += feedin_watt * dt_secs
            if soc is not None:
                self._cycle_soc_sum_ws += soc * dt_secs

        # "Zu frueh"-Erkennung: Kamen nach einem Nicht-PV-Zyklus binnen
        # 45 min doch noch >800 W Einspeisung, war der Start verfrueht.
        # Waehrend des offenen Fensters wird die verschenkte PV-Energie
        # (Netzeinspeisung in Wh) integriert und zusammen mit der daraus
        # abgeleiteten Ersparnis in die Warnung geschrieben. Hinweis: Sind
        # mehrere Nicht-PV-Zyklen gleichzeitig offen, wird das gemeinsame
        # Surplus je Pending gezaehlt (vernachlaessigbar in der Praxis).
        if self._pending_zu_frueh:
            noch_offen = []
            for pend in self._pending_zu_frueh:
                ende = self._parse_ts(pend.get("ende"))
                if ende is None:
                    continue
                # JSON-Timestamps sind naive; now ist bereits naiv (wurde in update() reduziert)
                # _parse_ts normalisiert zusaetzlich auf naive (sichert gemischte Formate)
                if feedin_watt is not None and dt_secs > 0:
                    pend["verpasste_wh"] += max(feedin_watt, 0.0) * dt_secs / 3600.0
                if (now - ende).total_seconds() < self.data.config.zu_frueh_fenster_min * 60:
                    noch_offen.append(pend)
                    continue
                if feedin_watt is not None and feedin_watt >= self.data.config.zu_frueh_threshold_w:
                    self.data.zu_frueh_events.append(
                        now.isoformat(timespec="seconds"))
                    # Auf config.zu_frueh_max_entries begrenzen
                    del self.data.zu_frueh_events[:-self.data.config.zu_frueh_max_entries]
                    verpasst = pend.get("verpasste_wh", 0.0)
                    ersparnis = verpasst / 1000.0 * ZU_FRUEH_STROMPREIS_EUR_KWH
                    logging.warning(
                        f"Learning: ZU FRUEH geheizt - 45 min nach "
                        f"Nicht-PV-Zyklus ({pend['ende']}) kommen "
                        f"{feedin_watt:.0f}W Einspeisung "
                        f"(verpasste ~{verpasst:.0f}Wh, "
                        f"~{ersparnis:.2f} EUR PV-Verlust @ "
                        f"{ZU_FRUEH_STROMPREIS_EUR_KWH:.2f} EUR/kWh)")
            self._pending_zu_frueh = noch_offen

        # Heizzyklus erkennen
        if compressor_is_on and not self._last_compressor_state:
            self._cycle_start_time = now
            self._cycle_start_temps = {
                "unten": temp_dict.get("unten"),
                "mittig": temp_dict.get("mittig"),
                "oben": temp_dict.get("oben"),
            }
            self._cycle_feedin_ws = 0.0
            self._cycle_soc_sum_ws = 0.0
            self._cycle_secs = 0.0

        elif not compressor_is_on and self._last_compressor_state and self._cycle_start_time is not None:
            self._finalize_cycle(now, temp_dict)

        # Zapfung erkennen (nur bei ausgeschaltetem Kompressor)
        if not compressor_is_on:
            self._detect_usage(
                now, temp_dict, legionellen_end_time=legionellen_end_time
            )

        self._last_compressor_state = compressor_is_on
        # Komfort-Verletzung erkennen (Punkt B)
        t_oben_aktuell = temp_dict.get("oben")
        if not compressor_is_on and t_oben_aktuell is not None:
            h = now.hour
            nachtsperre = (19 <= h or h < 8)
            self._detect_komfort_verletzung(
                now, t_oben_aktuell, nachtsperre_aktiv=nachtsperre,
                grenz_c=self._komfort_grenz_c,
            )

        # Kopie der Temperaturen speichern (nicht die Referenz auf
        # das temp_dict von außen, um unerwartete Aenderungen zu vermeiden)
        self._last_temps = dict(temp_dict)
        self._last_temp_time = now

    # ── Heizzyklus auswerten ───────────────────────────────

    def _bilanziere_vortag(self, tag: str) -> None:
        """Bildet die Tagesbilanz des VORTAGES und speichert sie.

        Der Haushaltsverbrauch eines Tages ist die ERZEUGTE Energie minus
        die EINSPEISUNG. Die Differenz ist der Verbrauch von Haus und
        Batterie - also genau das, was die Legionellen-Tageswahl braucht:
        ein Tag mit viel Ertrag ist nutzlos, wenn der Haushalt an diesem
        Tag mehr zieht als die PV hergibt.

        Ein Tag mit weniger als 5 Minuten Messdauer wird verworfen: dann
        waere die Bilanz nur ein Ausschnitt des Tages und wuerde den
        Verbrauch unterschaetzen.
        """
        # BUGFIX (21.10.2026): Der Hausverbrauch wird direkt aus der
        # Wechselrichterbilanz akkumuliert
        #   hausverbrauch = acpower - feedin - batPower
        # (inkl. Nachtverbrauch und Batterie). Die frueher genutzte
        # Differenz Erzeugung - Einspeisung erfasste nur den
        # Sonnentag: gemessen 5200 statt 9600 Wh/Tag, weil die
        # naechtlichen 4400 Wh fehlten.
        #
        # Reicht der Tag nicht fuer eine Bilanz, wird er verworfen
        # (lieber keine Zahl als eine falsche).
        if self._day_haus_wh <= 0:
            # Kein Hausverbrauch gemessen - der Tag taugt nicht.
            return
        if self._day_mess_secs < 300:
            # Weniger als 5 Minuten Messdauer: die Bilanz waere nur
            # ein Ausschnitt des Tages und wuerde den Verbrauch
            # unterschaetzen.
            return
        verbrauch = self._day_haus_wh
        self.data.tages_haushalt.append({
            "ts": tag,
            "verbrauch_wh": round(verbrauch, 1),
            "saison": self._saison_von_datum(tag),
        })
        # Historie begrenzen (die Medianberechnung nutzt die letzten
        # Wochen; aeltere Eintraege bringen nichts).
        max_eintraege = 120
        if len(self.data.tages_haushalt) > max_eintraege:
            self.data.tages_haushalt = self.data.tages_haushalt[-max_eintraege:]
        self._save()

    def _erfasse_stunden_abweichung(self, now: datetime,
                                     forecast_hourly_wh: Dict[int, float],
                                     erzeugung_w: float, dt_secs: float):
        """Sammelt die stuendliche Prognose-Abweichung.

        BUGFIX (21.10.2026): Fuellt `data.forecast_hourly_deviations`, das
        seit Anlage existierte, aber nie befuellt wurde - und nutzt damit
        den bis dahin toten Parameter `forecast_hourly_wh`.

        Je Stunde wird ein EWMA ueber
            actual   = erzeugte Energie dieser Stunde (Wh)
            forecast = erwartete Energie dieser Stunde (Wh)
        gebildet. Die Abweichung (actual - forecast) zeigt, OB und WIE
        stark die Prognose danebenliegt.

        Wichtig: Die Stundenwerte werden pro Kalendertag getrennt
        gesammelt (Schluessel "YYYY-MM-DD-HH"), damit der Vergleich
        taeglich neu bewertet wird. Eine alte Prognose vom Vortag darf
        nicht in die heutige Bewertung einlaufen.

        Nur Stunden MIT Prognose werden erfasst - Nachtstunden ohne
        Prognose (0 W) wuerden sonst als "Prognose daneben" zaehlen.
        """
        flaeche_qm = float(getattr(self.data.config, "pv_array_size_qm", 0.0) or 0.0)
        if flaeche_qm <= 0:
            return
        stunde = now.hour
        wm2 = forecast_hourly_wh.get(stunde)
        if wm2 is None:
            return
        try:
            wm2 = float(wm2)
        except (TypeError, ValueError):
            return
        if wm2 <= 0:
            return
        # Prognose-Wh dieser Stunde: W/m2 x m2 x Zeitanteil
        forecast_wh = wm2 * flaeche_qm * (dt_secs / 3600.0)
        actual_wh = max(erzeugung_w, 0.0) * dt_secs / 3600.0
        schluessel = f"{now.strftime('%Y-%m-%d')}-{stunde:02d}"
        alt = self.data.forecast_hourly_deviations.get(schluessel)
        if alt and alt.get("n"):
            alpha = self.data.config.surplus_ewma_alpha
            forecast_wh = (alt.get("forecast", forecast_wh) * (1 - alpha)
                           + forecast_wh * alpha)
            actual_wh = (alt.get("actual", actual_wh) * (1 - alpha)
                         + actual_wh * alpha)
            n = int(alt.get("n", 0)) + 1
        else:
            n = 1
        self.data.forecast_hourly_deviations[schluessel] = {
            "forecast": round(forecast_wh, 2),
            "actual": round(actual_wh, 2),
            "deviation": round(actual_wh - forecast_wh, 2),
            "n": n,
        }
        # Alte Eintraege verwerfen: die Auswertung interessiert nur die
        # letzten Tage, sonst waechst die Datei unbegrenzt.
        grenze = (now - timedelta(days=14)).strftime("%Y-%m-%d")
        if len(self.data.forecast_hourly_deviations) > 24 * 14 * 2:
            self.data.forecast_hourly_deviations = {
                k: v for k, v in self.data.forecast_hourly_deviations.items()
                if k[:10] >= grenze
            }

    def _kalibriere_forecast(self, now: datetime, heute: str,
                             forecast_today_wh_qm: Optional[float]):
        """Taegliche Kalibrierung (ab config.forecast_kalibrierung_ab_stunde, einmal pro Tag).

        Verhaeltnis der TATSACHLICH ERZEUGTEN Energie (Wh, aus `acpower`
        integriert) zur Tagesprognose (Wh/m2 mal Arrayflaeche) als EWMA
        (alpha=config.forecast_ewma_alpha), geklemmt auf
        config.forecast_ratio_min..config_ratio_max.

        Wichtig: als Zaehler dient die Erzeugung, NICHT die Netzeinspeisung.
        Mit dem Export im Zaehler war der Quotient fachlich
        ``1 - Hausanteil an der PV`` und lag dadurch dauerhaft unter 1
        (beobachtet x0,62). Die Prognose wurde also nicht am
        Hausverbrauch kalibriert, sondern systematisch zu pessimistisch
        bewertet - CalcStart wartete daraufhin zu lange.
        """
        cfg = self.data.config
        if self._kalibriert_datum == heute or now.hour < cfg.forecast_kalibrierung_ab_stunde:
            return
        self._kalibriert_datum = heute
        # Forecast-Werte werden an der Integrationsgrenze ausdrücklich
        # normalisiert. Keine Größen-Heuristik: 50 Wh/m² sind ein gültiger
        # niedriger Tageswert und dürfen nicht zu 50.000 Wh/m² werden.
        # Einheiten: Der Aufrufer (priority_control_logic) liefert die
        # HEUTE-Prognose bereits normalisiert in Wh/m2. Hier wird sie
        # deshalb NICHT erneut umgerechnet.
        #
        # Bug (2026-09): value_unit="wh_m2" las einen bereits in Wh/m2
        # liegenden Wert als kWh/m2. 5060 Wh/m2 wurden so zu "5.06 Wh/m2"
        # und lagen immer unter der 1000-Wh/m2-Schranke. Folge: keine
        # Kalibrierung, Faktor dauerhaft x1 und "samples 0/3" in der WebApp.
        if forecast_today_wh_qm is None or forecast_today_wh_qm < 1000:
            logging.info("Learning: Kalibrierung uebersprungen "
                         "(keine brauchbare Tagesprognose)")
            return
        if self._day_pv_wh <= 50:
            logging.info("Learning: Kalibrierung uebersprungen "
                         "(zu wenig PV-Erzeugungsdaten heute)")
            return
        # Einheiten: _day_pv_wh ist Wh (ERZEUGTE Energie aus acpower),
        # forecast_today_wh_qm ist Wh/m2. Fuer einen Quotienten wird die
        # Prognose mit der Arrayflaeche in Wh umgerechnet. Ohne das war
        # der Quotient um die Flaeche (~10x) zu gross und klemmte im
        # Testlauf dauerhaft am forecast_ratio_max.
        flaeche_qm = float(getattr(cfg, "pv_array_size_qm", 0.0) or 0.0)
        prognose_wh = float(forecast_today_wh_qm) * flaeche_qm
        if prognose_wh <= 0:
            logging.info("Learning: Kalibrierung uebersprungen "
                         "(keine PV-Arrayflaeche konfiguriert)")
            return
        ratio = max(cfg.forecast_ratio_min, min(
            cfg.forecast_ratio_max, self._day_pv_wh / prognose_wh))
        alpha = cfg.forecast_ewma_alpha
        n = self.data.forecast_ratio_samples + 1
        self.data.forecast_ratio = (
            round(ratio, 3) if n <= 1
            else round(self.data.forecast_ratio * (1 - alpha) + ratio * alpha, 3))
        self.data.forecast_ratio_samples = n
        self._save()
        logging.info(
            f"Learning: Forecast-Kalibrierung {heute}: Erzeugung "
            f"{self._day_pv_wh:.0f}Wh (Einspeisung {self._day_surplus_wh:.0f}Wh) "
            f"/ Prognose {prognose_wh:.0f}Wh ({forecast_today_wh_qm:.0f}Wh/m2 x "
            f"{flaeche_qm:.0f}m2) -> Faktor "
            f"{self.data.forecast_ratio:.2f} (n={n})")

    def _finalize_cycle(self, now: datetime, temp_dict: Dict[str, Optional[float]]):
        """Wertet einen abgeschlossenen Heizzyklus aus."""
        if self._cycle_start_time is None or self._cycle_start_temps is None:
            return

        start = self._cycle_start_time
        end = now
        duration_min = (end - start).total_seconds() / 60.0

        if duration_min < 5:
            self._cycle_start_time = None
            return

        start_unten = self._cycle_start_temps.get("unten") or 0
        end_unten = temp_dict.get("unten") or 0
        start_mitte = self._cycle_start_temps.get("mittig") or 0
        end_mitte = temp_dict.get("mittig") or 0

        delta_unten = max(0.1, end_unten - start_unten)
        delta_mitte = max(0.1, end_mitte - start_mitte)

        rate_unten = delta_unten / (duration_min / 60.0)
        rate_gesamt = delta_mitte / (duration_min / 60.0)
        season = _get_season(end.month)

        # Quellen-Attribution: Mittelwerte aus den Zyklus-Akkumulatoren
        avg_feedin = (self._cycle_feedin_ws / self._cycle_secs
                      if self._cycle_secs > 0 else None)
        avg_soc = (self._cycle_soc_sum_ws / self._cycle_secs
                   if self._cycle_secs > 0 else None)
        quelle = "gemischt"
        if avg_feedin is not None:
            if avg_feedin >= 400.0:
                quelle = "pv"
            elif avg_soc is not None and avg_soc >= 90.0 and avg_feedin >= -50.0:
                quelle = "batterie"
            elif avg_feedin < -50.0:
                quelle = "netz"
        elif avg_soc is not None and avg_soc >= 90.0:
            quelle = "batterie"

        cycle = HeatingCycle(
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            start_temp_unten=round(start_unten, 1),
            end_temp_unten=round(end_unten, 1),
            start_temp_mitte=round(start_mitte, 1),
            end_temp_mitte=round(end_mitte, 1),
            duration_min=round(duration_min, 1),
            rate_unten_c_h=round(rate_unten, 2),
            rate_gesamt_c_h=round(rate_gesamt, 2),
            season=season,
            avg_feedin_watt=round(avg_feedin, 0) if avg_feedin is not None else None,
            avg_soc=round(avg_soc, 1) if avg_soc is not None else None,
            quelle=quelle,
        )

        self.data.cycles.append(asdict(cycle))

        # Laufzeit je Quelle + Zu-frueh-Pruefung vormerken (Nicht-PV only)
        self.data.runtime_by_quelle_sec[quelle] = round(
            self.data.runtime_by_quelle_sec.get(quelle, 0.0)
            + self._cycle_secs, 1)
        if quelle != "pv":
            self._pending_zu_frueh.append(
                {"ende": end.isoformat(), "verpasste_wh": 0.0})
        self._cycle_feedin_ws = 0.0
        self._cycle_soc_sum_ws = 0.0
        self._cycle_secs = 0.0
        if len(self.data.cycles) > self.data.config.max_cycles:
            self.data.cycles = self.data.cycles[-self.data.config.max_cycles:]

        # Saisonale Heizrate: exponentiell geglaetteter Mittelwert (EWMA,
        # alpha=0.10) statt kumulativer Mittelwert - reagiert auf
        # Jahreszeit/Sensoraenderungen, statt fuer immer am alten Wert zu kleben.
        #
        # NUR aus repräsentativen Zyklen lernen: kurze Läufe messen die
        # lokale Reaktion des unteren Knotens. Ueber sie gemittelt ergab sich
        # im Log 18.09.-01.10.2026 eine Rate von 13,86 K/h gegenueber einer
        # nachhaltigen Rate von 4,73 K/h - der Startvorlauf des CalcStart
        # war dadurch rund zwei Drittel zu kurz (Belege: "brauche 1.1h" bei
        # physikalisch noetigen 3,96 h am 28.09., sowie "ZU SPAET!" am 30.09.).
        hr = self.data.heat_rates.get(season, {"avg": 3.0, "count": 0})
        lern_min = float(getattr(self.data.config, "heating_rate_lern_min_minuten", 0.0))
        rate_max = float(getattr(self.data.config, "heating_rate_max_c_h", 0.0))
        # BEFUND 18.10.2026: `delta_unten` wird oben auf 0,1 K geklemmt, damit
        # die Zyklus-Rate nie 0 wird. Ein Zyklus, in dem der Fuehler NICHT
        # gestiegen ist (Zapfung waehrend des Laufs, Abkuehlphase), wurde damit
        # als "0,54 C/h" bzw. "0,0 C/h" gezaehlt und zog das Lernmittel nach
        # unten: Im Betriebslog 14.09.-18.10.2026 standen 11-min-Zyklen mit
        # 0,54 C/h bei fallender Temperatur und ein 1215-min-"Zyklus" mit
        # 0,0 C/h. Bei alpha=0,1 senkt ein solcher Eintrag den Mittelwert um
        # 10 % - die Planungsrate (Heizplan) ist aber die Grundlage des
        # gesamten CalcStart-Fahrplans.
        anstieg_k = end_unten - start_unten
        min_anstieg = float(
            getattr(self.data.config, "heating_rate_min_anstieg_k", 0.5))
        lang_genug = duration_min >= lern_min if lern_min > 0 else True
        gewaermt = anstieg_k >= max(min_anstieg, 0.0)
        zaehlwert = lang_genug and gewaermt
        if not zaehlwert:
            # Zyklus wird weiter protokolliert, zaehlt aber nicht zur Rate.
            new_avg, count = hr.get("avg", 3.0), hr.get("count", 0)
            if not lang_genug:
                grund = ", zu kurz fuer die Rate"
            elif anstieg_k <= 0:
                grund = f", keine Aufwaermung (unten {anstieg_k:+.1f}K)"
            else:
                grund = f", Anstieg {anstieg_k:.1f}K < {min_anstieg:.1f}K"
        else:
            grund = ""
            rate_fuer_lernen = rate_unten
            gekappt = False
            if rate_max > 0 and rate_fuer_lernen > rate_max:
                rate_fuer_lernen = rate_max
                gekappt = True
            count = hr["count"] + 1
            if count <= 1:
                new_avg = rate_fuer_lernen
            else:
                alpha_hr = self.data.config.heating_rate_ewma_alpha
                new_avg = hr["avg"] * (1.0 - alpha_hr) + rate_fuer_lernen * alpha_hr
            if gekappt:
                logging.info(
                    "Heizrate auf %.1f C/h gekappt (Zyklus meldete %.1f C/h) - "
                    "lokaler Knoten, nicht der ganze Speicher",
                    rate_max, rate_unten,
                )
        self.data.heat_rates[season] = {
            "avg": round(new_avg, 3),
            "count": count,
        }

        self._save()
        logging.info(
            f"Learning: Heizzyklus - {duration_min:.0f}min, "
            f"unten {start_unten:.1f}->{end_unten:.1f}C = {rate_unten:.2f}C/h "
            f"({season}, MW={new_avg:.2f}, n={count}" + grund + ")"
        )

        self._cycle_start_time = None
        self._cycle_start_temps = None

    # ── Zapfung erkennen ───────────────────────────────────

    def _detect_usage(
        self, now: datetime, temp_dict: Dict[str, Optional[float]], legionellen_end_time: Optional[datetime] = None
    ):
        """
        Erkennt Warmwasser-Zapfung durch Temperaturabfall an ALLEN drei
        Fühler (unten kühlt zuerst, mitte folgt, oben zuletzt).
        
        Schwellwerte pro Fühler:
          - unten:  0.5 K (empfindlichster, kühlt zuerst)
          - mitte:  0.8 K
          - oben:   1.5 K (bisheriger Schwellwert)
        
        Ein Ereignis wird erkannt, wenn mindestens EIN Fühler seinen
        Schwellwert überschreitet. Alle drei Abfälle werden gespeichert
        und fließen in die Startzeitenberechnung (CalcStart) ein.
        """
        if not (5 <= now.hour < 23):
            return

        # Legionellen-Filter: Nach Legionellenfahrten kühlt der Boiler ab
        # (Gradient: oben 60°C, unten 45°C). Das darf nicht als Zapfung 
        # interpretiert werden. Filtere abkühlende Phase nach Legionellen.
        if legionellen_end_time is not None:
            try:
                end_naiv = to_naive(legionellen_end_time)
                now_naiv = to_naive(now)
                if (now_naiv - end_naiv).total_seconds() / 3600.0 < 4.0:
                    return
            except (TypeError, ValueError):
                pass

        # Temperaturwerte von aktuell und vorherigem Zyklus
        current = {
            "unten": temp_dict.get("unten"),
            "mittig": temp_dict.get("mittig"),
            "oben": temp_dict.get("oben"),
        }
        previous = {
            "unten": self._last_temps.get("unten") if self._last_temps else None,
            "mittig": self._last_temps.get("mittig") if self._last_temps else None,
            "oben": self._last_temps.get("oben") if self._last_temps else None,
        }

        # Prüfen ob alle benötigten Sensoren Werte liefern
        if any(current[s] is None for s in ["unten", "mittig"]):
            return
        if all(previous[s] is None for s in ["unten", "mittig", "oben"]):
            return

        # Temperaturabfall je Fühler berechnen
        drops = {}
        for sensor in ["unten", "mittig", "oben"]:
            if current[sensor] is not None and previous[sensor] is not None:
                drops[sensor] = previous[sensor] - current[sensor]
            else:
                drops[sensor] = 0.0

        # Schwellwerte pro Fühler
        SCHWELLWENGE = {
            "unten": 0.5,   # kühlt zuerst, niedriger Schwellwert
            "mittig": 0.8,
            "oben": 1.5,    # bester Schwellwert
        }

        # Zapfungs-Verfolgung (Befund 18.10.2026): Einen laufenden Vorgang
        # zuerst weiterverfolgen - waehrend einer Zapfung entstehen weitere
        # Abfaelle, aber kein zweites Ereignis.
        cfg = self.data.config
        dt_min = 0.0
        if isinstance(self._last_temp_time, datetime):
            dt_min = max((now - self._last_temp_time).total_seconds() / 60.0, 0.0)
        if self._pending_usage is not None:
            self._verfolge_usage(now, current, previous, dt_min)
            return

        # Ereignis erkennen: mindestens EIN Fühler ueberschreitet Schwellwert
        if not any(drops[s] >= SCHWELLWENGE[s] for s in drops):
            return

        if self._last_temp_time is None:
            return

        # Ein einziger Zapfvorgang erzeugt ueber mehrere Loop-Samples hinweg
        # viele Temperaturabfaelle. Ohne Entprellung wurden daraus bis zu 17
        # Events und die Lernzeit/-fenster wurden mit einem einzigen Vorgang
        # mehrfach gewichtet. Seit dem Befund vom 18.10.2026 zaehlt das Fenster
        # ab dem ENDE des letzten Vorgangs (vorher ab dessen Beginn) - der
        # laufende Vorgang wird ohnehin verfolgt, der Cooldown schuetzt nur
        # gegen Doppelzaehlung.
        cooldown_s = max(0, int(cfg.usage_event_cooldown_min)) * 60
        if self._last_usage_event_time is not None:
            try:
                seit_event = to_naive(now) - to_naive(self._last_usage_event_time)
                if 0 <= seit_event.total_seconds() < cooldown_s:
                    return
            except (TypeError, ValueError):
                pass

        # Vorgang eroeffnen: Beginn, Ausgangswerte und die erste Flanke.
        self._pending_usage = {
            "start": now,
            "bewegung": now,
            "before": dict(previous),
            "after": dict(current),
            "max_drop": {s: max(0.0, drops.get(s, 0.0)) for s in SCHWELLWENGE},
            #: Abfall, der in der STELLEN Phase entstanden ist - nur er
            #: entscheidet, ob der Vorgang als Zapfung gelernt wird.
            "schnell_drop": {s: max(0.0, drops.get(s, 0.0)) for s in SCHWELLWENGE},
            "max_slope": {
                s: (max(0.0, drops.get(s, 0.0)) / dt_min if dt_min > 0 else 0.0)
                for s in SCHWELLWENGE
            },
        }

    def _verfolge_usage(self, now, current, previous, dt_min):
        """Verfolgt einen laufenden Zapf-Vorgang bis zur Ruhe oder Fenstergrenze.

        Gespeichert wird der maximale Abfall ab Vorgangsbeginn (nicht die Summe
        der Sample-Deltas: der Speicher erholt sich zwischen zwei Zapfungen).
        Angesammelt wird nur in der STELLEN Phase - die Flanke wird aus dem
        Schritt zum VORIGEN Sample gebildet, nicht aus dem kumulierten Abfall:
        sonst wirkte auch langsames Abkuehlen (2 K ueber 20 min) wie ein
        schneller Abfall.
        """
        pend = self._pending_usage
        cfg = self.data.config
        grenze = float(cfg.usage_event_min_slope_k_min)
        for sensor in list(pend["max_drop"]):
            vorher = pend["before"].get(sensor)
            letzter = previous.get(sensor)
            jetzt = current.get(sensor)
            if vorher is None or jetzt is None:
                continue
            abfall = vorher - jetzt
            schritt = 0.0
            if letzter is not None:
                schritt = max(0.0, letzter - jetzt)
            flanke = (schritt / dt_min) if dt_min > 0 else 0.0
            if flanke >= grenze:
                # steile Phase: hier entsteht die Zapfung
                pend["schnell_drop"][sensor] = max(
                    pend["schnell_drop"].get(sensor, 0.0), abfall)
                pend["bewegung"] = now
            if abfall > pend["max_drop"][sensor]:
                # Gesamtabfall seit Vorgangsbeginn (auch die flache Auslauf-
                # phase des Zapfstrahls) - wird protokolliert, nicht bewertet.
                pend["max_drop"][sensor] = abfall
                pend["after"][sensor] = jetzt
            if flanke > pend["max_slope"][sensor]:
                pend["max_slope"][sensor] = flanke

        ruhe_min = (now - pend["bewegung"]).total_seconds() / 60.0
        alter_min = (now - pend["start"]).total_seconds() / 60.0
        if (ruhe_min >= float(cfg.usage_event_ruhe_min)
                or alter_min >= float(cfg.usage_event_fenster_max_min)):
            self._finalisiere_usage(now)

    def _finalisiere_usage(self, now):
        """Legt den verfolgten Vorgang als Ereignis ab und lernt die Zielzeit.

        Gelernt wird nur ein PLAUSIBLER Vorgang (staerkster Abfall UND steilste
        Flanke ueber den Grenzen). Sensorrauschen und das langsame Abkuehlen des
        Speichers werden protokolliert (WebApp/Diagnose), veraendern die
        gelernte Zielzeit aber nicht - vorher waren 26 von 27 Ereignissen
        solche Kleinabfaelle (Betriebslog 14.09.-18.10.2026).
        """
        pend = self._pending_usage
        self._pending_usage = None
        if pend is None:
            return
        cfg = self.data.config
        max_drop = pend["max_drop"]
        schnell_drop = pend.get("schnell_drop", max_drop)
        max_slope = pend["max_slope"]
        gewichte = {"unten": 1.0, "mittig": 0.5, "oben": 0.25}
        drop_gesamt = sum(max_drop.get(s, 0.0) * gewichte[s] for s in gewichte)
        # Geurteilt wird ueber die STELLE Phase: ein 0,5-K-Sprung plus danach
        # 2 K langsames Abkuehlen ergaebe sonst eine "Zapfung" von 2,5 K.
        staerkster = max(schnell_drop.values()) if schnell_drop else 0.0
        steilster = max(max_slope.values()) if max_slope else 0.0
        plausibel = staerkster >= float(cfg.usage_event_min_drop_k)

        def _wert(werte, sensor):
            wert = werte.get(sensor)
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                return round(float(wert), 1)
            return 0.0

        event = UsageEvent(
            timestamp=pend["start"].isoformat(),
            temp_before_unten=_wert(pend["before"], "unten"),
            temp_before_mitte=_wert(pend["before"], "mittig"),
            temp_before_oben=_wert(pend["before"], "oben"),
            temp_after_unten=_wert(pend["after"], "unten"),
            temp_after_mitte=_wert(pend["after"], "mittig"),
            temp_after_oben=_wert(pend["after"], "oben"),
            drop_unten_k=_wert(max_drop, "unten"),
            drop_mitte_k=_wert(max_drop, "mittig"),
            drop_oben_k=_wert(max_drop, "oben"),
            drop_gesamt_k=round(drop_gesamt, 2),
            plausibel=plausibel,
        )
        self.data.usage_events.append(asdict(event))
        # Cooldown ab dem ENDE des Vorgangs.
        self._last_usage_event_time = now
        if len(self.data.usage_events) > cfg.max_usage_events:
            self.data.usage_events = self.data.usage_events[-cfg.max_usage_events:]

        uhrzeit = pend["start"].strftime("%H:%M")
        dauer_min = (now - pend["start"]).total_seconds() / 60.0
        if not plausibel:
            logging.info(
                f"Learning: Zapfung {uhrzeit} nur protokolliert "
                f"({dauer_min:.0f} min, unten {max_drop.get('unten', 0.0):.1f}K, "
                f"mitte {max_drop.get('mittig', 0.0):.1f}K, "
                f"oben {max_drop.get('oben', 0.0):.1f}K, gew. {drop_gesamt:.2f}K; "
                f"steile Phase nur {staerkster:.1f}K < "
                f"{float(cfg.usage_event_min_drop_k):.1f}K, steilste Flanke "
                f"{steilster:.1f}K/min) -> Zielzeit unveraendert"
            )
            self._save()
            return

        # Nur erste Zapfung(en) pro Tageshaelfte beruecksichtigen
        ist_morgen = now.hour < 12
        today_str = now.strftime("%Y-%m-%d")
        today_events = [
            e for e in self.data.usage_events
            if e["timestamp"].startswith(today_str)
            and (ts := self._parse_ts(e["timestamp"])) is not None
            and (ts.hour < 12) == ist_morgen
            and e.get("plausibel", True)
        ]
        if len(today_events) <= cfg.max_usage_per_half_day:
            hour_f = pend["start"].hour + pend["start"].minute / 60.0
            # EWMA (alpha=0.15): reagiert auf Veraenderungen des
            # Duschverhaltens schneller als ein kumulativer Mittelwert
            if ist_morgen:
                count = self.data.morning_target_hour_samples + 1
                alt = self.data.learned_morning_target_hour
                alpha_t = cfg.target_hour_ewma_alpha
                new_target = hour_f if count <= 1 else alt * (1.0 - alpha_t) + hour_f * alpha_t
                self.data.learned_morning_target_hour = round(new_target, 2)
                self.data.morning_target_hour_samples = count
            else:
                count = self.data.target_hour_samples + 1
                alt = self.data.learned_target_hour
                alpha_t = cfg.target_hour_ewma_alpha
                new_target = hour_f if count <= 1 else alt * (1.0 - alpha_t) + hour_f * alpha_t
                self.data.learned_target_hour = round(new_target, 2)
                self.data.target_hour_samples = count
            self._save()
            logging.info(
                f"Learning: Zapfung {uhrzeit} "
                f"({dauer_min:.0f} min, unten {max_drop.get('unten', 0.0):.1f}K, "
                f"mitte {max_drop.get('mittig', 0.0):.1f}K, "
                f"oben {max_drop.get('oben', 0.0):.1f}K, gew. {drop_gesamt:.2f}K) -> "
                f"{'Morgen-' if ist_morgen else 'Abend-'}"
                f"Zielzeit {new_target:.1f}h (n={count})"
            )

    def get_recent_usage_events(
        self,
        hours: int = 2,
        now: Optional[datetime] = None,
        nur_unkompensierte: bool = True,
    ) -> List[Dict]:
        """Gibt Zapfungsereignisse zurueck, deren Temperaturverlust die
        Startzeitberechnung noch beeinflusst.

        Zwei Filter:

        1. ``hours`` - harte Obergrenze gegen sehr alte Eintraege.
        2. ``nur_unkompensierte`` - Ereignisse, die VOR dem zuletzt
           abgeschlossenen Heizzyklus liegen, fallen weg. Grund: sobald
           danach geheizt wurde, ist der Temperaturverlust ausgeglichen und
           der CalcStart darf den Start NICHT mehr verfruehen. Ohne diesen
           Filter zog derselbe Abzug ueber Stunden weiter den Start vor
           (Beobachtung 28.09.2026: Zapfung 13:57, Abzug um 14:48 noch
           aktiv - 0.25h dauerhafte Verfruehung des Starts).

        Der 2-h-Filter allein war dafuer nicht praezise genug: Innerhalb von
        zwei Stunden kann problemlos ein kompletter Heizzyklus gelaufen
        sein, der den Verlust laengst ausgeglichen hat.
        """
        if now is None:
            now = datetime.now()
        grenze = to_naive(now - timedelta(hours=hours))
        treffer = [
            e for e in self.data.usage_events
            if (ts := self._parse_ts(e.get("timestamp"))) is not None and ts >= grenze
            # Nur plausible Zapfungen: ein Kleinabfall (Rauschen) darf die
            # Startzeit nicht verfruehen (Befund 18.10.2026).
            and e.get("plausibel", True)
        ]
        if nur_unkompensierte:
            letzter_zyklus = self._letzter_zyklus_ende()
            if letzter_zyklus is not None:
                treffer = [
                    e for e in treffer
                    if (ts := self._parse_ts(e.get("timestamp"))) is not None
                    and ts > letzter_zyklus
                ]
        return treffer

    def _letzter_zyklus_ende(self) -> Optional[datetime]:
        """Ende des juengsten abgeschlossenen Heizzyklus (naiv) oder None."""
        if not self.data.cycles:
            return None
        letzter = self.data.cycles[-1]
        if isinstance(letzter, dict):
            return self._parse_ts(letzter.get("end_time"))
        return None
