"""Extract FunPay session cookies from Chrome Profile 3 → write funpay/cookie.txt.

Run AFTER signing into funpay.com via funpay/login.bat. This script
drives the dedicated automation Chrome profile (C:\\ChromeAutomation\\Profile 3)
in headless mode, navigates to funpay.com, grabs the live session cookies,
and saves them as a JSON list that web/app.py's funpay_session() reads.

Usage:
    python funpay/refresh_cookies.py
"""

import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = r"C:\Users\Duyy\Revenue-website"
COOKIE_FILE = os.path.join(BASE_DIR, "funpay", "cookie.txt")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_USER_DATA = r"C:\ChromeAutomation"
CHROME_PROFILE = "Profile 3"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _kill_orphan_chrome():
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='chrome.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=_NO_WINDOW).decode(errors="ignore")
    except Exception:
        return
    for line in out.splitlines():
        if CHROME_USER_DATA.lower() not in line.lower():
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2:
            continue
        pid = parts[1].strip()
        if pid.isdigit():
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, creationflags=_NO_WINDOW)


def refresh_cookies():
    import undetected_chromedriver as uc

    _kill_orphan_chrome()
    opts = uc.ChromeOptions()
    opts.binary_location = CHROME
    opts.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    opts.add_argument(f"--profile-directory={CHROME_PROFILE}")
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")

    drv = uc.Chrome(options=opts, use_subprocess=True, version_main=147)
    try:
        drv.get("https://funpay.com/en/")
        time.sleep(4)
        cookies = drv.get_cookies() or []
    finally:
        try:
            drv.quit()
        except Exception:
            pass

    if not cookies:
        raise RuntimeError("No FunPay cookies found — is Chrome Profile 3 signed into funpay.com? Run funpay/login.bat first.")

    cookie_list = [{
        "domain": c.get("domain", ".funpay.com"),
        "expirationDate": c.get("expiry", 0),
        "hostOnly": not c.get("domain", "").startswith("."),
        "httpOnly": c.get("httpOnly", False),
        "name": c["name"],
        "path": c.get("path", "/"),
        "sameSite": (c.get("sameSite", "None") or "None").lower() if c.get("sameSite") else None,
        "secure": c.get("secure", False),
        "session": c.get("expiry") is None,
        "storeId": None,
        "value": c["value"],
    } for c in cookies]

    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(cookie_list, f, indent=4)

    names = [c["name"] for c in cookie_list]
    has_session = "golden_key" in names and "PHPSESSID" in names
    print(f"Saved {len(cookie_list)} cookies to {COOKIE_FILE}")
    if not has_session:
        print("WARNING: Missing golden_key / PHPSESSID — you may not be logged in. Run funpay/login.bat then retry.")
    return cookie_list


if __name__ == "__main__":
    refresh_cookies()
