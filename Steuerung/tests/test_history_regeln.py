"""Tests fuer /history/regeln: Override-Felder fuer das Chart-Overlay (A2).

Die WebApp laedt die Regelhistorie parallel zum Temperaturverlauf und zeigt
die gewinnende Regel als Overlay. Weicht die Handlung von der Regel-Empfehlung
ab (Mindestlaufzeit/-pause, Debounce, Sicherheit), nennt ``handlung_override``
den Grund - der Chart-Tooltip kann ihn dann anzeigen.
"""
import os
import sys
from datetime import timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import api  # noqa: E402
import entscheidungs_log as el  # noqa: E402


def _schreibe(ts, gewinner, soll, laeuft, override=None, empfehlung=None):
    el.schreibe_eintrag(
        gewinner_name=gewinner,
        gewinner_grund=f"{gewinner}-Test",
        soll_einschalten=soll,
        kompressor_laeuft=laeuft,
        regel_empfehlung=empfehlung,
        diagnostics={"blocking_reason": override} if override else None,
        ts=ts,
    )


def test_history_regeln_liefert_override_felder():
    now = el._log_now()
    _schreibe(
        now - timedelta(minutes=30), "Einspeisung", True, True,
        override="Regel 'Einspeisung' sagt AUS, warte auf Mindestlaufzeit (noch 6m)",
        empfehlung=False,
    )
    daten = api.get_history_regeln(hours=24, limit=100)
    assert daten["count"] >= 1
    eintrag = daten["data"][-1]
    assert eintrag["regel"] == "Einspeisung"
    assert eintrag["regel_empfehlung"] is False
    assert "Handlung EIN trotz Regel-AUS" in eintrag["handlung_override"]


def test_history_regeln_ohne_override_bleibt_null():
    now = el._log_now()
    _schreibe(now - timedelta(minutes=10), "AdaptivePV", True, True, empfehlung=True)
    daten = api.get_history_regeln(hours=24, limit=100)
    eintrag = daten["data"][-1]
    assert eintrag["regel_empfehlung"] is True
    assert eintrag["handlung_override"] is None
