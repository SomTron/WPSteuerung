# API Server Installation

## Problem
Das System verwendet Python 3.13.7, aber `pip` ist nicht korrekt installiert.

## Lösung 1: PyCharm verwenden

Wenn du PyCharm verwendest:

1. Öffne PyCharm
2. Gehe zu **File → Settings → Project → Python Interpreter**
3. Klicke auf das **+** Symbol
4. Suche und installiere:
   - `fastapi`
   - `uvicorn`
   - `pydantic`

## Lösung 2: pip neu installieren

```powershell
# Als Administrator in PowerShell:
python -m ensurepip --upgrade
```

Danach:
```powershell
python -m pip install -r requirements.txt
```

## Lösung 3: Alternative Python Installation

Falls pip fehlt, kannst du Python neu installieren:
1. Von https://www.python.org/downloads/
2. Wichtig: **"Add Python to PATH"** anhaken
3. **"pip"** Checkbox aktivieren bei der Installation

## Nach erfolgreicher Installation:

```powershell
cd C:\Users\Patrik\PycharmProjects\WPSteuerungGit\WPSteuerung
python api_server.py
```

Der Server läuft dann auf: http://localhost:5000

## Schnelltest

Öffne im Browser: http://localhost:5000/docs
