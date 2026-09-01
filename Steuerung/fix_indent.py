#!/usr/bin/env python3
"""Fix indentation issues in main.py caused by edit attempts."""
import re

with open('main.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Fix line 362-363: over-indented logging line
for i, line in enumerate(lines):
    if 'elif ereignis == SOMMER_DEAKTIVIERT_DATEN' in line:
        # The next line is the logging line with 36 spaces - fix to 20 spaces
        if i + 1 < len(lines):
            stripped = lines[i+1].strip()
            if stripped:
                lines[i+1] = '                    ' + stripped + '\n'  # 20 spaces indent
        break

# Fix legionellen planning block (28 spaces indent -> 12 spaces)
# Find the start and end
seen_return = False
for i, line in enumerate(lines):
    if 'Legionellenprophylaxe Planung' in line:
        lines[i] = '            # --- Legionellenprophylaxe Planung (nach Prognose-Update) ---\n'
        # Continue fixing following lines
        i += 1
        while i < len(lines):
            s = lines[i]
            if s.strip() == '':
                i += 1
                continue
            if 'return last_vpn_check' in s:
                lines[i] = '    return last_vpn_check\n'
                seen_return = True
                i += 1
                continue
            if seen_return and not s.startswith('    '):
                break
            # Fix indentation: was at 28 spaces, should be at 12 spaces
            stripped = s.lstrip()
            if stripped:
                lines[i] = '            ' + stripped + '\n' if not stripped.endswith('\n') else '            ' + stripped
            i += 1
        break

with open('main.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("Indentation fixed")