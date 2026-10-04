"""Regressionstests fuer den Lebenszeichen-Heartbeat des Entscheidungslogs.

Anlass (Befund 03./04.10.2026): Trotz fehlerfreier Regelphase entstand ueber
19 Stunden keine einzige Logzeile. Ursache war eine Verriegelung aus zwei
Stellen in entscheidungs_log.py:

1. `_soll_schreiben` pruefte den Vergleich der Gewinner-Regel VOR dem
   Herzschlag. Ein Regelwechsel bei unveraenderter Handlung wird
   unterdrueckt - faellig wird der Herzschlag aber nur, wenn die Regel
   unveraendert ist. Beides gleichzeitig ist nie erreichbar.
2. Der Cache wurde im unterdrueckten Zweig nur unter der Bedingung
   `if _cache_pfad == LOG_DATEI and _cache_zeile is not None` gepflegt.
   Nach einem Neustart sind beide leer, der Guard griff nie, und
   `_letzte_logzeile()` las erneut aus der Datei - der Vergleich blieb also
   dauerhaft "anders" und die Unterdrueckung wiederholte sich in jedem Takt.

Diese Tests bilden genau diese Konstellation nach.
"""
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import entscheidungs_log as el


def _log_pfad(tmp_path):
    """Patcht den Logpfad und liefert ihn als String zurueck."""
    log_datei = str(tmp_path / "entscheidungs_log.jsonl")
    return patch.multiple(el, LOG_DATEI=log_datei, MAX_BYTES=1_000_000), log_datei


def _eintrag(gewinner, ts, soll=False, laeuft=False):
    return {
        "gewinner": gewinner,
        "grund": f"{gewinner}-Test",
        "soll_einschalten": soll,
        "kompressor_laeuft": laeuft,
        "ts": ts.isoformat(timespec="seconds"),
    }


def _schreibe(pfad, eintrag):
    with open(pfad, "a", encoding="utf-8") as f:
        f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")


def _schreibe_eintrag(gewinner, ts, soll=False, laeuft=False):
    return el.schreibe_eintrag(
        gewinner_name=gewinner,
        gewinner_grund=f"{gewinner}-Test",
        soll_einschalten=soll,
        kompressor_laeuft=laeuft,
        ts=ts,
    )


def test_herzschlag_durchbricht_regelwechsel_unterdrueckung(tmp_path):
    """Kern-Regression: Der Herzschlag muss auch bei anderer Regel faellig werden.

    Die Datei endet mit der Regel 'Einspeisung'. Die laufende Regel ist
    dauerhaft eine andere, die Handlung bleibt gleich. Genau so sah der
    Betrieb am 04.10. aus - und genau da blieb das Log 19 Stunden stumm.
    """
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher:
        t0 = datetime(2026, 10, 3, 15, 42, 5)
        _schreibe(log_datei, _eintrag("Einspeisung", t0))

        # Frischer Prozess: Cache leer, wie nach jedem Neustart.
        assert el._cache_zeile is None
        assert el._cache_pfad is None

        # Erster Takt: Regelwechsel bei gleicher Handlung -> unterdrueckt.
        assert _schreibe_eintrag("Solarueberschuss", t0 + timedelta(seconds=10)) is False

        # Der Herzschlag misst ab dem LETZTEN ECHTEN SCHREIBVORGANG, nicht ab dem
        # zuletzt bewerteten Takt. Der unterdrueckte Zwischenaufruf bei t0+10
        # darf die Frist also nicht verlaengern - das war der zweite Teil des
        # Befunds.
        assert _schreibe_eintrag(
            "Solarueberschuss", t0 + timedelta(seconds=1800)
        ) is True
        assert os.path.getsize(log_datei) > 0


def test_cache_wird_im_unterdrueckten_zweig_gepflegt(tmp_path):
    """Auch ohne Schreibvorgang muss der Cache den neuen Gewinner fuehren."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher:
        t0 = datetime(2026, 10, 3, 15, 42, 5)
        _schreibe(log_datei, _eintrag("Einspeisung", t0))

        assert _schreibe_eintrag("Solarueberschuss", t0 + timedelta(seconds=10)) is False

        assert el._cache_pfad == log_datei
        assert el._cache_zeile is not None
        assert el._cache_zeile["gewinner"] == "Solarueberschuss"


def test_regelwechsel_wird_nicht_pro_takt_wiederholt(tmp_path):
    """Derselbe unterdrueckte Regelwechsel darf sich nicht aufblaehen."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher:
        t0 = datetime(2026, 10, 3, 15, 42, 5)
        _schreibe(log_datei, _eintrag("Einspeisung", t0))

        _schreibe_eintrag("Solarueberschuss", t0 + timedelta(seconds=10))
        assert len(el._pending_regelwechsel) == 1

        for sek in (20, 30, 40):
            assert _schreibe_eintrag(
                "Solarueberschuss", t0 + timedelta(seconds=sek)
            ) is False
        assert len(el._pending_regelwechsel) == 1


def test_herzschlag_laeuft_kurzer_als_stillstand(tmp_path):
    """Bei laufender WP gilt das kuerzere Intervall (75 s)."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher:
        t0 = datetime(2026, 10, 3, 15, 42, 5)
        _schreibe(log_datei, _eintrag("Solarueberschuss", t0, soll=True, laeuft=True))

        assert _schreibe_eintrag(
            "Solarueberschuss", t0 + timedelta(seconds=60), soll=True, laeuft=True
        ) is False
        # Ab dem zuletzt bewerteten Eintrag (t0+60) sind 75 s Intervall faellig.
        assert _schreibe_eintrag(
            "Solarueberschuss", t0 + timedelta(seconds=140), soll=True, laeuft=True
        ) is True


def test_schreibfehler_wird_gemeldet(tmp_path, caplog):
    """Ein Schreibfehler muss im Journal auftauchen, nicht nur im Debug-Log."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher, patch("builtins.open", side_effect=OSError("Testfehler")):
        with caplog.at_level("WARNING"), patch.object(el, "_schreibfehlerzaehler", 0):
            assert _schreibe_eintrag("Test", datetime(2026, 10, 4, 10, 0, 0)) is False

    assert any("nicht schreibbar" in r.message for r in caplog.records)


def test_schreibfehler_wird_nicht_geflutet(tmp_path, caplog):
    """Ab dem zweiten Fehler nur noch jeder 50. - sonst Journal-Flut."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher, patch("builtins.open", side_effect=OSError("Testfehler")):
        with caplog.at_level("WARNING"), patch.object(el, "_schreibfehlerzaehler", 0):
            for _ in range(10):
                _schreibe_eintrag("Test", datetime(2026, 10, 4, 10, 0, 0))

    treffer = [r for r in caplog.records if "nicht schreibbar" in r.message]
    assert len(treffer) == 1


def test_aktionswechsel_schreibt_trotz_unterdrueckter_regel(tmp_path):
    """Eine Aenderung der Handlung bleibt immer sichtbar."""
    patcher, log_datei = _log_pfad(tmp_path)
    with patcher:
        t0 = datetime(2026, 10, 3, 15, 42, 5)
        _schreibe(log_datei, _eintrag("Einspeisung", t0, soll=False, laeuft=False))

        assert _schreibe_eintrag(
            "Komfort", t0 + timedelta(seconds=5), soll=True, laeuft=True
        ) is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])