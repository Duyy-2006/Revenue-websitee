@echo off
title FarmSync Device Monitor
cd /d "%~dp0"
echo Starting FarmSync Device Monitor...
echo.
python device_monitor.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Python failed. Make sure Python is installed and in PATH.
    echo   Install requests: pip install requests
)
echo.
pause
