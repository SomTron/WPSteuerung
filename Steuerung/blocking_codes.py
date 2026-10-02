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

#: Sperrfaehlen, die kein Fehler und kein Eingriff sind, sondern den
#: Normalzustand beschreiben: der Boiler ist bereits heiss genug, ein Start
#: waere nicht nur unnoetig, sondern wuerde am Temperaturlimit in einen
#: Kurzlauf laufen. Typischer Fall: die Stunden nach einer Legionellenfahrt.
#:
#: Ebenfalls hier: die Taktschutz-Sperren (Mindestpause, Mindestlaufzeit,
#: Start-Antizipation). Sie sind gewollte Hardware-Schuetzung und stehen
#: nach *jedem* Kompressorlauf fuer Minuten an - als Alarm waeren sie
#: Spam, weil sie im Wechsel mit dem jeweils naechsten Grund erneut greifen.
#: Sichtbar bleiben sie im Log, in der WebApp und in ``blocked``.
INFO_BLOCKING_CODES = frozenset({
    "boiler_bereits_warm",
    "mindestpause",
    "mindestlaufzeit",
    "start_antizipation",
    "boiler_max_erreicht",
})

#: Passende Logmeldung JE Sperrfamilie. Vorher gab es einen einzigen Text
#: fuer alle vier Faelle ("Boiler bereits heiss, kein Start noetig"), der
#: auch bei der Mindestpause erschien - bei 24 C Speichertemperatur also
#: eine offensichtlich falsche Aussage (Beobachtung 28.09.2026, 18:43:
#: "Boiler bereits heiss" bei Oben=24.4C, Sperre 'mindestpause').
#: ``boiler_max_erreicht`` unterscheidet den planmaessigen Zyklusabschluss von
#: der echten Sicherheitseingriffs-Sperre: die harte Boiler-Max-Abschaltung wird
#: geprueft BEVOR der normale Ausschaltweg und feuert auch dann, wenn die
#: Gewinner-Regel bereits selbst auf AUS stand (``unten >= Ausschaltpunkt``).
#: Im Betriebslog 02.10.2026, 12:19 war das der Fall: drei Regeln meldeten AUS,
#: der Lauf dauerte 143,7 min gegen 60 min Mindestlaufzeit - die Meldung
#: "Mindestlaufzeit gebrochen" war falsch und der Telegram-Alarm ein Fehlalarm
#: fuer regulaeres Ende eines Heizlaufs.
INFO_BLOCKING_MELDUNGEN = {
    "boiler_bereits_warm": "Boiler bereits heiss, kein Start noetig",
    "mindestpause": "Start gesperrt: Mindestpause nach letztem Lauf",
    "mindestlaufzeit": "Start gesperrt: Mindestlaufzeit noch nicht erreicht",
    "start_antizipation": "Start gesperrt: Start-Antizipation",
    "boiler_max_erreicht": (
        "Boiler-Maximum erreicht, Zyklus planmaessig beendet "
        "(Freigabe erst nach Abkuehlen)"
    ),
}
#: Fallback, falls eine neue Info-Familie ohne eigene Meldung hinzukommt.
INFO_BLOCKING_FALLBACK = "Start nicht noetig bzw. gesperrt"

#: Ueberleitungen von Freitext auf diese Sperrfamilien. Werden VOR der
#: generischen Musterpruefung geprueft, damit eine praezisere Benennung
#: nicht am generischen ``sonstige_sperre`` klebt.
_SONDER_CODES = (
    # Muss vor der generischen "boiler-max"-Pruefung stehen: der planmaessige
    # Abschluss traegt ebenfalls "boiler-maximum" im Text.
    ("boiler-maximum erreicht", "boiler_max_erreicht"),
    ("start-antizipation", "start_antizipation"),
    ("start blockiert", "start_antizipation"),
    ("start-vorhersage", "start_antizipation"),
    ("antizipation", "start_antizipation"),
    ("mindestlaufzeit", "mindestlaufzeit"),
    ("min. pause", "mindestpause"),
    ("mindestpause", "mindestpause"),
    ("overshoot-vorhersage", "overshoot_vorhersage"),
    ("ueberschwing-vorhersage", "overshoot_vorhersage"),
)

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
    # Taktschutz-Sperren zuerst und praezise: sie stehen als "warte auf
    # Mindestlaufzeit" bzw. "Start-Antizipation: ..." im Freitext.
    for muster, code in _SONDER_CODES:
        if muster in text:
            return code
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
