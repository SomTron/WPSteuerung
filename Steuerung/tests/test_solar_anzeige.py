"""Tests: Die Solarwerte in Status und WebApp sind die echten.

Anlass war `PV=330W` um 23:07 Uhr im Telegram-Menue. Nachts liefert
die Gleichstromseite 0 W, die Wechselstromseite dagegen rund 250 W -
`ACPower` ist der Wechselrichterausgang (Haus + Netz), nicht die
Erzeugung.

Gemessen auf dem Pi, Stundenmittel der Betriebsdaten:

    Stunde   DC (W)   AC (W)
    0-4          0    224-250
    9         2914      352
    12        6881     6749

Die WebApp zeigte bislang gar keine Erzeugung, nur den AC-Wert unter
dem Namen "AC-Leistung". Damit war die entscheidende Groesse im Bild
der Anlage unsichtbar.
"""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
WEBAPP = os.path.join(ROOT, 'webapp', 'index.html')
API = os.path.join(ROOT, 'Steuerung', 'api.py')


def _webapp():
    with open(WEBAPP, encoding='utf-8') as fh:
        return fh.read()


def _api():
    with open(API, encoding='utf-8') as fh:
        return fh.read()


def test_status_liefert_die_erzeugung():
    """Ohne diesen Feldnamen kann kein Client die Erzeugung anzeigen."""
    text = _api()
    assert '"pv_generation_w"' in text, (
        "die API liefert die PV-Erzeugung nicht - jeder Client zeigt "
        "weiterhin nur den Wechselrichterausgang"
    )
    assert '"hausverbrauch_w"' in text
    assert '"pv_ueberschuss_w"' in text


def test_erzeugung_und_ac_bleiben_getrennt():
    """Zusammengefasst waere der Unterschied spaeter nicht mehr rekonstruierbar."""
    text = _api()
    assert '"ac_power"' in text, "der AC-Wert fehlt (er wird weiter genutzt)"
    # Zwei verschiedene Felder, keines ersetzt das andere.
    assert '"pv_generation_w"' in text and '"ac_power"' in text


def test_webapp_hat_feld_fuer_die_erzeugung():
    text = _webapp()
    assert 'id="energy-pv"' in text, "kein Feld fuer die PV-Erzeugung"
    assert 'PV-Erzeugung' in text


def test_webapp_aktualisiert_die_erzeugung_bei_jedem_poll():
    """Ein nur einmal gerendertes Feld zeigt dauerhaft den ersten Wert.

    Genau das Muster steht in updateValues(): die Felder werden per
    getElementById().textContent gesetzt. Fehlt eine Zeile, bleibt der
    Wert aus dem initialen Rendern stehen.
    """
    text = _webapp()
    for feld in ('energy-pv', 'energy-haus', 'energy-ac'):
        assert f"getElementById('{feld}')" in text, (
            f"{feld} wird beim Rendern befuellt, aber bei keinem Poll "
            "aktualisiert - es zeigte dauerhaft den ersten Wert"
        )
    assert 'energy.pv_generation_w' in text
    assert 'energy.hausverbrauch_w' in text


def test_webapp_beschriftet_den_ac_wert_eindeutig():
    """"AC-Leistung" wird leicht als PV gelesen - gerade nachts."""
    text = _webapp()
    assert 'Wechselrichter-Ausgang' in text, (
        "der AC-Wert ist nicht eindeutig beschriftet"
    )
    assert re.search(r'>\s*AC-Leistung\s*<', text) is None, (
        "die alte, mehrdeutige Beschriftung ist noch aktiv"
    )


def test_kein_feld_beschriftet_den_ac_wert_als_pv():
    """Die alte Falle: ein AC-Wert unter der Aufschrift PV."""
    text = _webapp()
    # Erlaubt: "PV-Erzeugung" mit dem Erzeugungswert.
    # Verboten: der AC-Wert direkt unter "PV".
    zeilen = [z for z in text.splitlines() if 'energy-ac' in z]
    for zeile in zeilen:
        if 'title=' in zeile or "getElementById('energy-ac')" in zeile:
            continue
        assert '>PV<' not in zeile and '>PV ' not in zeile, zeile.strip()[:90]