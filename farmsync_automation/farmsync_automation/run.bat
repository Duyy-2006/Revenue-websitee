@echo off
title FarmSync Automation
cd /d "%~dp0"

:loop
echo Starting FarmSync Automation...
echo.
python automation.py
set exitcode=%errorlevel%

if %exitcode% equ 9009 (
    echo.
    echo [ERROR] Python not found. Make sure Python is installed and in PATH.
    echo   Install requests: pip install requests
    echo.
    pause
    exit /b 1
)

echo.
echo [%date% %time%] Script exited with code %exitcode%. Restarting in 10 seconds...
echo   Press Ctrl+C to stop.
timeout /t 10 /nobreak >nul
goto loop
