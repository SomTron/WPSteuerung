"""Tests fuer die Spam-Drosselung unbeaufsichtigter Telegram-Alarme.

Hintergrund (Analyse der Absender):

- ``handle_critical_compressor_error`` lief im 10-Sekunden-Takt des
  Hauptloops durch, sobald das Ausschalten dauerhaft fehlschlug
  (GPIO-/Relaisfehler) - bis zu 6 Nachrichten pro Minute.
- ``verify_compressor_running`` prueft einmal pro Minute. Schlaegt auch
  das Ausschalten fehl, kam jede Minute eine Nachricht mit steigender
  Nummer.
- Die Absturz-Meldung beim Start wird bei ``Restart=always`` mit
  ``RestartSec=10`` einmal pro Neustart gesendet.

Alle drei senden **nicht mehr stumm**, sondern gestaffelt: die erste
Meldung geht immer sofort raus, danach wachsen die Abstaende.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytz

STEUERUNG = Path(__file__).resolve().parents[1]
if str(STEUERUNG) not in sys.path:
    sys.path.insert(0, str(STEUERUNG))

import alert_throttle  # noqa: E402

TZ = pytz.timezone("Europe/Berlin")
BASIS = datetime(2026, 9, 25, 14, 0, tzinfo=TZ)


# ------------------------------------------------------------- Grundverhalten


def test_erste_nachricht_geht_sofort_raus():
    state = SimpleNamespace()
    assert alert_throttle.soll_senden(state, "k", BASIS, TZ) is True
    assert alert_throttle.gesendet_anzahl(state, "k") == 1


def test_zweite_nachricht_wartet_auf_die_stufe():
    state = SimpleNamespace()
    alert_throttle.soll_senden(state, "k", BASIS, TZ)
    # Standardstufen: nach 1 Nachricht 5 Minuten warten
    assert alert_throttle.soll_senden(state, "k", BASIS + timedelta(minutes=4), TZ) is False
    assert alert_throttle.soll_senden(state, "k", BASIS + timedelta(minutes=5), TZ) is True


def test_wartezeit_waechst_gestaffelt():
    """Nach jeder gesendeten Nachricht wird der Abstand groesser."""
    state = SimpleNamespace()
    jetzt = BASIS
    senden = []
    for _ in range(5):
        for _ in range(0, 24 * 60, 1):
            if alert_throttle.soll_senden(state, "k", jetzt, TZ):
                senden.append(jetzt)
                break
            jetzt += timedelta(minutes=1)
        jetzt += timedelta(minutes=1)
    assert len(senden) == 5
    abstaende = [
        (senden[i + 1] - senden[i]).total_seconds() / 60 for i in range(len(senden) - 1)
    ]
    assert abstaende == sorted(abstaende), f"Abstaende muessen waachsen: {abstaende}"
    assert abstaende[0] == 5
    assert abstaende[-1] >= 60


def test_reset_erlaubt_sofortige_erneute_meldung():
    state = SimpleNamespace()
    alert_throttle.soll_senden(state, "k", BASIS, TZ)
    alert_throttle.reset(state, "k")
    assert alert_throttle.gesendet_anzahl(state, "k") == 0
    assert alert_throttle.soll_senden(state, "k", BASIS + timedelta(minutes=1), TZ) is True


def test_verschiedene_alarme_teilen_keine_drosselung():
    state = SimpleNamespace()
    assert alert_throttle.soll_senden(state, "a", BASIS, TZ) is True
    assert alert_throttle.soll_senden(state, "b", BASIS, TZ) is True


def test_kaputte_zeitstempel_blockieren_nicht():
    """Ein unbrauchbarer Marker darf keinen Alarm verschlucken."""
    state = SimpleNamespace()
    state.k = "kein-zeitstempel"
    assert alert_throttle.soll_senden(state, "k", BASIS, TZ) is True


def test_verschiedene_stufen_pro_kontext():
    assert alert_throttle.ESCALATION_STEPS_MIN[0] == 0
    assert alert_throttle.VERIFIKATION_STEPS_MIN == (0, 5, 30, 120)
    assert alert_throttle.NEUSTART_STEPS_MIN == (0, 10, 30, 60)


def test_wartezeit_minuten_berechnet_korrekt():
    assert alert_throttle.wartezeit_minuten(0, (0, 5, 15)) == 0
    assert alert_throttle.wartezeit_minuten(1, (0, 5, 15)) == 5
    assert alert_throttle.wartezeit_minuten(2, (0, 5, 15)) == 15
    # Ueberlaeufe werden auf die letzte Stufe geklemmt, nicht indiziert.
    assert alert_throttle.wartezeit_minuten(99, (0, 5, 15)) == 15
    assert alert_throttle.wartezeit_minuten(-5, (0, 5, 15)) == 0


# ------------------------------------------ Kritischer Kompressorfehler


def _safety_state():
    return SimpleNamespace(
        local_tz=TZ,
        config=SimpleNamespace(
            Telegram=SimpleNamespace(CHAT_ID="1", BOT_TOKEN="t")
        ),
        bot_token="t",
    )


def _patch_tg(monkeypatch, safety):
    gesendet: list[str] = []

    async def fake_send(session, chat_id, msg, token, **kwargs):
        gesendet.append(msg)
        return True

    monkeypatch.setattr(safety, "send_telegram_message", fake_send)
    return gesendet


@pytest.mark.asyncio
async def test_kritischer_fehler_im_10s_takt_wird_gedrosselt(monkeypatch):
    """Regression: ohne Drosselung waeren 40 Loops = 40 Nachrichten.

    Erwartet wird: in den ersten 200 Sekunden genau eine Meldung, weil die
    zweite Stufe erst nach 5 Minuten (300 s) wiederholt.
    """
    import safety_logic as safety

    gesendet = _patch_tg(monkeypatch, safety)
    state = _safety_state()
    zeiten = iter(BASIS + timedelta(seconds=10 * i) for i in range(0, 60))
    monkeypatch.setattr(safety, "_now_for_state", lambda s: next(zeiten))

    for _ in range(20):  # 20 Loops = 200 Sekunden
        await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 1, f"erwartet 1, ergab {len(gesendet)}"

    # Weitere 20 Loops bis 400 s: die 5-Minuten-Stufe greift genau einmal.
    for _ in range(20):
        await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 2, f"erwartet 2, ergab {len(gesendet)}"

    # 60 Loops = 600 s: keine dritte Nachricht (naechste Stufe ist 15 min).
    for _ in range(20):
        await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 2, f"erwartet weiterhin 2, ergab {len(gesendet)}"


@pytest.mark.asyncio
async def test_kritischer_fehler_bremst_auf_eine_meldung_pro_stufe(monkeypatch):
    """Gegenprobe: 400 Loops (66 min) duerfen nur wenige Meldungen ergeben."""
    import safety_logic as safety

    gesendet = _patch_tg(monkeypatch, safety)
    state = _safety_state()
    zeiten = iter(BASIS + timedelta(seconds=10 * i) for i in range(0, 400))
    monkeypatch.setattr(safety, "_now_for_state", lambda s: next(zeiten))

    for _ in range(400):
        await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")

    assert len(gesendet) <= 4, (
        f"400 Loops duerfen hoechstens 4 Meldungen ergeben, ergab {len(gesendet)}"
    )
    assert len(gesendet) >= 2


@pytest.mark.asyncio
async def test_kritische_meldung_wiederholt_sich_nach_5_minuten(monkeypatch):
    import safety_logic as safety

    gesendet = _patch_tg(monkeypatch, safety)
    state = _safety_state()
    now = [BASIS]
    monkeypatch.setattr(safety, "_now_for_state", lambda s: now[0])

    await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 1

    # Nach 5 Minuten darf erneut gemeldet werden - ein dauerhafter Fehler
    # bleibt sichtbar.
    now[0] = BASIS + timedelta(minutes=5)
    await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 2


@pytest.mark.asyncio
async def test_kritische_fehlerarten_teilen_keine_drosselung(monkeypatch):
    """Ueberhitzung und Boiler-Maximum sind verschiedene Stoerungen."""
    import safety_logic as safety

    gesendet = _patch_tg(monkeypatch, safety)
    state = _safety_state()
    monkeypatch.setattr(safety, "_now_for_state", lambda s: BASIS)

    await safety.handle_critical_compressor_error(None, state, "bei Ueberhitzung")
    await safety.handle_critical_compressor_error(None, state, "bei Boiler-Maximum")
    assert len(gesendet) == 2


def test_kritisch_key_trennt_fehlerarten():
    import safety_logic as safety

    assert safety._kritisch_key("bei Ueberhitzung") == "ueberhitzung"
    assert safety._kritisch_key("bei Boiler-Maximum") == "boiler_max"
    assert safety._kritisch_key("") == "allgemein"


# ------------------------------------- Kompressorverifizierung / Absturz


def test_verifikation_hat_eigene_stufen():
    """Die Verifikation prueft einmal pro Minute - eigene, kuerzere Stufen."""
    assert alert_throttle.VERIFIKATION_STEPS_MIN[1] == 5
    assert len(alert_throttle.VERIFIKATION_STEPS_MIN) >= 3


def test_verifikations_drosselung_ist_im_code_verankert():
    """Regression: der Versandpfad muss die Drosselung benutzen."""
    text = (STEUERUNG / "safety_logic.py").read_text(encoding="utf-8")
    assert 'alert_throttle.soll_senden(' in text
    assert "tg_verifizierung" in text
    assert "alert_throttle.reset(state, \"tg_verifizierung\")" in text


def test_verifikation_drosselt_minuetentakt():
    """10 Verifikationsfehler in 10 Minuten -> 2 Meldungen, nicht 10."""
    gesendet = []
    state = SimpleNamespace()
    for i in range(10):
        jetzt = BASIS + timedelta(minutes=i)
        if alert_throttle.soll_senden(
            state, "tg_verifizierung", jetzt, TZ,
            alert_throttle.VERIFIKATION_STEPS_MIN,
        ):
            gesendet.append(jetzt)
    # Stufen 0/5/30/120: 0 min und 5 min sind erreicht, 30 min nicht.
    assert len(gesendet) == 2, f"erwartet 2, ergab {len(gesendet)}"


# ------------------------------------------------------- Absturz-Historie


def test_absturzzaehler_zaehlt_und_begrenzt(tmp_path, monkeypatch):
    import startup_diagnose as sd

    datei = tmp_path / "absturz_historie.json"
    monkeypatch.setattr(sd, "ABSTURZ_HISTORIE_DATEI", str(datei))
    jetzt = datetime(2026, 9, 25, 14, 0, tzinfo=TZ)

    assert sd.zaehle_absturze(True, jetzt) == 1
    assert sd.zaehle_absturze(True, jetzt + timedelta(minutes=1)) == 2
    assert sd.zaehle_absturze(True, jetzt + timedelta(minutes=2)) == 3


def test_absturzzaehler_verliert_alte_eintraege(tmp_path, monkeypatch):
    import startup_diagnose as sd

    datei = tmp_path / "absturz_historie.json"
    monkeypatch.setattr(sd, "ABSTURZ_HISTORIE_DATEI", str(datei))
    jetzt = datetime(2026, 9, 25, 14, 0, tzinfo=TZ)

    for i in range(3):
        sd.zaehle_absturze(True, jetzt + timedelta(minutes=i))
    # Zwei Stunden spaeter ist das Fenster (60 min) abgelaufen.
    assert sd.zaehle_absturze(True, jetzt + timedelta(hours=2)) == 1


def test_sauberer_start_setzt_historie_zurueck(tmp_path, monkeypatch):
    import startup_diagnose as sd

    datei = tmp_path / "absturz_historie.json"
    monkeypatch.setattr(sd, "ABSTURZ_HISTORIE_DATEI", str(datei))
    jetzt = datetime(2026, 9, 25, 14, 0, tzinfo=TZ)

    sd.zaehle_absturze(True, jetzt)
    sd.zaehle_absturze(True, jetzt + timedelta(minutes=1))
    assert sd._lade_absturze()

    # Ein sauberer Start bedeutet: kein Sturm, naechste Stoerung sofort.
    assert sd.zaehle_absturze(False, jetzt + timedelta(minutes=2)) == 0
    assert sd._lade_absturze() == []


def test_absturzmeldung_ist_abgestuft():
    """Ohne diese Abstufung wuerde ein Crash-Loop jede 10 s melden."""
    main_text = (STEUERUNG / "main.py").read_text(encoding="utf-8")
    assert "absturze_stunde" in main_text
    assert "tg_absturz_sturm" in main_text
    assert "ABSTURZ_STUFE_1" in main_text
