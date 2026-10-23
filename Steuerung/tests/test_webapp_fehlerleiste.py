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
