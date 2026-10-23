"""Tests fuer den /status-Hotspot: historisches 14-Tage-PV-Mittel (Wh).

Hintergrund: /status wird vom Dashboard alle 5 s abgefragt und las frueher bei
jedem Aufruf die KOMPLETTE heizungsdaten.csv mit pd.read_csv() (alle 20
Spalten, >120k Zeilen) - auf dem Pi ein RAM-Spike, der am 15.09. zum OOM-Kill
fuehrte (journal: status=9/KILL). Jetzt: stdlib csv, nur Kopf + Tail-Teil,
nur die zwei benoetigten Spalten und ein 30-Minuten-Cache.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import api  # noqa: E402


@pytest.fixture(autouse=True)
def _cache_leeren():
    """Cache vor/nach jedem Test zuruecksetzen (Modulzustand!)."""
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None
    api._history_cache_clear()
    yield
    api._HIST_WH_QM_CACHE["zeit"] = None
    api._HIST_WH_QM_CACHE["wert"] = None
    api._history_cache_clear()


# --- /history: Cache und RAM ---------------------------------------------
#
# Jeder Klick auf ein Zeitfenster las die CSV neu: 17 MB Datei, bis zu
# 100.000 Tailzeilen als Dict mit 21 Spalten. Gemessen auf dem Pi-
# Rechner: 121 MB RAM-Spitze bei 0,5 s Rechenzeit - bei einem 7-Tage-
# Fenster also ein RAM-Spike, der den Dienst killen kann (vgl. den
# OOM-Kill am 15.09. im Modul-Docstring).
#
# Zwei Massnahmen:
# 1. Die Zeilen kommen als kompaktes Tupel mit nur den 8 benoetigten
#    Spalten (121 MB -> 44 MB).
# 2. Ein LRU-Cache beantwortet wiederholte Abfragen aus dem Speicher
#    (0,5 s -> 0,1 ms).

HIST_KOPF = (
    "Zeitstempel,T_Oben,T_Mittig,T_Unten,T_Verd,Kompressor,ACPower,"
    "FeedinPower,BatPower,SOC,PowerDC1,PowerDC2,ConsumeEnergy,"
    "Einschaltpunkt,Ausschaltpunkt,Solarueberschuss,Urlaubsmodus,"
    "PowerSource,Prognose_Morgen\n"
)


def _history_csv(pfad, anzahl=2000, abstand_sec=10):
    """Schreibt eine CSV mit aktuellen Zeitstempeln (wie im Betrieb)."""
    jetzt = datetime.now()
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(HIST_KOPF)
        for i in range(anzahl):
            ts = (jetzt - timedelta(seconds=abstand_sec * i)).strftime("%Y-%m-%d %H:%M:%S")
            f.write(
                f"{ts},42.0,41.0,40.0,10.0,1,700.0,600.0,0.0,55.0,350.0,"
                "350.0,1900.0,42.0,48.0,0,0,Netz,4.6\n"
            )


def test_read_csv_tail_liefert_tupel_mit_acht_spalten(tmp_path):
    """Der Tail-Reader haelt nur die benoetigten Spalten - kompakt im RAM."""
    p = tmp_path / "h.csv"
    _history_csv(str(p), anzahl=50)
    rows, quality = api._read_csv_tail(str(p), max_rows=25)
    assert len(rows) == 25
    assert quality["invalid_rows"] == 0
    # Tupel mit genau 8 Werten (nicht Dict mit 21 Spalten).
    assert all(isinstance(r, tuple) and len(r) == 8 for r in rows)
    # Der Zeitstempel steht vorn, die Temperaturen folgen.
    assert rows[0][0].startswith("20")
    assert rows[0][1] == "42.0"


def test_read_csv_tail_liest_die_juengsten_zeilen(tmp_path):
    """Der Tail muss die LETZTEN Zeilen liefern, nicht die ersten."""
    p = tmp_path / "h.csv"
    with open(p, "w", encoding="utf-8") as f:
        f.write(HIST_KOPF)
        for i in range(100):
            f.write(
                f"2026-09-01 10:{i:02d}:00,1,2,3,4,AUS,0,0,0,0,0,0,0,42,48,0,0,Netz,0\n"
            )
    rows, _ = api._read_csv_tail(str(p), max_rows=5)
    assert len(rows) == 5
    # Chronologische Reihenfolge: die LETZTEN fuenf Zeilen der Datei
    # (i=95..99).
    assert rows[-1][0] == "2026-09-01 10:99:00"
    assert rows[0][0] == "2026-09-01 10:95:00"


def test_history_cache_liefert_beim_zweiten_aufruf_dieselben_daten(tmp_path, monkeypatch):
    """Der zweite Aufruf kommt aus dem Cache - ohne CSV-Lesen."""
    from types import SimpleNamespace
    import pytz

    p = tmp_path / "h.csv"
    _history_csv(str(p), anzahl=500)
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(p))
    monkeypatch.setattr(
        api, "shared_state",
        SimpleNamespace(local_tz=pytz.timezone("Europe/Berlin"), clock=None),
    )
    api._history_cache_clear()

    # Lesezaehler: zaehlt die tatsaechlichen CSV-Zugriffe. Seit dem
    # RAM-Umbau (23.10.2026) liest die Route ueber `_sammle_history`
    # (Streaming + Minuten-Verdichtung) statt ueber `_read_csv_tail`.
    aufrufe = {"n": 0}
    original = api._sammle_history

    def spion(*args, **kwargs):
        aufrufe["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "_sammle_history", spion)

    erste = api.get_history(hours=24)
    assert aufrufe["n"] == 1
    zweite = api.get_history(hours=24)
    assert aufrufe["n"] == 1, "Der zweite Aufruf muss aus dem Cache kommen"
    assert erste == zweite
    # Ein anderes Zeitfenster ist ein eigener Cache-Eintrag.
    api.get_history(hours=1)
    assert aufrufe["n"] == 2


def test_history_cache_wird_bei_neuen_csv_daten_verworfen(tmp_path, monkeypatch):
    """Die CSV waechst alle 10 s - der Cache muss das merken."""
    from types import SimpleNamespace
    import pytz

    p = tmp_path / "h.csv"
    _history_csv(str(p), anzahl=200)
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(p))
    monkeypatch.setattr(
        api, "shared_state",
        SimpleNamespace(local_tz=pytz.timezone("Europe/Berlin"), clock=None),
    )
    api._history_cache_clear()

    api.get_history(hours=24)
    key_vorher = api._history_cache_key(str(p), 24)
    with open(p, "a", encoding="utf-8") as f:
        f.write(
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},43,42,41,11,1,700,600,0,55,"
            "350,350,1900,42,48,0,0,Netz,4.6\n"
        )
    key_nachher = api._history_cache_key(str(p), 24)
    assert key_vorher != key_nachher, (
        "Der Cache-Key muss die CSV-Groesse enthalten, sonst bleibt die "
        "Anzeige bis zu 30 s alt"
    )


def test_history_cache_ist_begrenzt(tmp_path, monkeypatch):
    """Der Cache darf die RAM des Pi nicht unbegrenzt aufblaest."""
    from types import SimpleNamespace
    import pytz

    p = tmp_path / "h.csv"
    _history_csv(str(p), anzahl=50)
    monkeypatch.setattr(api, "HEIZUNGSDATEN_CSV", str(p))
    monkeypatch.setattr(
        api, "shared_state",
        SimpleNamespace(local_tz=pytz.timezone("Europe/Berlin"), clock=None),
    )
    api._history_cache_clear()
    for stunden in range(1, 12):
        api.get_history(hours=stunden)
    assert len(api._HISTORY_CACHE) <= api.HISTORY_CACHE_MAX_EINTRAEGE


def _csv_schreiben(pfad, tage=3, feedin=1000.0):
    """Schreibt je Tag 4 Viertelstunden-Zeilen (Summe je Tag = 1000 Wh)."""
    jetzt = datetime.now()
    zeilen = ["Zeitstempel,FeedinPower,BatPower,ACPower"]
    for t in reversed(range(tage)):
        tag = (jetzt - timedelta(days=t)).strftime("%Y-%m-%d")
        for minute in (0, 15, 30, 45):
            stunde = 12 if minute < 60 else 12
            zeilen.append(f"{tag} {stunde:02d}:{minute:02d}:00,{feedin},0.0,500.0")
    with open(pfad, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")


def test_history_wird_zeitbasiert_gedownsampelt_und_meldet_partial():
    rows = []
    base = api.datetime(2026, 9, 1, 0, 0)
    for minute in range(0, 180, 1):
        rows.append({
            "Zeitstempel": (base + api.timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S"),
            "T_Oben": "40", "T_Mittig": "40", "T_Unten": "40", "T_Verd": "10",
            "Kompressor": "AUS", "Einschaltpunkt": "42", "Ausschaltpunkt": "48",
        })
    selected, quality = api._downsample_history(
        rows, cutoff=base, max_points=500
    )
    assert len(selected) < len(rows)
    assert quality["selected_rows"] == len(selected)
    assert quality["raw_rows_in_range"] == len(rows)
    assert quality["sampling_seconds"] >= 60
    assert quality["partial"] is False
    assert quality["coverage_start"] == rows[0]["Zeitstempel"]


def test_history_meldet_veraltetes_csv_ende(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import pytz

    p = tmp_path / "hist.csv"
    p.write_text(
        "Zeitstempel,T_Oben,T_Mittig,T_Unten,T_Verd,Kompressor,Einschaltpunkt,Ausschaltpunkt\n"
        "2026-09-01 12:00:00,40,40,40,10,AUS,42,48\n",
        encoding="utf-8",
    )
    old_state = api.shared_state
    old_path = api.HEIZUNGSDATEN_CSV
    try:
        api.HEIZUNGSDATEN_CSV = str(p)
        api.shared_state = SimpleNamespace(local_tz=pytz.timezone("Europe/Berlin"))
        result = api.get_history(hours=1)
    finally:
        api.shared_state = old_state
        api.HEIZUNGSDATEN_CSV = old_path
    assert result["quality"]["partial"] is True
    assert "stale_end_s" in result["quality"]


def test_berechnet_mittel_ohne_pandas(tmp_path):
    """Korrektes Ergebnis - und zwar ohne pandas (stdlib csv)."""
    p = tmp_path / "hist.csv"
    _csv_schreiben(str(p))

    wert = api._historisches_wh_qm(str(p))

    # 4x 1000 W * 15/60 h = 1000 Wh pro Tag
    assert wert == pytest.approx(1000.0)


def test_integriert_echte_sample_abstaende_statt_fester_15_minuten(tmp_path):
    """14-Sekunden-Takt darf nicht wie ein 15-Minuten-Takt behandelt werden."""
    p = tmp_path / "hist.csv"
    p.write_text(
        "Zeitstempel,FeedinPower\n"
        "2026-09-01 12:00:00,1000\n"
        "2026-09-01 12:00:14,1000\n"
        "2026-09-01 12:00:28,1000\n",
        encoding="utf-8",
    )

    wert = api._berechne_hist_wh_qm(
        str(p), tage=14, jetzt=api.datetime(2026, 9, 1, 12, 1)
    )
    # 3 Intervalle a 14 s; der letzte Wert wird mit dem letzten plausiblen
    # Intervall geschaetzt: 1000 W * 42 s / 3600.
    assert wert == pytest.approx(1000.0 * 42.0 / 3600.0)


def test_grosse_zeitluecke_wird_nicht_integriert(tmp_path):
    p = tmp_path / "hist.csv"
    p.write_text(
        "Zeitstempel,FeedinPower\n"
        "2026-09-01 12:00:00,1000\n"
        "2026-09-01 13:00:00,1000\n",
        encoding="utf-8",
    )
    assert api._berechne_hist_wh_qm(
        str(p), tage=14, jetzt=api.datetime(2026, 9, 1, 13, 1)
    ) is None


def test_excel_seriennummer_wird_erkannt():
    """Die CSV kann Excel-Seriennummern enthalten (Datum als Zahl)."""
    # 45000 Tage seit 1899-12-30
    assert api._parse_zeitstempel("45000") is not None
    assert api._parse_zeitstempel("2026-09-15 12:00:00") == datetime(2026, 9, 15, 12, 0, 0)
    assert api._parse_zeitstempel("") is None
    assert api._parse_zeitstempel("keinDatum") is None


def test_aware_cache_und_state_zeit_kommen_mit_naiven_csv_daten_klar(tmp_path):
    """Status darf keine aware/naive Vergleichsfehler mehr loggen."""
    import pytz
    p = tmp_path / "hist.csv"
    _csv_schreiben(str(p))
    state = pytz.timezone("Europe/Berlin")
    old_state = api.shared_state
    try:
        api.shared_state = type("State", (), {"local_tz": state, "clock": None})()
        api._HIST_WH_QM_CACHE["zeit"] = state.localize(datetime.now() - timedelta(seconds=1))
        api._HIST_WH_QM_CACHE["wert"] = 1000.0
        wert = api._historisches_wh_qm(str(p))
    finally:
        api.shared_state = old_state
    assert wert == pytest.approx(1000.0)


def test_zweiter_aufruf_kommt_aus_dem_cache(tmp_path, monkeypatch):
    p = tmp_path / "hist.csv"
    _csv_schreiben(str(p))

    aufrufe = {"n": 0}
    echt = api._berechne_hist_wh_qm

    def spion(csv_path):
        aufrufe["n"] += 1
        return echt(csv_path)

    monkeypatch.setattr(api, "_berechne_hist_wh_qm", spion)

    erst = api._historisches_wh_qm(str(p))
    zweit = api._historisches_wh_qm(str(p))

    assert erst == zweit
    assert aufrufe["n"] == 1, "Der 5-s-Poll darf die CSV nicht jedes Mal neu lesen"


def test_fehlende_datei_liefert_none(tmp_path):
    fehlt = tmp_path / "gibts_nicht.csv"
    assert api._historisches_wh_qm(str(fehlt)) is None


def test_solar_datenalter_wird_gemeldet():
    """B4b: _solar_age_s liefert das Alter der Solax-Livedaten in Sekunden."""
    import pytz
    from types import SimpleNamespace

    tz = pytz.timezone("Europe/Berlin")
    jetzt = datetime.now(tz)
    old_state = api.shared_state
    try:
        api.shared_state = SimpleNamespace(
            local_tz=tz,
            solar=SimpleNamespace(last_api_call=jetzt - timedelta(minutes=4)),
        )
        alter = api._solar_age_s()
    finally:
        api.shared_state = old_state
    assert alter is not None
    assert 200 <= alter <= 260


def test_solar_datenalter_ohne_zeitstempel_ist_none():
    from types import SimpleNamespace

    old_state = api.shared_state
    try:
        api.shared_state = SimpleNamespace(
            local_tz=None, solar=SimpleNamespace(last_api_call=None))
        assert api._solar_age_s() is None
    finally:
        api.shared_state = old_state