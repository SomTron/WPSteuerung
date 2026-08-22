# -*- coding: utf-8 -*-
"""
Vertragstest: CSV-Zeile <-> utils.EXPECTED_CSV_HEADER.

Hintergrund: Die Forecast-CSV hatte einen Spalten-Drift (Code schrieb 8 Spalten,
Datei hatte 7-Spalten-Header). Dieser Test verhindert denselben Fehler fuer die
zentrale heizungsdaten.csv: main.build_heizungsdaten_zeile() MUSS exakt so
viele Felder in derselben Reihenfolge liefern wie EXPECTED_CSV_HEADER.

Hinweis: Der alte Kommentar in utils.py behauptete "19 Spalten" -- korrekt
sind 20 (der Konsistenztest hats aufgedeckt).
"""
import os
import re
import sys
from types import SimpleNamespace

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils import EXPECTED_CSV_HEADER
from main import build_heizungsdaten_zeile


def baue_state():
    """Minimaler State mit allen von build_heizungsdaten_zeile gelesenen Feldern."""
    return SimpleNamespace(
        sensors=SimpleNamespace(t_oben=45.5, t_unten=44.0, t_mittig=43.2,
                                t_boiler=47.1, t_verd=12.3),
        control=SimpleNamespace(kompressor_ein=True,
                                aktueller_einschaltpunkt=48.0,
                                aktueller_ausschaltpunkt=52.0,
                                solar_ueberschuss_aktiv=False),
        solar=SimpleNamespace(last_api_data={"acpower": 1500, "powerdc1": 800,
                                             "powerdc2": 700, "consumeenergy": 12345},
                              feedinpower=0, batpower=0, soc=None,
                              forecast_tomorrow=2.75),
        urlaubsmodus_aktiv=False,
    )


# Semantische Verankerung: Index -> erwartete Header-Spalte + Gueltigkeitspruefung
SPALTEN_VERTRAG = {
    0: ("Zeitstempel", lambda v: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(v)))),
    6: ("Kompressor", lambda v: str(v) in {"0", "1"}),
    16: ("Solar", lambda v: str(v) in {"0", "1"}),          # Solarueberschuss
    17: ("Urlaubsmodus", lambda v: str(v) in {"0", "1"}),
    18: ("PowerSource", lambda v: str(v) in {"Netz", "Solar", "Batterie"}),
}


class TestCsvHeaderVertrag:

    def test_header_hat_20_spalten(self):
        """Pinnt den Vertrag (alter utils-Kommentar sagte faelschlich 19)."""
        assert len(EXPECTED_CSV_HEADER) == 20, (
            f"EXPECTED_CSV_HEADER hat {len(EXPECTED_CSV_HEADER)} Spalten "
            f"(erwartet 20). Falls beabsichtigt: build_heizungsdaten_zeile() "
            f"in main.py anpassen und diesen Test aktualisieren!"
        )

    def test_zeile_hat_gleiche_anzahl_felder_wie_header(self):
        zeile = build_heizungsdaten_zeile(baue_state())
        assert len(zeile) == len(EXPECTED_CSV_HEADER), (
            f"CSV-Zeile hat {len(zeile)} Felder, Header {len(EXPECTED_CSV_HEADER)} "
            f"Spalten -> Daten waeren verschoben (wie bei sonnen_prognose.csv passiert)!"
        )

    def test_joined_zeile_passt_zum_header(self):
        """Ende-zu-Ende: Was in die Datei geschrieben wird, muss zum Header passen."""
        zeile = build_heizungsdaten_zeile(baue_state())
        csv_text = ",".join(zeile)
        assert len(csv_text.split(",")) == len(EXPECTED_CSV_HEADER)

    def test_semantische_zuordnung_index_zu_spalte(self):
        """Reihenfolge-Drift-Erkennung: verankerte Indizes muessen zur
        gleichnamigen Header-Spalte passen und gueltige Werte liefern."""
        zeile = build_heizungsdaten_zeile(baue_state())
        for idx, (spaltenname, gueltig) in SPALTEN_VERTRAG.items():
            header_name = EXPECTED_CSV_HEADER[idx]
            assert header_name.startswith(spaltenname), (
                f"Header[{idx}]={header_name!r}, erwartet Start mit {spaltenname!r} "
                f"-> Reihenfolge wurde geaendert?"
            )
            assert gueltig(zeile[idx]), (
                f"Zeile[{idx}]={zeile[idx]!r} ungueltig fuer {header_name!r}"
            )


class TestZeilenSemantik:

    def test_power_source_default_netz(self):
        state = baue_state()
        state.solar.feedinpower = 0
        state.solar.batpower = 0
        assert build_heizungsdaten_zeile(state)[18] == "Netz"

    def test_power_source_solar_und_batterie(self):
        state = baue_state()
        state.solar.feedinpower = 2500
        assert build_heizungsdaten_zeile(state)[18] == "Solar"

        state.solar.feedinpower = 0
        state.solar.batpower = 3000
        assert build_heizungsdaten_zeile(state)[18] == "Batterie"

    def test_none_werte_werden_na(self):
        state = baue_state()
        state.sensors.t_oben = None
        state.solar.soc = None
        zeile = build_heizungsdaten_zeile(state)
        assert zeile[1] == "N/A"   # T_Oben
        assert zeile[10] == "N/A"  # SOC

    def test_kompressor_als_0_1(self):
        state = baue_state()
        assert build_heizungsdaten_zeile(state)[6] == "1"
        state.control.kompressor_ein = False
        assert build_heizungsdaten_zeile(state)[6] == "0"

    def test_forecast_morgen_in_letzter_spalte(self):
        zeile = build_heizungsdaten_zeile(baue_state())
        assert EXPECTED_CSV_HEADER[-1] == "Prognose_Morgen"
        assert zeile[-1] == "2.75"
