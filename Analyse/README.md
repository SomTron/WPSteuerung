# Analyse-Werkzeuge

Alle Skripte sind **lesend** und nutzen nur die Standardbibliothek (kein pandas
noetig). Sie aendern weder Code noch Konfiguration.

## Woher die Beispieldaten kommen

Die CSV-Auswertungen im `logs/`-Ordner und die `Analyse/*.csv` sind **nicht**
versioniert (`.gitignore`, Abschnitt "Analyse-Artefakte"). Sie sind mehrere
Gigabyte gross und enthalten personenbezogene Anlagendaten - im Repository
lagen zuletzt 65 MB davon, was das `.git` aufgeblaeht hat.

Ein frischer Clone hat sie also nicht. Wer sie braucht, holt sie vom Pi
(`logs/import/`) oder laesst sie neu entstehen:

```bash
# Heizdaten und Zyklen aus dem Betrieb holen (Pi -> Arbeitsverzeichnis)
scp pi@<host>:~/WPSteuerung/WPSteuerung/'csv log'/heizungsdaten.csv Analyse/
scp pi@<host>:~/WPSteuerung/WPSteuerung/'csv log'/zyklen.csv       Analyse/

# merged_data.csv (Winterdaten fuer Analyse/sim/calibrate.py) neu erzeugen
python3 Analyse/wp_analysis.py
```

Fehlt eine Datei, laufen die Skripte nicht kaputt - `calibrate.py` meldet
"Keine Realdaten gefunden." und endet mit Exit-Code 1.

| Skript | Eingabe | Zweck |
| :--- | :--- | :--- |
| `log_analyse.py` | `entscheidungs_log*.jsonl` | Strukturierte Auswertung des Entscheidungslogs (Regeln, Zyklen, Quellen, Tages-KPIs) |
| `log_analyse_text.py` | `heizungssteuerung_log_*.txt` | Klartext-Journal: Rate der Entscheidungspfade (Start-Antizipation/Overshoot) + "Gewinner sagt AUS gegen rangniedrigere EIN-Regel" |
| `analysis_core.py` | — | Gemeinsame Parser/Hilfen fuer `log_analyse.py` |
| `wp_analysis.py` | `heizungsdaten.csv` | Heizkurven, Aufheizraten, Start-Empfehlungen (erzeugt `analysis_results.md`) |
| `diag_auswertung.py` | Diagnose-Archiv | Zustands-/Lerndaten-Auswertung |
| `sim/` | Szenarien | Mehrtages-Simulation der **echten** Regelungslogik (siehe `sim/README.md`) |

## Log-Analyse in Klartext (`log_analyse_text.py`)

```bash
py -3 Analyse/log_analyse_text.py logs/heizungssteuerung_log_18.10.26.txt bericht.txt
```

Der Bericht enthaelt sechs Abschnitte:

* **A) Gewinner-AUS gegen rangniedrigere EIN-Regeln** - der Gewinner der
  Regelbewertung ist die hoechste Prioritaet mit EIN/AUS. Eine AUS-Regel
  verdeckt damit jede rangniedrigere Regel. Das Skript zaehlt diese
  Konstellationen (Grundlage fuer den Befund vom 18.10.2026, siehe
  `Steuerung/REGELWERK.md` 4.8).
* **B) Start-Antizipations-Sperren** - Rate-Klassen und die Gegenrechnung, wie
  viele Sperren mit der Kappung auf `heating_rate_max_c_h` (6 C/h) entfallen.
* **C) Overshoot-Vorhersagen** - jede Abschaltung mit Rate, Prognose und
  Restlaufzeit, plus Gegenrechnung mit gekappter Rate.
* **D) Lernzyklen/Planungsraten** - gelernte Zyklusraten (inklusive der als "zu
  kurz fuer die Rate" verworfenen) gegen die im Heizplan genannten Raten.
* **E) Zaehler** - Kompressor EIN/AUS, gespeicherte Zyklen, unterdrueckte
  Abschaltwuensche.
* **F) Code-Staende im Log** - alle `GitHub-Rev`-Marker mit Zeitraum und
  Anzahl Starts. Zeigt, welche Code-Version wann gelaufen ist - ein Journal
  ueber mehrere Deployments laesst sich damit in Abschnitte trennen.

Beide Textfassungen der Antizipations-Meldung werden erkannt (mit und ohne
`Confidence`/`Reserve`-Zusatz); nicht erkannte Zeilen werden angezeigt, damit
Format-Aenderungen auffallen.

Gemessene Ergebnisse vom 18.10.2026 (Log 14.09.-18.10.2026, 1460
Regel-Bewertungen) sind im Kopf des Skripts dokumentiert.
