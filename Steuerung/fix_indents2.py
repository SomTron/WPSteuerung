"""Fix indentation errors with exact byte patterns."""
import sys, subprocess, py_compile

with open('priority_control_logic.py', 'rb') as f:
    d = f.read()

changes = 0

# 1. _extract_einschaltpunkt - the exact bytes
# Current bad code:
old1 = b'    elif name == "Einspeisung":\r\n            return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert\r\n        elif name == "Legionellen":\r\n            return config.legionellen.target_temp_c - 5.0  # EIN unter 55C\r\n\r\n        return config.sicherheit.max_temp_c\r\n\r\n\r\ndef _extract_ausschaltpunkt'

new1 = b'    elif name == "Einspeisung":\r\n        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert\r\n    elif name == "Legionellen":\r\n        return config.legionellen.target_temp_c - 5.0  # EIN unter 55C\r\n\r\n    return config.sicherheit.max_temp_c\r\n\r\n\r\ndef _extract_ausschaltpunkt'

if old1 in d:
    d = d.replace(old1, new1, 1)
    changes += 1
    print('1. Fixed _extract_einschaltpunkt')
else:
    print('1. FAIL: _extract_einschaltpunkt pattern not found')
    # Debug
    idx = d.find(b'elif name == "Einspeisung"')
    if idx >= 0:
        print(f'  Found at {idx}: {repr(d[idx:idx+150])}')

# 2. _extract_ausschaltpunkt
old2 = b'    elif name == "Einspeisung":\r\n            return config.einspeisung.ausschalten_bei_c\r\n        elif name == "Legionellen":\r\n            return config.legionellen.target_temp_c\r\n\r\n        return config.sicherheit.max_temp_c\r\n\r\n\r\n\r\ndef _gelerntes_morgenfenster'

new2 = b'    elif name == "Einspeisung":\r\n        return config.einspeisung.ausschalten_bei_c\r\n    elif name == "Legionellen":\r\n        return config.legionellen.target_temp_c\r\n\r\n    return config.sicherheit.max_temp_c\r\n\r\n\r\n\r\ndef _gelerntes_morgenfenster'

if old2 in d:
    d = d.replace(old2, new2, 1)
    changes += 1
    print('2. Fixed _extract_ausschaltpunkt')
else:
    print('2. FAIL: _extract_ausschaltpunkt pattern not found')
    idx = d.find(b'elif name == "Einspeisung"', d.find(b'_extract_ausschaltpunkt'))
    if idx >= 0:
        print(f'  Found at {idx}: {repr(d[idx:idx+150])}')

# 3. _boiler_max_info docstring
old3 = b'    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.\r\n        Bei aktiver Legionellenprophylaxe wird das Limit auf\r\n        legionellen_max_temp_c erhoeht (wenn gesetzt).\r\n        """'

new3 = b'    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.\r\n    Bei aktiver Legionellenprophylaxe wird das Limit auf\r\n    legionellen_max_temp_c erhoeht (wenn gesetzt).\r\n    """'

if old3 in d:
    d = d.replace(old3, new3, 1)
    changes += 1
    print('3. Fixed _boiler_max_info docstring')
else:
    print('3. FAIL: docstring not found')
    idx = d.find(b'Legionellenprophylaxe')
    if idx >= 0:
        print(f'  Found at {idx}: {repr(d[idx-60:idx+80])}')

# 4. _boiler_max_info code block
old4 = b'    # Standard-Limit aus Config\r\n        limit = float(getattr(cfg, "max_temp_c", 48.0))\r\n        # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der\r\n        # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum\r\n        # zuschlaegt.\r\n        legionellen_limit = getattr(state, "legionellen_temp_override", None)\r\n        if legionellen_limit is not None and legionellen_limit > limit:\r\n            limit = legionellen_limit\r\n\r\n        wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))\r\n        return temp, limit, wiederein, fuehler'

new4 = b'    # Standard-Limit aus Config\r\n    limit = float(getattr(cfg, "max_temp_c", 48.0))\r\n    # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der\r\n    # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum\r\n    # zuschlaegt.\r\n    legionellen_limit = getattr(state, "legionellen_temp_override", None)\r\n    if legionellen_limit is not None and legionellen_limit > limit:\r\n        limit = legionellen_limit\r\n\r\n    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))\r\n    return temp, limit, wiederein, fuehler'

if old4 in d:
    d = d.replace(old4, new4, 1)
    changes += 1
    print('4. Fixed _boiler_max_info code')
else:
    print('4. FAIL: boiler code not found')
    idx = d.find(b'Standard-Limit aus Config')
    if idx >= 0:
        print(f'  Found at {idx}: {repr(d[idx:idx+250])}')

# 5. bewerte_alle_regeln call
old5 = b'                        recent_usage_events=recent_usage_events,\r\n                                                bademodus_aktiv=bool(state.bademodus_aktiv),\r\n                                                legionellen_aktiv=bool(state.legionellen_aktiv),\r\n                                                legionellen_last_done=state.legionellen_last_done,\r\n                                                legionellen_started_at=state.legionellen_started_at,\r\n                                                forecast_day2_wh_qm=getattr(state.solar, \'forecast_day2\', None),\r\n                                            )'

new5 = b'                        recent_usage_events=recent_usage_events,\r\n                        bademodus_aktiv=bool(state.bademodus_aktiv),\r\n                        legionellen_aktiv=bool(state.legionellen_aktiv),\r\n                        legionellen_last_done=state.legionellen_last_done,\r\n                        legionellen_started_at=state.legionellen_started_at,\r\n                        forecast_day2_wh_qm=getattr(state.solar, \'forecast_day2\', None),\r\n                    )'

if old5 in d:
    d = d.replace(old5, new5, 1)
    changes += 1
    print('5. Fixed bewerte_alle_regeln call')
else:
    print('5. FAIL: bewerte_alle_regeln not found')
    idx = d.find(b'bademodus_aktiv')
    if idx >= 0:
        print(f'  Found at {idx}: {repr(d[idx-50:idx+200])}')

if changes == 0:
    print('NO CHANGES MADE - aborting!')
    sys.exit(1)

with open('priority_control_logic.py', 'wb') as f:
    f.write(d)
print(f'\n{changes} changes applied!')

# Run black
result = subprocess.run([sys.executable, '-m', 'black', 'priority_control_logic.py'], capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print('STDERR:', result.stderr[:500])

# Verify
try:
    py_compile.compile('priority_control_logic.py', doraise=True)
    print('Compilation: OK!')
except py_compile.PyCompileError as e:
    print(f'Compilation FAILED: {str(e)[:300]}')