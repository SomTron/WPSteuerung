"""Simulation: die Forecast-Kalibrierung sieht wieder die Erzeugung.

_kalibriere_forecast berechnet

    ratio = erzeugte_Wh / (prognose_Wh_per_m2 * flaeche_m2)

Bis eben lief in _day_pv_wh der Wechselstromausgang ein, also Haus
plus Netz. Was die Batterie aufnimmt, taucht dort nicht auf - der
Quotient bleibt zu niedrig.

Hier wird der 03.10.2026 durchgespielt: Stundenmittel der
Gleichstromseite gegen den Wechselstromausgang desselben Tages,
beides aus den Betriebsdaten auf dem Pi.

Ein Stundenmittel in Watt ist bereits Wattstunden (W x 1 h). Deshalb
steht unten KEIN Faktor 3600 - im ersten Entwurf stand einer, und die
Erwartung war um den Faktor 3600 zu gross.
"""

from datetime import datetime, timedelta

import pytest

import learning_engine as le

# Stundenmittel der Gleichstromseite (W) - die tatsaechliche Erzeugung.
# Summe 48.646 Wh.
PV_TAGESPROFIL = {
    7: 150, 8: 1305, 9: 2914, 10: 4796, 11: 6051, 12: 6881,
    13: 7396, 14: 6889, 15: 5863, 16: 4370, 17: 1832, 18: 199,
}

# Wechselstromausgang desselben Tages, aus den echten Logzeilen
# rekonstruiert: vormittags laedt die Batterie, der Ausgang bleibt bei
# rund 210 W, waehrend 1300-4800 W erzeugt werden. Ab 12 Uhr ist die
# Batterie voll, der Ausgang folgt der Erzeugung. Abends entlaedt sie.
# Summe 40.571 Wh, also 17 % weniger als die Erzeugung.
AC_TAGESPROFIL = {
    0: 250, 1: 244, 2: 224, 3: 232, 4: 243, 5: 607, 6: 958,
    7: 895, 8: 213, 9: 210, 10: 206, 11: 1500, 12: 6749,
    13: 7222, 14: 6723, 15: 5735, 16: 4285, 17: 1851, 18: 548,
    19: 457, 20: 334, 21: 353, 22: 278, 23: 254,
}

SCHRITT_SEK = 600            # 10 Minuten
FLAECHE_QM = 10.0
PROGNOSE_WH_QM = 4130.0       # so lautete die Prognose fuer den 03.10.

# Der erste 10-Minuten-Schritt hat dt=0 und geht nicht ein; das kostet
# beim AC-Profil 41.7 Wh (Wert 250 W), beim DC-Profil nichts (Wert 0 W).
DC_WH = 48646.0
AC_WH = 40571.0 - 250.0 * SCHRITT_SEK / 3600.0


def _engine(tmp_path, name="engine"):
    """Eigene Engine mit eigener Datei.

    LearningEngine LAEDT den Datenpfad beim Start. Zwei Engines auf
    derselben Datei beginnen die zweite mit dem Ergebnis der ersten, und
    die zweite Kalibrierung mittelt per EWMA weiter - im ersten Entwurf
    standen zwei Engines auf einer Datei. Das ergab 1.111 statt 0.98 und
    sah erst nach einem Systemfehler aus.
    """
    return le.LearningEngine(data_path=str(tmp_path / (name + ".json")))


def _spiele_tag(engine, profil, *, mit_erzeugung,
                prognose_wh_qm=PROGNOSE_WH_QM):
    """Simuliert den Tag in 10-Minuten-Schritten."""
    jetzt = datetime(2026, 10, 3, 0, 0)
    for _ in range(24 * 6):
        wert = profil.get(jetzt.hour, 0.0)
        kwargs = ({"pv_erzeugung_watt": wert} if mit_erzeugung
                  else {"pv_acpower_watt": wert})
        engine.update(
            now=jetzt,
            temp_dict={"unten": 40.0, "oben": 45.0},
            compressor_is_on=False,
            feedin_watt=0.0,
            soc=80.0,
            forecast_today_wh_qm=prognose_wh_qm,
            pv_array_size_qm=FLAECHE_QM,
            **kwargs,
        )
        jetzt = jetzt + timedelta(seconds=SCHRITT_SEK)
    return engine


def test_erzeugung_liefert_den_richtigen_faktor(tmp_path):
    engine = _engine(tmp_path)
    _spiele_tag(engine, PV_TAGESPROFIL, mit_erzeugung=True)

    erwartet = DC_WH / (PROGNOSE_WH_QM * FLAECHE_QM)     # ~1.178
    assert engine.data.forecast_ratio_samples == 1
    assert engine.data.forecast_ratio == pytest.approx(erwartet, rel=0.01), (
        f"Faktor {engine.data.forecast_ratio}, erwartet ~{erwartet:.3f}"
    )


def test_wechselstrom_wuerde_die_prognose_zu_niedrig_bewerten(tmp_path):
    """Gegenprobe: derselbe Tag, aber mit dem Wechselstromausgang.

    Das ist die Lage vor der Korrektur. Die Groessenordnung ist wichtig:
    die Wirkung liegt bei rund 17 %, nicht bei einem Faktor 1,8. An
    Tagen mit starkem Batterie-Zyklus ist sie groesser, an Tagen mit
    wenig Laden kleiner. Diese Zahl ist der 03.10.
    """
    engine = _engine(tmp_path)
    _spiele_tag(engine, AC_TAGESPROFIL, mit_erzeugung=False)

    # Nicht AC_WH/Prognose: die Engine kalibriert ab Stunde 20
    # (forecast_kalibrierung_ab_stunde), der Abend zaehlt also nie hinein.
    # Der genaue Wert haengt davon ab, in welchem Zehntelschritt die
    # Kalibrierung greift - deshalb die Bandbreite statt eines Festwerts.
    assert 0.90 < engine.data.forecast_ratio < 1.0, (
        f"die Gegenprobe liegt nicht darunter ({engine.data.forecast_ratio}) "
        "- dann prueft der Test nichts"
    )


def test_unterschied_entspricht_dem_batterieanteil(tmp_path):
    """Der alte Fehler ist so gross, wie die Batterie an dem Tag laedt."""
    gut = _engine(tmp_path, "dc")
    _spiele_tag(gut, PV_TAGESPROFIL, mit_erzeugung=True)
    schlecht = _engine(tmp_path, "ac")
    _spiele_tag(schlecht, AC_TAGESPROFIL, mit_erzeugung=False)

    anteil = AC_WH / DC_WH          # ~0.833
    verhaeltnis = gut.data.forecast_ratio / schlecht.data.forecast_ratio
    # Die Wirkung entspricht grob dem Batterieanteil (~17 %). Exakt
    # reziprok sind die beiden Faktoren nicht: die Engine kalibriert ab
    # Stunde 20, der Abend fehlt also in beiden Summen und bei der
    # Erzeugung staerker als beim Ausgang. Deshalb eine Bandbreite.
    assert 1.15 < verhaeltnis < 1.40, (
        f"Erzeugung {gut.data.forecast_ratio} gegen AC "
        f"{schlecht.data.forecast_ratio} = {verhaeltnis:.3f}, "
        f"Batterieanteil {anteil:.3f}"
    )


def test_kein_fehler_bei_nulll_erzeugung(tmp_path):
    """Eine Nacht ohne Sonne darf die Kalibrierung nicht zerlegen.

    ``_day_pv_wh <= 50`` fuehrt zum bewussten Ueberspringen. Wichtig ist,
    dass 0 W nicht als Fehler behandelt wird und kein Faktor entsteht.
    """
    engine = _engine(tmp_path)
    _spiele_tag(engine, {}, mit_erzeugung=True)

    assert engine.data.forecast_ratio_samples == 0, (
        "eine Nacht ohne Erzeugung hat den Faktor trotzdem veraendert"
    )
    assert engine.data.forecast_ratio == 1.0


def test_mehrere_tage_glaetten_zum_mittel(tmp_path):
    """Nach mehreren Tagen muss der Faktor einschwingen, nicht laufen."""
    # EINE Engine ueber alle Tage - sonst beginnt jeder Tag neu und die
    # Glaettung findet nicht statt (im ersten Entwurf stand hier pro Tag
    # eine frische Engine auf frischer Datei, samples blieb bei 1).
    engine = _engine(tmp_path, "serie")
    letzter = None
    for tag in range(4):
        jetzt = datetime(2026, 10, 3 + tag, 0, 0)
        for _ in range(24 * 6):
            engine.update(
                now=jetzt,
                temp_dict={"unten": 40.0, "oben": 45.0},
                compressor_is_on=False,
                feedin_watt=0.0,
                soc=80.0,
                forecast_today_wh_qm=PROGNOSE_WH_QM,
                pv_array_size_qm=FLAECHE_QM,
                pv_erzeugung_watt=PV_TAGESPROFIL.get(jetzt.hour, 0.0),
            )
            jetzt = jetzt + timedelta(seconds=SCHRITT_SEK)
        assert engine.data.forecast_ratio_samples == tag + 1
        letzter = engine

    assert 1.0 < letzter.data.forecast_ratio < 1.3, (
        f"der Faktor ist nach vier Tagen nicht eingeschwungen: "
        f"{letzter.data.forecast_ratio}"
    )
