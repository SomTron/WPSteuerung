"""Modi-Persistenz: Bademodus/Urlaubsmodus ueberleben Dienst-Neustarts.

Befund 07.10.2026: Beide Modi waren RAM-only. Ein Deployment/Neustart
warf den abends aktivierten Bademodus still weg. Jetzt:
- Bademodus: Wiederherstellung nur < 24 h nach Speicherung
- Urlaubsmodus: Wiederherstellung nur solange urlaubsmodus_ende in der
  Zukunft liegt
- Korrupte Datei -> fail-safe Default, kein Crash
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import modi_persistenz as mp  # noqa: E402


class _State:
    def __init__(self):
        self.bademodus_aktiv = False
        self.urlaubsmodus_aktiv = False
        self.urlaubsmodus_start = None
        self.urlaubsmodus_ende = None


def _utc_naiv():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _tmp():
    fd, pfad = tempfile.mkstemp()
    os.close(fd)
    return pfad


def test_speichern_und_laden_rundum():
    """Gespeicherter Bademodus wird nach 'Neustart' wiederhergestellt."""
    pfad = _tmp()
    try:
        s1 = _State()
        s1.bademodus_aktiv = True
        assert mp.speichere_modi(s1, pfad) is True

        s2 = _State()
        assert mp.lade_modi(s2, pfad, _utc_naiv()) is True
        assert s2.bademodus_aktiv is True
        assert s2.urlaubsmodus_aktiv is False
    finally:
        os.unlink(pfad)


def test_urlaubsmodus_mit_zukunft_wird_wiederhergestellt():
    pfad = _tmp()
    try:
        s1 = _State()
        s1.urlaubsmodus_aktiv = True
        now = datetime.now()
        s1.urlaubsmodus_start = now
        s1.urlaubsmodus_ende = now + timedelta(days=7)
        mp.speichere_modi(s1, pfad)

        s2 = _State()
        assert mp.lade_modi(s2, pfad, _utc_naiv()) is True
        assert s2.urlaubsmodus_aktiv is True
        assert s2.urlaubsmodus_ende is not None
    finally:
        os.unlink(pfad)


def test_urlaubsmodus_abgelaufen_wird_verworfen():
    pfad = _tmp()
    try:
        s1 = _State()
        s1.urlaubsmodus_aktiv = True
        now = datetime.now()
        s1.urlaubsmodus_start = now - timedelta(days=15)
        s1.urlaubsmodus_ende = now - timedelta(days=1)
        mp.speichere_modi(s1, pfad)

        s2 = _State()
        # echter 'jetzt' liegt nach dem Ende -> False
        assert mp.lade_modi(s2, pfad) is False
        assert s2.urlaubsmodus_aktiv is False
    finally:
        os.unlink(pfad)


def test_bademodus_24h_veraltet_wird_verworfen():
    pfad = _tmp()
    try:
        s1 = _State()
        s1.bademodus_aktiv = True
        mp.speichere_modi(s1, pfad)

        with open(pfad, "r", encoding="utf-8") as f:
            daten = json.load(f)
        daten["gespeichert_utc"] = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
        ).isoformat(timespec="seconds")
        with open(pfad, "w", encoding="utf-8") as f:
            json.dump(daten, f)

        s2 = _State()
        assert mp.lade_modi(s2, pfad, _utc_naiv()) is False
        assert s2.bademodus_aktiv is False
    finally:
        os.unlink(pfad)


def test_korrupte_datei_ist_fail_safe():
    pfad = _tmp()
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            f.write("{kaputt")
        s = _State()
        assert mp.lade_modi(s, pfad) is False
        assert s.bademodus_aktiv is False
        assert s.urlaubsmodus_aktiv is False
    finally:
        os.unlink(pfad)


def test_fehlende_datei_ist_ok():
    s = _State()
    assert mp.lade_modi(s, _tmp()) is False