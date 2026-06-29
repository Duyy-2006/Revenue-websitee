@echo off
REM One-time login helper for the automation Chrome profile.
REM Opens Chrome on C:\ChromeAutomation\Profile 3 (the same profile the
REM upload_offer / update_offer scripts use) pointing at Eldorado.
REM Sign in, close Chrome, then run run_upload.bat / run.bat.

setlocal
cd /d "%~dp0"

taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1
timeout /t 1 /nobreak >nul

"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --user-data-dir="C:\ChromeAutomation" ^
    --profile-directory="Profile 3" ^
    --no-first-run ^
    --no-default-browser-check ^
    https://www.eldorado.gg/login

echo.
echo 1. Sign into Eldorado in the Chrome window that just opened.
echo 2. Close that Chrome window.
echo 3. Run run_upload.bat (upload) or run.bat (update).
echo.
