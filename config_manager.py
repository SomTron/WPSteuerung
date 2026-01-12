import configparser
import logging
from typing import Optional
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

class AppConfig(BaseModel):
    Heizungssteuerung: HeizungssteuerungConfig = Field(default_factory=HeizungssteuerungConfig)
    Healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    SolaxCloud: SolaxCloudConfig = Field(default_factory=SolaxCloudConfig)
    Telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    Urlaubsmodus: UrlaubsmodusConfig = Field(default_factory=UrlaubsmodusConfig)
    Solarueberschuss: SolarueberschussConfig = Field(default_factory=SolarueberschussConfig)
    Logging: LoggingConfig = Field(default_factory=LoggingConfig)

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
            read_files = parser.read(self.config_path)
            if not read_files:
                logging.warning(f"Konfigurationsdatei '{self.config_path}' nicht gefunden. Verwende Standardwerte.")
                return

            config_dict = {}
            for section in parser.sections():
                config_dict[section] = dict(parser.items(section))

            try:
                self.config = AppConfig(**config_dict)
                logging.info(f"Konfiguration aus '{self.config_path}' erfolgreich geladen.")
            except ValidationError as e:
                logging.error(f"Validierungsfehler in Config: {e}")
                # Fallback: Versuche, Sektionen einzeln zu laden oder behalte Defaults
                # Hier behalten wir die Defaults der fehlgeschlagenen Validierung nicht bei, 
                # sondern loggen nur. Verbesserte Logik könnte hier partielle Updates machen.
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfiguration: {e}")

    def get(self):
        return self.config
