"""
JSON-basierte Konfiguration für die Pareto-optimierte WP-Steuerung.

Lädt Parameter aus wp_steuerung_parameter.json und stellt sie
als typsichere Pydantic-Modelle bereit.
"""

import json
import logging
import os
from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class WPConfig(BaseModel):
    """Wärmepumpen-Grundkonfiguration."""
    leistung_watt: int = Field(default=600, description="Nennleistung der WP in Watt")
    typ: str = Field(default="binaer_ein_aus", description="Steuerungstyp: binaer_ein_aus oder modulierend")


class ZyklusConfig(BaseModel):
    """Zeitliche Zyklen."""
    interval_minuten: int = Field(default=15, description="Hauptloop-Intervall in Minuten")
    mindestlaufzeit_minuten: int = Field(default=60, description="Minimale Kompressor-Laufzeit in Minuten")
    mindestpausenzeit_minuten: int = Field(default=30, description="Minimale Pause zwischen Einschaltungen in Minuten")


class SicherheitConfig(BaseModel):
    """Sicherheitsparameter."""
    nachtsperre_start: int = Field(default=19, description="Nachtsperre Start-Stunde (0-23)")
    nachtsperre_ende: int = Field(default=8, description="Nachtsperre Ende-Stunde (0-23)")
    max_temp_c: float = Field(default=48.0, description="Maximale Boiler-Temperatur (°C)")
    ueberhitzung_c: float = Field(default=58.0, description="Überhitzungsschutz (°C)")
    notfall_c: float = Field(default=36.0, description="Notfall-Einschalttemperatur (°C)")

    boiler_max_fuehler: str = Field(default="unten", description="Bezugsfuehler fuer das Boiler-Maximum (unten/mittig/oben)")
    boiler_max_hysterese_k: float = Field(default=2.0, description="Nach einem Maximum-Abschalten erst wieder einschalten, wenn der Bezugsfuehler <= max_temp_c - Hysterese ist")

    @model_validator(mode="after")
    def _pruefe_sicherheit(self):
        if self.boiler_max_fuehler not in ("unten", "mittig", "oben"):
            raise ValueError(
                f"boiler_max_fuehler muss 'unten', 'mittig' oder 'oben' sein, "
                f"nicht '{self.boiler_max_fuehler}'"
            )
        if self.boiler_max_hysterese_k < 0:
            raise ValueError("boiler_max_hysterese_k darf nicht negativ sein")
        if not (20.0 <= self.max_temp_c <= 70.0):
            raise ValueError(f"max_temp_c={self.max_temp_c} ausserhalb des plausiblen Bereichs (20-70 C)")
        if self.max_temp_c > self.ueberhitzung_c:
            raise ValueError(
                f"max_temp_c ({self.max_temp_c}) darf den Ueberhitzungsschutz "
                f"({self.ueberhitzung_c}) nicht uebersteigen"
            )
        return self

class WochenendeConfig(BaseModel):
    """Wochenende-Einstellungen."""
    aktiv: bool = Field(default=True, description="Wochenendmodus aktiv")
    fruehestens_uhr: int = Field(default=9, description="Frühester Einschaltzeitpunkt am Wochenende")
    prioritaet: int = Field(default=100, description="Prioritaet der Sperre (blockierend, hoechste)")


class PVRegel(BaseModel):
    """Eine einzelne PV-Steuerungsregel."""
    name: str = Field(default="PV Regel", description="Eindeutiger Name der Regel")
    prioritaet: int = Field(default=80, description="Priorität (hoeher = wichtiger)")
    pv_schwelle_watt: float = Field(default=200.0, description="Minimale PV-Leistung zum Einschalten (W)")
    temperaturfuehler: str = Field(default="mitte", description="Welcher Fühler: oben/mitte/unten")
    einschalten_bei_c: float = Field(default=40.0, description="Einschalten bei Temperatur <= (°C)")
    ausschalten_bei_c: float = Field(default=45.0, description="Ausschalten bei Temperatur >= (°C)")
    weiterlaufen_ab_pv_watt: float = Field(default=50.0, description="Weiterlaufen ab PV-Leistung (W)")


    @model_validator(mode="after")
    def _plausibel(self):
        """Einschaltpunkt muss unter dem Ausschaltpunkt liegen."""
        if self.einschalten_bei_c >= self.ausschalten_bei_c:
            raise ValueError(
                f"pv_regel '{self.name}': einschalten_bei_c ({self.einschalten_bei_c}) "
                f"muss kleiner als ausschalten_bei_c ({self.ausschalten_bei_c}) sein"
            )
        if self.pv_schwelle_watt < 0:
            raise ValueError("pv_schwelle_watt darf nicht negativ sein")
        return self


class KomfortConfig(BaseModel):
    """Komfort-Regel: Hält Mindesttemperatur."""
    prioritaet: int = Field(default=60, description="Priorität")
    notfall_einschalten_bei_c: float = Field(default=36.0, description="Notfall: Einschalten bei (°C)")
    komfort_einschalten_bei_c: float = Field(default=38.0, description="Komfort: Einschalten bei (°C)")
    ausschalten_bei_c: float = Field(default=42.0, description="Ausschalten bei (°C)")
    min_pv_fuer_komfort_watt: float = Field(default=50.0, description="Minimale PV für Komfort-Heizen (W)")


class MindestTempEintrag(BaseModel):
    """Eine Mindest-Temperatur-Garantie fuer einen Fuehler in einem Zeitfenster."""
    name: str = Field(default="Eintrag", description="Anzeigename (z.B. 'Mittag-Oben')")
    temperaturfuehler: str = Field(default="oben", description="'oben', 'mitte' oder 'unten'")
    min_temp_c: float = Field(default=40.0, description="Mindesttemperatur (°C)")
    start_uhr: int = Field(default=11, description="Fenster-Start (Stunde, 0-23)")
    ende_uhr: int = Field(default=16, description="Fenster-Ende (Stunde, exklusiv)")
    hysterese_k: float = Field(default=2.0, description="Ausschalten erst bei min_temp_c + K")
    fenster_aus_lernen: bool = Field(
        default=False,
        description=("Zeitfenster dynamisch aus dem gelernten Abend-Zapfverhalten "
                     "anpassen (max. 2h frueher Start, max. 1h spaeteres Ende)"),
    )
    nachtsperre_ueberschreiben: bool = Field(
        default=True,
        description=("True: Garantie feuert auch innerhalb der Nachtsperre "
                     "(alter Komfort-Vorrang). False: Regel bleibt waehrend der "
                     "Nachtsperre stumm - die Garantie gilt nur BIS zum Sperren-"
                     "Beginn, danach wird nicht mehr nachgeheizt (kein Netzstrom "
                     "in der Nacht, z.B. nach dem Abend-Duschen)."),
    )


    @model_validator(mode="after")
    def _plausibel(self):
        """Fenster und Temperaturgrenzen muess Sinn ergeben."""
        if self.ende_uhr <= self.start_uhr:
            raise ValueError(
                f"mindest_temp '{self.name}': ende_uhr ({self.ende_uhr}) muss "
                f"groesser als start_uhr ({self.start_uhr}) sein"
            )
        if not (20.0 <= self.min_temp_c <= 55.0):
            raise ValueError(f"mindest_temp '{self.name}': min_temp_c ausserhalb 20-55 C")
        if self.hysterese_k < 0.5:
            raise ValueError(f"mindest_temp '{self.name}': hysterese_k < 0.5 sinnlos")
        return self


class MindestTempConfig(BaseModel):
    """Mindest-Temperatur-Garantien: Der Boiler darf zu definierten Zeiten
    nicht zu kalt sein (Komfort-Vorrang vor Sparziele), auch waehrend der
    Nachtsperre - innerhalb der konfigurierten Fenster."""
    aktiv: bool = Field(default=True, description="Regel aktiv")
    prioritaet: int = Field(default=65, description="Priorität (ueber Komfort, unter PV)")
    eintraege: List[MindestTempEintrag] = Field(
        default_factory=lambda: [
            MindestTempEintrag(name="Frueh-Mitte", temperaturfuehler="mitte",
                               min_temp_c=38.0, start_uhr=6, ende_uhr=8,
                               fenster_aus_lernen=True),
            MindestTempEintrag(name="Mittag-Oben", temperaturfuehler="oben",
                               min_temp_c=40.0, start_uhr=11, ende_uhr=16),
            MindestTempEintrag(name="Abend-Mitte", temperaturfuehler="mitte",
                               min_temp_c=40.0, start_uhr=17, ende_uhr=22),
        ],
        description="Garantierte Mindesttemperaturen",
    )


class BatterieConfig(BaseModel):
    """Batterie-Regel: Heizen mit Hausbatterie-Strom, solange die Batterie
    genug geladen ist und das Haus keinen Strom aus dem Netz bezieht.
    Priorisierung: PV-Direkt > Batterie > Netz."""
    aktiv: bool = Field(default=True, description="Regel aktiv")
    prioritaet: int = Field(default=75, description="Priorität (unter den PV-Regeln)")
    temperaturfuehler: str = Field(default="unten", description="Regelfühler")
    einschalten_bei_c: float = Field(default=42.0, description="Einschalten bei (°C)")
    ausschalten_bei_c: float = Field(default=47.0, description="Ausschalten bei (°C)")
    min_soc_prozent: float = Field(default=90.0, description="Batterie mind. so voll (%)")
    max_netzbezug_watt: float = Field(
        default=-50.0,
        description="Heizen nur wenn Einspeisung >= Wert (W); <0 = kleiner Netzkauftoleranz",
    )
    soc_hysterese_prozent: float = Field(
        default=2.0,
        description=(
            "SOC-Hysterese gegen Flattern an der Grenzkante: Waehrend der "
            "Kompressor laeuft, genuegt (min_soc_prozent - Hysterese) zum "
            "Weiterlauf; ein 1%-SOC-Ticken schaltet nicht mehr ab."
        ),
    )

    # Dynamische Reserve (Punkt C): Bei gutem Morgen-Forecast darf die
    # Batterie tiefer entladen werden als min_soc_prozent.
    entlastung_max_prozent: float = Field(
        default=15.0,
        description="Maximale Absenkung der SOC-Reserve bei Top-Forecast (%-Punkte)",
    )
    min_soc_absolut: float = Field(
        default=10.0,
        description="Harte Untergrenze der dynamischen SOC-Reserve (%)",
    )

    # Dynamische Reserve (Punkt C): Bei gutem Morgen-Forecast darf die
    # Batterie tiefer entladen werden als min_soc_prozent.
    entlastung_max_prozent: float = Field(
        default=15.0,
        description="Maximale Absenkung der SOC-Reserve bei Top-Forecast (%-Punkte)",
    )
    min_soc_absolut: float = Field(
        default=10.0,
        description="Harte Untergrenze der dynamischen SOC-Reserve (%)",
    )


    @model_validator(mode="after")
    def _plausibel(self):
        """SOC-Grenzen und Temperatur-Hysterese pruefen."""
        if self.einschalten_bei_c >= self.ausschalten_bei_c:
            raise ValueError("batterie: einschalten_bei_c < ausschalten_bei_c erforderlich")
        if not (0.0 <= self.min_soc_prozent <= 100.0):
            raise ValueError("batterie: min_soc_prozent ausserhalb 0-100")
        return self


class EinspeisungConfig(BaseModel):
    """Einspeise-Begrenzungs-Regel (PV-Shaping): Nutzt Ueberschuss, der sonst
    (gegen das Netzlimit von z.B. 7500W) eingespeist wuerde.

    Einschalten sobald die Einspeisung die Grenze erreicht; der Kompressor
    laeuft dann weiter (mit Abschlag, da er selbst ~600W zieht), solange die
    Einspeisung ueber weiterlauf_ab_watt bleibt."""
    aktiv: bool = Field(default=True, description="Regel aktiv")
    prioritaet: int = Field(default=83, description="Prioritaet (ueber CalcStart/Batterie)")
    einspeisegrenze_watt: float = Field(
        default=7500.0, description="Einspeisen ab so viel Watt -> Heizung an (Netzlimit)",
    )
    weiterlauf_ab_watt: float = Field(
        default=6500.0, description="Weiterlaufen solange Einspeisung >= Wert (WP zieht ~600W)",
    )
    temperaturfuehler: str = Field(default="unten", description="Regelfühler")
    ausschalten_bei_c: float = Field(default=48.0, description="Ausschalten bei (°C)")


    @model_validator(mode="after")
    def _plausibel(self):
        """Weiterlauf-Abschlag muss unter der Einschalt-Grenze liegen."""
        if self.weiterlauf_ab_watt > self.einspeisegrenze_watt:
            raise ValueError(
                "einspeisung: weiterlauf_ab_watt > einspeisegrenze_watt "
                "(Weiterlauf wuerde nie greifen)"
            )
        if not (30.0 <= self.ausschalten_bei_c <= 50.0):
            raise ValueError("einspeisung: ausschalten_bei_c ausserhalb 30-50 C")
        return self


class ZeitfensterConfig(BaseModel):
    """Zeitfenster-Regel: Heizt zu bestimmten Uhrzeiten."""
    aktiv: bool = Field(default=True, description="Regel aktiv")
    prioritaet: int = Field(default=53, description="Priorität")
    start_uhr: int = Field(default=6, description="Start-Stunde (0-23)")
    ende_uhr: int = Field(default=16, description="Ende-Stunde (0-23)")
    modus: str = Field(default="einschalten", description="Modus: einschalten oder ausschalten")
    temperaturfuehler: str = Field(default="mitte", description="Welcher Fühler: oben/mitte/unten")
    max_temp_fuer_einschalten_c: float = Field(default=50.0, description="Einschalten wenn Temp <= (°C)")
    min_pv_watt: float = Field(default=0.0, description="Minimale PV-Leistung (W)")


class AbweichungConfig(BaseModel):
    """Abweichungsregel: Hält Temperatur nahe am Sollwert."""
    prioritaet: int = Field(default=47, description="Priorität")
    solltemperatur_c: float = Field(default=40.0, description="Solltemperatur (°C)")
    temperaturfuehler: str = Field(default="unten", description="Welcher Fühler: oben/mitte/unten")
    einschalten_bei_abweichung_k: float = Field(default=3.0, description="Einschalten bei Abweichung >= (K)")
    ausschalten_bei_abweichung_k: float = Field(default=0.5, description="Ausschalten bei Abweichung <= (K)")
    schichtung_min_oben_c: float = Field(default=42.0, description="2-Zonen-Schichtungs-Check: Nicht einschalten wenn oben >= (°C), vermeidet Netzstrom-Start bei Zapfen")




    @model_validator(mode="after")
    def _plausibel(self):
        """AUS-Schwelle muss unter der EIN-Schwelle liegen (Hysterese)."""
        if self.ausschalten_bei_abweichung_k >= self.einschalten_bei_abweichung_k:
            raise ValueError(
                "abweichung: ausschalten_bei_abweichung_k muss kleiner als "
                "einschalten_bei_abweichung_k sein"
            )
        if not (20.0 <= self.solltemperatur_c <= 55.0):
            raise ValueError("abweichung: solltemperatur_c ausserhalb 20-55 C")
        return self


class ForecastConfig(BaseModel):
    """Prognose-Regel: Vorheizen bei schlechter Solar-Prognose, sparen bei guter."""
    prioritaet: int = Field(default=57, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    temperaturfuehler: str = Field(default="mitte", description="Welcher Fuehler: oben/mitte/unten")
    fc_schwelle_hoch_wh: float = Field(default=3000.0, description="Prognose ueber Wert = guter Solartag (Wh/qm)")
    fc_schwelle_niedrig_wh: float = Field(default=800.0, description="Prognose unter Wert = schlechter Solartag (Wh/qm)")
    t_vorheiz_ab_c: float = Field(default=44.0, description="Vorheizen wenn Temp kleiner gleich (Grad C)")
    tmax_c: float = Field(default=48.0, description="Maximale Vorheiztemperatur (Grad C)")
    vorheiz_start_uhr: int = Field(default=8, description="Vorheiz-Fenster Start-Stunde")
    vorheiz_ende_uhr: int = Field(default=19, description="Vorheiz-Fenster Ende-Stunde")
    sparen_start_uhr: int = Field(default=11, description="Spar-Fenster Start-Stunde")
    sparen_ende_uhr: int = Field(default=15, description="Spar-Fenster Ende-Stunde")


    @model_validator(mode="after")
    def _plausibel(self):
        """Schlecht-Schwelle muss unter der Gut-Schwelle liegen."""
        if self.fc_schwelle_niedrig_wh >= self.fc_schwelle_hoch_wh:
            raise ValueError(
                f"forecast: fc_schwelle_niedrig_wh ({self.fc_schwelle_niedrig_wh}) "
                f"muss kleiner als fc_schwelle_hoch_wh ({self.fc_schwelle_hoch_wh}) sein"
            )
        return self


class AdaptivePVConfig(BaseModel):
    """Adaptive-PV-Regel: PV-Schwelle passt sich an Temperatur und Prognose an."""
    prioritaet: int = Field(default=55, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    base_threshold_watt: float = Field(default=300.0, description="Basis-PV-Schwelle (W)")
    temperaturfuehler: str = Field(default="unten", description="Fuehler: oben/mitte/unten")
    tmax_c: float = Field(default=48.0, description="Maximale Temperatur (Grad C)")
    t_aggressiv_kalt_c: float = Field(default=35.0, description="Schwelle x0.5 wenn Temp unter Wert (Grad C)")
    t_normal_kalt_c: float = Field(default=38.0, description="Schwelle x0.7 wenn Temp unter Wert (Grad C)")
    fc_schwelle_gut_wh: float = Field(default=4000.0, description="Prognose >= Wert (Wh/qm): Schwelle x1.5 (konservativer)")
    fc_schwelle_schlecht_wh: float = Field(default=1000.0, description="Prognose <= Wert (Wh/qm): Schwelle x0.5 (PV jetzt nutzen)")


class CalculatedStartConfig(BaseModel):
    """Startzeit-Regel: Berechnet optimalen Einschaltzeitpunkt fuer Zieltemperatur."""
    prioritaet: int = Field(default=82, description="Prioritaet")
    aktiv: bool = Field(default=True, description="Regel aktiv")
    solltemperatur_c: float = Field(default=44.0, description="Zieltemperatur (Grad C)")
    target_uhr: int = Field(default=17, description="Zielzeit (Stunde) - typische Zapfzeit")
    heizrate_unten_c_h: float = Field(default=3.0, description="Geschaetzte Heizrate unten (Grad C/h)")
    heizrate_gesamt_c_h: float = Field(default=2.0, description="Geschaetzte Heizrate gesamt (Grad C/h)")
    tmax_c: float = Field(default=48.0, description="Maximale Temperatur (Grad C)")


    @model_validator(mode="after")
    def _plausibel(self):
        """Zielzeit im gueltigen Bereich, Ziel unter Sicherheitslimit."""
        if not (0 <= self.target_uhr <= 23):
            raise ValueError("calculated_start: target_uhr ausserhalb 0-23")
        if not (20.0 <= self.solltemperatur_c <= 55.0):
            raise ValueError("calculated_start: solltemperatur_c ausserhalb 20-55 C")
        if self.tmax_c <= self.solltemperatur_c:
            raise ValueError("calculated_start: tmax_c muss ueber solltemperatur_c liegen")
        return self


class BademodusConfig(BaseModel):
    """Bademodus: Erhoeht die Zieltemperatur fuer heisses Brauchwasser."""
    solltemperatur_erhoehung_c: float = Field(default=3.0, description="Zieltemperatur-Erhoehung im Bademodus (K)")


class SommerModusConfig(BaseModel):
    """Sommer-Modus: Reduziert Zieltemperaturen bei mehrtaegiger guter PV-Prognose.

    Wenn fuer mehrere Tage hintereinander viel PV-Strom prognostiziert wird,
    macht es keinen Sinn, den Boiler jeden Tag auf 48°C aufzuheizen.
    Stattdessen werden die Solltemperaturen um temperatur_offset_c gesenkt,
    da ja jeden Tag genug PV-Strom zum Heizen zur Verfuegung steht.
    """
    aktiv: bool = Field(default=True, description="Sommer-Modus aktiv")
    mindest_prognose_wh: float = Field(default=2000.0, description="Mindest-Prognose pro Tag (Wh/qm)")
    benoetigte_tage: int = Field(default=3, description="Wieviele Tage hintereinander gut sein muessen")
    temperatur_offset_c: float = Field(default=-3.0, description="Temperatur-Offset (°C)")
    pv_ausschalt_offset_c: float = Field(
        default=-2.0,
        description="Zusaetzlicher Offset auf die PV-Ausschaltpunkte (Buffer nicht voll aufladen)",
    )


class KomfortVerletzungConfig(BaseModel):
    """Automatische Komfort-Erkennung (Punkt B): Faellt der oberste Fuehler
    unter grenz_c (ausserhalb der Nachtsperre), zaehlt das als Komfort-
    Verletzung und laesst das Lernen sanft nachziehen."""
    aktiv: bool = Field(default=True, description="Wachter aktiv")
    grenz_c: float = Field(default=40.0, description="Darunter gilt Warmwasser als zu kalt (C)")
    max_pro_tag: int = Field(default=3, description="Max. gezaehlte Verletzungen pro Tag")

    @model_validator(mode="after")
    def _pruefe(self):
        if not (20.0 <= self.grenz_c <= 55.0):
            raise ValueError(f"komfort_verletzung.grenz_c={self.grenz_c} ausserhalb 20-55 C")
        if self.max_pro_tag < 1:
            raise ValueError("max_pro_tag muss >= 1 sein")
        return self


class TaktschutzConfig(BaseModel):
    """Adaptiver Taktschutz (Punkt D): Schaltet der Regelwechsel zu oft hin
    und her, wird die Mindestpause zwischen zwei Starts verlaengert."""
    aktiv: bool = Field(default=True, description="Taktschutz aktiv")
    max_wechsel_pro_stunde: int = Field(default=8, description="Ab so vielen Entscheidungswechseln/h greift die Ruhephase")
    dauer_minuten: int = Field(default=120, description="Dauer der Ruhephase ab Ausloesung (Minuten)")
    zusatz_pause_minuten: int = Field(default=15, description="Zusaetzliche Mindestpause waehrend der Ruhephase")

    @model_validator(mode="after")
    def _pruefe(self):
        if self.max_wechsel_pro_stunde < 2:
            raise ValueError("max_wechsel_pro_stunde muss >= 2 sein")
        if self.dauer_minuten < 5:
            raise ValueError("dauer_minuten muss >= 5 sein")
        if self.zusatz_pause_minuten < 0:
            raise ValueError("zusatz_pause_minuten darf nicht negativ sein")
        return self


class BoilerModellConfig(BaseModel):
    """Boiler-Fuellstandsmodell (Punkt A): Schaetzt aus den drei Fuehlern,
    wie viel nutzbares Warmwasser noch im Speicher ist."""
    volumen_l: float = Field(default=150.0, description="Boiler-Gesamtvolumen (Liter)")
    nutztemp_c: float = Field(default=40.0, description="Temperatur, ab der Wasser als 'warm' zaehlt (C)")
    kaltwasser_c: float = Field(default=10.0, description="Frischwasser-/Kaltwassertemperatur (C)")

    @model_validator(mode="after")
    def _pruefe(self):
        if not (20.0 <= self.volumen_l <= 1000.0):
            raise ValueError(f"boiler_modell.volumen_l={self.volumen_l} ausserhalb 20-1000 l")
        if not (30.0 <= self.nutztemp_c <= 60.0):
            raise ValueError(f"nutztemp_c={self.nutztemp_c} ausserhalb 30-60 C")
        if not (0.0 <= self.kaltwasser_c <= 25.0):
            raise ValueError(f"kaltwasser_c={self.kaltwasser_c} ausserhalb 0-25 C")
        if self.kaltwasser_c >= self.nutztemp_c:
            raise ValueError("kaltwasser_c muss unter nutztemp_c liegen")
        return self


class KpiConfig(BaseModel):
    """Konfiguration fuer die Energiebilanz-Anzeige (Webapp-Karte 'Ernte')."""
    strompreis_eur_kwh: float = Field(default=0.35, description="Netzstrompreis (EUR/kWh)")
    wp_leistung_watt_fallback: float = Field(default=600.0, description="Fallback-WP-Leistung")


class WPSteuerungConfig(BaseModel):
    """Gesamtkonfiguration der Pareto-optimierten WP-Steuerung."""
    beschreibung: str = Field(default="WP Steuerung")
    wp: WPConfig = Field(default_factory=WPConfig)
    zyklus: ZyklusConfig = Field(default_factory=ZyklusConfig)
    sicherheit: SicherheitConfig = Field(default_factory=SicherheitConfig)
    wochenende: WochenendeConfig = Field(default_factory=WochenendeConfig)
    pv_regeln: List[PVRegel] = Field(default_factory=list)
    komfort: KomfortConfig = Field(default_factory=KomfortConfig)
    zeitfenster: ZeitfensterConfig = Field(default_factory=ZeitfensterConfig)
    abweichung: AbweichungConfig = Field(default_factory=AbweichungConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    adaptive_pv: AdaptivePVConfig = Field(default_factory=AdaptivePVConfig)
    calculated_start: CalculatedStartConfig = Field(default_factory=CalculatedStartConfig)
    sommer_modus: SommerModusConfig = Field(default_factory=SommerModusConfig)
    bademodus: BademodusConfig = Field(default_factory=BademodusConfig)
    mindest_temp: MindestTempConfig = Field(default_factory=MindestTempConfig)
    batterie: BatterieConfig = Field(default_factory=BatterieConfig)
    einspeisung: EinspeisungConfig = Field(default_factory=EinspeisungConfig)


    kpi: KpiConfig = Field(default_factory=KpiConfig)
    komfort_verletzung: KomfortVerletzungConfig = Field(default_factory=KomfortVerletzungConfig)
    taktschutz: TaktschutzConfig = Field(default_factory=TaktschutzConfig)
    boiler_modell: BoilerModellConfig = Field(default_factory=BoilerModellConfig)


class WPSteuerungConfigManager:
    """Lädt und verwaltet die JSON-Konfiguration."""

    def __init__(self, config_path: str = "wp_steuerung_parameter.json"):
            # Pfad relativ zum Verzeichnis dieser Datei (json_config.py) aufloesen
            # So wird die Config immer gefunden, egal aus welchem Ordner der Prozess startet
            _script_dir = os.path.dirname(os.path.abspath(__file__))
            self.config_path = os.path.join(_script_dir, config_path)
            self.config: WPSteuerungConfig = WPSteuerungConfig()
            self._last_mtime: Optional[float] = None

    def load_config(self) -> bool:
        """
        Lädt die JSON-Konfiguration.
        Gibt True zurück, wenn sich die Config geändert hat.
        """
        try:
            if not os.path.exists(self.config_path):
                logging.warning(f"JSON-Config '{self.config_path}' nicht gefunden. Verwende Defaults.")
                return False

            current_mtime = os.path.getmtime(self.config_path)
            if current_mtime == self._last_mtime:
                return False  # Keine Änderung

            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)

            self.config = WPSteuerungConfig(**raw)
            self._last_mtime = current_mtime
            logging.info(f"JSON-Config '{self.config_path}' geladen: {self.config.beschreibung}")
            return True

        except json.JSONDecodeError as e:
            logging.error(f"JSON-Syntaxfehler in '{self.config_path}': {e}")
            return False
        except Exception as e:
            logging.error(f"Fehler beim Laden der JSON-Config: {e}")
            return False

    def get(self) -> WPSteuerungConfig:
        """Gibt die aktuelle Konfiguration zurück."""
        return self.config

    def has_changed(self) -> bool:
        """Prüft, ob sich die Datei seit dem letzten Laden geändert hat."""
        if not os.path.exists(self.config_path):
            return False
        current_mtime = os.path.getmtime(self.config_path)
        return current_mtime != self._last_mtime

    def reload_if_changed(self) -> bool:
        """Lädt die Config neu, wenn sich die Datei geändert hat."""
        if self.has_changed():
            return self.load_config()
        return False
