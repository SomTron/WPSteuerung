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


# ---------- Option 25: die Unit selbst ----------

UNIT = os.path.join(ROOT, 'Steuerung', 'wpsteuerung.service')


def test_menueintrag_und_dispatch_25():
    q = _quelle()
    assert '25) stuerung_unit_installieren ;;' in q, (
        "Option 25 ist nicht im Dispatch"
    )
    assert re.search(r'printf "25\).*Unit installieren', q), (
        "Option 25 erscheint nicht in der Menueliste"
    )


def test_alle_unit_funktionen_vorhanden():
    for name in ('unit_aktiv_hat', 'stuerung_unit_status',
                 'stuerung_unit_installieren'):
        assert re.search(rf'^{name}\(\) \{{$', _quelle(), re.MULTILINE), (
            f"{name}() fehlt"
        )


def test_install_macht_daemon_reload_und_sichert():
    """Ohne daemon-reload bleibt die alte Unit aktiv."""
    install = _funktion('stuerung_unit_installieren')
    assert 'daemon-reload' in install
    assert re.search(r'install\s+-m\s+0644\s+"\$UNIT_QUELLE"\s+"\$UNIT_ZIEL"', install), (
        "Die Unit wird nicht aus dem Repo an die richtige Stelle kopiert"
    )
    assert '.bak' in install, "Keine Sicherung der bisherigen Unit"
    assert re.search(r'\[j/N\]', install), (
        "Keine Rueckfrage vor dem Ersetzen einer abweichenden Unit"
    )
    assert 'verify_service_action restart wpsteuerung' in install, (
        "Neustart nicht verifiziert"
    )


def test_status_prueft_die_sicherheitsrelevanten_zeilen():
    """Die drei Zeilen, deren Fehlen jeweils ein echtes Problem ist."""
    status = _funktion('stuerung_unit_status')
    assert 'EnvironmentFile' in status, (
        "Status meldet nicht, ob die Unit die api.env liest - genau daran "
        "scheiterte es auf dem Pi"
    )
    assert 'RestartPreventExitStatus=42' in status, (
        "Status meldet nicht, ob der Not-Aus durch systemd aufgehoben wuerde"
    )
    assert 'StartLimitIntervalSec=0' in status, (
        "Status meldet nicht das Startlimit, das am 15.09. zur Luecke fuehrte"
    )


def test_einbaupfad_prueft_die_echte_unit_nicht_nur_die_repo():
    """unit_aktiv_hat liest systemctl cat, nicht die Datei im Repo."""
    q = _quelle()
    m = re.search(r'^unit_aktiv_hat\(\) \{$.*?^\}$', q,
                  re.MULTILINE | re.DOTALL)
    assert m, "unit_aktiv_hat nicht gefunden"
    assert 'systemctl cat' in m.group(0), (
        "Die Pruefung liest nicht die INSTALLIERTE Unit - auf dem Pi ist "
        "die aelter als die im Repo"
    )


# ---------- Die Unit im Repo selbst ----------

def test_repo_unit_traegt_die_noetigen_zeilen():
    """Option 25 installiert diese Datei - sie muss das auch enthalten.

    Andernfalls wuerde die Option eine Unit ausrollen, die den Schluessel
    weiterhin nicht liest und den Not-Aus weiterhin aufhebt.
    """
    with open(UNIT, encoding='utf-8') as fh:
        text = fh.read()
    assert 'EnvironmentFile=-/etc/wpssteuerung/api.env' in text, (
        "Die Unit im Repo laesst WPS_API_KEY weiterhin ungelesen"
    )
    assert 'RestartPreventExitStatus=42' in text, (
        "Die Unit im Repo hebt den Not-Aus per Restart=always wieder auf"
    )
    assert 'StartLimitIntervalSec=0' in text


def test_option24_verweist_auf_option25():
    """Eine gesetzte api.env nuetzt nichts, wenn die Unit sie nicht liest."""
    status = _funktion('api_key_status')
    assert 'unit_aktiv_hat' in status, (
        "Option 24 prueft die Unit nicht - der Nutzer sieht eine gesetzte "
        "Datei und ein 503 und laeuft in die Irre"
    )
    assert '25' in status, "Kein Verweis auf Option 25"


# ---------- Das Deploy zieht die Unit mit ----------

DEPLOY = os.path.join(ROOT, 'Updater', 'rpi-deploy.sh')


def _deploy():
    with open(DEPLOY, encoding='utf-8') as fh:
        return fh.read()


def test_deploy_installiert_die_unit():
    """Ein Deploy zieht den Code, aber nicht die Unit.

    Deshalb lief auf dem Pi eine aeltere Unit: EnvironmentFile fehlte
    (Schluessel nie gelesen, 503) und RestartPreventExitStatus=42 fehlte
    (Not-Aus nach 10 s wieder da). Ohne diese Funktion driftet das bei
    jedem naechsten Deploy erneut.
    """
    d = _deploy()
    assert re.search(r'^sync_service_unit\(\) \{$', d, re.MULTILINE), (
        "sync_service_unit fehlt im Deploy-Skript"
    )
    m = re.search(r'^sync_service_unit\(\) \{$.*?^\}$', d,
                  re.MULTILINE | re.DOTALL)
    assert m
    fn = m.group(0)
    assert re.search(r'install\s+-m\s+0644\s+"\$quelle"\s+"\$ziel"', fn), (
        "Die Unit wird nicht aus dem Repo nach /etc/systemd/system kopiert"
    )
    assert 'daemon-reload' in fn
    assert '.bak' in fn, "Keine Sicherung der bisherigen Unit"
    assert 'cmp -s' in fn, (
        "Ohne Vergleich wird die Unit bei jedem Deploy unnoetig neu geladen"
    )


def test_deploy_ruft_die_synchronisierung_vor_dem_neustart():
    """Andernfalls laeuft der neue Code noch mit der alten Unit."""
    d = _deploy()
    aufrufe = [m.start() for m in re.finditer(r'if ! sync_service_unit; then', d)]
    assert len(aufrufe) >= 2, (
        "Beide Deploy-Pfade (Update und Branch-Wechsel) muessen die Unit "
        f"mitziehen, gefunden: {len(aufrufe)}"
    )
    for pos in aufrufe:
        rest = d[pos:pos + 600]
        assert 'verify_service_restart' in rest, (
            "Der Neustart wird verifiziert, die Unit aber nicht vorher "
            "synchronisiert"
        )
        # Der Aufruf muss VOR verify_service_restart stehen.
        assert rest.index('sync_service_unit') < rest.index('verify_service_restart')


def test_deploy_bricht_nicht_still_ab():
    """Unter `set -e` wuerde ein Fehlschlag still beenden."""
    d = _deploy()
    for m in re.finditer(r'if ! sync_service_unit; then(.*?)fi', d,
                         re.DOTALL):
        block = m.group(1)
        assert 'exit 1' in block, (
            "Fehlgeschlagene Synchronisierung fuehrt nicht zu einem klaren Abbruch"
        )
        assert 'NICHT neu gestartet' in block


@_eigene_shell
def test_deploy_bleibt_syntaxfehlerfrei():
    r = _run(_shell(), '-n', DEPLOY)
    assert r.returncode == 0, f"Syntaxfehler im Deploy-Skript:\n{r.stderr}"


# ---------- Rechte: der Menue-Benutzer muss die Datei lesen koennen ----------

def test_gruppe_statt_rootroot():
    """Das Menue laeuft als Benutzer, nicht als root.

    Mit root:root 640 kann root (und damit systemd) die Datei lesen, der
    Betreiber aber nicht - er kann den Schluessel dann nicht in der App
    eintragen und sieht im Menue "Kein Schluessel gesetzt", obwohl einer
    da ist. Genau das ist auf dem Pi passiert.
    """
    q = _quelle()
    assert re.search(r'API_GRUPPE="\$\{WPS_API_GROUP:-', q), (
        "Die Gruppe des aufrufenden Benutzers wird nicht ermittelt"
    )
    erzeugen = _funktion('api_key_erzeugen')
    assert 'chown "root:$API_GRUPPE"' in erzeugen, (
        "Die Datei gehoert weiterhin root:root - der Benutzer kann sie nicht lesen"
    )
    assert 'install -d -m 750 -o root -g "$API_GRUPPE"' in erzeugen, (
        "Das Verzeichnis ist nicht traversierbar fuer die eigene Gruppe"
    )


def test_status_unterscheidet_fehlend_von_nicht_lesbar():
    """"Kein Schluessel gesetzt" war bei vorhandener Datei schlicht falsch."""
    status = _funktion('api_key_status')
    # Achtung: das Shell-Schluesselwort heisst "elif", nicht "elsif".
    assert re.search(r'elif\s+\[\s*-f\s+"?\$\{?API_ENV_FILE\}?"?\s*\]', status), (
        "Der Fall 'Datei da, aber nicht lesbar' wird nicht behandelt"
    )
    assert 'chown root:%s' in status, (
        "Keine konkrete Abhilfe fuer die Rechte"
    )
    assert 'id -gn' in status

    anzeigen = _funktion('api_key_anzeigen')
    assert 'chown root:%s' in anzeigen, (
        "Option 3 meldet bei vorhandener, unlesbarer Datei 'nicht gesetzt'"
    )


def test_kein_sudo_als_leseweg():
    """Die Statusanzeige darf nicht interaktiv werden.

    Jedes 'sudo' im Lesepfad wuerde eine Passwortabfrage in einer
    Menueschleife ausloesen, die auch von Skripten genutzt wird.

    Gesucht wird nach einem ausgefuehrten sudo-Befehl, nicht nach dem
    Wort: der Hilfetext nennt "sudo chown ..." als Anleitung, und genau
    das soll er ja tun.
    """
    for name in ('api_key_read', 'api_key_status', 'api_key_anzeigen'):
        f = _funktion(name)
        befehle = re.findall(r'^\s*sudo\s', f, re.MULTILINE)
        assert not befehle, (
            f"{name}() fuehrt sudo aus ({len(befehle)}x) - die Anzeige wird "
            "sonst interaktiv"
        )
        assert '$(sudo' not in f, (
            f"{name}() ruft sudo in einer Befehlssubstitution auf"
        )