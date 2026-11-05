"""Thermisches Speichermodell des Warmwasserspeichers (3 Schichten).

Urspruenglich in ``Analyse/sim/thermal.py`` (Kalibrierung gegen
``logs/24.9/heizungsdaten.csv``, Herleitung in ``calibrate.py``). Seit
05.11.2026 liegt das Modell HIER, weil die Steuerung es braucht: die
Etappenrechnung in ``priority_control.evaluate_calculated_start`` sagt per
Vorwaertssimulation voraus, wann eine Zieltemperatur erreicht ist.
``Analyse/sim/thermal.py`` ist nur noch ein Re-Export - zwei getrennte
Kopien waeren auseinandergedriftet.

Betriebsdaten-Kalibrierung (05.11.2026, 545 Heiz- und ~2200 Ruheintervalle
aus 01.10.-05.11.2026): ``SpeicherParameter.gemessen_oktober_2026()``.

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
    #: Waerme der WP gleichmaessig auf die Schichten VERTEILEN (WP-Waerme-
    #: tauscher durchmischt den Speicher).
    #:
    #: Gemessen im Januar-Log waehrend des Heizens: unten 11,8 -> 42,4 C,
    #: mitte 17,4 -> 42,9 C, oben 40,9 -> 43,3 C. Die Waerme geht also
    #: nach UNTEN und MITTE; die obere Schicht wird nur ueber die
    #: Konvektion warm. Deshalb bekommen ``unten`` und ``mitte`` die
    #: Waerme anteilig nach Volumen, die obere Schicht nur ueber
    #: ``einspeisung_anteil_oben`` (Standard 0).
    waerme_verteilt: bool = False
    #: Anteil der WP-Leistung, der direkt in die OBERE Schicht fliesst
    #: (nur wirksam bei ``waerme_verteilt``).
    einspeisung_anteil_oben: float = 0.0
    #: Temperatur des Kaltwassers beim Zapfen (Grad C).
    kaltwasser_c: float = 10.0
    #: Umgebungstemperatur des Speichers (Grad C).
    umgebung_c: float = 20.0
    #: Umgebungstemperatur der oberen Schicht (Grad C). None = dieselbe
    #: wie ``umgebung_c``.
    #:
    #: Der obere Fuehler sitzt nicht im Keller, sondern im Aufstellraum: der
    #: Deckel des Speichers ist die Waermeverlustflaeche, die an die
    #: Raumluft geht. Der gemessene Winterverlauf zeigt genau das: der
    #: obere Fuehler verliert ueber Nacht nur 1 K (44,6 -> 43,6 C),
    #: waehrend der untere 17 K verliert (30,6 -> 13,8 C). Mit einer
    #: gemeinsamen Umgebung fuer alle Schichten kuehlt der obere Knoten
    #: viel zu stark ab - dann loest der Notfallschutz nachts aus, obwohl
    #: das in der Realitaet nie passiert.
    umgebung_oben_c: Optional[float] = None
    #: Multiplikator des Waermeverlustkoeffizienten der oberen Schicht.
    #: Der Deckel ist gut gedammt (Wert < 1 verringert den Verlust).
    verlust_oben_anteil: float = 1.0
    #: Zustandsabhaengigkeit der Heizleistung (W pro Kelvin).
    #:
    #: BEFUND 05.11.2026 (Parameter-Fit gegen 545 Heizintervalle): Mit
    #: KONSTANTER Leistung kann das Modell die Messung nicht treffen. Die
    #: zugefuehrte Waerme betraegt bei durchmischtem Speicher (delta <= 4 K,
    #: unten ~44 C) rund 980 Wh/h, bei stark geschichtetem Speicher
    #: (delta > 16 K, unten ~25 C) dagegen rund 1950 Wh/h - die WP gibt bei
    #: kaltem Ruecklauf fast die doppelte Leistung ab. Deshalb:
    #:
    #:     P = heizleistung_w + leistung_pro_k * (leistung_referenz_c - T_unten)
    #:
    #: 0.0 = altes Verhalten (konstante Leistung, rueckwaertskompatibel).
    leistung_pro_k: float = 0.0
    #: Bezugstemperatur der unteren Schicht fuer ``leistung_pro_k`` (Grad C).
    leistung_referenz_c: float = 40.0
    #: Zustandsabhaengige Waermeverteilung (Anteil nach unten pro Kelvin).
    #:
    #: BEFUND 05.11.2026 (zweiter Fit-Durchgang): Mit einem KONSTANTEN
    #: ``einspeisung_anteil_unten`` ist beides nicht zu treffen - bei
    #: durchmischtem Speicher (delta <= 4 K) steigt `mitte` real fast so
    #: schnell wie `unten` (3.75 zu 4.75 K/h), bei delta > 8 K bleibt die
    #: Waerme unten (mitte faellt sogar). Physikalisch: liegt der
    #: Waermetauscher in Wasser mit Speichertemperatur, verteilt sich die
    #: Waerme; liegt er in kaltem Wasser, bleibt sie unten (Dichtestroemung).
    #:
    #:     anteil_unten = einspeisung_anteil_unten + verteilung_pro_k * delta
    #:
    #: 0.0 = altes Verhalten (konstanter Anteil, rueckwaertskompatibel).
    verteilung_pro_k: float = 0.0

    def kopie(self) -> "SpeicherParameter":
        return replace(self)

    @classmethod
    def gemessen_oktober_2026(cls) -> "SpeicherParameter":
        """Parameter aus dem Fit gegen die Betriebsdaten (01.10.-05.11.2026).

        Datenbasis: 3.388 Viertelstunden-Intervalle, davon 545 mit
        durchlaufendem Kompressor (Heizphase) und ~2.200 Ruheintervalle
        (Kompressor aus, ohne Zapfung). Gefittet in zwei Stufen:

          * Heizphase (8 Parameter): Leistung, Waermeverteilung,
            Konvektion, zustandsabhaengige Leistung.
            Guete R2 = 0.93 ueber 6 Schichtungs-Bins x 3 Knoten.
          * Ruhephase (5 Parameter): Verluste und Umgebung.
            Fehler 3.8 (K/h)^2, getroffen in allen Temperaturlagen.

        Zwei BEFUNDE, die das Modell dafuer brauchte:

        1. Die Heizleistung ist NICHT konstant: bei kaltem Ruecklauf
           (unten 25 C) gibt die WP ~1950 Wh/h ab, bei durchmischtem
           Speicher (unten 44 C) nur ~980 Wh/h. Daher ``leistung_pro_k``.
        2. Die Waermeverteilung haengt vom Schichtungsabstand ab: bei
           delta <= 4 K steigt `mitte` fast so schnell wie `unten`, bei
           delta > 8 K bleibt die Waerme unten (`mitte` faellt). Daher
           ``verteilung_pro_k``.

        Diese Werte sind GEMESSEN, nicht abgeleitet - sie ersetzen die
        Default-Annahmen fuer diesen Speicher. Die Defaults bleiben
        unveraendert, damit andere Szenarien vergleichbar bleiben.
        """
        return cls(
            volumen_l=300.0,
            heizleistung_w=1227.9,
            leistung_pro_k=41.5,
            leistung_referenz_c=40.0,
            einspeisung_anteil_unten=0.42,
            verteilung_pro_k=0.09,
            konvektion_an_w_k=9.2,
            konvektion_oben_an_w_k=1.0,
            konvektion_aus_w_k=0.0,
            konvektion_oben_aus_w_k=1.68,
            verlust_w_k=2.93,
            verlust_oben_anteil=0.23,
            umgebung_c=20.1,
            waerme_verteilt=False,
        )


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
        # Die Leistung haengt vom Ruecklauf ab: je kaeltter die untere
        # Schicht, desto groesser die Spreizung am Kondensator und desto
        # mehr Waerme gibt die WP ab (leistung_pro_k, Default 0 = konstant).
        if kompressor_ein:
            leistung = p.heizleistung_w + p.leistung_pro_k * (
                p.leistung_referenz_c - self.unten.temperatur
            )
            q_in = max(0.0, leistung) * h
        else:
            q_in = 0.0
        if p.waerme_verteilt:
            # Der Waermetauscher der WP sitzt im unteren Bereich: die
            # Waerme geht nach unten und mitte (anteilig nach Volumen),
            # die obere Schicht wird nur ueber die Konvektion warm.
            # Gemessen im Januar-Log: unten 11,8 -> 42,4 C, mitte
            # 17,4 -> 42,9 C, oben 40,9 -> 43,3 C.
            anteil_oben = min(max(p.einspeisung_anteil_oben, 0.0), 1.0)
            dq_oben = q_in * anteil_oben
            rest = q_in - dq_oben
            vol_um = self.unten.volumen_l + self.mitte.volumen_l
            dq_unten = rest * (self.unten.volumen_l / vol_um)
            dq_mitte = rest * (self.mitte.volumen_l / vol_um)
        else:
            anteil = min(max(p.einspeisung_anteil_unten, 0.0), 1.0)
            if p.verteilung_pro_k:
                delta_um = self.mitte.temperatur - self.unten.temperatur
                anteil = min(1.0, max(0.0, anteil + p.verteilung_pro_k * delta_um))
            dq_unten = q_in * anteil
            dq_mitte = q_in * (1.0 - anteil)
            dq_oben = 0.0

        # --- Waermeverluste an die Umgebung (Wh, negativ) ---
        # Jede Schicht hat ihre eigene Umgebung: unten und mitte stehen
        # im Keller, oben sitzt der Fuehler am Deckel im Aufstellraum.
        dq_umgebung = 0.0
        umg_oben = (p.umgebung_oben_c if p.umgebung_oben_c is not None
                    else p.umgebung_c)
        for s in self.schichten:
            if s is self.oben:
                k_verlust = p.verlust_w_k * p.verlust_oben_anteil
                umgebung = umg_oben
            else:
                k_verlust = p.verlust_w_k
                umgebung = p.umgebung_c
            verlust = k_verlust * (s.temperatur - umgebung) * h
            s._verlust_wh = verlust
            dq_umgebung += verlust

        # --- Konvektion / Durchmischung (Wh) ---
        if kompressor_ein:
            k_um, k_mo = p.konvektion_an_w_k, p.konvektion_oben_an_w_k
        else:
            k_um, k_mo = p.konvektion_aus_w_k, p.konvektion_oben_aus_w_k

        q_um = k_um * (self.mitte.temperatur - self.unten.temperatur) * h
        q_mo = k_mo * (self.oben.temperatur - self.mitte.temperatur) * h

        # --- Stabilitaetsgrenze der expliziten Integration ---
        # Ein Kopplungsterm darf je Zeitschritt hoechstens einen Teil der
        # Kapazitaet der beteiligten Knoten uebertragen. Sonst kippt die
        # Rechnung: bei k=3716 W/K und 60-s-Schritt waeren 52 kWh je
        # Stunde im Spiel - der obere Knoten wird leer gepumpt und der
        # Speicher "ueberhitzt" scheinbar auf 99 C. Die Grenze haelt das
        # Modell physikalisch und numerisch stabil.
        def _begrenze(q, ks):
            grenze = 0.4 * min(k.kapazitaet_wh_k for k in ks)
            if abs(q) > grenze:
                return grenze if q > 0 else -grenze
            return q

        q_um = _begrenze(q_um, (self.unten, self.mitte))
        q_mo = _begrenze(q_mo, (self.mitte, self.oben))

        # --- Energiebilanz je Knoten (Wh) ---
        e_unten = dq_unten - self.unten._verlust_wh + q_um
        e_mitte = dq_mitte - self.mitte._verlust_wh - q_um + q_mo
        e_oben = dq_oben - self.oben._verlust_wh - q_mo

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
