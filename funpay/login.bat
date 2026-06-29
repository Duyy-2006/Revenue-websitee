@echo off
REM Sign into FunPay once on Chrome Profile 3. Session persists forever in
REM C:\ChromeAutomation\Profile 3 and is shared with the main web dashboard
REM + the other platform scripts (eldorado, g2g, playerauctions).
REM
REM After signing in, close this Chrome window then run:
REM   python refresh_cookies.py
REM ...to pull the cookies into funpay/cookie.txt for the HTTP session.

taskkill /F /IM chrome.exe >nul 2>&1
taskkill /F /IM chromedriver.exe >nul 2>&1
timeout /t 2 /nobreak >nul
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --user-data-dir=C:\ChromeAutomation ^
  --profile-directory="Profile 3" ^
  --no-first-run ^
  --no-default-browser-check ^
  "https://funpay.com/en/account/login"
echo.
echo 1. Sign into FunPay in the Chrome window that just opened.
echo 2. Close that Chrome window.
echo 3. Run: python refresh_cookies.py
echo.
