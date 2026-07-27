"""
Zentralisierte Konstanten für die Wärmepumpensteuerung.

Alle Magic Numbers werden hier definiert, um Wartbarkeit und Konsistenz zu gewährleisten.
"""

# --- Sensor ---
SENSOR_RETRY_COUNT: int = 3

# --- Temperature Validation ---
TEMP_MIN_VALID: float = -50.0       # Minimale gültige Temperatur (°C)
TEMP_MAX_VALID: float = 150.0       # Maximale gültige Temperatur (°C)
TEMP_VERD_MIN_VALID: float = -20.0  # Minimale gültige Verdampfertemperatur (°C)
TEMP_VERD_MAX_VALID: float = 50.0   # Maximale gültige Verdampfertemperatur (°C)

# --- Reduction Limits ---
REDUCTION_MIN: float = 0.0          # Minimale Temperaturreduktion (°C)
REDUCTION_MAX: float = 35.0         # Maximale Temperaturreduktion (°C)

# --- Time Intervals ---
DEFAULT_TIMEZONE: str = "Europe/Berlin"

# --- Time Intervals ---
SOLAR_WINDOW_HOURS: int = 2         # Dauer des Solarfensters (Stunden)
CONFIG_CHECK_INTERVAL_SEC: int = 60 # Interval für Konfigurations-Neuladung (Sekunden)
LOG_THROTTLE_DEFAULT_MIN: float = 5.0  # Standard-Logging-Throttle-Interval (Minuten)
MAIN_LOOP_INTERVAL_SEC: int = 10    # Hauptloop-Interval (Sekunden)
SOLAR_UPDATE_INTERVAL_SEC: int = 300   # Solar-API-Update-Interval (Sekunden)
FORECAST_UPDATE_INTERVAL_MIN: int = 60  # Wettervorhersage-Update-Interval (Minuten)
FORECAST_UPDATE_INTERVAL_HOURS: int = 6  # Wettervorhersage-Update-Interval (Stunden)
HEALTHCHECK_PING_INTERVAL_MIN: float = 1.0  # Healthcheck-Ping-Interval (Minuten)
VPN_CHECK_INTERVAL_SEC: int = 60        # VPN-Check-Interval (Sekunden)
WEATHER_UPDATE_INTERVAL_MIN: int = 60   # Wetter-Update-Interval (Minuten)

# --- Compressor Verification ---
COMPRESSOR_VERIFICATION_DELAY_MIN: int = 10   # Verzögerung vor Verifikation (Minuten)
COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN: int = 1  # Interval zwischen Verifikations-Checks (Minuten)
COMPRESSOR_VERD_DELTA_MIN: float = 1.5       # Minimale Verdampfer-Temp-Abfall für Verifikation (°C)
COMPRESSOR_VERD_START_TEMP_COLD: float = 15.0  # Kalte Start-Schwelle für Verdampfer (°C)
COMPRESSOR_VERD_DELTA_COLD_MIN: float = -0.5  # Minimaler Verdampfer-Abfall bei kaltem Start (°C)
COMPRESSOR_VERD_COLD_MAX: float = 12.0        # Maximale Verdampfer-Temp bei kaltem Start (°C)
COMPRESSOR_UNTEN_DELTA_MIN: float = 0.2       # Minimale untere Fühler-Änderung (°C)
COMPRESSOR_VERIFICATION_ERROR_THRESHOLD: int = 2  # Maximale Verifikationsfehler vor Abschaltung

# --- Solar Data Freshness ---
SOLAR_DATA_MAX_AGE_HOURS: int = 12    # Maximales Alter der Solar-Daten (Stunden)
SOLAR_DATA_MAX_AGE_MIN: int = 15      # Maximales Alter der Solar-Daten für Steuerung (Minuten)
SOLAR_DATA_STALE_THRESHOLD_MIN: int = 30  # Schwelle fuer veraltete Solar-Daten (Minuten)
SOLAR_API_TIMEOUT_SEC: int = 10       # Solax-API-Timeout (Sekunden)

# --- Bademodus ---
BADEMODUS_HYSTERESIS: float = 4.0     # Hysterese zwischen Ein/Ausschaltpunkt im Bademodus (°C)
FROSTSCHUTZ_AUSSCHALTPUNKT_BOOST: float = 2.0  # Erhoehter Ausschaltpunkt bei Frostschutz

# --- Telegram ---
TELEGRAM_MAX_RETRIES: int = 10        # Maximale Anzahl der Telegram-Versuche
TELEGRAM_RETRY_DELAY_SEC: float = 2.0  # Verzögerung zwischen Telegram-Versuchen (Sekunden)
TELEGRAM_RATE_LIMIT_SECONDS: float = 2.0  # Mindestabstand zwischen Telegram-Befehlen (Sekunden)

# --- Safety ---
SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

# --- GPIO / Hardware ---
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

# --- Network / API ---
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5  # Healthcheck-Request-Timeout (Sekunden)

# --- Deployment ---
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_SSL_PORT: int = 443             # Standard-Nginx-SSL-Port-Versuche
TELEGRAM_RETRY_DELAY_SEC: float = 2.0  # Verzögerung zwischen Telegram-Versuchen (Sekunden)

# --- Safety ---
SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

# --- GPIO / Hardware ---
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

# --- Network / API ---
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5  # Healthcheck-Request-Timeout (Sekunden)

# --- Deployment ---
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_SSL_PORT: int = 443             # Standard-Nginx-SSL-Port
NGINX_SSL_PORT: int = 443             # Standard-Nginx-SSL-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
NGINX_PORT: int = 80                  # Standard-Nginx-Port
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---
# --- Deployment ---

HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5  # Healthcheck-Request-Timeout (Sekunden)

HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5  # Healthcheck-Request-Timeout (Sekunden)

HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5  # Healthcheck-Request-Timeout (Sekunden)

REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
REQUEST_TIMEOUT_SEC: int = 10         # Standard-HTTP-Request-Timeout (Sekunden)
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_PORT: int = 8080           # Standard-API-Server-Port
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
API_SERVER_HOST: str = "0.0.0.0"      # Standard-API-Server-Host
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---
# --- Network / API ---

RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

RELAY_OFF_STATE: int = 0              # Relay-AUS-Zustand

RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
RELAY_ON_STATE: int = 1               # Relay-EIN-Zustand
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---
# --- GPIO / Hardware ---

SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

SOLAR_ERROR_MIN_PAUSE_MIN: int = 30   # Minimale Pause nach Solar-Fehler (Minuten)

# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---
# --- Safety ---

TELEGRAM_RETRY_DELAY_SEC: float = 2.0  # Verzögerung zwischen Telegram-Versuchen (Sekunden)

TELEGRAM_RETRY_DELAY_SEC: float = 2.0  # Verzögerung zwischen Telegram-Versuchen (Sekunden)

TELEGRAM_RETRY_DELAY_SEC: float = 2.0  # Verzögerung zwischen Telegram-Versuchen (Sekunden)

-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
-Versuche
