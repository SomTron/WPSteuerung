"""Stabile Klassifikation der Sperr-/Blockadegruende.

Der Steuerungsstatus fuehrt `blocking_reason` als Freitext, der je nach
Pfad unterschiedlich formuliert wird. Fuer Alarmierung, API-Vertrag und
Auswertung wird daraus eine stabile *Sperrfamilie* abgeleitet, damit

- Alarme nicht an einem Wortlaut haengen,
- der Normalzustand ("Boiler ist bereits heiss") von einer echten
  Stoerung unterscheidbar bleibt,
- die Auswertung stabile Codes statt variierender Texte bekommt.

Dieses Modul ist bewusst frei von Importen aus `main` oder `api`: beide
importieren es, eine Rueckrichtung wuerde einen Circular-Import erzeugen.
"""

from __future__ import annotations

from typing import Optional

# Sperrfaehlen, die kein Fehler und kein Eingriff sind, sondern den
# Normalzustand beschreiben: der Boiler ist bereits heiss genug, ein Start
# waere nicht nur unnoetig, sondern wuerde am Temperaturlimit in einen
# Kurzlauf laufen. Typischer Fall: die Stunden nach einer Legionellenfahrt.
# Diese Faelle erzeugen KEINEN Telegram-Alarm, bleiben aber sichtbar.
INFO_BLOCKING_CODES = frozenset({"boiler_bereits_warm"})

_ERSAETZUNGEN = (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"))


def normalisiere_sperrtext(reason: Optional[str]) -> str:
    """Kleinschreibung und Umlaut-Ersetzung fuer stabile Textvergleiche.

    Die Sperrtexte werden an mehreren Stellen per ``str.format`` erzeugt und
    enthalten je nach Pfad ``Naehe`` oder ``NaNaehe``/``Nähe``. Damit die
    Klassifikation nicht von der Schreibweise des jeweiligen Aufrufers
    abhaengt, werden beide Formen hier auf dieselbe Darstellung gebracht.
    """
    text = (reason or "").strip().lower()
    for zeichen, ersatz in _ERSAETZUNGEN:
        text = text.replace(zeichen, ersatz)
    return text


def blocking_code(reason: Optional[str]) -> str:
    """Liefert stabile Sperrfamilien statt dynamischer Freitext-Alarme."""
    text = normalisiere_sperrtext(reason)
    if not text:
        return ""
    # "Boiler ist bereits heiss" zuerst: das ist die Startnaehe-Sperre sowie
    # die Zieltemperatur- und Schichtungsgrenze - alles Normalzustand und
    # keine Stoerung. Die harte Boiler-Maximum-Abschaltung weiter unten
    # bleibt davon getrennt.
    if (
        "boiler-max-naehe" in text
        or "zieltemp" in text
        or "schichtungs-obergrenze" in text
    ):
        return "boiler_bereits_warm"
    if "mindestlaufzeit" in text or "warte auf mindestlaufzeit" in text:
        return "mindestlaufzeit"
    if "min. pause" in text or "mindestpause" in text or "pause" in text:
        return "mindestpause"
    if "boiler-max" in text or "boiler max" in text:
        return "boiler_max"
    if "sensor" in text:
        return "sensorfehler"
    if "druck" in text:
        return "druckfehler"
    if "verdampfer" in text:
        return "verdampfer"
    if "ueberhitzung" in text or "überhitzung" in text:
        return "uebertemperatur"
    if (
        "gpio" in text
        or "hardware" in text
        or "einschalten fehlgeschlagen" in text
        or "ausschalten fehlgeschlagen" in text
    ):
        return "hardwarefehler"
    if "regelungsfehler" in text:
        return "regelungsfehler"
    if "stale" in text or "veraltet" in text:
        return "daten_stale"
    if "nachtsperre" in text:
        return "nachtsperre"
    if "quelle" in text or "pv/batterie" in text:
        return "warte_quelle"
    return "sonstige_sperre"
