"""Tests fuer den Health-/Qualitaets-Helfer des WP-Managers."""
from __future__ import annotations

import importlib.util
import json
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "Updater"
HELFER = UPDATER_DIR / "manager_health.py"


def _lade():
    spec = importlib.util.spec_from_file_location("manager_health", HELFER)
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


mh = _lade()


class _Antwort:
    def __init__(self, daten: bytes):
        self._daten = daten

    def read(self):
        return self._daten

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ------------------------------------------------------------------ Health


def test_health_ok_wird_als_gesund_gewertet(monkeypatch):
    monkeypatch.setattr(
        mh.urllib.request,
        "urlopen",
        lambda *a, **k: _Antwort(b'{"status": "ok", "loop_age_s": 4}'),
    )
    daten = mh.fetch_health(base="http://127.0.0.1:8000", key="k")
    assert daten["status"] == "ok"
    text, gesund = mh.render_health(daten)
    assert gesund is True
    assert "Status:       ok" in text


def test_health_degraded_wird_als_krank_gemeldet_mit_hinweis():
    text, gesund = mh.render_health({"status": "degraded"})
    assert gesund is False
    assert "externer Dienst" in text


def test_health_zeigt_hardware_und_persistenz():
    """Hardware-Info und Persistenz-Fehler muessen sichtbar sein.

    Die Felder "gpio_ok"/"last_switch_error" wurden hier nie von der API
    geliefert - der Test pruefte ein Feldbild, das es nicht gab. Jetzt
    prueft er die Felder, die /health tatsaechlich liefert.
    """
    text, _ = mh.render_health(
        {
            "status": "degraded",
            "hardware": {"kompressor_ein": True, "verifizierungsfehler": 2},
            "last_state_write_ok": False,
            "last_state_write_error": "SD-Karte read-only",
        }
    )
    assert "Kompressor:   EIN" in text
    assert "2 Kompressor-Verifizierungsfehler" in text
    assert "SD-Karte read-only" in text


def test_health_http_error_wird_als_runtime_error(monkeypatch):
    def _fehler(*a, **k):
        raise urllib.error.HTTPError("u", 503, "nope", None, None)

    monkeypatch.setattr(mh.urllib.request, "urlopen", _fehler)
    with pytest.raises(RuntimeError) as exc:
        mh.fetch_health(base="http://127.0.0.1:8000")
    assert "503" in str(exc.value)


def test_health_ungueltiges_json_wird_abgewiesen(monkeypatch):
    monkeypatch.setattr(
        mh.urllib.request, "urlopen", lambda *a, **k: _Antwort(b"kein json")
    )
    with pytest.raises(RuntimeError):
        mh.fetch_health(base="http://127.0.0.1:8000")


def test_health_api_key_wird_als_header_gesendet(monkeypatch):
    gespeichert = {}

    def _fake(request, timeout=None):
        gespeichert["key"] = request.get_header("X-api-key")
        return _Antwort(b'{"status": "ok"}')

    monkeypatch.setattr(mh.urllib.request, "urlopen", _fake)
    mh.fetch_health(base="http://x", key="geheim")
    assert gespeichert["key"] == "geheim"


# ----------------------------------------------------------------- Quality


def _echter_report(**overrides) -> dict:
    """Bericht genau so, wie ihn `analysis_core.build_quality_report` liefert.

    Anlass: Der Renderer suchte deutsche Feldnamen (`quality_level`, `schema`,
    `zyklen`, `probleme`), die dieser Bericht nie hatte - Option 23 zeigte
    deshalb "Schema: n/a" und nie ein Problem (Befund 31.10.2026).
    """
    report = {
        "schema_version": 1,
        "generated_at": "2026-10-31T04:32:16+01:00",
        "git_revision": "67adf23",
        "status": "ok",
        "sources": {"pv": 120, "netz": 3},
        "end_reasons": {"regel_aus": 100, "legionellen_ziel": 2},
        "scenarios": {"pv": 120, "legionellen": 2},
        "cycle_quality": {
            "present": True,
            "rows": 201,
            "invalid_rows": 0,
            "invalid_timestamps": 0,
        },
        "decision_quality": {
            "present": True,
            "rows": 500,
            "invalid_rows": 0,
            "missing_reason_codes": 0,
            "unknown_reason_codes": 0,
            "reason_quality": {"direct": 500},
            "source_quality": {"direct": 500},
        },
        "summary": {
            "cycles": 201,
            "decisions": 500,
            "invalid_rows": 0,
            "unknown_reason_codes": 0,
            "unknown_decision_reason_codes": 0,
            "missing_source_at_start": 0,
            "missing_decision_reason_codes": 0,
        },
    }
    report.update(overrides)
    return report


def test_quality_gut_wird_als_gruen_gewertet(tmp_path):
    pfad = tmp_path / "quality_report.json"
    pfad.write_text(json.dumps(_echter_report()), encoding="utf-8")

    text, gruen = mh.render_quality(mh.load_quality_report(pfad))
    assert gruen is True
    assert "67adf23" in text
    assert "Schema:       1" in text, text          # vorher "n/a"
    assert "201 Zeilen / 500 Zeilen" in text, text  # vorher gar keine Kennzahlen
    assert "pv" in text                             # Szenarien
    assert "Probleme" not in text


def test_quality_degraded_zeigt_die_ursachen(tmp_path):
    """Der Logfall: 'degraded' muss die Zaehler als Probleme ausweisen."""
    report = _echter_report(status="degraded")
    report["summary"].update(
        {
            "unknown_reason_codes": 7,
            "unknown_decision_reason_codes": 7,
            "missing_source_at_start": 12,
            "missing_decision_reason_codes": 3,
        }
    )
    report["cycle_quality"]["missing_source_at_start"] = 12
    report["decision_quality"].update(
        {
            "missing_reason_codes": 3,
            "unknown_reason_codes": 7,
            "reason_quality": {"direct": 480, "direct_unmapped": 7, "missing": 3},
        }
    )
    pfad = tmp_path / "quality_report.json"
    pfad.write_text(json.dumps(report), encoding="utf-8")

    text, gruen = mh.render_quality(mh.load_quality_report(pfad))
    assert gruen is False
    assert "Probleme (" in text, text
    assert "7 unbekannte reason_codes" in text, text
    assert "12 Zyklen ohne source_at_start" in text, text
    assert "3 Entscheidungen ohne reason_code" in text, text
    assert "keine Einzelprobleme" not in text


def test_quality_degraded_ohne_details_erklaert_den_hinweis():
    """Ohne Zaehler darf der Hinweis 'siehe Probleme oben' nicht ins Leere gehen."""
    text, gruen = mh.render_quality({"status": "degraded", "schema_version": 1})
    assert gruen is False
    assert "keine Einzelprobleme" in text
    assert "quality_report.json" in text


def test_quality_endgrund_unbekannt_wird_nur_mit_zaehler_gemeldet():
    """Rohzaehlung allein reicht nicht - Altbestand darf kein Problem erzeugen.

    `end_reasons`/`sources` zaehlen Zyklen UND Entscheidungen inklusive
    Altbestand. Gemeldet wird nur, wenn der praezise Zaehler es bestaetigt
    (Befund 31.10.2026: 34x 'unbekannt' und 257x 'decision:unbekannt' stammten
    aus Zeilen ohne reason_code bzw. ohne Endgrund).
    """
    report = _echter_report(status="degraded")
    report["end_reasons"]["unbekannt"] = 34
    report["end_reasons"]["decision:unbekannt"] = 257
    report["sources"]["unbekannt"] = 2
    text, _gruen = mh.render_quality(report)
    assert "Endgrund 'unbekannt'" not in text, text
    assert "Endgrund 'decision:unbekannt'" not in text, text

    report["cycle_quality"]["unknown_end_grund"] = 34
    report["summary"]["missing_source_at_start"] = 2
    report["summary"]["missing_decision_reason_codes"] = 257
    text, _gruen = mh.render_quality(report)
    assert "Endgrund 'unbekannt': 34" in text, text
    assert "Endgrund 'decision:unbekannt': 257" in text, text
    assert "Quelle 'unbekannt': 2" in text, text


def test_quality_hinweise_zu_altbestand_und_abgeleiteten_quellen():
    """Altbestand und geschaetzte Quellen sind Hinweise, keine Probleme."""
    report = _echter_report(status="degraded")
    report["decision_quality"]["source_quality"] = {
        "direct": 1132,
        "inferred_feedin": 370,
    }
    report["decision_quality"]["legacy_missing_reason_codes"] = 257
    text, _gruen = mh.render_quality(report)
    assert "Quellen (Entscheidungen): direct=1132, inferred_feedin=370" in text, text
    assert "Altbestand: 257 Zeilen ohne reason_code (Schema < 2)" in text, text
    assert "Quellen 'inferred_feedin'" not in text, text
    assert "Probleme" not in text, text


def test_quality_viele_probleme_werden_gekuerzt():
    report = _echter_report(status="degraded")
    report["decision_quality"]["source_quality"] = {
        f"unklar_{i}": 1 for i in range(15)
    }
    text, gruen = mh.render_quality(report)
    assert gruen is False
    assert "Probleme (15)" in text, text
    assert "5 weitere" in text, text


def test_quality_fehlende_datei_wirft_runtime_error(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        mh.load_quality_report(tmp_path / "fehlt.json")
    assert "nicht gefunden" in str(exc.value)


def test_quality_ungueltiges_json_wirft_runtime_error(tmp_path):
    pfad = tmp_path / "q.json"
    pfad.write_text("{kaputt", encoding="utf-8")
    with pytest.raises(RuntimeError):
        mh.load_quality_report(pfad)


def test_quality_toleriert_utf8_bom(tmp_path):
    """Ein BOM aus einem Windows-Editor darf den Bericht nicht unlesbar machen."""
    pfad = tmp_path / "q.json"
    pfad.write_text(
        json.dumps({"quality_level": "ok"}), encoding="utf-8-sig"
    )
    daten = mh.load_quality_report(pfad)
    assert daten["quality_level"] == "ok"


# --------------------------------------------------------------------- CLI


def test_cli_health_ohne_server_meldet_fehler(capsys):
    rc = mh.main(["health", "--api-base", "http://127.0.0.1:1"])
    assert rc == 1
    assert "FEHLER" in capsys.readouterr().err


def test_cli_quality_ohne_pfad_meldet_fehler(capsys, monkeypatch):
    monkeypatch.delenv("WPS_QUALITY_REPORT", raising=False)
    rc = mh.main(["quality"])
    assert rc == 1
    assert "Kein Qualitaetsbericht" in capsys.readouterr().err


def test_cli_quality_gibt_exitcode_2_bei_warnung(tmp_path, capsys):
    pfad = tmp_path / "q.json"
    pfad.write_text(json.dumps({"quality_level": "warnung"}), encoding="utf-8")
    rc = mh.main(["quality", "--quality-report", str(pfad)])
    assert rc == 2
    assert "Analyse-Qualitaet" in capsys.readouterr().out


# --- Kopplung API <-> Manager -------------------------------------------
#
# Der Manager las seit langem Feldnamen, die die API gar nicht liefert:
# "Version: n/a", "Loop-Alter: n/a", "Regelung: n/a", "Sensoren: n/a".
# Und "Regelfehler: 0" war irrefuehrend - es wurde gar kein Wert gelesen,
# es stand nur die Vorgabe "0" da.
#
# Der Test nimmt die ECHTE API-Antwort und laesst den Manager damit
# arbeiten. Jede Umbenennung eines API-Feldes faellt hier sofort auf.


def _mock_state():
    """Ein State, der alle von /health gelesenen Felder traegt."""
    jetzt = datetime.now()
    return SimpleNamespace(
        clock=SimpleNamespace(now=lambda: jetzt),
        loop_heartbeat=jetzt - timedelta(seconds=3),
        last_control_success=jetzt - timedelta(seconds=2),
        last_sensor_success=jetzt - timedelta(seconds=1),
        last_status_snapshot_at=jetzt,
        consecutive_control_errors=0,
        last_data_update_ok=True,
        last_state_write_ok=True,
        last_state_write_error=None,
        kompressor_verification_error_count=0,
        last_sensor_error_time=None,
        last_pressure_error_time=None,
        control=SimpleNamespace(
            notaus_aktiv=False, notaus_grund=None, notaus_ts=None,
            kompressor_ein=False,
        ),
    )


@pytest.fixture
def api_antwort(monkeypatch):
    import api
    original = api.shared_state
    api.shared_state = _mock_state()
    try:
        yield api.health_status()
    finally:
        api.shared_state = original


def test_alle_manager_felder_kommen_aus_der_api(api_antwort):
    """Jeder vom Manager gelesene Wert muss in der API-Antwort stehen."""
    text, _ = mh.render_health(api_antwort)
    assert "n/a" not in text, f"Manager zeigt Felder als unbekannt:\n{text}"


def test_regelfehler_zeigen_den_echten_wert(api_antwort):
    """Vorher stand hier immer "0" - auch wenn der echte Wert unbekannt war."""
    api_antwort["consecutive_control_errors"] = 7
    text, _ = mh.render_health(api_antwort)
    assert "Regelfehler:  7" in text


def test_sd_karte_wird_angezeigt(api_antwort):
    api_antwort["sd_karte"] = {"sd_ok": True, "hinweise": []}
    text, gesund = mh.render_health(api_antwort)
    assert "SD-Karte:     ok" in text
    assert gesund is True

    api_antwort["sd_karte"] = {
        "sd_ok": False, "hinweise": ["Schreibtest fehlgeschlagen: kein Platz"],
    }
    text, gesund = mh.render_health(api_antwort)
    assert "SD-Karte:     WARNUNG" in text
    assert "kein Platz" in text
    assert gesund is False, "eine kaputte SD-Karte muss als ungesund gelten"


def test_version_wird_aus_der_api_uebernommen(api_antwort):
    api_antwort["version"] = "abc1234"
    text, _ = mh.render_health(api_antwort)
    assert "abc1234" in text


def test_hardware_status_wird_angezeigt(api_antwort):
    api_antwort["hardware"] = {"kompressor_ein": True, "verifizierungsfehler": 3}
    text, _ = mh.render_health(api_antwort)
    assert "Kompressor:   EIN" in text
    assert "3 Kompressor-Verifizierungsfehler" in text
