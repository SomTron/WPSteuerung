"""
JSON-basierte Konfiguration für die Pareto-optimierte WP-Steuerung.

Lädt Parameter aus wp_steuerung_parameter.json und stellt sie
als typsichere Pydantic-Modelle bereit.
"""

import json
import logging
import os
from typing import List, Optional
from pydantic import BaseModel, Field


class WPConfig(BaseModel):
    """Wärmepumpen-Grundkonfiguration."""
    leistung_watt: int = Field(default=600, description="Nennleistung der WP in Watt")
    typ: str = Field(default="binaer_ein_aus", description="Steuerungstyp: binaer_ein_aus oder modulierend")


class ZyklusConfig(BaseModel):
    """Zeitliche Zyklen."""
    interval_minuten: int = Field(default=15, description="Hauptloop-Intervall in Minuten")
    mindestlaufzeit_minuten: int = Field(default=60, description="Minimale Kompressor-Laufzeit in Minuten")
    mindestpausenzeit_minuten: int = Field(default=30, description="Minimale Pause zwischen Einschaltungen in Minuten")


class SicherheitConfig(BaseModel):
    """Sicherheitsparameter."""
    nachtsperre_start: int = Field(default=19, description="Nachtsperre Start-Stunde (0-23)")
    nachtsperre_ende: int = Field(default=8, description="Nachtsperre Ende-Stunde (0-23)")
    max_temp_c: float = Field(default=48.0, description="Maximale Boiler-Temperatur (°C)")
    ueberhitzung_c: float = Field(default=58.0, description="Überhitzungsschutz (°C)")
    notfall_c: float = Field(default=36.0, description="Notfall-Einschalttemperatur (°C)")


class WochenendeConfig(BaseModel):
    """Wochenende-Einstellungen."""
    aktiv: bool = Field(default=True, description="Wochenendmodus aktiv")
    fruehestens_uhr: int = Field(default=9, description="Frühester Einschaltzeitpunkt am Wochenende")


class PVRegel(BaseModel):
    """Eine einzelne PV-Steuerungsregel."""
    name: str = Field(default="PV Regel", description="Eindeutiger Name der Regel")
    prioritaet: int = Field(default=80, description="Priorität (hoeher = wichtiger)")
    pv_schwelle_watt: float = Field(default=200.0, description="Minimale PV-Leistung zum Einschalten (W)")
    temperaturfuehler: str = Field(default="mitte", description="Welcher Fühler: oben/mitte/unten")
    einschalten_bei_c: float = Field(default=40.0, description="Einschalten bei Temperatur <= (°C)")
    ausschalten_bei_c: float = Field(default=45.0, description="Ausschalten bei Temperatur >= (°C)")
    weiterlaufen_ab_pv_watt: float = Field(default=50.0, description="Weiterlaufen ab PV-Leistung (W)")


class KomfortConfig(BaseModel):
    """Komfort-Regel: Hält Mindesttemperatur."""
    prioritaet: int = Field(default=60, description="Priorität")
    notfall_einschalten_bei_c: float = Field(default=36.0, description="Notfall: Einschalten bei (°C)")
    komfort_einschalten_bei_c: float = Field(default=38.0, description="Komfort: Einschalten bei (°C)")
    ausschalten_bei_c: float = Field(default=42.0, description="Ausschalten bei (°C)")
    min_pv_fuer_komfort_watt: float = Field(default=50.0, description="Minimale PV für Komfort-Heizen (W)")


class ZeitfensterConfig(BaseModel):
    """Zeitfenster-Regel: Heizt zu bestimmten Uhrzeiten."""
    prioritaet: int = Field(default=53, description="Priorität")
    start_uhr: int = Field(default=6, description="Start-Stunde (0-23)")
    ende_uhr: int = Field(default=16, description="Ende-Stunde (0-23)")
    modus: str = Field(default="einschalten", description="Modus: einschalten oder ausschalten")
    temperaturfuehler: str = Field(default="mitte", description="Welcher Fühler: oben/mitte/unten")
    max_temp_fuer_einschalten_c: float = Field(default=50.0, description="Einschalten wenn Temp <= (°C)")
    min_pv_watt: float = Field(default=0.0, description="Minimale PV-Leistung (W)")


class AbweichungConfig(BaseModel):
    """Abweichungsregel: Hält Temperatur nahe am Sollwert."""
    prioritaet: int = Field(default=47, description="Priorität")
    solltemperatur_c: float = Field(default=40.0, description="Solltemperatur (°C)")
    temperaturfuehler: str = Field(default="unten", description="Welcher Fühler: oben/mitte/unten")
    einschalten_bei_abweichung_k: float = Field(default=3.0, description="Einschalten bei Abweichung >= (K)")
    ausschalten_bei_abweichung_k: float = Field(default=0.5, description="Ausschalten bei Abweichung <= (K)")




class ForecastConfig(BaseModel):
    """Prognose-Regel: Vorheizen bei schlechter Solar-Prognose, sparen bei guter."""
    prioritaet: int = Field(default=57, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    fc_schwelle_hoch_wh: float = Field(default=3000.0, description="Prognose ueber Wert = guter Solartag (Wh/qm)")
    fc_schwelle_niedrig_wh: float = Field(default=800.0, description="Prognose unter Wert = schlechter Solartag (Wh/qm)")
    t_vorheiz_ab_c: float = Field(default=44.0, description="Vorheizen wenn Temp kleiner gleich (Grad C)")
    tmax_c: float = Field(default=48.0, description="Maximale Vorheiztemperatur (Grad C)")
    vorheiz_start_uhr: int = Field(default=8, description="Vorheiz-Fenster Start-Stunde")
    vorheiz_ende_uhr: int = Field(default=19, description="Vorheiz-Fenster Ende-Stunde")
    sparen_start_uhr: int = Field(default=11, description="Spar-Fenster Start-Stunde")
    sparen_ende_uhr: int = Field(default=15, description="Spar-Fenster Ende-Stunde")


class AdaptivePVConfig(BaseModel):
    """Adaptive-PV-Regel: PV-Schwelle passt sich an Temperatur und Prognose an."""
    prioritaet: int = Field(default=55, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    base_threshold_watt: float = Field(default=300.0, description="Basis-PV-Schwelle (W)")
    temperaturfuehler: str = Field(default="unten", description="Fuehler: oben/mitte/unten")
    tmax_c: float = Field(default=48.0, description="Maximale Temperatur (Grad C)")
    t_aggressiv_kalt_c: float = Field(default=35.0, description="Schwelle x0.5 wenn Temp unter Wert (Grad C)")
    t_normal_kalt_c: float = Field(default=38.0, description="Schwelle x0.7 wenn Temp unter Wert (Grad C)")


class CalculatedStartConfig(BaseModel):
    """Startzeit-Regel: Berechnet optimalen Einschaltzeitpunkt fuer Zieltemperatur."""
    prioritaet: int = Field(default=82, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    solltemperatur_c: float = Field(default=44.0, description="Zieltemperatur (Grad C)")
    target_uhr: int = Field(default=17, description="Zielzeit (Stunde) - typische Zapfzeit")
    heizrate_unten_c_h: float = Field(default=3.0, description="Geschaetzte Heizrate unten (Grad C/h)")
    heizrate_gesamt_c_h: float = Field(default=2.0, description="Geschaetzte Heizrate gesamt (Grad C/h)")
    tmax_c: float = Field(default=48.0, description="Maximale Temperatur (Grad C)")


class WPSteuerungConfig(BaseModel):
    """Gesamtkonfiguration der Pareto-optimierten WP-Steuerung."""
    beschreibung: str = Field(default="WP Steuerung")
    wp: WPConfig = Field(default_factory=WPConfig)
    zyklus: ZyklusConfig = Field(default_factory=ZyklusConfig)
    sicherheit: SicherheitConfig = Field(default_factory=SicherheitConfig)
    wochenende: WochenendeConfig = Field(default_factory=WochenendeConfig)
    pv_regeln: List[PVRegel] = Field(default_factory=list)
    komfort: KomfortConfig = Field(default_factory=KomfortConfig)
    zeitfenster: ZeitfensterConfig = Field(default_factory=ZeitfensterConfig)
    abweichung: AbweichungConfig = Field(default_factory=AbweichungConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    adaptive_pv: AdaptivePVConfig = Field(default_factory=AdaptivePVConfig)
    calculated_start: CalculatedStartConfig = Field(default_factory=CalculatedStartConfig)


class WPSteuerungConfigManager:
    """Lädt und verwaltet die JSON-Konfiguration."""

    def __init__(self, config_path: str = "wp_steuerung_parameter.json"):
            # Pfad relativ zum Verzeichnis dieser Datei (json_config.py) aufloesen
            # So wird die Config immer gefunden, egal aus welchem Ordner der Prozess startet
            _script_dir = os.path.dirname(os.path.abspath(__file__))
            self.config_path = os.path.join(_script_dir, config_path)
            self.config: WPSteuerungConfig = WPSteuerungConfig()
            self._last_mtime: Optional[float] = None

    def load_config(self) -> bool:
        """
        Lädt die JSON-Konfiguration.
        Gibt True zurück, wenn sich die Config geändert hat.
        """
        try:
            if not os.path.exists(self.config_path):
                logging.warning(f"JSON-Config '{self.config_path}' nicht gefunden. Verwende Defaults.")
                return False

            current_mtime = os.path.getmtime(self.config_path)
            if current_mtime == self._last_mtime:
                return False  # Keine Änderung

            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)

            self.config = WPSteuerungConfig(**raw)
            self._last_mtime = current_mtime
            logging.info(f"JSON-Config '{self.config_path}' geladen: {self.config.beschreibung}")
            return True

        except json.JSONDecodeError as e:
            logging.error(f"JSON-Syntaxfehler in '{self.config_path}': {e}")
            return False
        except Exception as e:
            logging.error(f"Fehler beim Laden der JSON-Config: {e}")
            return False

    def get(self) -> WPSteuerungConfig:
        """Gibt die aktuelle Konfiguration zurück."""
        return self.config

    def has_changed(self) -> bool:
        """Prüft, ob sich die Datei seit dem letzten Laden geändert hat."""
        if not os.path.exists(self.config_path):
            return False
        current_mtime = os.path.getmtime(self.config_path)
        return current_mtime != self._last_mtime

    def reload_if_changed(self) -> bool:
        """Lädt die Config neu, wenn sich die Datei geändert hat."""
        if self.has_changed():
            return self.load_config()
        return False
