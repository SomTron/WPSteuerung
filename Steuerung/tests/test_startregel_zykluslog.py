"""Regression: `active_rule_name` beim Hardware-Start beschriften.

Befund aus der Analyse der Zustandsuebergaenge 04.10.2026.

In `set_kompressor_status` (main.py) wird die Startregel nur gesetzt,
wenn ein Pending-Wert oder ein manueller Start vorliegt:

    if manual_rule or pending_rule:
        state.control._lauf_start_regel = ...
        state.control.effective_rule_name = ...
        state.control.active_rule_name = ...

Im `else`-Zweig blieb `active_rule_name` auf dem Wert des VORHERIGEN
Laufs stehen. Eine Zeile weiter ruft die Funktion `begin_cycle` auf, und
genau dort wird gelesen:

    start_regel = getattr(control, "active_rule_name", None)

`begin_cycle` legt den Start-Snapshot fuer das Zyklus-CSV ab. Im
leeren Pending-Fall stand dort also die Regel des vorherigen Laufs,
waehrend die aktuelle Regelbewertung eine andere gefordert hatte.

Warum der leere Fall kein Randfall ist: `handle_compressor_on` leert
`_pending_start_rule` seit dem vorangegangenen Fix am Funktionsanfang
zuverlaessig. Ohne Pending-Eintrag verliess sich der Start auf ein
Feld, das gar nicht mehr gesetzt wurde.

Der Rueckfall nimmt `requested_rule_name` (Forderung der aktuellen
Bewertung) und danach `previous_modus` (zuletzt bestaetigter Modus).
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import main


def _state():
    import pytz
    tz = pytz.timezone('Europe/Berlin')
    st = SimpleNamespace()
    st.local_tz = tz
    st.control = SimpleNamespace(
        kompressor_ein=False,
        # ALTLAST aus dem vorherigen Lauf
        active_rule_name='Einspeisung',
        effective_rule_name='Einspeisung',
        _lauf_start_regel='Einspeisung',
        source_at_start='PV',
        source_current='PV',
        requested_rule_name='CalcStart',
        previous_modus='Abweichung',
        zyklus_id=3,
        _legionellen_verify_checks=set(),
    )
    st.solar = SimpleNamespace(energy_source='PV')
    st.sensors = SimpleNamespace(
        t_verd=10.0, t_unten=40.0, t_mittig=42.0, t_oben=45.0
    )
    st.stats = SimpleNamespace(
        last_compressor_on_time=None, last_compressor_off_time=None,
        total_runtime_today=0,
    )
    return st


@pytest.mark.asyncio
async def test_aktuelle_regel_ohne_pending_wird_gesetzt(monkeypatch):
    """Kern-Regression: ohne Pending-Wert darf nicht der alte Regelname
    in den Zyklus-Snapshot wandern."""
    st = _state()

    async def hw(state, an):
        return True

    monkeypatch.setattr(main, "_set_hardware_state", hw)
    monkeypatch.setattr(main, "_record_hardware_change", lambda *a, **k: None)
    monkeypatch.setattr(main, "begin_cycle", lambda *a, **k: None)

    # Kein _pending_start_rule - der Fall seit dem vorherigen Fix
    assert not hasattr(st.control, "_pending_start_rule") or \
        st.control._pending_start_rule is None

    ok = await main.set_kompressor_status(st, True)
    assert ok is True

    assert st.control.active_rule_name == 'CalcStart', (
        "active_rule_name traegt noch den Regelnamen des vorherigen Laufs: "
        f"{st.control.active_rule_name!r}"
    )
    assert st.control.effective_rule_name == 'CalcStart'
    assert st.control._lauf_start_regel == 'CalcStart'


@pytest.mark.asyncio
async def test_pending_regel_hat_vorrang(monkeypatch):
    """Gegenprobe: ein gesetzter Pending-Wert gewinnt weiterhin."""
    st = _state()
    st.control._pending_start_rule = 'AdaptivePV'
    st.control._pending_start_source = 'PV'

    async def hw(state, an):
        return True

    monkeypatch.setattr(main, "_set_hardware_state", hw)
    monkeypatch.setattr(main, "_record_hardware_change", lambda *a, **k: None)
    monkeypatch.setattr(main, "begin_cycle", lambda *a, **k: None)

    await main.set_kompressor_status(st, True)
    assert st.control.active_rule_name == 'AdaptivePV'


@pytest.mark.asyncio
async def test_manueller_start_hat_vorrang(monkeypatch):
    """Gegenprobe: 'API force_on' wird nicht von requested_rule_name
    ueberschrieben."""
    st = _state()
    st.control._lauf_start_regel = 'API force_on'
    st.control.manual_force_on_pending = True

    async def hw(state, an):
        return True

    monkeypatch.setattr(main, "_set_hardware_state", hw)
    monkeypatch.setattr(main, "_record_hardware_change", lambda *a, **k: None)
    monkeypatch.setattr(main, "begin_cycle", lambda *a, **k: None)

    await main.set_kompressor_status(st, True)
    assert st.control.active_rule_name == 'API force_on'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])