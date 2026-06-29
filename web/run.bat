@echo off
title Revenue Dashboard
cd /d "%~dp0"
pip install -r requirements.txt -q 2>nul

:loop
echo [%date% %time%] Starting server...
python app.py
echo [%date% %time%] Server crashed. Restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
