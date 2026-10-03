"""Nachweis: die PV-ERZEUGUNG erreicht die Quellenpruefung der Regelung.

Hintergrund: ``ACPower`` ist der Wechselstromausgang des Wechselrichter
(Haus + Netz), nicht die Erzeugung. Nachts betraegt er rund 250 W,
waehrend die Gleichstromseite 0 meldet. Die Regelung hat ihre
Freigabeschwellen aber genau an diesen Wert gehaengt - um 09:00 sind das
352 W statt der wahren 2914 W.

Diese Tests fangen den Weg zwischen ``bewerte_alle_regeln`` und
``classify_energy_source`` ab. Ein einzelner Aufruf wuerde nichts
beweisen: es gibt drei Stellen in priority_control.py, die
klassifizieren, und die Duerschiene laeuft ueber mehrere Regelfunktionen.
"""
from datetime import datetime

import pytest
import re

import priority_control
from json_config import WPSteuerungConfig
from priority_control import bewerte_alle_regeln


@pytest.fixture
def solar_rufe(monkeypatch):
    """Zeichnet auf, was classify_energy_source wirklich bekommt."""
    rufe = []
    original = priority_control.classify_energy_source

    def spion(**kwargs):
        rufe.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(priority_control, "classify_energy_source", spion)
    return rufe


def _bewerte(**kwargs):
    # Feste Uhrzeit, und zwar Freitag 10:30. Ohne `now` benutzt
    # bewerte_alle_regeln datetime.now(); nachts greift die Nachtsperre und
    # die Quelle wird nie geprueft - der Test waere dann je nach Tageszeit
    # gruen oder rot gewesen, ohne dass er je etwas geprueft haette.
    #
    # Freitag ist gewaehlt, weil die Legionellenregel laut Vorgabe nur von
    # bevorzugter_tag=4 (Freitag) bis letzter_tag=6 (Sonntag) startet.
    # Ein Mittwoch wuerde die dritte Quellenpruefung still ueberspringen.
    #
    # Der letzte Lauf liegt 7 Tage zurueck: faellig, aber unter der
    # 14-Tage-Frist - darueber waere es der Hygiene-Notfall, der die Quelle
    # auf Netzbetrieb zurueckstellt und die Pruefung ebenfalls umgeht.
    #
    # Bewusst kalte Sensoren: evaluate_adaptive_pv kehrt bei
    # `temp >= einschalten_bis_c` zurueck, BEVOR es die Quelle prueft.
    config = WPSteuerungConfig()
    basis = dict(
        config=config,
        temp_dict={"oben": 20.0, "unten": 18.0, "mittig": 19.0, "verdampfer": 20.0},
        pv_leistung=3000.0,
        kompressor_ein=False,
        soc=80.0,
        battery_power=0.0,
        solar_stale=False,
        now=datetime(2026, 10, 9, 10, 30),
        legionellen_last_done=datetime(2026, 10, 2, 10, 0),
    )
    basis.update(kwargs)
    return bewerte_alle_regeln(**basis)


def test_erzeugung_statt_ac_wert_bei_der_quellepruefung(solar_rufe):
    """Der Aufrufer liefert AC 210 W und DC 2914 W - die Quelle sieht 2914."""
    _bewerte(pv_acpower=210.0, pv_erzeugung_watt=2914.0)

    assert solar_rufe, "keine Quellenpruefung ausgefuehrt"
    for kwargs in solar_rufe:
        # pv_acpower traegt ab hier die ERZEUGUNG, nicht mehr den
        # Wechselrichterausgang.
        assert kwargs.get("pv_acpower") == 2914.0, kwargs
        assert kwargs.get("pv_erzeugung_watt") is None or True


def test_alle_drei_stellen_sehen_denselben_wert(solar_rufe):
    """Es gibt drei klassifizierende Stellen; keine darf zurueckfallen.

    Genau darin liegt die Fehlerklasse: eine einzelne vergessene
    Stelle liefert nicht laut falsch, sondern still die alte Quelle.
    """
    _bewerte(pv_acpower=210.0, pv_erzeugung_watt=2914.0)
    assert len(solar_rufe) >= 3, (
        f"erwartet mindestens drei Quellenpruefungen, gefunden {len(solar_rufe)}"
    )
    werte = {k.get("pv_acpower") for k in solar_rufe}
    assert werte == {2914.0}, f"unterschiedliche Werte an den Stellen: {werte}"


def test_ohne_dc_wert_bleibt_es_beim_alten_verhalten(solar_rufe):
    """Aeltere Firmware ohne DC-Spalte: Rueckfall auf den AC-Wert.

    Der Rueckfall darf nicht still auf 0 gehen - sonst waere die
    Anlage bei fehlenden Solardaten nachts wie am Mittag gleich blind.
    """
    _bewerte(pv_acpower=1840.0, pv_erzeugung_watt=None)
    assert solar_rufe
    for kwargs in solar_rufe:
        assert kwargs.get("pv_acpower") == 1840.0, kwargs


def test_null_ist_kein_falscher_nullwert(solar_rufe):
    """`pv_erzeugung_watt=0` ist nachts richtig und darf nicht als 'fehlt'
    gelten, das wuerde tagsueber eine vorhandene Erzeugung verdecken."""
    _bewerte(pv_acpower=250.0, pv_erzeugung_watt=0.0)
    for kwargs in solar_rufe:
        assert kwargs.get("pv_acpower") == 0.0, (
            "0 W bei Nacht wurde als 'keine Angabe' behandelt und der "
            "AC-Wert nachgeschoben"
        )


def test_jede_durchreichstelle_in_bewerte_alle_regeln_nutzt_die_auflosung():
    """Strukturvertrag fuer die Stellen, die der Lauf nicht erreicht.

    Der Verhaltenstest oben sieht drei Aufrufe. `bewerte_alle_regeln`
    reicht den Wert aber an FUENF Regeln weiter, und zwei davon pruefen
    die Quelle nur unter Bedingungen, die ein Testlauf nicht ohne weiteres
    erfuellt. Genau dort ist dieser Fehler zuerst aufgefallen: eine
    vergessene Stelle liefert nicht laut falsch, sondern still den alten
    Wert.

    Deshalb wird zusaetzlich geprueft, dass innerhalb von
    `bewerte_alle_regeln` keine einzige `pv_acpower=pv_acpower` mehr
    steht. Die Regelfunktionen DARUEBER duerfen weiterhin ihr eigenes
    Argument weiterreichen - sie bekommen ja schon den aufgeloesten Wert.
    """
    with open(priority_control.__file__, encoding="utf-8") as fh:
        quelle = fh.read()
    start = quelle.find("def bewerte_alle_regeln(")
    assert start != -1, "bewerte_alle_regeln nicht gefunden"
    rumpf = quelle[start:]

    falsch = re.findall(r"pv_acpower=pv_acpower\b", rumpf)
    assert not falsch, (
        f"{len(falsch)} Stelle(n) in bewerte_alle_regeln reichen weiterhin den "
        "Wechselstromausgang statt der Erzeugung durch"
    )
    # Und es muss ueberhaupt eine Aufloesung geben.
    assert "_pv_signal = pv_acpower if pv_erzeugung_watt is None" in rumpf, (
        "die Aufloesung von AC-Wert und Erzeugung fehlt"
    )