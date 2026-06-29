@echo off
REM Sign into G2G once on Chrome Profile 3. Session persists forever in
REM C:\ChromeAutomation\Profile 3 and is shared with the main web dashboard
REM + the other platform scripts (eldorado, playerauctions).
taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir=C:\ChromeAutomation ^
  --profile-directory="Profile 3" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://www.g2g.com/sign-in"
echo.
echo Sign into G2G in the Chrome window that just opened, then close it.
echo Your session will persist for all G2G scripts.
