"""Tests: API-Schluessel-Verwaltung im Updater-Menue (Option 24).

Anlass: `cat /etc/wpssteuerung/api.env` auf dem Pi -> Datei existiert
nicht. api.py liest WPS_API_KEY einmal beim Import, also war
API_KEY leer und jede Schreibroute antwortete mit 503 - auch /control,
also auch der Not-Aus. Die Datei war nur von Hand anlegbar.

Geprueft wird der Quelltext des Shell-Skripts. Wo eine POSIX-Shell
vorhanden ist, laeuft die Funktion zusaetzlich wirklich.
"""

import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
MENU = os.path.join(ROOT, 'Updater', 'wp-manager-menu.sh')


def _quelle():
    with open(MENU, encoding='utf-8') as fh:
        return fh.read()


def _funktion(name):
    """Holt genau eine Funktionsdefinition samt Rumpf."""
    m = re.search(rf'^{name}\(\) \{{$.*?^\}}$', _quelle(), re.MULTILINE | re.DOTALL)
    assert m, f"Funktion {name}() nicht gefunden"
    return m.group(0)


# ---------- Verdrahtung ----------

def test_menueintrag_und_dispatch():
    q = _quelle()
    assert '24) api_key_menu ;;' in q, "Option 24 ist nicht im Dispatch"
    assert re.search(r'printf "24\).*API-Schluessel', q), (
        "Option 24 erscheint nicht in der Menueliste"
    )


def test_alle_funktionen_vorhanden():
    for name in ('api_key_read', 'api_key_probe', 'api_key_status',
                 'api_key_erzeugen', 'api_key_anzeigen',
                 'api_key_entfernen', 'api_key_menu'):
        assert re.search(rf'^{name}\(\) \{{$', _quelle(), re.MULTILINE), (
            f"{name}() fehlt"
        )


# ---------- Die entscheidende Sicherheitseigenschaft ----------

def test_probe_loest_keinen_not_aus_aus():
    """Die Verifikation darf keinen Not-Aus ausloesen.

    Die naheliegende Probe waere POST /control - das ist genau die
    Route, die den Not-Aus ausfuehrt. Sie darf hier nicht vorkommen.
    """
    probe = _funktion('api_key_probe')
    assert '/control' not in probe, (
        "Die Probe geht auf /control - das wuerde beim Test einen "
        "Not-Aus ausloesen"
    )
    assert 'notaus' not in probe.lower(), (
        "Die Probe enthaelt einen Not-Aus-Befehl"
    )
    # Und sie muss eine harmlose Route mit Key-Pflicht nutzen.
    assert '/config/export' in probe, (
        "Ohne Key-Pflicht sagt die Probe nichts ueber den Schluessel"
    )
    assert 'X-API-Key' in probe


def test_probe_unterscheidet_die_drei_fehlerfaelle():
    """401 und 503 bedeuten voellig Verschiedenes und beide sind hier real."""
    probe = _funktion('api_key_probe')
    for code in ('200', '401', '503'):
        assert code in probe, f"HTTP {code} wird nicht ausgewertet"
    assert 'Neustart' in probe, (
        "401 bedeutet praktisch immer 'Neustart fehlt' - das muss gesagt werden"
    )


def test_erzeugen_fragt_beim_ersetzen_nach():
    """Ein Rotieren macht WebApp und Android sofort unbrauchbar."""
    erzeugen = _funktion('api_key_erzeugen')
    assert '.bak' in erzeugen, "Kein Backup des alten Schluessels"
    assert re.search(r'\[j/N\]', erzeugen), (
        "Keine Rueckfrage vor dem Ersetzen eines bestehenden Schluessels"
    )


def test_erzeugen_schreibt_atomar():
    """Ein halb geschriebenes api.env wuerde den Dienst unbrauchbar machen."""
    erzeugen = _funktion('api_key_erzeugen')
    assert 'mktemp' in erzeugen, "Keine temporaere Datei"
    assert re.search(r'mv\s+"\$tmp"', erzeugen), (
        "Die temporaere Datei wird nicht per mv umbenannt - der Schreibvorgang "
        "ist dann nicht atomar"
    )
    # Rechte: die Datei enthaelt ein Geheimnis.
    assert re.search(r'chmod\s+640', erzeugen), "api.env ist zu lesbar"
    assert re.search(r'install -d -m 750', erzeugen), "Verzeichnisrechte fehlen"


def test_neustart_wird_besprochen():
    """api.py liest die Datei nur beim Start - ohne Neustart wirkt nichts."""
    for name in ('api_key_erzeugen', 'api_key_entfernen'):
        f = _funktion(name)
        assert 'restart wpsteuerung' in f, (
            f"{name} startet den Dienst nicht neu - der Schluessel wirkt sonst nicht"
        )
        assert 'verify_service_action' in f, (
            f"{name} startet ohne Verifikation neu"
        )


def test_entfernen_beschreibt_die_folge():
    """503 auf allen Schreibrouten, aber die Heizung laeuft weiter."""
    entfernen = _funktion('api_key_entfernen')
    assert '503' in entfernen
    assert re.search(r'\[j/N\]', entfernen), "Entfernen ohne Rueckfrage"


# ---------- Der Bug in Option 22 ----------

def test_health_liest_die_datei_nicht_die_menue_umgebung():
    """Die alte Fassung las $WPS_API_KEY aus der Shell - nie gesetzt."""
    m = re.search(r'show_control_health\(\) \{$.*?^\}$', _quelle(),
                  re.MULTILINE | re.DOTALL)
    assert m, "show_control_health nicht gefunden"
    assert '${WPS_API_KEY:-}' not in m.group(0), (
        "Die Anzeige liest weiter die Menue-Umgebung statt der Datei und "
        "meldet deshalb immer 'nicht gesetzt'"
    )
    assert 'api_key_read' in m.group(0)


# ---------- Positivtest gegen eine echte Shell ----------

def _shell():
    for name in ('bash', 'sh'):
        pfad = shutil.which(name)
        if pfad:
            return pfad
    return None


# Die beiden Tests unten starten wirklich eine Shell. Unter Windows ist
# das unzuverlaessig: CreateProcess bzw. das Schliessen der Pipes schlagen
# sporadisch mit WinError 50 ("Anforderung nicht unterstuetzt") oder
# WinError 6 ("Handle ungueltig") fehl. Gemessen 14 von 20 Laeufen, und
# beide Codes wechseln - das Bild passt zu eingreifender Sicherheitssoftware,
# nicht zu diesem Skript. Derselbe Effekt plagt test_ram_schutz.py.
#
# Deshalb laeuft die Ausfuehrungspruefung nur ausserhalb von Windows, also
# auf dem Pi, wo das Menue ohnehin laeuft. Die Quelltextpruefungen weiter
# oben sind davon nicht betroffen und laufen ueberall.
_NICHT_WINDOWS = os.name != 'nt'

_eigene_shell = pytest.mark.skipif(
    _shell() is None or not _NICHT_WINDOWS,
    reason="Shell-Ausfuehrung nur ausserhalb von Windows (CreateProcess-Flake)")

_run = subprocess.run


@_eigene_shell
def test_api_key_read_arbeitet(tmp_path):
    """Fuehrt die echte Funktion gegen echte Dateien aus.

    Der Quelltext-Test oben kann nur zeigen, dass die Zeile das Richtige
    meint. Hier laeuft sie wirklich - inklusive 'Datei fehlt' und
    'andere Variablen daneben', die beim Lesen leicht falsch
    implementiert werden.
    """
    quelle = _funktion('api_key_read')
    datei = tmp_path / 'api.env'
    skript = tmp_path / 'lauf.sh'
    skript.write_text(
        '#!/bin/sh\n'
        f'API_ENV_FILE="{datei}"\n'
        + quelle +
        '\nk=$(api_key_read)\n'
        'if [ -n "$k" ]; then echo "WERT=$k"; else echo "WERT="; fi\n',
        encoding='utf-8')

    def lauf(text=None):
        if text is None:
            datei.unlink(missing_ok=True)
        else:
            datei.write_text(text, encoding='utf-8')
        r = _run(_shell(), str(skript))
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    assert lauf() == 'WERT=', "Datei fehlt, meldet aber einen Key"
    assert lauf('WPS_API_KEY=abc123\n') == 'WERT=abc123'
    # Weitere Variablen davor und danach duerfen nicht mitgelesen werden.
    gemischt = ('# Kommentar\nWPS_CORS_ORIGINS=https://x\n'
                'WPS_API_KEY=deadbeef\nWPS_OTHER=1\n')
    assert lauf(gemischt) == 'WERT=deadbeef'
    # Leerer Wert heisst "nicht gesetzt", nicht irgendein Schluessel.
    assert lauf('WPS_API_KEY=\n') == 'WERT='


@_eigene_shell
def test_menue_skript_ist_syntaxfehlerfrei():
    r = _run(_shell(), '-n', MENU)
    assert r.returncode == 0, f"Syntaxfehler im Menue-Skript:\n{r.stderr}"