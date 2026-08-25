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


LEARNING_DATA_FILE = "learning_data.json"


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
    """Erkannte Warmwasser-Zapfung."""
    timestamp: str  # ISO-Format
    temp_before: float
    temp_after: float
    drop_k: float


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

    # ── Verbrauchsbewusstsein: Stundensurplus-Profil ──
    # {"8": {"avg": 350.0, "n": 12}, ...}: gemitelte Netzeinspeisung je Stunde,
    # nur gesampelt bei AUSgeschaltetem Kompressor (= Haushaltsmuster pur).
    surplus_by_hour: Dict[str, Dict[str, float]] = field(default_factory=dict)


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
        self._komfort_grenz_c: float = 40.0
        # Solar-Tracking (Attribution/Kalibrierung/Surplus-Profil)
        self._cycle_feedin_ws: float = 0.0   # Zeitintegral Einspeisung [Ws]
        self._cycle_soc_sum_ws: float = 0.0
        self._cycle_secs: float = 0.0
        self._pending_zu_frueh: List[str] = []   # nur im RAM
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
                return LearningData(
                    cycles=raw.get("cycles", []),
                    heat_rates=raw.get("heat_rates", defaults.heat_rates),
                    usage_events=raw.get("usage_events", []),
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
        if hr["count"] < 3:
            return 3.0 if sensor == "unten" else 2.0
        factor = 1.0 if sensor == "unten" else 0.67
        return round(hr["avg"] * factor, 2)

    def get_learned_target_hour(self) -> float:
        """Gelernte optimale ABEND-Zielzeit (Default 17:00 bei <3 Samples)."""
        if self.data.target_hour_samples < 3:
            return 17.0
        return self.data.learned_target_hour

    def get_learned_morning_target_hour(self) -> float:
        """Gelernte MORGEN-Zielzeit (Default 07:00 bei <3 Samples)."""
        if self.data.morning_target_hour_samples < 3:
            return 7.0
        return self.data.learned_morning_target_hour

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
        grenze = now - timedelta(days=tage)
        stunden = []
        for e in self.data.usage_events:
            try:
                ts = datetime.fromisoformat(e["timestamp"])
            except (KeyError, ValueError):
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
            grenze = now - timedelta(days=tage)
            stunden = []
            for e in self.data.usage_events:
                try:
                    ts = datetime.fromisoformat(e["timestamp"])
                except (KeyError, ValueError):
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
        heute_count = sum(1 for v in self.data.komfort_verletzungen if v.startswith(heute))
        if heute_count >= max_pro_tag:
            return
        ts = now.isoformat(timespec="seconds")
        self.data.komfort_verletzungen.append(ts)
        # Auf letzte ~200 Eintraege begrenzen
        if len(self.data.komfort_verletzungen) > 200:
            self.data.komfort_verletzungen = self.data.komfort_verletzungen[-200:]
        self._save()
        logging.warning(f"KOMFORT-VERLETZUNG: t_oben {t_oben:.1f}C < {grenz_c}C um {ts}")

    def get_komfort_verletzung_rate(self, tage=7) -> int:
        """Gibt Anzahl Komfort-Verletzungen der letzten tage zurueck."""
        if not self.data.komfort_verletzungen:
            return 0
        grenze = (datetime.now() - timedelta(days=tage)).isoformat()
        return sum(1 for v in self.data.komfort_verletzungen if v >= grenze)

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
        z14 = sum(
            1 for v in self.data.zu_frueh_events
            if v >= (datetime.now() - timedelta(days=14)).isoformat())
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
            "quellen": self.get_quellen_statistik(),
            "surplus_stunden": sorted(self.get_surplus_profile().keys())
                if self.get_surplus_profile() else [],
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
    ):
        """
        Wird jeden Regelzyklus aufgerufen.
        Erkennt Heizzykus-Start/Ende und Warmwasser-Zapfung sowie
        Quellen-Attribution, Forecast-Kalibrierung und Surplus-Profil.
        """
        # ── Solar-Tracking ──
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
            self.data.surplus_by_hour[key] = {
                "avg": round(alt_e["avg"] * (1.0 - 0.08)
                             + float(feedin_watt) * 0.08, 1),
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
        if self._pending_zu_frueh:
            noch_offen = []
            for ende_iso in self._pending_zu_frueh:
                try:
                    ende = datetime.fromisoformat(ende_iso)
                except ValueError:
                    continue
                if (now - ende).total_seconds() < 45 * 60:
                    noch_offen.append(ende_iso)
                    continue
                if feedin_watt is not None and feedin_watt >= 800:
                    self.data.zu_frueh_events.append(
                        now.isoformat(timespec="seconds"))
                    del self.data.zu_frueh_events[:-100]
                    logging.warning(
                        f"Learning: ZU FRUEH geheizt - 45 min nach "
                        f"Nicht-PV-Zyklus ({ende_iso}) kommen "
                        f"{feedin_watt:.0f}W Einspeisung")
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
            self._detect_usage(now, temp_dict)

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

        self._last_temps = temp_dict
        self._last_temp_time = now

    # ── Heizzyklus auswerten ───────────────────────────────

    def _kalibriere_forecast(self, now: datetime, heute: str,
                             forecast_today_wh_qm: Optional[float]):
        """Taegliche Kalibrierung (ab 20 Uhr, einmal pro Tag).

        Verhaeltnis tatsaechlicher Netzeinschuss (Wh, integriert) zur
        Tagesprognose (Wh/m2) als EWMA (alpha=0.3), geklemmt auf 0.3-2.0.
        Lernt den HAUSspezifischen Langfehler des Forecast-Dienstes inkl.
        typischem Eigenverbrauchsniveau.
        """
        if self._kalibriert_datum == heute or now.hour < 20:
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
        ratio = max(0.3, min(
            2.0, self._day_surplus_wh / float(forecast_today_wh_qm)))
        n = self.data.forecast_ratio_samples + 1
        self.data.forecast_ratio = (
            round(ratio, 3) if n <= 1
            else round(self.data.forecast_ratio * 0.7 + ratio * 0.3, 3))
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
            self._pending_zu_frueh.append(end.isoformat())
        self._cycle_feedin_ws = 0.0
        self._cycle_soc_sum_ws = 0.0
        self._cycle_secs = 0.0
        if len(self.data.cycles) > 50:
            self.data.cycles = self.data.cycles[-50:]

        # Saisonale Heizrate: exponentiell geglaetteter Mittelwert (EWMA,
        # alpha=0.10) statt kumulativer Mittelwert - reagiert auf
        # Jahreszeit/Sensoraenderungen, statt fuer immer am alten Wert zu kleben.
        hr = self.data.heat_rates.get(season, {"avg": 3.0, "count": 0})
        count = hr["count"] + 1
        if count <= 1:
            new_avg = rate_unten
        else:
            new_avg = hr["avg"] * (1.0 - 0.10) + rate_unten * 0.10
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

    def _detect_usage(self, now: datetime, temp_dict: Dict[str, Optional[float]]):
        """
        Erkennt Warmwasser-Zapfung durch Temperaturabfall.
        Kriterien: Kompressor AUS, Abfall >1.5°C, 05:00-23:00 Uhr.
        Vor 12 Uhr zaehlt die Zapfung zur MORGEN-Zielzeit, danach zur ABEND-Zielzeit.
        """
        if not (5 <= now.hour < 23):
            return

        temp_oben = temp_dict.get("oben")
        last_oben = self._last_temps.get("oben") if self._last_temps else None

        if temp_oben is None or last_oben is None:
            return

        drop = last_oben - temp_oben
        if drop >= 1.5 and self._last_temp_time is not None:
            event = UsageEvent(
                timestamp=now.isoformat(),
                temp_before=round(last_oben, 1),
                temp_after=round(temp_oben, 1),
                drop_k=round(drop, 1),
            )
            self.data.usage_events.append(asdict(event))
            if len(self.data.usage_events) > 100:
                self.data.usage_events = self.data.usage_events[-100:]

            # Nur erste Zapfung(en) pro Tageshaelfte beruecksichtigen
            ist_morgen = now.hour < 12
            today_str = now.strftime("%Y-%m-%d")
            today_events = [
                e for e in self.data.usage_events
                if e["timestamp"].startswith(today_str)
                and (datetime.fromisoformat(e["timestamp"]).hour < 12) == ist_morgen
            ]
            if len(today_events) <= 2:
                hour_f = now.hour + now.minute / 60.0
                # EWMA (alpha=0.15): reagiert auf Veraenderungen des
                # Duschverhaltens schneller als ein kumulativer Mittelwert
                if ist_morgen:
                    count = self.data.morning_target_hour_samples + 1
                    alt = self.data.learned_morning_target_hour
                    new_target = hour_f if count <= 1 else alt * (1.0 - 0.15) + hour_f * 0.15
                    self.data.learned_morning_target_hour = round(new_target, 2)
                    self.data.morning_target_hour_samples = count
                else:
                    count = self.data.target_hour_samples + 1
                    alt = self.data.learned_target_hour
                    new_target = hour_f if count <= 1 else alt * (1.0 - 0.15) + hour_f * 0.15
                    self.data.learned_target_hour = round(new_target, 2)
                    self.data.target_hour_samples = count
                self._save()
                logging.info(
                    f"Learning: Zapfung {now.strftime('%H:%M')} "
                    f"(drop {drop:.1f}C) -> {'Morgen-' if ist_morgen else 'Abend-'}"
                    f"Zielzeit {new_target:.1f}h (n={count})"
                )
