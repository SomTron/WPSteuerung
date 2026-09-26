"""Mehrtages-Simulation der Wärmepumpen-Regelung.

Die Simulation treibt die **echte** Regelungslogik aus ``Steuerung/``
(``priority_control_logic`` / ``priority_control`` / ``learning_engine``)
ueber mehrere Tage. Nur Hardware, Telegram, Solax-API und Datei-Logging
werden durch Attrappen ersetzt; Zeit, Sensoren, PV, Batterie und Hauslast
kommen aus dem Speichermodell bzw. dem Szenario.

Aufruf::

    py -3 Analyse/sim/calibrate.py      # Speichermodell gegen logs/24.9 fitten
    py -3 Analyse/sim/run.py            # alle Szenarien simulieren
    py -3 Analyse/sim/varianten.py      # Konfigurationen vergleichen

Module
------
``thermal``    Dreischichtiger Waermespeicher mit Zapfung und Verlusten
``umgebung``   PV-Profile, Hauslast, Batteriemodell, Szenariendefinitionen
``harness``    Simulations-Harness (bindet die Produktionslogik ein)
``report``     KPIs, Tagesuebersicht, Diagnose
``run``        CLI fuer einzelne Szenarien
``varianten``  CLI fuer den Konfigurationsvergleich
``calibrate``  Parameter-Fit und Validierung gegen die Realdaten
"""
