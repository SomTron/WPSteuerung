# Verbesserter Log-Analysereport

**Quelle:** `heizungssteuerung_upload_6.9.2026-14.9.txt`  
**Ausgabe:** `C:\Python\WPSteuerung\WPSteuerung\logs\analyse_2026-09-08_14.09`

## 1. Log-Volumen & Verarbeitung

- Zeilen gesamt (Rohdatei): **37540**  
- Davon Regel-Detailzeilen (15er-Bloecke): **26730** = **71.2 %** (Redundanz)
- Regel-Bewertungs-Bloecke: **1782**  
- Level: INFO 6279 / DEBUG 3245 / WARNING 91 / ERROR 352

## 2. Tages-KPIs

| Tag | Starts | Laufzeit (min) | davon Netz | davon PV/Bat | Kurzzyklen (<20m) | BoilerMax | Overshoot | Prognose heute |
|---|---|---|---|---|---|---|---|---|
| 2026-09-08 | 10 | 266 | 62 | 204 | 6 | 1 | 4 | 5.34 |
| 2026-09-09 | 8 | 138 | 100 | 39 | 6 | 0 | 0 | 2.83 |
| 2026-09-10 | 4 | 116 | 64 | 51 | 2 | 0 | 0 | 0.59 |
| 2026-09-11 | 3 | 227 | 227 | 0 | 1 | 1 | 1 | 2.15 |
| 2026-09-12 | 14 | 240 | 52 | 188 | 12 | 2 | 7 | 5.25 |
| 2026-09-13 | 9 | 196 | 0 | 196 | 6 | 0 | 2 | 4.66 |
| 2026-09-14 | 4 | 336 | 136 | 200 | 2 | 0 | 0 | 3.36 |

## 3. Zyklen-Analyse

- Zyklen gesamt: **52**
- Gesamtlaufzeit: **1517 min** (= 25.3 h)
- Median: **15 min**, min 6, max 190
- Quelle: {'netz': 17, 'pv': 35}
- Abschaltgruende: {'keine_regel': 7, 'unbekannt': 24, 'regel:Einspeisung': 8, 'boiler_max': 2, 'regel:Komfort': 3, 'wechsel:Einspeisung': 4, 'verifizierung_fehler': 1, 'legionellen_timeout': 1, 'regel:Batterie': 1, 'wechsel:Batterie': 1}
- **Kurzzyklen (< 20 min): 35** (67 %)
    - 08.09 08:58 | 15 min | Regel Abweichung | Ende: unbekannt
    - 08.09 12:12 | 10 min | Regel Einspeisung | Ende: regel:Einspeisung
    - 08.09 13:33 | 10 min | Regel Einspeisung | Ende: regel:Einspeisung
    - 08.09 14:21 | 9 min | Regel Einspeisung | Ende: boiler_max
    - 08.09 15:58 | 10 min | Regel AdaptivePV | Ende: regel:Einspeisung
    - 08.09 17:48 | 10 min | Regel AdaptivePV | Ende: regel:Einspeisung
    - 09.09 08:43 | 15 min | Regel Abweichung | Ende: regel:Komfort
    - 09.09 10:14 | 15 min | Regel Abweichung | Ende: unbekannt
    - 09.09 11:20 | 15 min | Regel Forecast | Ende: regel:Komfort
    - 09.09 14:26 | 15 min | Regel AdaptivePV | Ende: unbekannt
    - 09.09 16:01 | 14 min | Regel AdaptivePV | Ende: wechsel:Einspeisung
    - 09.09 18:04 | 10 min | Regel AdaptivePV | Ende: unbekannt

## 4. Overshoot / BOILERMAX (Kritischste Stelle)

- Zyklen mit `unten max >= 48.5°C`: **15**
    - 11.09 09:18 | Dauer 162.4 min | max unten 57.2°C | Regeln Legionellen -> legionellen_timeout
    - 12.09 14:54 | Dauer 7.6 min | max unten 49.6°C | Regeln Einspeisung -> regel:Einspeisung
    - 12.09 14:09 | Dauer 7.8 min | max unten 49.5°C | Regeln Einspeisung -> boiler_max
    - 12.09 16:21 | Dauer 10.1 min | max unten 49.5°C | Regeln AdaptivePV -> regel:Einspeisung
    - 08.09 14:21 | Dauer 9.4 min | max unten 49.1°C | Regeln Einspeisung -> boiler_max
    - 12.09 18:01 | Dauer 12.4 min | max unten 49.1°C | Regeln AdaptivePV -> wechsel:Einspeisung
    - 13.09 15:11 | Dauer 10.0 min | max unten 49.1°C | Regeln Einspeisung -> unbekannt
    - 08.09 15:58 | Dauer 10.1 min | max unten 49.0°C | Regeln AdaptivePV -> regel:Einspeisung
    - 12.09 12:44 | Dauer 10.1 min | max unten 49.0°C | Regeln AdaptivePV -> unbekannt
    - 08.09 17:48 | Dauer 10.1 min | max unten 48.8°C | Regeln AdaptivePV -> regel:Einspeisung
    - 13.09 13:20 | Dauer 10.0 min | max unten 48.8°C | Regeln AdaptivePV -> unbekannt
    - 08.09 13:33 | Dauer 10.0 min | max unten 48.6°C | Regeln Einspeisung -> regel:Einspeisung
    - 12.09 10:57 | Dauer 10.0 min | max unten 48.6°C | Regeln AdaptivePV -> regel:Einspeisung
    - 12.09 13:05 | Dauer 17.8 min | max unten 48.6°C | Regeln Einspeisung -> unbekannt
    - 08.09 12:12 | Dauer 10.1 min | max unten 48.5°C | Regeln Einspeisung -> regel:Einspeisung
- **Geschaetzte verschenkte PV-Nutzung nach BOILERMAX:**
    - 08.09 14:30–15:58: Kuehlphase 88.1 min, davon schaetzbar **84.5 min** mit >= 500 W Einspeisung
    - 11.09 12:00–00:00: Kuehlphase 720.0 min, davon schaetzbar **348.7 min** mit >= 500 W Einspeisung
    - 12.09 14:17–14:54: Kuehlphase 37.2 min, davon schaetzbar **33.4 min** mit >= 500 W Einspeisung
    - 12.09 15:02–16:21: Kuehlphase 78.8 min, davon schaetzbar **78.8 min** mit >= 500 W Einspeisung

## 5. Morgendlicher Netzbezug vs. Prognose

| Tag | Start | Start-Regel | Quelle | Prognose heute (kWh/m²) |
|---|---|---|---|---|
| 2026-09-08 | 08:00 | Abweichung | netz | 5.34 |
| 2026-09-08 | 08:58 | Abweichung | netz | 5.34 |
| 2026-09-08 | 09:21 | AdaptivePV | pv | 5.34 |
| 2026-09-08 | 11:08 | AdaptivePV | pv | 5.34 |
| 2026-09-09 | 08:00 | Abweichung | netz | 2.83 |
| 2026-09-09 | 08:43 | Abweichung | netz | 2.83 |
| 2026-09-09 | 10:14 | Abweichung | netz | 2.83 |
| 2026-09-09 | 11:20 | Forecast | netz | 2.83 |
| 2026-09-10 | 08:00 | Abweichung | netz | 0.59 |
| 2026-09-10 | 08:48 | Abweichung | netz | 0.59 |
| 2026-09-10 | 10:50 | Abweichung | netz | 0.59 |
| 2026-09-11 | 08:00 | Legionellen | netz | 2.15 |
| 2026-09-11 | 08:56 | Legionellen | netz | 2.15 |
| 2026-09-11 | 09:18 | Legionellen | netz | 2.15 |
| 2026-09-12 | 09:00 | Abweichung | netz | 5.25 |
| 2026-09-12 | 09:27 | Abweichung | netz | 5.25 |
| 2026-09-12 | 10:57 | AdaptivePV | pv | 5.25 |
| 2026-09-12 | 11:36 | AdaptivePV | pv | 5.25 |
| 2026-09-13 | 10:12 | AdaptivePV | pv | 4.66 |
| 2026-09-13 | 11:53 | AdaptivePV | pv | 4.66 |
| 2026-09-14 | 08:00 | Abweichung | netz | 3.36 |
| 2026-09-14 | 10:06 | Abweichung | netz | 3.36 |
| 2026-09-14 | 10:34 | AdaptivePV | pv | 3.36 |

## 6. ZU-FRUEH-Ereignisse (Lernsignal)

- 08.09 09:32:13: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-08T08:47:10.782423) kommen 2940W Einspeisung
- 08.09 09:59:24: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-08T09:14:10.805462) kommen 3857W Einspeisung
- 09.09 14:24:39: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-09T13:39:38.875970) kommen 1852W Einspeisung
- 12.09 10:01:09: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-12T09:16:05.879725) kommen 2343W Einspeisung
- 13.09 10:47:47: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-13T10:02:45.939616) kommen 3277W Einspeisung
- 14.09 10:46:14: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-14T10:01:01.770866) kommen 4789W Einspeisung
- 14.09 11:06:42: Learning: ZU FRUEH geheizt - 45 min nach Nicht-PV-Zyklus (2026-09-14T10:21:32.035548) kommen 7568W Einspeisung

## 7. Warnungen/Fehler (stabile Codes)

| Code | Anzahl | | Code | Anzahl |
|---|---|---|---|---|
| ERR_SOLAX_API | 145 | WARN_TEMP_NORMALBEREICH | 6 |
| ERR_LOOP_DURCHLAUF | 115 | SONSTIGES | 5 |
| CYCLE_END_WE | 108 | WARN_BOILERMAX | 4 |
| CYCLE_START_WE | 94 | WARN_CALCSTART_NACHSPERRE | 4 |
| ERR_NETZWERK_ALLE_VERSUCHE | 61 | ERR_HEALTHCHECK | 4 |
| ERR_FALLBACK_DATEN | 48 | WARN_TAKTSCHUTZ | 3 |
| FORECAST | 31 | ERR_TELEGRAM_SENDEN | 3 |
| ERR_FORECAST | 24 | WARN_LCD | 2 |
| ERR_TELEGRAM_UPDATES | 11 | ERR_SENSOR | 1 |
| WARN_ZU_FRUEH | 7 |

### 7b. Stale-Daten-Phasen (Entscheidungen auf veralteten PV-Daten)

- Warnungen 'Solar-Daten veraltet' (waehrend Ausfall alle 5 min): **0**
- Keine Stale-Phasen im analysierten Zeitraum.

## 8. Regel-Einschalt-Haeufigkeit (alle Bewertungen)

| Regel | EIN | AUS |
|---|---|---|
| AdaptivePV | 154 | 174 |
| Batterie | 144 | 286 |
| Abweichung | 105 | 587 |
| Einspeisung | 104 | 174 |
| Forecast | 68 | 0 |
| Legionellen | 47 | 0 |
| Komfort | 9 | 599 |
| MinTemp-Abend-Mitte | 7 | 0 |
| Notfallschutz | 3 | 0 |
| MinTemp-Mittag-Oben | 3 | 0 |
| Wochenende | 0 | 211 |
| CalcStart | 0 | 174 |
