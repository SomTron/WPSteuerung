#!/usr/bin/env python3
"""Fix all unterminated f-strings in main.py."""
with open('main.py', 'rb') as f:
    data = bytearray(f.read())

# Find ALL occurrences of the pattern: real CRLF followed by " on its own line, followed by CRLF
# This is the result of a broken f-string with literal newline
count = 0
while True:
    # Pattern: \r\n"\r\n  - a CRLF, then a quote, then another CRLF
    old = bytearray(b'\r\n"\r\n')
    idx = data.find(old)
    if idx < 0:
        break
    # Check what's before it
    before = data[idx-5:idx]
    print(f'Found at {idx}: ...{before.hex()} {old.hex()} {data[idx+5:idx+10].hex()}')
    
    # Replace just the CRLF before the quote with literal \n
    # \r\n"\r\n  ->  \\n"\r\n
    new = bytearray(b'\\n"\r\n')
    data[idx:idx+len(old)] = new
    count += 1

print(f'Fixed {count} occurrences')

with open('main.py', 'wb') as f:
    f.write(data)

# Verify compilation
import py_compile, sys
try:
    py_compile.compile('main.py', doraise=True)
    print('Compilation OK!')
except py_compile.PyCompileError as e:
    msg = str(e)
    # Remove the unencodable chars
    msg_clean = msg.encode('utf-8', errors='replace').decode('utf-8', errors='replace')
    print(f'Syntax error remains: {msg_clean[:500]}')