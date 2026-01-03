import asyncio
import logging
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict

class State:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        # Initialer Config-Laden
        self.config = config_manager.get()
        
        self.local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(self.local_tz)

        # Locks
        self.gpio_lock = asyncio.Lock()
        
        # Session (wird später gesetzt)
        self.session = None

        # --- Status-Variablen ---
        self.last_update_id = None
        self.lcd = None
        self.last_sensor_readings: Dict = {}
        
        # --- Urlaubsmodus ---
        self.urlaubsmodus_aktiv: bool = False
        self.urlaubsmodus_start: Optional[datetime] = None
        self.urlaubsmodus_ende: Optional[datetime] = None
        self.awaiting_urlaub_duration: bool = False
        self.awaiting_custom_duration: bool = False

        # --- Bademodus ---
        self.bademodus_aktiv: bool = False
        self.previous_bademodus_aktiv: bool = False

        # --- Laufzeitstatistik ---
        self.current_runtime = timedelta()
        self.last_runtime = timedelta()
        self.total_runtime_today = timedelta()
        self.last_day = now.date()
        self.start_time: Optional[datetime] = None
        self.last_compressor_on_time = now
        self.last_compressor_off_time = now
        self.last_log_time = now - timedelta(minutes=1)
        self.last_completed_cycle: Optional[datetime] = None

        # --- Steuerungslogik ---
        self.kompressor_ein: bool = False
        self.solar_ueberschuss_aktiv: bool = False
        self.ausschluss_grund: Optional[str] = None
        self.previous_modus: Optional[str] = None
        self.previous_abschalten: bool = False
        self.previous_temp_conditions: bool = False
        
        # --- Schwellwerte (werden aus Config aktualisiert) ---
        # Initialwerte, werden im Loop updated
        self.aktueller_ausschaltpunkt = self.config.Heizungssteuerung.AUSSCHALTPUNKT
        self.aktueller_einschaltpunkt = self.config.Heizungssteuerung.EINSCHALTPUNKT
        self.previous_ausschaltpunkt = self.aktueller_ausschaltpunkt
        self.previous_einschaltpunkt = self.aktueller_einschaltpunkt
        self.previous_solar_ueberschuss_aktiv: bool = False

        # --- Fehler- und Statuszustände ---
        self.last_config_hash: Optional[str] = None
        self._last_config_check = now
        self.pressure_error_sent: bool = False
        self.last_pressure_error_time = now
        self.last_pressure_state: Optional[bool] = None
        self.last_pause_log: Optional[datetime] = None
        self.current_pause_reason: Optional[str] = None
        self.last_no_start_log: Optional[datetime] = None
        
        # --- Sensorwerte ---
        self.t_oben: Optional[float] = None
        self.t_unten: Optional[float] = None
        self.t_mittig: Optional[float] = None
        self.t_verd: Optional[float] = None
        self.t_boiler: Optional[float] = None

        # --- Solax-Daten ---
        self.acpower: Optional[float] = None
        self.feedinpower: Optional[float] = None
        self.batpower: Optional[float] = None
        self.soc: Optional[float] = None
        self.consumeenergy: Optional[float] = None
        self.last_api_call: Optional[datetime] = None
        self.last_api_data: Optional[dict] = None
        
        # --- VPN ---
        self.vpn_ip: Optional[str] = None

        # --- Logging-Throttle ---
        self.last_solar_window_check = now
        self.last_solar_window_status: bool = False
        self.last_solar_window_log: Optional[datetime] = None
        self.last_abschalt_log = now
        self.last_verdampfer_notification: Optional[datetime] = None
        self.verdampfer_blocked: bool = False

        # --- Healthcheck ---
        self.last_healthcheck_ping: Optional[datetime] = None

    @property
    def sicherheits_temp(self):
        return self.config.Heizungssteuerung.SICHERHEITS_TEMP

    @property
    def verdampfertemperatur(self):
        return self.config.Heizungssteuerung.VERDAMPFERTEMPERATUR
    
    @property
    def verdampfer_restart_temp(self):
        return self.config.Heizungssteuerung.VERDAMPFER_RESTART_TEMP
    
    @property
    def min_laufzeit(self):
        return timedelta(minutes=self.config.Heizungssteuerung.MIN_LAUFZEIT)
    
    @property
    def min_pause(self):
        return timedelta(minutes=self.config.Heizungssteuerung.MIN_PAUSE)

    @property
    def einschaltpunkt_erhoeht(self):
        return self.config.Heizungssteuerung.EINSCHALTPUNKT_ERHOEHT

    @property
    def ausschaltpunkt_erhoeht(self):
        return self.config.Heizungssteuerung.AUSSCHALTPUNKT_ERHOEHT
    
    @property
    def uebergangsmodus_morgens_ende(self):
        return datetime.strptime(self.config.Heizungssteuerung.UEBERGANGSMODUS_MORGENS_ENDE, "%H:%M").time()

    @property
    def uebergangsmodus_abends_start(self):
        return datetime.strptime(self.config.Heizungssteuerung.UEBERGANGSMODUS_ABENDS_START, "%H:%M").time()
        
    @property
    def nachtabsenkung_start(self):
        return datetime.strptime(self.config.Heizungssteuerung.NACHTABSENKUNG_START, "%H:%M").time()

    @property
    def nachtabsenkung_ende(self):
        return datetime.strptime(self.config.Heizungssteuerung.NACHTABSENKUNG_END, "%H:%M").time()
    
    @property
    def basis_einschaltpunkt(self):
        return self.config.Heizungssteuerung.EINSCHALTPUNKT

    @property
    def basis_ausschaltpunkt(self):
        return self.config.Heizungssteuerung.AUSSCHALTPUNKT
    
    def update_config(self):
        """Lädt die Konfiguration neu und aktualisiert lokale Referenzen bei Bedarf."""
        self.config_manager.load_config()
        self.config = self.config_manager.get()
