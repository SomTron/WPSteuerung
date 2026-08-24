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
from dataclasses import dataclass, asdict


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

    version: int = 3


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

    def _load(self) -> LearningData:
        """Lerndaten aus JSON laden oder Defaults erstellen."""
        defaults = LearningData(
            cycles=[], usage_events=[],
            heat_rates={"winter": {"avg": 3.0, "count": 0},
                       "transition": {"avg": 3.0, "count": 0},
                       "summer": {"avg": 3.0, "count": 0}},
            learned_target_hour=17.0, target_hour_samples=0, version=3
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
                    version=3,
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
        }

    # ── Zyklus-Update ──────────────────────────────────────

    def update(
        self,
        now: datetime,
        temp_dict: Dict[str, Optional[float]],
        compressor_is_on: bool,
    ):
        """
        Wird jeden Regelzyklus aufgerufen.
        Erkennt Heizzyklus-Start/Ende und Warmwasser-Zapfung.
        """
        # Heizzyklus erkennen
        if compressor_is_on and not self._last_compressor_state:
            self._cycle_start_time = now
            self._cycle_start_temps = {
                "unten": temp_dict.get("unten"),
                "mittig": temp_dict.get("mittig"),
                "oben": temp_dict.get("oben"),
            }

        elif not compressor_is_on and self._last_compressor_state and self._cycle_start_time is not None:
            self._finalize_cycle(now, temp_dict)

        # Zapfung erkennen (nur bei ausgeschaltetem Kompressor)
        if not compressor_is_on:
            self._detect_usage(now, temp_dict)

        self._last_compressor_state = compressor_is_on
        self._last_temps = temp_dict
        self._last_temp_time = now

    # ── Heizzyklus auswerten ───────────────────────────────

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
        )

        self.data.cycles.append(asdict(cycle))
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