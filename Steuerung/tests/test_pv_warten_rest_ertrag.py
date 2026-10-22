"""PV-Warten: der Rest-Tagesertrag muss den Bedarf decken (BUGFIX 22.10.2026).

Das PV-Warten pruefte bisher nur den TAGES-Forecast. Das ist falsch: um
11:00 ist der meiste Tagesertrag schon realisiert. Die Regel wartet
dann trotzdem bis 12:00 auf die Sonne, obwohl der Rest-Ertrag den
Aufheizbedarf nicht mehr deckt. Der Boiler bleibt zu kalt, und die
Regel tut so, als haette sie richtig gewartet.

Neu: Mit Stundenprognose und Speichervolumen wird der Rest-Ertrag bis
`pv_warten_bis_uhr` gegen den Bedarf geprueft (mit 30 % Reserve).
Ohne diese Daten bleibt das alte Verhalten (fail-open).
"""
from json_config import AbweichungConfig
from priority_control import evaluate_abweichung


# Realistischer Oktober-Tag, 10 m2 Array. Werte in W/m2.
_TAGESGANG_WM2 = {
    6: 60, 7: 220, 8: 430, 9: 600, 10: 650, 11: 610,
    12: 520, 13: 400, 14: 260, 15: 150, 16: 70, 17: 30,
}
_FLAECHE_M2 = 10.0
# Die Stundenprognose ist eine LEISTUNG in W fuer das ganze Array.
_STUNDEN_W = {h: w * _FLAECHE_M2 for h, w in _TAGESGANG_WM2.items()}


def _abweichung(stunde, *, mit_stunden=True, unten=25.0):
    abw = AbweichungConfig()
    return evaluate_abweichung(
        abw=abw,
        temp_dict={"unten": unten, "oben": 40.0, "mittig": 30.0},
        kompressor_ein=False,
        now_hour=stunde,
        nachtsperre_start=19,
        nachtsperre_ende=8,
        feedin_watt=0.0,
        soc=50.0,
        bademodus_aktiv=False,
        forecast_today_wh_qm=3000.0,
        fc_ratio=1.0,
        pv_acpower=0.0,
        battery_power=0.0,
        solar_stale=False,
        notfall_einschalten_c=25.0,
        pv_quelle_erlaubt=True,
        forecast_hourly_wh=_STUNDEN_W if mit_stunden else None,
        speicher_volumen_l=300.0 if mit_stunden else None,
    )


def test_morgens_wird_auf_pv_gewartet():
    """Um 07:00 reicht der Rest-Ertrag - die Regel wartet."""
    r = _abweichung(7)
    assert r.einschalten is None, (
        "Um 07:00 ist genug Ertrag zu erwarten - es muss gewartet "
        f"werden. Grund: {r.grund}")


def test_mittags_wird_gewartet_wenn_ertrag_reicht():
    """Um 09:00 reicht der Rest-Ertrag ebenfalls noch."""
    r = _abweichung(9)
    assert r.einschalten is None, (
        "Um 09:00 reicht der Rest-Ertrag - es muss gewartet werden. "
        f"Grund: {r.grund}")


def test_spaet_am_morgen_wird_nicht_mehr_gewartet():
    """Kernfall: Um 11:00 kommen nur noch 6100 Wh - das reicht fuer
    den Bedarf (5931 Wh) NICHT mit der 30-%-Reserve. Die Regel darf
    die Wartezeit nicht verbraten, sondern muss normal heizen."""
    r = _abweichung(11)
    assert r.einschalten is True, (
        "Um 11:00 reicht der Rest-Ertrag nicht mehr - die Regel muss "
        f"heizen. Grund: {r.grund}")


def test_ohne_stundenprognose_bleibt_das_alte_verhalten():
    """Ohne Stundenprognose gibt es keine Rest-Berechnung - dann bleibt
    das alte Verhalten (fail-open: lieber zu spaet als zu frue)."""
    r = _abweichung(11, mit_stunden=False)
    assert r.einschalten is None, (
        "Ohne Stundenprognose muss die Regel wie bisher warten. "
        f"Grund: {r.grund}")


def test_ohne_volumen_bleibt_das_alte_verhalten():
    """Ohne Speichervolumen ist der Bedarf nicht berechenbar."""
    abw = AbweichungConfig()
    r = evaluate_abweichung(
        abw=abw,
        temp_dict={"unten": 25.0, "oben": 40.0, "mittig": 30.0},
        kompressor_ein=False,
        now_hour=11,
        nachtsperre_start=19,
        nachtsperre_ende=8,
        feedin_watt=0.0,
        soc=50.0,
        bademodus_aktiv=False,
        forecast_today_wh_qm=3000.0,
        fc_ratio=1.0,
        pv_acpower=0.0,
        battery_power=0.0,
        solar_stale=False,
        notfall_einschalten_c=25.0,
        pv_quelle_erlaubt=True,
        forecast_hourly_wh=_STUNDEN_W,
        speicher_volumen_l=None,
    )
    assert r.einschalten is None, (
        "Ohne Volumen bleibt das alte Verhalten. Grund: " + str(r.grund))


def test_notfallschutz_bleibt_vorrangig():
    """Der Notfall-Tiefenschutz darf NIE durch das Warten blockiert
    werden - egal wie schlecht der Rest-Ertrag aussieht."""
    r = _abweichung(11, unten=20.0)
    # 20C liegt ueber dem Notfallwert 25C? -> nein, darunter.
    # Dann muss der Notfall greifen, nicht das Warten.
    assert r.einschalten is True, (
        "Unter dem Notfallwert muss geheizt werden. Grund: " + str(r.grund))


class TestVerdrahtung:
    """Die Stundenprognose muss BIS ZUR REGEL durchkommen.

    Der Funktionstest oben prueft `evaluate_abweichung` direkt. Die
    Verdrahtung (priority_control_logic -> bewerte_alle_regeln ->
    evaluate_abweichung) ist eine eigene Fehlerquelle - dort wurde
    schon einmal ein Aufruf entfernt (BUGFIX 21.10.2026, Safety).
    """

    def test_bewerte_alle_regeln_hat_den_parameter(self):
        import inspect
        from priority_control import bewerte_alle_regeln
        params = inspect.signature(bewerte_alle_regeln).parameters
        assert "forecast_hourly_wh" in params, (
            "bewerte_alle_regeln muss die Stundenprognose annehmen "
            "koennen")

    def test_der_aufrufer_reicht_sie_weiter(self):
        """Der Aufrufer in priority_control_logic muss die Stundenprognose
        tatsaechlich uebergeben - nicht nur den Parameter besitzen."""
        import pathlib as _pl
        quelle = _pl.Path(__file__).resolve().parent.parent / "priority_control_logic.py"
        text = quelle.read_text(encoding="utf-8")
        # In der Datei gibt es mehrere Aufrufstellen. Entscheidend ist,
        # dass KEINE davon die Stundenprognose auf None setzt - eine
        # einzelne "None"-Stelle wuerde den Check im Zweig toeten.
        zeilen = [z.strip() for z in text.splitlines()
                  if "forecast_hourly_wh=" in z]
        assert zeilen, (
            "Der Aufruf von bewerte_alle_regeln fuehrt die Stundenprognose "
            "gar nicht - der Rest-Ertrag-Check wuerde nie greifen")
        schlechte = [z for z in zeilen if "forecast_hourly_wh=None" in z]
        assert not schlechte, (
            "Die Stundenprognose wird auf None gesetzt: " + str(schlechte))
