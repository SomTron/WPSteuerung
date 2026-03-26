import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple
from logic_utils import parse_t

def classify_pv_value(val: float, low: float, high: float) -> str:
    """Klassifiziert einen PV-Wert in low, mid oder high."""
    if val < low: return "low"
    if val > high: return "high"
    return "mid"

def get_pv_strategy(state) -> str:
    """
    Ermittelt die heutige PV-Strategie basierend auf Prognose heute/morgen.
    Mögliche Werte: 'aggressive', 'balanced', 'conservative', 'cautious'
    """
    today = getattr(state.solar, "forecast_today", None)
    tomorrow = getattr(state.solar, "forecast_tomorrow", None)
    low = getattr(state.solar, "pv_threshold_low_kwh", None)
    high = getattr(state.solar, "pv_threshold_high_kwh", None)

    if any(v is None for v in (today, tomorrow, low, high)):
        return "balanced" # Default fallback

    t_cls = classify_pv_value(today, low, high)
    m_cls = classify_pv_value(tomorrow, low, high)

    if t_cls == "high":
        if m_cls == "low": return "aggressive"
        return "balanced" # high/high or high/mid
    elif t_cls == "mid":
        if m_cls == "high": return "balanced"
        return "balanced"
    else: # today is low
        if m_cls == "high": return "conservative"
        return "cautious"

def estimate_heating_runtime(current_t: float, target_t: float, rate_per_hour: float = 2.0) -> float:
    """Schätzt die benötigte Aufheizzeit in Stunden."""
    delta = target_t - current_t
    if delta <= 0: return 0.0
    return delta / rate_per_hour

def get_heating_deadline(state, target_t: float) -> datetime:
    """Berechnet den spätestmöglichen Einschaltzeitpunkt."""
    try:
        cfg = state.config.Heizungssteuerung
        # Ende des Fensters ist das logische Ende für die Beladung (z.B. NACHTABSENKUNG_START oder Sunset)
        window_end_str = cfg.NACHTABSENKUNG_START
        h, m = map(int, window_end_str.split(':'))
        
        now = datetime.now(state.local_tz)
        deadline_base = now.replace(hour=h, minute=m, second=0, microsecond=0)
        
        # Falls jetzt schon nach n_start ist, gilt morgen (sollte im solar window nicht passieren)
        if now > deadline_base:
            deadline_base += timedelta(days=1)
            
        # Aufheizzeit abziehen
        # Wir nehmen t_unten für Überschuss, aber t_mittig für Komfort. 
        # Hier als Schätzung einen Mix oder t_mittig
        current_t = state.sensors.t_mittig if state.sensors.t_mittig is not None else 40.0
        
        # Nutze gelernte Rate falls vorhanden
        rate = getattr(state.control, "learned_heating_rate", state.heating_rate)
        runtime_h = estimate_heating_runtime(current_t, target_t, rate_per_hour=rate)
        deadline = deadline_base - timedelta(hours=runtime_h)
        
        return deadline
    except Exception as e:
        logging.error(f"Fehler in get_heating_deadline: {e}")
        return datetime.now(state.local_tz) + timedelta(hours=2) # Fallback 2h Puffer

def should_delay_for_peak(state, strategy: str) -> bool:
    """Entscheidet, ob wir auf den Mittags-Peak warten sollten."""
    if strategy not in ("balanced", "conservative"):
        return False
        
    now = datetime.now(state.local_tz)
    # Peak-Fenster meist zwischen 11:00 und 15:00
    peak_start = now.replace(hour=11, minute=0, second=0, microsecond=0)
    peak_end = now.replace(hour=15, minute=0, second=0, microsecond=0)
    
    if peak_start <= now <= peak_end:
        return False # Wir sind bereits im Peak
        
    if now < peak_start:
        # Nur warten, wenn Batterie noch nicht voll ist
        soc = state.solar.soc if state.solar.soc is not None else 0.0
        if soc < 90:
            return True
            
    return False
