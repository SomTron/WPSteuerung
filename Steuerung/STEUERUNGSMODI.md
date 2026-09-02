# Steuerungsmodi der WP-Steuerung

Diese Datei beschreibt **alle Steuerungsmodi** der Wärmepumpen-Steuerung sowie die
**Prioritäten-Engine** (Regel-Engine), die seit der Umstellung die eigentliche
Schaltlogik übernimmt. Sie ergänzt [`REGELWERK.md`](REGELWERK.md), das die
Gesamtlogik aus Regelsicht beschreibt.

---

## 1. Einordnung & Begriffsklärung

Die Steuerung wurde von einer **modusbasierten Logik** auf eine
**prioritätenbasierte Regel-Engine** umgestellt
(`archive/legacy_control_logic.py` ist Archiv, `priority_control.py` +
`priority_control_logic.py` sind live). Dadurch existieren **drei
Bedeutungsebenen** des Wortes „Modus":

| Ebene | Was ist das? | Wo sichtbar? |
|---|---|---|
| **Bedienmodi** | Vom Nutzer (Webapp/API/Telegram) oder automatisch geschaltete Sonderzustände, die Parameter der Regeln **verschieben** | Webapp-Schalter, Telegram-Buttons |
| **Regel-Modi** | Die aktuell **gewinnende Regel** der Prioritäten-Engine (= „Aktueller Modus"-Anzeige) | Webapp `mode.current`, Telegram-Status, CSV-Log |
| **Sperr-/Schutzzustände** | Dauerhafte oder temporäre Blockaden unabhängig von den Regeln | `blocking_reason` in Webapp/API |

> **Wichtig:** `state.control.previous_modus` enthält heute den **Namen der
> Gewinner-Regel** (z. B. `Abweichung`, `PV_unten`, `CalcStart`), nicht mehr den
> alten Betriebsmodusnamen. Der Initialwert beim Start ist `"Normalmodus"`.

### 1.1 Aufrufweg der Engine (Überblick)

```
main.run_logic_step()
  └─ pcl.determine_mode_and_setpoints(state, t_unten, t_mittig, learning_engine)
      ├─ effektive Config bauen (Bad-/Urlaubs-/Sommer-Offsets, tiefe Kopie)
      ├─ LearningEngine.update(...)                     (Lernen, s. Kap. 3.4)
      ├─ pcl.bewerte_alle_regeln(...)                  (alle 12+ Regeln)
      └─ Gewinner → Setpoints extrahieren (_extract_*)
      └─ Rückgabe { modus, soll_einschalten, gewinner_ergebnis, ... }
  ├─ handle_compressor_off(...) / handle_compressor_on(...)   (Mindestlauf-/pausen, Boiler-Max)
  └─ Legionellen-Lifecycle-Tracking
```

---

## 2. Bedienmodi (benutzer-schaltbar / automatisch)

### 2.1 Normalmodus

- **Flag:** `state.control.previous_modus = "Normalmodus"` (Initialwert)
- **Wirkung:** Kein Offset; alle Regeln arbeiten mit den unveränderten Sollwerten
  aus `wp_steuerung_parameter.json`.
- **Aktivierung:** Wird automatisch übernommen, wenn kein Sondermodus gesetzt ist
  und gerade keine andere Gewinner-Regel den Modus setzt.

### 2.2 🛁 Bademodus (erhöhter Warmwasserbedarf)

- **Flag:** `state.bademodus_aktiv` (Boolean)
- **Schaltung:** Webapp-Schalter → `POST /control {command: "set_mode", mode:
  "bademodus"}`; Telegram-Button „🛁 Bademodus"; nur die Werte
  `bademodus`/`urlaubsmodus` sind als Modénamen erlaubt
  (`api.py: ALLOWED_MODES`).
- **Wirkung** (`determine_mode_and_setpoints`):
  - `abweichung.solltemperatur_c += bademodus.solltemperatur_erhoehung_c`
  - Standard **+3.0 K** (aus `bademodus.solltemperatur_erhoehung_c`,
    `wp_steuerung_parameter.json`)
  - **Bricht die Nachtsperre** für die Abweichungs-Regel: `evaluate_abweichung`
    erlaubt dann das Einschalten auch zwischen 19–8 Uhr
    („der Nutzer möchte warmes Wasser, auch nachts").
- **Deaktivierung:** Schalter/Telegram „🛁 Bademodus aus" → Flag auf `False`.

### 2.3 🌴 Urlaubsmodus (Sparmodus/Abwesenheit)

- **Flag:** `state.urlaubsmodus_aktiv` (Boolean) + `urlaubsmodus_start/ende`
  (datetimes, nur Telegram-Variante)
- **Schaltung:** Telegram „🌴 Urlaub" mit **Zeitauswahl (Dauer)**;
  Webapp-Schalter `set_mode/mode=urlaubsmodus`.
- **Wirkung:** `abweichung.solltemperatur_c -= URLAUBSABSENKUNG`
  (Standard **−15 K**, `[Urlaubsmodus] URLAUBSABSENKUNG`, `config.ini.example`).
  Der Boiler wird nur noch im absoluten Notfall (Notfallschutz ≤ 36 °C)
  geheizt.
- **Besonderheit:** Telegram-Aktivierung fragt die Dauer ab (1–7 Tage) und
  setzt automatisch `urlaubsmodus_ende = now + duration`; die
  Telegram-UI deaktiviert nach Ablauf automatisch.

### 2.4 ☀️ Sommer-Modus (automatisch, PV-Prognose-basiert)

- **Flag:** `state.sommer_modus_aktiv` (Boolean) + Zähler `sommer_modus_zaehler`
- **Bewertung:** `logic_utils.evaluate_sommer_modus` (1× pro Kalendertag, nach
  Forecast-Update):
  - Prognose „gut" = **heute, morgen UND übermorgen** jeweils ≥
    `sommer_modus.mindest_prognose_wh` (Standard **2000** Wh/m²).
  - Nach **`benoetigte_tage`** (Standard **3**) aufeinanderfolgenden guten Tagen
    wird der Modus aktiviert.
  - Schlechte oder unvollständige Prognose setzt die Serie zurück und
    deaktiviert den Modus **sofort** (konservativ); Datenlücke > 1 Tag bricht
    die Serie ebenfalls.
- **Wirkung** (`wende_sommer_offset_an` auf die effektive Config):
  - `abweichung.solltemperatur_c` − `temperatur_offset_c` (Standard **−3.0 K**)
  - Alle PV-Regeln `ausschalten_bei_c` − `pv_ausschalt_offset_c`
    (Standard **−2.0 K** → 46 °C statt 48 °C)
  - **Synchron dazu** `einschalten_bei_c` − `pv_einschalt_offset_c`
    (Standard **−2.0 K** → 40 °C statt 42 °C) – dadurch bleibt die Hysterese
    erhalten und das Taktverhalten verbessert sich
    („Sommer-Modus Hysterese"-Anforderung).
  - Garantien (MinTemp/Notfallschutz) bleiben unangetastet.
- **Bademodus-Vorrang:** Ist der **Bademodus** aktiv, wird der Sommer-Offset
  **temporär ausgesetzt** (`if not state.bademodus_aktiv`), damit die
  Temp-Anhebung (+3 K) des Bademodus nicht neutralisiert wird.

> **Warum:** Im Sommer scheint fast täglich die Sonne → der Boiler muss nicht
> jeden Tag auf 44 °C+ hochgeheizt werden; morgen kommt ja wieder PV-Strom.

### 2.5 Übersicht Bedienmodi

| Modus | Flag (State) | Effektive Änderung | Standard | Quelle |
|---|---|---|---|---|
| Normal | `previous_modus="Normalmodus"` | – | – | – |
| Bademodus | `bademodus_aktiv` | Abweichungs-Soll **+3.0 K**; Nachtsperren-Bypass | `bademodus.solltemperatur_erhoehung_c=3.0` | JSON |
| Urlaubsmodus | `urlaubsmodus_aktiv` | Abweichungs-Soll **−15 K** | `[Urlaubsmodus].URLAUBSABSENKUNG=15` | INI |
| Sommer-Modus | `sommer_modus_aktiv` | Soll **−3.0 K**, PV-Ausschaltpunkte **−2.0 K** nach 3 guten Tagen | `sommer_modus.*` | JSON |

---

## 3. Die Prioritäten-Engine (genaue Beschreibung)

### 3.1 Architektur & Dateien

| Datei | Rolle |
|---|---|
| `priority_control.py` | **Regeln** (`evaluate_*`-Funktionen), `RegelErgebnis`, `bewerte_alle_regeln`, `formatiere_ergebnisse` |
| `priority_control_logic.py` | **Orchestrierung**: `determine_mode_and_setpoints`, Gewinner-Debounce, Setpoint-Extraktion, `handle_compressor_on/off`, Taktschutz, Boiler-Maximum, Sicherheits-Checks |
| `json_config.py` | Typsichere Pydantic-Modelle für alle Regel-Konfigurationen |
| `learning_engine.py` | Selbstlernende Parameter (Heizraten, Zapf-Zeiten, Forecast-Kalibrierung, Surplus-Profil) |
| `main.py` | Hauptloop, `run_logic_step`, Sommer-/Legionellen-Tracking |

### 3.2 Das Daten-Schema `RegelErgebnis`

Jede Regel liefert pro Zyklus genau ein `RegelErgebnis`:

```python
@dataclass
class RegelErgebnis:
    name: str                       # Anzeigename, z.B. "PV_unten"
    prioritaet: int                 # höher = wichtiger
    aktiv: bool                     # Regel "im Spiel"? (sonst: sensoren fehlen, Nachtsperre, etc.)
    einschalten: Optional[bool]     # True=EIN  |  False=AUS  |  None=keine Aktion
    grund: str                      # lesbarer Entscheidungsgrund (Log/Webapp)
    regel_dict: Optional[Dict]      # optionale Zusatzdaten für Fehlersuche
```

- **`aktiv=False`** → Regel darf *nicht* gewinnen (z. B. Sensor fehlt, Nachtsperre,
  Konfig inaktiv).
- **`einschalten=None`** → Regel ist bewertungsfähig, will aber *gerade keine
  Aktion* („stumm"/Hysterese) und blockiert damit auch keine andere Regel.
- **`einschalten=True/False`** → klare Entscheidung; nur solche Regeln kommen in
  die Gewinner-Auswahl.

### 3.3 Ablauf von `bewerte_alle_regeln` (Schritt für Schritt)

Die Funktion (Signatur in `priority_control.py:1404`) bekommt die **effektive
Config** (inkl. Bad-/Urlaub-/Sommer-Offsets), Sensorwerte, Solardaten, Lernwerte
und Flags.

Ablauf:

1. **Alle Regeln in fester Reihenfolge bewerten** (Reihenfolge ist fürs Ergebnis
   egal, da später nach Priorität sortiert wird):
   -1. `Notfallschutz` (`evaluate_notfallschutz`)
   0. `Wochenende` (`evaluate_wochenende`)
   1. `Einspeisung` (`evaluate_einspeisung`)
   1a. **jede PV-Regel** aus `config.pv_regeln[]` (`evaluate_pv_regel`)
   2. `Komfort` (`evaluate_komfort`)
   2b. **jeden MinTemp-Eintrag** (`evaluate_mindesttemp` → Liste)
   2c. `Batterie` (`evaluate_batterie`)
   3. `Zeitfenster` (`evaluate_zeitfenster`)
   4. `Abweichung` (`evaluate_abweichung`)
   5. `Forecast` (`evaluate_forecast`)
   6. `AdaptivePV` (`evaluate_adaptive_pv`)
   7. `CalcStart` (`evaluate_calculated_start`)
   8. `Legionellen` (`evaluate_legionellen`)

2. **Kandidaten filtern:** `aktive_regeln = [e for e in ergebnisse if e.aktiv
   and e.einschalten is not None]`. Ist die Liste leer → Rückgabe
   `(None, ergebnisse)` = „Keine Regel aktiv" → Kompressor wird mit der
   Standard-Abschaltlogik gefahren (siehe Kap. 4).

3. **`solar_stale`-Pause** (Solardaten veraltet, `_solar_daten_veraltet`):
   Die Regeln `Einspeisung`, `Batterie`, `AdaptivePV`, `Zeitfenster`, `Forecast`
   und alle `PV_*`-Regeln werden **pausiert** (aktiv=False, einschalten=None,
   Grund „Solar-Daten veraltet -> Regel pausiert"). **Bewusst NICHT** pausiert:
   Notfallschutz, MinTemp-Garantien, Komfort und Abweichung (Netzstrom als
   Rückfallebene).

4. **Sortieren & Gewinner wählen:** `aktive_regeln.sort(key=prioritaet,
   reverse=True)`; `gewinner = aktive_regeln[0]` → die **höchstpriore Regel mit
   klarer Entscheidung** gewinnt.

5. **Notfallschutz (Prio 110) greift von selbst:** Da er die höchste Priorität
   hat, braucht es keinen manuellen Override mehr. Der frühere Spezialfall
   („Komfort-`NOTFALL` überschreibt Wochenende") ist entfernt – der Schutzleiter
   gewinnt über alle Sperren allein über die Prioritäten-Kaskade.

> Details zu jeder Regel siehe **Kap. 5** – die Tabellen dort enthalten
> EIN-/AUS-/Weiterlauf-Bedingungen, Hysteresen und Konfig-Schlüssel.

### 3.4 Orchestrierung in `determine_mode_and_setpoints` (`priority_control_logic.py`)

Vor dem Regel-Aufruf passiert:

1. **Effektive Config** (tiefe Kopie, Offsets je Bedienmodus, s. Kap. 2):
   - Bademodus: `abweichung.solltemperatur_c += 3.0`
   - Urlaubsmodus: `abweichung.solltemperatur_c -= URLAUBSABSENKUNG`
   - Sommer-Modus: `wende_sommer_offset_an(effektive_config)` (Soll −3 K,
     PV-Ausschaltpunkte −2 K)
2. **Forecast-Werte** aus `state.solar` für die Regeln:
   - `forecast_wh_qm` = **morgen**-Prognose (Forecast/AdaptivePV)
   - `forecast_today_wh` = **heute**-Prognose (CalcStart)
3. **LearningEngine-Update**: Heizzyklen, Zapfungen, Solar-Tracking
   (nur wenn `learning_engine` übergeben wurde).
4. **Lernparameter lesen** (mit Defensiv-Fallbacks, wenn Methoden fehlen):
   - `gelernte_rate_unten` / `gelernte_rate_gesamt` (Heizraten °C/h)
   - `gelernte_zielzeit` (Zapf-Zielstunde, default `calculated_start.target_uhr`)
   - `gelerntes_abendfenster` / `gelerntes_morgenfenster(+Bonus)`
   - `fc_ratio` (Forecast-Kalibrierung, default 1.0)
   - `surplus_profil` (Surplus-Stundenprofil, default None)
   - `recent_usage_events` (default [])
5. **CalcStart-Konflikt-Guard**: einmalig warnen/loggen, wenn die CalcStart-
   Zielzeit innerhalb/hinter der Nachtsperre liegt (Regel wird sonst stumm).
**Nach dem Gewinner**:

7. `state.control.active_rule_name` / `active_rule_sensor` setzen; `regelfuehler`
   aus `grund`/`name` der Gewinner-Regel abgeleitet (unten/mittig/oben).
8. **Setpoints** über `_extract_einschaltpunkt` / `_extract_ausschaltpunkt`
   **aus der effektiven Config** extrahieren (Wichtig: sonst würden die
   Bad-/Urlaubs-Offsets in der Anzeige fehlen) und in
   `state.control.aktueller_einschaltpunkt` / `aktueller_ausschaltpunkt`
   eintragen. Bei `einschalten=False` wird der Einschaltpunkt auf
   `max(eps, ausp)` angehoben, damit kein Neu-Einschalten erfolgt.
9. `state.control.komfort_aktiv` setzen (für UI), `solar_ueberschuss_aktiv`
   = `pv_leistung >= min(pv_schwelle aller PV-Regeln)`.
10. **Rückgabe-Dict:**
    ```python
    { "modus": gewinner.name if gewinner else "Keine Regel aktiv",
      "einschaltpunkt", "ausschaltpunkt", "regelfuehler",
      "solar_ueberschuss_aktiv", "soll_einschalten", "gewinner_ergebnis",
      "alle_ergebnisse" }
    ```
11. **Wechsel-Debounce + Schaltverzug** (`_gewinner_debounce`): Ein Modus-
    Wechsel wird erst nach **2 aufeinanderfolgenden identischen Bewertungen**
    bestätigt (verhindert Pendeln an scharfen Kanten, z.B. Batterie-EIN
    42,0 °C vs. Komfort-AUS). **`soll_einschalten` wird erst an die Hardware
    übergeben**, wenn der Gewinner-Wechsel per Debounce bestätigt ist –
    bis dahin gilt die letzte bestätigte Schaltempfehlung
    (`state.control._soll_einschalten_bestaetigt`). Nur bestätigte Wechsel
    setzen `previous_modus` und zählen für den Taktschutz (`_track_wechsel`).
12. **Entscheidungslog** (`entscheidungs_log.jsonl`, JSON-Lines): pro Zyklus ein
    Eintrag (Gewinner, Grund, soll_einschalten, Kompressor-Status, PV, SOC,
    Temperaturen) – nie blockierend, Fehler werden nur gedebuggt.

### 3.5 Die Schaltentscheidung (`handle_compressor_on`/`handle_compressor_off`)

`main.run_logic_step` übernimmt `soll_einschalten` (bereits debounce-bestaetigt)
in `state.control._soll_einschalten` und ruft je nach Kompressor-Status:

**`handle_compressor_off`** (Kompressor läuft):
1. **Überhitzungsschutz** (harte Abschaltung): `t_oben >= ueberhitzung_c`
   (Standard 58 °C; während Legionellen dynamisch auf `legionellen_max_temp_c`)
   → sofort AUS, `force=True`, `blocking_reason` setzen.
2. **Boiler-Maximum** (hart, bricht Mindestlaufzeit): Bezugsfühler
   (`sicherheit.boiler_max_fuehler`, Standard `unten`) ≥ `sicherheit.max_temp_c`
   (Standard 48 °C) → AUS; Wiedereinschalten erst, wenn Fühler ≤
   `max_temp_c - boiler_max_hysterese_k` (Standard 46 °C); setzt
   `boiler_max_blockiert`.
3. **Keine Regel aktiv / Regel sagt AUS:** `_soll_einschalten == False` → sofern
   die **Mindestlaufzeit** (`zyklus.mindestlaufzeit_minuten`, Standard 60 min)
   erreicht ist, wird ausgeschaltet. `regel_name` unterscheidet im Log
   „Regel X sagt AUS" von „Keine Regel aktiv".

**`handle_compressor_on`** (Kompressor aus):
1. **Boiler-Max-Kühlphase:** `boiler_max_blockiert` aktiv → erst wieder EIN,
   wenn der Bezugsfühler ≤ Wiederein-Schwelle abgekühlt ist.
2. **Ein-Sperre in Limitnähe:** Bezugsfühler ≥
   `max_temp_c - boiler_max_ein_abstand_k` (Standard 48 − 3 = 45 °C) → kein
   Start (verhindert Kurzlauf am Limit).
3. **Taktschutz** (Kap. 4.5): zusätzliche Mindestpause verlängern.
4. **Neustartsperre** (Kap. 4.6): `restart_lockout_until` in der Zukunft → keine
   Aktion, Grund im `blocking_reason`.
5. **Mindestpause** (`zyklus.mindestpausenzeit_minuten`, Standard 30 min) prüfen.
6. **Stop-Bedingung:** Regelfühler der aktiven Regel ≥ Ausschaltpunkt → kein Start
   (`blocking_reason="Zieltemp erreicht"`).
7. Sonst: Einschalten (`set_kompressor_status(True)`) und Verifizierungs-
   Startwerte (t_verd/t_unten) für die Kompressor-Verifikation speichern.
6. **`bewerte_alle_regeln(...)`** aufrufen und das **Gewinner-Ergebnis** greifen.
### 3.6 Setpoint-Extraktion (`_extract_einschaltpunkt` / `_extract_ausschaltpunkt`)

Die Setpoints werden **nicht von der Engine berechnet**, sondern pro
Gewinner-Regel aus deren Konfiguration abgeleitet (Anzeige + Abschaltung):

| Regel | Einschaltpunkt | Ausschaltpunkt |
|---|---|---|
| `PV_*` | `pv_regel.einschalten_bei_c` (42) | `pv_regel.ausschalten_bei_c` (48) |
| `Komfort` | `komfort.komfort_einschalten_bei_c` (38) | `komfort.ausschalten_bei_c` (42) |
| `Zeitfenster` | `zeitfenster.max_temp_fuer_einschalten_c` (44.5) | dito |
| `Abweichung` | `soll − einschalten_bei_abweichung_k` (44−4,9≈39,1) | `soll − ausschalten_bei_abweichung_k` (44−0,7=43,3) |
| `Forecast` | `forecast.t_vorheiz_ab_c` (44) | `forecast.tmax_c` (48) |
| `AdaptivePV` | `adaptive_pv.base_threshold_watt` (300 W) | `adaptive_pv.tmax_c` (48) |
| `CalcStart` | `calculated_start.solltemperatur_c` (44) | `calculated_start.tmax_c` (48) |
| `MinTemp-*` | `eintrag.min_temp_c` | `eintrag.min_temp_c + hysterese_k` |
| `Batterie` | `batterie.einschalten_bei_c` (42) | `batterie.ausschalten_bei_c` (47) |
| `Einspeisung` | `ausschalten_bei_c − 6.0` (42, Anzeige-Wert) | `einspeisung.ausschalten_bei_c` (48) |
| `Legionellen` | `target_temp_c − 5.0` (55, Anzeige-Wert) | `legionellen.target_temp_c` (60) |
| Default | `sicherheit.max_temp_c` (48) | `sicherheit.max_temp_c` (48) |

(Werte in Klammern = Defaults aus `json_config.py` / `wp_steuerung_parameter.json`)

### 3.7 Die wichtigsten Quellen-Helfer

**`_energiequelle_ok(feedin, soc, pv_min_watt, soc_min_prozent, max_netzkauf_watt)`**
– „Strom-Priorität: PV direkt > Batterie > Netz":
```python
if feedin >= pv_min_watt:                          # echte PV-Einspeisung
    return True
return soc >= soc_min_prozent and feedin >= max_netzkauf_watt  # volle Batterie, kein Netzkauf
```

**`_energiequelle_mit_grund(...)`** – dieselbe Logik, aber mit lesbarem Grund für
Regel-Logs („PV 1234W >= 50W", „Batterie SOC 92% >= 90%", „SOC 41% < 90%").

**`_forecast_quelle_ok(...)`** – fürs Vorheizen (Forecast-Regel): erlaubt Netz nur,
wenn `vorheiz_netz_erlaubt=True` gesetzt ist; sonst `_energiequelle_mit_grund`.

**`_mittagstief_stunden(...)`** – Surplus-schwache Stunden (< 250 W) zwischen jetzt
und Zielzeit aus dem gelernten Profil; liefert Anzahl + Label
(z. B. „Mittagstief 12-13 Uhr").

---

## 4. Automatische Sperren & Schutzfunktionen

Unabhängig von der Regel-Auswahl greifen folgende Zustände (blockieren
Einschaltungen bzw. erzwingen Abschaltungen):

| Funktion | Wirkung | Parameter (Default) |
|---|---|---|
| **Nachtsperre** | Keine Einschaltungen 19–8 Uhr; Ausnahmen: Notfallschutz, MinTemp mit `nachtsperre_ueberschreiben=true`, Abweichung bei Bademodus | `sicherheit.nachtsperre_start/ende` (19/8) |
| **Wochenende-Sperre** | Sa/So vor `fruehestens_uhr` keine Einschaltungen (Regel Prio 100, blockierend); Ausnahme: Notfallschutz (Prio 110) | `wochenende.fruehestens_uhr` (9) |
| **Boiler-Maximum** | Hartes AUS, wenn Bezugsfühler ≥ `max_temp_c`; Wiederein erst ≤ `max_temp_c − hysterese`; + kein EIN in Limitnähe (< `ein_abstand`) | `sicherheit.*` (48 / 2.0 / 3.0) |
| **Überhitzungsschutz** | Sofort-Abschaltung bei `t_oben ≥ ueberhitzung_c`; Warnung ab `max_temp_c + 2`. **Legionellen-Bypass:** Während aktiver Legionellenfahrt wird `ueberhitzung_c` dynamisch auf `legionellen_max_temp_c` angehoben | `sicherheit.ueberhitzung_c` (58) / `legionellen.legionellen_max_temp_c` (65) |
| **Kompressor-Verifikation** | Nach dem EIN wird geprüft, ob t_verd/t_unten plausibel fallen/steigen; sonst Fehler + Neustartsperre | `main.py` |
| **Neustartsperre** | Nach Verifizierungsfehler keine Neueinschaltung für X Minuten | `setze_neustartsperre(minuten=10)` |
| **Taktschutz** | > `max_wechsel_pro_stunde` Gewinner-Wechsel/h → Zusatzpause `zusatz_pause_minuten` | `taktschutz.*` |
| **Mindestlaufzeit** | Laufender Lauf wird nicht vor Ablauf unterbrochen (außer Sicherheit/Boiler-Max). **PV-Läufe** (von `PV_*`/`AdaptivePV`/`Einspeisung` gestartet) dürfen nach Ablauf der Hardware-Schutzzeit `pv_min_laufzeit_minuten` (10–15 min) bei PV-Einbruch abschalten – ohne 60 min Netzbezug zu erzwingen | `zyklus.mindestlaufzeit_minuten` (60) / `zyklus.pv_min_laufzeit_minuten` (10) |
| **Mindestpause** | Kein Neustart vor Ablauf nach dem AUS | `zyklus.mindestpausenzeit_minuten` (30) |
| **Solar-stale** | PV-/Batterie-/Forecast-/AdaptivePV-Regeln pausiert bei veralteten Solardaten (Notfallschutz/Garantien bleiben) | `SOLAR_DATA_STALE_THRESHOLD_MIN` |
---

## 5. Die Regelbausteine im Detail

Die Prioritäten-Engine besteht aus **13 Regeln** (bzw. Regel-Gruppen). Nachfolgend
jede Regel mit Priorität, Zweck, Eingangsgrößen, Bewertungslogik (in der
Reihenfolge der Prüfungen im Code!), Hysterese, Nachtsperren-Verhalten und
Konfigurationsschlüsseln.

> **Aktuelle Prioritäten-Kaskade (glatt):**
> `110 Notfallschutz → 100 Wochenende → 90 Legionellen → 85 Einspeisung →
> 78 AdaptivePV / PV_unten / PV_mitte (Fallback) → 75 Batterie → 65 MinTemp →
> 60 Komfort → 57 Forecast → 53 Zeitfenster → 47 Abweichung`

> Die **Konfig-Werte** in Klammern sind die Defaults/aktuellen Werte aus
> `wp_steuerung_parameter.json` bzw. `json_config.py`. Regel-Evaluatoren sind in
> `priority_control.py` implementiert.

---

### 5.1 Notfallschutz (Prio 110) – Reiner Schutzleiter

- **Zweck:** Aus dem früheren Komfort-Notfall ausgekoppelter, eigenständiger
  Schutzleiter für die Brauchwasser-Mindesttemperatur. Sinkt die Nutz-
  Wassertemperatur unter `einschalten_bei_c` (36 °C), wird **ohne Workaround
  vor allen Sperren** eingeschaltet (Wochenende, Nachtsperre – nichts kann ihn
  blockieren).
- **Eingänge:** `temp_dict` (Fühler-Priorität: **oben** > **mittig** > **unten**).
- **Logik** (`evaluate_notfallschutz`):
  1. Config inaktiv → inaktiv.
  2. Fühler in der Prioritätsreihenfolge auswählen; keiner verfügbar → inaktiv.
  3. `temp <= einschalten_bei_c` (36) → `EIN` (`NOTFALLSCHUTZ`).
  4. Sonst → `einschalten=None` (**stumm** – blockt andere Regeln nie).
- **Besonderheiten:**
  - Kein Nachtsperren-/Wochenende-/Urlaubs-Check nötig – die **Priorität 110**
    gewinnt gegen alle Sperren (früher: Spezialfall in `bewerte_alle_regeln`).
  - Die Abschaltung nach der Notfall-Heizung erfolgt über den normalen Setpoint
    (`ausschalten_bei_c` = 38 °C, via `_extract_ausschaltpunkt`).
- **Konfig:** `notfallschutz.{aktiv (true), prioritaet (110),
  einschalten_bei_c (36.0), ausschalten_bei_c (38.0)}`.

---

### 5.2 Wochenende (Prio 100) – Blockier-Regel

- **Zweck:** Am Wochenende (Sa/So) soll vor einer definierten Uhrzeit nicht
  geheizt werden.
- **Eingänge:** `now` (Wochentag/Stunde).
- **Logik** (`evaluate_wochenende`):
  1. Regeln deaktiviert (`wochenende.aktiv=false`) → inaktiv.
  2. Kein Wochenende → inaktiv („Kein Wochenende").
  3. Wochenende **und** `now.hour < fruehestens_uhr` → `aktiv=true`,
     `einschalten=false` (**blockiert alles andere**, höchste Priorität).
  4. Wochenende ab `fruehestens_uhr` → `aktiv=true`, `einschalten=None`
     (Freigabe: andere Regeln dürfen entscheiden).
- **Nachtsperre:** – (nicht relevant für die Regel selbst, wirkt formal über die
  anderen Regeln).
- **Besonderheiten:** Prio 100 ist die höchste *blockierende* Regel; nur
  der Notfallschutz (Prio 110) liegt darüber (Kap. 3.3, Punkt 5).
  Die Legionellen-Regel (Prio 90) holt ihre Fahrt nach der Sperre nach
  (siehe 5.3).
- **Konfig:** `wochenende.aktiv` (true), `wochenende.fruehestens_uhr` (9),
  `wochenende.prioritaet` (100).
---

### 5.3 Legionellen (Prio 90) – Legionellenprophylaxe

- **Zweck:** Einmal pro Kalenderwoche das Warmwasser auf ``target_temp_c`` (60 °C)
  erhitzen und dort **``probezeit_minuten`` (30 min)** halten, um
  Legionellenbildung im Boiler zu verhindern.
- **Eingänge:** `temp_dict["unten"]`, `now`, `legionellen_aktiv`,
  `legionellen_last_done`, `legionellen_started_at`, `kompressor_ein`.
- **Planung (in `main.py`, nach dem Forecast-Update):**
  - Nur relevant, wenn die Prophylaxe in der **aktuellen Kalenderwoche** noch
    nicht durchgeführt wurde (`legionellen_last_done`).
  - Sucht den **besten Tag** zwischen `bevorzugter_tag` (Standard **4** =
    Donnerstag) und `letzter_tag` (Standard **6** = Sonntag), ausgehend vom
    heutigen Wochentag.
  - Auswahl nach PV-Prognose: Der Alternativ-Tag gewinnt, wenn seine Prognose
    um ≥ `erforderliche_wh_qm` (800 Wh/m²) besser ist als der bevorzugte Tag
    **oder** ≥ `pv_prognose_schwelle_gut` (2000 Wh/m²) erreicht und der
    bevorzugte Tag das nicht tut (Legionellenerwärmung soll möglichst mit
    PV-Strom laufen).
  - Ergebnis: `legionellen_planned_day` / `legionellen_planned_time` (API-Anzeige).
- **Logik** (`evaluate_legionellen`):
  1. Config inaktiv → inaktiv.
  2. `t_unten` fehlt → inaktiv.
  3. **Prophylaxe läuft bereits** (`legionellen_aktiv=true`):
     - `t_unten < target_temp_c` → `EIN` („heize weiter").
     - `t_unten >= target_temp_c`: Probezeit seit `legionellen_started_at`
       abgelaufen → `AUS` (Ziel erreicht + Probezeit gehalten); sonst `EIN`
       (Ziel erreicht, warte Probezeit).
  4. **Neue Prophylaxe:**
     - Bereits in dieser Kalenderwoche erledigt → inaktiv.
     - Nur **exakt zur Startzeit** (`now.hour == start_h` und
       `now.minute >= start_m`) aktiv; sonst inaktiv.
     - Kompressor läuft bereits → `einschalten=None` (warte auf Abschluss).
     - Sonst → `EIN`.
- **Lifecycle (in `run_logic_step`):** Bei `EIN` wird `legionellen_aktiv=true`,
  `legionellen_started_at=jetzt` und der Temperatur-Override
  `legionellen_max_temp_c` (65 °C) gesetzt. Erreicht `t_unten` das Ziel, wird
  `legionellen_target_reached_at` gesetzt (Probezeit-Start). Liefert die Regel
  `AUS` und die Probezeit ist abgelaufen → `legionellen_last_done=heute`,
  `legionellen_aktiv=false`, Override gelöscht, Telegram-Erfolgsmeldung.
  Ohne Zielerreichung → Abbruch der Prophylaxe.
- **Nachtsperre:** Regel kennt keine Nachtsperre (Startzeit 8 Uhr liegt außerhalb).
- **Wochenende (Prio 90 < 100):** Am Sa/So blockt die Wochenende-Sperre Starts
  vor `fruehestens_uhr` (9 Uhr). Die Regel haelt das Startfenster flexibel
  offen, bis die Sperre endet, und holt die Fahrt danach nach
  (Startfenster bis `spaeteste_start_uhr`, Standard 16 Uhr).
- **Konfig:** `legionellen.{aktiv (true), prioritaet (90), target_temp_c (60),
  legionellen_max_temp_c (65), bevorzugter_tag (4), letzter_tag (6),
  start_uhr (8), spaeteste_start_uhr (16), probezeit_minuten (30),
  erforderliche_wh_qm (800), pv_prognose_schwelle_gut (2000)}`.

---

### 5.4 Einspeisung (Prio 85) – PV-Shaping am Netzlimit

- **Zweck:** Heizen, wenn die PV-Einspeisung die Einspeisegrenze erreicht
  (Standard **7500 W**). Der Strom wäre sonst gedrosselt worden → quasi gratis.
- **Eingänge:** `feedin_watt`, `temp_dict[temperaturfuehler]`, `kompressor_ein`,
  `now_hour`.
- **Logik** (`evaluate_einspeisung`):
  1. Config inaktiv → inaktiv.
  2. Nachtsperre → inaktiv (nachts keine PV-Einspeisung).
  3. Sensor fehlt → inaktiv.
  4. `temp >= ausschalten_bei_c` (48) → `AUS` (Ziel erreicht).
  5. **Weiterlauf:** Kompressor läuft **und** `feedin >= weiterlauf_ab_watt`
     (6500 W) → `EIN` (Weiterlauf bis Ausschaltpunkt). Der Abschlag von
     7500 → 6500 W kompensiert den Eigenverbrauch der WP (~600 W), der die
     Einspeisung beim Start einbrechen lässt und sonst Flattern verursachen
     würde.
  6. **Neustart:** `feedin >= einspeisegrenze_watt` (7500 W) → `EIN`.
  7. Sonst → `einschalten=None` (keine Aktion).
- **Konfig:** `einspeisung.{aktiv (true), prioritaet (83),
  einspeisegrenze_watt (7500), weiterlauf_ab_watt (6500),
  temperaturfuehler (unten), ausschalten_bei_c (48)}`.

---

### 5.5 CalcStart (Prio 82) – Berechneter Start zur Zielzeit

- **Zweck:** Berechnet, wann der Kompressor spätestens starten muss, damit zur
  **gelernten Zapf-Zielzeit** (Standard `target_uhr` **17:00**) genügend warmes
  Wasser bereitsteht – möglichst mit PV.
- **Eingänge:** `temp_dict`, `now_hour/minute`, `forecast_today_wh_qm`
  (heute-Prognose), gelernte Heizraten/Zielzeit, `feedin_watt`, `soc`,
  `fc_ratio`, `surplus_profile`.
- **Logik** (`evaluate_calculated_start`):
  1. Config inaktiv → inaktiv.
  2. Nachtsperre → inaktiv (mit Konflikt-Hinweis, wenn die Zielzeit innerhalb/
     hinter der Sperre liegt – dann kann die Regel NIE feuern).
  3. `temp_unten`/`temp_mitte` fehlen → inaktiv.
  4. `temp_unten >= tmax_c` (48) → `AUS` (Ziel bereits erreicht).
  5. `current_time >= target_uhr` → inaktiv („Nach Zielzeit").
  6. **Temperatur-Differenz:** `diff = max(0, soll - ist)` für unten/mittig/oben;
     `diff_gesamt` muss > 0 sein, sonst `AUS`.
  7. **Heizrate:** gelernte Rate (`learning_engine.get_learned_heating_rate`)
     oder Config-Default `heizrate_unten_c_h` (3.0 °C/h) /
     `heizrate_gesamt_c_h` (2.0 °C/h).
  8. `hours_needed = diff_unten / max(heizrate_unten, 0.1)` (+ Mittagstief).
  9. **PV-Faktor** multipliziert den Puffer (Prognose heute × `fc_ratio`):
     ≥ 3000 Wh/m² → ×2.0; ≥ 1500 → ×1.5; ≤ 500 → ×0.5; sonst ×1.0.
  10. **Mittagstief** (`_mittagstief_stunden`): gelernte Surplus-schwache
      Stunden (< 250 W) zwischen jetzt und Zielzeit zählen nur **75 %** als
      Heizzeit → `hours_needed` vergrößert sich, Start verfrüht sich.
  11. `buffer_hours = time_left - hours_needed`;
      `effektiver_puffer = buffer_hours * pv_faktor`.
  12. **Entscheidung:**
      - `buffer_hours < 0` → `EIN` („ZU SPÄT! Zeitablauf … (Notfall)") –
        garantiert die Zapf-Versorgung, notfalls ohne PV.
      - Quelle vorhanden **und** `effektiver_puffer < 0.5 h` → `EIN`.
      - `buffer_hours <= spaetstart_puffer_h` (0.5) → `EIN`
        („SPAETEST-START … Zapf-Garantie; Quelle: …") – ebenfalls auch ohne
        akzeptierte Energiequelle.
      - Sonst → `None` („Puffer reicht, warte auf PV/Batterie").
- **Konfig:** `calculated_start.{aktiv (true), prioritaet (82),
  solltemperatur_c (44), target_uhr (17), heizrate_unten_c_h (3.0),
  heizrate_gesamt_c_h (2.0), tmax_c (48)}`.
- **Lernintegration:** nutzt `gelernte_zielzeit`, gelernte Heizraten,
  `fc_ratio` (Forecast-Kalibrierung) und das Surplus-Profil.

---

### 5.6 PV_unten (Prio 78) & 5.7 PV_mitte (Prio 78) – PV-Backup (ohne Forecast)

- **Zweck:** Heizen bei echter PV-Einspeisung über einer Schwelle – unten ab
  **500 W**, mittig ab **700 W**. Nutzt den
  „gratis"-Strom zum Aufheizen bis 48 °C.
- **Backup-Rolle (PV-Exklusivität):** `PV_unten`/`PV_mitte` (beide **Prio 78**)
  sind reines **Backup bei fehlendem Forecast**. Sobald Forecast-Daten
  vorhanden sind (`forecast_wh_qm != None`) und `adaptive_pv.exklusiv_mit_forecast`
  (true) gesetzt ist, steuert ausschliesslich **AdaptivePV**; die statischen
  PV-Regeln bleiben stumm (Grund: „PV-Exklusivitaet: Forecast vorhanden,
  AdaptivePV steuert").
- **Eingänge:** `pv_leistung`, `temp_dict[temperaturfuehler]`, `kompressor_ein`,
  `now_hour`.
- **Logik** (`evaluate_pv_regel`, für jede konfigurierte PV-Regel):
  1. Nachtsperre → inaktiv.
  2. Sensor fehlt → inaktiv.
  3. `temp >= ausschalten_bei_c` (48) → `AUS` (erst prüfen!).
  4. **Weiterlauf (PV-Shaping):** Kompressor läuft **und**
     `pv >= weiterlaufen_ab_pv_watt` (50 W) → `EIN` – so wird von der
     Einschaltschwelle bis zum Ausschaltpunkt durchgeheizt
     (z. B. 42 → 48 °C), auch durch die Hysterese-Zone.
  5. **Neustart:** `pv >= pv_schwelle_watt` **und** `temp <= einschalten_bei_c`
     (42) → `EIN`.
  6. Hysterese (`einschalten_bei_c < temp < ausschalten_bei_c`) → `None`.
  7. Sonst → `None` („Keine Bedingung erfüllt").
- **Verhalten PV_unten vs. PV_mitte:** Identische Logik, nur PV-Schwelle
  (500/700 W) und Fühler unterscheiden sich. Bei Gleichzeitigkeit gewinnt
  die zuerst konfigurierte Regel (gleiche Prio 78).
- **Konfig:** `pv_regeln[]` (Liste!) – Default-Lastdaten in
  `wp_steuerung_parameter.json`:
  - `PV_unten`: `{prioritaet: 78, pv_schwelle_watt: 500,
    temperaturfuehler: "unten", einschalten_bei_c: 42, ausschalten_bei_c: 48,
    weiterlaufen_ab_pv_watt: 50}`
  - `PV_mitte`: `{prioritaet: 78, pv_schwelle_watt: 700,
    temperaturfuehler: "mitte", …}`

---

### 5.8 Batterie (Prio 75) – Heizen mit Hausbatterie

- **Zweck:** Heizen, wenn die Hausbatterie voll genug ist und kein nennenswerter
  Netzbezug stattfindet (Priorität: PV direkt > Batterie > Netz).
- **Eingänge:** `soc`, `feedin_watt`, `temp_dict[temperaturfuehler]`,
  `kompressor_ein`, `forecast_wh_qm` (für dynamische Reserve), `now_hour`.
- **Logik** (`evaluate_batterie`):
  1. Config inaktiv → inaktiv.
  2. `soc` fehlt → inaktiv.
  3. Nachtsperre → inaktiv („kein Batterie-Heizen").
  4. Sensor fehlt → inaktiv.
  5. `temp >= ausschalten_bei_c` (47) → `AUS`.
  6. **Dynamische Reserve (Punkt C):** Wenn die **morgen**-Prognose ≥ 2000 Wh/m²,
     darf die Batterie tiefer entladen werden:
     `eff_min_soc = min_soc_prozent − min(entlastung_max_prozent (15),
     min_soc − min_soc_absolut (10))`.
  7. **SOC-Hysterese:** Bei laufendem Kompressor genügt
     `eff_min_soc − soc_hysterese_prozent` (2), damit ein 1 %-SOC-Ticken den
     Lauf nicht abbricht.
  8. `strom_ok = soc >= soc_schwelle and feedin_watt >= max_netzbezug_watt (−50)`.
  9. **Weiterlauf:** Kompressor läuft, `strom_ok` und `temp > einschalten_bei_c`
     (42) → `EIN`.
  10. **Neustart:** `strom_ok` und `temp <= einschalten_bei_c` → `EIN`.
  11. Sonst → `None`; die `grund`-Meldung nennt den Grund (SOC zu niedrig /
      Netzbezug zu hoch / in Hysterese).
- **Konfig:** `batterie.{aktiv (true), prioritaet (75),
  temperaturfuehler (unten), einschalten_bei_c (42), ausschalten_bei_c (47),
  min_soc_prozent (90), max_netzbezug_watt (−50), soc_hysterese_prozent (2),
  entlastung_max_prozent (15), min_soc_absolut (10)}`.

---

### 5.9 MindestTemp-* (Prio 65) – Mindest-Temperatur-Garantien

- **Zweck:** Komfort-Garantien je Fühler + Zeitfenster. Der Boiler darf zu
  definierten Zeiten nicht zu kalt sein (z. B. oben mittags ≥ 40 °C, mitte am
  Abend ≥ 42 °C zum Duschen). Liefert **einen Regel-Ergebnis pro Eintrag**
  (`evaluate_mindesttemp` → Liste).
- **Eingänge:** `temp_dict`, `now_hour`, gelernte Fenster
  (`learned_evening_window` / `learned_morning_window`), `nachtsperre_*`.
- **Logik pro Eintrag:**
  1. **Lernfenster** (`fenster_aus_lernen=true`): gelerntes Fenster wird
     übernommen, aber **geklemmt**: max. 2 h früherer Start, max. 1 h späteres
     Ende relativ zur Konfiguration; mind. 1 h Dauer. Morgen-Fenster für
     Einträge mit `start_uhr < 12`, sonst Abend-Fenster.
  2. Außerhalb des Fensters → inaktiv.
  3. Nachtsperre aktiv und `nachtsperre_ueberschreiben=false` → inaktiv
     (Garantie gilt nur BIS zum Sperren-Beginn – nachts kein Nachheizen).
  4. Sensor fehlt → inaktiv.
  5. **EIN** wenn `temp < min_temp_c` – **auch während der Nachtsperre**
     (wenn `nachtsperre_ueberschreiben=true`). Grund markiert das mit
     „[Nachtsperre ueberschrieben]".
  6. Sonst → `None` (Garantie erfüllt bei `temp >= min + hysterese_k` oder in
     Hysterese).
- **Wichtig (additiv):** Die Regel kann nur EINSCHALTEN, aber nie blockieren –
  ein explizites AUS würde mit der hohen Priorität andere Heizwünsche
  (PV/Abweichung/CalcStart) ungewollt abschneiden. Die Abschaltung nach
  einem Garantie-EINSCHALTEN übernimmt der normale Setpoint
  (`min_temp_c + hysterese_k` via `_extract_ausschaltpunkt`).
- **Aktuelle Einträge (JSON):**
  - `Mittag-Oben`: oben, `min_temp_c=40`, `start_uhr=11`, `ende_uhr=16`,
    `hysterese_k=2.0`, kein Lernen, Nachtsperre überschreiben=true.
  - `Abend-Mitte`: mitte, `min_temp_c=42`, `start_uhr=17`, `ende_uhr=22`,
    `hysterese_k=2.0`, `fenster_aus_lernen=true`,
    `nachtsperre_ueberschreiben=false` (Abend-Garantie endet mit dem
    Sperren-Beginn: nach 19 Uhr kein Nachheizen von der Garantie).
- **Konfig:** `mindest_temp.{eintraege[]}` mit Feldern `name`,
  `temperaturfuehler`, `min_temp_c`, `start_uhr`, `ende_uhr`,
  `hysterese_k`, `fenster_aus_lernen`, `nachtsperre_ueberschreiben`.

---

### 5.10 Komfort (Prio 60) – PV-Komfort (ohne Notfall)

- **Zweck:** Hält zusätzliche Wärme, **sofern genug PV verfügbar** ist. Der
  reine Notfall-Schutz (≤36 °C, auch nachts) ist in die eigenständige Regel
  **Notfallschutz** (Prio 110) ausgekoppelt – Komfort regelt nur noch den
  PV-abhängigen Komfort.
- **Eingänge:** `temp_dict` (unten), `pv_leistung`, `now_hour`.
- **Logik** (`evaluate_komfort`), Prüf-Reihenfolge:
  1. `temp_unten` fehlt → inaktiv.
  2. Nachtsperre → inaktiv („kein Komfort-Heizen").
  3. `temp >= ausschalten_bei_c` (42) → `AUS`.
  4. **Komfort-EIN:** `pv >= min_pv_fuer_komfort_watt` (50 W) **und**
     `temp <= komfort_einschalten_bei_c` (38) → `EIN`.
  5. Sonst → `einschalten=None` (keine Bedingung erfüllt).
- **Konfig:** `komfort.{prioritaet (60),
  komfort_einschalten_bei_c (38), ausschalten_bei_c (42),
  min_pv_fuer_komfort_watt (50)}`.

---

### 5.11 Forecast (Prio 57) – Vorheizen/Sparen nach Prognose

- **Zweck:** „Morgen schlecht → heute vorheizen; morgen gut → heute sparen".
  Dient der Nutzung guter Sonnentage und der Absicherung vor schlechten.
  **Quellenblinde** Regeln (Priorität vor PV-Regeln) dürfen nicht einfach mit
  Netzstrom heizen.
- **Eingänge:** `forecast_wh_qm` (morgen), `feedin_watt`, `soc`,
  `temp_dict[temperaturfuehler]` (Fallback `oben`), `now_hour`.
- **Logik** (`evaluate_forecast`):
  1. Config inaktiv → inaktiv.
  2. Keine Prognose verfügbar → inaktiv.
  3. Nachtsperre → inaktiv.
  4. Kein Sensorwert → inaktiv.
  5. **VORHEIZEN** (`forecast <= fc_schwelle_niedrig_wh` (800) und innerhalb
     `vorheiz_start_uhr..vorheiz_ende_uhr` (8–19 Uhr)):
     - `temp <= t_vorheiz_ab_c` (44) → Quellen-Check `_forecast_quelle_ok`:
       PV direkt ≥ 50 W oder Batterie voll (SOC ≥ 90 %, kein Netzkauf). Netz
       nur, wenn `vorheiz_netz_erlaubt=true`.
       - Quelle ok → `EIN`
       - Quelle nicht ok → `einschalten=None` („wartet auf Quelle" – bewusst
         STUMM, damit niedriger priorisierte Regeln wie Abweichung entscheiden
         dürfen).
     - `temp > t_vorheiz_ab_c` → `einschalten=None`.
  6. **SPAREN** (`forecast >= fc_schwelle_hoch_wh` (3000) und innerhalb
     `sparen_start_uhr..sparen_ende_uhr` (11–15 Uhr), `temp >= t_vorheiz_ab_c`)
     → `einschalten=False` (AUS/Sparen).
  7. Sonst → `einschalten=None`.
- **Konfig:** `forecast.{aktiv (true), prioritaet (57),
  fc_schwelle_hoch_wh (3000), fc_schwelle_niedrig_wh (800),
  t_vorheiz_ab_c (44), tmax_c (48), vorheiz_start_uhr (8),
  vorheiz_ende_uhr (19), sparen_start_uhr (11), sparen_ende_uhr (15),
  temperaturfuehler (unten)}`.

---

### 5.12 AdaptivePV (Prio 78) – Exklusive PV-Schwelle

- **Zweck:** **PV-Exklusivität** – sobald Forecast-Daten vorliegen, ist
  AdaptivePV die **einzige** PV-Heizregel (die statischen `PV_unten`/`PV_mitte`
  sind reines Backup bei fehlendem Forecast). Passt die benötigte
  PV-Einspeisung **dynamisch** an Boiler-Temperatur und Tagesprognose an:
  Je kälter der Boiler, desto niedriger die Schwelle
  (aggressiver heizen). Je besser die Prognose, desto höher die Schwelle
  (konservativer warten).
- **Exklusiv-Schalter:** `adaptive_pv.exklusiv_mit_forecast` (Standard `true`):
  nur wenn dieser Schalter aktiv ist, werden die statischen PV-Regeln bei
  vorhandenem Forecast stummgeschaltet.
- **Eingänge:** `pv_leistung`, `temp_dict[temperaturfuehler]` (unten),
  `forecast_wh_qm` (heute/morgen, je nach Aufruf), `kompressor_ein`, `now_hour`,
  `fc_ratio` (Kalibrierung).
- **Logik** (`evaluate_adaptive_pv`, Schwelle = `base_threshold_watt`):
  1. Config inaktiv → inaktiv.
  2. Nachtsperre → inaktiv.
  3. Sensor fehlt → inaktiv.
  4. `temp >= tmax_c` (48) → `AUS`.
  5. **Einschalt-Hysterese (Default):** `temp >= einschalten_bis_c`
     (= `tmax_c − 3.0`) und Kompressor aus → `einschalten=None`
     (PV-Regel mit eigener Hysterese soll entscheiden).
  6. **Temperatur-Anpassung:** `temp < t_aggressiv_kalt_c` (35) → Schwelle × 0.5;
     `temp < t_normal_kalt_c` (38) → Schwelle × 0.7.
  7. **Prognose-Anpassung** (`prognose_eff = forecast × fc_ratio`):
     ≥ `fc_schwelle_gut_wh` (4000) → Schwelle × 1.5; ≤ `fc_schwelle_schlecht_wh`
     (1000) → Schwelle × 0.5.
  8. `pv_leistung >= schwelle` → `EIN`; sonst `None`.
- **Konfig:** `adaptive_pv.{aktiv (true), prioritaet (78),
  exklusiv_mit_forecast (true), base_threshold_watt (300),
  temperaturfuehler (unten), tmax_c (48), t_aggressiv_kalt_c (35),
  t_normal_kalt_c (38), fc_schwelle_gut_wh (4000),
  fc_schwelle_schlecht_wh (1000)}`.
  `einschalten_bis_c` ist optional; Default: `tmax_c − 3.0`.

---

### 5.13 Zeitfenster (Prio 53) – Festes Heizzeitfenster

- **Zweck:** Heizen in einem fest konfigurierten Zeitfenster, wenn genug PV und
  die Temperatur unter der Grenze liegt. Aktuell **deaktiviert** in der
  Default-Konfiguration (Standard-Reihenfolge der Engine bewertet sie trotzdem).
- **Eingänge:** `now_hour`, `pv_leistung`, `temp_dict[temperaturfuehler]`.
- **Logik** (`evaluate_zeitfenster`):
  1. Config inaktiv → inaktiv.
  2. Außerhalb des Fensters (`start_uhr..ende_uhr`) → inaktiv.
  3. `min_pv_watt > 0` und `pv < min_pv_watt` → `einschalten=None`
     („Zeitfenster aktiv, aber PV zu wenig").
  4. Sensor fehlt → `einschalten=None`.
  5. `temp <= max_temp_fuer_einschalten_c` → `EIN`; sonst `AUS`.
- **Konfig:** `zeitfenster.{aktiv (false in JSON), prioritaet (53),
  start_uhr (16), ende_uhr (17), modus ("einschalten"),
  temperaturfuehler (mitte), max_temp_fuer_einschalten_c (44.5),
  min_pv_watt (410)}`.

---

### 5.14 Abweichung (Prio 47) – Sollwertregler mit Hysterese

- **Zweck:** Hält die Boiler-Temperatur (Fühler unten) nahe am Sollwert
  (`solltemperatur_c`). Es ist die **Basis-Regel**, wenn keine PV-/Batterie-/
  Prognose-Regel greift – mit Tiefschutz und Schichtungs-Check.
- **Eingänge:** `temp_dict[temperaturfuehler]` (unten), `kompressor_ein`,
  `now_hour`, `feedin_watt`, `soc`, `bademodus_aktiv`.
- **Logik** (`evaluate_abweichung`), `abweichung = soll − ist`:
  1. Sensor fehlt → inaktiv.
  2. **AUS:** `abweichung <= ausschalten_bei_abweichung_k` (0.7) → `AUS`
     (Ziel erreicht/überschritten).
  3. Nachtsperre **und nicht** Bademodus → inaktiv (kein Einschalten).
  4. **EIN** wenn `abweichung >= einschalten_bei_abweichung_k` (4.9):
     - **Schichtungs-Check:** Fühler ≠ `oben` und `oben >= schichtung_min_oben_c`
       (42) → `einschalten=None` (kein Netz-Start, wenn oben noch warm –
       vermeidet unnötiges Heizen nach Zapfen).
     - **Quellen-Gate** (`quelle_warten=true`, Default):
       `temp > soll − netz_notfall_offset_k` (44 − 8 = 36) und keine PV/Batterie-
       Quelle → `einschalten=None` („wartet auf PV/Batterie (Netz erst unter
       36 °C)"). Erst unter der Tiefschutz-Grenze darf mit **Netzstrom** geheizt
       werden.
     - Sonst → `EIN`.
  5. In der Hysterese (zwischen `ausschalten_bei` und `einschalten_bei`) →
     `einschalten=None`.
- **Konfig:** `abweichung.{prioritaet (47), solltemperatur_c (44),
  temperaturfuehler (unten), einschalten_bei_abweichung_k (4.9),
  ausschalten_bei_abweichung_k (0.7), schichtung_min_oben_c (42)}`.
  Zusätzlich (Defaults aus `json_config.py`): `quelle_warten=true`,
  `netz_notfall_offset_k=8.0`, `pv_einspeisung_min_watt=50`,
  `soc_min_prozent=90`, `max_netzbezug_watt=-50`.
- **Besonderheit:** Der **Bademodus** hebt nicht nur den Sollwert (Kap. 2.2),
  sondern lässt die Abweichungs-Regel auch **während der Nachtsperre**
  einschalten.

---

## 6. Manuelle Overrides & Legacy

### 6.1 API-Befehle (`/control` in `api.py`)

| Befehl | Wirkung | Beispiele |
|---|---|---|
| `force_on` | Kompressor **sofort** einschalten (Hardware `set_compressor_state(True, force=True)`) | `{"command": "force_on"}` |
| `force_off` | Kompressor **sofort** ausschalten | `{"command": "force_off"}` |
| `set_mode` | Bedienmodus setzen/ändern | `{"command":"set_mode","params":{"mode":"bademodus","active":true}}` |

Erlaubte Modusnamen: `bademodus`, `urlaubsmodus` (`ALLOWED_MODES`). Andere Werte
werden mit `400 Bad Request` abgelehnt. Die API-Clients (Webapp z. B.) rufen
`toggleMode()` für die Schalter auf.

### 6.2 Hardware-Steuerungstyp

- `wp.typ = "binaer_ein_aus"`: Die WP wird binär (Relais) ein-/ausgeschaltet –
  keine Modulierung. `hardware.py` steuert GPIO (Relais für Kompressor,
  Eingang für Druckschalter); `hardware_mock.py` ersetzt GPIO in Entwicklung.

### 6.3 Legacy / archivierte Modi

Die frühere modusbasierte Logik (`archive/legacy_control_logic.py`,
`archive/legacy_main.py`) ist **archiviert und wird nicht mehr importiert**.
Übrig gebliebene Helfer in `logic_utils.py` dienen nur noch der
Status-Anzeige/Abwärtskompatibilität:

- `is_solar_window(...)`: Prüft, ob man im „Solarfenster" nach der
  Nachtabsenkung ist (berechnet aus `NACHTABSENKUNG_END` + `SOLAR_WINDOW_HOURS`).
- `ist_uebergangsmodus_aktiv(...)`: Prüft die alten Übergangszeiten
  (`UEBERGANGSMODUS_MORGENS_ENDE` / `UEBERGANGSMODUS_ABENDS_START`).

Diese Legacymodi steuern **nichts mehr** – Entscheidungen trifft ausschließlich
die Prioritäten-Engine.

---

## 7. Kurzübersicht

### 7.1 Bedienmodi (Kap. 2)

| Modus | Aktivierung | Parameterverschiebung |
|---|---|---|
| Normalmodus | default | – |
| Bademodus 🛁 | Benutzer | Soll +3 K, Nachtsperre erlaubt, setzt Sommer-Offset aus |
| Urlaubsmodus 🌴 | Benutzer | Soll −15 K |
| Sommer-Modus ☀️ | automatisch (3 gute Tage) | Soll −3 K, PV-Ausschalt-/Einschaltpunkte −2 K |

### 7.2 Regeln & Prioritäten (Kap. 5)

| Prio | Regel | Kernwirkung |
|---|---|---|
| 110 | Notfallschutz | Reiner Schutzleiter: ≤36 °C heizt vor allen Sperren |
| 100 | Wochenende | blockiert vor 9 Uhr am Sa/So |
| 90 | Legionellen | 60 °C 1×/Woche, 30 min Probezeit; Wochenend-Nachholung |
| 85 | Einspeisung | Heizen ab 7500 W Einspeisung |
| 82 | CalcStart | Startrechnung zur Zielzeit (PV-Faktor, Mittagstief) |
| 78 | AdaptivePV | Exklusive PV-Schwelle (300 W Basis), sobald Forecast da |
| 78 | PV_unten/PV_mitte | PV-Backup bei fehlendem Forecast (500/700 W) |
| 75 | Batterie | Heizen aus Batterie (SOC ≥ 90, kein Netzbezug) |
| 65 | MindestTemp-* | Fenster-Garantien (oben mittags ≥ 40, mitte abends ≥ 42) |
| 60 | Komfort | PV-Komfort ≤ 38 °C mit PV (AUS 42 °C); Notfall liegt bei Prio 110 |
| 57 | Forecast | Vorheizen bei schlechter, Sparen bei guter Prognose |
| 53 | Zeitfenster | Festes Fenster (aktuell inaktiv) |
| 47 | Abweichung | Soll 44 °C ± 4,9/0,7 K Hysterese |

### 7.3 Die wichtigsten Querverweise

- `REGELWERK.md` – Regelsicht auf die Gesamtlogik (inkl. Zielbild).
- `STEUERUNGSMODI.md` (diese Datei) – Modus- und Engine-Sicht.
- `wp_steuerung_parameter.json` – alle Regelparameter (JSON-Config).
- `config.ini.example` – INI-Basiskonfiguration (Urlaubsmodus, Sensoren, Solax).
- `tests/` – Unit-/Integrationstests der einzelnen Regeln.
