# Code-Verbesserungen – WPSteuerung

Erstellt am 03.03.2026. Status-Tracking für alle identifizierten Verbesserungen.

---

## 🔴 Priorität 1 – Bugs & Risiken

- [ ] **1. Fehlender `EXPECTED_CSV_HEADER` Import in `main.py`**
  - Zeile 414 in `log_system_state()` referenziert `EXPECTED_CSV_HEADER`, aber nur `HEIZUNGSDATEN_CSV` wird importiert (Zeile 28).
  - **Fix:** `from utils import safe_timedelta, HEIZUNGSDATEN_CSV, EXPECTED_CSV_HEADER`

- [ ] **2. Fehlender `logging` Import in `utils.py`**
  - `rotate_csv()` (Zeile 73) ruft `logging.info()` auf, aber `import logging` steht erst in Zeile 83 – nach der Funktion.
  - **Fix:** `import logging` an den Anfang der Datei verschieben.

- [ ] **3. Doppelter `import os` in `utils.py`**
  - `os` wird in Zeile 2 und Zeile 86 importiert. Alle Imports konsolidieren.

- [ ] **4. Fire-and-forget Tasks ohne Error-Handling (`safety_logic.py`)**
  - `asyncio.create_task()` in Zeile 13 und 117 ohne Exception-Callback. Fehler werden verschluckt.
  - **Fix:** Task-Referenz speichern und `add_done_callback()` verwenden.

---

## 🟡 Priorität 2 – Architektur & Wartbarkeit

- [ ] **5. Globaler `hardware_manager` statt Dependency Injection (`main.py`)**
  - `hardware_manager` ist global und wird direkt in Funktionen referenziert. Besser: in `State` kapseln.

- [ ] **6. `set_kompressor_status` als Top-Level-Funktion (`main.py`)**
  - 50-Zeilen-Funktion mit Seiteneffekten und Zugriff auf globale Objekte. Refactoring in Klasse empfohlen.

- [ ] **7. Redundante `nacht_reduction` Berechnung (`control_logic.py`)**
  - Wird in `determine_mode_and_setpoints` zweimal berechnet (Zeile 56 und 82). Die zweite überschreibt die erste.

- [ ] **8. `import re` innerhalb der Funktion (`main.py:308`)**
  - Bei jedem Schleifendurchlauf wird `re` importiert. Auf Modul-Ebene verschieben.

- [ ] **9. Hardkodierte Sensor-IDs (`sensors.py:27-33`)**
  - Sensor-IDs sollten in `config.ini` ausgelagert werden.

- [ ] **10. Mehrfaches identisches Time-Parsing (`logic_utils.py`)**
  - `parse_t()` wird in 3 Funktionen identisch definiert. Gemeinsame Utility-Funktion erstellen.

---

## 🟢 Priorität 3 – Code-Qualität

- [ ] **11. `patch_datetime` falsch platziert (`test_compressor_verification.py`)**
  - Hilfsfunktion wird vor Definition benutzt. An den Anfang verschieben.

- [ ] **12. Fehlender Top-Level Import (`test_compressor_verification.py`)**
  - `from unittest.mock import patch` fehlt als Top-Level-Import.

- [ ] **13. Bare `except:` Blöcke**
  - `main.py:494`, `main.py:501`, `logic_utils.py:93`, `logic_utils.py:116` – Exceptions werden verschluckt.
  - **Fix:** Mindestens `except Exception as e:` mit Logging.

- [ ] **14. Inkonsistente Sprache (Deutsch/Englisch)**
  - Konvention festlegen: z.B. Code/Variablen Englisch, Kommentare Deutsch.

- [ ] **15. Fehlende `__repr__`/`__str__` in State-Klassen (`state.py`)**
  - Debugging erschwert, da keine String-Repräsentation vorhanden.

- [ ] **16. Magic Numbers**
  - `2.0`/`0.2` in `safety_logic.py:99-100`, `timedelta(minutes=10)` in `main.py:360`, etc.
  - In Config oder benannte Konstanten auslagern.

---

## 🔧 Priorität 4 – Test-Verbesserungen

- [ ] **17. Fehlende Edge-Case-Tests (`test_compressor_verification.py`)**
  - Nicht getestet: `kompressor_ein=False`, `start_time=None`, Error Count ≥ 2, `bot_token=None`.

- [ ] **18. Fehlende Integration-Tests für `main_loop`**
  - Zusammenspiel von `update_system_data` → `run_logic_step` → `log_system_state` ist ungetestet.
