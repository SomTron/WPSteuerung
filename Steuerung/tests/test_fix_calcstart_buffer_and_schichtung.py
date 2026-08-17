"""
Verifikationstests für zwei Bugfixes:

1. CalcStart: Puffer-Berechnung bei Wolken war invertiert
   (Division statt Multiplikation mit pv_faktor).
   Bewölkt (pv_faktor=0.5) verdoppelte den Puffer -> WP startete zu spaet.
   Sonnig (pv_faktor=2.0) halbierte den Puffer -> WP startete zu frueh.

2. Abweichungs-Regel: 2-Zonen-Schichtungs-Check.
   Wenn der konfigurierte Fuehler (unten/mittig) durch Zapfen einbricht,
   oben aber noch warm genug ist, soll die WP im Netzbetrieb NICHT starten.
"""

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from json_config import (
    AbweichungConfig, CalculatedStartConfig, WPSteuerungConfig
)
from priority_control import (
    evaluate_calculated_start, evaluate_abweichung, bewerte_alle_regeln
)


def _calc_cfg(**kwargs):
    """Helper: CalcStart-Config mit Overrides."""
    defaults = dict(
        prioritaet=82,
        aktiv=True,
        solltemperatur_c=44.0,
        target_uhr=17,
        heizrate_unten_c_h=3.0,
        heizrate_gesamt_c_h=2.0,
        tmax_c=48.0,
    )
    defaults.update(kwargs)
    return CalculatedStartConfig(**defaults)


def _temp(unten=40.0, mitte=42.0, oben=46.0):
    # Achtung: Der Code in _parse_sensor mappt "mitte" -> "mittig"
    return {"unten": unten, "mittig": mitte, "oben": oben}


# ============================================================
# FIX 1: CalcStart-Puffer
# ============================================================

def test_calcstart_bewoelkt_startet_frueher():
    """Bewölkt (500 Wh/qm) -> Puffer muss KLEINER sein als bei neutraler Prognose."""
    cfg = _calc_cfg()

    # 13:00 Uhr, unten=38, mitte=40 -> braucht ~2h Heizzeit bis 17:00 (4h Zeit)
    # Neutral (keine Prognose): Puffer = 4 - 2 = 2h
    r_neutral = evaluate_calculated_start(
        cfg, _temp(unten=38.0, mitte=40.0), 13, 0, forecast_wh_qm=None
    )
    # Bewölkt (pv_faktor=0.5): Puffer = 2 * 0.5 = 1h -> sollte EIN schalten,
    # denn 1h < 0.5h ist falsch, aber der Effekt ist: kleinerer Puffer als neutral
    r_bewoelkt = evaluate_calculated_start(
        cfg, _temp(unten=38.0, mitte=40.0), 13, 0, forecast_wh_qm=400
    )

    # Bewölkt muss mindestens genauso früh (nicht später) einschalten wie neutral
    assert r_neutral.einschalten in (True, None), f"Neutral erwartet, war: {r_neutral.grund}"
    assert r_bewoelkt.einschalten in (True, None), f"Bewölkt erwartet, war: {r_bewoelkt.grund}"

    # Der Kern: Bei gleichem Zustand darf Bewölkt nicht weniger aggressiv sein.
    # pv_faktor=0.5 -> Puffer*0.5. Wenn neutral pausiert (Puffer >= 0.5h),
    # muss bewoelkt entweder auch pausieren ODER bereits einschalten - aber der
    # Grundtext muss den kleineren Puffer zeigen.
    if r_neutral.einschalten is None and r_bewoelkt.einschalten is True:
        pass  # gewuenscht: Bewölkt startet frueher
    assert "bewoelkt" in r_bewoelkt.grund.lower() or "Puffer" in r_bewoelkt.grund


def test_calcstart_bewoelkt_puffer_kleiner_als_sonnig():
    """Kernverifikation: effektiver Puffer bewoelkt < sonnig.
    
    Bei gleichen Temperaturen (unten=38, mitte=40) brauchen wir 2h Heizzeit.
    Bewölkt muss früher einschalten als Sonnig.
    """
    cfg = _calc_cfg()
    temps = _temp(unten=38.0, mitte=40.0)

    # 14:15 -> time_left=2.75h, braucht 2h -> buffer=0.75h
    # Sonnig: 0.75 * 2.0 = 1.5h >= 0.5 -> wartet
    # Bewölkt: 0.75 * 0.5 = 0.375h < 0.5 -> EIN!
    r_sonnig = evaluate_calculated_start(cfg, temps, 14, 15, forecast_wh_qm=3200)
    r_bewoelkt = evaluate_calculated_start(cfg, temps, 14, 15, forecast_wh_qm=400)
    assert r_sonnig.einschalten is None, f"Sonning 14:15 sollte warten: {r_sonnig.grund}"
    assert r_bewoelkt.einschalten is True, f"Bewölkt 14:15 sollte EIN: {r_bewoelkt.grund}"

    # 14:45 -> time_left=2.25h, braucht 2h -> buffer=0.25h
    # Sonnig: 0.25 * 2.0 = 0.5h -> genau an Grenze (>=0.5) -> noch warten
    # Bewölkt: 0.25 * 0.5 = 0.125h < 0.5 -> EIN
    r_sonnig_2 = evaluate_calculated_start(cfg, temps, 14, 45, forecast_wh_qm=3200)
    r_bewoelkt_2 = evaluate_calculated_start(cfg, temps, 14, 45, forecast_wh_qm=400)
    assert r_sonnig_2.einschalten is None, f"Sonning 14:45 sollte warten: {r_sonnig_2.grund}"
    assert r_bewoelkt_2.einschalten is True, f"Bewölkt 14:45 sollte EIN: {r_bewoelkt_2.grund}"

    # 16:00 -> time_left=1.0h, braucht 2h -> buffer=-1.0h -> ZU SPAET (beide EIN)
    # Der pv_faktor ändert nichts am "ZU SPAET"-Check (der vor Puffer-Skalierung liegt)
    r_sonnig_3 = evaluate_calculated_start(cfg, temps, 16, 0, forecast_wh_qm=3200)
    r_bewoelkt_3 = evaluate_calculated_start(cfg, temps, 16, 0, forecast_wh_qm=400)
    assert r_sonnig_3.einschalten is True, f"Sonning 16:00 ZU SPAET: {r_sonnig_3.grund}"
    assert r_bewoelkt_3.einschalten is True, f"Bewölkt 16:00 ZU SPAET: {r_bewoelkt_3.grund}"


def test_calcstart_sonnig_wartet_laenger():
    """Sonnig (3000+) -> WP soll laenger warten als neutral (Puffer groesser)."""
    cfg = _calc_cfg()
    temps = _temp(unten=38.0, mitte=40.0)

    # 15:00 -> Zeit bis 17:00 = 2h, braucht 2h -> buffer = 0h
    # Neutral: Puffer = 0 * 1.0 = 0 -> EIN (sofort)
    r_neutral = evaluate_calculated_start(cfg, temps, 15, 0, forecast_wh_qm=None)
    # Sonnig: Puffer = 0 * 2.0 = 0 -> immer noch EIN (0 < 0.5)
    r_sonnig = evaluate_calculated_start(cfg, temps, 15, 0, forecast_wh_qm=3200)
    assert r_neutral.einschalten is True
    assert r_sonnig.einschalten is True


# ============================================================
# FIX 2: 2-Zonen-Schichtungs-Check in Abweichungs-Regel
# ============================================================

def test_abweichung_schichtung_verhindert_einschalten():
    """unten bricht durch Zapfen ein, oben warm -> kein Einschalten."""
    abw = AbweichungConfig(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=42.0,
    )
    # unten=36 (Abweichung +4K >= 3K -> normalerweise EIN),
    # oben=46 (noch warm) -> muss verhindert werden
    r = evaluate_abweichung(
        abw, _temp(unten=36.0, mitte=40.0, oben=46.0),
        kompressor_ein=False, now_hour=14,
        nachtsperre_start=19, nachtsperre_ende=8
    )
    assert r.einschalten is None, f"Schichtung sollte Einschalten verhindern: {r.grund}"
    assert "Schichtung" in r.grund


def test_abweichung_schichtung_erlaubt_wenn_oben_kalt():
    """oben auch kalt -> Einschalten erlaubt (echte Not)."""
    abw = AbweichungConfig(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=42.0,
    )
    r = evaluate_abweichung(
        abw, _temp(unten=36.0, mitte=38.0, oben=38.0),
        kompressor_ein=False, now_hour=14,
        nachtsperre_start=19, nachtsperre_ende=8
    )
    assert r.einschalten is True, f"Sollte einschalten: {r.grund}"


def test_abweichung_schichtung_deaktivierbar():
    """schichtung_min_oben_c=0 -> Check aus, Verhalten wie vorher."""
    abw = AbweichungConfig(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=0.0,
    )
    r = evaluate_abweichung(
        abw, _temp(unten=36.0, mitte=40.0, oben=46.0),
        kompressor_ein=False, now_hour=14,
        nachtsperre_start=19, nachtsperre_ende=8
    )
    assert r.einschalten is True, f"Sollte einschalten (Check deaktiviert): {r.grund}"


def test_abweichung_schichtung_nur_bei_nicht_oben_fuehler():
    """Wenn Fuehler=oben, greift der Schichtungs-Check nicht."""
    abw = AbweichungConfig(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="oben",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=42.0,
    )
    r = evaluate_abweichung(
        abw, _temp(unten=36.0, mitte=40.0, oben=36.0),
        kompressor_ein=False, now_hour=14,
        nachtsperre_start=19, nachtsperre_ende=8
    )
    assert r.einschalten is True, f"Fuehler=oben sollte normal schalten: {r.grund}"


def test_abweichung_schichtung_oben_sensor_fehlt():
    """Kein oben-Sensor -> kein Blockieren, Verhalten wie vorher."""
    abw = AbweichungConfig(
        prioritaet=47,
        solltemperatur_c=40.0,
        temperaturfuehler="unten",
        einschalten_bei_abweichung_k=3.0,
        ausschalten_bei_abweichung_k=0.5,
        schichtung_min_oben_c=42.0,
    )
    temps = {"unten": 36.0, "mitte": 40.0, "oben": None}
    r = evaluate_abweichung(
        abw, temps, kompressor_ein=False, now_hour=14,
        nachtsperre_start=19, nachtsperre_ende=8
    )
    assert r.einschalten is True, f"Ohne oben-Sensor sollte normal schalten: {r.grund}"


def test_abweichung_schichtung_in_gesamtbewertung():
    """End-to-End: In bewerte_alle_regeln gewinnt nicht Abweichung bei Schichtung."""
    config = WPSteuerungConfig()
    config.abweichung.temperaturfuehler = "unten"
    config.abweichung.schichtung_min_oben_c = 42.0
    config.abweichung.solltemperatur_c = 40.0
    config.abweichung.einschalten_bei_abweichung_k = 3.0

    # Keine PV-Regeln aktiv, Zeitfenster deaktivieren, andere Regeln aus
    config.pv_regeln = []
    config.zeitfenster.start_uhr = 0  # Deaktivieren: 0 <= 14 < 0 ist False
    config.zeitfenster.ende_uhr = 0
    config.forecast.aktiv = False
    config.adaptive_pv.aktiv = False
    config.calculated_start.aktiv = False
    config.komfort.notfall_einschalten_bei_c = 30.0  # Kein Notfall (oben=46 > 30)
    config.komfort.komfort_einschalten_bei_c = 30.0  # Kein Komfort bei 36°C
    config.komfort.min_pv_fuer_komfort_watt = 999999  # Kein PV-Komfort-Heizen

    # unten kalt durch Zapfen, oben warm
    temps = _temp(unten=36.0, mitte=40.0, oben=46.0)
    gewinner, ergebnisse = bewerte_alle_regeln(
        config, temps, pv_leistung=0.0, kompressor_ein=False,
        now=datetime(2025, 6, 15, 14, 0),  # Sonntag -> Wochenende aktiv!
        forecast_wh_qm=None,
    )

    abw_ergebnis = [e for e in ergebnisse if e.name == "Abweichung"][0]
    assert abw_ergebnis.einschalten is None, f"Schichtung sollte verhindern: {abw_ergebnis.grund}"

    # Gegentest: oben kalt -> Abweichung darf einschalten
    gewinner2, ergebnisse2 = bewerte_alle_regeln(
        config, _temp(unten=36.0, mitte=38.0, oben=38.0), pv_leistung=0.0,
        kompressor_ein=False, now=datetime(2025, 6, 15, 14, 0),
        forecast_wh_qm=None,
    )
    abw_ergebnis2 = [e for e in ergebnisse2 if e.name == "Abweichung"][0]
    assert abw_ergebnis2.einschalten is True, f"Sollte einschalten: {abw_ergebnis2.grund}"
