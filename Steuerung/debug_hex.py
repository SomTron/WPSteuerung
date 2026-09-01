#!/usr/bin/env python3
"""Debug the main.py f-string issue."""
with open('main.py', 'rb') as f:
    data = f.read()
idx = data.find(b'*Legionellenprophylaxe gestartet!')
if idx >= 0:
    print('Found at index', idx)
    # Show hex around the area
    for i in range(idx - 20, idx + 40):
        ch = chr(data[i]) if 32 <= data[i] < 127 else '.'
        print(f'{i:5d}: {data[i]:02x} ({ch})')