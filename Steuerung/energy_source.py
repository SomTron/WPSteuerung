"""Einheitliche Klassifikation der aktuellen WP-Energiequelle.

VORZEICHEN-KONVENTION ``batPower`` (am 02.10.2026 gegen echte Messreihen
geprueft, siehe tests/test_energy_source.py):
    batPower > 0  -> die Batterie LAEDT (nimmt Solarueberschuss auf)
    batPower < 0  -> die Batterie SPEIST (entlaedt sich)
Beleg: In 11.078 Live-Logzeilen und 254.305 CSV-Zeilen liegt der Median
waehrend steigender SOC bei +1180 W bis +1335 W, waehrend fallender SOC
bei -330 W bis -355 W. Der frueher dokumentierte "Vertrag"
("+ = Entladung") war invertiert und ist korrigiert.

Die Regel-Engine verwendet ausschliesslich positive Entladeleistung als
Batterie-Signal und verlangt fuer PV/Batterie, dass gleichzeitig kein
relevanter Netzbezug gemessen wird.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Optional


class Energiequelle(str, Enum):
    PV = "PV"
    BATTERIE = "Batterie"
    NETZ = "Netz"
    KEINE = "keine Quelle"
    STALE = "Daten stale"


@dataclass(frozen=True)
class EnergiequellenStatus:
    quelle: Energiequelle
    verfuegbar: bool
    begruendung: str
    pv_acpower_watt: Optional[float] = None
    feedin_watt: Optional[float] = None
    batterie_entladung_watt: Optional[float] = None
    soc_prozent: Optional[float] = None

    @property
    def ist_erneuerbar(self) -> bool:
        return self.verfuegbar and self.quelle in (Energiequelle.PV, Energiequelle.BATTERIE)


def _finite(value: object) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def pv_erzeugung_watt(
    powerdc1: object,
    powerdc2: object,
    acpower: object,
    batpower_raw: object,
) -> Optional[float]:
    """Tatsaechliche PV-Erzeugung in Watt.

    ``ACPower`` ist NICHT die Erzeugung. Es ist der Wechselstromausgang
    des Wechselrichter - das, was ins Haus und (anteilig) ins Netz
    fliesst. Belegt an den Betriebsdaten des Pi:

        Stunde   DC (W)   AC (W)
        0-4          0    224-250     <- Nacht: keine Sonne, AC trotzdem > 0
        9         2914      352     <- 2562 W wandern in den Speicher
        12        6881     6749

    Nachts liefert die Gleichstromseite 0 W. Damit ist die Erzeugung
    eindeutig ``powerdc1 + powerdc2``.

    Fehlen die DC-Werte (aeltere Firmware), wird genaehert mit
    ``acpower + max(0, batpower)``: was der Wechselrichter abgibt, plus
    das, was zusaetzlich in den Speicher fliesst. Nachts ist
    ``batpower`` negativ oder 0, dann faellt das auf ``acpower``
    zurueck und bleibt damit weiterhin nur eine Naeherung.
    """
    dc1 = _finite(powerdc1)
    dc2 = _finite(powerdc2)
    if dc1 is not None or dc2 is not None:
        # 0 ist hier ein gueltiger Messwert (Nacht), kein "fehlt".
        return max(0.0, (dc1 or 0.0) + (dc2 or 0.0))
    ac = _finite(acpower)
    if ac is None:
        return None
    bat = _finite(batpower_raw) or 0.0
    return max(0.0, ac + max(0.0, bat))


def hausverbrauch_watt(
    acpower: object,
    feedin: object,
    batpower_raw: object = None,
) -> Optional[float]:
    """Leitet den Hausverbrauch aus der Wechselrichterbilanz ab.

    ``batpower_raw`` wird nicht mehr gebraucht und ist nur noch zur
    Aufrufkompatibilitaet vorhanden. Er darf nicht in die Rechnung
    eingehen - die Begruendung steht unten.

        acpower = Hausverbrauch + Einspeisung

    also::

        Hausverbrauch = acpower - feedin

    ``ACPower`` ist der Wechselstromausgang des Wechselrichter - das,
    was ins Haus fliesst, plus was eingespeist wird. Die Batterie gehoert
    hier NICHT hinein: sie steht zwischen der Gleichstromseite und der
    Wechselstromseite und aendert am AC-Ausgang nichts.

    Bis September stand hier ``acpower - feedin - batPower``, was die
    Annahme ``acpower = PV-Erzeugung`` voraussetzte. Die ist falsch -
    nachts liefert die DC-Seite 0 W, die AC-Seite dagegen rund 250 W.
    Folge der alten Formel: bei ladender Batterie war das Ergebnis
    negativ und wurde auf 0 begrenzt (im Betriebslog des 03.10. durchgehend
    ``haus=0``), bei entladender Batterie wurde die Entladung faelschlich
    als Verbrauch addiert.

    Etwa 8 % der Werte fallen leicht negativ, weil Ausgang und
    Einspeisung nicht exakt gleichzeitig gemessen werden; das Ergebnis
    wird deshalb auf >= 0 begrenzt und nicht als exakte
    Momentanleistung ausgewiesen.

    Wichtig: Der Wert ENTHAEALT den Verbrauch der Waermepumpe, wenn sie
    laeuft. Als Zuschaltkriterium taugt deshalb nicht der Hausverbrauch,
    sondern der eigens berechnete Ueberschuss.
    """
    ac = _finite(acpower)
    grid = _finite(feedin)
    if ac is None or grid is None:
        return None
    return max(0.0, ac - grid)


def pv_ueberschuss_watt(
    acpower: object,
    feedin: object,
    batpower_raw: object,
) -> Optional[float]:
    """PV-Leistung, die nach Deckung des Hauses uebrig bleibt.

    Aus der Bilanz folgt fuer den UEBERSCHUSS::

        Ueberschuss = max(0, feedin + batPower)

    Bei ladender Batterie ist das genau ``acpower - hausverbrauch``; bei
    entladener greift die Klammer auf 0, weil die Batterie dann nichts
    zusaetzlich bereitstellt, sondern gespeicherte Energie zurueckgibt.
    Nur dieser Betrag steht fuer die Waermepumpe zusaetzlich zur Verfuegung.

    Achtung fuer Aufruf mit ``batpower < 0``: dann gilt
    ``hausverbrauch + ueberschuss != acpower``, weil das Haus dann mehr
    aufnimmt als die PV erzeugt.

    Der Ueberschuss wird bewusst DIREKT aus ``feedin + batPower``
    berechnet und nicht als ``acpower - hausverbrauch``: bei Messversatz
    wird der Hausverbrauch auf 0 begrenzt, wodurch die zweite Variante
    zu viel auswiese (gemessen: 300 W statt der tatsaechlichen 410 W).
    """
    # Haeufigster Aufrufweg: classify_energy_source() reicht den Rohwert
    # weiter. Fehlt er, ist die Bilanz unvollstaendig und es wird None
    # geliefert - nicht 0, denn "kein Ueberschuss" und "keine Messung"
    # fuehren zu unterschiedlichen Entscheidungen.
    grid = _finite(feedin)
    bat = _finite(batpower_raw)
    if grid is None or bat is None:
        return None
    return max(0.0, grid + bat)


def batterie_entladung_watt(raw_batpower: object) -> Optional[float]:
    """Normalisiert Solax ``batPower`` zu positiver ENTLADEleistung.

    ``batPower < 0`` = Entladung, daher wird das Vorzeichen gedreht. Der
    Rohwert bleibt fuer Diagnose unveraendert erhalten.
    """
    value = _finite(raw_batpower)
    return None if value is None else max(0.0, -value)


def batterie_ladung_watt(raw_batpower: object) -> Optional[float]:
    """Normalisiert Solax ``batPower`` zu positiver LADEleistung.

    ``batPower > 0`` = Ladung (Solarueberschuss wandert in die Batterie).
    """
    value = _finite(raw_batpower)
    return None if value is None else max(0.0, value)


def classify_energy_source(
    *,
    pv_acpower: object,
    feedin_watt: object,
    batpower_raw: object,
    soc: object,
    solar_stale: bool = False,
    pv_min_watt: float = 50.0,
    battery_min_watt: float = 50.0,
    soc_min_prozent: float = 90.0,
    max_netzkauf_watt: float = -50.0,
    pv_erzeugung_watt: object = None,
) -> EnergiequellenStatus:
    """Klassifiziert PV > Batterie > Netz anhand aktueller und valider Werte.

    ``batpower_raw`` ist der UNVERAENDERTE, vorzeichenbehaftete Solax-Rohwert
    (``+`` = Ladung, ``-`` = Entladung). Er wird hier genau einmal auf die
    Entladeleistung normalisiert.

    Wichtig: Der Parameter darf KEIN bereits normalisiertes
    ``state.solar.battery_discharge_watt`` entgegennehmen - das war vorher
    moeglich und normalisierte ein zweites Mal, wodurch sich Vorzeichen und
    Betrag gegenseitig aufhoben und jede Entladung als 0 W gemeldet wurde.

    Für direkte Rule-Unit-Tests bleibt ``feedin_watt`` als PV-Signal-Fallback
    erhalten, wenn kein separates ``pv_acpower`` angegeben wurde. Produktiv
    liefert ``determine_mode_and_setpoints`` beide Messungen aus dem State.
    """
    pv = _finite(pv_acpower)
    feedin = _finite(feedin_watt)
    discharge = batterie_entladung_watt(batpower_raw)
    soc_value = _finite(soc)
    pv_threshold = max(0.0, float(pv_min_watt))
    battery_threshold = max(0.0, float(battery_min_watt))
    soc_threshold = float(soc_min_prozent)
    grid_limit = float(max_netzkauf_watt)

    def result(status: Energiequelle, verfuegbar: bool, text: str) -> EnergiequellenStatus:
        return EnergiequellenStatus(
            quelle=status,
            verfuegbar=verfuegbar,
            begruendung=text,
            pv_acpower_watt=pv,
            feedin_watt=feedin,
            batterie_entladung_watt=discharge,
            soc_prozent=soc_value,
        )

    if solar_stale:
        return result(Energiequelle.STALE, False, "Solardaten veraltet")

    kein_netzkauf = feedin is not None and feedin >= grid_limit
    ueberschuss = pv_ueberschuss_watt(pv, feedin, batpower_raw)

    # Zwei Nachweise fuer Solarstrom:
    #  1. die ERZEUGUNG liegt ueber der Schwelle, ODER
    #  2. es ist UEBERSCHUSS nachweisbar - die Batterie laedt oder es wird
    #     eingespeist.
    #
    # Zu (1): bevorzugt wird `pv_erzeugung_watt` (powerdc1 + powerdc2). Ohne
    # diesen Wert greift der Rueckfall auf `pv_signal`, also den
    # Wechselstromausgang. Das ist KEIN Ersatz fuer die Erzeugung, sondern
    # Haus plus Netz - und genau daraus entstand die urspruengliche
    # Fehldiagnose "1,5 h ungenutztes Solar".
    #
    # Der damalige Befund lautete: PV=210 W bei ausgewiesenem Ueberschuss von
    # 2354-5488 W, 09:18-10:49. Die Anzeige stand dabei nicht fest - sie war
    # nie die Erzeugung. Nachts liefert die DC-Seite 0 W, die AC-Seite
    # dagegen rund 250 W. Der zweite Nachweis bleibt als Sicherheitsnetz
    # bestehen: er ist unabhaengig von der Erzeugungsmessung.
    generation = _finite(pv_erzeugung_watt)
    pv_signal = generation if generation is not None else (
        pv if pv is not None else feedin
    )
    pv_ok = (
        pv_signal is not None
        and pv_signal >= pv_threshold
        and kein_netzkauf
    )
    ueberschuss_ok = (
        ueberschuss is not None
        and ueberschuss >= pv_threshold
        and kein_netzkauf
    )
    if pv_ok or ueberschuss_ok:
        # Beide Werte nennen: sie koennen weit auseinanderliegen, und genau
        # diese Diskrepanz ist das Symptom der stale Erzeugungsanzeige.
        # Einer der beiden kann None sein - der Weg ueber pv_ok laeuft auch
        # dann, wenn gar keine Erzeugung gemeldet wird.
        erzeugung_txt = "n/a" if pv_signal is None else f"{pv_signal:.0f}W"
        ueberschuss_txt = "n/a" if ueberschuss is None else f"{ueberschuss:.0f}W"
        return result(
            Energiequelle.PV,
            True,
            f"PV-Erzeugung {erzeugung_txt}, Ueberschuss {ueberschuss_txt}"
            f" >= {pv_threshold:.0f}W, kein Netzkauf",
        )

    batterie_ok = (
        discharge is not None
        and discharge >= battery_threshold
        and soc_value is not None
        and soc_value >= soc_threshold
        and kein_netzkauf
    )
    if batterie_ok:
        return result(
            Energiequelle.BATTERIE,
            True,
            (
                f"Batterie {discharge:.0f}W >= {battery_threshold:.0f}W, "
                f"SOC {soc_value:.0f}% >= {soc_threshold:.0f}%, kein Netzkauf"
            ),
        )

    pv_text = "n/a" if pv_signal is None else f"{pv_signal:.0f}W"
    batt_text = "n/a" if discharge is None else f"{discharge:.0f}W"
    soc_text = "n/a" if soc_value is None else f"{soc_value:.0f}%"
    return result(
        Energiequelle.NETZ,
        False,
        (
            f"keine erneuerbare Quelle: PV-Erzeugung {pv_text}<{pv_threshold:.0f}W, "
            f"Batterie {batt_text}/{soc_text}<{battery_threshold:.0f}W/"
            f"{soc_threshold:.0f}% oder Netzkauf"
        ),
    )
