# Regelungslogik & PV-Strategien

Dieses Dokument beschreibt die intelligenten Strategien der Wärmepumpensteuerung zur Optimierung des Eigenverbrauchs und zur Reduzierung der Netzeinspeisung (Peak Shaving).

## Grundprinzipien

Die Steuerung unterscheidet zwischen drei Prioritäten:
1. **Komfort (Normalmodus)**: Sicherstellung der minimalen Zieltemperatur während des Tages.
2. **Effizienz (Batterieschonung)**: Nutzung von PV-Überschuss, aber unter Berücksichtigung des Hausverbrauchs und der Batterie.
3. **Peak Shaving (Netzentlastung)**: Gezieltes Hochfahren der WP bei vollem Akku oder hoher Einspeisung, um "Spitzen" zu kappen.

---

## 1. PV-Strategie-Klassifizierung

Basierend auf der Prognose für heute und morgen wird täglich eine Strategie gewählt:

| Heute | Morgen | Strategie | Verhalten |
| :--- | :--- | :--- | :--- |
| **HIGH** | **LOW** | **Aggressiv** | Heute "vortanken" (bis 55°C), da morgen wenig Sonne kommt. WP startet früh bei erstem Überschuss. |
| **HIGH** | **HIGH** | **Balanced (Peak Shaving)** | WP wartet morgens ab (Strategisches Warten), um die Mittagsspitze zu kappen. Ziel: 50-52°C. |
| **LOW** | **HIGH** | **Konservativ** | Heute nur Normalbetrieb (45°C), Batterie für die Nacht sparen. Morgen wird voll geladen. |
| **LOW** | **LOW** | **Vorsichtig** | Nur Normalbetrieb. WP läuft nur, wenn absolut nötig (Frostschutz/Komfort). |

---

## 2. Dynamische Deadline-Berechnung

Um sicherzustellen, dass das Wasser immer warm ist, berechnet das System eine "Deadline":

- **Formel**: `Deadline = Ende_Solarfenster - Benötigte_Aufheizzeit`
- **Aufheizzeit**: Wird berechnet aus `(Zieltemp - Ist-Temp) / Aufheizrate (ca. 2.0°C/h)`.
- **Verhalten**: Vor Erreichen der Deadline wartet die WP auf hohen Überschuss (Peak Shaving). Nach Erreichen der Deadline schaltet sie ein, um das Ziel rechtzeitig zu erreichen.

---

## 3. Fühler-Logik (Regelfühler)

Je nach Modus wechselt der Regelfühler automatisch:

- **Normal-/Nachtmodus**: `t_mittig` (schnelle Reaktion für Komfort).
- **Überschuss-/Bademodus**: `t_unten` (maximale thermische Beladung des Speichers).

---

## 4. Schwellenwerte (Beispiel)

- **Batterieladen > 600W**: Erster Trigger für Überschuss.
- **Einspeisung > 600W**: Sofortiger Start (Peak Shaving), wenn die Batterie voll ist oder nicht mehr schneller laden kann.
- **SOC < 15% (bzw. MIN_SOC)**: Stoppt Überschuss-Modus, um Hausversorgung zu priorisieren.
