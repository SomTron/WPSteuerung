# Regelwerk der WP-Steuerung

Diese Datei beschreibt die komplette Entscheidungslogik der Warmwasser-Wärmepumpe.
Die Steuerung ist eine **prioritätenbasierte Regel-Engine**: Jeder Zyklus bewertet
alle Regeln (`bewerte_alle_regeln` in `priority_control.py`), und die Regel mit der
**höchsten Priorität, die eine klare Entscheidung (EIN/AUS) liefert, gewinnt**.

---

## 1. Betriebsweise (Zielbild)

| Ziel | Umsetzung |
|---|---|
| **Nie wärmer als 48 °C** | `ausschalten_bei_c` / `tmax_c = 48` in allen Heiz-Regeln + Sicherheitsabschaltung (`sicherheit.max_temp_c`) |
| **Nachts so wenig wie möglich laufen** | Nachtsperre 19–8 Uhr: keine Einschaltungen außer expliziten Garantien |
| **Strom-Priorität: PV direkt > Batterie > Netz** | PV-Regeln + AdaptivePV reagieren auf echte Netzeinspeisung; Batterie-Regel nutzt nur SOC-Überschuss ohne Netzkauf; sonst läuft nichts |
| **Boiler als PV-Buffer** | Bei Überschuss heizen bis 48 °C; bei schlechter Prognose morgen → heute vorheizen; bei guter Prognose → heute sparen |
| **Dauer-Sonne → Buffer nicht voll laden** | Sommermodus senkt nach 3 guten Tagen den Abweichungs-Soll (−3 K) und die PV-Abschaltpunkte (−2 K → 46 °C statt 48 °C) |
| **Komfort-Garantien** | Oben mittags ≥ 40 °C, Mitte am Abend ≥ 40 °C (Duschen) – auch gegen die Nachtsperre |

---

## 2. Prioritäten-Übersicht

| Prio | Regel | Zweck | Quelle |
|---|---|---|---|
| 100 | Wochenende | Wochenend-Vorheizen ab `fruehestens_uhr` | `wochenende` |
| 83 | **Einspeisung** | PV-Shaping am Netzlimit (7500 W) – gratis Strom nutzen | `einspeisung` |
| 82 | CalcStart | Berechneter Start für gelernte Zapf-Zeit (17:00) | `calculated_start` |
| 81 | PV_unten | Heizen bei echter Netzeinspeisung ≥ 500 W (Fühler unten) | `pv_regeln[]` |
| 80 | PV_mitte | wie oben, Schwelle 700 W (Fühler mittig) | `pv_regeln[]` |
| 75 | **Batterie** | Heizen aus voller Hausbatterie ohne Netzbezug | `batterie` |
| 65 | **MinTemp-\*** | Mindest-Temperatur-Garantien pro Fühler+Zeitfenster | `mindest_temp` |
| 60 | Komfort | Notfall ≤ 36 °C (immer), Komfort ≤ 38 °C mit etwas PV | `komfort` |
| 57 | Forecast | Morgen schlecht → heute vorheizen; morgen gut → sparen | `forecast` |
| 55 | AdaptivePV | Dynamische PV-Schwelle je nach Boiler-Temp & Tagesprognose | `adaptive_pv` |
| 53 | Zeitfenster | Zeitfenster 6–16 Uhr mit min. 410 W PV | `zeitfenster` |
| 47 | Abweichung | Solltemperatur ± Hysterese am unteren Fühler | `abweichung` |

Dazu immer aktiv (keine Priorität nötig):

- **Nachtsperre** (19–8): blockt alle *Einschaltungen*; Ausnahmen siehe unten
- **Sicherheit**: Überhitzung/Notfall greift vor allem anderen

---

## 3. Die Regeln im Detail

### 3.1 Einspeisung (PV-Shaping am Netzlimit) — Prio 83

> Vorgabe: Es darf nicht mehr als ~7500 W eingespeist werden. Genau dieser
> Zeitraum ist der ideale Heizzeitraum – der Strom wäre sonst gedrosselt worden.

- **EIN**, sobald `feedinpower ≥ einspeisegrenze_watt` (7500 W) und der Regelfühler
  unter `ausschalten_bei_c` (48 °C) liegt
- **Weiterlauf**, solange `feedinpower ≥ weiterlauf_ab_watt` (6500 W): Die WP zieht
  selbst ~600 W, die Einspeisung bricht beim Start also um diesen Betrag ein – der
  Abschlag verhindert Flattern
- **AUS** bei Fühler ≥ 48 °C
- Respektiert die Nachtsperre (nachts gibt es keine PV-Einspeisung)

Konfiguration: `einspeisung.{einspeisegrenze_watt, weiterlauf_ab_watt,
ausschalten_bei_c, temperaturfuehler}`

### 3.2 CalcStart — Prio 82

Berechnet selbstständig, wann der Kompressor spätestens starten muss, damit zur
gelernten Zapf-Zeit genug warmes Wasser bereitsteht:

- Ziel: `solltemperatur_c` (44 °C) am unteren Fühler zur **gelernten** Zielzeit
  (default 17:00, siehe Kapitel 5 „Selbstlernen")
- Benötigte Vorlaufzeit = Temperaturdifferenz ÷ saisonal **gelernter Heizrate**
  (+ Sicherheitspuffer); bei sehr sonnigem HEUTE (`forecast_today_wh_qm`) wird der
  Puffer verdoppelt – dann wartet die Regel lieber auf kostenlosen PV-Strom
- Saisonaler Puffer schützt vor Kaltwasserschauern im Winter
- Konfigurierbarer Konflikt mit der Nachtsperre wird erkannt und geloggt

### 3.3 PV-Regeln — Prio 81/80

Klassisches PV-Shaping über die **echte Netzeinspeisung** (`feedinpower`):
Solange Hausverbrauch und Batterieladung den Solarstrom schlucken, feuern sie
nicht – genau die gewünschte Reihenfolge PV-Haus → PV-Batterie → PV-WP.

- EIN bei Einspeisung ≥ Schwelle (500/700 W) und Fühler kalt (≤ 42 °C)
- Weiterlauf bis 48 °C, solange weiterlaufen_ab_pv_watt (50 W) erreicht bleibt
- Im **Sommermodus** werden ihre Abschaltpunkte abgesenkt (siehe 4.2)

### 3.4 Batterie-Regel — Prio 75

> Vorgabe: erst PV direkt, dann Batterie, erst ganz zum Schluss Netzstrom.

Heizen mit Hausbatterie-Strom, **ohne dass das Haus Netzstrom kauft**:

- **EIN** wenn alle drei Bedingungen erfüllt sind:
  1. `soc ≥ min_soc_prozent` (90 %) – Batterie deutlich gefüllt
  2. `feedinpower ≥ max_netzbezug_watt` (−50 W) – kein nennenswerter Netzbezug
     (kleiner Negativ-Puffer toleriert Messrauschen)
  3. Fühler ≤ `einschalten_bei_c` (42 °C)
- **Weiterlauf** aus der Batterie bis `ausschalten_bei_c` (47 °C), solange 1.+2. halten
- Sinkt der SOC darunter oder bezieht das Haus Netz → keine Aktion (Schonung)
- Respektiert die Nachtsperre (nachts so wenig wie möglich)

Konfiguration: `batterie.{min_soc_prozent, max_netzbezug_watt, einschalten_bei_c,
ausschalten_bei_c, temperaturfuehler}`

### 3.5 Mindest-Temperatur-Garantien (MinTemp-*) — Prio 65

> Vorgabe: Die obere Temperatur soll mittags nicht unter 40 °C fallen; die mittlere
> Temperatur soll am Abend (Duschen) nicht unter 40 °C fallen.

Pro Eintrag (`mindest_temp.eintraege[]`) gilt:

| Eintrag | Fühler | Garantie | Fenster (default) |
|---|---|---|---|
| Mittag-Oben | oben | ≥ 40 °C | 11–16 Uhr |
| Abend-Mitte | mittig | ≥ 40 °C | 17–22 Uhr *(lernend)* |

- **EIN**, wenn der Fühler unter der Mindesttemperatur liegt – **auch während der
  Nachtsperre**. Das ist der Sinn der Regel: Sie garantiert Komfort zu den Zeiten,
  in denen er gebraucht wird.
- Zwischen `min_temp_c` und `min_temp_c + hysterese_k` (Hysterese 2 K) tritt die
  Regel stumm zurück.
- **Rein additiv:** Die Regel kann nur *einschalten*, niemals blockieren. Ist die
  Garantie erfüllt, liefert sie keine Aktion – so schneidet sie Heizwünsche anderer
  Regeln (PV/CalcStart/Abweichung) nie weg. Die Abschaltung nach einem Garantie-
  Lauf übernimmt der normale Ausschaltpunkt (`min_temp_c + hysterese_k`).
- **Lernend:** Einträge mit `"fenster_aus_lernen": true` verschieben ihr Zeitfenster
  anhand des gelernten Zapfverhaltens (Kapitel 5.3). Klemmung: maximal 2 h früherer
  Start und 1 h späteres Ende gegenüber der Konfiguration.

Konfiguration: `mindest_temp.{aktiv, prioritaet, eintraege[].{name, temperaturfuehler,
min_temp_c, start_uhr, ende_uhr, hysterese_k, fenster_aus_lernen}}`

### 3.6 Komfort — Prio 60

- **Notfall:** oben ≤ 36 °C → EIN, **auch in der Nachtsperre** (letzte Komfort-Linie)
- **Komfort:** unten ≤ 38 °C UND etwas PV (≥ 50 W) → EIN

### 3.7 Forecast — Prio 57

Nimmt die Wh/m²-Prognose von MORGEN (Richtung bewusst so gewählt):

- **Morgen schlecht** (≤ 800 Wh/m²): heute bis 17 Uhr vorheizen (bis 44 °C), solange
  der Fühler kälter als 44 °C ist – der Buffer wird aufgebaut, solange Strom da ist
- **Morgen gut** (≥ 3000 Wh/m²): zwischen 11–15 Uhr nicht unnötig heizen – morgen
  kommt wieder PV-Strom

### 3.8 AdaptivePV — Prio 55

Dynamische Einspeise-Schwelle statt fester Werte (wirkt auch ohne konfigurierte
`pv_regeln`, z. B. Basis 300 W):

- Boiler kalt (unten < 35 °C) → aggressiver (Schwelle × 0.7)
- Boiler warm (unten > 38 °C) → konservativer (× 1.5), bis `tmax_c`
- Sehr sonniger HEUTE-Tag → Schwelle × 2 (warten lohnt sich)
- Schlechter Tag (< 1000 Wh/m²) → Schwelle × 0.5 (jede Gelegenheit nutzen)
- Im **Sommermodus** wird `tmax_c` abgesenkt (48 → 46 °C)

### 3.9 Zeitfenster — Prio 53

6–16 Uhr mit mindestens 410 W PV-Einspeisung heizen, solange der mittlere Fühler
unter 50 °C bleibt. Klassisches „nutz den Tag"-Backup.

### 3.10 Abweichung — Prio 47

Grundregel: unterer Fühler vs. Solltemperatur (40 °C).

- EIN, wenn `soll − temp ≥ einschalten_bei_abweichung_k` (+3 K)
- AUS, wenn `soll − temp ≤ ausschalten_bei_abweichung_k` (+0.5 K)
- **Schichtungsschutz:** Wenn oben schon ≥ 42 °C ist, aber unten kalt, wird NICHT
  eingeschaltet (vermeidet sinnlose Netzstrom-Läufe bei geschichtetem Boiler)
- Bademodus erhöht den Soll (+3 K), Urlaubsmodus senkt ihn (−5 K)

---

## 4. Übergreifende Mechanismen

### 4.1 Nachtsperre (19–8 Uhr)

Blockiert alle *Einschaltpunkte*. Bewusste Ausnahmen (Garantien):

1. Komfort-Notfall (oben ≤ 36 °C) — Prio 60
2. Mindest-Temp-Garantien innerhalb ihrer Fenster (z. B. Abend-Mitte bis 22 Uhr) — Prio 65

Laufende Heizzyklen werden nicht hart unterbrochen; die Abschaltlogik arbeitet
weiter mit dem Ausschaltpunkt der Gewinner-Regel.

### 4.2 Sommermodus (Dauer-Sonne)

Zählt aufeinanderfolgende Tage, an denen heute+morgen+übermorgen jeweils ≥
2000 Wh/m² prognostiziert sind (`logic_utils.evaluate_sommer_modus`, max. eine
Bewertung pro Kalendertag, Rücksetzung bei jeder schlechten Prognose). Nach
3 Tagen aktiviert er zwei Absenkungen:

| Wirkung | Offset | Ziel |
|---|---|---|
| Abweichungs-Soll | −3 K (40 → 37 °C) | Boiler nicht jeden Tag voll heizen |
| PV-Abschaltpunkte & AdaptivePV-tmax | −2 K (48 → 46 °C) | **Buffer wird bei Dauer-Sonne bewusst nicht voll geladen – Max-Temperatur minimiert** |

Klemmung: Der Abschaltpunkt bleibt mindestens 2 K über dem Einschaltpunkt.
Die Mindestgarantien (3.5) bleiben unberührt – Komfort geht vor.

### 4.3 Bademodus / Urlaubsmodus

Werden als Solltemperatur-Offsets auf die effektive Config angewendet, bevor die
Regeln laufen; alle Setpoints in der Statusanzeige zeigen die effektiven Werte.

---

## 5. Selbstlernen (`learning_engine.py`, Persistenz: `learning_data.json`)

### 5.1 Saisonal gelernte Heizrate

Jeder abgeschlossene Heizzyklus (>5 min) liefert °C/h am unteren Fühler; gleitender
Mittelwert getrennt nach Winter / Übergang / Sommer (ab 3 Zyklen aktiv).
CalcStart nutzt die Rate für die Vorlaufzeit-Berechnung – die Steuerung lernt also,
*wie schnell* ihr Boiler in der jeweiligen Jahreszeit ist.

### 5.2 Gelernte Zapf-Zeit (Zielzeit)

Erkannte Warmwasser-Zapfungen (Temperaturabfall > 1.5 K am oberen Fühler,
16–23 Uhr) aktualisieren die durchschnittliche erste Abend-Zapfzeit
(`learned_target_hour`). Ab 3 Samples übernimmt **CalcStart** diese Zeit als
Ziel-Uhrzeit – die Steuerung richtet die Vorheizung also automatisch am realen
Duschverhalten aus.

### 5.3 Gelerntes Abend-Fenster (neu)

`get_learned_evening_window()` bestimmt aus allen Zapfungen der letzten 14 Tage
die früheste und späteste Zapfzeit (inkl. 1.5 h Vorlauf / 0.75 h Nachlauf, ab
4 Ereignissen aktiv). MindestTemp-Einträge mit `"fenster_aus_lernen": true`
(das Abend-Fenster ist per Default so konfiguriert) passen ihren Zeitraum
dynamisch an: Duscht der Haushalt z. B. regelmäßig schon ab 18 Uhr, beginnt die
Garantie entsprechend früher – geklemmt auf max. 2 h früher / 1 h später als die
statische Konfiguration.

---

## 6. Datenfluss (Kurzüberblick)

```
Solax-API ─► state.solar (feedinpower/batpower/soc/acpower, forecast_today/tomorrow/day2)
Sensoren  ─► state.sensors (t_oben/t_mittig/t_unten/t_verd)
                │
                ▼
main.check_periodic_tasks ─► evaluate_sommer_modus (Tageszähler)
                │
                ▼
pcl.determine_mode_and_setpoints
    ├─ learning_engine.update()          (Zyklen + Zapfungen lernen)
    ├─ effektive Config                  (Bad/Urlaub/Sommer-Offsets inkl. PV-Cap)
    ├─ bewerte_alle_regeln(...)          (alle Regeln, soc/battery/learned window inkl.)
    └─ Gewinner ─► handle_compressor_on/off (Setpoints, Mindestlaufzeiten)
                │
                ▼
api.py / webapp / telegram      (Modus, aktive Regel, alle Ergebnisse, Lerndaten)
```

---

## 7. Konfigurationsreferenz (Auszug)

Alle Schlüssel liegen in `wp_steuerung_parameter.json` (partiell ladbar – fehlende
Schlüssel nehmen ihre Defaults an, siehe `json_config.py`):

```jsonc
{
  "sicherheit":   { "max_temp_c": 48, "nachtsperre_start": 19, "nachtsperre_ende": 8 },
  "abweichung":   { "solltemperatur_c": 40, "schichtung_min_oben_c": 42 },
  "einspeisung":  { "einspeisegrenze_watt": 7500, "weiterlauf_ab_watt": 6500 },
  "batterie":     { "min_soc_prozent": 90, "max_netzbezug_watt": -50 },
  "mindest_temp": { "eintraege": [ /* Fuehler+Fenster+min_temp_c+fenster_aus_lernen */ ] },
  "sommer_modus": { "temperatur_offset_c": -3.0, "pv_ausschalt_offset_c": -2.0 }
}
```

## 8. Tests

Die Logik ist durch Unit-/Integrationstests abgedeckt (`tests/`), u. a.:

- `test_einspeisung.py` – Netzlimit-Regel: EIN/Weiterlauf/AUS/Nachtsperre/Priorität
- `test_batterie.py` – SOC-/Netzbezug-Gates, Schonung, Vorrang der PV-Regeln
- `test_mindesttemp.py` – Garantien inkl. Nachtsperren-Bypass und Additivität
- `test_zapf_fenster.py` – Lernfenster: Berechnung, Klemmung, Durchreichung
- `test_sommer_pv_cap.py` – Sommer-Absenkung der PV-Ziele ohne Garantie-Verletzung
- `test_calcstart_nachtsperre.py` – Konflikt-Erkennung Zielzeit ↔ Nachtsperre
