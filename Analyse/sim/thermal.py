"""Thermisches Speichermodell des Warmwasserspeichers (3 Schichten).

Kalibriert gegen ``logs/24.9/heizungsdaten.csv`` (23 Tage Realbetrieb);
die Herleitung der Parameter steht in ``calibrate.py``.

Modellannahmen
--------------
* Der Speicher (300 L) wird in drei Knoten geteilt, die den drei DS18B20-
  Fuehlern entsprechen: ``unten`` (kalt, Zapf-Eintritt), ``mitte``,
  ``oben`` (heiss, Schichtung).
* Die Waermepumpe bringt ihre Waerme am unteren Bereich ein (Waermetauscher
  am Speicherboden). Deshalb steigt ``unten`` zuerst und schnell.
* Beim Heizen ist die Konvektion stark (Heizdelta -> Umwaelzung), im
  Leerlauf schwach (stabile Schichtung: ``mitte - unten`` ~ +12 K).
* Waermeverluste sind linear zur Temperaturdifferenz gegen die
  Umgebungstemperatur des Speichers.
* Zapfungen ersetzen Volumen im oberen Bereich durch Kaltwasser am Boden.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import List, Optional

# Spezifische Waermekapazitaet von Wasser: 4.18 kJ/(kg*K) = 1.1611 Wh/(kg*K)
C_WASSER_WH_K = 4.18 / 3.6


@dataclass
class Schicht:
    """Ein Wasserknoten des Speichers."""

    name: str
    volumen_l: float
    temperatur: float
    #: Verlust des letzten Zeitschritts in Wh (intern, fuer die Bilanz).
    _verlust_wh: float = 0.0

    @property
    def kapazitaet_wh_k(self) -> float:
        return self.volumen_l * C_WASSER_WH_K


@dataclass
class SpeicherParameter:
    """Physikalische Parameter des Speichermodells."""

    volumen_l: float = 300.0
    #: Waermeeintrag des Heizkreises bei laufendem Kompressor (W).
    heizleistung_w: float = 1320.0
    #: Waermeverlustkoeffizient des Speichers (W/K gegen die Umgebung).
    verlust_w_k: float = 2.6
    #: Konvektion unten<->mitte bei laufendem Kompressor (W/K).
    konvektion_an_w_k: float = 900.0
    #: Konvektion mitte<->oben bei laufendem Kompressor (W/K).
    konvektion_oben_an_w_k: float = 260.0
    #: Konvektion unten<->mitte im Leerlauf (W/K) - schwache Schichtung.
    konvektion_aus_w_k: float = 12.0
    #: Konvektion mitte<->oben im Leerlauf (W/K).
    konvektion_oben_aus_w_k: float = 3.0
    #: Anteil der Leistung, der direkt in die untere Schicht fliesst.
    einspeisung_anteil_unten: float = 1.0
    #: Temperatur des Kaltwassers beim Zapfen (Grad C).
    kaltwasser_c: float = 10.0
    #: Umgebungstemperatur des Speichers (Grad C).
    umgebung_c: float = 20.0

    def kopie(self) -> "SpeicherParameter":
        return replace(self)


class Speicher:
    """Dreischichtiger Waermespeicher mit Zapfung und Schichtverlusten."""

    def __init__(self, parameter: Optional[SpeicherParameter] = None,
                 start_unten: float = 40.0, start_mitte: float = 42.0,
                 start_oben: float = 44.0):
        self.p = parameter or SpeicherParameter()
        v = self.p.volumen_l
        self.unten = Schicht("unten", v * 0.28, start_unten)
        self.mitte = Schicht("mitte", v * 0.34, start_mitte)
        self.oben = Schicht("oben", v * 0.38, start_oben)
        self.schichten: List[Schicht] = [self.unten, self.mitte, self.oben]

    @property
    def temperaturen(self) -> dict:
        return {s.name: s.temperatur for s in self.schichten}

    @property
    def mitteltemperatur(self) -> float:
        kap = sum(s.kapazitaet_wh_k for s in self.schichten)
        return sum(s.temperatur * s.kapazitaet_wh_k for s in self.schichten) / kap

    @property
    def gesamtenergie_wh(self) -> float:
        """Abspeicherbare Waerme ueber Umgebungstemperatur (fuer KPIs)."""
        return sum(
            s.kapazitaet_wh_k * max(0.0, s.temperatur - self.p.umgebung_c)
            for s in self.schichten
        )

    def setze_temperaturen(self, unten: float, mitte: float, oben: float) -> None:
        self.unten.temperatur = unten
        self.mitte.temperatur = mitte
        self.oben.temperatur = oben

    # ------------------------------------------------------------------
    # Zeitschritt
    # ------------------------------------------------------------------
    def schritt(self, dt_s: float, kompressor_ein: bool) -> float:
        """Integriert ``dt_s`` Sekunden.

        Rueckgabe: die dem Speicher zugefuehrte Waermemenge in Wh
        (positiv = Gewinn, negativ = Verlust).
        """
        if dt_s <= 0:
            return 0.0
        p = self.p
        h = dt_s / 3600.0  # Zeitschritt in Stunden

        # --- Waermeeintrag des Heizkreises (Wh) ---
        q_in = p.heizleistung_w * h if kompressor_ein else 0.0
        anteil = min(max(p.einspeisung_anteil_unten, 0.0), 1.0)
        dq_unten = q_in * anteil
        dq_mitte = q_in * (1.0 - anteil)

        # --- Waermeverluste an die Umgebung (Wh, negativ) ---
        dq_umgebung = 0.0
        for s in self.schichten:
            verlust = p.verlust_w_k * (s.temperatur - p.umgebung_c) * h
            s._verlust_wh = verlust
            dq_umgebung += verlust

        # --- Konvektion / Durchmischung (Wh) ---
        if kompressor_ein:
            k_um, k_mo = p.konvektion_an_w_k, p.konvektion_oben_an_w_k
        else:
            k_um, k_mo = p.konvektion_aus_w_k, p.konvektion_oben_aus_w_k

        q_um = k_um * (self.mitte.temperatur - self.unten.temperatur) * h
        q_mo = k_mo * (self.oben.temperatur - self.mitte.temperatur) * h

        # --- Energiebilanz je Knoten (Wh) ---
        e_unten = dq_unten - self.unten._verlust_wh + q_um
        e_mitte = dq_mitte - self.mitte._verlust_wh - q_um + q_mo
        e_oben = -self.oben._verlust_wh - q_mo

        self.unten.temperatur += e_unten / self.unten.kapazitaet_wh_k
        self.mitte.temperatur += e_mitte / self.mitte.kapazitaet_wh_k
        self.oben.temperatur += e_oben / self.oben.kapazitaet_wh_k

        for s in self.schichten:
            s.temperatur = min(max(s.temperatur, 1.0), 99.0)
        return q_in - dq_umgebung

    def zapfe(self, volumen_l: float, temperatur_c: Optional[float] = None) -> float:
        """Entnimmt ``volumen_l`` Liter; Kaltwasser tritt unten wieder ein.

        Energiebilanz (exakt, in Wh): das entnommene Wasser verlaesst den
        Speicher am oberen Bereich, das Kaltwasser tritt unten ein. Weil die
        Volumen der Schichten konstant bleiben, wird der Volumenversatz als
        Kaskade modelliert - jede Schicht rutscht um ``V`` zur naechsthoeheren
        nach, die unterste bekommt Kaltwasser:

            dE_unten = V*c*(T_kalt - T_unten)
            dE_mitte = V*c*(T_unten - T_mitte)
            dE_oben  = V*c*(T_mitte - T_oben)

        Die Summe ist ``V*c*(T_kalt - T_oben)`` - also genau die Waerme, die
        als Warmwasser den Speicher verlaesst. Der starke Abfall am unteren
        Fuehler und die kleinen Abfaelle darueber entsprechen damit exakt dem
        im September-Log gemessenen Zapfprofil (Median -3.3 K).
        """
        if volumen_l <= 0:
            return 0.0
        t_kalt = self.p.kaltwasser_c if temperatur_c is None else temperatur_c
        m = volumen_l * C_WASSER_WH_K
        de_unten = m * (t_kalt - self.unten.temperatur)
        de_mitte = m * (self.unten.temperatur - self.mitte.temperatur)
        de_oben = m * (self.mitte.temperatur - self.oben.temperatur)
        self.unten.temperatur += de_unten / self.unten.kapazitaet_wh_k
        self.mitte.temperatur += de_mitte / self.mitte.kapazitaet_wh_k
        self.oben.temperatur += de_oben / self.oben.kapazitaet_wh_k
        for s in self.schichten:
            s.temperatur = min(max(s.temperatur, 1.0), 99.0)
        return -(de_unten + de_mitte + de_oben)
