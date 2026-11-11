"""Vertrag: Repo-Hygiene und reproduzierbare Installation.

Beide Befunde waren "nur" Drift, kein Absturz - aber beide haben das Repo
schleichend unbrauchbar gemacht:

1. Produktivdaten im Git: `logs/` und `Analyse/*.csv` waren mit 65 MB
   getrackt (u. a. ein 17,7-MB-tar-Archiv vom Pi, `merged_data.csv` 31 MB).
   Der `.gitignore` hob die eigene Regel mit `!Analyse/*.csv` sogar wieder
   auf, und bei `logs/` musste jedes neue Dateiformat einzeln nachgetragen
   werden - Dateien wie `logs/941fgu` rutschten durch.

2. Unpinnte Abhaengigkeiten: `pip install -r requirements.txt` konnte auf dem
   Pi still eine andere Major-Version holen (pandas 2 -> 3, matplotlib 3.8 ->
   3.11) und die Steuerung erst nach dem naechsten Neustart brechen.

Deshalb `contract`: die Tests lesen Konfigurationsdateien, nicht Verhalten.
"""
import os
import re

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")
REQUIREMENTS = os.path.join(REPO_ROOT, "requirements.txt")
REQUIREMENTS_PI = os.path.join(REPO_ROOT, "requirements-pi.txt")
REQUIREMENTS_DEV = os.path.join(REPO_ROOT, "requirements-dev.txt")

#: Werkzeuge, die nur auf Entwicklungsrechner und CI gehoeren.
#: "mock" ist bewusst NICHT dabei: das Repo nutzt ausschliesslich
#: `unittest.mock` (Standardbibliothek). Der alte requirements-dev.txt-Eintrag
#: war ein toter Rest - gepinnt wird nur, was wirklich gebraucht wird.
WERKZEUGE = {"pytest", "pytest-asyncio", "ruff", "mypy"}


def _paketnamen(pfad):
    """Die Paketnamen einer Requirements-Datei (ohne Includes/Kommentare)."""
    namen = set()
    for zeile in _paketzeilen(pfad):
        if zeile.startswith("-"):
            continue        # "-r andere-datei.txt" ist ein Include
        name = re.split(r"[\[<>=!~;]", zeile, maxsplit=1)[0].strip()
        if name:
            namen.add(name.lower())
    return namen


def _text(pfad):
    with open(pfad, encoding="utf-8") as fh:
        return fh.read()


def _paketzeilen(pfad):
    """Codezeilen einer Requirements-Datei (ohne Kommentare/Leerzeilen)."""
    zeilen = []
    for roh in _text(pfad).splitlines():
        z = roh.split("#", 1)[0].strip() if not roh.lstrip().startswith("#") else ""
        if not z:
            continue
        zeilen.append(z)
    return zeilen


# --- 1. Repo-Hygiene --------------------------------------------------------

def test_logs_ordner_ist_vollstaendig_ignoriert():
    """"logs/" pauschal, nicht jede Datei einzeln - sonst rutscht was durch."""
    text = _text(GITIGNORE)
    treffer = re.search(r"^logs/\s*$", text, re.MULTILINE)
    assert treffer, (
        "logs/ muss komplett ignoriert werden. Einzelmuster haben versagt: "
        "logs/941fgu (17,7 MB tar) und logs/heizungssteuerung_log_18.10.26.txt "
        "waren getrackt, obwohl 'logs/heizungssteuerung*.log' existierte")


def test_analyse_csv_werden_nicht_wieder_eingeschlossen():
    """Die Gegenregel "!Analyse/*.csv" hat den Ignore-Schutz aufgehoben.

    Geprueft wird nur auf aktive Regeln, nicht auf den erklaerenden
    Kommentar - der nennt die alte Zeile naemlich absichtlich.
    """
    text = _text(GITIGNORE)
    aktiv = [z.strip() for z in text.splitlines() if z.strip() and not z.lstrip().startswith("#")]
    assert "!Analyse/*.csv" not in aktiv, (
        "!Analyse/*.csv hebt das Ignorieren der Analyse-Artefakte wieder auf - "
        "genau diese Zeile war der Grund, warum 65 MB CSV im Repo lagen")
    assert "Analyse/*.csv" in aktiv, (
        "Analyse/*.csv muss ignoriert sein (mehrere zehn MB, Anlagendaten)")


def test_analyse_readme_bleibt_versioniert():
    """Die handgeschriebene Uebersicht ist die einzige Ausnahme."""
    text = _text(GITIGNORE)
    assert "!Analyse/README.md" in text
    assert os.path.isfile(os.path.join(REPO_ROOT, "Analyse", "README.md"))


def test_keine_android_buildartefakte_im_git():
    """Gradle legt .gradle/ und build/ vor dem ersten Build an."""
    text = _text(GITIGNORE)
    assert "android/.gradle/" in text
    assert "android/build/" in text


def test_gitignore_kennt_keine_doppelte_generierte_zeile():
    """Der alte Stand fielen durch doppelte Einträge auf (letzter_lauf.json
    stand zweimal, *.pyc dreimal). Das ist kein Fehler, aber ein Zeichen
    dafür, dass niemand mehr durchsieht."""
    zeilen = [z.strip() for z in _text(GITIGNORE).splitlines()]
    generiert = [z for z in zeilen if z and not z.startswith("#")]
    doppelt = sorted({z for z in generiert if generiert.count(z) > 1})
    assert not doppelt, f"doppelte Ignore-Regeln: {doppelt}"


def test_dokumentation_erwaehnt_die_nicht_versionierten_daten():
    """Ohne Hinweis sucht ein neuer Clone die Beispieldaten vergeblich."""
    readme = _text(os.path.join(REPO_ROOT, "Analyse", "README.md"))
    assert "Woher die Beispieldaten kommen" in readme
    assert "calibrate.py" in readme


# --- 2. Reproduzierbare Installation ---------------------------------------

def test_alle_pakete_sind_exakt_gepinnt():
    """Ohne == holt pip beim naechsten Build eine andere Major-Version."""
    for pfad in (REQUIREMENTS, REQUIREMENTS_PI):
        for zeile in _paketzeilen(pfad):
            # "-r andere-datei.txt" ist ein Include, kein Paket.
            if zeile.startswith("-"):
                continue
            name = re.split(r"[\[<>=!~;]", zeile, maxsplit=1)[0].strip()
            if not name:
                continue
            assert "==" in zeile, (
                f"{os.path.basename(pfad)}: '{zeile}' ist nicht gepinnt - "
                f"ein Build waere damit nicht reproduzierbar")


def test_requirements_datei_ist_pip_faehig():
    """Die Pins muessen sich mit pip parsen lassen (nicht nur aussehen)."""
    from pip._internal.req import req_file

    for pfad in (REQUIREMENTS, REQUIREMENTS_PI):
        with open(pfad, encoding="utf-8") as fh:
            reqs = list(req_file.parse_requirements(pfad, session="verify"))
        assert reqs, f"{os.path.basename(pfad)} ergab keine Pakete"


def test_pins_sind_dokumentiert():
    """Dokumentations-Konsistenz: Kommentar nennt das Pruefdatum."""
    text = _text(REQUIREMENTS)
    assert "PINNING" in text, (
        "ohne Hinweis weiss niemand, wogegen die Pins geprueft wurden")
    assert "11.11.2026" in text


# --- 3. Trennung Laufzeit / Werkzeuge ---------------------------------------

def test_werkzeuge_sind_nicht_mehr_in_der_laufzeit():
    """pytest/ruff/mypy gehoeren nicht auf den Pi.

    Vorher standen sie in requirements.txt, und requirements-pi.txt zog sie
    ueber "-r requirements.txt" mit - Grundlast auf einem 512-MB-Geraet, ohne
    im Betrieb je verwendet zu werden.
    """
    laufzeit = _paketnamen(REQUIREMENTS)
    verblieben = WERKZEUGE & laufzeit
    assert not verblieben, (
        f"requirements.txt enthaelt Werkzeuge: {sorted(verblieben)} - sie "
        f"gehoeren nach requirements-dev.txt")


def test_werkzeuge_sind_in_dev_gepinnt():
    """Der Pi bekommt sie nicht - die Entwicklung braucht sie aber-versionstreu."""
    namen = _paketnamen(REQUIREMENTS_DEV)
    fehlt = WERKZEUGE - namen
    assert not fehlt, (
        f"requirements-dev.txt vermisst: {sorted(fehlt)}")
    for zeile in _paketzeilen(REQUIREMENTS_DEV):
        if zeile.startswith("-"):
            continue
        name = re.split(r"[\[<>=!~;]", zeile, maxsplit=1)[0].strip().lower()
        if name in WERKZEUGE:
            assert "==" in zeile, (
                f"requirements-dev.txt: '{zeile}' ist nicht gepinnt")


def test_pi_lauft_mit_laufzeit_und_hardware():
    """Der Pi installiert nur Laufzeit + Hardware - ohne Werkzeuge."""
    namen = _paketnamen(REQUIREMENTS_PI)
    assert not (WERKZEUGE & namen), (
        f"requirements-pi.txt zieht Werkzeuge auf den Pi: "
        f"{sorted(WERKZEUGE & namen)}")
    # Es muss aber die Laufzeit enthalten (ueber das Include).
    text = _text(REQUIREMENTS_PI)
    assert "-r requirements.txt" in text, (
        "requirements-pi.txt bezieht die Laufzeit nicht mehr ein")


def test_ci_installiert_dev_und_nicht_nur_laufzeit():
    """Die CI braucht pytest - sonst scheitert der Testschritt still."""
    ci = _text(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))
    assert "requirements-dev.txt" in ci, (
        "die CI installiert requirements.txt - ohne die Werkzeuge ist der "
        "Testschritt nicht mehr lauffaehig")
    assert not re.search(r"pip install -r requirements\.txt\s*$", ci, re.MULTILINE), (
        "die CI installiert ausschliesslich die Laufzeit")