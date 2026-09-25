# WPSteuerung – Steuerung

## Bekannte Test-Luecken (bewusst zurueckgestellt)

Analyse vom 25.09.2026: sieben Produktionsmodule werden von keinem Test
importiert. Drei davon sind inzwischen abgedeckt, vier bleiben offen.

### Erledigt

| Modul | Test |
|---|---|
| `rule_types.py` | `tests/test_rule_types.py` |
| `api_client.py` | `tests/test_api_client.py` |
| `hardware_interface.py` | `tests/test_hardware_interface.py` |

### Offen

| Modul | Zeilen | Warum es aufgeschoben ist |
|---|---:|---|
| `log_query.py` | 311 | wird ausschliesslich vom WP-Manager genutzt (Logabfragen nach Zeit/Dauer). Geringeres Risiko fuer die Steuerung, Fehler fallen aber nur auf dem Pi auf. |
| `telegram_charts.py` | 331 | befehlsgetrieben (Diagramm auf Anfrage). Fehler betreffen die Anzeige, nicht die Regelung. |
| `telegram_ui.py` | 68 | befehlsgetrieben, zusammen mit `telegram_charts.py` zu behandeln. |

Empfohlene Reihenfolge, wenn Zeit ist:

1. `log_query.py` – Datums-/Zeitfenster-Parsing ist fehleranfaellig
   (Zeitzonen, Sommerzeit, CSV-Header) und wirkt direkt auf die
   WP-Manager-Optionen 11 und 12.
2. `telegram_charts.py` und `telegram_ui.py` – zusammen behandeln, beide
   sind befehlsgetrieben und daher nicht zeitkritisch.

### Zusaetzlich erledigt

- `constants_clean.py` (0-Byte-Altlast) geloescht
- `tests/test_repo_hygiene.py` prueft jetzt leere Dateien und BOMs

