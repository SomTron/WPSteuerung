"""Prioritaets-Matrix der Regel-Engine als gepruefter Vertrag.

Professioneller Standard: Die Reihenfolge der Regeln (Safety first) ist
keine Implementierungsdetail mehr, sondern ein Dokument, das gegen
Regressionen getestet wird. Eine Veraenderung der Prioritaeten MUSS hier
ausdruecklich geprueft und freigegeben werden.

Abgedeckte Fehlerklassen:
- Unabsichtliche Prioritaets-Doppelvergabe (Tie-Break waere sonst
  versteckt: max() nimmt das erste Element)
- Sicherheit Reihenfolge: Notfallschutz muss IMMER ueber allem stehen
  und der Wochenend-/Legionellen-Block ueber den Spar-Regeln
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from json_config import WPSteuerungConfig  # noqa: E402


def _matrix():
    """Alle namentlichen Regeln mit ihren konfigurierten Prioritaeten."""
    c = WPSteuerungConfig()
    return {
        "notfallschutz": c.notfallschutz.prioritaet,
        "wochenende": c.wochenende.prioritaet,
        "legionellen": c.legionellen.prioritaet,
        "einspeisung": c.einspeisung.prioritaet,
        "calcstart": c.calculated_start.prioritaet,
        "adaptive_pv": c.adaptive_pv.prioritaet,
        "batterie": c.batterie.prioritaet,
        "mindest_temp": c.mindest_temp.prioritaet,
        "komfort": c.komfort.prioritaet,
        "forecast": c.forecast.prioritaet,
        "zeitfenster": c.zeitfenster.prioritaet,
        "abweichung": c.abweichung.prioritaet,
    }


def test_prioritaeten_sind_eindeutig():
    """Keine Doppelvergabe: Ein Tie-Break durch max() waere versteckt."""
    m = _matrix()
    werte = list(m.values())
    assert len(werte) == len(set(werte)), (
        f"Doppelte Prioritaeten: {sorted(m.items(), key=lambda kv: -kv[1])}"
    )


def test_safety_first_reihenfolge():
    """Der Notfallschutz (Schutzleiter) muss ueber ALLEN Regeln stehen."""
    m = _matrix()
    notfall = m["notfallschutz"]
    for name, prio in m.items():
        if name == "notfallschutz":
            continue
        assert prio < notfall, (
            f"{name} ({prio}) darf den Notfallschutz ({notfall}) "
            "nicht erreichen oder ueberragen"
        )


def test_blockierer_ueber_sparregeln():
    """Wochenend- und Legionellen-Block muessen ueber den Heizen-Regeln
    (Komfort, MindestTemp, Abweichung, Zeitfenster, Forecast) stehen,
    sonst blockieren sie nicht, wenn es kalt wird."""
    m = _matrix()
    blockierer = [m["wochenende"], m["legionellen"]]
    sparregeln = [
        m["komfort"], m["mindest_temp"], m["abweichung"],
        m["zeitfenster"], m["forecast"],
    ]
    for b in blockierer:
        for s in sparregeln:
            assert b > s, (
                f"Blockierer {b} muss ueber Spar-Regel {s} liegen"
            )


def test_pv_ueberschuss_darf_notfallschutz_nicht_ueberstimmen():
    """BUGFIX 11.11.2026: PV_Ueberschuss stand NICHT in der Kaskade.

    Nachgewiesen: `pv_ueberschuss.prioritaet = 200` wurde akzeptiert, obwohl
    der Notfallschutz (110) dann nicht mehr gewinnen kann. Ein Tippfehler in
    der JSON-Config haette den Schutzleiter ausgehebelt.
    """
    import pytest

    c = WPSteuerungConfig()
    d = c.model_dump()
    for prio in (200, 111, 110):
        d2 = {**d, "pv_ueberschuss": {**d["pv_ueberschuss"], "prioritaet": prio}}
        with pytest.raises(ValueError, match="kleiner als der Notfallschutz"):
            WPSteuerungConfig.model_validate(d2)

    # Knapp darunter bleibt erlaubt.
    d3 = {**d, "pv_ueberschuss": {**d["pv_ueberschuss"], "prioritaet": 109}}
    WPSteuerungConfig.model_validate(d3)


def test_pv_ueberschuss_darf_ueber_calcstart_liegen():
    """Die dokumentierte Freiheit bleibt: Ueberschuss vor Zeitplan.

    Die Feldbeschreibung sagt ausdruecklich "Hoeher setzen, wenn der
    Ueberschuss Vorrang vor dem Zeitplan (CalcStart 82) haben soll." Eine
    feste Position in der Kaskade haette das verboten.
    """
    c = WPSteuerungConfig()
    d = c.model_dump()
    for prio in (83, 90, 100):
        d2 = {**d, "pv_ueberschuss": {**d["pv_ueberschuss"], "prioritaet": prio}}
        WPSteuerungConfig.model_validate(d2)


def test_pv_regeln_duerfen_notfallschutz_nicht_ueberstimmen():
    """Die Liste `pv_regeln` war ebenfalls ungeprueft."""
    import pytest

    c = WPSteuerungConfig()
    d = c.model_dump()
    for prio in (200, 110):
        d2 = {**d, "pv_regeln": [{"name": "PV_Test", "prioritaet": prio}]}
        with pytest.raises(ValueError, match="kleiner als der Notfallschutz"):
            WPSteuerungConfig.model_validate(d2)

    # 78 (AdaptivePV-Niveau, Default) bleibt erlaubt.
    d3 = {**d, "pv_regeln": [{"name": "PV_Test", "prioritaet": 78}]}
    WPSteuerungConfig.model_validate(d3)


def test_pv_regeln_prioritaet_muss_mindestens_eins_sein():
    import pytest

    c = WPSteuerungConfig()
    d = c.model_dump()
    d2 = {**d, "pv_regeln": [{"name": "PV_Test", "prioritaet": 0}]}
    with pytest.raises(ValueError, match="mindestens 1"):
        WPSteuerungConfig.model_validate(d2)


def test_notfallschutz_default_bereich():
    """Der Notfall startet bei 36 C (Default) und endet bei 38 C."""
    c = WPSteuerungConfig()
    nf = c.notfallschutz
    assert nf.einschalten_bei_c == 36.0
    assert nf.ausschalten_bei_c == 38.0
    assert nf.einschalten_bei_c < nf.ausschalten_bei_c


def test_sicherheitsgrenzen_sind_stimmig():
    """Konfigurierbare Grenzen muessen logisch sein (Professionell)."""
    c = WPSteuerungConfig()
