"""Kompatibilitaets-Fassade: Nur noch die von main.py live genutzten Funktionen.

Die Pareto-Prioritaeten-Engine (priority_control_logic + priority_control)
hat die alte modusbasierte Logik vollstaendig ersetzt. Die fruehere
Implementierung liegt unveraendert unter archive/legacy_control_logic.py
(wird nicht mehr importiert - nur Referenz; Git-Historie haelt den Rest).

Live genutzt (daher hier re-exportiert):
- verify_compressor_running  (safety_logic)  - Kompressor-Verifizierung
- check_sensors_and_safety   (safety_logic)  - Sensor- und Sicherheitspruefungen
- is_solar_window            (logic_utils)   - Solarfenster (LCD/Telegram-Status)
- send_telegram_message      (telegram_api)  - Einmal-Alarme

Neue Features gehoeren in das Prioritaeten-System, nicht hierher.
"""

from logic_utils import (
    is_solar_window,  # noqa: F401 - von main.py genutzt (LCD/Telegram-Status)
)
from safety_logic import (
    check_sensors_and_safety,  # noqa: F401 - von main.py genutzt
    verify_compressor_running,  # noqa: F401 - von main.py genutzt
)

try:
    from telegram_api import send_telegram_message  # noqa: F401 - von main.py genutzt
except ImportError:
    pass
