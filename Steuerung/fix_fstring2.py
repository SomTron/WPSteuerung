#!/usr/bin/env python3
"""Fix unterminated f-string in main.py caused by literal newline inside f-string."""

with open('main.py', 'rb') as f:
    data = f.read()

# The problem: there's a real newline inside an f-string.
# Pattern: *!\r\n"\r\n  -> should be  *!\n"\r\n
# (Replace the real CRLF before the isolated " with the literal \n escape)
old = b'*!\r\n"\r\n'
new = b'*!\\n"\r\n'
count = data.count(old)
print(f'Found {count} patterns')
if count:
    data = data.replace(old, new)
    with open('main.py', 'wb') as f:
        f.write(data)
    
    # Verify compilation
    import py_compile
    try:
        py_compile.compile('main.py', doraise=True)
        print('Compilation OK!')
    except py_compile.PyCompileError as e:
        print('Still broken:', e)
else:
    # Try without CR
    old2 = b'*!\n"\n'
    new2 = b'*!\\n"\n'
    count2 = data.count(old2)
    print(f'Found {count2} patterns (no CR)')
    if count2:
        data = data.replace(old2, new2)
        with open('main.py', 'wb') as f:
            f.write(data)
        import py_compile
        try:
            py_compile.compile('main.py', doraise=True)
            print('Compilation OK!')
        except py_compile.PyCompileError as e:
            print('Still broken:', e)
    else:
        print('No patterns found, showing context...')
        idx = data.find(b'Legionellenprophylaxe gestartet')
        if idx >= 0:
            print(repr(data[idx-10:idx+80]))