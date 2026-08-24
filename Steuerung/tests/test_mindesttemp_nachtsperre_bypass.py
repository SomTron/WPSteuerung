"""Tests: MinTemp-Garantie x Nachtsperre-Bypass (nachtsperre_ueberschreiben=False).

Anforderung: Um ~19 Uhr soll genug warmes Wasser zum Duschen sein
(Mitte >= 42 C) - aber NACH dem Duschen darf nachts nicht mehr
geheizt werden (kein PV -> Netzstrom); die Nachtsperre soll gelten.

Loesung: Eintraege mit "nachtsperre_ueberschreiben": false feuern nur
BIS zum Sperren-Beginn; innerhalb der Sperre bleiben sie stumm.
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402
import priority_control as pc  # noqa: E402


def baue_config_ohne_nacht_bypass():
    """Abend-Mitte wie im Einsatz: 42C-Garantie, KEIN Nachtsperren-Bypass."""
    config = WPSteuerungConfig()
    for eintrag in config.mindest_temp.eintraege:
        if eintrag.name == "Abend-Mitte":
            eintrag.min_temp_c = 42.0
            eintrag.nachtsperre_ueberschreiben = False
    return config


def bewerte_ohne_bypass(temp_dict, now_hour):
    config = baue_config_ohne_nacht_bypass()
    return pc.evaluate_mindesttemp(
        config.mindest_temp, temp_dict, now_hour,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
    )


def finde(ergebnisse, name_teil):
    return next(e for e in ergebnisse if name_teil in e.name)


# ── Verhalten vor / in der Nachtsperre ──

def test_abend_ohne_bypass_feuert_vor_der_nachtsperre():
    """18 Uhr (vor Sperre): Mitte zu kalt -> EIN, rechtzeitig vorheizen."""
    erg = bewerte_ohne_bypass({"mittig": 40.5}, now_hour=18)
    e = finde(erg, "Abend-Mitte")
    assert e.aktiv and e.einschalten is True
    assert "Nachtsperre" not in e.grund


def test_abend_ohne_bypass_pausiert_in_der_nachtsperre():
    """21 Uhr (in Sperre): Auch bei 38C kein EIN mehr - kein Nacht-Heizen."""
    erg = bewerte_ohne_bypass({"mittig": 38.0}, now_hour=21)
    e = finde(erg, "Abend-Mitte")
    assert not e.aktiv
    assert e.einschalten is None
    assert "Garantie ueberschreibt nicht" in e.grund


def test_default_ueberschreibt_weiterhin():
    """Rueckwaertskompatibilitaet: Default-Flag=True feuert weiter in der Sperre."""
    config = WPSteuerungConfig()
    erg = pc.evaluate_mindesttemp(
        config.mindest_temp, {"mittig": 39.0}, 21,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
    )
    e = next(x for x in erg if "Abend-Mitte" in x.name)
    assert e.aktiv and e.einschalten is True
    assert "Nachtsperre ueberschrieben" in e.grund


def test_mittag_oben_unveraendert():
    """Mittags-Garantie (Default-Flag) bleibt vom neuen Feld unberuehrt."""
    config = WPSteuerungConfig()
    for eintrag in config.mindest_temp.eintraege:
        if eintrag.name == "Mittag-Oben":
            assert eintrag.nachtsperre_ueberschreiben is True
    erg = pc.evaluate_mindesttemp(
        config.mindest_temp, {"oben": 39.0}, 12,
        config.sicherheit.nachtsperre_start, config.sicherheit.nachtsperre_ende,
    )
    e = next(x for x in erg if "Mittag-Oben" in x.name)
    assert e.aktiv and e.einschalten is True


# ── Integration mit der echten Parameterdatei ──

def test_json_config_abend_mitte_neues_verhalten():
    """Echte Parameterdatei: 42C-Garantie bis 19 Uhr, danach Nachtsperre."""
    import json_config as jc
    mgr = jc.WPSteuerungConfigManager()
    mgr.load_config()
    cfg = mgr.get()
    eintrag = next(e for e in cfg.mindest_temp.eintraege if e.name == "Abend-Mitte")
    assert eintrag.min_temp_c == 42.0
    assert eintrag.nachtsperre_ueberschreiben is False

    # 20 Uhr, mittig 41C (nach dem Duschen abgekuehlt): KEIN EIN mehr
    erg = pc.evaluate_mindesttemp(
        cfg.mindest_temp, {"mittig": 41.0}, 20,
        cfg.sicherheit.nachtsperre_start, cfg.sicherheit.nachtsperre_ende,
    )
    e = finde(erg, "Abend-Mitte")
    assert not e.aktiv and e.einschalten is None

    # 18 Uhr, mittig 41C (kurz vor dem Duschen): EIN - Wasser wird warm
    erg = pc.evaluate_mindesttemp(
        cfg.mindest_temp, {"mittig": 41.0}, 18,
        cfg.sicherheit.nachtsperre_start, cfg.sicherheit.nachtsperre_ende,
    )
    e = finde(erg, "Abend-Mitte")
    assert e.aktiv and e.einschalten is True
