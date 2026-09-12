from pathlib import Path

def fix_file(filepath, old_pattern, new_pattern):
    text = Path(filepath).read_text(encoding='utf-8')
    new_text = text.replace(old_pattern, new_pattern)
    if new_text != text:
        Path(filepath).write_text(new_text, encoding='utf-8')
        print(f"Fixed {filepath}")
        return True
    print(f"No changes needed for {filepath}")
    return False

# Fix test_boiler_max.py
boiler_path = r'c:\Python\WPSteuerung\WPSteuerung\Steuerung\tests\test_boiler_max.py'

# Add t_mittig to handle_compressor_on calls with [] 
boiler_on_old = 't_oben=state.sensors.t_oben,\n        set_kompressor_status_func=_set_status_sammler([]),'
boiler_on_new = 't_oben=state.sensors.t_oben,\n        t_mittig=state.sensors.t_mittig,\n        set_kompressor_status_func=_set_status_sammler([]),'
fix_file(boiler_path, boiler_on_old, boiler_on_new)

# Add t_mittig to handle_compressor_on calls with (calls)
boiler_on2_old = 't_oben=state.sensors.t_oben,\n        set_kompressor_status_func=_set_status_sammler(calls),'
boiler_on2_new = 't_oben=state.sensors.t_oben,\n        t_mittig=state.sensors.t_mittig,\n        set_kompressor_status_func=_set_status_sammler(calls),'
fix_file(boiler_path, boiler_on2_old, boiler_on2_new)

# Remove t_mittig from handle_compressor_off calls (should not have it)
boiler_off_old = 't_oben=state.sensors.t_oben,\n        t_mittig=state.sensors.t_mittig,\n        set_kompressor_status_func=_set_status_sammler(calls),'
boiler_off_new = 't_oben=state.sensors.t_oben,\n        set_kompressor_status_func=_set_status_sammler(calls),'
fix_file(boiler_path, boiler_off_old, boiler_off_new)

# Fix test_neustartsperre.py
neustart_path = r'c:\Python\WPSteuerung\WPSteuerung\Steuerung\tests\test_neustartsperre.py'
neustart_old = 't_oben=40.0, set_kompressor_status_func=mock_set,'
neustart_new = 't_oben=40.0, t_mittig=50.0, set_kompressor_status_func=mock_set,'
fix_file(neustart_path, neustart_old, neustart_new)

print("Done!")
