"""Der Beobachtungs-Endpunkt fuer die Zapfungs-Vorhaltung.

Ziel: Im laufenden Betrieb erkennbar machen, OB die Vorhaltung
greift - ohne Journal graben. Der Endpunkt zeigt die gelernte
Zapfungsmenge je Wochentag, die daraus berechnete Vorhaltung und
warnt, wenn der angehobene Sollwert die Boiler-Obergrenze
ueberschreiten wuerde.
"""
import json
from datetime import datetime, timedelta

import pytest

import api
from learning_engine import LearningEngine


def _engine(tmp_path):
    return LearningEngine(data_path=str(tmp_path / "lern.json"))


def _zapfe(eng, start, unten_vor, unten_nach):
    eng.update(start, {"unten": unten_vor, "mittig": 44.0, "oben": 46.0}, False)
    eng.update(start + timedelta(minutes=1),
               {"unten": unten_nach, "mittig": 43.0, "oben": 44.0}, False)
    eng.update(start + timedelta(minutes=8),
               {"unten": unten_nach, "mittig": 43.0, "oben": 44.0}, False)


@pytest.fixture
def mit_engine(monkeypatch, tmp_path):
    """Setzt eine Lernengine in den API-State."""
    monkeypatch.setattr(api, "API_KEY", "test-secret")
    eng = _engine(tmp_path)

    class _State:
        pass

    st = _State()
    st.learning_engine = eng
    monkeypatch.setattr(api, "shared_state", st)
    return eng


class TestEndpunkt:
    def test_ohne_lernengine_meldet_es(self, monkeypatch):
        monkeypatch.setattr(api, "API_KEY", "test-secret")
        monkeypatch.setattr(api, "shared_state", None)
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        assert r["verfuegbar"] is False
        assert "grund" in r

    def test_ohne_zapfungen_ist_die_vorhaltung_null(self, mit_engine):
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        assert r["verfuegbar"] is True
        assert r["vorhaltung_peak_k"] == 0.0
        assert r["zapfmenge_samples"] == 0
        assert "Noch keine Zapfung" in r["hinweis"]

    def test_zeigt_die_menge_je_wochentag(self, mit_engine):
        """Sa braucht mehr als Mo - das muss sichtbar sein."""
        eng = mit_engine
        for i in range(3):
            _zapfe(eng, datetime(2026, 10, 17, 18, 30) + timedelta(days=7 * i),
                   43.0, 27.0)   # Samstag
        for i in range(3):
            _zapfe(eng, datetime(2026, 10, 12, 18, 30) + timedelta(days=7 * i),
                   43.0, 35.0)   # Montag
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        assert r["verfuegbar"] is True
        assert r["je_wochentag"]["Sa"]["menge_l"] > \
            r["je_wochentag"]["Mo"]["menge_l"], (
            "Samstag muss mehr zeigen als Montag")
        assert r["je_wochentag"]["Sa"]["vorhaltung_k"] > \
            r["je_wochentag"]["Mo"]["vorhaltung_k"]
        assert r["je_wochentag"]["Sa"]["samples"] == 3
        assert r["je_wochentag"]["Mo"]["samples"] == 3

    def test_meldet_die_wirksame_vorhaltung(self, mit_engine):
        eng = mit_engine
        for i in range(3):
            _zapfe(eng, datetime(2026, 10, 17, 18, 30) + timedelta(days=7 * i),
                   43.0, 27.0)
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        assert r["vorhaltung_peak_k"] > 0.0
        assert r["soll_mit_vorhaltung_c"] > r["sollwert_c"]
        assert "Vorhaltung aktiv" in r["hinweis"]

    def test_warnt_bei_boiler_max_ueberschreitung(self, mit_engine, monkeypatch):
        """Wenn der angehobene Sollwert ueber der Boiler-Obergrenze
        liegt, muss der Endpunkt warnen."""
        eng = mit_engine
        for i in range(3):
            _zapfe(eng, datetime(2026, 10, 17, 18, 30) + timedelta(days=7 * i),
                   43.0, 10.0)   # extremer Abfall -> riesige Menge
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        # Die Warnung erscheint, wenn der Sollwert die Grenze reisst.
        if r["soll_mit_vorhaltung_c"] > r["boiler_max_c"]:
            assert "ACHTUNG" in r["hinweis"], (
                "Bei Ueberschreitung der Boiler-Obergrenze muss gewarnt werden")

    def test_ist_json_serialisierbar(self, mit_engine):
        """Die API-Antwort muss JSON-faehig sein (FastAPI macht daraus
        eine HTTP-Antwort)."""
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        text = json.dumps(r, ensure_ascii=False)
        assert json.loads(text)["verfuegbar"] is True

    def test_erfordert_api_key(self):
        """Der Endpunkt ist durch einen API-Key geschuetzt (Security-Audit)."""
        import inspect
        src = inspect.getsource(api.debug_zapfvorhaltung)
        assert "_check_api_key" in src, (
            "Der Beobachtungs-Endpunkt muss durch einen API-Key geschuetzt sein")

    def test_config_fallback_wird_gemeldet(self, mit_engine, monkeypatch, caplog):
        """Faellt das Config-Lesen aus, darf der Default nicht still gelten.

        Vorher war der Fallback `except Exception: pass`. Die Antwort
        enthielt dann 48.0 C, als waere das die gemessene Obergrenze - ohne
        jede Spur im Log. Jetzt: WARNUNG + Kennzeichnung in der Antwort.
        """
        def _platze(*_a, **_kw):
            raise RuntimeError("Config nicht lesbar")

        monkeypatch.setattr("json_config.WPSteuerungConfigManager", _platze)

        with caplog.at_level("WARNING"):
            r = api.debug_zapfvorhaltung(x_api_key="test-secret")

        assert r["verfuegbar"] is True
        assert r["boiler_max_c"] == 48.0
        assert r["boiler_max_Quelle"] == "default", (
            "Der Fallback muss in der Antwort erkennbar sein")
        assert any("Boiler-Obergrenze" in m for m in caplog.messages), (
            "Der Fallback muss im Log stehen")

    def test_antwort_belegt_config_als_quelle(self, mit_engine):
        """Im Normalfall ist die Config die Quelle - nicht der Default."""
        r = api.debug_zapfvorhaltung(x_api_key="test-secret")
        assert r["boiler_max_Quelle"] == "config"
