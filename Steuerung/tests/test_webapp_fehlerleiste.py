# -*- coding: utf-8 -*-
"""Die WebApp muss Render-Fehler SICHTBAR machen.

Befund 23.10.2026: Auf dem Pi lieferte `/status` korrekte Werte
(Temperaturen, Setpoints), die Oberflaeche blieb aber komplett auf "-".
Ohne Browser-Konsole war die Ursache nicht zu finden - auf dem Handy ist
sie gar nicht erreichbar. Ein stummer Abbruch im Renderpfad sieht exakt
aus wie "keine Daten": Der Verbindungstext bleibt stehen, weil
`updateConnectionStatus()` erst NACH `updateValues()` laeuft.
"""
import os
import re

WEBAPP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "webapp", "index.html",
)


def _html() -> str:
    with open(WEBAPP, encoding="utf-8") as f:
        return f.read()


def test_fehlerleiste_existiert_im_markup():
    assert 'id="js-fehler"' in _html(), "sichtbare Fehlerleiste fehlt im HTML"


def test_globale_fehler_handler_sind_registriert():
    html = _html()
    assert "window.addEventListener('error'" in html, "kein window.onerror-Handler"
    assert "window.addEventListener('unhandledrejection'" in html, (
        "abgelehnte Promises wuerden stumm verschwinden"
    )


def test_statusabruf_meldet_fehler_in_der_leiste():
    """Ein Fehler aus renderApp()/updateValues() landet im catch von
    fetchStatus - dort muss er auch in die Leiste, nicht nur in die
    Konsole (die auf dem Handy niemand sieht)."""
    html = _html()
    assert re.search(r"zeigeJsFehler\('Statusabruf: '", html), (
        "Statusabruf-Fehler werden nicht sichtbar gemeldet"
    )


def test_webapp_meldet_fehler_an_den_server():
    """Der Fehler muss ins STEUERUNGS-Log, nicht nur auf den Bildschirm."""
    html = _html()
    assert "/debug/clientfehler" in html, "kein Server-Report der WebApp"
    assert "meldeAnServer(text, typ)" in html, "zeigeJsFehler meldet nicht an den Server"


# --- 07.11.2026: Jede Fehlermeldung landet im Log -------------------------

def _funktion(html: str, name: str) -> str:
    """Quelltext einer Funktion (Einrueckung 8 Leerzeichen, s. index.html)."""
    rest = html[html.index("function " + name):]
    return rest[: rest.index("\n        }")]


def test_servermeldung_haengt_nicht_an_der_fehlerleiste():
    """Die Meldung an den Server darf nicht von der Anzeige abhaengen.

    Vorher stand `meldeAnServer(text)` NACH den beiden `return`s
    (`!leiste` = alte gecachte Huelle, Text schon vorhanden) - in genau
    diesen Faellen war der Fehler im Log unsichtbar.
    """
    koerper = _funktion(_html(), "zeigeJsFehler")
    meldung = koerper.index("meldeAnServer(text")
    anzeige = koerper.index("document.getElementById('js-fehler')")
    assert meldung < anzeige, (
        "meldeAnServer muss VOR der Anzeige laufen - sonst verschluckt ein "
        "fehlendes Leisten-Element die Log-Meldung"
    )


def test_servermeldung_wird_clientseitig_gedrosselt():
    """Ein 5-s-Poll mit Timeout darf nicht 12 POSTs je Minute erzeugen."""
    html = _html()
    assert "SERVER_MELDUNG_DROSSEL_MS" in html
    koerper = _funktion(html, "meldeAnServer")
    assert "SERVER_MELDUNG_DROSSEL_MS" in koerper, "keine Drossel im Meldepfad"


def test_servermeldung_ueberlebt_fehlendes_api_base():
    """`API_BASE` ist ein `const` weiter unten (Temporal Dead Zone).

    Ein Fehler VOR dieser Zeile wuerde beim Zugriff einen ReferenceError
    ausloesen - die Meldung darf deshalb nicht daran scheitern.
    """
    koerper = _funktion(_html(), "meldeAnServer")
    assert "window.location.origin" in koerper, (
        "kein Rueckfall auf den eigenen Origin, wenn API_BASE noch fehlt"
    )


def test_servermeldung_schickt_den_api_key_mit():
    """`/debug/clientfehler` ist seit dem Security-Audit API-Key-pflichtig.

    Ohne `X-API-Key` antwortet der Server mit 401 (Key gesetzt) bzw. 503
    (Key leer). Der WebApp-Fehler waere dann genau im Fehlerfall unsichtbar -
    live nachgewiesen am 08.11.2026: Meldung ohne Header -> 401, keine
    Logzeile.
    """
    koerper = _funktion(_html(), "meldeAnServer")
    assert "apiHeaders(" in koerper, (
        "meldeAnServer schickt keinen X-API-Key - der Server weist die "
        "Meldung mit 401/503 ab"
    )


def test_webapp_zeigt_die_wirksame_ueberschuss_schwelle():
    """Nutzerwunsch 10.11.2026: Der Sommer-Modus muss die geltende Schwelle
    zeigen - sonst ist nicht erkennbar, ob ab 500 W oder 5000 W geheizt wird.
    """
    html = _html()
    # Wirksame Schwelle aus mode.pv_ueberschuss_schwelle_w im Sommer-Info-Text.
    assert "pv_ueberschuss_schwelle_w" in html
    assert "PV-Überschuss erst ab " in html
    assert "Inaktiv (PV-Überschuss ab " in html
    # Regelliste nennt beide Schwellen (Winter/Sommer).
    assert "im Sommer >= " in html


def test_webapp_zeigt_berechnete_startzeit_und_gemeinsame_etappen():
    """Nutzerwunsch 09.11.2026: CalcStart zeigt "Start 09:15 -> 12:00" im
    Detailtext und eine gemeinsame Etappen-Uebersicht beider Regeln.

    Die Uebersicht ist NUR eine Anzeige und steht bewusst NICHT in einer
    Regelzeile (sonst liest es sich, als gehoerte die Abend-Etappe zur
    Mittag-Regel). Berechnet wird getrennt: CalcStart-Mittag gegen
    target_mittag_uhr, CalcStart-Abend gegen die Abendzeit.
    """
    html = _html()
    # Berechnete Startzeit im Detailtext der jeweiligen Regel.
    assert "' Start ' + formatCalcHour(e.start_uhr)" in html, (
        "die berechnete Startzeit fehlt im Detailtext der CalcStart-Regel"
    )
    # Eigene Funktion fuer die gemeinsame Uebersicht.
    assert "function calcStartUebersichtHtml(" in html, (
        "keine gemeinsame Etappen-Uebersicht"
    )
    assert "CalcStart-Uebersicht" in html, "die Uebersicht ist nicht benannt"
    # Sie wird an die Regelliste ANGEHAENGT, nicht in eine Regelzeile gehaengt.
    assert ".join('') + calcStartUebersichtHtml(" in html, (
        "die Uebersicht muss am Ende der Regelliste stehen"
    )
    # Jede Etappe nennt nur ihre eigenen Werte (Start/Ziel/Soll/Puffer). Die
    # globalen "geplant"/"spaetestens" der Ein-Ziel-Rechnung duerfen NICHT im
    # Etappen-Detail stehen: sie mischen den Mittagstief-Aufschlag hinein und
    # lieferten im Betrieb 09.11.2026 "geplant 08:46" neben "Start 10:30".
    assert "').join(' &middot; ') + planned + latest;" not in html, (
        "globale geplant/spaetestens-Zeiten verwirren bei Etappen"
    )
    assert "Start = rechnerischer Einschaltzeitpunkt" in html, (
        "die Legende fuer Start/Puffer fehlt"
    )
    assert "const zielHinweis = zielGelernt ? ' (gelernt)' : '';" in html, (
        "die gelernte Zielzeit ist nicht gekennzeichnet"
    )
    assert "etappenHtml += '<div class=\"rule-etappen-sammel\"" not in html, (
        "die Abend-Etappe darf nicht in der Mittag-Zeile erscheinen"
    )


def test_alle_abrufpfade_melden_an_den_server():
    """Karten/Badge/Feedback zeigen den Fehler nur dem, der hinschaut.

    Ins Betriebslog muessen auch: Fehler-API, Health, Analyse, Historie,
    Steuerbefehl und die Service-Worker-Registrierung.
    """
    html = _html()
    erwartet = {
        "Fehler-API": "'errors'",
        "Health-API": "'health'",
        "Historie": "'historie'",
        "Steuerbefehl": "'befehl'",
        "Service-Worker-Registrierung": "'sw'",
        "Analyse": "'analyse'",
    }
    for text, typ in erwartet.items():
        assert text in html, f"Meldung {text!r} fehlt"
        # Entweder direkt ins Log oder ueber zeigeJsFehler (das meldet seit
        # 1.10.29 immer an den Server, auch wenn die Leiste fehlt).
        assert ("meldeAnServer('" + text) in html or ("zeigeJsFehler('" + text) in html, (
            f"{text!r} wird nicht an den Server gemeldet"
        )
        assert typ in html, f"Typ {typ} fehlt - die Logzeile ist nicht einordenbar"

