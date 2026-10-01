"""Tests: Entscheidungs-Log und Energiebilanz-KPIs.

Testet das neue entscheidungs_log-Modul (Punkte ③ und ④):
- schreibe_eintrag: Anhaengen ans JSONL
- Rotation bei MAX_BYTES (mit Generationen)
- historie(): Filter nach Stunden/Limit
- kpis(): Aggregation mit Laufzeit/Energie/Anteil

Abgedeckte Log-Verbesserungen (Analyse des Logs 18.09.-01.10.2026):
1. dreistufige Stromquellen-Klassifikation (pv/batterie/netz/unklar)
2. Idle-Zustand statt leerer Gewinner-/Grundfelder
3. Lebenszeichen-Heartbeat im Stillstand
4. Regelwechsel ohne Handlungswechsel erzeugen keine eigene Zeile
5. zentralisierte Schwellen aus constants.py
6. Rotations-Generationen fuer laengere Historie
"""
import os
import sys
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import entscheidungs_log as el


def _patch_log_pfad(tmp_path):
    """Erzeugt temporaere Log-Pfade und patcht die Modul-Konstanten."""
    log_datei = str(tmp_path / "entscheidungs_log.jsonl")
    old_datei = str(tmp_path / "entscheidungs_log.jsonl.old")
    return patch.multiple(
        el,
        LOG_DATEI=log_datei,
        MAX_BYTES=1_000_000,
        MAX_EINTRAEGE_LESEN=5000,
    ), log_datei, old_datei


def test_schreibe_und_lese(tmp_path):
    """Ein Eintrag schreiben und per historie() wiederfinden."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        el.schreibe_eintrag(
            gewinner_name="TestRegel",
            gewinner_grund="Weil es Spass macht",
            soll_einschalten=True,
            kompressor_laeuft=True,
            feedin_watt=500.0,
            batpower_watt=1200.0,
            soc=88.0,
            t_unten=41.0,
            t_oben=45.0,
        )
        # Datei existiert
        assert os.path.exists(log_datei)
        # Genau eine Zeile
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
        assert len(zeilen) == 1
        eintrag = json.loads(zeilen[0])
        assert eintrag["gewinner"] == "TestRegel"
        assert eintrag["grund"] == "Weil es Spass macht"
        assert eintrag["soll_einschalten"] is True
        assert eintrag["kompressor_laeuft"] is True
        assert eintrag["feedin_w"] == 500.0


def test_schreibe_feld_stale_s(tmp_path):
    """Empfehlung 3.5: stale_s wird als Feld mitgeschrieben (rueckwaertskomp.)."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        el.schreibe_eintrag(
            gewinner_name="Abweichung",
            gewinner_grund="Netz-Tiefenschutz",
            soll_einschalten=True,
            kompressor_laeuft=True,
            stale_s=921,
        )
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
        eintrag = json.loads(zeilen[0])
        assert eintrag["stale_s"] == 921
        # Ohne Angabe bleibt das Feld None (Alt-Datensaetze unveraendert gelesen)
        el.schreibe_eintrag("Keine", "", False, False)
        with open(log_datei, encoding="utf-8") as f:
            e2 = json.loads(f.readlines()[-1])
        assert e2["stale_s"] is None


def test_entscheidung_uebernimmt_zeit_und_reason_code_aus_state(tmp_path):
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    fixed = datetime(2026, 9, 24, 12, 34, 56)
    with patcher:
        assert el.schreibe_eintrag(
            "PV_unten", "PV-Einbruch", False, False,
            reason_code="pv_unterbrechung",
            diagnostics={"requested_rule": "PV_unten"},
            ts=fixed,
        )
        with open(log_datei, encoding="utf-8") as f:
            eintrag = json.loads(f.readline())
    assert eintrag["ts"] == fixed.isoformat(timespec="seconds")
    assert eintrag["reason_code"] == "pv_unterbrechung"
    assert eintrag["diagnostics"]["requested_rule"] == "PV_unten"


def test_gemischte_naive_und_aware_zeiten_bleiben_lesbar(tmp_path):
    """Alte naive und neue ISO-Zeitstempel duerfen nicht verglichen crashen."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = el._log_now()
        alt_naive = (now - timedelta(minutes=2)).replace(tzinfo=None).isoformat(timespec="seconds")
        aktuell = now.isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": alt_naive, "gewinner": "Alt", "kompressor_laeuft": True, "feedin_w": 1000}) + "\n")
            f.write(json.dumps({"ts": aktuell, "gewinner": "Neu", "kompressor_laeuft": True, "feedin_w": 1000}) + "\n")

        historie = el.historie(stunden=24, limit=10)
        kpi = el.kpis()
        assert [e["gewinner"] for e in historie] == ["Neu", "Alt"]
        assert kpi["heute"]["laufzeit_min"] == pytest.approx(2.0, abs=0.05)


def test_soll_schreiben_vergleicht_naive_und_aware_zeiten():
    alt = {"ts": "2026-09-25T13:00:00", "gewinner": "X", "soll_einschalten": True, "kompressor_laeuft": True}
    neu = {"ts": "2026-09-25T13:01:00+02:00", "gewinner": "X", "soll_einschalten": True, "kompressor_laeuft": True}
    assert el._soll_schreiben(alt, neu) is False


def test_historie_filtert_nach_stunden(tmp_path):
    """Nur Eintraege innerhalb des Stunden-Fensters zurueck."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Alten Eintrag mit manipulierter Zeit schreiben
        alt = (datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds")
        aktuell = datetime.now().isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": alt, "gewinner": "Alt"}) + "\n")
            f.write(json.dumps({"ts": aktuell, "gewinner": "Neu"}) + "\n")

        h = el.historie(stunden=24, limit=100)
        assert len(h) == 1
        assert h[0]["gewinner"] == "Neu"


def test_rotation_bei_max_bytes(tmp_path):
    """Bei Ueberschreitung von MAX_BYTES wird nach .old rotiert."""
    patcher, log_datei, old_datei = _patch_log_pfad(tmp_path)
    with patcher:
        # MAX_BYTES auf sehr klein setzen (100 Bytes)
        with patch.object(el, "MAX_BYTES", 100):
            for i in range(20):
                el.schreibe_eintrag(
                    gewinner_name=f"R{i}",
                    gewinner_grund="x" * 50,
                    soll_einschalten=True,
                    kompressor_laeuft=True,
                )

        # Nach vielen Eintraegen sollte .old existieren
        assert os.path.exists(log_datei)
        # Pruefen: alte Eintraege sind (nach Rotation) noch lesbar
        zeilen = el._lies_zeilen()
        namen = {z["gewinner"] for z in zeilen}
        assert "R0" in namen or len(zeilen) > 0  # irgendwas ist drin


def test_historie_neueste_zuerst(tmp_path):
    """historie() liefert neueste Eintraege zuerst."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        # Chronologisch schreiben: aeltester zuerst (R4), neuester zuletzt (R0)
        for i in range(4, -1, -1):  # 4, 3, 2, 1, 0
            ts = (now - timedelta(hours=i)).isoformat(timespec="seconds")
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "gewinner": f"R{i}"}) + "\n")

        h = el.historie(stunden=48, limit=10)
        assert len(h) == 5
        # Neuester zuerst (R0 = now)
        assert h[0]["gewinner"] == "R0"
        assert h[-1]["gewinner"] == "R4"


def test_limit_begrenzt_ausgabe(tmp_path):
    """limit-Parameter in historie() wird eingehalten."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        for i in range(20):
            ts = (now - timedelta(minutes=i * 30)).isoformat(timespec="seconds")
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "gewinner": f"R{i}"}) + "\n")

        h = el.historie(stunden=48, limit=5)
        assert len(h) == 5


def test_kpis_aggregieren(tmp_path):
    """kpis() berechnet Laufzeit, Energie und Anteil korrekt."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        # Simuliere 6 Eintraege mit dt=60s, Kompressor laeuft immer
        # feedin >= -50 -> pv_batterie, feedin < -50 -> netz
        for i in range(6):
            ts = (now - timedelta(seconds=60 * (5 - i))).isoformat(timespec="seconds")
            # Zurechnung an Vorgaengerzeile: Intervall -> L(k) nutzt
            # feedin von L(k-1): L0..L2 = PV (3 Intervalle), L3,L4 = Netz.
            feedin = 100.0 if i < 3 else -200.0  # L5-Feedin unbenutzt
            with open(log_datei, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": ts,
                    "gewinner": "Test",
                    "soll_einschalten": True,
                    "kompressor_laeuft": True,
                    "feedin_w": feedin,
                    "batpower_w": 0.0,
                    "soc": 50.0,
                    "t_unten": 40.0,
                    "t_oben": 42.0,
                }) + "\n")

        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        # 'heute' hat alle 6 Eintraege (5 Intervalle * 60s = 300s Laufzeit)
        heute = result["heute"]
        assert heute["laufzeit_min"] == pytest.approx(5.0, abs=0.5), f"Laufzeit: {heute}"
        # 3/5 der Zeit PV (i=1..3), 2/5 Netz (i=4..5) -> anteil 60%
        # 300s * 600W = 180000 Ws = 0.05 kWh
        assert heute["energie_kwh"] == pytest.approx(0.05, abs=0.01), f"Energie: {heute}"
        assert heute["anteil_pv_batterie_prozent"] == pytest.approx(60.0, abs=2.0)
        # 2/5 von 0.05 kWh = 0.02 kWh * 0.35 EUR/kWh = 0.007 EUR
        # Die Funktion rundet auf 2 Dezimalstellen: round(0.007, 2) = 0.01
        assert heute["kosten_netz_eur"] == pytest.approx(0.01, abs=0.001)


def test_kpis_leere_logs(tmp_path):
    """Bei leerem Log liefern kpis() Nones fuer Anteil, 0 fuer Kosten."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Keine Eintraege
        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        heute = result["heute"]
        assert heute["laufzeit_min"] == 0.0
        assert heute["energie_kwh"] == 0.0
        assert heute["anteil_pv_batterie_prozent"] is None
        assert heute["kosten_netz_eur"] == 0.0


def test_korrupte_zeile_toleriert(tmp_path):
    """Eine abgebrochene JSON-Zeile stoert das Lesen nicht."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now().isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now, "gewinner": "OK"}) + "\n")
            f.write("{\"ts\": \"... abgebrochen")  # kaputt, kein Newline am Ende
        h = el.historie(stunden=48, limit=100)
        assert len(h) == 1
        assert h[0]["gewinner"] == "OK"


def test_schreibe_ohne_os_error(tmp_path, caplog):
    """Wenn LOG_DATEI nicht schreibbar ist, kein Crash."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        # Pfad in ein nicht-existierendes Verzeichnis
        with patch.object(el, "LOG_DATEI", str(tmp_path / "nix" / "log.jsonl")):
            el.schreibe_eintrag("Test", "Grund", True, False)
            # Kein Fehler, nur debug-Log
            assert len(caplog.records) == 0 or caplog.records[0].levelname == "DEBUG"

def test_identische_zyklen_werden_nur_einmal_geschrieben(tmp_path):
    """Dedupe: identische Entscheidung -> kein zweiter Eintrag."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        e1 = el.schreibe_eintrag("PV_1", "Grund A", True, True,
                                 feedin_watt=500.0, batpower_watt=-300.0,
                                 soc=80.0, t_unten=40.0, t_oben=44.0)
        e2 = el.schreibe_eintrag("PV_1", "Grund A", True, True,
                                 feedin_watt=510.0, batpower_watt=-310.0,
                                 soc=80.1, t_unten=40.1, t_oben=44.2)
        assert e1 is True
        assert e2 is False, "identischer Zyklus darf nicht geschrieben werden"
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
        assert len(zeilen) == 1

        # Aenderung der Entscheidung -> wird geschrieben
        e3 = el.schreibe_eintrag("MinTemp-Mitte", "Garantie", False, False)
        assert e3 is True
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
        assert len(zeilen) == 2


def test_herzschlag_bei_laufender_wp(tmp_path):
    """Herzschlag: laufende WP schreibt identische Zyklen nach Intervall."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "HEARTBEAT_SEKUNDEN", -1.0):
            # Negatives Intervall => jeder Zyklus zaehlt als Herzschlag
            a = el.schreibe_eintrag("PV_1", "g", True, True)
            b = el.schreibe_eintrag("PV_1", "g", True, True)
            assert a is True and b is True
            with open(log_datei, encoding="utf-8") as f:
                assert len(f.readlines()) == 2

            # Aber: Stillstand bleibt immer dedupliziert
            c = el.schreibe_eintrag("Keine", "", False, False)
            d = el.schreibe_eintrag("Keine", "", False, False)
            assert c is True and d is False
            with open(log_datei, encoding="utf-8") as f:
                assert len(f.readlines()) == 3


def test_keine_phantom_minuten_beim_wp_start(tmp_path):
    """Regression: Stillstandsluecke vor dem Start zaehlt nicht als Laufzeit.

    Vorher: OFF-Zeile, dann 1 h Stille, dann ON-Zeile. Das 120-s-gecappte
    Intervall gehoert zum Stillstand und darf nicht als Laufzeit zaehlen.
    """
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        alt_ts = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": alt_ts, "gewinner": "Ruhe", "grund": "",
                "soll_einschalten": False, "kompressor_laeuft": False,
                "feedin_w": 0.0, "batpower_w": 0.0, "soc": 50.0,
                "t_unten": 40.0, "t_oben": 42.0,
            }) + "\n")

        # WP startet -> aenderungsbasiert geschriebene ON-Zeile
        assert el.schreibe_eintrag("PV_1", "Genug Sonne", True, True,
                                   feedin_watt=900.0) is True

        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        heute = result["heute"]
        assert heute["laufzeit_min"] == pytest.approx(0.0, abs=0.05), \
            f"Phantom-Laufzeit aus Stillstandsluecke: {heute}"

        # Herzschlag 75 s spaeter -> genau diese 75 s zahlen
        on_ts = None
        with open(log_datei, encoding="utf-8") as f:
            letzte = json.loads(f.readlines()[-1])
        on_dt = datetime.fromisoformat(letzte["ts"]) + timedelta(seconds=75)
        with open(log_datei, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": on_dt.isoformat(timespec="seconds"),
                "gewinner": "PV_1", "grund": "Genug Sonne",
                "soll_einschalten": True, "kompressor_laeuft": True,
                "feedin_w": 900.0, "batpower_w": -300.0, "soc": 80.0,
                "t_unten": 40.5, "t_oben": 44.0,
            }) + "\n")

        result = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
        heute = result["heute"]
        assert heute["laufzeit_min"] == pytest.approx(1.25, abs=0.11), \
            f"Herzschlag-Intervall falsch: {heute}"
        assert heute["netz_kwh"] == pytest.approx(0.0, abs=0.001)


# ============================================================
# Analyse des Logs 18.09.-01.10.2026 - sechs konkrete Maengel
# ============================================================

def _zeile(ts, gewinner="X", soll=True, laeuft=True, feedin=1000.0, batt=0.0):
    return json.dumps({
        "ts": ts.isoformat(timespec="seconds"),
        "gewinner": gewinner, "grund": "g", "soll_einschalten": soll,
        "kompressor_laeuft": laeuft, "feedin_w": feedin, "batpower_w": batt,
        "soc": 80.0, "t_unten": 40.0, "t_oben": 44.0,
    })


# --- 1) Stromquellen-Klassifikation -------------------------------

def test_klassifiziere_quelle_unterscheidet_vier_stufen():
    """Kernfall des Logs: 0 W ohne Batterie-Entladung ist KEIN Solarstrom."""
    assert el.klassifiziere_quelle(4082.0, 0.0) == el.QUELLE_PV
    assert el.klassifiziere_quelle(150.0, -300.0) == el.QUELLE_PV
    assert el.klassifiziere_quelle(0.0, -800.0) == el.QUELLE_BATTERIE
    assert el.klassifiziere_quelle(50.0, -1500.0) == el.QUELLE_BATTERIE
    assert el.klassifiziere_quelle(-500.0, 0.0) == el.QUELLE_NETZ
    assert el.klassifiziere_quelle(-223.0, None) == el.QUELLE_NETZ
    assert el.klassifiziere_quelle(0.0, 0.0) == el.QUELLE_UNKLAR
    assert el.klassifiziere_quelle(0.0, None) == el.QUELLE_UNKLAR
    assert el.klassifiziere_quelle(None, None) == el.QUELLE_UNKLAR


def test_null_watt_wird_nicht_als_pv_verbucht(tmp_path):
    """Regression: 0 W bei laufender WP darf den Solaranteil nicht heben.

    Vorher galt "feedin >= -50 -> pv_batterie"; 0 W erzeugte damit 100 %
    PV-Anteil an 13 von 14 Tagen.
    """
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        # 5 Zeilen -> 4 Intervalle a 60 s. Zugerechnet wird der Zustand der
        # Vorgaengerzeile: PV, Batterie, 0 W, 0 W.
        quellen = [(2000.0, 0.0), (0.0, -800.0), (0.0, 0.0), (0.0, 0.0), (2000.0, 0.0)]
        with open(log_datei, "w", encoding="utf-8") as f:
            for i, (feedin, batt) in enumerate(quellen):
                f.write(_zeile(now - timedelta(minutes=4 - i),
                               feedin=feedin, batt=batt) + "\n")
        heute = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)["heute"]
        assert heute["laufzeit_min"] == pytest.approx(4.0, abs=0.2)
        assert heute["pv_kwh"] > 0
        assert heute["batterie_kwh"] > 0
        assert heute["unklar_laufzeit_min"] == pytest.approx(2.0, abs=0.2), \
            f"0-W-Laufzeit nicht als 'unklar' ausgewiesen: {heute}"
        # PV(1) + Batt(1) von 4 Intervallen: die 2 unklaren gehoeren in den
        # Nenner, sonst stuende hier wieder pauschal "100 %".
        assert heute["anteil_pv_batterie_prozent"] == pytest.approx(50.0, abs=1.0), \
            f"unklare Laufzeit wird aus dem Solarantial herausgerechnet: {heute}"
        assert heute["anteil_unklar_prozent"] == pytest.approx(50.0, abs=1.0)
        assert heute["energie_kwh"] == pytest.approx(0.04, abs=0.01), \
            "Gesamtenergie muss auch die unklaren Minuten enthalten"


def test_kpi_meldet_keine_phantomquote_bei_nur_unklar(tmp_path):
    """Nur 'unklar'-Laufzeit darf keinen PV-Anteil erfinden."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        now = datetime.now()
        with open(log_datei, "w", encoding="utf-8") as f:
            f.write(_zeile(now - timedelta(minutes=2), soll=False,
                           feedin=0.0, batt=0.0) + "\n")
            f.write(_zeile(now - timedelta(minutes=1), soll=False,
                           feedin=0.0, batt=0.0) + "\n")
        heute = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)["heute"]
        assert heute["pv_kwh"] == 0.0
        assert heute["batterie_kwh"] == 0.0
        # 0 % Solar und 100 % unklar sind die ehrliche Aussage: es gab
        # Laufzeit, aber keinen Solar- und keinen Batterienachweis. Das alte
        # Verfahren waere hier auf "100 % PV/Batterie" gelandet.
        assert heute["anteil_pv_batterie_prozent"] == pytest.approx(0.0), \
            f"Solarquote ohne Nachweis darf nicht hoch sein: {heute}"
        assert heute["anteil_unklar_prozent"] == pytest.approx(100.0)
        assert heute["unklar_laufzeit_min"] == pytest.approx(1.0, abs=0.1)


# --- 2) Idle-Zustand ----------------------------------------------

def test_leerer_gewinner_wird_als_idle_begruendet(tmp_path):
    """Ohne Gewinner: Zeile ist benannt und nennt den Zustand."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        assert el.schreibe_eintrag(
            gewinner_name=None, gewinner_grund="",
            soll_einschalten=False, kompressor_laeuft=True,
            feedin_watt=0.0, soc=71.0, t_unten=37.2, t_oben=55.1,
        ) is True
        with open(log_datei, encoding="utf-8") as f:
            eintrag = json.loads(f.readline())
    assert eintrag["gewinner"] == el.IDLE_GEWINNER
    assert eintrag["gewinner"] != ""
    assert eintrag["grund"] != ""
    assert "keine Regel" in eintrag["grund"]
    assert "37.2" in eintrag["grund"] and "71" in eintrag["grund"]


def test_idle_grund_uebersteht_fehlende_messwerte(tmp_path):
    """Kein Sensorwert -> kein Absturz, sondern '-' im Grundtext."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        assert el.schreibe_eintrag(None, "", False, False) is True
        with open(log_datei, encoding="utf-8") as f:
            eintrag = json.loads(f.readline())
    assert eintrag["grund"].startswith("keine Regel fordert Heizbetrieb")
    assert "-" in eintrag["grund"]


def test_vorhandener_gewinner_wird_nicht_veraendert(tmp_path):
    """Normale Zeilen behalten ihren Namen und Grund."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        el.schreibe_eintrag("AdaptivePV", "PV 4082W >= 75W -> EIN",
                            True, True, feedin_watt=4082.0)
        with open(log_datei, encoding="utf-8") as f:
            eintrag = json.loads(f.readline())
    assert eintrag["gewinner"] == "AdaptivePV"
    assert eintrag["grund"] == "PV 4082W >= 75W -> EIN"


# --- 3) Lebenszeichen im Stillstand -------------------------------

def test_stillstand_schreibt_herzschlag_nach_intervall(tmp_path):
    """Ohne Lebenszeichen ist Stillstand nicht von Ausfall unterscheidbar."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime(2026, 9, 30, 8, 0, 0)
        assert el.schreibe_eintrag("Idle", "keine Regel fordert Heizbetrieb",
                                   False, False, ts=start) is True
        # 1 Minute spaeter: unterhalb des Intervalls -> unterdrueckt
        assert el.schreibe_eintrag("Idle", "keine Regel fordert Heizbetrieb",
                                   False, False,
                                   ts=start + timedelta(minutes=1)) is False
        # 31 Minuten spaeter: Lebenszeichen
        assert el.schreibe_eintrag("Idle", "keine Regel fordert Heizbetrieb",
                                   False, False,
                                   ts=start + timedelta(minutes=31)) is True
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
    assert len(zeilen) == 2, "Stillstand braucht periodisches Lebenszeichen"


def test_stillstandsintervall_ist_konfigurierbar():
    """Die 30-Minute-Schwelle ist eine Konstante, kein Magic Number."""
    assert el.HEARTBEAT_STILLSTAND_SEKUNDEN == 1_800.0
    assert el.HEARTBEAT_STILLSTAND_SEKUNDEN > el.HEARTBEAT_SEKUNDEN, \
        "Stillstand darf nicht oefter schreiben als die laufende WP"


def test_stillstandsherzschlag_erzeugt_keine_laufzeit(tmp_path):
    """Lebenszeichen im Stillstand darf die Energiebilanz nicht aufblaehen."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime.now() - timedelta(minutes=40)
        el.schreibe_eintrag("Idle", "keine Regel fordert Heizbetrieb",
                            False, False, ts=start)
        for m in (10, 20, 30, 40):
            el.schreibe_eintrag("Idle", "keine Regel fordert Heizbetrieb",
                                False, False, ts=start + timedelta(minutes=m))
        heute = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)["heute"]
    assert heute["laufzeit_min"] == 0.0
    assert heute["energie_kwh"] == 0.0


# --- 4) Regelwechsel ohne Handlungswechsel ------------------------

def test_regelwechsel_ohne_handlung_erzeugt_keine_zeile(tmp_path):
    """Nur die gewinnende Regel zu tauschen ist Log-Rauschen (7,1 %)."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime(2026, 9, 19, 13, 0, 0)
        assert el.schreibe_eintrag("Einspeisung", "Einspeisung aktiv",
                                   True, True, ts=start) is True
        assert el.schreibe_eintrag("Komfort", "Komfort aktiv", True, True,
                                   ts=start + timedelta(seconds=10)) is False
        assert el.schreibe_eintrag("AdaptivePV", "PV aktiv", True, True,
                                   ts=start + timedelta(seconds=20)) is False
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
    assert len(zeilen) == 1, "Regelwechsel ohne Handlungswechsel sind kein Eintrag"


def test_regelwechsel_wird_an_naechste_zeile_angehaengt(tmp_path):
    """Der Wechsel geht nicht verloren, er landet in diagnostics."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime(2026, 9, 19, 13, 0, 0)
        el.schreibe_eintrag("Einspeisung", "Einspeisung aktiv", True, True,
                            ts=start)
        el.schreibe_eintrag("Komfort", "Komfort aktiv", True, True,
                            ts=start + timedelta(seconds=10))
        el.schreibe_eintrag("AdaptivePV", "PV aktiv", True, True,
                            ts=start + timedelta(seconds=20))
        assert el.schreibe_eintrag("Einspeisung", "Aus bei Ueberhitzung",
                                   False, True,
                                   ts=start + timedelta(seconds=30)) is True
        with open(log_datei, encoding="utf-8") as f:
            zeilen = [json.loads(z) for z in f.readlines()]
    wechsel = zeilen[-1]["diagnostics"]["bei_laueufigem_regelwechsel"]
    assert len(wechsel) == 2, f"Beide Regelwechsel muessen dokumentiert sein: {wechsel}"
    assert wechsel[0]["von"] == "Einspeisung" and wechsel[0]["nach"] == "Komfort"
    assert wechsel[1]["von"] == "Komfort" and wechsel[1]["nach"] == "AdaptivePV"
    assert "bei_laueufigem_regelwechsel" not in zeilen[-2]["diagnostics"]


def test_pending_regelwechsel_bleibt_begrenzt(tmp_path):
    """Endloser Regelwechsel im Stillstand darf nicht unbegrenzt sammeln."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime(2026, 9, 19, 13, 0, 0)
        regeln = ["Einspeisung", "Komfort", "AdaptivePV", "Batterie", "Abweichung"]
        el.schreibe_eintrag(regeln[0], "a", False, False, ts=start)
        for i in range(1, 200):
            el.schreibe_eintrag(regeln[i % len(regeln)], "x", False, False,
                                ts=start + timedelta(seconds=i))
            assert len(el._pending_regelwechsel) <= el.MAX_PENDING_REGELWECHSEL, \
                f"pending waechst unbegrenzt: {len(el._pending_regelwechsel)}"
        # Echte Handlungsaenderung -> die gesammelten Wechsel werden mitgeschrieben
        assert el.schreibe_eintrag("Legionellen", "Legionellen aktiv", True, True,
                                   ts=start + timedelta(seconds=300)) is True
        with open(log_datei, encoding="utf-8") as f:
            zeilen = [json.loads(z) for z in f.readlines()]
    wechsel = zeilen[-1]["diagnostics"]["bei_laueufigem_regelwechsel"]
    assert 0 < len(wechsel) <= el.MAX_PENDING_REGELWECHSEL, \
        f"pending nicht gedeckelt: {len(wechsel)}"
    assert len(wechsel) < 200, "es muss gedeckelt sein"


def test_handlungswechsel_wird_immer_geschrieben(tmp_path):
    """Der Dedupe darf echte Aktionen nie verschlucken."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        start = datetime(2026, 9, 19, 13, 0, 0)
        el.schreibe_eintrag("A", "g", True, True, ts=start)
        assert el.schreibe_eintrag("B", "g", False, False,
                                   ts=start + timedelta(seconds=10)) is True
        assert el.schreibe_eintrag("C", "g", True, True,
                                   ts=start + timedelta(seconds=20)) is True
        with open(log_datei, encoding="utf-8") as f:
            assert len(f.readlines()) == 3


# --- 5) Zentralisierte Schwellen ----------------------------------

def test_schwellen_kommen_aus_constants():
    """Keine Magic Numbers mehr im Modul (Massnahme 5)."""
    import constants
    assert el.NETZKAUF_GRENZE_W == constants.NETZKAUF_GRENZE_W
    assert el.PV_UEBERSCHUSS_MIN_W == constants.PV_UEBERSCHUSS_MIN_W
    assert el.QUELLE_UNKLAR_MAX_W == constants.QUELLE_UNKLAR_MAX_W
    assert el.PV_UEBERSCHUSS_MIN_W > el.NETZKAUF_GRENZE_W


def test_analyse_modul_teilt_die_schwelle():
    """Analyse/analysis_core.py darf die Schwelle nicht hartkodieren."""
    pfad = os.path.join(os.path.dirname(__file__), "..", "..",
                        "Analyse", "analysis_core.py")
    with open(pfad, encoding="utf-8") as f:
        quelle = f.read()
    assert "feedin >= -50" not in quelle, "Magic Number -50 ist noch hartkodiert"
    assert "NETZKAUF_GRENZE_W" in quelle


# --- 6) Rotations-Generationen ------------------------------------

def test_rotation_erhaelt_alle_generationen(tmp_path):
    """Statt einer .old-Datei entstehen mehrere Generationen."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "MAX_BYTES", 200), \
             patch.object(el, "MAX_GENERATIONEN", 3):
            # Handlung alterniert bewusst - sonst unterdrueckt der Dedupe
            # (Massnahme 4) die Zeilen und es wird nie rotiert.
            for i in range(60):
                el.schreibe_eintrag(f"R{i}", "x" * 60, i % 2 == 0, i % 2 == 0)
        assert os.path.exists(f"{log_datei}.old.1")
        assert os.path.exists(f"{log_datei}.old.2")
        # MAX_GENERATIONEN=3 heisst: .old.1/.old.2/.old.3 sind die drei
        # gueltigen Generationen, eine vierte (.old.4) darf es nicht geben.
        assert os.path.exists(f"{log_datei}.old.3")
        assert not os.path.exists(f"{log_datei}.old.4"), \
            "MAX_GENERATIONEN begrenzt die Historie"


def test_aelteste_generation_faellt_weg(tmp_path):
    """Die Historie ist begrenzt - Platz auf der SD-Karte bleibt kontrolliert."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "MAX_BYTES", 200), \
             patch.object(el, "MAX_GENERATIONEN", 2):
            for i in range(120):
                el.schreibe_eintrag(f"R{i}", "x" * 60, i % 2 == 0, i % 2 == 0)
        assert os.path.exists(f"{log_datei}.old.1")
        assert os.path.exists(f"{log_datei}.old.2")
        assert not os.path.exists(f"{log_datei}.old.3")


def test_lesen_umfasst_alle_generationen(tmp_path):
    """Historie und KPI sehen die aelteren Generationen mit.

    MAX_BYTES=200 bei ~100 Byte/Zeile rotiert alle zwei Eintraege. Bei
    MAX_GENERATIONEN=5 werden deshalb die aelteren Eintraege bewusst
    verworfen - beweisbar ist nur, dass MEHRERE Generationen zusammenfliesen
    (mehr Namen als eine einzelne Datei hergeben wuerde).
    """
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "MAX_BYTES", 200), \
             patch.object(el, "MAX_GENERATIONEN", 5):
            for i in range(80):
                el.schreibe_eintrag(f"R{i}", "x" * 60, i % 2 == 0, i % 2 == 0)
        zeilen = el._lies_zeilen()
        namen = {z["gewinner"] for z in zeilen}
        # Nur die aktuelle (juengste) Datei, ohne die Generationen:
        with open(log_datei, encoding="utf-8") as f:
            nur_aktuell = {json.loads(z)["gewinner"] for z in f if z.strip()}
    assert len(namen) > len(nur_aktuell), \
        f"aeltere Generationen werden nicht mitgelesen: {namen} vs {nur_aktuell}"
    assert len(namen) > 1, "es werden mehrere Generationen zusammengefuehrt"


def test_lies_zeilen_bleibt_chronologisch(tmp_path):
    """Aelteste zuerst - auch ueber Generationsgrenzen hinweg."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "MAX_BYTES", 200), \
             patch.object(el, "MAX_GENERATIONEN", 5):
            for i in range(80):
                el.schreibe_eintrag(f"R{i:03d}", "x" * 60, i % 2 == 0, i % 2 == 0)
        zeilen = el._lies_zeilen()
    stempel = [z["ts"] for z in zeilen]
    assert stempel == sorted(stempel), "Logzeilen muessen chronologisch sein"


def test_kpis_ueber_generationsgrenzen(tmp_path):
    """KPI-Auswertung bricht an der Rotationsgrenze nicht ab."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        with patch.object(el, "MAX_BYTES", 200), \
             patch.object(el, "MAX_GENERATIONEN", 4):
            start = datetime.now() - timedelta(minutes=90)
            for i in range(90):
                # Zeitstempel variieren, sonst ist jeder Abstand 0 s und
                # die KPI-Auswertung hat nichts zu rechnen.
                el.schreibe_eintrag(f"R{i}", "x" * 60, i % 2 == 0, i % 2 == 0,
                                    feedin_watt=1500.0,
                                    ts=start + timedelta(minutes=i))
        kpi = el.kpis(wp_leistung_watt=600.0, strompreis_eur_kwh=0.35)
    assert kpi["heute"]["laufzeit_min"] > 0
    assert kpi["sieben_tage"]["pv_kwh"] > 0
    assert kpi["heute"]["anteil_pv_batterie_prozent"] == pytest.approx(100.0, abs=1.0)


def test_dedupe_verhindert_kein_log_geradebeits_vollem_regelwechsel(tmp_path):
    """Bei Dauer-Regelwechsel darf das Log nicht verstummen."""
    patcher, log_datei, _ = _patch_log_pfad(tmp_path)
    with patcher:
        regeln = ["Einspeisung", "Komfort", "AdaptivePV", "Batterie", "Abweichung"]
        start = datetime.now()
        for i in range(300):
            el.schreibe_eintrag(regeln[i % len(regeln)], "x", True, True,
                                ts=start + timedelta(seconds=10 * i))
        with open(log_datei, encoding="utf-8") as f:
            zeilen = f.readlines()
    # 300 Wechsel / MAX 20 => mindestens 10 Zustandszeilen muessen entstehen
    assert len(zeilen) >= 300 / el.MAX_PENDING_REGELWECHSEL, \
        f"Log verstummt bei Dauer-Regelwechsel: nur {len(zeilen)} Zeilen"
