# -*- coding: utf-8 -*-
"""Boiler-Fuellstandsmodell (Punkt A).

Schaetzt aus den drei Fuehlern (oben/mittig/unten), wie viel nutzbares
Warmwasser noch im Speicher ist. Die Temperaturgradienten zwischen den
Fuehlern werden linear interpoliert.

Annahme: 3 Fuehler auf gleicher Hoehe verteilt (unten=0%, mittig=50%, oben=100%),
oder individuell konfiguriert ueber hoehen_anteil.
"""

from typing import Dict, Optional, Tuple


# Standard-Positionen der Fuehler im Speicher (0=ganz unten, 1=ganz oben)
DEFAULT_HOEHEN = {
    "unten": 0.05,
    "mittig": 0.50,
    "oben": 0.95,
}


def schaetze_warmwasser(
    temp_dict: Dict[str, Optional[float]],
    volumen_l: float = 150.0,
    nutztemp_c: float = 40.0,
    kaltwasser_c: float = 10.0,
    hoehen: Optional[Dict[str, float]] = None,
) -> Tuple[float, float]:
    """Schaetzt den Warmwasser-Vorrat im Boiler.

    Args:
        temp_dict: Dict mit Keys 'unten', 'mittig', 'oben' (Temperaturen in C).
        volumen_l: Boiler-Gesamtvolumen in Litern.
        nutztemp_c: Temperatur, ab der Wasser als "warm" zaehlt.
        kaltwasser_c: Kaltwasser-Zulauftemperatur (Frischwasser).
        hoehen: Optionale Fuehler-Positionen (0..1). Default: unten 5%,
                mittig 50%, oben 95%.

    Returns:
        (warmwasser_liter, anteil_prozent)
    """
    if hoehen is None:
        hoehen = DEFAULT_HOEHEN

    # Fuehler-Temperaturen holen
    t_unten = temp_dict.get("unten")
    t_mittig = temp_dict.get("mittig")
    t_oben = temp_dict.get("oben")

    # Wenn kein einziger Fuehler lesbar ist -> None
    if t_unten is None and t_mittig is None and t_oben is None:
        return 0.0, 0.0

    # Hilfsfunktion: Temperatur an einer Hoehe h (0..1) interpolieren
    def temp_bei(h: float) -> Optional[float]:
        punkte = []
        for name, pos in hoehen.items():
            t = temp_dict.get(name)
            if t is not None:
                punkte.append((pos, t))
        if not punkte:
            return None
        if len(punkte) == 1:
            return punkte[0][1]  # nur ein Fuehler -> konstante Temp
        # Sortieren nach Hoehe
        punkte.sort()
        # Unterhalb des tiefsten Fuehlers: konstant = tiefster Wert
        if h <= punkte[0][0]:
            return punkte[0][1]
        # Oberhalb des hoechsten Fuehlers: konstant = hoechster Wert
        if h >= punkte[-1][0]:
            return punkte[-1][1]
        # Linear interpolieren zwischen den zwei benachbarten Fuehlern
        for i in range(len(punkte) - 1):
            h1, t1 = punkte[i]
            h2, t2 = punkte[i + 1]
            if h1 <= h <= h2:
                anteil = (h - h1) / (h2 - h1)
                return t1 + anteil * (t2 - t1)
        return punkte[-1][1]

    # Ueber 100 Schichten integrieren
    schritte = 100
    warmwasser_volumen = 0.0
    for i in range(schritte):
        h_mitte = (i + 0.5) / schritte
        t = temp_bei(h_mitte)
        if t is None:
            continue
        # Schicht zaehlt als "warm" wenn sie ueber nutztemp_c liegt
        # bzw. wenn sie naeher an nutztemp als an kaltwasser ist
        if t >= nutztemp_c:
            warmwasser_volumen += volumen_l / schritte

    anteil = (warmwasser_volumen / volumen_l * 100.0) if volumen_l > 0 else 0.0
    return round(warmwasser_volumen, 1), round(anteil, 1)