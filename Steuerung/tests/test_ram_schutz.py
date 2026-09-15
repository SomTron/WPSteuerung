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

import pandas as pd

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

    aufrufe = []
    echt = pd.read_csv

    def spion(*args, **kwargs):
        aufrufe.append(args[0] if args else kwargs.get("filepath_or_buffer"))
        return echt(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", spion)

    r = api.debug_csv()

    assert r["csv_exists"] is True
    assert r["rows"] == 3
    assert r["columns"] == ["Zeitstempel", "FeedinPower", "BatPower"]
    assert r["first_timestamp"] == "2026-09-15 10:00:00"
    assert r["last_timestamp"] == "2026-09-15 10:20:00"

    assert aufrufe, "read_csv wurde nicht aufgerufen"
    assert all(not isinstance(a, str) or a != str(p) for a in aufrufe), (
        "Der Dateipfad darf NICHT direkt an pd.read_csv gehen (Voll-Read = OOM-Gefahr)"
    )


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