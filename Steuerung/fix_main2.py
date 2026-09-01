#!/usr/bin/env python3
"""Fix the unterminated f-string in main.py caused by emoji newline issue."""

with open('main.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the broken f-string with emoji
old = '''                        try:
                            msg = (f"\U0001f9a0 *Legionellenprophylaxe gestartet!*"
"
                                   f"Heize auf {legionellen_cfg_lc.target_temp_c:.0f}\u00b0C "
                                   f"(unten: {state.sensors.t_unten:.1f}\u00b0C)")
                            from telegram_api import send_telegram_message as _send_tg'''

new = '''                        try:
                            msg = (f"\\U0001f9a0 *Legionellenprophylaxe gestartet!*\\n"
                                   f"Heize auf {legionellen_cfg_lc.target_temp_c:.0f}\\u00b0C "
                                   f"(unten: {state.sensors.t_unten:.1f}\\u00b0C)")
                            from telegram_api import send_telegram_message as _send_tg'''

if old in content:
    content = content.replace(old, new)
    print('OK: Fixed emoji f-string')
else:
    print('FAILED: Could not match the broken text')
    # Find the problematic area
    idx = content.find('Legionellenprophylaxe gestartet')
    if idx >= 0:
        print('Found at', idx)
        print(repr(content[idx:idx+400]))

# Also fix the done message
old2 = '''                                    try:
                                    msg = (f"\u2705 *Legionellenprophylaxe abgeschlossen!*\\n"
                                           f"KW {aktuelle_kw}: {legionellen_cfg_lc.target_temp_c:.0f}\\u00b0C erreicht")'''

# Check if this one has issues too
idx2 = content.find('abgeschlossen')
if idx2 >= 0:
    print('Found done message at', idx2)
    print(repr(content[idx2:idx2+400]))

with open('main.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done!')