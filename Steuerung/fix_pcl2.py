#!/usr/bin/env python3
"""Fix priority_control_logic.py - add Legionellen cases to extract functions."""

with open('priority_control_logic.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix _extract_einschaltpunkt
old1 = '''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert

    return config.sicherheit.max_temp_c'''

new1 = '''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert
    elif name == "Legionellen":
        return config.legionellen.target_temp_c - 5.0  # EIN unter 55C

    return config.sicherheit.max_temp_c'''

if old1 in content:
    content = content.replace(old1, new1)
    print('OK: _extract_einschaltpunkt')
else:
    print('FAILED: _extract_einschaltpunkt - finding actual text...')
    idx = content.find('def _extract_einschaltpunkt')
    end = content.find('def _extract_ausschaltpunkt', idx)
    section = content[idx:end]
    # Find the end of the function
    for line in section.split('\n'):
        if 'return config.sicherheit' in line:
            print('Found return line:', repr(line))
        if 'Einspeisung' in line:
            print('Found Einspeisung line:', repr(line))

# Fix _extract_ausschaltpunkt
old2 = '''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c

    return config.sicherheit.max_temp_c'''

new2 = '''    elif name == "Einspeisung":
        return config.einspeisung.ausschalten_bei_c
    elif name == "Legionellen":
        return config.legionellen.target_temp_c

    return config.sicherheit.max_temp_c'''

if old2 in content:
    content = content.replace(old2, new2)
    print('OK: _extract_ausschaltpunkt')
else:
    print('FAILED: _extract_ausschaltpunkt - finding actual text...')
    idx = content.find('def _extract_ausschaltpunkt')
    end = content.find('\n\n\n', idx)
    if end < 0:
        end = idx + 800
    section = content[idx:end]
    print(repr(section[:600]))

with open('priority_control_logic.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')