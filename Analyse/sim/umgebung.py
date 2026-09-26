"""Stromseite der Simulation: PV, Batterie, Hauslast, Netz.

Die PV-Profile des Szenarios ``september`` sind die real gemessenen
Tagessprofile aus ``logs/24.9``. Die Winterszenarien skalieren das
Septemberprofil mit einem physikalisch begruendeten Faktor
(Globalstrahlung Kärnten: September ~5 kWh/m², Dezember ~0.5-1.0 kWh/m²).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

#: Real gemessene September-PV-Erzeugung (W je Stunde), aus logs/24.9.
SEPTEMBER_PV: Dict[int, float] = {
    0: 229, 1: 220, 2: 215, 3: 225, 4: 216, 5: 205, 6: 234, 7: 240,
    8: 516, 9: 1227, 10: 3708, 11: 5576, 12: 6256, 13: 6735, 14: 6480,
    15: 6131, 16: 4864, 17: 3027, 18: 1416, 19: 488, 20: 359, 21: 341,
    22: 337, 23: 271,
}

#: Real gemessene September-Einspeisung (W je Stunde) bei ausgeschalteter WP.
SEPTEMBER_FEEDIN: Dict[int, float] = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: -7, 6: -16, 7: -3, 8: -1,
    9: 597, 10: 2976, 11: 4686, 12: 5267, 13: 6006, 14: 6004, 15: 5694,
    16: 4400, 17: 2553, 18: 781, 19: 3, 20: 0, 21: 0, 22: 0, 23: 0,
}

#: Real gemessene Tagesprognose im September (Wh/m²).
SEPTEMBER_FORECAST_WH = 5000.0

#: Hauslast bei ausgeschalteter WP, stuendlich gemessen (Median aus
#: logs/24.9, Kompressor AUS, 148745 Samples). Nachts konstant ~200-330 W,
#: morgens und abends Koch-/Badspitzen. Diese Werte bestimmen, wann die
#: PV den Hausverbrauch deckt - und damit das Entladefenster der Batterie.
HAUSLAST_W: Dict[int, float] = {
    0: 229, 1: 195, 2: 196, 3: 233, 4: 194, 5: 194, 6: 234, 7: 218,
    8: 262, 9: 274, 10: 300, 11: 320, 12: 340, 13: 330, 14: 320,
    15: 310, 16: 330, 17: 500, 18: 1120, 19: 329, 20: 297, 21: 314,
    22: 317, 23: 252,
}


def hauslast_w(stunde: int, faktor: float = 1.0) -> float:
    """Hauslast in Watt fuer die gegebene Stunde (aus dem Log kalibriert)."""
    return HAUSLAST_W.get(stunde, 240.0) * faktor


@dataclass
class BatterieParameter:
    kapazitaet_wh: float = 10000.0
    soc_start_prozent: float = 55.0
    #: Wirkungsgrad (Richtung je Ladevorgang, quadratisch verrechnet).
    eta: float = 0.95
    max_ladung_w: float = 3000.0
    max_entladung_w: float = 2500.0
    soc_min_prozent: float = 12.0
    soc_max_prozent: float = 100.0
    #: Nachts ist die Batterie im Realbetrieb passiv (Log 24.9: bat ~ 0 W
    #: von 19-7 Uhr, das Haus laeuft dann aus dem Netz). Ohne diesen Anteil
    #: wuerde das Modell die Batterie nachts leerziehen und den
    #: Batterie-Heizpfad der Regelung voellig unmoeglich machen.
    nacht_entladung_anteil: float = 0.0
    #: Ab dieser PV-Leistung arbeitet der Wechselrichter im Eigenverbrauchs-
    #: betrieb und nutzt die Batterie. Kalibriert am September-Log: die
    #: Entladung tritt nur in den PV-Schultern 8-9 und 18-19 Uhr auf
    #: (AC 330-1120 W), nie bei tiefer Winter-PV und nie nachts.
    pv_schwelle_w: float = 250.0


class Batterie:
    """Energiemodell der Hausbatterie (Selbstverbrauchsbetrieb).

    ``rechne`` liefert die fuer die Regelung sichtbaren Groessen:
    Batterieentladung in Watt (positiv) und die verbleibende Einspeisung.

    Verhalten wie im September-Log gemessen:
    * PV-Ueberschuss laedt die Batterie, der Rest wird eingespeist.
    * Reicht die PV nicht fuer Haus + WP, deckt die Batterie das Defizit -
      aber nur solange PV vorhanden ist (Eigenverbrauch).
    * Nachts bleibt die Batterie stehen; das Haus laeuft aus dem Netz.
    """

    def __init__(self, parameter: Optional[BatterieParameter] = None):
        self.p = parameter or BatterieParameter()
        self.soc = float(self.p.soc_start_prozent)

    def rechne(self, dt_h: float, pv_w: float, hauslast_w_w: float,
               wp_w: float) -> tuple:
        """Liefert (entladung_w, ladung_w, einspeisung_rest_w, netz_w)."""
        if dt_h <= 0:
            return 0.0, 0.0, 0.0, hauslast_w_w + wp_w - pv_w
        p = self.p
        saldo = pv_w - hauslast_w_w - wp_w
        if saldo > 0:
            platz_wh = max(0.0, (p.soc_max_prozent - self.soc) / 100.0 * p.kapazitaet_wh)
            geladen_wh = min(min(p.max_ladung_w, saldo) * dt_h * p.eta, platz_wh)
            self.soc = min(p.soc_max_prozent, self.soc + geladen_wh / p.kapazitaet_wh * 100.0)
            ladung_w = geladen_wh / dt_h
            einspeisung = max(0.0, saldo - ladung_w)
            return 0.0, ladung_w, einspeisung, 0.0

        fehlend = -saldo
        # Nachts (ohne PV) entlaedt die Batterie nur anteilig - das Haus
        # laeuft sonst wie im echten Betrieb ueberwiegend aus dem Netz.
        if pv_w < p.pv_schwelle_w:
            fehlend_netz = fehlend * (1.0 - p.nacht_entladung_anteil)
            entl_budget = 0.0
        else:
            fehlend_netz = fehlend
            entl_budget = fehlend
        verfuegbar_wh = max(0.0, (self.soc - p.soc_min_prozent) / 100.0 * p.kapazitaet_wh)
        entladen_wh = min(min(p.max_entladung_w, entl_budget) * dt_h, verfuegbar_wh / p.eta)
        self.soc = max(p.soc_min_prozent, self.soc - entladen_wh / p.kapazitaet_wh * 100.0)
        entladung_w = entladen_wh / dt_h
        rest = max(0.0, fehlend_netz - entladung_w)
        return entladung_w, 0.0, 0.0, rest

    @property
    def energie_wh(self) -> float:
        return self.soc / 100.0 * self.p.kapazitaet_wh


@dataclass
class Szenario:
    """Ein Simulationsszenario ueber mehrere Tage."""

    name: str
    start: date
    tage: int
    #: PV-Skalierung gegenueber dem Septemberprofil (1.0 = Original).
    pv_faktor: float = 1.0
    #: Staerke der zufaelligen Wolkenmodulation (0 = aus).
    pv_streuung: float = 0.0
    #: Tagesprognose heute/morgen/uebermorgen in Wh/m².
    forecast_wh: float = SEPTEMBER_FORECAST_WH
    forecast_wh_morgen: Optional[float] = None
    forecast_wh_uebermorgen: Optional[float] = None
    #: Faktor auf die Hauslast (Winter: etwas hoeher).
    hauslast_faktor: float = 1.0
    batterie: bool = True
    #: Start-Ladezustand der Batterie in Prozent.
    soc_start_prozent: float = 55.0
    #: Nutzbare Batteriekapazitaet in Wh.
    batterie_kapazitaet_wh: float = 10000.0
    #: SOC-Obergrenze, die im Szenario nie erreicht wird ("nie voll").
    soc_max_prozent: float = 100.0
    #: Anteil des Nachtdefizits, den die Batterie deckt (Realbetrieb: 0).
    nacht_entladung_anteil: float = 0.0
    #: Umgebungstemperatur des Speichers (Grad C) - treibt die Verluste.
    umgebung_c: float = 20.0
    #: Elektrische WP-Leistungsaufnahme (W).
    wp_leistung_w: float = 600.0
    seed: int = 42
    #: Vorbelegung: der letzte Legionellen-Lauf lag zu Simulationsbeginn
    #: so viele Tage zurueck (simuliert einen bestehenden Betrieb).
    legionellen_vor_tagen: int = 0

    def __post_init__(self):
        if self.forecast_wh_morgen is None:
            self.forecast_wh_morgen = self.forecast_wh
        if self.forecast_wh_uebermorgen is None:
            self.forecast_wh_uebermorgen = self.forecast_wh


#: Vordefinierte Szenarien
SZENARIEN: Dict[str, Szenario] = {
    "september": Szenario(
        name="September (PV-reich, echte Profile)",
        start=date(2026, 9, 8), tage=7, pv_faktor=1.0, pv_streuung=0.12,
        forecast_wh=5000.0, hauslast_faktor=1.0, umgebung_c=20.0,
    ),
    "winter_wenig_pv": Szenario(
        name="Dezember, wenig PV (trueb, 700 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=0.13, pv_streuung=0.35,
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
    ),
    "winter_batterie_leer": Szenario(
        name=("Dezember, wenig PV, Batterie wird NIE voll "
              "(Deckel 55 %, Start 20 %)"),
        start=date(2026, 12, 7), tage=7, pv_faktor=0.13, pv_streuung=0.35,
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
        soc_start_prozent=20.0, soc_max_prozent=55.0,
    ),
    "winter_batterie_voll": Szenario(
        name=("Dezember, wenig PV, Batterie kurzfristig VOLL "
              "(100 %, Kapazitaet klein)"),
        start=date(2026, 12, 7), tage=7, pv_faktor=0.13, pv_streuung=0.35,
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
        soc_start_prozent=100.0, batterie_kapazitaet_wh=3000.0,
        soc_max_prozent=100.0,
    ),
    "winter_ohne_pv": Szenario(
        name="Dezember, kein PV (Starknebel, 120 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=0.0, pv_streuung=0.0,
        forecast_wh=120.0, hauslast_faktor=1.15, umgebung_c=10.0,
    ),
    "winter_ohne_pv_lang": Szenario(
        name="Dezember, 5 Wochen ohne PV (bestehender Betrieb)",
        start=date(2026, 12, 1), tage=35, pv_faktor=0.0, pv_streuung=0.0,
        forecast_wh=120.0, hauslast_faktor=1.15, umgebung_c=10.0,
        legionellen_vor_tagen=10,
    ),
    "winter_misch": Szenario(
        name="Dezember, wechselnd (1600 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=0.22, pv_streuung=0.5,
        forecast_wh=1600.0, hauslast_faktor=1.15, umgebung_c=12.0,
    ),
}
