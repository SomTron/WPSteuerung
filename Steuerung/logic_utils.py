import logging
import math
from datetime import datetime, timedelta, date, tzinfo
from typing import Optional
from utils import safe_timedelta
from constants import TEMP_MIN_VALID, TEMP_MAX_VALID, REDUCTION_MIN, REDUCTION_MAX, SOLAR_WINDOW_HOURS

def is_valid_temperature(temp: Optional[float], min_temp: float = TEMP_MIN_VALID, max_temp: float = TEMP_MAX_VALID) -> bool:
    """Prüft, ob ein Temperaturwert gültig ist."""
    if temp is None:
        return False
    if not isinstance(temp, (int, float)):
        return False
    if math.isnan(temp) or math.isinf(temp):
        return False
    if temp < min_temp or temp > max_temp:
        return False
    return True

def forecast_kwh_m2_to_wh_m2(value):
    """Wandelt Open-Meteo-kWh/m² explizit in die interne Wh/m²-Einheit um."""
    if value is None:
        return None
    try:
        wert = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(wert) or wert < 0:
        return None
    return wert * 1000.0


def normalize_forecast_wh_qm(value, value_unit="kwh_m2"):
    """Normalisiert einen Forecast anhand seiner expliziten Einheit.

    Die alte Heuristik ("Werte < 100 sind kWh/m²") konnte einen echten
    niedrigen Wh/m²-Wert falsch um thousandfach vergrößern. Die Quelle
    der Tageswerte ist Open-Meteo und liefert kWh/m²; bereits normalisierte
    Werte müssen explizit mit ``value_unit='wh_m2'`` übergeben werden.
    """
    if value_unit == "wh_m2":
        if value is None:
            return None
        try:
            wert = float(value)
        except (TypeError, ValueError):
            return None
        return wert if math.isfinite(wert) and wert >= 0 else None
    if value_unit != "kwh_m2":
        raise ValueError(f"Unbekannte Forecast-Einheit: {value_unit}")
    return forecast_kwh_m2_to_wh_m2(value)


def check_log_throttle(state, attribute_name: str, interval_minutes: float = 5.0) -> bool:
    """Prüft, ob eine Log-Nachricht gesendet werden soll (Throttling)."""
    last_time = getattr(state, attribute_name, None)
    # Mock-/Partial-States dürfen keinen MagicMock als Zeitstempel liefern.
    if not isinstance(last_time, datetime):
        last_time = None
    local_tz = getattr(state, "local_tz", None)
    if not isinstance(local_tz, tzinfo):
        local_tz = None
    now = datetime.now(local_tz)
    if last_time is None or safe_timedelta(now, last_time, local_tz) > timedelta(minutes=interval_minutes):
        setattr(state, attribute_name, now)
        return True
    return False

def is_solar_window(config, state):
    """Prüft, ob die aktuelle Uhrzeit im Solarfenster nach der Nachtabsenkung liegt."""
    now = datetime.now(state.local_tz)
    try:
        end_time_str = config.Heizungssteuerung.NACHTABSENKUNG_END
        end_hour, end_minute = map(int, end_time_str.split(':'))
        potential_night_setback_end_today = now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
        
        if now < potential_night_setback_end_today + timedelta(hours=SOLAR_WINDOW_HOURS):
            night_setback_end_time_today = potential_night_setback_end_today
        else:
            night_setback_end_time_today = potential_night_setback_end_today + timedelta(days=1)
        
        within_solar = night_setback_end_time_today <= now < night_setback_end_time_today + timedelta(hours=SOLAR_WINDOW_HOURS)
        return within_solar
    except Exception as e:
        logging.error(f"Fehler in is_solar_window: {e}")
        return False

def ist_uebergangsmodus_aktiv(state):
    """Prüft, ob aktuell der Übergangsmodus (morgens oder abends) aktiv ist."""
    try:
        now_time = datetime.now(state.local_tz).time()
        
        cfg = state.config.Heizungssteuerung
        def parse_t(s): return datetime.strptime(s, "%H:%M").time()
        
        n_ende = parse_t(cfg.NACHTABSENKUNG_END)
        u_m_ende = parse_t(cfg.UEBERGANGSMODUS_MORGENS_ENDE)
        u_a_start = parse_t(cfg.UEBERGANGSMODUS_ABENDS_START)
        n_start = parse_t(cfg.NACHTABSENKUNG_START)

        morgens_aktiv = n_ende <= now_time <= u_m_ende
        abends_aktiv = u_a_start <= now_time <= n_start
        return morgens_aktiv or abends_aktiv
    except Exception as e:
        logging.error(f"Fehler bei ist_uebergangsmodus_aktiv: {e}")
        return False

def get_validated_reduction(config, section: str, key: str, default: float = 0.0) -> float:
    """Validiert und gibt Temperaturreduktionswerte zurück."""
    try:
        section_obj = getattr(config, section, None)
        if section_obj:
            value = getattr(section_obj, key, default)
        else:
            return default
        reduction = float(value)
        if reduction < REDUCTION_MIN or reduction > REDUCTION_MAX:
            return default
        return reduction
    except Exception:
        return default


# --- Sommer-Modus ---
# Ereignis-Konstanten der Bewertung (fuer das Logging im Aufrufer)
SOMMER_KEIN_EREIGNIS = "kein_ereignis"
SOMMER_AKTIVIERT = "aktiviert"
SOMMER_DEAKTIVIERT_PROGNOSE = "deaktiviert_prognose"
SOMMER_DEAKTIVIERT_DATEN = "deaktiviert_daten"

def evaluate_sommer_modus(
    benoetigte_tage: int,
    mindest_prognose_wh: float,
    rad_today,
    rad_tomorrow,
    rad_day2,
    heute: date,
    aktueller_zaehler: int,
    ist_aktiv: bool,
    letzter_bewertungstag=None,
):
    """
    Reine Bewertung des Sommer-Modus (ohne Seiteneffekte -> gut unit-testbar).

    Semantik:
      - Prognose 'gut' = heute, morgen UND uebermorgen jeweils >= mindest_prognose_wh.
      - Es zaehlt maximal EINE Bewertung pro Kalendertag (mehrere Forecast-Updates
        am selben Tag zaehlen NICHT mehrfach!).
      - Eine schlechte oder unvollstaendige Prognose setzt die Serie zurueck und
        deaktiviert den Modus sofort (konservativ).
      - Eine Luecke von mehr als einem Tag ohne gueltige Bewertung (Ausfall,
        Neustart) bricht die Serie ebenfalls.

    Hintergrund: Ist sichergestellt, dass mehrtaegig genug PV-Strom kommt, ist
    Vorheizen/Buffern unnoetig -> die Solltemperatur wird dann ueber
    temperatur_offset_c gesenkt (Anwendung siehe priority_control_logic).

    Rueckgabe: (neuer_zaehler, aktiv, letzter_bewertungstag, ereignis)
    """
    # Luecke in der Bewertungshistorie? Dann kann die Serie nicht fortgesetzt werden.
    if letzter_bewertungstag is not None and (heute - letzter_bewertungstag).days > 1:
        aktueller_zaehler = 0

    daten_vollstaendig = all(v is not None for v in (rad_today, rad_tomorrow, rad_day2))
    if not daten_vollstaendig:
        ereignis = SOMMER_DEAKTIVIERT_DATEN if (ist_aktiv or aktueller_zaehler > 0) else SOMMER_KEIN_EREIGNIS
        return 0, False, letzter_bewertungstag, ereignis

    alle_gut = all(v >= mindest_prognose_wh for v in (rad_today, rad_tomorrow, rad_day2))

    if not alle_gut:
        ereignis = SOMMER_DEAKTIVIERT_PROGNOSE if (ist_aktiv or aktueller_zaehler > 0) else SOMMER_KEIN_EREIGNIS
        return 0, False, letzter_bewertungstag, ereignis

    # Gute Prognose: nur einmal pro Kalendertag hochzaehlen
    if letzter_bewertungstag != heute:
        aktueller_zaehler += 1

    ereignis = SOMMER_KEIN_EREIGNIS
    aktiv = ist_aktiv
    if aktueller_zaehler >= benoetigte_tage and not ist_aktiv:
        aktiv = True
        ereignis = SOMMER_AKTIVIERT

    return aktueller_zaehler, aktiv, heute, ereignis
