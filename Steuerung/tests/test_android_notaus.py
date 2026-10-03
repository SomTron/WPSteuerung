"""Tests: Not-Aus in der Android-App.

Wichtig: Die Android-App kann auf diesem Rechner NICHT gebaut werden
(kein JDK). Statt eines Kotlin-Builds pruefen diese Tests deshalb den
Quelltext. Das ist kein Ersatz, deckt aber genau die Fehlerklasse ab,
die beim Lesen von Kotlin leicht durchrutscht: verdrahteter Code, der
an einer Stelle fehlt oder auf einen nicht vorhandenen String zeigt.

Geprueft wird:
- jeder `R.string.x`-Verweis existiert in strings.xml (baut sonst nicht),
- die Kette Repository -> ViewModel -> Screen ist vollstaendig,
- der Not-Aus ist an eine Rueckfrage gebunden,
- nach dem Ausloesen pollt die App nicht weiter (Dienst ist dann tot),
- die Not-Aus-Felder im Statusmodell sind nullable (Gson-Falle).
"""

import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
ANDROID = os.path.join(ROOT, 'android', 'app', 'src', 'main')
KT = os.path.join(ANDROID, 'java', 'com', 'wpsteuerung', 'app')


def _lese(pfad):
    with open(pfad, encoding='utf-8') as fh:
        return fh.read()


def _screen():
    return _lese(os.path.join(KT, 'ui', 'screens', 'DashboardScreen.kt'))


def _viewmodel():
    return _lese(os.path.join(KT, 'viewmodel', 'DashboardViewModel.kt'))


def _repository():
    return _lese(os.path.join(KT, 'data', 'repository', 'WPRepository.kt'))


def _model():
    return _lese(os.path.join(KT, 'data', 'model', 'SystemStatus.kt'))


def test_alle_string_verweise_existieren():
    """Ein `R.string.x` ohne Eintrag in strings.xml bricht den Build ab."""
    xml = _lese(os.path.join(ANDROID, 'res', 'values', 'strings.xml'))
    vorhanden = set(re.findall(r'<string name="([^"]+)"', xml))

    fehlend = []
    for wurzel, _dirs, dateien in os.walk(KT):
        for datei in dateien:
            if not datei.endswith('.kt'):
                continue
            quelle = _lese(os.path.join(wurzel, datei))
            for name in re.findall(r'R\.string\.(\w+)', quelle):
                if name not in vorhanden:
                    fehlend.append(f"{datei}: {name}")

    assert not fehlend, "R.string-Verweise ohne Eintrag in strings.xml: " + ", ".join(fehlend)


def test_notaus_ist_vom_bildschirm_aus_erreichbar():
    """Die Repository-Methode allein genuegt nicht - jemand muss sie tippen."""
    screen = _screen()
    assert 'viewModel.bestaetigungOeffnen()' in screen, (
        "Not-Aus-Knopf ruft die Sicherheitsabfrage nicht auf"
    )
    assert 'viewModel.clearNotAus()' in screen, (
        "Kein Knopf zum Aufheben einer Sperre"
    )
    assert 'status.notausAktiv' in screen, (
        "Der Bildschirm wertet den Sperrzustand nicht aus"
    )


def test_sicherheitsabfrage_ist_verknuepft():
    """Der Not-Aus darf nicht direkt auf den Befehl gehen."""
    viewmodel = _viewmodel()
    assert 'fun triggerNotAus()' in viewmodel
    assert 'fun bestaetigungOeffnen()' in viewmodel
    assert 'fun bestaetigungSchliessen()' in viewmodel

    screen = _screen()
    bestaetigung = screen[screen.find('AlertDialog('):]
    bestaetigung = bestaetigung[:bestaetigung.find('Column(')]
    assert 'viewModel.triggerNotAus()' in bestaetigung, (
        "Die Rueckfrage loest den Not-Aus nicht aus"
    )


def test_viewmodel_reicht_an_das_repository_weiter():
    viewmodel = _viewmodel()
    assert 'repository.triggerNotAus()' in viewmodel, (
        "ViewModel umgeht das Repository - der Android-Befehl landet nirgends"
    )
    assert 'repository.clearNotAus()' in viewmodel


def test_pollen_steht_nach_notaus_still():
    """Der Dienst laeuft nach dem Not-Aus nicht mehr.

    Ohne diese Bremse springt die App auf die Fehlerseite und der
    Not-Aus wirkt, als haette er nichts getan.
    """
    viewmodel = _viewmodel()
    assert '_dienstBeendet.value = true' in viewmodel, (
        "Nach dem Not-Aus wird die Abfrage nicht angehalten"
    )
    assert 'if (_dienstBeendet.value) break' in viewmodel, (
        "Die Abfrage-Schleife ignoriert den Stopp"
    )


def test_statusmodell_kan_notaus_felder_verwerfen():
    """`/status` liefert die Felder flach - sonst sieht Gson nichts."""
    api = _lese(os.path.join(ROOT, 'Steuerung', 'api.py'))
    for feld in ('notaus_aktiv', 'notaus_grund', 'notaus_ts'):
        assert f'"{feld}"' in api, f"api.py liefert {feld} nicht"

    modell = _model()
    for feld in ('notausAktiv', 'notausGrund', 'notausTs'):
        assert f'val {feld}' in modell, f"SystemStatus kennt {feld} nicht"


def test_statusstrings_sind_nullable():
    """Gson erzeugt SystemStatus per Unsafe und setzt nur vorhandene Felder.

    Ein Kotlin-Standardwert wie `notausTs: String = ""` gilt dabei NICHT -
    das Feld bliebe null und waere als nicht-null String deklariert.
    Ein aelterer Dienst ohne diese Felder wuerde die App zum Absturz
    bringen. Deshalb nullable.
    """
    modell = _model()
    for feld in ('notausGrund', 'notausTs'):
        treffer = re.search(rf'val {feld}\s*:\s*(\S+?)\s*(?:=|$)', modell, re.MULTILINE)
        assert treffer, f"{feld} nicht gefunden"
        assert treffer.group(1).endswith('?'), (
            f"{feld} ist {treffer.group(1)} statt String? - bei einer aelteren "
            "Dienstversion ohne das Feld waere das ein Absturz"
        )


def test_bademodus_fehlerpfad_ist_bedient():
    """Der alte Zweig trug nur den Kommentar 'Could show error toast'."""
    viewmodel = _viewmodel()
    assert 'Could show error toast' not in viewmodel, (
        "Fehlerpfad des Bademodus ist weiterhin nicht bedient"
    )


# ---------- API-Schluessel ----------

def test_api_schluessel_wird_mitgeschickt():
    """Ohne X-API-Key ist die ganze App nur lesend.

    Die Leserouten sind offen, aber /control verlangt den Schluessel
    (api.py::_check_api_key) und antwortet sonst mit 401. Betroffen
    waeren Bademodus und Not-Aus gleichermassen.
    """
    client = _lese(os.path.join(KT, 'data', 'api', 'RetrofitClient.kt'))
    assert 'header("X-API-Key"' in client, (
        "Retrofit schickt keinen API-Schluessel - jeder Schreibbefehl endet mit 401"
    )
    assert 'Einstellungen.apiKey' in client, (
        "Der Schluessel wird nicht aus den Einstellungen gelesen"
    )
    assert '.addInterceptor(apiKeyInterceptor)' in client, (
        "Der Interceptor ist gebaut, aber nicht eingehängt"
    )


def test_schluessel_bleibt_aus_dem_log():
    """HEADERS allein genuegt nicht - dort stuende der Schluessel im Klartext."""
    client = _lese(os.path.join(KT, 'data', 'api', 'RetrofitClient.kt'))
    assert 'Level.BODY' not in client, (
        "BODY protokolliert Anfrage und Antwort vollstaendig"
    )
    assert 'redactHeader("X-API-Key")' in client, (
        "Der Schluessel landet im Logcat. Jeder mit adb liest ihn mit."
    )


def test_einstellungen_werden_vor_dem_ersten_aufruf_initialisiert():
    main = _lese(os.path.join(KT, 'MainActivity.kt'))
    assert 'Einstellungen.init(applicationContext)' in main, (
        "Ohne init() liefert getSharedPreferences null und der Schluessel bleibt leer"
    )
    # 'setContent {' statt 'setContent' - sonst trifft der Import von
    # androidx.activity.compose.setContent und der Vergleich rutscht
    # scheinbar in die richtige Reihenfolge.
    assert main.find('Einstellungen.init') < main.find('setContent {'), (
        "Initialisierung muss vor dem ersten API-Aufruf stehen"
    )


def test_schluesselfeld_ist_erreichbar():
    screen = _screen()
    assert 'viewModel.apiKeySpeichern()' in screen, (
        "Der Schluessel laesst sich in der App nicht hinterlegen"
    )
    assert 'PasswordVisualTransformation' in screen, (
        "Der Schluessel wird im Klartext angezeigt"
    )


# ---------- Formales ----------

def _ohne_kommentare(quelle):
    """Entfernt Kommentare und Literale - in RICHTIGER Reihenfolge.

    Zuerst die Strings, sonst frisst die `//`-Regel bei "http://..."
    den Rest der Zeile und der dann unterminierte String verschluckt
    Zeilen mit geschweiften Klammern. Genau das hat einmal einen
    falschen Befund produziert.
    """
    quelle = re.sub(r'/\*.*?\*/', '', quelle, flags=re.S)
    quelle = re.sub(r'"(\\.|[^"\\])*"', '""', quelle)
    quelle = re.sub(r"'(\\.|[^'\\])*'", "''", quelle)
    return re.sub(r'//[^\n]*', '', quelle)


def test_klammern_ausgeglichen():
    """Fängt beschädigte Dateien beim Ersetzen von Textblöcken."""
    kaputt = []
    for wurzel, _dirs, dateien in os.walk(KT):
        for datei in dateien:
            if datei.endswith('.kt'):
                s = _ohne_kommentare(_lese(os.path.join(wurzel, datei)))
                if s.count('{') != s.count('}'):
                    kaputt.append(datei)
    assert not kaputt, "Klammern unausgeglichen: " + ", ".join(kaputt)


def test_key_quelle_stimmt_mit_api_ueberein():
    """Die Beschriftung muss dorthin zeigen, wo der Schluessel wirklich ist.

    api.py liest WPS_API_KEY aus der Umgebung; die Unit laedt sie aus
    /etc/wpssteuerung/api.env. Ein Verweis auf die config.ini fuehrt
    den Nutzer bei der Suche in die falsche Datei - die gibt es hier
    nicht als Quelle des Schluessels.
    """
    api = _lese(os.path.join(ROOT, 'Steuerung', 'api.py'))
    assert 'os.environ.get("WPS_API_KEY")' in api, (
        "api.py laesst den Schluessel woanders her - die Beschreibung "
        "in der App muesste angepasst werden"
    )

    unit = _lese(os.path.join(ROOT, 'Steuerung', 'wpsteuerung.service'))
    assert '/etc/wpssteuerung/api.env' in unit, (
        "Die Unit laedt keine api.env - woher kommt WPS_API_KEY?"
    )

    xml = _lese(os.path.join(ANDROID, 'res', 'values', 'strings.xml'))
    warnung = re.search(
        r'<string name="verbindung_warnung">(.*?)</string>', xml, re.S).group(1)
    assert '/etc/wpssteuerung/api.env' in warnung, (
        "Die Beschriftung nennt nicht die echte Quelle des Schluessels"
    )
    assert 'WPS_API_KEY' in warnung, (
        "Die Beschriftung nennt die Variablennummer nicht"
    )


def test_manifest_sichert_das_geheimnis():
    """Seit die App den API-Schluessel ablegt, darf er nicht mitgesichert werden.

    allowBackup="true" packt SharedPreferences in Cloud-/adb-Backups -
    damit laege der Schluessel kopierbar ausserhalb des Geraets.
    """
    manifest = _lese(os.path.join(ANDROID, 'AndroidManifest.xml'))
    assert 'android:allowBackup="false"' in manifest, (
        "Backup aktiv - der API-Schluessel waere auslesbar kopierbar"
    )
    assert 'android:allowBackup="true"' not in manifest


def test_strings_ohne_unescapte_apostrophe():
    """Android bricht den Build bei einem rohen ' in strings.xml ab."""
    xml = _lese(os.path.join(ANDROID, 'res', 'values', 'strings.xml'))
    schuldig = [name for name, inhalt in re.findall(
        r'<string name="([^"]+)">(.*?)</string>', xml, re.S)
        if "'" in inhalt]
    assert not schuldig, "Apostrophe nicht escaped: " + ", ".join(schuldig)