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

    # ── Zielzeit (Zapfverhalten) ──
    target_hour_ewma_alpha: float = 0.15
    target_hour_min_samples: int = 3
    max_usage_events: int = 100
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
    surplus_by_hour: Dict[str, Dict[str, float]] = field(default_factory=dict)

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
        self._cycle_start_time: Optional[datetime] = None
        self._cycle_start_temps: Optional[Dict[str, float]] = None
        self._last_temps: Optional[Dict[str, Optional[float]]] = None
        self._last_temp_time: Optional[datetime] = None
        self._komfort_grenz_c: float = self.data.config.comfort_grenz_c
        # Solar-Tracking (Attribution/Kalibrierung/Surplus-Profil)
        self._cycle_feedin_ws: float = 0.0   # Zeitintegral Einspeisung [Ws]
        self._cycle_soc_sum_ws: float = 0.0
        self._cycle_secs: float = 0.0
        self._pending_zu_frueh: List[Dict] = []   # nur im RAM: {"ende": iso, "verpasste_wh": Wh}
        self._day_surplus_wh: float = 0.0
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
                    surplus_by_hour=raw.get("surplus_by_hour", {}),
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
        return round(hr["avg"] * factor, 2)

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
        zaehlt die Verletzung (ausserhalb Nachtsperre, max. max_pro_tag)."""
        if t_oben is None or t_oben >= grenz_c or nachtsperre_aktiv:
            return
        heute = now.strftime("%Y-%m-%d")
        heute_mitternacht = to_naive(
            now.replace(hour=0, minute=0, second=0, microsecond=0))
        heute_count = sum(
            1 for v in self.data.komfort_verletzungen
            if (ts := self._parse_ts(v)) is not None and ts >= heute_mitternacht
        )
        if heute_count >= self.data.config.comfort_max_pro_tag:
            return
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

    def get_forecast_hourly_deviations(self) -> Dict:
        """Stundenscharfe Forecast-Abweichungen {stunde: {forecast, actual, deviation}}.

        deviation = actual - forecast (positiv = Forecast zu niedrig, negativ = zu hoch)."""
        return dict(self.data.forecast_hourly_deviations)

    def get_surplus_profile(self):
        """Stundensurplus-Profil {stunde: watt} oder None (noch unbrauchbar).

        Eine Stunde gilt als brauchbar ab n>=5 Samples; das Profil als
        Ganzes ab 4 brauchbaren Stunden (Tageslicht-Luecken erlaubt)."""
        nutzbar = {}
        for key, e in self.data.surplus_by_hour.items():
            try:
                stunde = int(key)
            except (TypeError, ValueError):
                continue
            if e.get("n", 0) >= 5:
                nutzbar[stunde] = float(e["avg"])
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
        soc: Optional[float] = None,
        forecast_today_wh_qm: Optional[float] = None,
        legionellen_end_time: Optional[datetime] = None,
        forecast_hourly_wh: Optional[Dict[int, float]] = None,
    ):
        """
        Wird jeden Regelzyklus aufgerufen.
        Erkennt Heizzykus-Start/Ende und Warmwasser-Zapfung sowie
        Quellen-Attribution, Forecast-Kalibrierung und Surplus-Profil.
        """
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

        # Tages-Surplus integrieren (positive Netzeinspeisung = echter
        # Ueberschuss) und abends gegen die Prognose kalibrieren (B).
        heute = now.strftime("%Y-%m-%d")
        if self._surplus_tag != heute:
            self._surplus_tag = heute
            self._day_surplus_wh = 0.0
        if feedin_watt is not None and dt_secs > 0:
            self._day_surplus_wh += max(feedin_watt, 0.0) * dt_secs / 3600.0
        self._kalibriere_forecast(now, heute, forecast_today_wh_qm)

        # Stundensurplus-Profil: Nur bei AUSgeschaltetem Kompressor sampeln,
        # sonst verfaelscht die WP-Leistung das Haushaltsmuster. Persistenz
        # opportunistisch ueber die _save()-Aufrufe der anderen Events.
        if feedin_watt is not None and not compressor_is_on:
            key = str(now.hour)
            alt_e = self.data.surplus_by_hour.get(
                key, {"avg": float(feedin_watt), "n": 0})
            alpha_surplus = self.data.config.surplus_ewma_alpha
            self.data.surplus_by_hour[key] = {
                "avg": round(alt_e["avg"] * (1.0 - alpha_surplus)
                             + float(feedin_watt) * alpha_surplus, 1),
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

    def _kalibriere_forecast(self, now: datetime, heute: str,
                             forecast_today_wh_qm: Optional[float]):
        """Taegliche Kalibrierung (ab config.forecast_kalibrierung_ab_stunde, einmal pro Tag).

        Verhaeltnis tatsaechlicher Netzeinschuss (Wh, integriert) zur
        Tagesprognose (Wh/m2) als EWMA (alpha=config.forecast_ewma_alpha),
        geklemmt auf config.forecast_ratio_min..forecast_ratio_max.
        Lernt den HAUSspezifischen Langfehler des Forecast-Dienstes inkl.
        typischem Eigenverbrauchsniveau.
        """
        cfg = self.data.config
        if self._kalibriert_datum == heute or now.hour < cfg.forecast_kalibrierung_ab_stunde:
            return
        self._kalibriert_datum = heute
        if forecast_today_wh_qm is None or forecast_today_wh_qm < 1000:
            logging.info("Learning: Kalibrierung uebersprungen "
                         "(keine brauchbare Tagesprognose)")
            return
        if self._day_surplus_wh <= 50:
            logging.info("Learning: Kalibrierung uebersprungen "
                         "(zu wenig Surplus-Daten heute)")
            return
        ratio = max(cfg.forecast_ratio_min, min(
            cfg.forecast_ratio_max, self._day_surplus_wh / float(forecast_today_wh_qm)))
        alpha = cfg.forecast_ewma_alpha
        n = self.data.forecast_ratio_samples + 1
        self.data.forecast_ratio = (
            round(ratio, 3) if n <= 1
            else round(self.data.forecast_ratio * (1 - alpha) + ratio * alpha, 3))
        self.data.forecast_ratio_samples = n
        self._save()
        logging.info(
            f"Learning: Forecast-Kalibrierung {heute}: Surplus "
            f"{self._day_surplus_wh:.0f}Wh / Prognose "
            f"{forecast_today_wh_qm:.0f}Wh/qm -> Faktor "
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
        hr = self.data.heat_rates.get(season, {"avg": 3.0, "count": 0})
        count = hr["count"] + 1
        if count <= 1:
            new_avg = rate_unten
        else:
            alpha_hr = self.data.config.heating_rate_ewma_alpha
            new_avg = hr["avg"] * (1.0 - alpha_hr) + rate_unten * alpha_hr
        self.data.heat_rates[season] = {
            "avg": round(new_avg, 3),
            "count": count,
        }

        self._save()
        logging.info(
            f"Learning: Heizzyklus - {duration_min:.0f}min, "
            f"unten {start_unten:.1f}->{end_unten:.1f}C = {rate_unten:.2f}C/h "
            f"({season}, MW={new_avg:.2f}, n={count})"
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

        # Ereignis erkennen: mindestens EIN Fühler ueberschreitet Schwellwert
        if not any(drops[s] >= SCHWELLWENGE[s] for s in drops):
            return

        if self._last_temp_time is None:
            return

        # Gewichteten Gesamt-Abfall berechnen
        # unten hat das hoechste Gewicht (1.0), mitte (0.5), oben (0.25)
        # da unten der Hauptindikator fuer Warmwasserentnahme ist
        gewichte = {"unten": 1.0, "mittig": 0.5, "oben": 0.25}
        drop_gesamt = sum(
            drops[s] * gewichte[s] for s in drops
        )

        event = UsageEvent(
            timestamp=now.isoformat(),
            temp_before_unten=round(previous["unten"], 1) if previous["unten"] is not None else 0.0,
            temp_before_mitte=round(previous["mittig"], 1) if previous["mittig"] is not None else 0.0,
            temp_before_oben=round(previous["oben"], 1) if previous["oben"] is not None else 0.0,
            temp_after_unten=round(current["unten"], 1),
            temp_after_mitte=round(current["mittig"], 1),
            temp_after_oben=round(current["oben"], 1) if current["oben"] is not None else 0.0,
            drop_unten_k=round(drops["unten"], 1),
            drop_mitte_k=round(drops["mittig"], 1),
            drop_oben_k=round(drops["oben"], 1),
            drop_gesamt_k=round(drop_gesamt, 2),
        )
        self.data.usage_events.append(asdict(event))
        if len(self.data.usage_events) > self.data.config.max_usage_events:
            self.data.usage_events = self.data.usage_events[-self.data.config.max_usage_events:]

        # Nur erste Zapfung(en) pro Tageshaelfte beruecksichtigen
        ist_morgen = now.hour < 12
        today_str = now.strftime("%Y-%m-%d")
        today_events = [
            e for e in self.data.usage_events
            if e["timestamp"].startswith(today_str)
            and (ts := self._parse_ts(e["timestamp"])) is not None
            and (ts.hour < 12) == ist_morgen
        ]
        if len(today_events) <= self.data.config.max_usage_per_half_day:
            hour_f = now.hour + now.minute / 60.0
            # EWMA (alpha=0.15): reagiert auf Veraenderungen des
            # Duschverhaltens schneller als ein kumulativer Mittelwert
            if ist_morgen:
                count = self.data.morning_target_hour_samples + 1
                alt = self.data.learned_morning_target_hour
                alpha_t = self.data.config.target_hour_ewma_alpha
                new_target = hour_f if count <= 1 else alt * (1.0 - alpha_t) + hour_f * alpha_t
                self.data.learned_morning_target_hour = round(new_target, 2)
                self.data.morning_target_hour_samples = count
            else:
                count = self.data.target_hour_samples + 1
                alt = self.data.learned_target_hour
                alpha_t = self.data.config.target_hour_ewma_alpha
                new_target = hour_f if count <= 1 else alt * (1.0 - alpha_t) + hour_f * alpha_t
                self.data.learned_target_hour = round(new_target, 2)
                self.data.target_hour_samples = count
            self._save()
            logging.info(
                f"Learning: Zapfung {now.strftime('%H:%M')} "
                f"(unten {drops['unten']:.1f}K, mitte {drops['mittig']:.1f}K, "
                f"oben {drops['oben']:.1f}K, gew. {drop_gesamt:.2f}K) -> "
                f"{'Morgen-' if ist_morgen else 'Abend-'}"
                f"Zielzeit {new_target:.1f}h (n={count})"
            )

    def get_recent_usage_events(
        self,
        hours: int = 2,
        now: Optional[datetime] = None,
    ) -> List[Dict]:
        """Gibt alle Zapfungsereignisse der letzten Stunden zurueck.
        
        Wird von CalcStart genutzt um den Temperaturverlust aus
        erkannten Zapfungen in die Startzeitenberechnung einzubeziehen.
        """
        if now is None:
            now = datetime.now()
        grenze = to_naive(now - timedelta(hours=hours))
        return [
            e for e in self.data.usage_events
            if (ts := self._parse_ts(e.get("timestamp"))) is not None and ts >= grenze
        ]
