import configparser
import logging
from pydantic import BaseModel, Field, ValidationError

class HeizungssteuerungConfig(BaseModel):
    MIN_LAUFZEIT: int = Field(default=15, description="Minimale Laufzeit in Minuten")
    MIN_PAUSE: int = Field(default=20, description="Minimale Pause in Minuten")
    NACHTABSENKUNG_START: str = Field(default="19:30")
    NACHTABSENKUNG_END: str = Field(default="08:00")
    VERDAMPFERTEMPERATUR: float = Field(default=6.0)
    VERDAMPFER_RESTART_TEMP: float = Field(default=9.0)
    SICHERHEITS_TEMP: float = Field(default=52.0)
    NACHTABSENKUNG: float = Field(default=0.0)
    EINSCHALTPUNKT_ERHOEHT: int = Field(default=42)
    AUSSCHALTPUNKT_ERHOEHT: int = Field(default=48)
    TEMP_OFFSET: int = Field(default=3)
    EINSCHALTPUNKT: int = Field(default=42)
    AUSSCHALTPUNKT: int = Field(default=45)
    UEBERGANGSMODUS_MORGENS_ENDE: str = Field(default="10:00")
    UEBERGANGSMODUS_ABENDS_START: str = Field(default="17:00")
    API_HOST: str = Field(default="0.0.0.0")
    API_PORT: int = Field(default=8000)

class HealthcheckConfig(BaseModel):
    HEALTHCHECK_URL: str = Field(default="")
    HEALTHCHECK_INTERVAL_MINUTES: int = Field(default=15)

class SolaxCloudConfig(BaseModel):
    TOKEN_ID: str = Field(default="")
    SN: str = Field(default="")

class TelegramConfig(BaseModel):
    BOT_TOKEN: str = Field(default="")
    CHAT_ID: str = Field(default="")

class UrlaubsmodusConfig(BaseModel):
    URLAUBSABSENKUNG: float = Field(default=6.0)

class SolarueberschussConfig(BaseModel):
    BATPOWER_THRESHOLD: float = Field(default=600.0)
    SOC_THRESHOLD: float = Field(default=95.0)
    FEEDINPOWER_THRESHOLD: float = Field(default=600.0)

class LoggingConfig(BaseModel):
    ENABLE_FULL_LOG: bool = Field(default=True)

class WetterprognoseConfig(BaseModel):
    LATITUDE: float = Field(default=46.7142)
    LONGITUDE: float = Field(default=13.6361)
    TILT: int = Field(default=30)

# Bekannte Schluessel aus aelteren Versionen des Projekts - bewusst ohne
# Funktion im aktuellen Code. Sie erzeugen nur noch eine INFO mit Hinweis,
# WO der Wert heute konfiguriert wird, statt einer beunruhigenden Warnung.
LEGACY_HINWEISE = {
    "Heizungssteuerung": {
        "MIN_LAUFZEIT_S": "heute MIN_LAUFZEIT (Minuten)",
        "MIN_AUSZEIT_S": "heute MIN_PAUSE (Minuten)",
        "HYSTERESE_MIN": "Hysterese kommt aus der Regellogik",
        "LOOP_INTERVAL": "heute constants.py: MAIN_LOOP_INTERVAL_SEC",
        "ENABLE_LCD": "LCD-Unterstuetzung entfernt",
        "BADEMODUS_HYSTERESE": "Bademodus ueber wp_steuerung_parameter.json [abweichung]",
        "FALLBACK_T_MITTIG": "Sensor-Fallback nicht Bestandteil der aktuellen Logik",
        "PEAK_SHAVING_TARGET_SOC": "Peak-Shaving nicht Bestandteil; Batterie-Regel siehe JSON [batterie]",
        "WP_POWER_EXPECTED": "heute wp_steuerung_parameter.json: wp.leistung_watt",
    },
    "Solarueberschuss": {
        "BATTERY_CAPACITY_KWH": "wird nicht ausgewertet",
        "MIN_SOC": "heute wp_steuerung_parameter.json: batterie.min_soc_prozent",
    },
    "Wetterprognose": {
        "PANEL_EFFICIENCY": "Prognose liefert Solax - Anlagenparameter werden nicht genutzt",
        "PANEL_GROUPS": "Prognose liefert Solax - Anlagenparameter werden nicht genutzt",
    },
}


class AppConfig(BaseModel):
    Heizungssteuerung: HeizungssteuerungConfig = Field(default_factory=HeizungssteuerungConfig)
    Healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    SolaxCloud: SolaxCloudConfig = Field(default_factory=SolaxCloudConfig)
    Telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    Urlaubsmodus: UrlaubsmodusConfig = Field(default_factory=UrlaubsmodusConfig)
    Solarueberschuss: SolarueberschussConfig = Field(default_factory=SolarueberschussConfig)
    Logging: LoggingConfig = Field(default_factory=LoggingConfig)
    Wetterprognose: WetterprognoseConfig = Field(default_factory=WetterprognoseConfig)

class ConfigManager:
    def __init__(self, config_path: str = "config.ini"):
        self.config_path = config_path
        self.config: AppConfig = AppConfig()
        self.load_config()

    def load_config(self):
        """Liest die Config-Datei, validiert sie und lädt sie in das Pydantic Model."""
        parser = configparser.ConfigParser()
        parser.optionxform = str  # Behalte Groß-/Kleinschreibung bei (wichtig für Pydantic Models)
        try:
            # "utf-8-sig": toleriert UTF-8 MIT Byte Order Mark (aeltere Windows-Editoren);
            # ohne BOM-Behandlung wuerde der Name der ersten Sektion unsichtbar
            # verfaelscht und die ganze Sektion stillschweigend ignoriert.
            read_files = parser.read(self.config_path, encoding="utf-8-sig")
            if not read_files:
                logging.warning(f"Konfigurationsdatei '{self.config_path}' nicht gefunden. Verwende Standardwerte.")
                return

            config_dict = {}
            for section in parser.sections():
                config_dict[section] = dict(parser.items(section))

            self.config = self._lade_app_config(config_dict)
            logging.debug(f"Konfiguration aus '{self.config_path}' geladen.")
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfiguration: {e}")

    def _lade_app_config(self, config_dict: dict) -> AppConfig:
        """Laedt alle Sektionen einzeln statt Alles-oder-nichts.

        Bisher verfaelschte EIN Tippfehler (z.B. API_PORT = keine_zahl) die
        KOMPLETTE Datei - auch die 30 korrekten Werte wurden verworfen und
        stillschweigend durch Defaults ersetzt. Jetzt bleibt jede Sektion
        bzw. jeder einzelne Wert so weit wie moeglich erhalten.
        """
        sektionen = {}
        for feld_name, modell_feld in AppConfig.model_fields.items():
            modell_klasse = modell_feld.default_factory
            werte = config_dict.get(feld_name, {})
            sektionen[feld_name] = self._lade_sektion(feld_name, modell_klasse, werte)
        return AppConfig(**sektionen)

    def _lade_sektion(self, name: str, modell_klasse, werte: dict):
        """Laedt eine Sektion; bei Validierungsfehlern nur die betroffenen Felder kappen.

        Behandlungspfad fuer jeden INI-Schluessel:
        1. Exakter Treffer im Modell          -> laden
        2. Nur Gross-/Kleinschreibung weicht ab
           (z.B. 'URLAUBSABsenkung')          -> laden, INFO loggen
        3. Bekannter Legacy-Schluessel        -> ignorieren, INFO mit Hinweis
        4. Sonst                              -> WARNING (echter Tippfehler?)
        """
        felder = modell_klasse.model_fields
        upper_map = {}
        for feld_name in felder:
            upper_map.setdefault(feld_name.upper(), []).append(feld_name)

        gueltig = {}
        unbekannt = []
        for schluessel, wert in werte.items():
            if schluessel in felder:
                gueltig[schluessel] = wert
                continue
            treffer = upper_map.get(schluessel.upper())
            if treffer and len(treffer) == 1 and treffer[0] not in gueltig:
                logging.info(
                    f"[{name}] '{schluessel}' als '{treffer[0]}' uebernommen "
                    f"(Gross-/Kleinschreibung angepasst)"
                )
                gueltig[treffer[0]] = wert
            elif not (treffer and len(treffer) == 1):
                unbekannt.append(schluessel)

        if unbekannt:
            legacy = LEGACY_HINWEISE.get(name, {})
            echte_tippfehler = []
            for k in sorted(unbekannt):
                if k in legacy:
                    logging.info(f"[{name}] Legacy-Schluessel '{k}' ignoriert ({legacy[k]})")
                else:
                    echte_tippfehler.append(k)
            if echte_tippfehler:
                logging.warning(
                    f"Unbekannte Schluessel in [{name}] (Tippfehler?): "
                    f"{', '.join(echte_tippfehler)}"
                )

        try:
            return modell_klasse(**gueltig)
        except ValidationError:
            gefiltert = {}
            for schluessel, wert in gueltig.items():
                try:
                    modell_klasse(**{schluessel: wert})
                    gefiltert[schluessel] = wert
                except ValidationError:
                    logging.error(
                        f"Ungueltiger Wert in [{name}]: {schluessel} = '{wert}' "
                        f"- verwende Defaultwert"
                    )
            gekappt = len(gueltig) - len(gefiltert)
            if gekappt > 0:
                logging.warning(
                    f"Sektion [{name}]: {gekappt} ungueltige(r) Wert(e) auf Default gesetzt"
                )
            return modell_klasse(**gefiltert)

    def get(self):
        return self.config
