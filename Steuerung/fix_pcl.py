#!/usr/bin/env python3
# Fix priority_control_logic.py - update _boiler_max_info and _extract functions

with open('priority_control_logic.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update _boiler_max_info with legionellen override
old = '''def _boiler_max_info(state):
    """Infos zum harten Boiler-Maximum: (temp, limit, wiederein, fuehler).

    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
    """
    cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if cfg is None:
        return None, None, None, "unten"
    fuehler = getattr(cfg, "boiler_max_fuehler", None) or "unten"
    temp = getattr(getattr(state, "sensors", None), f"t_{fuehler}", None)
    if temp is None or not isinstance(temp, (int, float)):
        return None, None, None, fuehler
    limit = float(getattr(cfg, "max_temp_c", 48.0))
    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
    return temp, limit, wiederein, fuehler'''

new = '''def _boiler_max_info(state):
    """Infos zum harten Boiler-Maximum: (temp, limit, wiederein, fuehler).

    temp kann None sein (Fuehler fehlt) -> die Pruefungen entfallen dann.
    Bei aktiver Legionellenprophylaxe wird das Limit auf
    legionellen_max_temp_c erhoeht (wenn gesetzt).
    """
    cfg = getattr(getattr(state, "priority_config", None), "sicherheit", None)
    if cfg is None:
        return None, None, None, "unten"
    fuehler = getattr(cfg, "boiler_max_fuehler", None) or "unten"
    temp = getattr(getattr(state, "sensors", None), f"t_{fuehler}", None)
    if temp is None or not isinstance(temp, (int, float)):
        return None, None, None, fuehler

    # Standard-Limit aus Config
    limit = float(getattr(cfg, "max_temp_c", 48.0))
    # Legionellen-Uebersteuerung: Wenn die Prophylaxe aktiv ist, darf der
    # Boiler auf legionellen_max_temp_c hochheizen, bevor das harte Maximum
    # zuschlaegt.
    legionellen_limit = getattr(state, "legionellen_temp_override", None)
    if legionellen_limit is not None and legionellen_limit > limit:
        limit = legionellen_limit

    wiederein = limit - float(getattr(cfg, "boiler_max_hysterese_k", 2.0))
    return temp, limit, wiederein, fuehler'''

if old in content:
    content = content.replace(old, new)
    print('OK: _boiler_max_info updated')
else:
    print('FAILED: _boiler_max_info')
    idx = content.find('def _boiler_max_info')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+500]))

# 2. Update _extract_einschaltpunkt with Legionellen case
idx_ep = content.find("def _extract_einschaltpunkt")
if idx_ep >= 0:
    # Find the Einspeisung case and add Legionellen after it
    old2 = '''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert

        return config.sicherheit.max_temp_c'''

    new2 = '''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c - 6.0  # Anzeige-Wert
        elif name == "Legionellen":
            return config.legionellen.target_temp_c - 5.0  # EIN unter 55C

        return config.sicherheit.max_temp_c'''

    if old2 in content:
        content = content.replace(old2, new2)
        print('OK: _extract_einschaltpunkt updated')
    else:
        print('FAILED: _extract_einschaltpunkt')
        print('Looking for Einspeisung in _extract_einschaltpunkt...')
        ep_section = content[idx_ep:idx_ep+500]
        idx_ein = ep_section.find('Einspeisung')
        if idx_ein >= 0:
            print(repr(ep_section[idx_ein:idx_ein+200]))
else:
    print('FAILED: _extract_einschaltpunkt not found')

# 3. Update _extract_ausschaltpunkt with Legionellen case
idx_ap = content.find("def _extract_ausschaltpunkt")
if idx_ap >= 0:
    old3 = '''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c

        return config.sicherheit.max_temp_c'''

    new3 = '''    elif name == "Einspeisung":
            return config.einspeisung.ausschalten_bei_c
        elif name == "Legionellen":
            return config.legionellen.target_temp_c

        return config.sicherheit.max_temp_c'''

    if old3 in content:
        content = content.replace(old3, new3)
        print('OK: _extract_ausschaltpunkt updated')
    else:
        print('FAILED: _extract_ausschaltpunkt')
else:
    print('FAILED: _extract_ausschaltpunkt not found')

with open('priority_control_logic.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')