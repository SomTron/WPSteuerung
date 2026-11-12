# WPSteuerung – Steuerung

## Bekannte Test-Luecken (bewusst zurueckgestellt)

Analyse vom 25.09.2026: sieben Produktionsmodule werden von keinem Test
importiert. Drei davon sind inzwischen abgedeckt, vier bleiben offen.

### Erledigt

| Modul | Test | Befund beim Schreiben |
|---|---|---|
| `rule_types.py` | `tests/test_rule_types.py` | Re-Inferenz war von der Zuweisungsreihenfolge abhaengig - behoben |
| `api_client.py` | `tests/test_api_client.py` | 4xx/5xx-Trennung war ungesichert |
| `hardware_interface.py` | `tests/test_hardware_interface.py` | fail-safe-Rueckmeldung war ungesichert |
| `log_query.py` | `tests/test_log_query.py` | `tail_log()` lieferte konstant eine Zeile zu wenig - behoben |
| `telegram_charts.py` | `tests/test_telegram_ui_charts.py` | RAM-Schutz (kein Top-Level-pandas) ungesichert |
| `telegram_ui.py` | `tests/test_telegram_ui_charts.py` | dynamische Tastatur und Formathelfer ungesichert |
| `thermal_modell.py` | `tests/test_thermal_modell.py` | Modell war nur indirekt (ueber Regel-Tests) abgedeckt - 21 Tests fuer Schichtung, Waermeeintrag, Verluste, Zapfung |

### Offen

Alle zuvor ungetesteten Produktionsmodule sind jetzt abgedeckt.
`thermal_modell.py` (dreischichtiges Speichermodell, genutzt von
`regel_helpers._etappen_plan`) hatte als einziges Produktionsmodul keinen
direkten Test-Import und ist seit 11/2026 mit 21 Tests abgedeckt.
Eine erneute Lueckenanalyse sollte laufen, bevor neue Kandidaten gesammelt werden.

### Zusaetzlich erledigt

- `constants_clean.py` (0-Byte-Altlast) geloescht
- `tests/test_repo_hygiene.py` prueft jetzt leere Dateien und BOMs
  (dabei zwei weitere BOM-Dateien gefunden: `api.py`, `api_server.py`)

