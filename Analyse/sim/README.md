# Mehrtages-Simulation der Wärmepumpen-Regelung

Simuliert die **echte** Regelungslogik aus `Steuerung/` über mehrere Tage
und macht die Wirkung von Konfigurationsänderungen messbar.

## Schnellstart

```bash
# 1. Speichermodell gegen die Realdaten kalibrieren (einmalig)
py -3 Analyse/sim/calibrate.py

# 2. Alle Szenarien durchrechnen
py -3 Analyse/sim/run.py

# 3. Konfigurationen vergleichen
py -3 Analyse/sim/varianten.py --tage 14 --varianten basis,verbessert
```

## Warum das so gebaut ist

Die Simulation ruft **nicht** eine nachgebaute Logik auf, sondern dieselben
Funktionen, die auch der 10-Sekunden-Hauptloop aufruft:

```
check_safety_limits  ->  determine_mode_and_setpoints
                     ->  handle_compressor_on / handle_compressor_off
```

Hardware, Telegram, Solax-API und Datei-Logging sind Attrappen. Dadurch
lassen sich Regeländerungen gegen dieselbe Logik messen, die auf dem Pi läuft.

## Grenzen des Speichermodells (wichtig fuer die Interpretation)

Das Modell mischt **deutlich staerker** als die reale Anlage. Nach der
Legionellenprophylaxe bleibt im Modell auch `unten` warm (Tiefpunkt 43,2 C
im 14-Tage-Winterlauf), waehrend die reale Anlage am 28.09.2026 auf
`unten 22,8 C / mitte 41,2 C / oben 55,0 C` fiel - ein Gefaelle von 32 K.

Ursache: `konvektion_aus_w_k = 12.0` und `konvektion_oben_aus_w_k = 3.0`
im Leerlauf; die Schichtung bleibt im Modell bei ~14 K.

**Folge fuer den Test:** Der fehlerhafte Zustand aus dem Nutzer-Log laesst
sich im normalen Szenario-Ablauf nicht erzeugen.
`check_legionella_zustand.py` setzt ihn deshalb direkt in die Startwerte
des Speichers. Ausserdem hebt der Legionellen-Nachlauf im
Simulationszustand das Limit - die Simulation kann die Blockierwirkung von
`boiler_max_fuehler = "max"` daher nicht abbilden. Der Nachweis fuer diese
Einstellung kommt aus den Boiler-Max-Tests mit den Logwerten, nicht aus
der Simulation.

Nichtsdestotrotz hat die Simulation einen echten Absturz gefunden: Die
Blockiermeldung formatierte `t_mittig` ungeschuetzt - bei ausgefallenem
Mittig-Fuehler (None) wuerde der f-string die komplette Regelschleife
abwuergen, die WP staende dann still ohne protokollierten Grund. Behoben,
gesichert durch
`test_boiler_max_meldung_uebersteht_fehlenden_mittig_fuehler`.

Aufruf:
```bash
py -3 Analyse/sim/check_legionella_zustand.py
```

## Start-Antizipation: Kandidatenvergleich (Befund 14.10.2026)

Anlass: Im Nutzerprotokoll vom 14.10. blockte die Start-Antizipation um 18:05
einen quellengestuetzten CalcStart-Start ("hub zur Obergrenze nur 6,1K (Rate
21C/h, Confidence 40% -> 18min < 60min Mindestlaufzeit)"), obwohl im **selben**
Lauf Heizplan und CalcStart **5,7 C/h** nannten. Verdacht: zwei verschiedene
Ratenquellen fuer dieselbe physikalische Groesse.

```bash
py -3 Analyse/sim/kandidaten.py --szenarien winter_misch --tage 14
py -3 Analyse/sim/kandidaten.py --varianten basis --tage 14
```

Kandidaten: `rate_cap` (Antizipations-Rate auf `heating_rate_max_c_h` = 6 C/h
begrenzt), `rate_cap_12` (12 C/h), `calcstart_pv` (CalcStart als
quellengesteuerter Lauf, 10 statt 60 min), `antizipation_aus` (Grenzwert).

Ergebnis (10 Tage, Schritte 60 s, identische Seeds):

| Szenario | Kandidat | Netz/Tag | Kurzlaeufe | Antiz.-Sperre | min unten |
| --- | --- | --- | --- | --- | --- |
| September | basis | 0,02 kWh | 14 | 2,37 h | 36,6 C |
| September | rate_cap | 0,02 kWh | 15 | 0,00 h | 37,7 C |
| Dezember misch | basis | 3,49 kWh | 24 | 21,18 h | 31,3 C |
| Dezember misch | rate_cap_12 | 3,49 kWh | 24 | 21,18 h | 31,3 C |
| Dezember misch | rate_cap | 3,69 kWh | 29 | 14,27 h | 31,5 C |
| Dezember misch | antizipation_aus | 3,74 kWh | 35 | 0,00 h | 33,8 C |

Drei belegte Aussagen:

1. **Im PV-reichen Betrieb ist die Regel praktisch wirkungslos** (2,4 h Sperre,
   danach identische Kennzahlen bis auf Rauschen) - sie kostet dort nichts.
2. **Jede Lockerung erkauft Komfort mit Netzstrom UND mehr Kurzlaeufen.**
   Im wechselhaften Dezember kostet `rate_cap` +0,20 kWh/Tag Netz und +5
   Kurzlaeufe; das komplette Abschalten +0,24 kWh/Tag und +11 Kurzlaeufe.
   `rate_cap` ist damit eine Zwischenstufe ohne eigenen Vorteil.
3. **`calcstart_pv` ist im Modell ein No-op - strukturell.** CalcStart gewinnt
   dort **keinen einzigen Lauf** (Laeufe je Regel: nur AdaptivePV, Abweichung,
   Notfallschutz, Forecast). Die Simulation kann diesen Kandidaten deshalb
   **nicht** bewerten; die Frage "ist CalcStart ein PV-Lauf (10 min) oder ein
   Bedarfslauf (60 min)?" bleibt eine Entscheidung des Betreibers.

**Grenze der Aussage:** Das Modell rechnete mit Raten unter 12 C/h, die
Anlage mit 21 C/h (Begruendung siehe `learning_engine.py:46-50`:
*"Laeuft der untere Knoten sichtbar schneller, ist das ein Lokaleffekt und
kein Mass fuer die Startplanung."*). Die **Richtung** der Aussagen ist
uebertragbar, die Groesse der Effekte nicht.

**Beschluss:** Keine Aenderung an der Start-Antizipation und keiner
Mindestlaufzeit. Die beobachtete Stunde im Nutzerprotokoll ist die
Mindestlaufzeit-Sperre (Verdichterschutz) nach einem Start, den die
Antizipation selbst durchgelassen hat (Hub 20,8 K -> 59,4 min + 1 >= 60) -
kein Regelungsfehler.

## Szenarien

| Name | Beschreibung |
| --- | --- |
| `september` | PV-reich, echte DC-Profile aus `logs/24.9` (58,3 kWh/Tag, Median) |
| `winter_wenig_pv` | Dezember, trüb, 700 Wh/m² (~6,7 kWh/Tag) |
| `winter_ohne_pv` | Dezember, Starknebel, 120 Wh/m², kein PV |
| `winter_misch` | Dezember, wechselnd, 1600 Wh/m² |
| `winter_ohne_pv_lang` | 5 Wochen ohne PV (bestehender Betrieb) |
| `winter_batterie_leer` | wenig PV, **Batterie wird nie voll** (SOC-Deckel 55 %, Start 20 %) |
| `winter_batterie_voll` | wenig PV, **Batterie kurzfristig voll** (Start 100 %, 3 kWh) |

### Die PV-Profile (20.10.2026 korrigiert)

Die PV-Erzeugung kommt aus den **Gleichstromseiten** der Betriebslogs
(`powerdc1 + powerdc2`), nicht aus `acpower`:

| Profil | Quelle | Summe/Tag | Spitze |
| --- | --- | --- | --- |
| `september` | `logs/24.9/heizungsdaten.csv` (Median je Stunde) | 58,3 kWh | 8,1 kW (13 Uhr) |
| `dezember` | `Analyse/merged_data.csv` (Median je Stunde) | 9,5 kWh | 2,0 kW (13 Uhr) |

`acpower` ist der Wechselstromausgang des Wechselrichters (Haus + Netz) und
liefert nachts rund 230 W, obwohl die Sonne nicht scheint. Mit diesem Wert
als „PV-Erzeugung" hätte die Simulation nachts Strom aus dem Nichts
erzeugt, das PV-Fenster nie geschlossen und die Tagesprognose zu keinem
Profil gepasst.

Die Stundenprognose wird aus der **Form** des Profils und der **Höhe**
der Szenario-Tagesprognose gebildet; ihre Summe über 24 h ergibt damit
exakt `forecast_wh`. Die Quellenlage (`feedinpower`) wird wie im Betrieb
geführt: negativ bei Netzbezug – nur so erkennt das Quellen-Gate der
Regelung („kein Netzkauf") einen Netzbezug.

**Offene Lücke:** Der Dezember-Log `Analyse/heizungsdaten.csv` hat 80 %
unveränderte Folgewertzeilen (Stale-Werte) und liefert tagsüber eine
unplausible Hauslast (2,2 kW bei ausgeschalteter WP). Für die
Hauslast-Kalibrierung wird deshalb weiterhin das September-Profil mit
Faktor 1,15 verwendet. Belastbare Winter-Hauslastdaten fehlen (siehe
„Datenbedarf" unten).

### Die beiden Batterie-Szenarien

Sie belegen, warum die Batterie-Regel **deaktiviert** wurde
(`batterie.aktiv: false`).

* `winter_batterie_leer`: Die Batterie erreicht den 90-%-Schwellwert
  nie (0 % der Zeit) – die Regel wäre prinzipiell blockiert.
* `winter_batterie_voll`: Die Batterie startet voll und ist 5–8 % der
  Zeit über 90 % – die Voraussetzungen wären erfüllt.

**Ergebnis:** Selbst bei voller Batterie gewinnt die Batterie-Regel
(Prio 75) nie. Grund: Sie startet erst bei `unten ≤ 41 °C`, während der
Notfallschutz (Prio 110) den Speicher im Winter auf ~44–45 °C hält. Die
Bedingungskette `SOC ≥ 90 %` **und** `Entladung ≥ 50 W` tritt in 1,1 % der
Schritte auf – mit `unten ≤ 41 °C` sind es **0 Schritte**. Im September-Log
konnte sie in 0,65 % der Samples feuern (Fenster 9–11 und 17–18 Uhr); im
Winter schließt dieses Fenster. Die Regel war damit tote Konfiguration und
ist jetzt bewusst abgeschaltet.

### Notfallschutz: reiner Schutzleiter (Nutzerentscheidung)

`notfallschutz.temperaturfuehler = "oben"`, `ausschalten_bei_c = 38.0`.

Der Notfallschutz ist ausschließlich ein Schutzleiter für den oberen Fühler
und steuert die Komforttemperatur **nicht** mit. Das kostet im Winter
Temperatur, ist aber die gewünschte klare Trennung:

| Notfallschutz | min. `mitte` (14 Tage, wenig PV) |
| --- | --- |
| `oben` / 38 °C (Nutzerwunsch) | 15,3 °C |
| `alle` / 42 °C (Alternative) | 32,2 °C |

**Vorwarnung:** Mit `oben`/38 °C fällt der Speicher in PV-armen Nächten
unter 20 °C (gemessener Tiefpunkt: 15,1 °C). Die Warmwasser-Versorgung
hängt dann allein an der Abweichungs-Regel und am Zapfprofil. Vor dem
Produktivbetrieb sollte geprüft werden, ob das für den Haushalt
akzeptabel ist oder ob die Komforttemperatur anderswo gesichert wird
(z. B. `MinTemp`-Zeitfenster erweitern).

## Kalibrierung

`calibrate.py` leitet die Speicherparameter aus `logs/24.9/heizungsdaten.csv`
(23 Tage Realbetrieb) ab und validiert sie, indem es echte Kompressorzyklen
mit dem gemessenen Verlauf nachfährt.

Gemessene Zielwerte (15-min-Fenster, Median):

| Größe | Messung |
| --- | --- |
| Heizrate unten (AN) | +5,6 K/h |
| Kühlrate unten (AUS) | −0,48 K/h |
| Schichtung mitte−unten (AN) | +0,40 K |
| Schichtung mitte−unten (AUS) | +15,4 K |
| Schichtung oben−unten (AN) | +2,2 K |
| Schichtung oben−unten (AUS) | +21,0 K |
| Zapfereignisse | 7,5/Tag, Median −3,3 K am unteren Fühler |

Validierung (alle Zyklen ≥ 25 min, Modell minus Messung):
`unten` +1,6 K, `mitte` +2,9 K, `oben` −0,8 K bei je ~1 K Streuung.

## Grenzen des Modells

* Der Speicher ist **drei Knoten** (unten/mitte/oben) mit linearer
  Konvektion. Reale Durchmischung im Heizkreis ist komplexer; die
  reproduced Raten und Schichtungen liegen aber im Zielbereich.
* **Kein Wärmekreislauf**: simuliert wird ausschließlich Brauchwasser.
* COP ist konstant; ein temperaturabhängiger COP wäre genauer, ändert an den
  relativen Verbesserungen aber nichts.
* PV ist ein **gemessenes** Profil (Gleichstromseite, Median je Stunde),
  kein synthetischer Sonnenstand. Für Tage mit wechselnder Bewölkung
  addiert die Simulation ein Streuungsmodell auf.
* Aussagen sind **relativ**: Der Vergleich Basis ↔ Variante läuft mit
  identischem Zufalls-Seed und ist damit belastbar; absolute kWh-Zahlen
  haben die Genauigkeit des Modells (±1–2 K Temperatur, wenige Prozent Energie).

## Validierung gegen die Betriebsdaten (20.10.2026)

| Größe | Messung | Simulation |
| --- | --- | --- |
| PV-Erzeugung September | 58,4 kWh/Tag | 58,3 kWh/Tag |
| PV-Erzeugung Dezember | 9,5 kWh/Tag (Median) | 6,7 kWh/Tag (Faktor 0,7 = trüber Tag) |
| WP-Leistung beim Heizen | ~480 W (Sept.) / ~2,1 kW (Dez.) | 500 W / 2100 W |
| Batterie nachts | ~190 W Entladung, ~27 W Netz | 80 % der Deckung |
| Forecast-Kalibrierung | ~1,0 (Verhältnis Erzeugung/Prognose) | 1,00–1,11 |
| Netzbezug Dezember | 9,4 kWh/Tag | 9,6 kWh/Tag ✅ |

✅ **Der Winter-Netzbezug stimmt** – nach der Winterkalibrierung (siehe
unten). Die frühere Abweichung von 4 kWh/Tag kam daher, dass die
Simulation den Speicher warm startete (41 °C) statt mit den gemessenen
11,9 °C und zu wenig Wärmeverlust modellierte.

**Die WP-Leistung beim Heizen ist saisonabhängig** – aber nicht, weil
der COP einbricht: die WP steht im Keller, dessen Temperatur sich
zwischen Winter und Sommer kaum unterscheidet. Gemessen:

| | thermisch auf den Speicher | elektrisch |
| --- | --- | --- |
| September | 1,3 kW | ~0,48 kW |
| Dezember/Januar | 1,6 kW | ~2,1 kW |

Die thermische Leistung ist praktisch gleich, die elektrische um den
Faktor 4,4 verschieden. Bei gleichem Keller kann das kein COP-Effekt
sein. **Was** die winterliche Zusatzlast ist (Räume, separate Heizung,
Maschine mit Heizkreis) lässt sich aus dem Log nicht trennen – für die
Regelung ist es aber auch egal:

**Entscheidend ist, dass die Zusatzlast den PV-Überschuss aufzehrt.**
Gemessen (DC-Erzeugung minus Hauslast, 9–16 Uhr):

| | Anteil Stunden mit Überschuss > 500 W | Median Überschuss |
| --- | --- | --- |
| September | 92 % | 5.592 W |
| Dezember | 31 % | 0 W |
| Januar | 38 % | 75 W |

Die PV-Regeln (Abweichung, CalcStart, Batterie, AdaptivePV) gaten
aber genau an diesem Überschuss. **Im Winter haben sie in 62–69 % der
Tagesstunden nichts, worauf sie reagieren können** – die PV-Fenster
sind dann keine Zeitenfenster, sondern Messwerte, die man nicht
beeinflussen kann.

Mit dem September-Wert für die WP-Leistung unterschätzte die Simulation
den Winter-Netzbezug um rund 8 kWh/Tag. Die Werte stammen aus
`Analyse/merged_data.csv` (Dezember 2025, Januar 2026), dessen
Energiebilanz auf 0,1–0,9 % genau aufgeht.

**Die zweite, größere Modelllücke:** Die Simulation startet den Speicher
warm (41/42/43,5 °C) und modelliert zu wenig Wärmeverlust. Real steht
der Speicher im Winter morgens bei **13 °C** – die Nachtsperre verhindert
das Nachheizen, also muss die WP jeden Morgen von 13 °C auf 42 °C
heizen (29 K). Die Simulation braucht dafür 2,6 h/Tag, real sind es
5,2 h/Tag. Deshalb bleibt ihr Winter-Netzbezug um rund 4 kWh/Tag zu
niedrig. Für die **relativen** Varianten-Aussagen ist das unkritisch –
alle Varianten machen denselben Fehler.

### Die Winterkalibrierung (20.10.2026)

Die Lücke oben ist inzwischen geschlossen. `calibrate.py winter` kalibriert
das Speichermodell auf den Winterdaten (`Analyse/merged_data.csv`,
Dezember 2025 + Januar 2026) und schreibt `parameter_winter.json`.
Die Winterszenarien laden diese Datei über `parameter_datei` und starten
mit den gemessenen Temperaturen (11,9 / 32,0 / 44,2 °C).

| Größe | Messung Dezember 2025 | Simulation (kalibriert) |
| --- | --- | --- |
| Netzbezug | 9,4 kWh/Tag | **9,3 kWh/Tag** |
| WP-Laufzeit | 5,2 h/Tag | 4,6 h/Tag |
| tiefste Temperatur | 13 °C | 11,9 °C |
| tiefste Temperatur oben | 42,4 °C | 41,0 °C |
| Startstunden | 8–22 Uhr, Schwerpunkt 9–13 | 8–14 Uhr |

**Warum der Winter anders kalibriert werden muss:** Der gemessene
`T_Unten` verhält sich im Winter *nicht* wie ein passiver Speicherknoten –
er fällt nach dem Abschalten der WP in etwa einer Stunde um 10–15 K,
während `T_Mitte` und `T_Oben` warm bleiben. Diese Bilanz ist mit einem
Speichermodell nicht schließbar: das Volumen, das den unteren Knoten auf
13 °C abkühlen lässt, müsste den oberen Knoten mitheizen. Der Fit über
die Raten bleibt deshalb bei ≥ 7 K Fehler, bei allen Parametersätzen.

Kalibriert wird deshalb nicht auf Raten, sondern auf das, was am Ende
zählt: die Simulations-Kennzahlen gegen die gemessenen Werte
(`kalibriere_winter_kpi`). Analytisch aus den Daten abgeleitet:

| Größe | Dezember | Januar | September (Kontrolle) |
| --- | --- | --- | --- |
| Wärmeverlust des Speichers (18→06 Uhr) | 301 W | 339 W | 244 W |
| daraus: Verlustkoeffizient | 3,70 W/K | 4,13 W/K | 2,35 W/K |
| WP-Nettoleistung (09→15 Uhr) | 829 W | 934 W | 381 W |

### Drei Modellbefunde aus der Winterkalibrierung

**1. Der obere Fühler sitzt nicht im Keller.** Über Nacht verliert der
obere Fühler 1 K (44,6 → 43,6 °C), der untere 17 K (30,6 → 13,8 °C).
Mit einer gemeinsamen Umgebung für alle Schichten kühlte der obere
Knoten viel zu stark ab – der Notfallschutz (Prio 110, `oben` < 36 °C)
löste dann **nachts** aus und die WP startete um 3 Uhr. Real startet sie
nie nachts. Deshalb hat die obere Schicht jetzt eine eigene Umgebung
(`umgebung_oben_c` = Aufstellraum) und einen eigenen Verlustanteil
(`verlust_oben_anteil`).

**2. Die Konvektion sprengte die explizite Integration.** Bei
k = 3716 W/K und 60-s-Schritt waren 52 kWh je Stunde im Spiel – der
obere Knoten wurde leer gepumpt, der Speicher „überhitzte" scheinbar
auf 99 °C. Die Kopplungsterme sind jetzt auf 40 % der Knotenkapazität
begrenzt.

**3. Die Legionellenfahrt lief 281-mal statt 2-mal.** Die Wärme wurde
gleichmäßig auf alle Schichten verteilt, dadurch erreichte `oben` die
65 °C, bevor `unten` die Zieltemperatur 60 °C erreichte – der
Überhitzungsschutz beendete die Fahrt und die Regel startete sofort
neu (38 Läufe je Tag). Mit der aus den Daten abgeleiteten Verteilung
(30 % in die obere Schicht) läuft die Fahrt einmal und endet regulär.

**Verbleibende Modellgrenze:** Das Nachfahren der einzelnen Zyklen bleibt
im Winter ungenau (Bias +2,3 K am unteren Fühler) – das ist die
unaufgelöste Wärmebilanz des unteren Knotens. Die Kennzahlen (Energie,
Laufzeit, Startzeiten) treffen die Realität, die Temperaturkurve tut es
nur als Tendenz.

### Das Ergebnis des PV-Fenster-Vergleichs (20.10.2026)

Mit der kalibrierten Winterbasis (14 Tage je Szenario):

| Szenario | Variante | Netz kWh/Tag | CalcStart-Läufe | PV-Eigenverbr. | h mitte<40 °C |
| --- | --- | --- | --- | --- | --- |
| Dezember, wenig PV | mit Fenster | 9,41 | 10 | 62 % | 184 |
| Dezember, wenig PV | ohne Fenster | 9,45 | 16 | 51 % | 178 |
| Dezember, wechselnd | mit Fenster | 1,73 | 9 | 36 % | 198 |
| Dezember, wechselnd | ohne Fenster | 1,90 | 16 | 33 % | 185 |

**Das PV-Fenster verschiebt die Starts – es senkt den Netzbezug kaum.**
Der Effekt auf den Netzbezug liegt bei 0,04 bis 0,17 kWh/Tag (unter 2 %).
Deutlicher ist der Wechsel der auslösenden Regel: mit Fenster laufen
weniger CalcStart-Läufe (10 statt 16) und mehr PV-Regeln (Forecast
12 statt 0, AdaptivePV 21 statt 15). Die Starts liegen mit Fenster
später (13–15 Uhr statt 14–15 Uhr).

**Warum der Effekt klein ist:** Im Winter gibt es in 62–69 % der
Tagesstunden keinen PV-Überschuss. Die PV-Fenster-Regeln haben dann
nichts, worauf sie reagieren können – das Fenster ist in diesen Stunden
eine Zeitvorgabe ohne PV-Wirkung.

### Die Einheiten der Prognose (20.10.2026)

Die Regelung liest die Tagesprognose über `normalize_forecast_wh_qm` und
erwartet – wie Open-Meteo – **kWh/m²**. Ein Szenario definiert sie in
Wh/m²; der Harness rechnet um. Ohne diese Umrechnung las die Regelung
700 Wh/m² als 700 kWh/m²: die Prognose war um den Faktor 1000 zu
pessimistisch, das PV-Fenster schloss nie und die Forecast-Kalibrierung
klemmte am unteren Wert (0,3).

Zusätzlich gab `priority_control_logic` der Lernengine `pv_erzeugung_watt`
**nie** weiter. Die Engine zählte damit den Wechselstromausgang
(Haus + Netz) statt der Erzeugung – nachts ~230 W statt 0 W. Auch das
ist im Produktivcode behoben.

## Datenbedarf

Für belastbare Winter-Aussagen fehlen noch:

1. **Winter-Speichertemperaturen und WP-Laufzeiten** über mehrere Wochen,
   um das Speichermodell für den Winter zu kalibrieren. `calibrate.py`
   arbeitet bisher nur mit September-Daten – dort läuft die WP 2,3 h/Tag
   (real 4,8 h/Tag), im Winter 5,2 h/Tag. Solange das Speichermodell
   nicht für den Winter kalibriert ist, ist der **Absolutwert** des
   Winter-Netzbezugs (und damit der Winter-Komfort) eine Tendenz; die
   **relativen** Varianten-Aussagen bleiben belastbar.
2. **Batterieverhalten im Winter** (SOC-Verlauf über Tage). Der
   Nachtentladungsanteil (0,8) ist aus dem September-Log abgeleitet;
   im Winter mit wenig PV und kaltem Speicher kann die Batterie
   durchaus tagsüber entladen.
3. **Reale Prognose-Stundenwerte** (`forecast_hourly_wm2`) für
   Wintertage. Aktuell bildet die Simulation die Prognose aus dem
   PV-Profil; eine echte Prognose weicht davon ab (Wolken), und genau
   diese Abweichung ist der Gegenstand der CalcStart-Untersuchung.
   Das Stundenprofil wird heute **nicht protokolliert** – es müsste
   mitgeschrieben werden (siehe unten).

**Erledigt:** Die Hauslast ist inzwischen aus den Winterlogs
gemessen (Basislast nachts ~250 W, Heizlast 2,1–2,5 kW während der
Heizstunden) – siehe `Analyse/merged_data.csv`, Monate 12 und 01.

### Was zum Mitschreiben fehlt

Für einen vollständigen Winter-Datensatz braucht es die Rohwerte des
Betriebslogs im Format von `logs/24.9/heizungsdaten.csv`
(Spalten: `Zeitstempel`, `T_Oben`, `T_Unten`, `T_Mittig`, `T_Boiler`,
`T_Verd`, `Kompressor`, `ACPower`, `FeedinPower`, `BatPower`, `SOC`,
`PowerDC1`, `PowerDC2`, `ConsumeEnergy`, `Einschaltpunkt`,
`Ausschaltpunkt`, `Solarüberschuss`, `Urlaubsmodus`, `PowerSource`,
`Prognose_Morgen`) – im 14-Sekunden-Raster.

Der Raster ist dabei belanglos, solange er **gleichmäßig** ist: die
Wechselrichter-Werte werden nur alle ~5 min gemeldet, die restlichen
Zeilen wiederholen den letzten Wert (das ist kein Datenfehler, sondern
das Sendeintervall des Geräts – der September-Log hat denselben Anteil
von 96 %). Entscheidend ist nur, dass die Datei den **ganzen Tag** abdeckt
(Stunden 0–23 gleichmäßig belegt).

### Zielkonflikt am Abend (20.10.2026)

Die Analyse des Abendverlaufs (welche Regel heizt wann) hat zwei
unabhängige Blocker gefunden:

1. **Die Abendgarantie endet zu früh.** `MinTemp-Abend-Mitte` (17–22 Uhr)
   hat `nachtsperre_ueberschrieben: false` – die Garantie gilt nur bis
   zum Nachtsperren-Beginn (19 Uhr). Im echten Dezember-Log fällt
   `T_Mittig` von 44,4 °C (19 Uhr) auf 33,7 °C (22 Uhr); an 19 von 22
   Tagen lag der Fühler abends unter 42 °C.
2. **Die Start-Antizipation blockiert den Start.** Sie rechnet den
   freien Hub bis zum Ausschaltpunkt gegen die Mindestlaufzeit: bei
   ~4,3 K/h und 1,0 K Hub sind das 15 min gegenüber 60 min
   Mindestlaufzeit – der Start wird nicht freigegeben.

**Die Wirkung beider Faktoren (Simulation, 14 Tage, wenig PV):**

| Variante | Netz kWh/Tag | h mitte < 40 °C | Zyklen |
| --- | --- | --- | --- |
| Basis | 1,54 | 113,9 | 24 |
| nur Abendgarantie bis 22 h | 1,54 | 113,9 | 24 |
| nur Antizipation aus | 1,56 | 81,4 | 27 |
| **beides** | 1,60 | **45,1** | 30 |

Die Abendgarantie allein bewirkt **nichts**, weil sie nur einschaltet,
wenn der Fühler bereits unter 42 °C liegt – und das passiert erst nach
19 Uhr, wo sie stumm ist. Der volle Effekt braucht beides. Die
Entscheidung (Duschkomfort gegen Netzstrom) ist eine Nutzerfrage; Details
im `REGELWERK.md`, Abschnitt 3.5.
