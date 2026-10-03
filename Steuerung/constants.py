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
# Zieltrennung: ohne Solarquelle Basiskomfort, mit PV optionaler Solarbuffer.
BASIS_COMFORT_TEMP_C: float = 42.0
MAX_SOLAR_BUFFER_TEMP_C: float = 48.0

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
FORECAST_MAX_AGE_HOURS: int = 12
# Nach einem FEHLGESCHLAGENEN Prognose-Abruf (Netz/DNS weg) erst nach dieser
# Zeit erneut fragen. Ohne Throttle wuerde der 10-s-Loop die Open-Meteo-API
# dauerhaft anfragen und das Log fluten (beobachtet im Pi-Log 12.09.).
FORECAST_RETRY_INTERVAL_MIN: int = 15
# Wartezeit zwischen zwei Solax-Retry-Versuchen. Als Konstante, damit die
# Retry-Logik in Tests ohne echte Wartezeit pruefbar ist (3 Versuche x 2
# Wartezeiten x 5 s = 10 s pro Test, sonst 40 s nur fuer diese Pfade).
SOLAX_RETRY_DELAY_SEC: float = 5.0
SOLAX_MAX_RETRIES: int = 3
# Gleiche Situation fuer den Open-Meteo-Abruf: die Retry-Wartezeit als
# Konstante, damit die Timeout-/Fehlerpfade ohne echte Wartezeit testbar sind
# (2 Versuche x 3 s = 3 s pro Test, sonst der teuerste Einzelposten).
FORECAST_API_TIMEOUT_SEC: int = 10
FORECAST_MAX_RETRIES: int = 2
FORECAST_RETRY_DELAY_SEC: float = 3.0
HEALTHCHECK_PING_INTERVAL_MIN: float = 1.0
VPN_CHECK_INTERVAL_SEC: int = 60
WEATHER_UPDATE_INTERVAL_MIN: int = 60
# Wie oft RSS/verfuegbarer RAM ins Log geschrieben werden. Macht OOM-Faelle
# (Kernel-Kill, status=9/KILL) im Steuerungs-Log nachvollziehbar - vorher
# waren sie nur im Kernel-Journal sichtbar.
MEMORY_LOG_INTERVAL_SEC: int = 3600

# --- Compressor Verification ---
COMPRESSOR_VERIFICATION_DELAY_MIN: int = 10
COMPRESSOR_VERIFICATION_CHECK_INTERVAL_MIN: int = 1
COMPRESSOR_VERD_DELTA_MIN: float = 1.5
COMPRESSOR_VERD_START_TEMP_COLD: float = 15.0
COMPRESSOR_VERD_DELTA_COLD_MIN: float = -0.5
COMPRESSOR_VERD_COLD_MAX: float = 12.0
COMPRESSOR_UNTEN_DELTA_MIN: float = 0.2
COMPRESSOR_VERIFICATION_ERROR_THRESHOLD: int = 2

# Verifizierung im Legionellenmodus: Der untere Fuehler saettigt am oberen
# Boilerende (Nettorate < 0.2 K/10 min, Incident 11.09) und wuerde Fehlalarm
# erzeugen, obwohl der Verdampfer den Kompressor einwandfrei bestaetigt.
# Im Legionellenmodus genuegt daher der Verdampfer-Abfall als Beweis.
LEGIONELLEN_VERIFY_NUR_VERDAMPFER: bool = True

# --- Solar Data Freshness ---
# Einheitliche Frischegrenze für Solax-Livedaten und API/Regelung.
SOLAR_DATA_STALE_THRESHOLD_MIN: int = 15
SOLAR_API_TIMEOUT_SEC: int = 10
# Hintergrund-Refresh bleibt hart begrenzt; niemals den 10-s-Hauptloop blockieren.
# Der Wert muss aber zum Retry-Budget passen, sonst bricht die Deadline die
# Wiederholungen ab und der innere Fallback ("verwende Fallback-Daten") wird
# nie erreicht: 3 x SOLAR_API_TIMEOUT_SEC (10 s) + 2 x SOLAX_RETRY_DELAY_SEC
# (5 s) = 40 s, plus Reserve. Befund Betriebslog 02.10.2026: mit 15 s endete
# der Lauf nach Versuch 2 mit TimeoutError statt sauberer Fallback.
SOLAR_REFRESH_DEADLINE_SEC: int = 45
SOLAR_REFRESH_INTERVAL_SEC: int = 60

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

# --- Energiebilanz / Stromquellen-Klassifikation ---
# EINE Quelle der Wahrheit fuer die Zuordnung "WP laeuft mit PV / Batterie /
# Netz". Vorher stand -50 W zusaetzlich in entscheidungs_log.py UND
# (dupliziert) in Analyse/analysis_core.py - bei Aenderung einer Schwelle
# drifteten KPI-Anzeige und Offline-Analyse auseinander.
#
# SEMANTIK (am 02.10.2026 mit dem Betreiber geklaert): `feedin` ist die Leistung,
# die INS NETZ gespeist wird - nicht die PV-Erzeugung. Die Anlage ist
# wechselrichtergekoppelt: PV speist erst Haus und WP, der Rest laedt die
# Batterie, erst der allrestliche Rest wird eingespeist.
#   feedin > 0  -> echter Export
#   feedin = 0  -> PV deckt den Lokalbezug, Ueberschuss laedt die Batterie
#   feedin < 0  -> Haus kauft Netzstrom
# Warum die alte Heuristik nicht ausreichte: sie kannte nur feedin, nicht die
# Batterie, und verbuchte deshalb "feedin >= -50 -> pv_batterie" pauschal -
# Folge im Log 18.09.-01.10.2026: 100 % PV-Anteil an 13 von 14 Tagen.
NETZKAUF_GRENZE_W: float = -50.0
# Ab hier ist es ein echter, nutzbarer PV-Ueberschuss (nicht nur "kein
# Netzzukauf"). Darunter entscheidet das Batterie-Feld.
PV_UEBERSCHUSS_MIN_W: float = 100.0
# Lädt die Batterie, ist Solarstrom im Spiel: die Batterie kann nur aus dem
# PV-Ueberschuss gespeist werden, also erzeugt die PV mehr als den Lokalbezug
# (Haus + WP) - der WP-Lauf ist damit solar gedeckt, auch bei feedin = 0.
# Der kleine Totbereich faengt Leerlauf-Rauschen um 0 W ab.
BATTERIE_LADUNG_MIN_W: float = 50.0
# Kennzeichnung fuer Luecken in der Messung: unterhalb dieser Leistung UND ohne
# Batterie-Bewegung laesst sich die Quelle nicht bestimmen (weder PV noch
# Batterie). Wird als "unklar" ausgewiesen, statt faelschlich als PV.
QUELLE_UNKLAR_MAX_W: float = 0.0

# --- Safety ---
SOLAR_ERROR_MIN_PAUSE_MIN: int = 30

# Wiederholungsintervall des Telegram-Alarms bei fehlenden Solax-Livedaten.
# Nicht "einmal": ein Ausfall, der nach 20 min endet, und einer ueber den
# ganzen Tag muessen unterschiedlich auffallen. 90 min haelt den Kanal
# sichtbar, ohne zu fluten.
STALE_ALARM_MINUTEN: float = 90.0

# --- GPIO / Hardware ---
RELAY_ON_STATE: int = 1
RELAY_OFF_STATE: int = 0

# --- Network / API ---
# ACHTUNG: Das sind die DEFAULT-Werte. Tatsaechlich verwendet wird
# AppConfig.Heizungssteuerung.API_HOST/API_PORT (config_manager.py importiert
# diese Konstanten als Default) - Port hier NICHT separat pflegen.
API_SERVER_HOST: str = "0.0.0.0"
API_SERVER_PORT: int = 8000
REQUEST_TIMEOUT_SEC: int = 10
HEALTHCHECK_REQUEST_TIMEOUT_SEC: int = 5

# --- Deployment ---
NGINX_PORT: int = 80
NGINX_SSL_PORT: int = 443
