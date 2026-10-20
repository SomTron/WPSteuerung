"""Stromseite der Simulation: PV, Batterie, Hauslast, Netz.

Die PV-Profile sind die real gemessenen Gleichstrom-Profile (powerdc1 +
powerdc2) aus den Betriebslogs:

* ``SEPTEMBER_PV_DC`` aus ``logs/24.9/heizungsdaten.csv`` (September 2026)
* ``DEZEMBER_PV_DC`` aus ``Analyse/merged_data.csv`` (Dezember 2025)

Wichtig: Es ist die GLEICHSTROMseite, nicht ``acpower``. ``acpower`` ist der
Wechselstromausgang des Wechselrichters (Haus + Netz) und liefert nachts
rund 230 W, obwohl die Sonne nicht scheint. Mit diesem Wert als
"PV-Erzeugung" haette die Simulation nachts Strom aus dem Nichts und das
PV-Fenster wuerde nie schliessen.

Die Winterszenarien nutzen das gemessene Dezemberprofil (Tagesspitze ~2 kW
statt ~8 kW im September) statt eines skalierten Septemberprofils.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Optional

#: Real gemessene September-PV-ERZEUGUNG (Gleichstromseite, Median je
#: Stunde, W), aus logs/24.9/heizungsdaten.csv. Nachts 0 W - die Sonne
#: scheint nicht.
SEPTEMBER_PV_DC: Dict[int, float] = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 251, 8: 1375,
    9: 3010, 10: 5302, 11: 7164, 12: 8018, 13: 8095, 14: 7972,
    15: 7548, 16: 5572, 17: 3470, 18: 569, 19: 0, 20: 0, 21: 0,
    22: 0, 23: 0,
}

#: Real gemessene Dezember-PV-Erzeugung (Gleichstromseite, Median je
#: Stunde, W), aus Analyse/merged_data.csv. Kuerzerer Tag, flacherer
#: Verlauf, rund ein Sechstel der September-Erzeugung.
DEZEMBER_PV_DC: Dict[int, float] = {
    0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0, 7: 0, 8: 189,
    9: 741, 10: 1698, 11: 2066, 12: 1928, 13: 2002, 14: 653,
    15: 221, 16: 0, 17: 0, 18: 0, 19: 0, 20: 0, 21: 0, 22: 0,
    23: 0,
}

#: Verfuegbare Tagesprofile je Szenario.
PV_PROFILE: Dict[str, Dict[int, float]] = {
    "september": SEPTEMBER_PV_DC,
    "dezember": DEZEMBER_PV_DC,
}

#: Real gemessene Tagesprognose im September (Wh/m²).
SEPTEMBER_FORECAST_WH = 5000.0

#: Hauslast bei ausgeschalteter WP (Median je Stunde aus logs/24.9,
#: Kompressor AUS): hausverbrauch = acpower - feedinpower. Nachts konstant
#: ~200-240 W, tagsueber ~200-300 W, am spaeten Abend die
#: Koch-/Badspitze. Diese Werte bestimmen, wann die PV den Hausverbrauch
#: deckt - und damit das Entladefenster der Batterie.
HAUSLAST_W: Dict[int, float] = {
    0: 229, 1: 195, 2: 196, 3: 233, 4: 194, 5: 195, 6: 235, 7: 218,
    8: 262, 9: 274, 10: 302, 11: 254, 12: 254, 13: 285, 14: 208,
    15: 200, 16: 226, 17: 233, 18: 235, 19: 329, 20: 297, 21: 314,
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
    #: Anteil des Nachtdefizits, den die Batterie deckt; der Rest kommt aus
    #: dem Netz. Gemessen im September-Log (Median je Stunde): die Batterie
    #: entlaedt nachts rund 190 W bei ~230 W Hauslast, das Netz liefert nur
    #: ~27 W. Der Anteil liegt damit bei ~0.8 - mit 0.0 (= kein Anteil)
    #: ueberschaetzt die Simulation den Netzbezug nachts um rund 5 kWh/Tag.
    nacht_entladung_anteil: float = 0.8
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
    * Nachts entlaedt die Batterie anteilig (ca. 80 %), der Rest kommt
      aus dem Netz.

    Die Bilanz ist geschlossen: was die PV hergibt, wird geladen,
    eingespeist, von der Batterie geliefert oder aus dem Netz bezogen -
    nichts geht verloren und nichts entsteht.
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
        # Nachts (ohne PV) entlaedt die Batterie nur anteilig - der Rest
        # kommt aus dem Netz. Wichtig: der Anteil bezieht sich auf das
        # ENTLADEN, nicht auf den Netzbezug. Vorher wurde der Netzanteil
        # reduziert und die Batterie gar nicht entladen - so ging
        # nachts Energie verloren (das Defizit war weder gedeckt noch
        # bezogen).
        if pv_w < p.pv_schwelle_w:
            entl_budget = fehlend * p.nacht_entladung_anteil
        else:
            entl_budget = fehlend
        verfuegbar_wh = max(0.0, (self.soc - p.soc_min_prozent) / 100.0 * p.kapazitaet_wh)
        entladen_wh = min(min(p.max_entladung_w, entl_budget) * dt_h, verfuegbar_wh / p.eta)
        self.soc = max(p.soc_min_prozent, self.soc - entladen_wh / p.kapazitaet_wh * 100.0)
        entladung_w = entladen_wh / dt_h
        # Der Rest des Defizits kommt aus dem Netz: Energie geht weder
        # verloren noch entsteht mehr.
        rest = max(0.0, fehlend - entladung_w)
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
    #: PV-Skalierung gegenueber dem gewaehlten Tagesprofil (1.0 = Original).
    pv_faktor: float = 1.0
    #: Name des gemessenen PV-Tagesprofils (siehe ``PV_PROFILE``):
    #: "september" oder "dezember".
    pv_profil: str = "september"
    #: Staerke der zufaelligen Wolkenmodulation (0 = aus).
    pv_streuung: float = 0.0
    #: Tagesprognose heute/morgen/uebermorgen in Wh/m².
    #:
    #: ACHTUNG Einheit: Die Regelung liest die Prognose ueber
    #: ``normalize_forecast_wh_qm`` und erwartet - wie Open-Meteo - kWh/m².
    #: ``state.solar.forecast_today`` muss deshalb in kWh/m² gesetzt
    #: werden (siehe ``Harness._solar_setzen``). Sonst interpretiert die
    #: Regelung 700 Wh/m² als 700 kWh/m² und rechnet mit dem Faktor 1000
    #: falsch: die Prognose wirkt dann um drei Größenordnungen zu
    #: pessimistisch, das PV-Fenster schließt nie und die
    #: Forecast-Kalibrierung klemmt am unteren Wert.
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
    #: Anteil des Nachtdefizits, den die Batterie deckt (Realbetrieb: ~0.8,
    #: gemessen im September-Log). Der Rest kommt aus dem Netz.
    nacht_entladung_anteil: float = 0.8
    #: Umgebungstemperatur des Speichers (Grad C) - treibt die Verluste.
    umgebung_c: float = 20.0
    #: Elektrische WP-Leistungsaufnahme (W) WAEHREND DES HEIZENS.
    #:
    #: Der Wert ist stark saisonabhaengig - aber NICHT, weil der COP
    #: einbricht: die WP steht im Keller, dessen Temperatur unterscheidet
    #: sich zwischen Winter und Sommer kaum. Gemessen (Hauslast
    #: acpower-feedin, Median je Stunde):
    #:
    #: * September (logs/24.9): ~480 W Zusatzverbrauch gegenueber dem
    #:   Hausverbrauch mit ausgeschalteter WP.
    #: * Dezember/Januar (Analyse/merged_data.csv): ~2,1-2,5 kW waehrend
    #:   der Heizstunden (11-16 Uhr). Die Energiebilanz dieser Datei
    #:   geht auf (Rest 0,1-0,9 % der DC-Erzeugung).
    #:
    #: Die thermische Leistung auf den Speicher ist in beiden Monaten
    #: praktisch gleich (1,3 kW im September, 1,6 kW im Dezember), die
    #: elektrische dagegen um den Faktor 4,4 verschieden. Bei gleichem
    #: Keller kann das kein COP-Effekt sein. WAS die winterliche
    #: Zusatzlast ist (Raeume, separate Heizung, Maschine mit
    #: Heizkreis) laesst sich aus dem Log nicht trennen - fuer die
    #: Regelung ist es aber auch egal:
    #:
    #: Entscheidend ist, dass sie den **PV-Ueberschuss aufzehrt**. Gemessen
    #: (DC-Erzeugung minus Hauslast, 9-16 Uhr): Im Dezember ist in 69 %
    #: dieser Stunden kein Ueberschuss ueber 500 W vorhanden (Median 0 W),
    #: im Januar in 62 % (Median 75 W), im September dagegen in 92 %
    #: (Median 5,6 kW). Die PV-Regeln (Abweichung, CalcStart, Batterie,
    #: AdaptivePV) gaten aber genau an diesem Ueberschuss - im Winter
    #: haben sie also meist nichts, worauf sie reagieren koennen.
    #:
    #: Mit dem September-Wert unterschetzte die Simulation den
    #: Winter-Netzbezug um rund 8 kWh/Tag.
    wp_leistung_w: float = 500.0
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
#:
#: Die PV-Erzeugung kommt aus den gemessenen Gleichstromprofilen
#: (``PV_PROFILE``). ``pv_faktor`` skaliert das jeweilige Profil:
#: der Dezember-Median (9498 Wh/Tag) entspricht ~950 Wh/m² bei 10 m².
#: "wenig PV" ist deshalb ein klarer Wetter-Tag mit weniger Strahlung
#: (Faktor 0.7), nicht ein skaliertes Septemberprofil.
SZENARIEN: Dict[str, Szenario] = {
    "september": Szenario(
        name="September (PV-reich, echte Profile)",
        start=date(2026, 9, 8), tage=7, pv_faktor=1.0, pv_streuung=0.12,
        pv_profil="september",
        forecast_wh=5000.0, hauslast_faktor=1.0, umgebung_c=20.0,
        wp_leistung_w=500.0,
    ),
    "winter_wenig_pv": Szenario(
        name="Dezember, wenig PV (trueb, 700 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=0.7, pv_streuung=0.35,
        pv_profil="dezember",
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
        wp_leistung_w=2100.0,
    ),
    "winter_batterie_leer": Szenario(
        name=("Dezember, wenig PV, Batterie wird NIE voll "
              "(Deckel 55 %, Start 20 %)"),
        start=date(2026, 12, 7), tage=7, pv_faktor=0.7, pv_streuung=0.35,
        pv_profil="dezember",
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
        soc_start_prozent=20.0, soc_max_prozent=55.0,
        wp_leistung_w=2100.0,
    ),
    "winter_batterie_voll": Szenario(
        name=("Dezember, wenig PV, Batterie kurzfristig VOLL "
              "(100 %, Kapazitaet klein)"),
        start=date(2026, 12, 7), tage=7, pv_faktor=0.7, pv_streuung=0.35,
        pv_profil="dezember",
        forecast_wh=700.0, hauslast_faktor=1.15, umgebung_c=12.0,
        soc_start_prozent=100.0, batterie_kapazitaet_wh=3000.0,
        soc_max_prozent=100.0,
        wp_leistung_w=2100.0,
    ),
    "winter_ohne_pv": Szenario(
        name="Dezember, kein PV (Starknebel, 120 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=0.0, pv_streuung=0.0,
        pv_profil="dezember",
        forecast_wh=120.0, hauslast_faktor=1.15, umgebung_c=10.0,
        wp_leistung_w=2100.0,
    ),
    "winter_ohne_pv_lang": Szenario(
        name="Dezember, 5 Wochen ohne PV (bestehender Betrieb)",
        start=date(2026, 12, 1), tage=35, pv_faktor=0.0, pv_streuung=0.0,
        pv_profil="dezember",
        forecast_wh=120.0, hauslast_faktor=1.15, umgebung_c=10.0,
        legionellen_vor_tagen=10,
        wp_leistung_w=2100.0,
    ),
    "winter_misch": Szenario(
        name="Dezember, wechselnd (1600 Wh/m2)",
        start=date(2026, 12, 7), tage=7, pv_faktor=1.6, pv_streuung=0.5,
        pv_profil="dezember",
        forecast_wh=1600.0, hauslast_faktor=1.15, umgebung_c=12.0,
        wp_leistung_w=2100.0,
    ),
}
