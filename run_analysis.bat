@echo off
setlocal enabledelayedexpansion

echo [WP Analysis] Starte Datenanalyse...

:: Pruefe ob Python installiert ist
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python konnte nicht gefunden werden. Bitte installiere Python.
    pause
    exit /b %errorlevel%
)

:: Script ausfuehren
python Analyse\wp_analysis.py

if %errorlevel% neq 0 (
    echo [ERROR] Die Analyse ist fehlgeschlagen.
    pause
    exit /b %errorlevel%
)

echo [WP Analysis] Analyse abgeschlossen.
echo [WP Analysis] Oeffne Dashboard: Analyse\dashboard.html
start "" "Analyse\dashboard.html"

:: Git Workflow
echo [WP Analysis] Aktualisiere GitHub...
git add Analyse\analysis_results.md
git add Analyse\merged_data.csv
git add Analyse\*.csv
git commit -m "Auto-Update: WP Analyse Ergebnisse und Daten"
:: git push :: Optional: Hier koennte man direkt pushen, aber wir belassen es beim commit

echo [WP Analysis] Fertig.
pause
