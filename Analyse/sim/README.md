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

## Szenarien

| Name | Beschreibung |
| --- | --- |
| `september` | PV-reich, echte Stundenprofile aus `logs/24.9` (~50 kWh/Tag) |
| `winter_wenig_pv` | Dezember, trüb, 700 Wh/m² (~6 kWh/Tag) |
| `winter_ohne_pv` | Dezember, Starknebel, 120 Wh/m², kein PV |
| `winter_misch` | Dezember, wechselnd, 1600 Wh/m² |
| `winter_ohne_pv_lang` | 5 Wochen ohne PV (bestehender Betrieb) |
| `winter_batterie_leer` | wenig PV, **Batterie wird nie voll** (SOC-Deckel 55 %, Start 20 %) |
| `winter_batterie_voll` | wenig PV, **Batterie kurzfristig voll** (Start 100 %, 3 kWh) |

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
* PV ist ein skaliertes Septemberprofil, kein synthetischer Sonnenstand.
* Aussagen sind **relativ**: Der Vergleich Basis ↔ Variante läuft mit
  identischem Zufalls-Seed und ist damit belastbar; absolute kWh-Zahlen
  haben die Genauigkeit des Modells (±1–2 K Temperatur, wenige Prozent Energie).
