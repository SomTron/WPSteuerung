"""Regressionen fuer die zentrale Energiequellen-Klassifikation."""
import pytest

from energy_source import (
    Energiequelle,
    batterie_entladung_watt,
    batterie_ladung_watt,
    classify_energy_source,
    hausverbrauch_watt,
    pv_erzeugung_watt,
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


def test_feedin_beweist_solar_auch_ohne_erzeugungsanzeige():
    """Einspeisung gilt als Solar-NACHWEIS, nicht als Erzeugungsmessung.

    Frueher stand hier "Einspeisung allein ist keine PV-Erzeugung" -> Netz.
    Physikalisch ist das falsch: ein Wechselrichter kann nur einspeisen,
    wenn die PV mehr erzeugt als das Haus aufnimmt. Ein `acpower` von 0 bei
    1500 W Export ist eine kaputte Anzeige, kein Beweis fuer "kein Solar".

    Wichtig bleibt: die Einspeisung wird NICHT als Erzeugungswert verwendet
    (die PV-Erzeugung ist `acpower`), sondern nur als Nachweis, dass
    Solarstrom verfuegbar ist.
    """
    ergebnis = classify_energy_source(
        pv_acpower=0.0,
        feedin_watt=1500.0,
        batpower_raw=0.0,
        soc=50.0,
        pv_min_watt=50.0,
        max_netzkauf_watt=-50.0,
    )
    assert ergebnis.quelle is Energiequelle.PV
    assert ergebnis.verfuegbar is True
    # Die Erzeugungsanzeige bleibt "nicht verwertbar" - sie wird nicht
    # stillschweigend durch die Einspeisung ersetzt.
    assert "0W" in ergebnis.begruendung, ergebnis.begruendung


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
    """Hausverbrauch = acpower - feedin. Die Batterie gehoert NICHT hinein.

    ``acpower`` ist der Wechselstromausgang des Wechselrichter. Was er abgibt,
    fliesst ins Haus und zu einem Teil ins Netz; mehr kann er nicht abgeben.
    Die Batterie steht davor und aendert daran nichts.

    Bis September stand hier ``acpower - feedin - batPower``. Das setzt voraus,
    ``acpower`` sei die PV-Erzeugung - ist es nicht. Nachts liefert die
    DC-Seite 0 W, die AC-Seite rund 250 W.
    """
    # Betriebslog 03.10.2026, 10:49: Wechselrichter gibt 1823 W ab, davon
    # 1621 W eingespeist, Batterie laedt zusaetzlich mit 4029 W.
    # Das Haus hat also 1823 - 1621 = 202 W verbraucht. Die alte Formel
    # lieferte max(0, 1823 - 1621 - 4029) = 0 und behauptete, das Haus
    # verbrauche nichts - im Log steht ueber Stunden genau das.
    assert hausverbrauch_watt(1823.0, 1621.0, 4029.0) == pytest.approx(202.0)
    # Batterie laedt: der Ladestrom darf den Hausverbrauch nicht veraendern.
    assert hausverbrauch_watt(1200.0, 0.0, 750.0) == pytest.approx(1200.0)
    assert hausverbrauch_watt(1200.0, 0.0, 0.0) == pytest.approx(1200.0)
    # Batterie speist: ebenso ohne Wirkung auf den AC-Ausgang.
    assert hausverbrauch_watt(700.0, 0.0, -200.0) == pytest.approx(700.0)
    # Nacht, Netzbezug 320 W, Wechselrichter still -> Haus laeuft am Netz.
    assert hausverbrauch_watt(0.0, -320.0, 0.0) == pytest.approx(320.0)
    # Messversatz kann leicht negative Werte ergeben -> auf 0 begrenzt
    assert hausverbrauch_watt(300.0, 400.0, 0.0) == 0.0
    # Fehlende Messung bleibt None, nicht 0
    assert hausverbrauch_watt(None, 0.0, 0.0) is None
    assert hausverbrauch_watt(500.0, None, 0.0) is None


def test_pv_erzeugung_ist_die_gleichstromseite():
    """ACPower ist nicht die Erzeugung - nachts schon gar nicht.

    Gemessen auf dem Pi, Stundenmittel aus den Betriebsdaten:

        Stunde   DC (W)   AC (W)
        0-4          0    224-250
        9         2914      352
        12        6881     6749
    """
    # Nachts: DC sagt 0, AC behauptet 250 W.
    assert pv_erzeugung_watt(0.0, 0.0, 250.0, -200.0) == 0.0
    # Morgens: 1305 W erzeugt, davon nur 517 W ueber den AC-Ausgang.
    assert pv_erzeugung_watt(1305.0, None, 517.0, 788.0) == pytest.approx(1305.0)
    # Zwei Strings werden addiert.
    assert pv_erzeugung_watt(3000.0, 3900.0, 6700.0, 0.0) == pytest.approx(6900.0)
    # Ohne DC-Werte (aeltere Firmware) wird genahert: AC plus Ladung.
    assert pv_erzeugung_watt(None, None, 352.0, 2562.0) == pytest.approx(2914.0)
    # Nachts ohne DC: Batterie speist, es darf nichts "erzeugt" werden.
    assert pv_erzeugung_watt(None, None, 250.0, -200.0) == pytest.approx(250.0)
    assert pv_erzeugung_watt(None, None, None, 0.0) is None


def test_bilanz_ist_intern_konsistent():
    """Der Ueberschuss ist die Erzeugung abzueglich des Hauses.

    Aus der Bilanz folgt fuer die Naeherung ohne DC-Seite::

        PV = haus + feedin + batPower
           = haus + ueberschuss

    Die frueher geprueft Identitaet ``haus + ueber == acpower`` galt nur,
    weil beides aus derselben, falschen Annahme (``acpower`` = Erzeugung)
    gerechnet wurde. Sie war eine Tautologie, kein physikalisches Gesetz.
    """
    faelle = [
        (1200.0, 0.0, 750.0), (4000.0, 3000.0, 500.0), (800.0, 0.0, 0.0),
        (700.0, 0.0, -200.0), (50.0, -600.0, -900.0), (300.0, 10.0, 400.0),
    ]
    for ac, fi, ba in faelle:
        ueber = pv_ueberschuss_watt(ac, fi, ba)
        assert ueber == pytest.approx(max(0.0, fi + ba)), (ac, fi, ba)

    # Die Erzeugung traegt Haus und Ueberschuss - sofern die Klammer nicht
    # greift. Der Batterieanteil ist in beiden enthalten.
    for ac, fi, ba in faelle:
        haus = hausverbrauch_watt(ac, fi, ba)
        ueber = pv_ueberschuss_watt(ac, fi, ba)
        erzeugung = pv_erzeugung_watt(None, None, ac, ba)
        if haus is None or ueber is None or erzeugung is None:
            continue
        if fi + ba < 0 or ac - fi < 0:
            # Netzbezug: die Bilanz traegt nicht.
            continue
        assert erzeugung == pytest.approx(haus + ueber), (ac, fi, ba)


def test_erzeugungsschwelle_richtet_sich_nach_dc_seite():
    """Der 03.10.2026, 09:18-10:49: AC 352 W, DC 2914 W.

    Vorher stand an dieser Stelle der AC-Wert, der unter der
    300-W-Schwelle lag. Der zweite Nachweis (Ueberschuss) hat die Freigabe
    trotzdem erteilt - deshalb blieb der Fehler lange unbemerkt. Mit der
    DC-Seite ist es der erste Nachweis, und der Wert ist auch in der
    Begruendungsliefer.
    """
    mit_dc = classify_energy_source(
        pv_acpower=210.0,
        feedin_watt=0.0,
        batpower_raw=2562.0,
        soc=60.0,
        pv_min_watt=300.0,
        pv_erzeugung_watt=2914.0,
    )
    assert mit_dc.quelle is Energiequelle.PV
    assert mit_dc.verfuegbar is True
    assert "2914W" in mit_dc.begruendung, mit_dc.begruendung

    # Ohne DC bleibt das alte Verhalten: 210 W liegen unter der Schwelle,
    # und ohne Ladevorgang gibt es auch keinen Ueberschuss als zweiten
    # Nachweis. Genau so stand es im Betriebslog.
    ohne_dc = classify_energy_source(
        pv_acpower=210.0,
        feedin_watt=0.0,
        batpower_raw=0.0,
        soc=60.0,
        pv_min_watt=300.0,
    )
    assert ohne_dc.quelle is not Energiequelle.PV


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


def test_ueberschuss_oeffnet_die_freigabe_auch_bei_kleiner_erzeugung():
    """Regression 03.10.2026, 09:18-10:49: 1,5 h Solar ungenutzt.

    Der Wechselrichter meldet nachts rund 200 W Eigenverbrauch. Bleibt die
    Erzeugungsanzeige auf diesem Stand, verweigerte die Freigabe, obwohl
    2354-5488 W Ueberschuss ausgewiesen waren und unten bei 39 C lag -
    AdaptivePV blieb an der 300-W-Erzeugungsschwelle stehen.
    """
    pv = classify_energy_source(
        pv_acpower=210.0,        # Nachtstandby / stehen gebliebene Anzeige
        feedin_watt=0.0,
        batpower_raw=2354.0,     # Batterie laedt -> 2354 W Ueberschuss
        soc=41.0,
        pv_min_watt=300.0,       # AdaptivePV-Schwelle aus dem Log
        max_netzkauf_watt=-50.0,
    )
    assert pv.quelle is Energiequelle.PV
    assert pv.verfuegbar is True
    assert "2354W" in pv.begruendung, pv.begruendung
    # Beide Werte muessen genannt werden - die Diskrepanz ist das Symptom.
    assert "210W" in pv.begruendung, pv.begruendung


def test_nachts_bleibt_ohne_ueberschuss_gesperrt():
    """Gegenprobe: nachts darf die Freigabe NICHT aufgehen.

    Betriebslog 03.10.2026, 05:18: PV=255W, SOC fallend (Batterie speist
    das Haus). Ohne Ueberschuss bleibt es Netz.
    """
    nacht = classify_energy_source(
        pv_acpower=255.0,
        feedin_watt=0.0,
        batpower_raw=-239.0,     # Batterie SPEIST das Haus
        soc=89.0,
        pv_min_watt=300.0,
        battery_min_watt=50.0,
        soc_min_prozent=90.0,
        max_netzkauf_watt=-50.0,
    )
    assert nacht.quelle is not Energiequelle.PV
    assert nacht.verfuegbar is False


def test_einspeisung_allein_oeffnet_die_freigabe():
    """Klassischer Solarfall: Export vorhanden."""
    solar = classify_energy_source(
        pv_acpower=310.0,
        feedin_watt=2500.0,
        batpower_raw=0.0,
        soc=80.0,
        pv_min_watt=300.0,
    )
    assert solar.quelle is Energiequelle.PV
    assert solar.verfuegbar is True


def test_netzkauf_blockiert_auch_bei_ueberschuss_signal():
    """Der Netzzkauf-Vorbehalt bleibt zwingend."""
    trotz_ueberschuss = classify_energy_source(
        pv_acpower=210.0,
        feedin_watt=-400.0,      # Haus kauft Netzstrom
        batpower_raw=2354.0,
        soc=41.0,
        pv_min_watt=300.0,
        max_netzkauf_watt=-50.0,
    )
    assert trotz_ueberschuss.quelle is not Energiequelle.PV
    assert trotz_ueberschuss.verfuegbar is False


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
    # Sie ist KEINE Entladungsquelle - die kann gar nicht liefern.
    assert beim_laden.quelle is not Energiequelle.BATTERIE
    assert beim_laden.batterie_entladung_watt == 0.0
    # Sonst aber sehr wohl eine Solar-Quelle: die Batterie nimmt gerade
    # 3582 W auf, und ohne Netzbezug kommt das nur aus PV-Ueberschuss.
    assert beim_laden.quelle is Energiequelle.PV
    assert beim_laden.verfuegbar is True


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
