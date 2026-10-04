"""Regression: PV-Mindestlaufzeit war beim Start wirkungslos.

Befund aus der Regelanalyse 04.10.2026:

`handle_compressor_on` berechnet ueber `_effektive_mindestlaufzeit` die
Mindestlaufzeit fuer die Start-Antizipation. Es uebergab dafür
`state.control.active_rule_name`.

Genau das ist der Fehler: `determine_mode_and_setpoints` setzt
`active_rule_name` im Aus-Zustand auf `None` - und der Start laeuft immer
im Aus-Zustand. Damit war `regel_name` immer None, `_effektive_
mindestlaufzeit` lieferte konstant die vollen 60 min statt der
konfigurierten `pv_min_laufzeit_minuten = 10`.

Auswirkung: Die Start-Antizipation blockierte PV-Starts, die laut
Konfiguration erlaubt sind. Beispiel unten 46,8 C, Ausschaltpunkt 48 C,
Rate 2 C/h: 1,2 K Hub = 36 min, +1 min Reserve = 37 min. Das passt zu
10 min (Start erlaubt), nicht zu 60 min (Start blockiert).

Ausserdem behauptete der Docstring von `_effektive_mindestlaufzeit` das
Gegenteil der Wirklichkeit ("Im Produktivbetrieb wird active_rule_name
vor handle_compressor_on gesetzt") - deshalb blieb der Fehler lange
unbemerkt.
"""
import os
import sys
from datetime import timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control_logic as pcl


def _state(pv_min=10, min_lauf=60):
    from types import SimpleNamespace
    return SimpleNamespace(
        priority_config=SimpleNamespace(
            zyklus=SimpleNamespace(
                pv_min_laufzeit_minuten=pv_min,
                mindestlaufzeit_minuten=min_lauf,
            )
        ),
        control=SimpleNamespace(active_rule_name=None, _lauf_start_regel=None),
    )


def test_start_uebergibt_regelname_an_handle_compressor_on():
    """Der Aufrufer muss die gewinnende Regel durchreichen.

    `active_rule_name` ist im Aus-Zustand per Definition None - darauf
    kann sich der Start nicht verlassen.
    """
    import inspect
    params = list(inspect.signature(pcl.handle_compressor_on).parameters)
    assert "regel_name" in params

    main_quelle = open(
        os.path.join(os.path.dirname(pcl.__file__), "main.py"),
        encoding="utf-8",
    ).read()
    aufruf = main_quelle[main_quelle.find("handle_compressor_on("):][:900]
    assert "regel_name=gewinner_name" in aufruf, (
        "main.py muss die gewinnende Regel an handle_compressor_on reichen"
    )


def test_pv_mindestlaufzeit_greift_beim_start():
    """Kern-Regression: mit uebergebener Regel greift pv_min_laufzeit."""
    state = _state()
    basis = timedelta(minutes=60)

    # Mit Regel: PV-Mindestlaufzeit greift.
    assert pcl._effektive_mindestlaufzeit(state, basis, "AdaptivePV") == timedelta(minutes=10)
    # Netz-/Komfortlauf bleibt bei der vollen Mindestlaufzeit.
    assert pcl._effektive_mindestlaufzeit(state, basis, "Abweichung") == basis
    # Genau der Zustand, in dem der Start real ist.
    assert pcl._effektive_mindestlaufzeit(state, basis, None) == basis


def test_start_antizipation_rechnet_mit_pv_mindestlaufzeit():
    """Der Zahlenfall aus dem Befund: 37 min passt zu 10, nicht zu 60."""
    state = _state()
    basis = timedelta(minutes=60)

    hub_k = 48.0 - 46.8          # 1,2 K
    rate = 2.0                   # C/h
    erwartet_min = hub_k / rate * 60.0
    puffer_min = 1.0
    gesamt = erwartet_min + puffer_min

    assert abs(gesamt - 37.0) < 1.0, gesamt

    pv_minz = pcl._effektive_mindestlaufzeit(state, basis, "AdaptivePV")
    netz_minz = pcl._effektive_mindestlaufzeit(state, basis, None)

    pv_minz_min = pv_minz.total_seconds() / 60.0
    netz_minz_min = netz_minz.total_seconds() / 60.0

    # Mit korrekter PV-Mindestlaufzeit: Start erlaubt.
    assert not (gesamt < pv_minz_min), "PV-Start muss bei 37 min moeglich sein"
    # Mit dem alten, falschen Vergleich: blockiert.
    assert gesamt < netz_minz_min, "Genau dieser Vergleich hat blockiert"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])