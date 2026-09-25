"""Tests fuer die interaktive Zeilenanzahl-Abfrage des WP-Managers.

Anforderung: Die Log-Optionen 2 und 3 sollen standardmaessig 200 Zeilen
zeigen, aber vorher fragen, ob eine andere Anzahl gewuenscht ist.

Der Test prueft beides:

- den statischen Vertrag im Menue (Optionen fragen, keine festen 200 mehr)
- die Shell-Funktion `frage_zeilen` selbst, mit echter POSIX-Shell
  (Enter = Standard, Zahl, Müll, 0, EOF, Sicherheitsdeckel)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

UPDATER_DIR = Path(__file__).resolve().parents[2] / "Updater"
MENU = UPDATER_DIR / "wp-manager-menu.sh"

POSIX_SH = shutil.which("sh")


def _menu_text() -> str:
    return MENU.read_text(encoding="utf-8")


def _extrahiere_funktion(name: str) -> str:
    """Schneidet eine Shell-Funktion inkl. Rumpf aus dem Menue heraus."""
    zeilen = _menu_text().splitlines()
    start = None
    for i, z in enumerate(zeilen):
        if z.strip() == f"{name}() {{":
            start = i
            break
    assert start is not None, f"Funktion {name}() nicht gefunden"
    for j in range(start + 1, len(zeilen)):
        if zeilen[j] == "}":
            return "\n".join(zeilen[start : j + 1])
    raise AssertionError(f"Funktion {name}() hat keine schliessende Klammer")


# ------------------------------------------------------------------- Vertrag


def test_logoptionen_zeigen_standard_200_und_fragen_nach_anzahl():
    text = _menu_text()
    assert 'printf "2) ' in text
    assert 'printf "3) ' in text
    assert "Standard 200" in text
    # Die alten, fest verdrahteten Beschreibungen sind entfernt.
    assert "Last 200 log lines" not in text
    assert "Error Log (Last 200 lines)" not in text


def test_option_2_und_3_fragten_eigenstaendig_nach_anzahl():
    text = _menu_text()
    assert 'frage_zeilen "${CYAN}Wie viele Logzeilen anzeigen? (Enter = 200):${NC} " 200' in text
    assert (
        'frage_zeilen "${CYAN}Wie viele Error-Log-Zeilen anzeigen? (Enter = 200):${NC} " 200'
        in text
    )
    # Beide Optionen nutzen die variable Anzahl, nicht mehr ein festes 200.
    assert 'tail -n "$ZEILEN_ANTWORT" "$LOG_FILE"' in text
    assert 'tail -n "$ZEILEN_ANTWORT" "$ERROR_LOG_FILE"' in text
    assert 'tail -n 200 "$LOG_FILE"' not in text
    assert 'tail -n 200 "$ERROR_LOG_FILE"' not in text


def test_alle_zeilenabfragen_nutzen_dieselbe_hilfsfunktion():
    """Kein zweites, eigenstaendiges Zahlen-Parsing mehr im Menue."""
    text = _menu_text()
    assert text.count("frage_zeilen ") >= 5
    assert text.count("frage_zahl ") >= 1
    # Die alte, dreifach duplizierte Mustervalidierung ist ersetzt.
    assert "read log_lines" not in text
    assert "read zyk_n" not in text
    assert "read log_hours" not in text
    assert "ist keine Zahl – verwende Standardwert" not in text


def test_abfrage_wirft_das_ergebnis_nicht_per_stdout():
    """Eine Command-Substitution wuerde den Prompt mitverschlucken."""
    text = _menu_text()
    assert "ZEILEN_ANTWORT=" in text
    assert "$(frage_zeilen" not in text
    assert "$(frage_zahl" not in text


def test_abfrage_hat_sicherheitsdeckel_und_eingabevalidierung():
    text = _menu_text()
    assert "WPS_MAX_LOG_LINES" in text
    assert "muss groesser als %s sein" in text
    assert "wird auf %s begrenzt" in text


def test_stundenabfrage_in_option_12_ist_mit_validiert():
    """Regression: Option 12 hatte das alte Zahlen-Parsing behalten."""
    text = _menu_text()
    assert (
        'frage_zahl "${CYAN}Letzte wieviele Stunden anzeigen?'
        in text
    )
    assert 'log_hours="$ZAHL_ANTWORT"' in text


def test_kopfzeilen_der_logoptionen_ueberleben_leere_farbvariablen():
    """Regression: dash interpretiert ein fuehrendes '-' als printf-Option.

    Ohne TTY sind die Farbvariablen leer, dann beginnt der Formatstring mit
    '---'. Ohne das protective '--' verschwindet die Kopfzeile komplett.
    """
    text = _menu_text()
    assert (
        'printf -- "${CYAN}--- Letzte %s Zeilen: %s ---${NC}\\n"' in text
    ), "Kopfzeile von Option 2 braucht printf --"
    assert (
        'printf -- "${CYAN}--- Letzte %s Fehlerzeilen: %s ---${NC}\\n"' in text
    ), "Kopfzeile von Option 3 braucht printf --"


# --------------------------------------------------------------- Funktionell


def _fuehre_aus(eingabe: str, *, max_lines: str | None = None) -> str:
    """Ruft frage_zeilen in einer echten Shell auf und gibt ZEILEN_ANTWORT zurueck."""
    assert POSIX_SH is not None
    script = "\n".join(
        (
            _extrahiere_funktion("is_number"),
            "RED=''; GREEN=''; YELLOW=''; NC=''",
            _extrahiere_funktion("frage_zahl"),
            _extrahiere_funktion("frage_zeilen"),
            'frage_zeilen "Anzahl: " 200',
            "printf 'ERGEBNIS=%s\\n' \"$ZEILEN_ANTWORT\"",
        )
    )
    env = None
    if max_lines is not None:
        env = dict(os.environ)
        env["WPS_MAX_LOG_LINES"] = max_lines
    result = subprocess.run(
        [POSIX_SH, "-c", script],
        input=eingabe,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(autouse=True)
def ohne_wartezeit():
    """Die Meldung "keine Zahl" nicht 1 s stehen lassen.

    Die Ausgabe bleibt identisch - nur die Wartezeit entfaellt, damit die
    Shell-Tests nicht mehrere Sekunden kosten.
    """
    os.environ["WPS_MANAGER_NO_SLEEP"] = "1"
    yield
    os.environ.pop("WPS_MANAGER_NO_SLEEP", None)


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_leere_eingabe_uebernimmt_den_standardwert_200():
    assert "ERGEBNIS=200" in _fuehre_aus("\n")


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_enter_allein_uebernimmt_den_standardwert_200():
    assert "ERGEBNIS=200" in _fuehre_aus("")


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_gueltige_zahl_wird_uebernommen():
    assert "ERGEBNIS=50" in _fuehre_aus("50\n")
    assert "ERGEBNIS=1" in _fuehre_aus("1\n")


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_muell_faellt_kontrolliert_auf_den_standardwert_zurueck():
    ausgabe = _fuehre_aus("abc\n")
    assert "ERGEBNIS=200" in ausgabe
    assert "keine Zahl" in ausgabe


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_eof_liefert_keine_leere_ausgabe():
    # read liefert bei EOF false; die Funktion muss trotzdem den Standard setzen.
    assert "ERGEBNIS=200" in _fuehre_aus("")


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_null_wird_abgewiesen():
    ausgabe = _fuehre_aus("0\n")
    assert "ERGEBNIS=200" in ausgabe
    assert "groesser als 0" in ausgabe


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_sehr_grosse_anzahl_wird_begrenzt():
    ausgabe = _fuehre_aus("99999999\n", max_lines="1000")
    assert "ERGEBNIS=1000" in ausgabe
    assert "begrenzt" in ausgabe


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_unsinniger_deckel_stoert_nicht():
    ausgabe = _fuehre_aus("300\n", max_lines="abc")
    assert "ERGEBNIS=300" in ausgabe


def _fuehre_zahl_aus(eingabe: str) -> str:
    """Ruft frage_zahl direkt auf - so wie Option 12 es fuer Stunden tut."""
    assert POSIX_SH is not None
    script = "\n".join(
        (
            _extrahiere_funktion("is_number"),
            "RED=''; GREEN=''; YELLOW=''; NC=''",
            _extrahiere_funktion("frage_zahl"),
            # Entspricht dem Aufruf in Option 12 (Stunden, Minimum 1).
            'frage_zahl "Stunden: " 2 1 "" "Stundenzahl"',
            "printf 'ERGEBNIS=%s\\n' \"$ZAHL_ANTWORT\"",
        )
    )
    result = subprocess.run(
        [POSIX_SH, "-c", script],
        input=eingabe,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_stundenabfrage_uebernimmt_enter_als_standard():
    assert "ERGEBNIS=2" in _fuehre_zahl_aus("\n")


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_stundenabfrage_lehnt_null_und_muell_ab():
    for eingabe in ("0\n", "abc\n"):
        assert "ERGEBNIS=2" in _fuehre_zahl_aus(eingabe), eingabe


@pytest.mark.skipif(POSIX_SH is None, reason="keine POSIX-Shell 'sh' verfuegbar")
def test_stundenabfrage_nennt_die_richtige_bezeichnung():
    ausgabe = _fuehre_zahl_aus("0\n")
    assert "Stundenzahl muss groesser als 0 sein" in ausgabe
    assert "Anzahl muss" not in ausgabe

