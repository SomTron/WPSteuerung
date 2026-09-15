# -*- coding: utf-8 -*-
"""Tests fuer die Startup-Diagnose: OOM-Erkennung + Speicherwerte.

Hintergrund (15.09.): Ein OOM-Kill sah im Steuerungs-Log wie ein normaler
Neustart aus. Diese Tests sichern, dass ein unsauberes Ende erkannt und als
OOM gemeldet wird - ohne root, ohne Linux (/proc wird injiziert).
"""
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import startup_diagnose as sd  # noqa: E402


# --- Statusdatei: unsauberes Ende erkennen ------------------------------------

def test_erster_start_ist_kein_fehler(tmp_path):
    info = sd.pruefe_letzten_lauf(str(tmp_path / "letzter_lauf.json"))
    assert info["unsauber"] is False
    assert info["erster_start"] is True


def test_unsauberes_ende_wird_erkannt(tmp_path):
    p = tmp_path / "letzter_lauf.json"
    sd.markiere_lauf_start(str(p))

    info = sd.pruefe_letzten_lauf(str(p))

    assert info["unsauber"] is True
    assert info["vorheriger_start"]
    assert info["vorheriges_ende"] is None


def test_sauberes_ende_wird_erkannt(tmp_path):
    p = tmp_path / "letzter_lauf.json"
    sd.markiere_lauf_start(str(p))
    sd.markiere_sauberes_ende(str(p))

    info = sd.pruefe_letzten_lauf(str(p))

    assert info["unsauber"] is False
    assert info["vorheriges_ende"]


def test_kaputte_statusdatei_gilt_als_erster_start(tmp_path):
    p = tmp_path / "letzter_lauf.json"
    p.write_text("{kein json", encoding="utf-8")
    assert sd.pruefe_letzten_lauf(str(p))["erster_start"] is True


# --- OOM-Erkennung ------------------------------------------------------------

def test_oom_zeile_wird_erkannt():
    text = (
        "Sep 15 12:08:17 raspberrypiz2 kernel: Out of memory: Killed process 571 "
        "(python) total-vm:2071252kB, anon-rss:257104kB file-rss:1576kB\n"
    )
    hinweis = sd.oom_hinweis_aus_text(text)
    assert hinweis is not None
    assert "Killed process 571" in hinweis


def test_normales_kernel_log_ergibt_keinen_oom_hinweis():
    assert sd.oom_hinweis_aus_text("Sep 15 12:00:01 kernel: wlan0: link is ready") is None
    assert sd.oom_hinweis_aus_text("") is None
    assert sd.oom_hinweis_aus_text(None) is None


def test_kernel_log_wird_nur_bei_unsauberem_ende_gelesen(tmp_path, monkeypatch):
    p = tmp_path / "letzter_lauf.json"
    aufrufe = {"n": 0}

    def fake_kernel_log():
        aufrufe["n"] += 1
        return "Out of memory: Killed process 571 (python)"

    monkeypatch.setattr(sd, "kernel_log_lesen", fake_kernel_log)
    monkeypatch.setattr(sd, "LAUF_STATUS_DATEI", str(p))

    # sauberer Vorlauf -> kein Kernel-Log-Zugriff, keine OOM-Meldung
    sd.markiere_lauf_start(str(p))
    sd.markiere_sauberes_ende(str(p))
    info = sd.pruefe_lauf_start_grund()
    assert info["unsauber"] is False
    assert info["oom_hinweis"] is None
    assert aufrufe["n"] == 0

    # harter Abbruch (z. B. OOM-Kill) -> Kernel-Log wird gelesen
    sd.markiere_lauf_start(str(p))
    info = sd.pruefe_lauf_start_grund()
    assert info["unsauber"] is True
    assert info["oom_hinweis"] and "Killed process" in info["oom_hinweis"]
    assert aufrufe["n"] == 1


# --- Speicherwerte ------------------------------------------------------------

def test_speicherwerte_werden_gelesen_und_formatiert(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:         425984 kB\n"
        "MemFree:           98765 kB\n"
        "MemAvailable:     237568 kB\n",
        encoding="utf-8",
    )
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmRSS:\t      54272 kB\n", encoding="utf-8")

    werte = sd.speicher_werte(str(meminfo), str(status))

    assert werte["total_kb"] == 425984
    assert werte["verfuegbar_kb"] == 237568
    assert werte["rss_kb"] == 54272

    text = sd.formatiere_speicher(werte)
    assert "RSS=53 MB" in text
    assert "verfuegbar=232 MB" in text
    assert "total=416 MB" in text


def test_fehlende_procdateien_liefern_none(tmp_path):
    werte = sd.speicher_werte(str(tmp_path / "fehlt"), str(tmp_path / "fehlt2"))
    assert werte == {"total_kb": None, "verfuegbar_kb": None, "rss_kb": None}
    assert sd.formatiere_speicher(werte) == "RSS=- | verfuegbar=- | total=-"