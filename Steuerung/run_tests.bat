@echo off
setlocal enabledelayedexpansion
cd /d %~dp0

echo ========================================
echo   Heat Pump Control - Test Runner
echo ========================================

REM Detect Python (prefer Windows launcher 'py', fallback to 'python')
set "PYTHON_CMD="
where py >nul 2>&1
if !errorlevel! equ 0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=python"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python wurde nicht gefunden. Bitte installiere Python und fuege es zum PATH hinzu.
    pause
    exit /b 1
)

REM Check if pytest is installed, otherwise try to install it automatically
%PYTHON_CMD% -m pytest --version >nul 2>&1
if !errorlevel! neq 0 (
    echo [INFO] pytest ist nicht installiert oder nicht im PATH.
    echo [INFO] Versuche, pytest und pytest-asyncio zu installieren ...
    %PYTHON_CMD% -m pip install pytest pytest-asyncio
    
    REM Re-check after installation attempt
    %PYTHON_CMD% -m pytest --version >nul 2>&1
    if !errorlevel! neq 0 (
        echo [ERROR] pytest konnte nicht installiert oder nicht gefunden werden.
        echo Bitte fuehre folgenden Befehl manuell aus:
        echo     %PYTHON_CMD% -m pip install pytest pytest-asyncio
        pause
        exit /b 1
    )
)

echo [INFO] Running all tests in /tests directory...
%PYTHON_CMD% -m pytest -s tests/

if !errorlevel! equ 0 (
    echo.
    echo [SUCCESS] All tests passed!
) else (
    echo.
    echo [FAILURE] Some tests failed. Please check the output above.
)

echo ========================================
pause
