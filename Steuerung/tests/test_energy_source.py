"""Regressionen fuer die zentrale Energiequellen-Klassifikation."""
from energy_source import (
    Energiequelle,
    batterie_entladung_watt,
    batterie_ladung_watt,
    classify_energy_source,
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
