"""Tests: Tageswechsel sichert den Vortags-Endstand (Bugfix #2).

Vorher: Der ueber-Mitternacht-Anteil wurde addiert und zwei Zeilen spaeter
durch den Reset verworfen - die Tagesbilanz war systematisch falsch.
"""
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytz

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import handle_day_transition  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")


def baue_state(letzter_tag, komp_ein, startzeit=None, heute_runtime=timedelta(),
               gestern_runtime=None):
    """startzeit: naive ODER aware datetime (aware wird 1:1 uebernommen)."""
    if startzeit is not None and startzeit.tzinfo is None:
        startzeit = TZ.localize(startzeit)
    return SimpleNamespace(
        local_tz=TZ,
        control=SimpleNamespace(kompressor_ein=komp_ein),
        stats=SimpleNamespace(
            last_day=letzter_tag,
            total_runtime_today=heute_runtime,
            total_runtime_yesterday=(
                gestern_runtime if gestern_runtime is not None else timedelta()
            ),
            last_compressor_on_time=startzeit,
            last_completed_cycle=datetime.now(TZ),
        ),
    )


def test_ueber_mitternacht_laufend_anteil_und_endstand_gerettet():
    """Kompressor laeuft ueber Mitternacht: Vortag = Endstand + Nach-Mitternacht-Anteil."""
    state = baue_state(
        letzter_tag=datetime(2026, 8, 21).date(),
        komp_ein=True,
        startzeit=datetime(2026, 8, 21, 23, 30),  # 30 Min vor Mitternacht
        heute_runtime=timedelta(hours=2),
    )
    now = TZ.localize(datetime(2026, 8, 22, 0, 10))

    with patch('main.rotiere_csv_monatlich', return_value=None):
        handle_day_transition(state, now)

    assert state.stats.total_runtime_yesterday == timedelta(hours=2, minutes=30)
    assert state.stats.total_runtime_today == timedelta()
    assert state.stats.last_compressor_on_time == TZ.localize(datetime(2026, 8, 22, 0, 0))
    assert state.stats.last_completed_cycle is None


def test_kompressor_aus_endstand_wird_erhalten():
    """Ohne laufenden Kompressor: Vortags-Endstand wandert 1:1 in yesterday."""
    state = baue_state(
        letzter_tag=datetime(2026, 8, 21).date(),
        komp_ein=False,
        heute_runtime=timedelta(hours=5, minutes=12),
    )
    now = TZ.localize(datetime(2026, 8, 22, 6, 0))

    with patch('main.rotiere_csv_monatlich', return_value=None):
        handle_day_transition(state, now)

    assert state.stats.total_runtime_yesterday == timedelta(hours=5, minutes=12)
    assert state.stats.total_runtime_today == timedelta()


def test_gleicher_tag_nichts_wird_angeruehrt():
    """Kein Tageswechsel: weder yesterday noch today noch Startzeit veraendern sich."""
    now = datetime.now(TZ)
    state = baue_state(
        letzter_tag=now.date(),
        komp_ein=True,
        startzeit=now - timedelta(minutes=30),
        heute_runtime=timedelta(hours=1),
        gestern_runtime=timedelta(hours=9),
    )

    with patch('main.rotiere_csv_monatlich', return_value=None):
        handle_day_transition(state, now)

    assert state.stats.total_runtime_today == timedelta(hours=1)
    assert state.stats.total_runtime_yesterday == timedelta(hours=9)
    assert state.stats.last_day == now.date()


def test_erster_aufruf_setzt_nur_starttag():
    """last_day=None (frischer Start): nur Tag setzen, keine Statistik-Verarbeitung."""
    now = datetime.now(TZ)
    state = baue_state(
        letzter_tag=None,
        komp_ein=True,
        startzeit=now - timedelta(minutes=5),
        heute_runtime=timedelta(minutes=5),
    )

    with patch('main.rotiere_csv_monatlich', return_value=None):
        handle_day_transition(state, now)

    assert state.stats.last_day == now.date()
    assert state.stats.total_runtime_today == timedelta(minutes=5)  # unveraendert
    assert state.stats.total_runtime_yesterday == timedelta()       # unveraendert
