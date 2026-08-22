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
        """Laedt eine Sektion; bei Validierungsfehlern nur die betroffenen Felder kappen."""
        # Unbekannte Schluessel melden - das sind fast immer Tippfehler,
        # die sonst stillschweigend wirkungslos bleiben wuerden.
        unbekannt = [k for k in werte if k not in modell_klasse.model_fields]
        if unbekannt:
            logging.warning(
                f"Unbekannte Schluessel in [{name}] (Tippfehler?): {', '.join(sorted(unbekannt))}"
            )

        try:
            return modell_klasse(**werte)
        except ValidationError:
            gueltige_werte = {}
            for schluessel, wert in werte.items():
                if schluessel not in modell_klasse.model_fields:
                    continue  # wurde oben schon als unbekannt gemeldet
                try:
                    modell_klasse(**{schluessel: wert})
                    gueltige_werte[schluessel] = wert
                except ValidationError:
                    logging.error(
                        f"Ungueltiger Wert in [{name}]: {schluessel} = '{wert}' "
                        f"- verwende Defaultwert"
                    )
            gekappt = len(werte) - len(gueltige_werte) - len(unbekannt)
            if gekappt > 0:
                logging.warning(
                    f"Sektion [{name}]: {gekappt} ungueltige(r) Wert(e) auf Default gesetzt"
                )
            return modell_klasse(**gueltige_werte)

    def get(self):
        return self.config
