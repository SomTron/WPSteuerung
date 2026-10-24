# Befunde, Warnungen und offene Punkte

Stand: 24.10.2026 (Branch `entwicklung`, Basis-Commit `2019248`
„unfertige api und zapfraten fix").

Diese Liste sammelt die gemeldeten Fehler/Schwachstellen, den jeweiligen
Bearbeitungsstand und die Analyse der beiden Log-Warnungen
(`/status` langsam, Start-Antizipation). Sie ist die Arbeitsliste für die
nächsten Schritte – **nicht** eine Beschreibung des Soll-Zustands.

## 1. Befundliste

| Nr. | Schwere | Komponente | Befund | Status |
|-----|---------|------------|--------|--------|
| B1 | hoch | `main.py`, Legionellen-Planung | Die 7-Tage-Schleife (`range(0, 8)`) plante auch Tage der **nächsten Kalenderwoche**. War der nächste Freitag besser als das aktuelle Wochenende, wurde die Fahrt still um eine Woche verschoben; am Montag schlug dann der Hygiene-Notfall an. | **erledigt** (`2019248`, Test `test_naechste_kalenderwoche_wird_nicht_geplant`) |
| B2 | mittel | `priority_control_logic.py`, Zapf-Vorhaltung | `planned_date` (Legionellen-Termin) überschrieb den Wochentag der gelernten Zapfmenge. Stand der Plan z. B. auf Sonntag, rechnete CalcStart die ganze Woche mit dem Sonntagsverbrauch (Baden) statt mit dem Werktagswert (Duschen). | **erledigt** (`2019248` + Test; Code/Kommentar hier auf „immer heute" bereinigt) |
| B3 | mittel | `api.py`, `/status` | Entscheidungs-Historie und KPIs wurden bei jedem 5-s-Poll neu von der SD-Karte gelesen (2–4 s auf dem Pi) → `/status` über der 3-s-Warnschwelle, WebApp-Timeout. Der erste Cache-Ansatz (10 s, Berechnung im Request) ließ bei 5-s-Poll noch jeden zweiten Request rechnen. | **erledigt** (hier fertiggestellt: 30 s TTL, stale-while-revalidate, Single-Flight-Hintergrund-Refresh, Tests) |
| B4 | niedrig | `richtige_config.ini` / Tests | Doppelte Sektion `[Wetterprognose]` ließ `configparser` mit `DuplicateSectionError` abbrechen. Datei ist lokal (gitignored); `test_telegram_handler.py` liest sie jetzt fehlertolerant. | **teilweise erledigt** – die Datei selbst wird nicht mehr versioniert; eine dauerhafte Prüfung auf Duplikate fehlt (siehe Backlog) |
| B5 | niedrig | `weather_forecast.py` | Docstring behauptet „IMMER genau 8 Werte", geliefert werden seit dem 7-Tage-Upgrade 9 (`forecast_tage`). | **offen** (Backlog) |
| B6 | mittel | `webapp/index.html`, `service-worker.js` | WebApp-Timeout `STATUS_TIMEOUT_MS` war 8 s und lag damit unter der realen `/status`-Dauer auf dem Pi. | **erledigt** (`2019248`: 12 s; hier Version `1.10.27` + Shell-Cache `v7`, damit die neue Hülle geladen wird) |
| B7 | niedrig | Repo-Hygiene | Der unfertige Commit enthielt 28 Backup-CSVs (`backup/`), `csv log/heizungsdaten.csv`, `Updater/wp-manager.sh.bak`, `Steuerung/test_config.ini` und das Wegwerf-Skript `test_header_fix.py` (nur `print`, keine Assertions, legte `backup/` im Root an). | **erledigt** (entfernt, `.gitignore` ergänzt, echter Test `tests/test_csv_header_fix.py`) |

### Was am unfertigen Commit konkret nachgearbeitet wurde

* **B3 (`api.py`)** – Der Cache rechnet nicht mehr im Request:
  * `_ENTSCHEIDUNGEN_KPI_TTL_SEC = 30 s` (statt 10 s). Bei 5-s-Poll rechnet
    der Pi höchstens alle 30 s statt jeden zweiten Request.
  * `_hole_entscheidungen_und_kpis(..., rechne_auswertung=...)`:
    Request → nur Cache lesen, bei Ablauf `_starte_entscheidungen_refresh()`
    (Hintergrund, Single-Flight) und den letzten gültigen Stand liefern.
    Vorwärmen (`erwaerme_auswertungen`, Startpfad) → synchron rechnen.
  * `time.monotonic()` + `None` als „nie berechnet" (NTP-Sprünge können den
    Cache nicht dauerhaft frisch halten).
  * Tests: Cache-Treffer, „Request rechnet nie selbst", Single-Flight;
    `conftest.py` setzt den Cache vor jedem Test kalt (sonst wandern
    Mock-Werte zwischen Tests).
* **B2 (`priority_control_logic.py`)** – Der Sonderfall ist entfernt:
  `_zapf_wochentag = now_local.weekday()`; der Kommentar erklärt, warum der
  Legionellen-Plan hier **nicht** mehr hineinwirkt.
* **B7** – Die Backup-CSVs stammten aus `utils.backup_csv()`, das relativ
  zum Arbeitsverzeichnis `backup/` anlegt. Der neue pytest-Test läuft in
  `tmp_path`, prüft die Reparatur **mit Assertions** und dass das Backup
  außerhalb des Projektordners landet.

## 2. Warum kamen die Warnungen im Log?

### W1 – `/status brauchte 3.7 s` und `WebApp-Fehler: Statusabruf Zeitüberschreitung`

```
WARNING - /status brauchte 3.7 s (Warnschwelle 3 s) - die WebApp bricht nach 8 s ab
WARNING - WebApp-Fehler (Seite /, Version 1.10.26): webapp: Statusabruf: Statusabruf Zeitueberschreitung
```

**Ursache (Kette):**

1. Die WebApp pollt `/status` alle **5 s**.
2. In `/status` wurden (bis 24.10.2026) bei **jedem** Aufruf
   `entscheidungs_log.historie()` **und** `entscheidungs_log.kpis()`
   aufgerufen. Beide lesen und parsen `Steuerung/entscheidungs_log.jsonl`
   von der SD-Karte: auf dem Pi 2–4 s.
3. Parallel laufen Sensorabfragen (1-Wire, ~750 ms je Fühler) und
   Netzarbeiten. Der 10-s-Regelzyklus und der `/status`-Poll überholen
   sich; die Antwortzeiten summieren sich.
4. Über Tailscale/WLAN kommt Netzlatenz dazu. Überschreitet die Antwort
   3 s → Warnung; reißt sie 8 s → bricht die WebApp ab
   (`STATUS_TIMEOUT_MS`) und zeigt „keine Werte".

**Das war kein Regelungsfehler** – Temperaturen und Kompressor-Status
standen längst bereit. Es war reine I/O-Last.

**Maßnahmen (Stand jetzt):**

* B3: Historie/KPIs sind 30 s gecacht und werden **nie im Request**
  gerechnet (stale-while-revalidate, Single-Flight). Antwortzeit < 1 ms.
* B6: WebApp-Timeout 8 s → 12 s puffert Verbindungsspitzen.
* Bereits vorher umgesetzt (23.10.2026): PV-Profil/14-Tage-Auswertung laufen
  nur im Hintergrund, `/status` liest nur Caches; Single-Flight verhindert
  paralleles Rechnen mehrerer Polls.
* Der `_melde_langsame_antwort`-Wächter bleibt aktiv (Warnschwelle 3 s,
  gedrosselt auf 5 min). Er ist das Frühwarnsystem, falls wieder etwas
  Teures in den Request-Pfad gerät.

**Noch sinnvoll (Backlog):** Poll-Intervall im Browser bei verstecktem Tab
drosseln und prüfen, ob 12 s Timeout reichen (Log weiter beobachten).

### W2 – `Info: Start-Antizipation: hub zur Obergrenze nur 5.5K (...)`

Kein Fehler, sondern ein **Schutzmechanismus**:

* Situation im Log: `unten 36.5 °C`, Ziel `42.0 °C` → Hub 5,5 K.
  Gleichzeitig `Einspeis=-1455 W` (Netzbezug) und `SOC=15 %` → der Lauf
  wäre reiner Netzstrom.
* Bei Netzbetrieb gilt die **Mindestlaufzeit 60 min** (+ 1 min Reserve).
  Bei der gelernten Rate (6 K/h, Confidence 40 %) wäre das Ziel schon nach
  ~55 min erreicht.
* Die WP müsste also 5–6 min über das Soll heizen – und würde dabei die
  obere Schicht (`oben 56,9 °C`, `mittig 55,6 °C`) an Schichtungs- bzw.
  Überhitzungsgrenze treiben und dort abgeschaltet.
* Die Start-Antizipation verhindert genau diesen Zyklus: Sie wartet, bis
  der Hub für die volle Mindestlaufzeit reicht (Boiler ~1 K kühler) oder
  der Spätstart erreicht ist.

**Was dagegen tun?** Nichts erzwingen. Wenn der Zustand häufig auftritt,
sind die Stellschrauben: Mindestlaufzeit (Zyklus-Config), Zieltemperatur
von CalcStart oder die Schichtungsgrenze – nicht die Antizipation selbst.

## 3. Backlog (Reihenfolge = Vorschlag)

1. **B5** – Docstring in `weather_forecast.py` auf 9 Rückgabewerte
   korrigieren (inkl. `forecast_tage`) und einen Test ergänzen, der die
   Tupellänge festnagelt (verhindert den nächsten stillen Bruch).
2. **B4** – Config-Duplikate dauerhaft verhindern: `configparser` mit
   `strict=True` ist Standard; besser eine Prüfung in `config_manager`
   (z. B. beim Laden `DuplicateSectionError` klar loggen und mit klarer
   Meldung abbrechen) + ein Test, der die ausgelieferte Beispiel-Config
   prüft.
3. **W1** – WebApp: Poll bei `document.hidden` auf 30–60 s drosseln
   (spart Pi-Last und Mobilfunk-Traffic); Timeout-Beobachtung im Log
   (`_melde_langsame_antwort`) auswerten.
4. **B3** – Nach dem Deploy die tatsächliche `/status`-Dauer im Log prüfen
   (Ziel < 500 ms). Falls die KPIs weiterhin auffallen: TTL auf 60 s.
5. Optional: `/status`-Antwortzeit als eigene Kennzahl in der Diagnose
   (P95 statt Einzelwarnung), damit „langsam" vor dem Timeout sichtbar wird.
