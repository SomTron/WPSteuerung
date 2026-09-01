#!/usr/bin/env python3
"""Fix unterminated f-string by binary patching."""
with open('main.py', 'rb') as f:
    data = bytearray(f.read())

# Fix 1: The emoji start message
# Pattern: *\r\n"\r\n  -> *\\n"\r\n
old = bytearray(b'*\r\n"\r\n')   # 2a 0d 0a 22 0d 0a
new = bytearray(b'*\\n"\r\n')    # 2a 5c 6e 22 0d 0a
idx = data.find(old)
if idx >= 0:
    data[idx:idx+len(old)] = new
    print(f'Fixed pattern 1 at position {idx}')
else:
    print('Pattern 1 not found')
    # Debug
    idx2 = data.find(b'*Legionellenprophylaxe gestartet')
    if idx2 >= 0:
        print('Found at', idx2)
        print('Bytes after:', data[idx2+27:idx2+40].hex())

# Fix 2: The done message - same pattern
idx2 = data.find(b'*\xe2\x9c\x85')
if idx2 >= 0:
    print('Done checkmark found at', idx2)
    # Show context
    print(data[idx2:idx2+30].hex())
    # Find the pattern nearby
    for i in range(idx2, min(len(data), idx2+150)):
        if data[i:i+6] == old:
            data[i:i+6] = new
            print(f'Fixed pattern 2 at position {i}')
            break

with open('main.py', 'wb') as f:
    f.write(data)
print('Written.')

# Verify compilation
import py_compile
try:
    py_compile.compile('main.py', doraise=True)
    print('Compilation OK!')
except py_compile.PyCompileError as e:
    print('Syntax error remains:', str(e)[:500])