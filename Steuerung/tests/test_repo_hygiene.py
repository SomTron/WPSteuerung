"""Repository-Hygiene: keine kaputten oder halb-gemergten Quelldateien.

Hintergrund (Aufraeum-Runde D): `Steuerung/archive/legacy_main.py` lag als
UTF-16-Datei mit Mojibake-Umlauten und 13 unaufgeloesten Merge-Konfliktmarkern
im Repo. Weder ruff noch compileall pruefen `archive/`, deshalb fiel das nie
auf. Dieser Test prueft ALLE Python-Dateien des Repos.

Hintergrund (WebApp 03./04.10.2026): Das Inline-Skript der WebApp war seit dem
Commit 2478da6 syntaktisch kaputt ("Unexpected token '<'"). Elf weitere Commits
wurden darauf gebaut - zwei davon waren Versuche, die Anzeige zu repararieren.
Der Dienst lieferte die Seite mit HTTP 200 aus, die WebApp meldete
"Verbunden" und blieb dann leer. Weder ruff noch pytest betrachten JavaScript,
also fiel der Fehler durch das gesamte System.

Dieser Test prueft daher das Inline-Skript der WebApp - mit Node, wenn
vorhanden, und sonst ueber eine eigene Klammer-/String-Bilanz.
"""
import os
import re
import shutil
import subprocess
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Verzeichnisse ohne eigenen Quellcode (Venvs, Caches, Fremd-Backups)
AUSGESCHLOSSEN = {
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".idea",
    "node_modules", ".ci_venv", "_fremde_aenderungen_backup",
}

KONFLIKT_MARKER = ("<<<<<<<", "=======", ">>>>>>>")

WEBAPP_HTML = os.path.join(REPO_ROOT, "webapp", "index.html")

# Exakt die Browser-Logik: Inline-Skripte (ohne src=) von externen trennen.
RE_INLINE_SCRIPT = re.compile(
    r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


def _python_dateien():
    for wurzel, dirs, dateien in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in AUSGESCHLOSSEN]
        for name in dateien:
            if name.endswith(".py"):
                yield os.path.join(wurzel, name)


def test_alle_python_dateien_sind_utf8():
    kaputt = []
    for pfad in _python_dateien():
        try:
            open(pfad, "rb").read().decode("utf-8")
        except (UnicodeDecodeError, OSError) as e:
            kaputt.append(f"{os.path.relpath(pfad, REPO_ROOT)}: {e}")
    assert not kaputt, "Nicht als UTF-8 lesbar:\n" + "\n".join(kaputt)


def test_keine_merge_konfliktmarker():
    treffer = []
    for pfad in _python_dateien():
        with open(pfad, encoding="utf-8") as f:
            for nr, zeile in enumerate(f, start=1):
                if zeile.startswith(KONFLIKT_MARKER):
                    treffer.append(f"{os.path.relpath(pfad, REPO_ROOT)}:{nr}: {zeile.strip()}")
    assert not treffer, "Unaufgeloeste Merge-Konflikte:\n" + "\n".join(treffer)


def test_keine_leeren_python_dateien():
    """Regression: `constants_clean.py` lag als 0-Byte-Datei im Repo.

    Eine leere Datei wird von ruff und compileall stillschweigend
    akzeptiert, sieht aber wie ein Modul aus und verwirrt beim Import.
    """
    leer = []
    for pfad in _python_dateien():
        if os.path.getsize(pfad) == 0:
            leer.append(os.path.relpath(pfad, REPO_ROOT))
    assert not leer, "Leere Python-Dateien (nicht verwaist):\n" + "\n".join(leer)


def test_keine_python_dateien_mit_bom():
    """Regression: `test_legionellen_plan.py` trug eine unsichtbare BOM.

    Ein BOM am Dateianfang fuehrt beim Parsen zu
    `SyntaxError: invalid non-printable character U+FEFF`.
    """
    mit_bom = []
    for pfad in _python_dateien():
        with open(pfad, "rb") as f:
            if f.read(3) == b"\xef\xbb\xbf":
                mit_bom.append(os.path.relpath(pfad, REPO_ROOT))
    assert not mit_bom, "Dateien mit UTF-8-BOM:\n" + "\n".join(mit_bom)


# --- WebApp: Inline-JavaScript muss syntaktisch gueltig sein --------------
def _webapp_inline_scripts():
    """Liefert den Inhalt aller Inline-Skripte der WebApp."""
    with open(WEBAPP_HTML, encoding="utf-8") as f:
        html = f.read()
    return [s for s in RE_INLINE_SCRIPT.findall(html) if s.strip()]


def _node_pruefung(js):
    """Prueft `js` mit `node --check`.

    Gibt `(node_verfuegbar, bestanden, fehlermeldung)` zurueck. Die
    drei Werte sind bewusst getrennt: ein frueherer Entwurf gab
    `(node_verfuegbar, meldung)` zurueck, wodurch "Fehler" und "node
    fehlt" nicht unterscheidbar waren und der Test still bestanden.
    """
    node = shutil.which("node")
    if not node:
        return False, None, ""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(js)
        pfad = tmp.name
    try:
        r = subprocess.run(
            [node, "--check", pfad], capture_output=True, text=True, timeout=60
        )
        if r.returncode == 0:
            return True, True, ""
        # Nur die relevanten Zeilen: Position und Meldung
        zeilen = [
            z for z in r.stderr.splitlines()
            if ".js:" in z or "SyntaxError" in z or "Error:" in z
        ]
        return True, False, "\n".join(zeilen[:6])
    finally:
        os.unlink(pfad)


def _klammerbilanz(js):
    """Ersatzpruefung ohne Node: Klammerbilanz je Block.

    Zaehlt {} () [] und ueberspringt Zeichenketten sowie Kommentare. Das
    erkennt die haeufigsten Bruchstellen (fehlendes }, ungeschlossenes
    Template-Literal, '<' als Ergebnis eines unvollstaendigen Ausdrucks).
    """
    tiefe = 0
    zeilen = js.split("\n")
    # Regex fuer '...', "..." und `...` - greedy ist hier in Ordnung,
    # weil ein Ueberhang ueber Zeilengrenzen ohnehin auffaellt.
    muster = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`")
    for nr, zeile in enumerate(zeilen, start=1):
        ohne = muster.sub('""', zeile)
        ohne = re.sub(r"//.*$", "", ohne)          # Zeilenkommentar
        ohne = re.sub(r"/\*.*?\*/", "", ohne)      # Blockkommentar
        tiefe += ohne.count("{") + ohne.count("(") + ohne.count("[")
        tiefe -= ohne.count("}") + ohne.count(")") + ohne.count("]")
        if tiefe < 0:
            return False, f"Zeile {nr}: zu viele schliessende Klammern"
    if tiefe != 0:
        return False, f"Klammerbilanz am Ende: {tiefe} (0 erwartet)"
    return True, ""


def test_webapp_inline_script_ist_syntaktisch_gueltig():
    """Regression: Die WebApp war seit 2478da6 syntaktisch kaputt.

    Ein SyntaxError im Inline-Skript beendet die Ausfuehrung des GANZEN
    Skripts - nicht nur einer Funktion. Die Seite wurde trotzdem mit
    HTTP 200 ausgeliefert und die WebApp zeigte "Verbunden", ohne je
    Werte anzuzeigen. Elf Commits wurden darauf gebaut.

    Node ist die verlaessliche Pruefung. Ohne Node greift die
    Klammerbilanz, die die gaeufigsten Bruchstellen erkennt.
    """
    skripte = _webapp_inline_scripts()
    assert skripte, "Kein Inline-Skript in webapp/index.html gefunden"

    node_da, _, _ = _node_pruefung("")
    assert node_da, (
        "node nicht gefunden. Die Klammerbilanz erkennt nur die "
        "haeufigsten Bruchstellen, nicht die volle Syntax - bitte Node "
        "installieren oder den Test lokal laufen lassen."
    )

    fehler = []
    for i, js in enumerate(skripte, start=1):
        _, bestanden, meldung = _node_pruefung(js)
        if not bestanden:
            zeilen_meldung = "\n".join(
                "      " + z for z in meldung.splitlines()
            )
            fehler.append(f"Inline-Skript {i}:\n{zeilen_meldung}")
    assert not fehler, (
        "JavaScript der WebApp ist syntaktisch kaputt. Ein SyntaxError "
        "bricht das GANZE Skript ab: Die Seite wird trotzdem mit HTTP 200 "
        "ausgeliefert und die WebApp zeigt 'Verbunden', ohne je Werte "
        "anzuzeigen.\n\n" + "\n\n".join(fehler)
    )


def test_webapp_klammerbilanz_ist_ausgeglichen():
    """Zweite, node-unabhaengige Absicherung.

    Laeuft auch dort, wo kein Node installiert ist - und erkennt den
    Fehler, der elf Commits lang unbemerkt blieb.
    """
    for i, js in enumerate(_webapp_inline_scripts(), start=1):
        ok, grund = _klammerbilanz(js)
        assert ok, f"Inline-Skript {i} unausgeglichen: {grund}"


def test_webapp_kein_abgebrochener_script_block():
    """Regression: Ein `</script>` in einem String beendet den Block frueh.

    Der HTML-Parser kennt keine JavaScript-Strings - er trennt am
    naechsten `</script>`. Alles danach wird als HTML interpretiert und
    der Rest des Skripts faellt als "SyntaxError: Unexpected token '<'"
    auf. Beide Anzahlen muessen zusammenpassen.
    """
    with open(WEBAPP_HTML, encoding="utf-8") as f:
        html = f.read()
    # Jede Oeffnung braucht ein Ende - externe (src=) genauso wie Inline.
    extern = len(re.findall(r"(?i)<script[^>]*\bsrc\s*=[^>]*>", html))
    inline = len(RE_INLINE_SCRIPT.findall(html))
    schliessend = len(re.findall(r"(?i)</script\s*>", html))
    assert extern + inline == schliessend, (
        f"{extern} externe + {inline} Inline-<script> gegen {schliessend} "
        "</script>. Vermutlich steht ein </script> in einem String."
    )


def test_webapp_keine_mojibake_zeichen():
    """U+FFFD (Replacement Character) bedeutet: eine Datei wurde mit der
    falschen Kodierung gelesen und wieder geschrieben.

    Im Log vom 03.10.2026 erschienen zerschaenderte Umlaute in der
    Not-Aus-Meldung. Mojibake in einer Quelltextdatei ist ein
    Spielfeld fuer Folgefehler.

    Das Zeichen wird bewusst per chr(0xFFFD) gebildet: steht es
    woertlich im Quelltext, verschwindet es beim Speichern in manchen
    Editoren und Pipelines - und der Test prueft dann nichts mehr.
    """
    with open(WEBAPP_HTML, encoding="utf-8") as f:
        inhalt = f.read()
    anzahl = inhalt.count(chr(0xFFFD))
    assert anzahl == 0, (
        f"webapp/index.html enthaelt {anzahl}x U+FFFD (Mojibake). Die Datei "
        "wurde vermutlich mit der falschen Kodierung geschrieben."
    )
