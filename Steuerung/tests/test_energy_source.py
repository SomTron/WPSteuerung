"""Regressionen fuer die zentrale Energiequellen-Klassifikation."""
import pytest

from energy_source import (
    Energiequelle,
    batterie_entladung_watt,
    batterie_ladung_watt,
    classify_energy_source,
    hausverbrauch_watt,
    pv_ueberschuss_watt,
)


def test_pv_ist_nur_mit_echter_pv_leistung_und_ohne_netzkauf_erlaubt():
    pv_ohne_netzkauf = classify_energy_source(
        pv_acpower=800.0,
        feedin_watt=100.0,
        batpower_raw=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert pv_ohne_netzkauf.quelle is Energiequelle.PV
    assert pv_ohne_netzkauf.verfuegbar is True

    pv_mit_netzkauf = classify_energy_source(
        pv_acpower=800.0,
        feedin_watt=-100.0,
        batpower_raw=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert pv_mit_netzkauf.quelle is Energiequelle.NETZ
    assert pv_mit_netzkauf.verfuegbar is False


def test_feed_in_allein_ist_keine_pv_erzeugung():
    ergebnis = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=1500.0,
        batpower_raw=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.quelle is Energiequelle.NETZ
    assert ergebnis.verfuegbar is False


def test_batterie_rohwert_wird_ueber_vorzeichen_normiert():
    """batPower > 0 = LADUNG, batPower < 0 = ENTLADUNG (Messtabelle unten)."""
    assert batterie_ladung_watt(750.0) == 750.0
    assert batterie_entladung_watt(-750.0) == 750.0
    # Gegenrichtung: die jeweils andere Richtung liefert 0, nicht den Betrag.
    assert batterie_entladung_watt(750.0) == 0.0
    assert batterie_ladung_watt(-750.0) == 0.0
    # Ladung ist KEINE Entladung - genau der Fehler, der vorlag.
    assert batterie_entladung_watt(3582.0) == 0.0
    assert batterie_ladung_watt(3582.0) == 3582.0

    ergebnis = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=0.0,
        # Rohwert -600 W = die Batterie SPEIST (Vorzeichen: negativ).
        batpower_raw=-600.0,
        soc=95.0,
        battery_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.quelle is Energiequelle.BATTERIE
    assert ergebnis.verfuegbar is True


# Echte Messpaare (batpower_w, SOC-Differenz zur Folgezeile) aus den
# Betriebslogs unter logs/*/entscheidungs_log.jsonl, August bis Dezember 2026.
# Sie belegen die Vorzeichen-Konvention unabhaengig von jedem Kommentar im Code.
_LADEN_SOC_STEIGT = [
    (916.0, +1.0), (1574.0, +4.0), (1330.0, +1.0),
    (867.0, +1.0), (3582.0, +17.0), (3632.0, +3.0),
]
_ENTLADEN_SOC_FAELLT = [
    (-1331.0, -7.0), (-1354.0, -1.0), (-1824.0, -1.0),
    (-477.0, -5.0), (-949.0, -1.0), (-355.0, -3.0),
]


def test_vorzeichen_stimmt_mit_echten_messreihen_ueberein():
    """Regression: das Vorzeichen von ``batPower`` war dokumentiert invertiert.

    Ein Datensatz umfasste 11.078 Live-Logzeilen und 254.305 CSV-Zeilen.
    Der Median von ``batPower`` lag bei steigender SOC bei +1180 W bis
    +1335 W, bei fallender SOC bei -330 W bis -355 W. Die Normalisierung
    muss daher LADEN als positive Rohwerte lesen.
    """
    for rohwert, _delta in _LADEN_SOC_STEIGT:
        assert batterie_ladung_watt(rohwert) > 0.0, (
            f"{rohwert} W bei steigender SOC muss als Ladung gelten"
        )
        assert batterie_entladung_watt(rohwert) == 0.0

    for rohwert, _delta in _ENTLADEN_SOC_FAELLT:
        assert batterie_entladung_watt(rohwert) > 0.0, (
            f"{rohwert} W bei fallender SOC muss als Entladung gelten"
        )
        assert batterie_ladung_watt(rohwert) == 0.0


def test_hausverbrauch_aus_der_wechselrichterbilanz():
    """Hausverbrauch = acpower - feedinpower - batPower.

    Die Formel ist vom Betreiber vorgeschlagen und an 254.305 CSV-Messreihen
    geprueft: Median 353 W, Nacht 00-04 Uhr ~270 W, Mittagsspitze 1776-2550 W
    (darin die Waermepumpe) - ein plausibles Tagesprofil.
    """
    # Realer Fall aus dem Betriebslog 02.10.2026, 08:28 (MinTemp-Mittag):
    # PV 1200 W, nichts eingespeist, Batterie laedt mit 750 W.
    assert hausverbrauch_watt(1200.0, 0.0, 750.0) == pytest.approx(450.0)
    # Nacht: keine PV, Netzbezug 320 W -> Hausverbrauch 320 W
    assert hausverbrauch_watt(0.0, -320.0, 0.0) == pytest.approx(320.0)
    # Batterie SPEIST 200 W: das Haus nimmt PV + Batterie auf, also 900 W.
    # Achtung: hier ist hausverbrauch > acpower - genau deshalb gilt die
    # Identitaet "haus + ueber == acpower" bei Entladung nicht.
    assert hausverbrauch_watt(700.0, 0.0, -200.0) == pytest.approx(900.0)
    # Messversatz kann leicht negative Werte ergeben -> auf 0 begrenzt
    assert hausverbrauch_watt(300.0, 10.0, 400.0) == 0.0
    # Fehlende Messung bleibt None, nicht 0
    assert hausverbrauch_watt(None, 0.0, 0.0) is None
    assert hausverbrauch_watt(500.0, None, 0.0) is None
    assert hausverbrauch_watt(500.0, 0.0, None) is None


def test_pv_ueberschuss_entspricht_einspeisung_und_ladung():
    """Aus der Bilanz folgt Ueberschuss = feedin + batPower."""
    # Batterie laedt mit 750 W, nichts eingespeist -> 750 W Ueberschuss
    assert pv_ueberschuss_watt(1200.0, 0.0, 750.0) == pytest.approx(750.0)
    # 3000 W eingespeist, Batterie laedt mit 500 W -> 3500 W Ueberschuss
    assert pv_ueberschuss_watt(4000.0, 3000.0, 500.0) == pytest.approx(3500.0)
    # PV deckt das Haus exakt -> kein Ueberschuss
    assert pv_ueberschuss_watt(800.0, 0.0, 0.0) == 0.0
    # Batterie speist, kein Ueberschuss (Netzbezug liegt vor)
    assert pv_ueberschuss_watt(300.0, -400.0, -500.0) == 0.0


def test_bilanz_ist_intern_konsistent():
    """Die ausnahmslos gueltige Identitaet: Ueberschuss == max(0, feedin + batPower).

    ``hausverbrauch + ueberschuss == acpower`` gilt NUR bei ladender oder
    ruhender Batterie. Bei Entladung nimmt das Haus mehr auf als die PV
    erzeugt, weil die Batterie zusaetzlich einspeist - das ist korrekt und
    war die Ursache fuer einen zunächst falschen Testerwartungswert.
    """
    faelle = [
        (1200.0, 0.0, 750.0), (4000.0, 3000.0, 500.0), (800.0, 0.0, 0.0),
        (700.0, 0.0, -200.0), (50.0, -600.0, -900.0), (300.0, 10.0, 400.0),
    ]
    for ac, fi, ba in faelle:
        ueber = pv_ueberschuss_watt(ac, fi, ba)
        assert ueber == pytest.approx(max(0.0, fi + ba)), (ac, fi, ba)

    # Nur bei ladender/rruhender Batterie gilt zusaetzlich die Bilanz.
    for ac, fi, ba in faelle:
        if ba < 0 or ac - fi - ba < 0:
            # Entladung: Bilanz traegt nicht. Messversatz: Hausverbrauch
            # wurde auf 0 begrenzt, dann gilt sie ebenfalls nicht.
            continue
        haus = hausverbrauch_watt(ac, fi, ba)
        ueber = pv_ueberschuss_watt(ac, fi, ba)
        assert haus + ueber == pytest.approx(ac), (ac, fi, ba)


def test_rohwert_wird_nur_einmal_normalisiert():
    """Regression: der Wert darf nicht doppelt normalisiert werden.

    ``state.solar.battery_discharge_watt`` ist BEREITS normalisiert. Wurde es
    als ``batpower_raw`` uebergeben, normalisierte classify_energy_source ein
    zweites Mal - Vorzeichen und Betrag hoben sich auf und JEDE Entladung
    wurde als 0 W gemeldet, die Batteriequelle war damit unerreichbar.
    """
    roh = -1200.0
    # Einmal normalisiert: das ist die Entladung, die die Klassifikation sehen muss.
    einmal = batterie_entladung_watt(roh)
    assert einmal == 1200.0

    ergebnis = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=0.0,
        batpower_raw=roh,
        soc=95.0,
        battery_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.batterie_entladung_watt == 1200.0
    assert ergebnis.quelle is Energiequelle.BATTERIE
    assert ergebnis.verfuegbar is True


def test_ladende_batterie_ist_keine_entladungsquelle():
    """Kernfehler: eine LADENDE Batterie darf nicht als Quelle taugen.

    Vor der Korrektur meldete ``batterie_entladung_watt(3582) == 3582``
    und die Quelle wurde als Batterie eingestuft - obwohl die Batterie den
    Solarueberschuss gerade aufnimmt und gar nicht liefern kann.
    """
    beim_laden = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=0.0,
        # Rohwert +3582 W = die Batterie LAEDT gerade.
        batpower_raw=3582.0,
        soc=100.0,
        battery_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzkauf_watt=-50.0,
    )
    assert beim_laden.quelle is not Energiequelle.BATTERIE
    assert beim_laden.verfuegbar is False


def test_stale_und_fehlende_daten_sind_fail_safe():
    stale = classify_energy_source(
        pv_acpower=1000.0,
        feedin_watt=1000.0,
        batpower_raw=-500.0,
        soc=95.0,
        solar_stale=True,
    )
    assert stale.quelle is Energiequelle.STALE
    assert stale.verfuegbar is False

    unbekannt = classify_energy_source(
        pv_acpower=None,
        feedin_watt=None,
        batpower_raw=None,
        soc=None,
    )
    assert unbekannt.quelle is Energiequelle.NETZ
    assert unbekannt.verfuegbar is False
