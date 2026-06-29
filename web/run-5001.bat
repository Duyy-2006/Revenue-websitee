@echo off
REM Temporary launcher on port 5001 — use until next reboot clears the stuck
REM sockets on port 5000 (left over from Claude Code's process churn).
title Revenue Dashboard (port 5001)
cd /d "%~dp0"
set DASHBOARD_PORT=5001
pip install -r requirements.txt -q 2>nul

:loop
echo [%date% %time%] Starting server on port 5001...
python app.py
echo [%date% %time%] Server crashed. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
