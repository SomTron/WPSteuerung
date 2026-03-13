import configparser
import logging
from typing import Optional
from pydantic import BaseModel, Field, ValidationError

class SensorenConfig(BaseModel):
    OBEN: str = Field(default="28-0bd6d4461d84")
    MITTIG: str = Field(default="28-6977d446424a")
    UNTEN: str = Field(default="28-445bd44686f4")
    VERD: str = Field(default="28-213bd4460d65")
    VORLAUF: str = Field(default="28-2ce8d446a504")

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
    WP_POWER_EXPECTED: float = Field(default=600.0, description="Erwarteter Verbrauch der Wärmepumpe in Watt")

class HealthcheckConfig(BaseModel):
    HEALTHCHECK_URL: str = Field(default="")
    HEALTHCHECK_INTERVAL_MINUTES: int = Field(default=15)

class ApiConfig(BaseModel):
    API_KEY: str = Field(default="", description="API-Key für die Web-Oberfläche. Leer = keine Authentifizierung.")

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
    BATTERY_CAPACITY_KWH: float = Field(default=0.0, description="Batteriekapazität in kWh")
    MIN_SOC: float = Field(default=0.0, description="Minimaler SoC in Prozent")
    ADAPTIVE_PV_THRESHOLDS: bool = Field(default=True, description="LOW/HIGH PV-Schwellen automatisch aus Historie berechnen")
    PV_THRESHOLD_LOOKBACK_DAYS: int = Field(default=45, description="Wie viele Tage Historie für die Schwellenberechnung")
    PV_THRESHOLD_LOW_PERCENTILE: float = Field(default=0.25, description="Perzentil für LOW_PV (0..1)")
    PV_THRESHOLD_HIGH_PERCENTILE: float = Field(default=0.75, description="Perzentil für HIGH_PV (0..1)")
    PV_THRESHOLD_MIN_DAYS: int = Field(default=10, description="Mindestanzahl Tage, bevor Schwellen gesetzt werden")

class LoggingConfig(BaseModel):
    ENABLE_FULL_LOG: bool = Field(default=True)

class WetterprognoseConfig(BaseModel):
    LATITUDE: float = Field(default=46.7142)
    LONGITUDE: float = Field(default=13.6361)
    TILT: int = Field(default=30, description="Fallback-Neigungswinkel in Grad, falls keine Panelgruppen konfiguriert sind")
    PANEL_EFFICIENCY: float = Field(
        default=0.20,
        description="PV-Wirkungsgrad (0-1), z.B. 0.20 für 20%%"
    )
    PANEL_GROUPS: str = Field(
        default="",
        description=(
            "Optionale PV-Panel-Gruppen im Format "
            "'anzahl,länge_m,breite_m,tilt_deg,azimuth_deg; ...'. "
            "Beispiel: '12,1.722,1.134,5,90;24,1.722,1.134,30,60'"
        ),
    )

class AppConfig(BaseModel):
    Heizungssteuerung: HeizungssteuerungConfig = Field(default_factory=HeizungssteuerungConfig)
    Healthcheck: HealthcheckConfig = Field(default_factory=HealthcheckConfig)
    SolaxCloud: SolaxCloudConfig = Field(default_factory=SolaxCloudConfig)
    Telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    Urlaubsmodus: UrlaubsmodusConfig = Field(default_factory=UrlaubsmodusConfig)
    Solarueberschuss: SolarueberschussConfig = Field(default_factory=SolarueberschussConfig)
    Logging: LoggingConfig = Field(default_factory=LoggingConfig)
    Wetterprognose: WetterprognoseConfig = Field(default_factory=WetterprognoseConfig)
    Sensoren: SensorenConfig = Field(default_factory=SensorenConfig)
    API: ApiConfig = Field(default_factory=ApiConfig)

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
                logging.debug(f"Konfiguration aus '{self.config_path}' erfolgreich geladen.")  # Changed to DEBUG to reduce log noise
            except ValidationError as e:
                logging.error(f"Validierungsfehler in Config: {e}")
                # Fallback: Versuche, Sektionen einzeln zu laden oder behalte Defaults
                # Hier behalten wir die Defaults der fehlgeschlagenen Validierung nicht bei, 
                # sondern loggen nur. Verbesserte Logik könnte hier partielle Updates machen.
        except Exception as e:
            logging.error(f"Fehler beim Laden der Konfiguration: {e}")

    def get(self):
        return self.config
