@echo off
setlocal enabledelayedexpansion
cd /d %~dp0

echo ========================================
echo   Heat Pump Control - Test Runner
echo ========================================

REM Check if pytest is installed
python -m pytest --version >nul 2>&1
if !errorlevel! neq 0 (
    echo [ERROR] pytest is not installed or not in PATH.
    echo Please install it using: pip install pytest pytest-asyncio
    pause
    exit /b 1
)

echo [INFO] Running all tests in /tests directory...
python -m pytest -s tests/

if !errorlevel! equ 0 (
    echo.
    echo [SUCCESS] All tests passed!
) else (
    echo.
    echo [FAILURE] Some tests failed. Please check the output above.
)

echo ========================================
pause
