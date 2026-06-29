@echo off
title Revenue Dashboard (visible Chrome)
cd /d "%~dp0"
set SCRAPE_VISIBLE=1
echo SCRAPE_VISIBLE=1 - Chrome will open with visible windows
echo.
python app.py
echo.
echo (server exited - press any key to close)
pause >nul
