from pathlib import Path

path = Path(r'c:\Python\WPSteuerung\WPSteuerung\Steuerung\tests\test_boiler_max.py')
text = path.read_text(encoding='utf-8')

# Fix lines 175-176 pattern (with t_oben)
old = '''        t_oben=state.sensors.t_oben,
        set_kompressor_status_func=_set_status_sammler(calls),'''
new = '''        t_oben=state.sensors.t_oben,
        t_mittig=state.sensors.t_mittig,
        set_kompressor_status_func=_set_status_sammler(calls),'''
text = text.replace(old, new)
path.write_text(text, encoding='utf-8')
print("Fixed!")
