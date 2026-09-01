with open('state.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """        self.sommer_letzter_bewertungstag: Optional[date] = None  # Kalendertag der letzten guten Bewertung (Serienschutz)
        
        # System/Internal"""

new = """        self.sommer_letzter_bewertungstag: Optional[date] = None  # Kalendertag der letzten guten Bewertung (Serienschutz)
        
        # Legionellenprophylaxe-Felder
        self.legionellen_aktiv: bool = False
        self.legionellen_last_done: Optional[date] = None  # Letzte Durchfuehrung (Datum)
        self.legionellen_started_at: Optional[datetime] = None  # Startzeit der aktiven Prophylaxe
        self.legionellen_target_reached_at: Optional[datetime] = None  # Zeitpunkt der Zielerreichung
        self.legionellen_wochennummer: Optional[int] = None  # In welcher Kalenderwoche wurde zuletzt gemacht?
        self.legionellen_planned_day: Optional[str] = None  # Geplanter Wochentag
        self.legionellen_planned_time: Optional[str] = None  # Geplante Uhrzeit
        self.legionellen_planned_reason: Optional[str] = None  # Grund fuer die Wahl
        self.legionellen_telegram_start_sent: bool = False
        self.legionellen_telegram_done_sent: bool = False
        self.legionellen_temp_override: Optional[float] = None  # Uebersteuert max_temp_c waehrend aktiver Prophylaxe
        
        # System/Internal"""

if old in content:
    content = content.replace(old, new)
    with open('state.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('OK: state.py updated')
else:
    print('FAILED: state.py - old text not found')
    idx = content.find('sommer_letzter_bewertungstag')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+200]))