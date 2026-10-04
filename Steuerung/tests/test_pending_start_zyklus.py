"""Regression: `_pending_start_rule` gehoerte nicht zum aktuellen Zyklus.

Befund aus der Analyse der Zustandsuebergaenge 04.10.2026.

`handle_compressor_on` setzt `state.control._pending_start_rule` auf den
Gewinnernamen - aber nur im Block `should_on and pause_ok and nacht_ok`.
Geleert wurde der Wert nur an zwei Stellen:

  * `stop_condition` (Zieltemp erreicht),
  * nach erfolgreichem Hardware-Start.

In allen anderen Rueckgabepfaden blieb er stehen:

  * `nacht_ok == False` (Mindestlaufzeit ueberlebt die Nachtabschaltung
    nicht) -> `return False`, Pending bleibt,
  * Minute-Pause, Taktschutz, Neustartsperre, Boiler-Max: der ganze
    Block wird uebersprungen, der Pending-Wert eines VORHERIGEN
    Zyklus bleibt stehen,
  * der generische Rueckgabepfad am Ende der Funktion.

Konsument ist `set_kompressor_status` in main.py:

    pending_rule = getattr(state.control, "_pending_start_rule", None)
    ...
    state.control._lauf_start_regel = pending_rule
    state.control.effective_rule_name = pending_rule
    state.control.active_rule_name = pending_rule

Ein stehengebliebener Wert wird damit beim naechsten erfolgreichen Start
als `effektive_rule_name`/`active_rule_name` uebernommen - die Regel, die
tatsaechlich den Kompressor gestartet hat, steht dann nicht im
Entscheidungslog und nicht in der Statuszeile.

Der Fix setzt den Pending-Eintrag zu Beginn des Aus-Zweigs auf None,
sodass er pro Zyklus neu gefuellt und bei jeder Abkehr verworfen wird.
"""
import os
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import priority_control_logic as pcl

TZ = None


def _state():
    """Kompressor AUS, Start ist gewollt, aber die Nachtsperre blockiert."""
    import pytz
    tz = pytz.timezone('Europe/Berlin')
    st = SimpleNamespace()
    st.local_tz = tz
    st.control = SimpleNamespace(
        kompressor_ein=False,
        _soll_einschalten=True,
        requested_rule_name='CalcStart',
        previous_modus='Abweichung',
        source_current='PV',
        _pending_start_rule=None,
        _pending_start_source=None,
        blocking_reason=None,
        zyklus_id=7,
        aktueller_einschaltpunkt=42.0,
        aktueller_ausschaltpunkt=48.0,
        _lauf_start_regel=None,
        active_rule_name=None,
        effective_rule_name=None,
        restart_lockout_until=None,
        notfall_aktiv=False,
    )
    st.stats = SimpleNamespace(
        last_compressor_on_time=None, last_compressor_off_time=None
    )
    st.sensors = SimpleNamespace(
        t_oben=45.0, t_unten=40.0, t_mittig=42.0, t_verd=10.0
    )
    st.solar = SimpleNamespace(
        pv_erzeugung=3000.0, feedinpower=1500.0, batpower=0.0,
        acpower=3000.0, soc=80.0, energy_source='PV',
    )
    st.min_laufzeit = timedelta(minutes=60)
    st.min_pause = timedelta(minutes=20)
    # priority_config wird an mehreren Stellen gelesen (Boiler-Max,
    # Taktschutz, Nachtabschaltung). Ohne diese Felder bricht handle_
    # compressor_on vorher ab - unabhaengig von dem hier getesteten Verhalten.
    st.priority_config = SimpleNamespace(
        sicherheit=SimpleNamespace(
            boiler_max_fuehler='unten',
            boiler_max_c=48.0,
            ein_sperre_abstand_k=2.0,
            nachtsperre_start=19,
            nachtsperre_ende=8,
            start_vorhersage_aktiv=False,
        ),
        zyklus=SimpleNamespace(
            mindestlaufzeit_minuten=60,
            mindestpausenzeit_minuten=20,
            pv_min_laufzeit_minuten=10,
        ),
        taktschutz=SimpleNamespace(
            aktiv=False, max_wechsel_pro_stunde=6, pause_minuten=20,
        ),
    )
    return st


async def _kein_start(state, status, force=False, t_boiler_oben=None, end_grund=None):
    """set_kompressor_status-Ersatz: der Start schlaegt fehl."""
    return False


@pytest.mark.asyncio
async def test_altlast_wird_verworfen_wenn_gar_nicht_gestartet_wird():
    """Kern-Regression: ein Regelname aus einem frueheren Zyklus darf nicht
    als 'wird gerade starten' weiterleben."""
    st = _state()
    # Altlast aus einem vorherigen Zyklus
    st.control._pending_start_rule = 'Einspeisung'
    st.control._pending_start_source = 'PV'

    # Neustartsperre blockiert den Start - der Block wird uebersprungen
    import pytz
    st.control.restart_lockout_until = (
        datetime_now(pytz.timezone('Europe/Berlin')) + timedelta(minutes=10)
    )

    ergebnis = await pcl.handle_compressor_on(
        st, None, 40.0, 42.0, 48.0,
        timedelta(minutes=60), timedelta(minutes=20), 45.0, 42.0, _kein_start,
    )

    assert ergebnis is False
    assert st.control._pending_start_rule is None, (
        "Altlast blieb im State - sie wird beim naechsten Start als "
        "_lauf_start_regel uebernommen: "
        f"{st.control._pending_start_rule!r}"
    )
    assert st.control._pending_start_source is None


def datetime_now(tz):
    from datetime import datetime
    return datetime.now(tz)


@pytest.mark.asyncio
async def test_pending_wird_aktuell_gefuellt():
    """Gegenprobe: der Wert wird im aktuellen Zyklus gesetzt, wenn ein
    Start tatsaechlich versucht wird - das Verhalten bleibt erhalten."""
    st = _state()
    st.control._pending_start_rule = 'Einspeisung'   # Altlast

    gesetzt = {}

    async def setz(state, status, force=False, t_boiler_oben=None, end_grund=None):
        gesetzt['gelesen'] = getattr(state.control, "_pending_start_rule", None)
        return True

    await pcl.handle_compressor_on(
        st, None, 40.0, 42.0, 48.0,
        timedelta(minutes=60), timedelta(minutes=20), 45.0, 42.0, setz,
    )
    # Der aktuelle Gewinner, nicht die Altlast
    assert gesetzt.get('gelesen') == 'CalcStart', gesetzt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])