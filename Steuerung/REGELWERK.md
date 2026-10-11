# Regelwerk der WP-Steuerung

Diese Datei beschreibt die komplette Entscheidungslogik der Warmwasser-Wärmepumpe.
Die Steuerung ist eine **prioritätenbasierte Regel-Engine**: Jeder Zyklus bewertet
alle Regeln (`bewerte_alle_regeln` in `priority_control.py`), und die Regel mit der
**höchsten Priorität, die eine klare Entscheidung (EIN/AUS) liefert, gewinnt**.

---

## 1. Betriebsweise (Zielbild)

| Ziel | Umsetzung |
|---|---|
| **Nie wärmer als 48 °C (normal) / 65 °C (Legionellen)** | `sicherheit.max_temp_c = 48` im Normalbetrieb; während Legionellenprophylaxe temporär auf `legionellen_max_temp_c = 65` angehoben |
| **Nachts so wenig wie möglich laufen** | Nachtsperre 19–8 Uhr: keine Einschaltungen außer expliziten Garantien |
| **Strom-Priorität: PV direkt > Batterie > Netz** | PV-Regeln + AdaptivePV reagieren auf echte Netzeinspeisung; Batterie-Regel nutzt nur SOC-Überschuss ohne Netzkauf; sonst läuft nichts |
| **Boiler als PV-Buffer** | Bei Überschuss heizen bis 48 °C; bei schlechter Prognose morgen → heute vorheizen; bei guter Prognose → heute sparen |
| **Dauer-Sonne → Buffer nicht voll laden** | Sommermodus senkt nach 3 guten Tagen den Abweichungs-Soll (−3 K) und die PV-Abschaltpunkte (−2 K → 46 °C statt 48 °C) |
| **Komfort-Garantien** | MinTemp-Garantien (Prio 65): **Mitte mittags ≥ 42 °C** (9–16 Uhr) und **Mitte abends ≥ 42 °C** (17–22 Uhr) zum Duschen – Vorheizen bis zum Nachtsperren-Beginn (19 Uhr), danach kein Nacht-Heizen mehr |

---

## 2. Prioritäten-Übersicht

| Prio | Regel | Zweck | Quelle |
|---|---|---|---|
| 110 | **Notfallschutz** | Reiner Schutzleiter: ≤36 °C heizt vor allen Sperren (Wochenende, Nachtsperre); Abschaltung nur am *eigenen* Lauf; **spätestens bis `bis_uhr` (22:00)**, nachts gesperrt; Start nur, wenn die Mindestlaufzeit vorher endet (Ausnahme Legionella) | `notfallschutz` |
| 100 | Wochenende | Wochenend-Vorheizen ab `fruehestens_uhr` (9 Uhr). **Blockt auch den Legionellen-Start** vor 9 Uhr – die Fahrt wird ab 9 Uhr nachgeholt (`test_legionellen_mode.py`) | `wochenende` |
| 90 | Legionellen | Legionellenprophylaxe 1×/Woche (Ziel 60 °C, Limit 65 °C); **temporär hebt Boiler-Limit von 48 auf 65 °C** | `legionellen` |
| 85 | **Einspeisung** | PV-Shaping am Netzlimit (7500 W) – gratis Strom nutzen | `einspeisung` |
| 82 | CalcStart | Berechneter Start für gelernte Zapf-Zeit (17:00) | `calculated_start` |
| 78 | **AdaptivePV** | Exklusive PV-Schwelle, sobald Forecast vorhanden | `adaptive_pv` |
| 78 | PV_unten / PV_mitte | PV-Backup bei fehlendem Forecast (500/700 W). **Derzeit praktisch ruhend**: seit AdaptivePV (Sep. 2026) greift es nur noch, wenn keine Prognose vorliegt | `pv_regeln[]` |
| 75 | **Batterie** | Heizen aus voller Hausbatterie ohne Netzbezug. **Derzeit `aktiv=false`** – den Batterie-Pfad deckt das Quellen-Gate der Abweichungs-Regel ab (SOC ≥ 90 % ohne Netzkauf) | `batterie` |
| 65 | **MinTemp-\*** | Mindest-Temperatur-Garantien pro Fühler+Zeitfenster | `mindest_temp` |
| 60 | Komfort | PV-Komfort ≤ 38 °C mit etwas PV (nur PV); Notfall liegt bei Prio 110. **Derzeit `aktiv=false`** – den Komfort-Boden decken die MinTemp-Garantien (42 °C) | `komfort` |
| 57 | Forecast | Morgen schlecht → heute vorheizen; morgen gut → sparen | `forecast` |
| 53 | Zeitfenster | Zeitfenster 16–17 Uhr mit min. 410 W PV. **Derzeit `aktiv=false`** (von AdaptivePV abgelöst) | `zeitfenster` |
| 47 | Abweichung | Solltemperatur ± Hysterese am unteren Fühler | `abweichung` |

Dazu immer aktiv (keine Priorität nötig):

- **Nachtsperre** (19–8): blockt alle *Einschaltungen*; Ausnahmen siehe unten
- **Sicherheit**: Überhitzung/Notfall greift vor allem anderen

> **Wer liefert das 48-°C-AUS?** `AdaptivePV`, `CalcStart` und `Einspeisung`
> haben alle einen eigenen AUS-Zweig bei `tmax_c` (48 °C). Im Entscheidungslog
> steht dort fast immer `Einspeisung`, weil sie die höchste dieser Prioritäten
> hat (85). Nachts (Nachtsperre) sind alle drei inaktiv – dann übernimmt
> `Abweichung` (Prio 47) das AUS, weil `soll − temp` dort ≤ +0,0 K ist.
> Der Boiler-Deckel wird also immer von genau einer Regel getragen; die
> AUS-Zweige der niedriger priorisierten Regeln sind der Rückfall.

---

## 3. Die Regeln im Detail

### 3.1 Einspeisung (PV-Shaping am Netzlimit) — Prio 85

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

- Ziel: `solltemperatur_c` (42 °C) am unteren Fühler zur **gelernten** Zielzeit
  (default 17:00, siehe Kapitel 5 „Selbstlernen")
- Benötigte Vorlaufzeit = Temperaturdifferenz ÷ saisonal **gelernter Heizrate**
  (+ Sicherheitspuffer); bei sehr sonnigem HEUTE (`forecast_today_wh_qm`) wird der
  Puffer verdoppelt – dann wartet die Regel lieber auf kostenlosen PV-Strom
- Saisonaler Puffer schützt vor Kaltwasserschauern im Winter
- Konfigurierbarer Konflikt mit der Nachtsperre wird erkannt und geloggt

**Energie-Quellen-Gate mit errechnetem Spätest-Start:** Die Regel war quellenblind
und startete bei Bewölkung notfalls schon Stunden vorher mit Netzstrom. Jetzt:

- **Frühstart nur mit Quelle**: der knappe effektive Puffer (< 0,5 h) löst nur
  aus, wenn echte PV-Einspeisung (≥ `pv_einspeisung_min_watt`, 50 W) oder volle
  Batterie ohne Netzkauf (SOC ≥ `soc_min_prozent`) vorliegt
- **Ohne Quelle wartet die Regel** bis zum ERRECHNETEN Spätest-Start:
  `Zielzeit − berechnete Heizzeit − spaetstart_puffer_h` (0,5 h). Ab dort hat
  die Zapf-Garantie Vorrang – geheizt wird notfalls auch mit Netz, aber nur im
  letzten halben Stunde statt stundenlang vorher
- Der „ZU SPÄT"-Notfall (Puffer negativ) bleibt unverändert als letzte Rettung

### 3.3 PV-Regeln — Prio 78

Klassisches PV-Shaping über die **echte Netzeinspeisung** (`feedinpower`):
Solange Hausverbrauch und Batterieladung den Solarstrom schlucken, feuern sie
nicht – genau die gewünschte Reihenfolge PV-Haus → PV-Batterie → PV-WP.

- EIN bei Einspeisung ≥ Schwelle (500/700 W) und Fühler kalt (≤ 42 °C)
- Weiterlauf bis 48 °C, solange weiterlaufen_ab_pv_watt (50 W) erreicht bleibt
- Im **Sommermodus** werden ihre Abschaltpunkte abgesenkt (siehe 4.2)

### 3.4 Batterie-Regel — Prio 75

> Vorgabe: erst PV direkt, dann Batterie, erst ganz zum Schluss Netzstrom.

**Derzeit `aktiv=false`.** Den Batterie-Pfad deckt das Quellen-Gate der
Abweichungs-Regel ab (SOC ≥ 90 % ohne Netzkauf) – die Vorgabe bleibt damit
erfüllt; die Regel bleibt als eigenständiger Weg verfügbar.

Heizen mit Hausbatterie-Strom, **ohne dass das Haus Netzstrom kauft**:

- **EIN** wenn alle drei Bedingungen erfüllt sind:
  1. `soc ≥ min_soc_prozent` (90 %) – Batterie deutlich gefüllt
  2. `feedinpower ≥ max_netzbezug_watt` (−50 W) – kein nennenswerter Netzbezug
     (kleiner Negativ-Puffer toleriert Messrauschen)
  3. Fühler ≤ `einschalten_bei_c` (41 °C)
- **Weiterlauf** aus der Batterie bis zum Basisziel `ausschalten_bei_c` (42 °C),
  solange 1.+2. halten. Batterie ist bewusst **kein Solarbuffer**; der 48-°C-Puffer
  bleibt aktueller PV vorbehalten.
- Sinkt der SOC darunter oder bezieht das Haus Netz → keine Aktion (Schonung)
- **SOC-Hysterese** (`soc_hysterese_prozent`, default 2): Solange der Kompressor
  läuft, genügt für den Weiterlauf `min_soc − Hysterese` (z. B. 88 %). Ein
  1-%-Ticken der BMS-Anzeige an der Grenzkante bricht einen Lauf nicht mehr ab;
  ein *Neustart* erfordert weiterhin die volle Schwelle.
- Respektiert die Nachtsperre (nachts so wenig wie möglich)

Konfiguration: `batterie.{min_soc_prozent, max_netzbezug_watt, einschalten_bei_c,
ausschalten_bei_c, temperaturfuehler}`

### 3.5 Mindest-Temperatur-Garantien (MinTemp-*) — Prio 65

> Vorgabe: Ohne aktuelle PV-Quelle bleiben obere und mittlere Temperatur bei
> **42 °C**. Mit PV dürfen die PV-Regeln den laufenden Boiler bis maximal
> **48 °C** als Solarbuffer weiterführen. Nach dem Duschen wird nachts nicht
> mehr geheizt.

Pro Eintrag (`mindest_temp.eintraege[]`) gilt:

| Eintrag | Fühler | Garantie | Fenster (default) |
|---|---|---|---|
| Mittag-Mitte | mittig | ≥ 42 °C ohne PV / bis 48 °C mit PV | 9–16 Uhr |
| Abend-Mitte | mittig | ≥ 42 °C ohne PV / bis 48 °C mit PV | 17–22 Uhr *(lernend, effektiv bis Nachtsperren-Beginn)* |

- **EIN**, wenn der Fühler unter der Mindesttemperatur liegt – **auch während der
  Nachtsperre**, solange `"nachtsperre_ueberschreiben": true` ist. Das ist der
  Sinn der Regel: Sie garantiert Komfort zu den Zeiten, in denen er gebraucht wird.
- **`"nachtsperre_ueberschreiben": false`** (beim Einsatz für `Abend-Mitte` aktiv):
  Die Regel bleibt innerhalb der Nachtsperre stumm. Ihre Garantie gilt also nur
  **bis zum Sperren-Beginn** (19 Uhr): Vor dem Duschen wird rechtzeitig auf 42 °C
  vorgeheizt, kühlt der Boiler nach dem Duschen ab, wird nicht mehr nachgeheizt –
  nachts gäbe es dafür ohnehin kein PV und die Nachtsperre bleibt unangetastet.
  Letzte Komfort-Linie bleibt weiterhin der Notfallschutz (Prio 110, 3.11).
- Zwischen `min_temp_c` und `min_temp_c + hysterese_k` (Standard 0 K) tritt die
  Regel stumm zurück. Die Basisregel selbst erzeugt keinen AUS-Befehl; eine
  bestätigte PV-Quelle darf den laufenden Zyklus bis 48 °C weiterführen.
- **Rein additiv:** Die Regel kann nur *einschalten*, niemals blockieren. Ist die
  Garantie erfüllt, liefert sie keine Aktion – so schneidet sie Heizwünsche anderer
  Regeln (PV/CalcStart/Abweichung) nie weg. Die Abschaltung nach einem Garantie-
  Lauf übernimmt der normale Ausschaltpunkt (`min_temp_c + hysterese_k`).
- **Lernend:** Einträge mit `"fenster_aus_lernen": true` verschieben ihr Zeitfenster
  anhand des gelernten Zapfverhaltens (Kapitel 5.3). Klemmung: maximal 2 h früherer
  Start und 1 h späteres Ende gegenüber der Konfiguration.

Konfiguration: `mindest_temp.{aktiv, prioritaet, eintraege[].{name, temperaturfuehler,
min_temp_c, start_uhr, ende_uhr, hysterese_k, fenster_aus_lernen,
nachtsperre_ueberschreiben}}`

### 3.6 Komfort — Prio 60

**Derzeit `aktiv=false`.** Den Komfort-Boden decken die MinTemp-Garantien
(42 °C, 3.5); der frühere „Komfort-Notfall" ist als eigenständige Regel
**Notfallschutz** (Prio 110, 3.11) ausgekoppelt.

- **Komfort:** unten ≤ 38 °C UND etwas PV (≥ 50 W) → EIN

### 3.7 Forecast — Prio 57

Nimmt die Wh/m²-Prognose von MORGEN (Richtung bewusst so gewählt):

- **Morgen schlecht** (≤ 800 Wh/m²): heute vorheizen (bis `t_vorheiz_ab_c` = 42 °C)
  im Fenster 8–19 Uhr – also bis zum Nachtsperren-Beginn
- **Morgen gut** (≥ 3000 Wh/m²): zwischen 11–15 Uhr nicht unnötig heizen – morgen
  kommt wieder PV-Strom

**Energie-Quellen-Gate fürs Vorheizen:** Die Regel ist quellenblind gewesen und
heizte notfalls mit Batterie-/Netzstrom. Jetzt gilt für den EIN-Wunsch:

- **PV-Direkt**: echte Netzeinspeisung ≥ `pv_einspeisung_min_watt` (50 W), oder
- **Batterie**: SOC ≥ `soc_min_prozent` (90 %) UND keine nennenswerter Netzkauf
  (Einspeisung ≥ `vorheiz_max_netzbezug_watt`, −50 W)
- Ist beides nicht erfüllt, wartet die Regel (stumm, kein AUS) – niedriger
  priorisierte Regeln wie Abweichung entscheiden weiter. Mit
  `vorheiz_netz_erlaubt: true` stellt man das alte Verhalten (Netzstrom erlaubt)
  wieder her. Die Sparen-Entscheidung (AUS) braucht keine Quelle.
- Sicherheitsnetz bleibt der Notfallschutz (Prio 110, 3.11): Fällt oben ≤ 36 °C,
  heizt er auch nachts – unabhängig vom Quellen-Gate.

### 3.8 AdaptivePV — Prio 78

Dynamische Einspeise-Schwelle statt fester Werte (wirkt auch ohne konfigurierte
`pv_regeln`, z. B. Basis 300 W):

- Boiler kalt (unten < 35 °C) → aggressiver (Schwelle × 0.7)
- Boiler warm (unten > 38 °C) → konservativer (× 1.5), bis `tmax_c`
- Sehr sonniger HEUTE-Tag → Schwelle × 2 (warten lohnt sich)
- Schlechter Tag (< 1000 Wh/m²) → Schwelle × 0.5 (jede Gelegenheit nutzen)
- Im **Sommermodus** wird `tmax_c` abgesenkt (48 → 46 °C)

### 3.9 Zeitfenster — Prio 53

**Derzeit `aktiv=false`** (von AdaptivePV abgelöst).

16–17 Uhr mit mindestens 410 W PV-Einspeisung heizen, solange der mittlere Fühler
unter 50 °C bleibt. Klassisches „nutz den Tag"-Backup.

### 3.10 Abweichung — Prio 47

Grundregel: **unterer Fühler** vs. Solltemperatur (aktiv **42 °C**). Der untere
Fühler ist der kälteste Punkt und der Frühindikator für „Boiler entladen"
(Zapfung bringt Kaltwasser unten ein); „oben" bleibt durch die Schichtung lange
warm. Regelfühler ist konfigurierbar (`temperaturfuehler`).

- EIN, wenn `soll − temp ≥ einschalten_bei_abweichung_k` (aktiv **+4,9 K**)
- AUS, wenn `soll − temp ≤ ausschalten_bei_abweichung_k` (aktiv **+0,0 K**)
- **Schichtungsschutz:** Ist `oben ≥ schichtung_min_oben_c` (Default 42 °C),
  entscheidet `schichtung_erlaube_start`:
  - `false`: es wird **NICHT** geheizt – die Regel wartet. Verhindert
    die sinnlosen Morgen-Netzläufe bei geschichtetem Boiler (oben heiß, unten kalt).
  - `true` (**aktiv seit 28.09.2026**): Warmstart mit Deckel – oben darf nur
    um `schichtung_max_steig_k` (aktiv **1 K**) steigen. Grund: Bei `oben 57.8 °C`
    / `unten 23.7 °C` (Log 28.09.) blockierte `false` jeden Start – der untere Boiler
    blieb dauerhaft kalt, weil *keine* Regel den Schichtungs-Check umgehen konnte.
  ⚠️ Dieser Zweig liegt **vor** dem Quellen-Gate: bei `true` kann also auch ohne
  PV/Batterie geheizt werden.
- Bademodus erhöht den Soll (+3 K), Urlaubsmodus senkt ihn (−5 K)

**Messbefund (15.09., 3 Wochen Entscheidungslog) – Begründung der Deployment-Werte:**
410 von 435 Abweichungs-Entscheidungen fielen in 8–9 Uhr (direkt nach der
Nachtsperre); **237** davon bei `oben ≥ 42 °C` und **alle ohne PV/Batterie-Quelle**
(385 von 393 Lauf-Einträgen netzgespeist). Neun Läufe heizten den unteren Bereich
mit 0 W Einspeisung über 47–62 min bis ≥ 40 °C. Deshalb `schichtung_erlaube_start:
false`. Gegenprobe: An Tagen mit auch kaltem Oberteil (35–40 °C) bleibt der
normale Pfad aktiv – dort ist Heizen richtig.

**Quellen-Gate mit Tiefenschutz** (`quelle_warten`, default true): Die Regel war
tagsueber quellenblind und heizte bei Durchkuehlung mit Netzstrom. Jetzt:

- Normalfall: EIN nur mit echter PV-Einspeisung (>= 50 W) oder voller Batterie
  ohne Netzkauf - sonst wartet die Regel (stumm, kein AUS)
- **Tiefenschutz**: `solltemperatur_c - netz_notfall_offset_k` (Default 8 K, **aktiv 12 K**)
  wird die Waermepumpe trotz Netzstrom erlaubt - der Boiler kuehlt nie ganz
  durch, nur weil keine Sonne scheint. Begründung für 12 K: nach der Nachtsperre liegt „unten"
  regelmäßig bei ≈ 33 °C, wodurch die 8-K-Schwelle (36 °C) **jeden Morgen**
  auslöste – eine „Notfall“-Grenze, die im Normalbetrieb greift, ist keine
  Notfall-Grenze.
- Mit `quelle_warten: false` stellt man das alte quellenblinde Verhalten her

**PV-Warten-Overlay bei guter Tagesprognose** (`pv_warten_*`, default aktiv):
Bekämpft die Mehrfach messbare „ZU FRUEH“-Phänomene (morgens mit Netzstrom
heizen, kurze Zeit später steht PV-Überschuss bereit). Wenn heute eine sehr
gute Tagesprognose vorliegt, verzichtet die Abweichungs-Regel morgens (bis
`pv_warten_bis_uhr`, default 12 Uhr, exklusiv = ab 13 Uhr wieder normal) auf
den Netzstart - auch unter der Tiefenschutz-Schwelle. Absicherungen:

- Effektive Prognose = `forecast_today_wh_qm × fc_ratio`, erst ab
  `pv_warten_forecast_schwelle_wh_qm` wird gewartet. Default 2.500 Wh/m²,
  **aktiv 1.200 Wh/m²**: auch mäßige Sonnentage sollen erst PV abwarten,
  statt morgens Netzstrom zu verbrauchen.
- `pv_warten_aktiv: false` schaltet das Overlay komplett ab
- Unter `pv_warten_unten_min_c` (default 20 °C) gilt als „echt kalt“
  und es wird sofort (Netz) geheizt

---

### 3.11 Notfallschutz / Wochenende / Legionellen (Prio 110 / 100 / 90)

Diese drei Regeln stehen über allen PV-/Komfort-Regeln (Kapitel 2) und haben
je einen eigenen Zweck:

- **Notfallschutz (110)** – reiner Schutzleiter: `oben ≤ 36 °C` heizt **vor allen
  Sperren** (Wochenende, Nachtsperre). Die Abschaltung (`oben ≥ 38 °C`) gilt
  **nur für den eigenen Lauf** – ein von Komfort/PV gestarteter Lauf wird nicht
  abgewürgt. Spätestens bis `bis_uhr` (22 Uhr), nachts gesperrt; ein Start
  erfolgt nur, wenn die Mindestlaufzeit vorher endet (Ausnahme: angekündigte
  Legionellenfahrt).
- **Wochenende (100)** – blockiert **Starts** vor `fruehestens_uhr` (9 Uhr),
  auch den Legionellen-Start; die Fahrt wird ab 9 Uhr nachgeholt. Ein *laufender*
  Zyklus wird nicht abgewürgt (die Regel bleibt dann stumm, `einschalten=None`).
- **Legionellen (90)** – Prophylaxe 1×/Woche (bevorzugt Donnerstag, spätestens
  Samstag), Ziel `target_temp_c` 60 °C, Limit `legionellen_max_temp_c` 65 °C.
  Während der Fahrt hebt sie das Boiler-Limit von 48 auf 65 °C an (sonst bräche
  der 48-°C-Puffer die Prophylaxe ab). Start bevorzugt mit PV
  (`pv_start_min_watt` 500 W) oder voller Batterie; `spaeteste_start_uhr` 16 Uhr,
  `max_tage_ohne_lauf` 14 als Sicherheitsnetz.

## 4. Übergreifende Mechanismen

### 4.1 Nachtsperre (19–8 Uhr)

Blockiert alle *Einschaltpunkte*. Bewusste Ausnahmen (Garantien):

1. Notfallschutz (oben ≤ 36 °C) — Prio 110 (eigenständige Regel; der frühere
   „Komfort-Notfall" ist dorthin ausgekoppelt)
2. Mindest-Temp-Garantien innerhalb ihrer Fenster **mit
   `"nachtsperre_ueberschreiben": true`** — Prio 65 (Default)
   - Ausnahme: Einträge mit `false` (z. B. Abend-Mitte) feuern nicht in der
     Sperre – ihre Garantie endet mit dem Sperren-Beginn, damit nach dem
     Abend-Duschen kein Nacht-Heizen ohne PV mehr passiert.

Laufende Heizzyklen werden nicht hart unterbrochen; die Abschaltlogik arbeitet
weiter mit dem Ausschaltpunkt der Gewinner-Regel.

### 4.2 Sommermodus (Dauer-Sonne)

Zählt aufeinanderfolgende Tage, an denen heute+morgen+übermorgen jeweils ≥
2000 Wh/m² prognostiziert sind (`logic_utils.evaluate_sommer_modus`, max. eine
Bewertung pro Kalendertag, Rücksetzung bei jeder schlechten Prognose). Nach
3 Tagen aktiviert er zwei Absenkungen:

| Wirkung | Offset | Ziel |
|---|---|---|
| Abweichungs-Soll | −3 K (42 → 39 °C) | Boiler nicht jeden Tag voll heizen |
| PV-Abschaltpunkte & AdaptivePV-tmax | −2 K (48 → 46 °C) | **Buffer wird bei Dauer-Sonne bewusst nicht voll geladen – Max-Temperatur minimiert** |

Klemmung: Der Abschaltpunkt bleibt mindestens 2 K über dem Einschaltpunkt.
Die Mindestgarantien (3.5) bleiben unberührt – Komfort geht vor.

### 4.3 Bademodus / Urlaubsmodus

Werden als Solltemperatur-Offsets auf die effektive Config angewendet, bevor die
Regeln laufen; alle Setpoints in der Statusanzeige zeigen die effektiven Werte.

### 4.4 Taktschutz

Zählt **echte Hardware-Schaltvorgänge** im letzten 60-min-Fenster
(`_hardware_wechsel_historie`). Reine Gewinnerwechsel werden separat als
`_wechsel_historie` diagnostisch aufgezeichnet, lösen aber keine zusätzliche
Pause aus. Ab `max_wechsel_pro_stunde` (8) wird die Mindestpause auf
`zusatz_pause_minuten` (15 min) verlängert; das harte Boiler-Maximum und die
Sicherheitsabschaltung gehen weiterhin vor.

Wichtig: Gezählt werden nur tatsächliche Hardware-Schaltvorgänge (z. B.
`AUS → EIN` oder `EIN → AUS`) – **nicht** jeder Loop-Durchlauf (~10 s). Ein
stabiler Gewinner über Stunden bleibt im hardware_seitigen Histogramm daher
ohne zusätzliche Takteinträge.

Zusätzlich flattersicher:

- **Gewinner-Debouncing**: Ein Wechsel zählt/loggt erst, wenn derselbe neue
  Gewinner **2× hintereinander** bewertet wurde (`_gewinner_debounce`). Grund:
  Mehrere Regeln teilen sich scharfe Kanten (Batterie-EIN bei `unten ≤ 42 °C`
  gegen Komfort-AUS bei `unten ≥ 42 °C`, SOC-Grenze 90 %) – Sensor-/SOC-Jitter
  dort erzeugte sonst Dutzende echte Wechsel pro Stunde. Ein-Sensor-Ticks
  bleiben jetzt ohne Wirkung; die Verzögerung eines echten Wechsels beträgt
  maximal einen Loop (~10 s).
- **Gewinner-Mindesthaltezeit** (`GEWINNER_MINDESTHALTE_MIN = 5 min`,
  `_gewinner_haltezeit_blockiert`): Ein einmal bestätigter Gewinner bleibt
  mindestens 5 min stehen, bevor ein **anderer** Gewinner übernehmen darf.
  Grund: Im Bestandslog flatterte der Notfallschutz am 03.09.2026 40×/30 min,
  weil `oben` zwischen 36,0 und 36,06 °C pendelte – der 2-Loop-Debounce ist
  gegen ein 0,06-K-Rauschen zu kurz. Ein Wechsel *auf denselben* Gewinner ist
  nie blockiert; die Sicherheits-Abschaltungen (Boiler-Max, Überhitzung,
  Not-Aus, Druck) liegen außerhalb dieser Wahl und greifen unverändert sofort.
- **Episode-Logging**: „Taktschutz aktiv" wird nur beim Übergang in die
  Episode gemeldet, „Taktschutz beendet" beim Verlassen – nicht mehr bei jedem
  Durchlauf. Die Meldung „verlängert Pause" erscheint max. alle 30 min.

### 4.5 Forecast-Frische und Heizraten

Forecast-Daten werden nach 12 Stunden ohne erfolgreichen Abruf als stale
markiert und für Prognose-, PV-Warte-, Sommer- und Legionellenregeln
neutralisiert. Die Start-Antizipation verwendet den Median der jüngsten drei
positiven Live-Heizraten. Der Status und das Entscheidungslog enthalten
`rate_confidence`, erwartete Laufzeit, Mindestlaufzeit und Puffer.


### 4.6 Hartes Boiler-Maximum & Ein-Sperre in Limitnaehe

**Zwei Schutzebenen, bewusst getrennt:**

| Ebene | Limit (normal / Legionellen) | Fühler | Zweck |
|---|---|---|---|
| **Überhitzungsschutz** | 58 / 65 °C | **`oben`** (fest) | Schutz des Boiler-Oberteils – greift **unabhängig von den Regeln**, auch gegen Regelfehler |
| **Boiler-Maximum** | 48 / 65 °C | `boiler_max_fuehler` = **`"unten"`** | Brauchwasser-/PV-Puffer-Ziel am unteren Fühler |

**Warum das Boiler-Maximum auf `"unten"` bleibt (Entscheidung):** Ein 48-°C-Deckel
auf `oben` würde den Normalbetrieb brechen. Messung Oktober 2026 (Kompressor
läuft, ohne Legionellen): `oben` liegt im Mittel bei **47,4 °C** (max 54,2 °C)
und ist in **32 %** der Zeit ≥ 48 °C, während `unten` erst bei ~40,8 °C steht –
normale Schichtung, z. B. nach einer Zapfung. Ein `oben`-Deckel bei 48 würde
dieses Nachheizen abschneiden und den Boiler am Limit takten. **Der Schutz des
oberen Bereichs ist Aufgabe des Überhitzungsschutzes** (58/65 °C). Die Option
`"max"` (heißester Fühler) bleibt konfigurierbar, ändert aber das
Abschaltverhalten (dann zählt der heißeste Fühler, und die 2-Zonen-Ausnahme der
Ein-Sperre entfällt).

**Legionellen-Anhebung (`_erhoehtes_legionellen_limit`):** Während einer Fahrt
**und danach** bleibt das Limit auf `legionellen_max_temp_c` (65 °C) angehoben –
**für Boiler-Maximum und Überhitzungsschutz gemeinsam** (sonst würde ein Start
bei `oben` 60 °C sofort am 58er-Schutz abgebrochen). Die Anhebung endet, wenn

- der Zeitnachlauf abgelaufen ist (`sicherheit.legionellen_nachlauf_min`,
  180 min) **und**
- der Boiler `oben` das normale Limit **deutlich unterschritten** hat
  (`max_temp_c − boiler_max_hysterese_k` = **46 °C**),

spätestens aber nach `LEGIONELLEN_ABKUEHL_MAX_H` (12 h). So bleibt der Boiler
nach der Prophylaxe nutzbar (er kühlt nur passiv ab) und das 48-°C-Limit greift
erst wieder, wenn er wirklich abgekühlt ist. `legionellen_nachlauf_min = 0`
schaltet die Anhebung nach der Fahrt komplett ab.

1. **Abschaltung (nachtraeglich):** Erreicht der Fuehler 48 C, wird der Kompressor
   sofort ausgeschaltet - auch wenn dadurch die Mindestlaufzeit gebrochen wird.
   Zusätzlich wird die Kuehlschwelle `boiler_max_blockiert = max_temp_c -
   boiler_max_hysterese_k` (= 46 C) gesetzt: Ein Neustart ist erst wieder
   moeglich, wenn der Fuehler die Schwelle unterschreitet. Telegram-Warnung inklusive.
2. **Ein-Sperre im Naecherbereich (proaktiv, seit Incident 26.08.2026):** Schon
   **vor** dem Einschalten greift `boiler_max_ein_abstand_k` (aktiv **3.0 K**):
   Liegt der Fuehler bei >= max_temp_c - Abstand (= 45 C), wird kein EIN mehr
   ausgeloest, sondern nur der Blocking-Reason „Boiler-Max-Naehe ..." gesetzt.
   Hintergrund: Startet die WP knapp unter dem Limit, erreicht sie es noch
   innerhalb der Mindestlaufzeit -> Kurzlauf mit gebrochener Laufzeit und
   anschliessend verschenkter Puffer durch die Kuehlphase (im Incident lief die WP
   bei unten 47,94 C genau 2 min und loeste dann BOILERMAX-AUS aus).

Die Ein-Sperre gilt dauerhaft im Naecherbereich (unabhaengig vom
Kuehlschwellen-Flag) und hebt sich automatisch auf, sobald wieder >= 3 K Luft
zum Limit bestehen. Sonderfall bleibt erlaubt: oben warm (48,7 C) / unten kalt
(30 C) -> EIN, weil nur der Bezugsfuehler zaehlt.

**Vorausschauende Vermeidung (seit der Log-Analyse, Config `sicherheit`):**
Die statische Ein-Sperre ist um zwei dynamische Schutzschichten ergaenzt:
- **EIN-Antizipation** (`start_vorhersage_aktiv`): Vor jedem Start wird
  `Hub/Rate` gegen die Mindestlaufzeit gerechnet. Reicht der freie Hub bis zum
  Ausschaltpunkt nicht, um Mindestlaufzeit + `start_vorhersage_puffer_min` zu
  fuellen, wird **nicht** eingeschaltet (`reason=start_vorhersage`) - die
  beobachteten 6-10-Minuten-Kurzlaeufe am Limit entfallen, statt in den
  erzwungenen Laufzeit-Bruch/Kuehlphase zu laufen.
- **AUS-Vorhersage** (`overshoot_vorhersage_aktiv`, `overshoot_rate_schwelle_c_h`,
  `overshoot_reserve_k`): Hat die Regel den Abschaltpunkt erreicht und wuerde
  die Mindestlaufzeit den Fuehler mit der aktuellen Rate ueber die Obergrenze
  treiben, wird **vorher** abgeschaltet (`reason=overshoot_vorhersage`) - statt
  bei 49 C den Bruch zu erleben.

Die Rate stammt aus der Live-Messung (`state.control._rate_messung`), der
gelernten Rate der Lern-Engine oder `rate_fallback_c_h`. Der Telegram-Alarm
„Boiler-Max-Naehe“ wird auf 1x/Tag gedrosselt (frueher bis zu 38x/Woche).

### 4.7 Taktungs-Reduktion: PV-Weiterlauf-Band & Hysterese-Sparen (3.2)

- **PV-Weiterlauf-Band** (`zyklus.pv_weiterlauf_abschalt_delay_min`, default
  3 min): Will eine PV-Regel waehrend eines laufenden Zyklus wegen „PV zu
  wenig“ abschalten, wird das AUS fuer die Delay-Minuten zurueckgehalten
  (Wolke). Erst danach greift der AUS-Wunsch. Temperatur-bedingte AUS bleiben
  sofort wirksam.
- **AdaptivePV-Hysterese-Sparen** (`adaptive_pv.sparen_hysterese_k`, default
  1 K; `adaptive_pv.hysterese_forecast_schwelle_wh_qm`, default 2500 Wh/m²):
  Bei sehr guter HEUTE-Prognose wird die EIN-Grenze gesenkt (45→44 C) - die WP
  startet tiefer und laeuft laenger statt oben-nahe zu takten.

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


### 5.4 Quellen-Attribution je Heizzyklus

Die Quelle eines Zyklus wird **beim Start** festgehalten
(`state.control.source_at_start`, Werte `PV` / `Batterie` / `Netz` aus
`energy_source`) und landet als Spalte `quelle` in `csv log/zyklen.csv`.
Fehlt der Startwert (Alt-Datensatz), greift ein **Regelname-Fallback**
(`cycle_logging._quelle`): `AdaptivePV`/`Einspeisung`/`PV_*` → `pv`,
`Batterie` → `batterie`, alle übrigen Regeln (Legionellen, Notfallschutz,
Abweichung, CalcStart, Komfort, Zeitfenster, Wochenende, API) → `netz`,
sonst `unbekannt`.

> ⚠️ Das ist **nicht** dieselbe Kennzahl wie der Energie-Split im
> Entscheidungslog (`entscheidungs_log.kpis`, sichtbar unter `/status` → `kpi`).
> Der rechnet je Regelzyklus aus `feedin`/`batpower` und kennt
> `pv` / `batterie` / `netz` / `unklar` (Schwellen in `constants.py`).
> Der Regelname-Fallback in `zyklen.csv` kann deshalb von der gemessenen
> Quelle abweichen (Beispiel: ein `Abweichung`-Lauf wird ohne
> `source_at_start` als `netz` geführt, obwohl er real überwiegend aus PV lief).

Daraus entsteht der Laufzeit-Split ("Wie viel deiner Warmwasser-Waerme kam
wirklich aus PV?") plus die **Zu-frueh-Erkennung**: Endet ein Nicht-PV-Zyklus
und kommen binnen 45 min doch >800 W Einspeisung, wird das als verfruehter
start gezaehlt (`zu_frueh_events`). Sichtbar ueber `/status`
(`quellen`, `forecast_ratio`, `surplus_stunden`,
`surplus_profil`) und als Karten "Selbstlernen" / "Ueberschuss-Profil"
im Dashboard (webapp/index.html).

### 5.5 Forecast-Kalibrierung (Haus-spezifischer Langfehler)

Taeglich ab 20 Uhr wird das Verhaeltnis *tatsaechlicher Tages-Netzeinschuss
(Wh, integriert) / Tagesprognose (Wh/m2)* als EWMA (alpha=0.3) gelernt und auf
0.3-2.0 geklemmt. Ab 3 Tageswerten multipliziert dieser Faktor die HEUTE-
Prognose in CalcStart UND AdaptivePV - ein Haus mit dauerhaft zu optimistischem
Solcast lernt das in ~3 Tagen und wartet realistischer. Tage ohne brauchbare
Prognose (<1000 Wh/m2) oder mit Datenluecken (<50 Wh Surplus) werden
uebersprungen.

### 5.6 Surplus-Stundenprofil (Verbrauchsbewusstsein / Mittagstief)

Die Engine sampelt stuendlich die Netzeinspeisung - aber nur bei
AUSgeschaltetem Kompressor, also das reine Haushaltsmuster (Kochen um 12 Uhr,
Waschen am Abend). Ab 5 Samples je Stunde und 4 brauchbaren Stunden steht das
Profil zur Verfuegung. CalcStart zaehlt gelernte Tiefstunden (<250 W) zwischen
jetzt und Zielzeit nur zu 75% als Heizzeit: Der Spaetest-Start verfrueht sich,
die WP erreicht Soll VOR dem Mittagstief, pausiert waehrenddessen natuerlich
(Komfort-AUS) und bedient die Zapfzeit trotzdem. Im Log als
`| Mittagstief 12-13 Uhr`.

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
  "sicherheit":   { "max_temp_c": 48, "boiler_max_fuehler": "unten", "boiler_max_hysterese_k": 2.0, "boiler_max_ein_abstand_k": 3.0, "nachtsperre_start": 19, "nachtsperre_ende": 8, "start_vorhersage_aktiv": true, "start_vorhersage_puffer_min": 1.0, "ueberhitzung_c": 58, "notfall_c": 36, "legionellen_nachlauf_min": 180 },
  "abweichung":   { "solltemperatur_c": 42, "einschalten_bei_abweichung_k": 4.9, "ausschalten_bei_abweichung_k": 0.0, "schichtung_min_oben_c": 42, "schichtung_netz_max_oben_c": 45, "pv_warten_aktiv": true, "pv_warten_forecast_schwelle_wh_qm": 1200, "pv_warten_bis_uhr": 12, "pv_warten_unten_min_c": 20 },
  "einspeisung":  { "einspeisegrenze_watt": 7500, "weiterlauf_ab_watt": 6500, "ausschalten_bei_c": 48 },
  "zyklus":       { "mindestlaufzeit_minuten": 60, "mindestpausenzeit_minuten": 30, "pv_min_laufzeit_minuten": 10, "pv_weiterlauf_abschalt_delay_min": 3.0 },
  "batterie":     { "aktiv": false, "min_soc_prozent": 90, "max_netzbezug_watt": -50 },
  "adaptive_pv":  { "base_threshold_watt": 300, "tmax_c": 48, "sparen_hysterese_k": 1.0, "hysterese_forecast_schwelle_wh_qm": 2500 },
  "mindest_temp": { "eintraege": [ /* Fuehler+Fenster+min_temp_c+fenster_aus_lernen+nachtsperre_ueberschreiben */ ] },
  "sommer_modus": { "temperatur_offset_c": -3.0, "pv_ausschalt_offset_c": -2.0 }
}
```

> Die Liste ist ein **Auszug**. Maßgeblich ist immer
> `wp_steuerung_parameter.json`; fehlende Schlüssel nehmen ihre Defaults aus
> `json_config.py` an. Werte mit dem Zusatz „aktiv" oben im Text entsprechen
> genau dieser Datei (Stand des letzten Reviews).

## 8. Tests

Die Logik ist durch Unit-/Integrationstests abgedeckt (`tests/`), u. a.:

- `test_einspeisung.py` – Netzlimit-Regel: EIN/Weiterlauf/AUS/Nachtsperre/Priorität
- `test_batterie.py` – SOC-/Netzbezug-Gates, Schonung, Vorrang der PV-Regeln
- `test_mindesttemp.py` – Garantien inkl. Nachtsperren-Bypass und Additivität
- `test_mindesttemp_nachtsperre_bypass.py` – `nachtsperre_ueberschreiben=false`:
  Abend-Garantie (42 °C) endet mit dem Sperren-Beginn, kein Nacht-Heizen
- `test_zapf_fenster.py` – Lernfenster: Berechnung, Klemmung, Durchreichung
- `test_sommer_pv_cap.py` – Sommer-Absenkung der PV-Ziele ohne Garantie-Verletzung
- `test_calcstart_nachtsperre.py` – Konflikt-Erkennung Zielzeit ↔ Nachtsperre
