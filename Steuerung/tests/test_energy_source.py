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
        battery_discharge_watt=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert pv_ohne_netzkauf.quelle is Energiequelle.PV
    assert pv_ohne_netzkauf.verfuegbar is True

    pv_mit_netzkauf = classify_energy_source(
        pv_acpower=800.0,
        feedin_watt=-100.0,
        battery_discharge_watt=0.0,
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
        battery_discharge_watt=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.quelle is Energiequelle.NETZ
    assert ergebnis.verfuegbar is False


def test_batterie_rohwert_wird_ueber_vorzeichen_normiert():
    assert batterie_entladung_watt(750.0) == 750.0
    assert batterie_ladung_watt(-750.0) == 750.0
    assert batterie_entladung_watt(-750.0) == 0.0

    ergebnis = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=0.0,
        battery_discharge_watt=600.0,
        soc=95.0,
        battery_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.quelle is Energiequelle.BATTERIE
    assert ergebnis.verfuegbar is True


def test_stale_und_fehlende_daten_sind_fail_safe():
    stale = classify_energy_source(
        pv_acpower=1000.0,
        feedin_watt=1000.0,
        battery_discharge_watt=500.0,
        soc=95.0,
        solar_stale=True,
    )
    assert stale.quelle is Energiequelle.STALE
    assert stale.verfuegbar is False

    unbekannt = classify_energy_source(
        pv_acpower=None,
        feedin_watt=None,
        battery_discharge_watt=None,
        soc=None,
    )
    assert unbekannt.quelle is Energiequelle.NETZ
    assert unbekannt.verfuegbar is False
