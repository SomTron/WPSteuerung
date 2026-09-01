"""Fix indentation errors in priority_control_logic.py using raw byte replacement."""
with open('priority_control_logic.py', 'rb') as f:
    content = f.read()

changes = 0

# 1. Fix _extract_einschaltpunkt - Legionellen block
old1 = b'''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert
        elif name == "Legionellen":
            return config.legionellen.target_temp_c - 5.0  # EIN unter 55C

        return config.sicherheit.max_temp_c'''

new1 = b'''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert
    elif name == "Legionellen":
        return config.legionellen.target_temp_c - 5.0  # EIN unter 55C

    return config.sicherheit.max_temp_c'''

if old1 in content:
    content = content.replace(old1, new1, 1)
    changes += 1
    print(f'1. Fixed _extract_einschaltpunkt')
else:
    print(f'1. FAIL: _extract_einschaltpunkt pattern not found')

# 2. Fix _extract_ausschaltpunkt - Legionellen block
old2 = b'''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c
        elif name == "Legionellen":
            return config.legionellen.target_temp_c

        return config.sicherheit.max_temp_c'''

new2 = b'''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c
    elif name == "Legionellen":
        return config.legionellen.target_temp_c

    return config.sicherheit.max_temp_c'''

if old2 in content:
    content = content.replace(old2, new2, 1)
    changes += 1
    print(f'2. Fixed _extract_ausschaltpunkt')
else:
    print(f'2. FAIL: _extract_ausschaltpunkt pattern not found')

# 3. Fix _boiler_max_info docstring indentation
old3 = b'''    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
        Bei aktiver Legionellenprophylaxe wird das Limit auf
        legionellen_max_temp_c erhoeht (wenn gesetzt).
        """'''

new3 = b'''    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
    Bei aktiver Legionellenprophylaxe wird das Limit auf
    legionellen_max_temp_c erhoeht (wenn gesetzt).
    """'''

if old3 in content:
    content = content.replace(old3, new3, 1)
    changes += 1
    print(f'3. Fixed _boiler_max_info docstring')
else:
    print(f'3. FAIL: docstring pattern not found')

# 4. Fix _boiler_max_info code-block indentation
old4 = b'''    # Standard-Limit aus Config
        limit = float(getattr(cfg, "max_temp_c", 48.0))
        # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der
        # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum
        # zuschlaegt.
        legionellen_limit = getattr(state, "legionellen_temp_override", None)
        if legionellen_limit is not None and legionellen_limit > limit:
            limit = legionellen_limit

        wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
        return temp, limit, wiederein, fuehler'''

new4 = b'''    # Standard-Limit aus Config
    limit = float(getattr(cfg, "max_temp_c", 48.0))
    # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der
    # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum
    # zuschlaegt.
    legionellen_limit = getattr(state, "legionellen_temp_override", None)
    if legionellen_limit is not None and legionellen_limit > limit:
        limit = legionellen_limit

    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
    return temp, limit, wiederein, fuehler'''

if old4 in content:
    content = content.replace(old4, new4, 1)
    changes += 1
    print(f'4. Fixed _boiler_max_info code-block')
else:
    print(f'4. FAIL: boiler_max_info code-block pattern not found')

# 5. Fix bewerte_alle_regeln call indentation
old5 = b'''                        recent_usage_events=recent_usage_events,
                                                bademodus_aktiv=bool(state.bademodus_aktiv),
                                                legionellen_aktiv=bool(state.legionellen_aktiv),
                                                legionellen_last_done=state.legionellen_last_done,
                                                legionellen_started_at=state.legionellen_started_at,
                                                forecast_day2_wh_qm=getattr(state.solar, 'forecast_day2', None),
                                            )'''

new5 = b'''                        recent_usage_events=recent_usage_events,
                        bademodus_aktiv=bool(state.bademodus_aktiv),
                        legionellen_aktiv=bool(state.legionellen_aktiv),
                        legionellen_last_done=state.legionellen_last_done,
                        legionellen_started_at=state.legionellen_started_at,
                        forecast_day2_wh_qm=getattr(state.solar, 'forecast_day2', None),
                    )'''

if old5 in content:
    content = content.replace(old5, new5, 1)
    changes += 1
    print(f'5. Fixed bewerte_alle_regeln call')
else:
    print(f'5. FAIL: bewerte_alle_regeln pattern not found')

if changes == 0:
    print('NO CHANGES MADE!')
    # Debug: find the problematic areas
    idx = content.find(b'Legionellen')
    if idx >= 0:
        print(f'Found "Legionellen" at byte {idx}')
        print(repr(content[max(0,idx-30):idx+100]))
    sys.exit(1)

with open('priority_control_logic.py', 'wb') as f:
    f.write(content)
print(f'\n{changes} changes applied successfully!')

# Now run black on it
import subprocess
import sys
result = subprocess.run([sys.executable, '-m', 'black', 'priority_control_logic.py'], capture_output=True, text=True)
print(result.stdout)
if result.stderr:
    print('STDERR:', result.stderr[:500])

# Verify compilation
import py_compile
try:
    py_compile.compile('priority_control_logic.py', doraise=True)
    print('Compilation: OK!')
except py_compile.PyCompileError as e:
    print(f'Compilation FAILED: {str(e)[:300]}')