"""Einheitliche Klassifikation der aktuellen WP-Energiequelle.

Die Regel-Engine verwendet ausschließlich positive Entladeleistung als
Batterie-Signal und verlangt für PV/Batterie, dass gleichzeitig kein relevanter
Netzbezug gemessen wird.
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


def batterie_entladung_watt(raw_batpower: object) -> Optional[float]:
    """Normalisiert Solax ``batPower`` zu positiver Entladeleistung.

    Vertrag des Projekts: ``batPower > 0`` = Entladung, ``batPower < 0`` =
    Ladung. Der unveränderte Rohwert bleibt für Diagnose erhalten.
    """
    value = _finite(raw_batpower)
    return None if value is None else max(0.0, value)


def batterie_ladung_watt(raw_batpower: object) -> Optional[float]:
    """Normalisiert negative Solax-Ladeleistung zu einem positiven Betrag."""
    value = _finite(raw_batpower)
    return None if value is None else max(0.0, -value)


def classify_energy_source(
    *,
    pv_acpower: object,
    feedin_watt: object,
    battery_discharge_watt: object,
    soc: object,
    solar_stale: bool = False,
    pv_min_watt: float = 50.0,
    battery_min_watt: float = 50.0,
    soc_min_prozent: float = 90.0,
    max_netzkauf_watt: float = -50.0,
) -> EnergiequellenStatus:
    """Klassifiziert PV > Batterie > Netz anhand aktueller und valider Werte.

    Für direkte Rule-Unit-Tests bleibt ``feedin_watt`` als PV-Signal-Fallback
    erhalten, wenn kein separates ``pv_acpower`` angegeben wurde. Produktiv
    liefert ``determine_mode_and_setpoints`` beide Messungen aus dem State.
    """
    pv = _finite(pv_acpower)
    feedin = _finite(feedin_watt)
    discharge = batterie_entladung_watt(battery_discharge_watt)
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

    pv_signal = pv if pv is not None else feedin
    kein_netzkauf = feedin is not None and feedin >= grid_limit
    pv_ok = (
        pv_signal is not None
        and pv_signal >= pv_threshold
        and kein_netzkauf
    )
    if pv_ok:
        return result(
            Energiequelle.PV,
            True,
            f"PV-Erzeugung {pv_signal:.0f}W >= {pv_threshold:.0f}W, kein Netzkauf",
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
