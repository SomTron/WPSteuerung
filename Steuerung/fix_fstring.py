#!/usr/bin/env python3
"""Fix unterminated f-string in main.py caused by newline in emoji string."""
with open('main.py', 'rb') as f:
    data = f.read()

# The problematic section:
# Line 541 ends with `*"` then \r\n
# Line 542 is just `"` then \r\n
# Line 543 starts with `f"Heize auf`
# This should be:
# Line 541: `*\\n"` then \r\n
# Line 543: (same)

# Find the exact bytes
pattern_emoji = b'Legionellenprophylaxe gestartet'
idx = data.find(pattern_emoji)
if idx >= 0:
    # Show what's around it
    start = max(0, idx - 60)
    end = min(len(data), idx + 200)
    print("Before:")
    print(repr(data[start:end]))

# The broken sequence is:
# `msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*"\r\n"\r\n                                   f"Heize auf`
# Should be:
# `msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*\n"\r\n                                   f"Heize auf`

old = b'                            msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*"\r\n"\r\n                                   f"Heize auf'
new = b'                            msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*\\n"\r\n                                   f"Heize auf'

if old in data:
    data = data.replace(old, new, 1)
    print("Fixed emoji")
else:
    print("Pattern not found, trying alternative...")
    # Try without \r
    old2 = b'                            msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*"\n"\n                                   f"Heize auf'
    new2 = b'                            msg = (f"\xf0\x9f\xa6\xa0 *Legionellenprophylaxe gestartet!*\\n"\n                                   f"Heize auf'
    if old2 in data:
        data = data.replace(old2, new2, 1)
        print("Fixed emoji (no CR)")
    else:
        print("Still not found")
        # Debug
        idx = data.find(b'*Legionellenprophylaxe gestartet!*"')
        if idx >= 0:
            print("Found primary:", repr(data[idx:idx+50]))

# Fix done message (check for similar issue)
idx_done = data.find(b'\xe2\x9c\x85 *Legionellenprophylaxe abgeschlossen')
if idx_done >= 0:
    print("Done message found, checking...")
    # Show context
    print(repr(data[idx_done-40:idx_done+200]))
    # Check if newline issue exists
    done_old = b'msg = (f"\xe2\x9c\x85 *Legionellenprophylaxe abgeschlossen!*"\r\n"\r\n                                           f"KW'
    done_new = b'msg = (f"\xe2\x9c\x85 *Legionellenprophylaxe abgeschlossen!*\\n"\r\n                                           f"KW'
    if done_old in data:
        data = data.replace(done_old, done_new, 1)
        print("Fixed done message")
    else:
        done_old2 = b'msg = (f"\xe2\x9c\x85 *Legionellenprophylaxe abgeschlossen!*"\n"\n                                           f"KW'
        done_new2 = b'msg = (f"\xe2\x9c\x85 *Legionellenprophylaxe abgeschlossen!*\\n"\n                                           f"KW'
        if done_old2 in data:
            data = data.replace(done_old2, done_new2, 1)
            print("Fixed done message (no CR)")

with open('main.py', 'wb') as f:
    f.write(data)
print('Done!')