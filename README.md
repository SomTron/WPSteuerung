Das Programm ist eine umfassende Steuerungssoftware für eine Heizungsanlage (z.B. Wärmepumpe) auf einem Raspberry Pi. Es integriert Temperaturmessung, Kompressorsteuerung, Solarüberschuss-Nutzung, Telegram-Benachrichtigungen und Visualisierung. Entwickelt in Python mit asyncio für asynchrone Abläufe, überwacht es kontinuierlich Sensoren, steuert Hardware über GPIO und speichert Daten in einer CSV-Datei. Die Bedienung erfolgt über eine Telegram-Schnittstelle mit benutzerdefinierten Befehlen.
Hauptfunktionen

    Temperaturmessung und Überwachung:
        Misst Temperaturen an drei Punkten (Boiler oben, Boiler hinten, Verdampfer) mit DS18B20-Sensoren.
        Prüft Plausibilität (z.B. -20°C bis 100°C) und meldet Fehler (z.B. "Fehler" bei Sensorproblemen).
    Kompressorsteuerung:
        Schaltet den Kompressor (GPIO 21) basierend auf Temperaturen, Druckschalter (GPIO 17) und Betriebsmodi ein/aus.
        Berücksichtigt Mindestlaufzeit (MIN_LAUFZEIT) und Mindestpausenzeit (MIN_PAUSE), um Hardware zu schonen.
    Betriebsmodi:
        Normalmodus: Einschalt- und Ausschaltpunkte basierend auf t_boiler_oben.
        PV-Überschuss-Modus: Nutzt Solarüberschuss (SolaxCloud-Daten) mit erhöhten Sollwerten und zusätzlichen Grenzen für t_boiler_hinten.
        Nachtmodus: Senkt Sollwerte um eine konfigurierte Nachtabsenkung.
        Urlaubsmodus: Reduziert Sollwerte für längere Abwesenheit.
    Telegram-Interaktion:
        Befehle wie "Temperaturen", "Status", "Verlauf 6h/24h", "Urlaub ein/aus", "Hilfe".
        Sendet Benachrichtigungen bei Fehlern (z.B. Druckschalter) oder Statusänderungen.
    Display-Anzeige:
        Zeigt auf einem 20x4 LCD (I2C) zyklisch Temperaturen, Kompressorstatus und Solax-Daten.
    Datenlogging und Visualisierung:
        Speichert Messwerte, Kompressorstatus und Solax-Daten minütlich in heizungsdaten.csv.
        Erstellt Diagramme (6h/24h) mit Temperaturen, Sollwerten und PV-Grenzen (falls relevant).
    SolaxCloud-Integration:
        Ruft PV-Daten (z.B. Batterieleistung, Einspeiseleistung) ab, um Solarüberschuss zu erkennen.
    Robustheit:
        Fehlerbehandlung für Sensoren, GPIO, API und LCD.
        Watchdog überwacht Zykluszeiten, um Hängenbleiben zu verhindern.

Steuerlogik im Detail

Die Steuerung läuft in der asynchronen Hauptschleife main_loop und basiert auf Temperaturen, Betriebsmodi und Sicherheitsbedingungen. Hier ist die Logik Schritt für Schritt:
1. Datenerfassung

    Temperaturen: t_boiler_oben, t_boiler_hinten, t_verd von DS18B20-Sensoren via read_temperature.
        Fehler (z.B. None) → t_boiler = "Fehler", sonst Durchschnitt von oben/hinten.
    Druckschalter: GPIO 17 (LOW = OK, HIGH = Fehler).
    Solax-Daten: API-Abruf alle 5 Minuten (zwischengespeichert), z.B. batPower, feedinpower, soc.

2. Betriebsmodi und Sollwerte

    Normalmodus (solar_ueberschuss_aktiv = False):
        aktueller_einschaltpunkt = EINSCHALTPUNKT - nacht_reduction (z.B. 42°C - 5°C = 37°C).
        aktueller_ausschaltpunkt = AUSSCHALTPUNKT - nacht_reduction (z.B. 45°C - 5°C = 40°C).
    PV-Modus (solar_ueberschuss_aktiv = True):
        Aktiviert, wenn batPower > 600W oder soc > 95% und feedinpower > 600W.
        aktueller_einschaltpunkt = EINSCHALTPUNKT - nacht_reduction (z.B. 42°C - 5°C = 37°C).
        aktueller_ausschaltpunkt = AUSSCHALTPUNKT_ERHOEHT - nacht_reduction (z.B. 52°C - 5°C = 47°C).
        Zusätzliche Grenzen: UNTERER_FUEHLER_MIN (z.B. 45°C) und UNTERER_FUEHLER_MAX (z.B. 50°C) für t_boiler_hinten.
    Nachtmodus: nacht_reduction (z.B. 5°C) wird zwischen NACHTABSENKUNG_START (z.B. 22:00) und NACHTABSENKUNG_END (z.B. 06:00) angewendet.
    Urlaubsmodus: Reduziert Sollwerte um URLAUBSABSENKUNG (z.B. 6°C), z.B. aktueller_ausschaltpunkt = 45°C - 6°C = 39°C.

3. Kompressorsteuerung

    Sicherheitsprüfungen:
        Druckschalter: Wenn pressure_ok = False, wird der Kompressor sofort ausgeschaltet (force_off=True), mit 5-minütiger Sperre.
        Verdampfertemperatur: Wenn t_verd < VERDAMPFERTEMPERATUR (z.B. 6°C), bleibt der Kompressor aus.
        Boiler-Sensoren: Bei Fühlerfehler, Übertemperatur (z.B. > 55°C) oder Differenz > 50°C wird ausgeschaltet.
    Normalmodus:
        Einschalten: t_boiler_oben < aktueller_einschaltpunkt (z.B. < 37°C).
        Ausschalten: t_boiler_oben >= aktueller_ausschaltpunkt (z.B. >= 40°C).
    PV-Modus:
        Einschalten: t_boiler_hinten < UNTERER_FUEHLER_MIN (z.B. < 45°C) UND t_boiler_oben < aktueller_ausschaltpunkt (z.B. < 47°C).
        Ausschalten: t_boiler_oben >= aktueller_ausschaltpunkt (z.B. >= 47°C) ODER t_boiler_hinten >= UNTERER_FUEHLER_MAX (z.B. >= 50°C).
    Zeitbeschränkungen:
        Mindestlaufzeit (z.B. 10 Min.): Verhindert Ausschalten, wenn < MIN_LAUFZEIT.
        Mindestpause (z.B. 20 Min.): Verhindert Einschalten, wenn < MIN_PAUSE seit letztem Ausschalten.

4. Laufzeitverfolgung

    current_runtime: Laufzeit, während der Kompressor läuft.
    last_runtime: Dauer der letzten Laufzeit.
    total_runtime_today: Summe aller Laufzeiten pro Tag, zurückgesetzt um Mitternacht.

5. Logging und Visualisierung

    CSV: Minütlich oder bei Statusänderung: Zeitstempel, Temperaturen, Kompressorstatus, Solax-Daten, Sollwerte, PV-Status, Nachtabsenkung.
    Diagramm:
        Zeigt T_Oben, T_Hinten, historische Einschaltpunkt/Ausschaltpunkt.
        UNTERER_FUEHLER_MIN/MAX nur bei PV-Modus im Zeitraum.
        Zeitraum (6h/24h) immer vollständig, Lücken bei fehlenden Daten.

6. Telegram-Befehle

    🌡️ Temperaturen: Aktuelle Werte.
    📊 Status: Temperaturen, Kompressorstatus, Laufzeiten, Sollwerte, aktive Modi.
    📈/📉 Verlauf: 6h/24h-Diagramm.
    🌴 Urlaub: Aktiviert Urlaubsmodus.
    🏠 Urlaub aus: Deaktiviert Urlaubsmodus.
    🆘 Hilfe: Befehlsübersicht.

Technische Details

    Hardware:
        Raspberry Pi mit GPIO (Kompressor: 21, Druckschalter: 17).
        DS18B20-Sensoren (1-Wire).
        I2C-LCD (20x4).
    Software:
        Python mit asyncio für parallele Tasks (main_loop, telegram_task, display_task).
        Bibliotheken: aiohttp, matplotlib, RPLCD, RPi.GPIO.
    Datenquellen:
        SolaxCloud-API für PV-Daten.
        config.ini für Einstellungen (z.B. Sollwerte, Telegram-Token).

Beispielablauf

    Normalmodus, Nacht:
        t_boiler_oben = 36°C, aktueller_einschaltpunkt = 37°C, aktueller_ausschaltpunkt = 40°C.
        Kompressor einschalten, da 36°C < 37°C.
        Nach 15 Min.: t_boiler_oben = 41°C > 40°C → ausschalten.
    PV-Modus, Tag:
        batPower = 700W, solar_ueberschuss_aktiv = True.
        t_boiler_oben = 46°C, t_boiler_hinten = 44°C, aktueller_ausschaltpunkt = 52°C, UNTERER_FUEHLER_MIN = 45°C.
        Einschalten, da 44°C < 45°C und 46°C < 52°C.
        Später: t_boiler_hinten = 51°C > UNTERER_FUEHLER_MAX = 50°C → ausschalten.
    Fehlerfall:
        Druckschalter HIGH → Kompressor aus, Telegram: "Druckfehler", 5 Min. Sperre.
