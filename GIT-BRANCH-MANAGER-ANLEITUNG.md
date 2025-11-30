# Git Branch Manager - Anleitung

## Übersicht
Dieses Skript automatisiert das Aktualisieren (Pull) und Hochladen (Push) von Git-Branches zu GitHub mit interaktiver Branch-Auswahl.

## Installation
Das Skript liegt bereits in deinem Projektordner:
```
c:\Users\Patrik\PycharmProjects\WPSteuerung\WPSteuerung\git-branch-manager.ps1
```

## Verwendung

### Interaktiver Modus (empfohlen)
Einfach das Skript ausführen:
```powershell
.\git-branch-manager.ps1
```

Du bekommst dann ein Menü mit folgenden Optionen:
1. **Branch von GitHub aktualisieren (Pull)** - Holt die neuesten Änderungen
2. **Änderungen zu GitHub pushen (Push)** - Lädt deine Änderungen hoch
3. **Beides (Pull dann Push)** - Erst updaten, dann pushen

### Direkt-Modus mit Parametern
```powershell
# Nur Pull
.\git-branch-manager.ps1 -Pull

# Nur Push
.\git-branch-manager.ps1 -Push
```

## Workflow

### Branch aktualisieren (Pull)
1. Skript startet
2. Zeigt aktuellen Branch an
3. Holt neueste Infos von GitHub (`git fetch --all`)
4. Du wählst den Branch aus, den du aktualisieren möchtest
5. Skript wechselt zum Branch (falls nötig) und pullt die Änderungen

### Änderungen pushen (push)
1. Skript zeigt Git-Status an
2. Du gibst eine Commit-Nachricht ein
3. Alle Änderungen werden hinzugefügt (`git add .`)
4. Commit wird erstellt
5. Du wählst den Ziel-Branch:
   - Option 1: Gleicher Branch (empfohlen)
   - Option 2: Anderer Branch (mit Warnung)
6. Änderungen werden zu GitHub gepusht

## Beispiel-Ablauf

```
╔═══════════════════════════════════════╗
║   Git Branch Manager                  ║
╚═══════════════════════════════════════╝

Aktueller Branch: master

Was möchtest du tun?
1. Branch von GitHub aktualisieren (Pull)
2. Änderungen zu GitHub pushen (Push)
3. Beides (Pull dann Push)
0. Beenden

Wähle (0-3): 1

=== Welchen Branch möchtest du aktualisieren? ===
1. master
2. android-api
3. funktioniert
0. Abbrechen

Wähle eine Option (0-3): 1

✓ Branch 'master' erfolgreich aktualisiert!
```

## Tipps
- **Vor dem Arbeiten**: Führe Pull aus, um die neuesten Änderungen zu holen
- **Nach dem Arbeiten**: Führe Push aus, um deine Änderungen hochzuladen
- **Option "Beides"**: Praktisch wenn du schnell pullen und pushen willst
- Das Skript verwendet `git pull --rebase` um Merge-Konflikte zu minimieren

## Sicherheit
- Änderungen an anderen Branches werden mit Warnung bestätigt
- Bei Fehlern bricht das Skript ab
- Kein automatisches Force-Push
