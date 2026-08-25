# -*- coding: utf-8 -*-
"""Anti-Flatter-Tests:

Beobachtetes Problem (Log 2026-08-25 18:52): 28-29 echte Wechsel/h, weil
Batterie-EIN (unten <= 42.0C) und Komfort-AUS (unten >= 42.0C) an derselben
Kante liegen und Sensor-/SOC-Jitter den Gewinner im Sekundentakt pendeln
liess. Dazu: Taktschutz-Meldungen wiederholten sich jeden Loop.

Gegenmassnahmen:
1) Gewinner-Debouncing (_gewinner_debounce): Ein Wechsel zaehlt erst nach
   2 identischen Bewertungen.
2) Batterie-SOC-Hysterese (soc_hysterese_prozent): Weiterlauf schon ab
   min_soc - Hysterese -> 1%-SOC-Ticken brechen einen Lauf nicht ab.
3) Taktschutz-Meldung nur 1x pro Episode.
"""
import sys
from collections import deque
from datetime import datetime, timedelta
from types import SimpleNamespace


sys.stdout.reconfigure(encoding="utf-8")

import priority_control_logic as pcl  # noqa: E402
from priority_control import evaluate_batterie  # noqa: E402


# ─────────────────────────── Hilfs-Fixtures ───────────────────────────

def _state(previous=None):
    return SimpleNamespace(
        local_tz=None,
        control=SimpleNamespace(previous_modus=previous),
    )


def _batt_cfg(**overrides):
    cfg = SimpleNamespace(
        aktiv=True,
        prioritaet=75,
        temperaturfuehler="unten",
        einschalten_bei_c=42.0,
        ausschalten_bei_c=47.0,
        min_soc_prozent=90.0,
        max_netzbezug_watt=-50.0,
        entlastung_max_prozent=15.0,
        min_soc_absolut=10.0,
        soc_hysterese_prozent=2.0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _temps(unten):
    return {"oben": 45.0, "mittig": 44.0, "unten": unten}


# ─────────────────────── 1) Gewinner-Debouncing ───────────────────────

class TestGewinnerDebounce:
    def test_erster_wechsel_benoetigt_zwei_bewertungen(self):
        state = _state(previous="Keine Regel aktiv")
        assert pcl._gewinner_debounce(state, "Batterie") is False
        assert pcl._gewinner_debounce(state, "Batterie") is True
        # Nach Bestaetigung: gleicher Modus -> kein weiterer Wechsel
        assert pcl._gewinner_debounce(state, "Batterie") is False

    def test_einzel_loop_glitch_zaehlt_nicht(self):
        """Ein Loop mit abweichendem Gewinner (Sensor-Tick) ist kein Wechsel."""
        state = _state(previous="Batterie")
        assert pcl._gewinner_debounce(state, "Komfort") is False  # Tick
        assert pcl._gewinner_debounce(state, "Batterie") is False  # zurueck
        assert getattr(state.control, "_pending_modus", None) is None

    def test_pendeln_ohne_bestaetigung_erzeugt_keinen_wechsel(self):
        """A-B-A-B je 1x: nie 2x hintereinander -> nie bestätigt."""
        state = _state(previous="A")
        folge = ["B", "A", "B", "A", "B", "A"]
        assert not any(pcl._gewinner_debounce(state, m) for m in folge)

    def test_debounce_verhindert_taktschutz_ausloesung(self):
        """Integration: Flatter-Folge mit Ein-Tick-Pausen zaehlt kaum Wechsel."""
        state = _state(previous="Keine Regel aktiv")
        hist = deque()
        state.control._wechsel_historie = hist

        # Realistisches Flattern: Batterie <-> Komfort-AUS, aber jeder
        # Zustand haelt meist 2+ Loops; nur Einzel-Ticks pendeln.
        folge = (
            ["Batterie", "Batterie"]           # EIN bestaetigt
            + ["Komfort"]                       # 1 Tick (SOC wackelt)
            + ["Batterie", "Batterie"]          # weiter EIN
            + ["Komfort", "Komfort"]            # AUS bestaetigt
            + ["Batterie"]                      # 1 Tick
            + ["Komfort", "Komfort"]            # bleibt AUS
        )
        for modus in folge:
            if pcl._gewinner_debounce(state, modus):
                pcl._track_wechsel(state, modus)

        # Nur 2 echte Wechsel (Start-Baseline + EIN->AUS), nicht 7 Hubs
        assert len(hist) == 2


# ─────────────────────── 2) Batterie-SOC-Hysterese ────────────────────

class TestBatterieSocHysterese:
    NSP = None  # nachtsperre_start/ende irrelevant (Tag)

    def _bewerte(self, soc, unten, kompressor_ein, cfg=None):
        return evaluate_batterie(
            cfg or _batt_cfg(),
            _temps(unten),
            feedin_watt=0.0,
            soc=soc,
            kompressor_ein=kompressor_ein,
            now_hour=12,
            nachtsperre_start=19,
            nachtsperre_ende=8,
        )

    def test_lauf_endet_nicht_bei_1pct_soc_tick(self):
        """Laufender Kompressor + SOC 89% (1% unter Grenze): Weiterlauf."""
        erg = self._bewerte(soc=89.0, unten=45.0, kompressor_ein=True)
        assert erg.einschalten is True
        assert "Weiterlauf" in erg.grund

    def test_lauf_endet_unterhalb_der_hysterese(self):
        """Erst unterhalb (min_soc - Hysterese) wird geschont."""
        erg = self._bewerte(soc=85.0, unten=45.0, kompressor_ein=True)
        assert erg.einschalten is None
        assert "Schonung" in erg.grund

    def test_neustart_braucht_weiterhin_volle_grenze(self):
        """Ausgeschaltet: EIN weiterhin erst bei >= min_soc (90%)."""
        erg = self._bewerte(soc=89.5, unten=40.0, kompressor_ein=False)
        assert erg.einschalten is None
        assert "Schonung" in erg.grund

        erg = self._bewerte(soc=90.0, unten=40.0, kompressor_ein=False)
        assert erg.einschalten is True

    def test_hysterese_konfigurierbar(self):
        """Hysterese 0 => altes Verhalten (hart an der Kante)."""
        erg = self._bewerte(
            soc=89.9, unten=45.0, kompressor_ein=True,
            cfg=_batt_cfg(soc_hysterese_prozent=0.0),
        )
        assert erg.einschalten is None
        assert "Schonung" in erg.grund

    def test_fehlendes_feld_default_hysterese(self):
        """Alte Configs ohne Feld (SimpleNamespace): Default 2% greift."""
        cfg = _batt_cfg()
        del cfg.soc_hysterese_prozent
        erg = self._bewerte(soc=88.5, unten=45.0, kompressor_ein=True, cfg=cfg)
        assert erg.einschalten is True  # 88.5 >= 90 - 2


# ──────────────── 3) Taktschutz: Meldung 1x pro Episode ────────────────

class TestTaktschutzEpisodeLogging:
    def _state_mit_8_wechseln(self):
        now = datetime.now()
        hist = deque((now - timedelta(minutes=m), "X") for m in range(1, 9))
        return SimpleNamespace(
            local_tz=None,
            control=SimpleNamespace(_wechsel_historie=hist),
            priority_config=SimpleNamespace(
                taktschutz=SimpleNamespace(
                    aktiv=True, max_wechsel_pro_stunde=8,
                    dauer_minuten=120, zusatz_pause_minuten=15,
                )
            ),
        )

    def test_meldung_nur_beim_episodenstart(self, caplog):
        state = self._state_mit_8_wechseln()
        with caplog.at_level("WARNING"):
            for _ in range(5):
                pause = pcl._taktschutz_blockiert(state, state.priority_config)
                assert pause == 15 * 60.0
        warnings = [r for r in caplog.records if r.levelno == __import__("logging").WARNING]
        assert len(warnings) == 1

    def test_entwarnung_wird_gemeldet(self, caplog):
        import logging
        state = self._state_mit_8_wechseln()
        with caplog.at_level(logging.INFO):
            pcl._taktschutz_blockiert(state, state.priority_config)  # Episode startet
            # Wechsel altern aus dem Fenster:
            now = datetime.now()
            state.control._wechsel_historie.clear()
            state.control._wechsel_historie.append(
                (now - timedelta(minutes=2), "X"))
            pcl._taktschutz_blockiert(state, state.priority_config)  # beendet
        texte = [r.getMessage() for r in caplog.records]
        assert any("beendet" in t for t in texte)
