import asyncio
import logging
import hashlib
import pytz
from datetime import datetime, timedelta
from typing import Optional, Dict

class SensorsState:
    def __init__(self):
        self.t_oben: Optional[float] = None
        self.t_unten: Optional[float] = None
        self.t_mittig: Optional[float] = None
        self.t_verd: Optional[float] = None
        self.t_boiler: Optional[float] = None
        self.last_readings: Dict = {}

class SolarState:
    def __init__(self):
        self.acpower: Optional[float] = None
        self.feedinpower: Optional[float] = None
        self.batpower: Optional[float] = None
        self.soc: Optional[float] = None
        self.consumeenergy: Optional[float] = None
        self.last_api_call: Optional[datetime] = None
        self.last_api_data: Optional[dict] = None
        self.forecast_today: Optional[float] = None
        self.forecast_tomorrow: Optional[float] = None
        self.sunrise_today: Optional[str] = None
        self.sunset_today: Optional[str] = None

class ControlState:
    def __init__(self, config):
        self.kompressor_ein: bool = False
        self.solar_ueberschuss_aktiv: bool = False
        self.ausschluss_grund: Optional[str] = None
        self.previous_modus: Optional[str] = None
        self.aktueller_ausschaltpunkt = config.Heizungssteuerung.AUSSCHALTPUNKT
        self.aktueller_einschaltpunkt = config.Heizungssteuerung.EINSCHALTPUNKT
        self.pressure_error_sent: bool = False
        self.last_pressure_state: Optional[bool] = None
        self.current_pause_reason: Optional[str] = None
        self.active_rule_sensor: Optional[str] = None
        self.blocking_reason: Optional[str] = None  # Current blocking reason
        self.last_blocking_reason: Optional[str] = None  # For change detection

class StatsState:
    def __init__(self, now):
        self.current_runtime = timedelta()
        self.last_runtime = timedelta()
        self.total_runtime_today = timedelta()
        self.last_day = now.date()
        self.start_time: Optional[datetime] = None
        self.last_compressor_on_time: Optional[datetime] = None
        self.last_compressor_off_time: Optional[datetime] = None
        self.last_completed_cycle: Optional[datetime] = None

class State:
    def __init__(self, config_manager):
        self.config_manager = config_manager
        self.config = config_manager.get()
        self.local_tz = pytz.timezone("Europe/Berlin")
        now = datetime.now(self.local_tz)

        # Sub-States
        self.sensors = SensorsState()
        self.solar = SolarState()
        self.control = ControlState(self.config)
        self.stats = StatsState(now)
        
        # Urlaubs/Bademodus (Legacy/Simple Group)
        self.urlaubsmodus_aktiv: bool = False
        self.urlaubsmodus_start: Optional[datetime] = None
        self.urlaubsmodus_ende: Optional[datetime] = None
        self.bademodus_aktiv: bool = False
        self.awaiting_urlaub_duration: bool = False
        self.awaiting_custom_duration: bool = False
        
        # System/Internal
        self.gpio_lock = asyncio.Lock()
        self.session = None
        self.last_forecast_update: Optional[datetime] = None
        self.vpn_ip: Optional[str] = None
        self.last_healthcheck_ping: Optional[datetime] = None
        self.last_solar_window_status: bool = False

        # --- Compressor Verification ---
        self.kompressor_verification_start_time: Optional[datetime] = None
        self.kompressor_verification_start_t_verd: Optional[float] = None
        self.kompressor_verification_start_t_unten: Optional[float] = None
        self.kompressor_verification_failed: bool = False
        self.kompressor_verification_error_count: int = 0
        self.kompressor_verification_last_check: Optional[datetime] = None

        # --- Safety & Error Handling ---
        self.verdampfer_blocked: bool = False
        self.last_sensor_error_time: Optional[datetime] = None
        self.last_pressure_error_time: Optional[datetime] = None
        self._last_config_check: Optional[datetime] = now # Initialize with current time
        self.last_config_hash: Optional[str] = None

    # --- Properties representing Config Values ---
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
    def basis_einschaltpunkt(self):
        return self.config.Heizungssteuerung.EINSCHALTPUNKT

    @property
    def basis_ausschaltpunkt(self):
        return self.config.Heizungssteuerung.AUSSCHALTPUNKT

    @property
    def bot_token(self):
        return self.config.Telegram.BOT_TOKEN
    
    @property
    def chat_id(self):
        return self.config.Telegram.CHAT_ID

    @property
    def healthcheck_url(self):
        return self.config.Healthcheck.HEALTHCHECK_URL

    @property
    def healthcheck_interval(self):
        return float(self.config.Healthcheck.HEALTHCHECK_INTERVAL_MINUTES)
    
    @property
    def battery_capacity(self):
        return self.config.Solarueberschuss.BATTERY_CAPACITY_KWH

    @property
    def min_soc(self):
        return self.config.Solarueberschuss.MIN_SOC
    
    def update_config(self):
        """Reload config only if file has changed (detected via MD5 hash)."""
        try:
            with open(self.config_manager.config_path, 'rb') as f:
                new_hash = hashlib.md5(f.read()).hexdigest()
            
            if new_hash != self.last_config_hash:
                logging.info(f"Config file changed (hash mismatch), reloading...")
                self.config_manager.load_config()
                self.config = self.config_manager.get()
                self.last_config_hash = new_hash
            else:
                logging.debug("Config file unchanged (hash match), skipping reload")
        except Exception as e:
            logging.error(f"Error checking config hash: {e}")
