"""
Zentralisierte Konstanten fuer die Waermepumpensteuerung.

Alle Magic Numbers werden hier definiert, um Wartbarkeit und Konsistenz zu gewaehrleisten.
"""

# --- Timezone ---
DEFAULT_TIMEZONE: str = "Europe/Berlin"

# --- Temperature Validation ---
TEMP_MIN_VALID: float = -50.0
TEMP_MAX_VALID: float = 150.0
TEMP_VERD_MIN_VALID: float = -20.0
TEMP_VERD_MAX_VALID: float = 50.0

# --- Reduction Limits ---
REDUCTION_MIN: float = 0.0
REDUCTION_MAX: float = 35.0

# --- Time Intervals ---
SOLAR_WINDOW_HOURS: int = 2
CONFIG_CHECK_INTERVAL_SEC: int = 60
LOG_THROTTLE_DEFAULT_MIN: float = 5.0
MAIN_LOOP_INTERVAL_SEC: int = 10
SOLAR_UPDATE_INTERVAL_SEC: int = 300
FORECAST_UPDATE_INTERVAL_MIN: int = 60
FORECAST_UPDATE_INTERVAL_HOURS: int = 6
HEALTHCHECK_PING_INTERVAL_MIN: float = 1.0
VPN_CHECK_INTERVAL_SEC: int = 60
WEATHER_UPDATE_INTERVAL_MIN: int = 60

# --- Compressor Verification ---
COMPRESSOR_VERIFICATION_DELAY_MIN: int = 10
COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN: int = 1
COMPRESSOR_VERD_DELTA_MIN: float = 1.5
COMPRESSOR_VERD_START_TEMP_COLD: float = 15.0
COMPRESSOR_VERD_DELTA_COLD_MIN: float = -0.5
COMPRESSOR_VERD_COLD_MAX: float = 12.0
COMPRESSOR_UNTEN_DELTA_MIN: float = 0.2
COMPRESSOR_VERIFICATION_ERROR_THRESHOLD: int = 2

# --- Solar Data Freshness ---
SOLAR_DATA_MAX_AGE_HOURS: int = 12
SOLAR_DATA_MAX_AGE_MIN: int = 15
SOLAR_DATA_STALE_THRESHOLD_MIN: int = 30
SOLAR_API_TIMEOUT_SEC: int = 10

# --- Bademodus ---
BADEMODUS_HYSTERESIS: float = 4.0

# --- Frostschutz ---
FROSTSCHUTZ_AUSSCHALTPUNKT_BOOST: float = 3.0

# --- Sensor ---
SENSOR_RETRY_COUNT: int = 3

# --- Telegram ---
TELEGRAM_MAX_RETRIES: int = 10
TELEGRAM_RETRY_DELAY_SEC: float = 2.0
TELEGRAM_RATE_LIMIT_SECONDS: float = 2.0

# --- Safety ---
SOLAR_ERROR_MIN_PAUSE_MIN: int = 30

# --- GPIO / Hardware ---
RELAY_ON_STATE: int = 1
RELAY_OFF_STATE: int = 0

# --- Network / API ---
API_SERVER_HOST: str = "0.0.0.0"
API_SERVER_PORT: int = 8080
REQUEST_TIMEOUT_SEC: int = 10
HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5

# --- Deployment ---
NGINX_PORT: int = 80
NGINX_SSL_PORT: int = 443
