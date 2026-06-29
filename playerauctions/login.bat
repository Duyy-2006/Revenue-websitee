@echo off
REM Sign into PlayerAuctions once on Chrome Profile 3. Session persists
REM forever in C:\ChromeAutomation\Profile 3 and is shared with the main
REM web dashboard + the other platform scripts (eldorado, etc.).
REM
REM Login URL is on account.playerauctions.com (NOT www) -- it redirects
REM back to member.playerauctions.com/offers/active after sign-in.
taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir=C:\ChromeAutomation ^
  --profile-directory="Profile 3" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://account.playerauctions.com/login?returnUrl=https:%%2F%%2Fmember.playerauctions.com%%2Foffers%%2Factive"
echo.
echo Sign into PlayerAuctions in the Chrome window that just opened,
echo then close it. Your session will persist for all PA scripts.
