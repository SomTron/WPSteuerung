#!/usr/bin/env python3
"""Add legionellen info to api.py status endpoint."""
with open('api.py', 'r', encoding='utf-8') as f:
    content = f.read()

old = """        "komfort": komfort_info,
        "pv_profil": pv_profil_info,"""

new = """        "komfort": komfort_info,
        "legionellen": {
            "aktiv": getattr(shared_state, 'legionellen_aktiv', False),
            "planned_day": getattr(shared_state, 'legionellen_planned_day', None),
            "planned_time": getattr(shared_state, 'legionellen_planned_time', None),
            "planned_reason": getattr(shared_state, 'legionellen_planned_reason', None),
            "last_done": str(getattr(shared_state, 'legionellen_last_done', '')) if getattr(shared_state, 'legionellen_last_done', None) else None,
            "target_temp_c": getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None).target_temp_c if getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None) else None,
            "probezeit_minuten": getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None).probezeit_minuten if getattr(getattr(shared_state, 'priority_config', None), 'legionellen', None) else None,
        },
        "pv_profil": pv_profil_info,"""

if old in content:
    content = content.replace(old, new)
    with open('api.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('OK: api.py updated')
else:
    print('FAILED: api.py')
    idx = content.find('"komfort":')
    if idx >= 0:
        print(repr(content[idx:idx+200]))