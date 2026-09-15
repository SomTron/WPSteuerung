# -*- coding: utf-8 -*-
"""RAM-Schutz-Tests: kein Voll-Read der CSV, matplotlib nicht beim Start laden.

Hintergrund (OOM-Kill auf dem Pi, 15.09.): Der Kernel hat den Prozess mit
SIGKILL beendet (journal: 'code=killed, status=9/KILL'). Ursachen-Kandidaten,
die hier abgesichert werden:
  * /debug/csv las die komplette heizungsdaten.csv mit pd.read_csv()
  * telegram_charts importierte matplotlib schon beim Programmstart
    (gemessen: ~25-40 MB RSS dauerhaft, nur fuer seltene Telegram-Charts)
"""
import os
import subprocess
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402

STEUERUNG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_debug_csv_liest_nur_tail(monkeypatch, tmp_path):
    """Der Debug-Endpoint darf die CSV nicht komplett in pandas laden."""
    p = tmp_path / "heizungsdaten.csv"
    p.write_text(
        "Zeitstempel,FeedinPower,BatPower\n"
        "2026-09-15 10:00:00,100,0\n"
        "2026-09-15 10:10:00,200,0\n"
        "2026-09-15 10:20:00,300,0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(p))

    r = api.debug_csv()

    assert r["csv_exists"] is True
    assert r["rows"] == 3
    assert r["columns"] == ["Zeitstempel", "FeedinPower", "BatPower"]
    assert r["first_timestamp"] == "2026-09-15 10:00:00"
    assert r["last_timestamp"] == "2026-09-15 10:20:00"
    assert r["column_types"]["FeedinPower"] == "float"
    assert r["column_types"]["Zeitstempel"] == "text"


def test_debug_csv_ohne_datei(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(tmp_path / "fehlt.csv"))
    r = api.debug_csv()
    assert r["csv_exists"] is False
    assert "rows" not in r


def test_telegram_charts_laedt_matplotlib_erst_bei_bedarf():
    """Regression: matplotlib beim Start kostete auf dem Pi dauerhaft RAM."""
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import telegram_charts; "
        "print('GELADEN' if 'matplotlib' in sys.modules else 'NICHT_GELADEN')" % STEUERUNG
    )
    ergebnis = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120, cwd=STEUERUNG,
    )
    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "NICHT_GELADEN" in ergebnis.stdout, (
        "matplotlib wird beim Import von telegram_charts geladen - RAM-Grundlast!"
    )


def _in_frischem_prozess(code: str) -> str:
    """Fuehrt Code in einem frischen Interpreter aus (Modulzustand isoliert)."""
    ergebnis = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=180, cwd=STEUERUNG,
    )
    assert ergebnis.returncode == 0, ergebnis.stderr
    return ergebnis.stdout


def test_telegram_charts_laedt_pandas_nicht_beim_import():
    """Regression: pandas beim Start kostete ~70 MB RSS (pandas ist nicht gratis)."""
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import telegram_charts; "
        "print('GELADEN' if 'pandas' in sys.modules else 'NICHT_GELADEN')" % STEUERUNG
    )
    assert "NICHT_GELADEN" in _in_frischem_prozess(code), (
        "pandas wird beim Import von telegram_charts geladen - RAM-Grundlast!"
    )


def test_api_laedt_pandas_nicht_beim_import():
    """Der Web-/API-Pfad darf pandas gar nicht erst laden (~70 MB RSS)."""
    code = (
        "import sys; sys.path.insert(0, r'%s'); "
        "import api; "
        "print('GELADEN' if 'pandas' in sys.modules else 'NICHT_GELADEN')" % STEUERUNG
    )
    assert "NICHT_GELADEN" in _in_frischem_prozess(code), (
        "api.py importiert pandas - das bindet dauerhaft ~70 MB (OOM-Gefahr)!"
    )


def test_csv_auswertungen_laufen_ohne_pandas(tmp_path):
    """/status-Historie und /debug/csv muessen mit stdlib csv auskommen."""
    csv = tmp_path / "heizungsdaten.csv"
    csv.write_text(
        "Zeitstempel,FeedinPower,BatPower\n2026-09-15 10:00:00,100,0\n",
        encoding="utf-8",
    )
    code = (
        "import sys; sys.path.insert(0, r'{d}'); import api; "
        "api._berechne_hist_wh_qm(r'{c}'); "
        "api._csv_spaltentypen(['a'], [['1']]); "
        "print('GELADEN' if 'pandas' in sys.modules else 'NICHT_GELADEN')"
    ).format(d=STEUERUNG, c=csv)
    assert "NICHT_GELADEN" in _in_frischem_prozess(code), (
        "Die CSV-Auswertung zieht pandas nach - RAM-Grundlast trotz stdlib-Code!"
    )