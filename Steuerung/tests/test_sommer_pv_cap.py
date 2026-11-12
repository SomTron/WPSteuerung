"""Tests: Sommermodus senkt auch die PV-Abschaltpunkte (Nutzeranforderung:
"bei laengeren Perioden sehr schoenen Wetters soll der Buffer nicht
komplett aufgebaut werden ... es soll die Max Temperatur minimiert werden").
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402
import priority_control_logic as pcl  # noqa: E402


def test_sommer_cap_pv_regeln():
    from json_config import PVRegel
    config = WPSteuerungConfig()
    config.pv_regeln = [
        PVRegel(name="PV_unten", prioritaet=81, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0),
        PVRegel(name="PV_mitte", prioritaet=80, temperaturfuehler="mitte",
                pv_schwelle_watt=700.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0),
    ]
    config.adaptive_pv.tmax_c = 48.0

    pcl.wende_sommer_offset_an(config)

    for pv in config.pv_regeln:
        assert pv.ausschalten_bei_c == 46.0  # 48 - 2 (pv_ausschalt_offset_c)
    assert config.adaptive_pv.tmax_c == 46.0
    assert config.abweichung.solltemperatur_c == 39.0  # Default-Soll 42 - 3


def test_sommer_cap_klemmt_an_einschaltpunkt():
    """Sehr niedriger Einschaltpunkt darf nicht unterlaufen werden."""
    from json_config import PVRegel
    config = WPSteuerungConfig()
    config.pv_regeln = [
        PVRegel(name="PV_x", prioritaet=80, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=45.0, ausschalten_bei_c=47.0),
    ]
    pcl.wende_sommer_offset_an(config)
    # 47-2=45 <= ein+2 -> Klemme auf 47? Nein: max(45, 45+2)=47 bleibt 47
    assert config.pv_regeln[0].ausschalten_bei_c == 47.0


def test_pv_shaping_stoppt_frueher_im_sommer():
    """End-to-End: Sommermodus aktiv -> evaluate_pv_regel sagt bei 46.5C AUS."""
    import priority_control as pc
    from json_config import PVRegel

    config = WPSteuerungConfig()
    config.pv_regeln = [
        PVRegel(name="PV_unten", prioritaet=81, temperaturfuehler="unten",
                pv_schwelle_watt=500.0, weiterlaufen_ab_pv_watt=50.0,
                einschalten_bei_c=42.0, ausschalten_bei_c=48.0),
    ]

    # Ohne Sommermodus: bei 46.5C laeuft die WP weiter (bis 48)
    erg_normal = pc.evaluate_pv_regel(
        config.pv_regeln[0], {"unten": 46.5}, pv_leistung=2000.0,
        kompressor_ein=True, now_hour=12, nachtsperre_start=19, nachtsperre_ende=8,
    )
    assert erg_normal.einschalten is True

    # Mit Sommer-Cap: bei 46.5C wird abgeschaltet (Grenze jetzt 46)
    config_kopie = config.model_copy(deep=True)
    pcl.wende_sommer_offset_an(config_kopie)
    erg_sommer = pc.evaluate_pv_regel(
        config_kopie.pv_regeln[0], {"unten": 46.5}, pv_leistung=2000.0,
        kompressor_ein=True, now_hour=12, nachtsperre_start=19, nachtsperre_ende=8,
    )
    assert erg_sommer.einschalten is False


def test_mindestgarantien_bleiben_von_sommer_cap_unberuehrt():
    """Die MindestTemp-Garantien duerfen vom Sommermodus nie abgesenkt werden."""
    config = WPSteuerungConfig()
    vor = [(e.name, e.min_temp_c) for e in config.mindest_temp.eintraege]
    pcl.wende_sommer_offset_an(config)
    nach = [(e.name, e.min_temp_c) for e in config.mindest_temp.eintraege]
    assert vor == nach


class _State:
    """Minimaler State-Mock fuer `_baue_effektive_config`."""

    def __init__(self, config, bademodus=False, urlaub=False, sommer=False):
        self.priority_config = config
        self.config = config
        self.bademodus_aktiv = bademodus
        self.urlaubsmodus_aktiv = urlaub
        self.sommer_modus_aktiv = sommer


def test_baue_effektive_config_bademodus_erhoeht_solltemperatur():
    """Bademodus hebt die Solltemperatur (aus der Config) an."""
    config = WPSteuerungConfig()
    soll = config.abweichung.solltemperatur_c
    erhoehung = config.bademodus.solltemperatur_erhoehung_c

    effektiv = pcl._baue_effektive_config(_State(config, bademodus=True))

    assert effektiv.abweichung.solltemperatur_c == soll + erhoehung
    # Original-Config bleibt unveraendert.
    assert config.abweichung.solltemperatur_c == soll


def test_baue_effektive_config_sommer_cap_und_bademodus_ausnahme():
    """Sommer-Offset wird angewandt - aber NICHT bei gleichzeitigem Bademodus."""
    config = WPSteuerungConfig()
    soll = config.abweichung.solltemperatur_c

    # Nur Sommer: Solltemperatur sinkt (Default -3C).
    effektiv_sommer = pcl._baue_effektive_config(_State(config, sommer=True))
    assert effektiv_sommer.abweichung.solltemperatur_c == soll - 3.0

    # Sommer + Bademodus: Sommer-Offset ausgesetzt, nur Bademodus-Erhoehung.
    config2 = WPSteuerungConfig()
    soll2 = config2.abweichung.solltemperatur_c
    erhoehung2 = config2.bademodus.solltemperatur_erhoehung_c
    effektiv_beide = pcl._baue_effektive_config(
        _State(config2, bademodus=True, sommer=True))
    assert effektiv_beide.abweichung.solltemperatur_c == soll2 + erhoehung2
