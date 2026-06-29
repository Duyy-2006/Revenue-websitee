"""
Revenue Dashboard — Adopt Me! Account Sales Analytics
Aggregates sold orders from FunPay, u7buy, Eldorado, G2G, PlayerAuctions.

Auth strategy:
- FunPay:  HTTP cookie session (funpay/cookie.txt)
- u7buy:   HTTP OpenAPI Basic auth (u7buy/u7buy_apikey.txt — AppId + AppSecret)
- Eldorado / G2G / PlayerAuctions: Chrome Selenium on dedicated Profile 3
"""

import hashlib
import html as html_mod
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import threading
from datetime import datetime, timedelta
from contextlib import contextmanager

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Flag so every subprocess call runs without flashing a console window on
# Windows (wmic, taskkill, chrome/edge launches). Zero on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

from flask import Flask, render_template, jsonify, request, Response
import requests as http_requests

# ═══════════════════════════════════════════════════════════════
#  Config
# ═══════════════════════════════════════════════════════════════

BASE_DIR = r"C:\Users\Duyy\Revenue-website"
DB_FILE = os.path.join(BASE_DIR, "web", "data.db")
FUNPAY_COOKIE_FILE = os.path.join(BASE_DIR, "funpay", "cookie.txt")
U7BUY_APIKEY_FILE = os.path.join(BASE_DIR, "u7buy", "u7buy_apikey.txt")

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CHROME_USER_DATA = r"C:\ChromeAutomation"
CHROME_PROFILE = "Profile 3"

# u7buy OpenAPI (Basic auth with AppId/AppSecret from u7buy_apikey.txt)
U7BUY_OPENAPI_BASE = "https://openapi.u7buy.com/prod-api/open-api"
# Paid-order statuses per OpenAPI OrderStatus enum:
#   4 = "To Receive" (delivered, awaiting buyer's receipt confirm — sits here for
#                     days/weeks before flipping to 5)
#   5 = "Completed" (buyer confirmed receipt)
# Both represent realised revenue. Excluding status 4 loses every recent sale
# until the buyer manually confirms.
U7BUY_ORDER_PAID_STATUSES = (4, 5)

ELDORADO_DASHBOARD_URL = "https://www.eldorado.gg/dashboard/offers?category=Account"
ELDORADO_ORDERS_URL = "https://www.eldorado.gg/dashboard/orders/sold"
ELDORADO_LOGIN_HOST = "login.eldorado.gg"
ELDORADO_FEE_RATE = 0.21   # platform takes 21%; sales table stores seller net (×0.79)

G2G_ORDERS_URL = "https://www.g2g.com/g2g-user/sale"
G2G_LOGIN_HOST = "g2g.com/login"

PA_MEMBER_URL = "https://member.playerauctions.com"
PA_ORDERS_URL = f"{PA_MEMBER_URL}/orders/selling"
PA_OFFERS_URL = f"{PA_MEMBER_URL}/offers/active"

# FunPay auto-boost
FUNPAY_USER_ID = 11554164
FUNPAY_RAISE_URL = "https://funpay.com/lots/raise"
FUNPAY_RAISE_TICK_SECONDS = 180          # attempt cycle every 3 min
FUNPAY_RAISE_DISCOVERY_SECONDS = 6 * 3600  # re-scan user's active categories every 6h
FUNPAY_RAISE_DEFAULT_COOLDOWN = 4 * 3600 + 5 * 60   # 4h5m safety window after success
FUNPAY_RAISE_MAX_BACKOFF = 10 * 60   # never wait longer than 10 min before re-probing a
                                     # category — so if FunPay shortens its raise cooldown
                                     # we catch it fast instead of sitting on our 4h5m guess
FUNPAY_RAISE_ERROR_BACKOFF = 60 * 60 # after an error / FunPay anti-flood HTTP 428, wait
                                     # 1h before retrying. 20 min was too frequent and kept
                                     # FunPay's throttle alive for hours; an offer can only
                                     # be raised ~once / 4h, so frequent retries just abuse it.

# Online-presence keep-alive
FUNPAY_HEARTBEAT_SECONDS = 60             # HTTP GET funpay.com/ every 60s
CHROME_PRESENCE_INTERVAL_SECONDS = 20 * 60  # every 20 min, re-probe each Chrome platform

# Background auto-sync — pull new sold orders across all platforms on a schedule
AUTO_SYNC_INTERVAL_SECONDS = 10 * 60       # full sync cycle every 10 min

# FarmSync — Roblox device-farm REST API.
# Lives inside this repo at BASE_DIR/farmsync_automation/  (note underscore;
# the old hyphenated `farmsync-automation/` standalone clone is deprecated).
# Override with FARMSYNC_APIKEY_FILE env var if your layout differs.
FARMSYNC_API_BASE = "https://api.farmsync.cloud"
FARMSYNC_APIKEY_FILE = os.environ.get("FARMSYNC_APIKEY_FILE") or \
    os.path.join(BASE_DIR, "farmsync_automation", "api_keys.txt")
FARMSYNC_CACHE_TTL = 60

# YesCaptcha — balance lookup (free, no points cost). Replaces "All Time" tile.
YESCAPTCHA_API_BASE = "https://api.yescaptcha.com"
_yescaptcha_candidates = [
    os.environ.get("YESCAPTCHA_KEY_FILE") or "",
    os.path.join(BASE_DIR, "yescapcha", "apikey.txt"),
    os.path.join(BASE_DIR, "yescapcha", "apikey.txt.txt"),
]
YESCAPTCHA_APIKEY_FILE = next((p for p in _yescaptcha_candidates if p and os.path.exists(p)), _yescaptcha_candidates[1])
YESCAPTCHA_CACHE_TTL = 60          # /getBalance is free but no need to hammer it
YESCAPTCHA_USD_PER_POINT = 0.00014  # 1000 points = 1 CNY ≈ $0.14 (per docs, 2026-04)

# FarmSync Automation subprocess — runs farmsync_automation/farmsync_automation/automation.py
# alongside the website. Automation writes _state_devices.json / _state_accounts.json
# each cycle; website reads those files first (avoids duplicate cloud-API fetches).
FARMSYNC_AUTOMATION_DIR = os.path.join(BASE_DIR, "farmsync_automation", "farmsync_automation")
FARMSYNC_AUTOMATION_SCRIPT = os.path.join(FARMSYNC_AUTOMATION_DIR, "automation.py")
FARMSYNC_STATE_DEVICES = os.path.join(FARMSYNC_AUTOMATION_DIR, "_state_devices.json")
FARMSYNC_STATE_ACCOUNTS = os.path.join(FARMSYNC_AUTOMATION_DIR, "_state_accounts.json")
FARMSYNC_PAUSE_FLAG = os.path.join(FARMSYNC_AUTOMATION_DIR, "_paused.flag")
# When this flag exists, every code path that would launch / kill a Chrome
# session for the automation profile is a no-op. Used so the user can run
# login.bat without the presence loop / status probe / sync jobs taskkill-ing
# their freshly-opened Chrome window.
CHROME_FREEZE_FLAG = os.path.join(FARMSYNC_AUTOMATION_DIR, "_chrome_frozen.flag")
# State file older than this → ignore and hit the cloud API directly.
# Was 30 min — too long: the automation cycles every 20 min, so the state file
# was almost always old enough that every device's last_updated looked >10 min
# stale, making the dashboard show all 29 as "tool dead" even when only ~2
# actually were. Keep it tight so the heartbeat check stays accurate.
FARMSYNC_STATE_MAX_AGE = 120  # 2 minutes
FARMSYNC_AUTOMATION_AUTOSTART = os.environ.get("FARMSYNC_AUTOSTART", "1") != "0"

# ── ZP ZeroSolver — auto-solve CAPTCHA-locked farm accounts ──────────
# Every cycle, collect FarmSync accounts whose error == "CAPTCHA" (assigned to a
# device, with a live cookie) and POST them to ZeroSolver's in-game solver. The
# ZeroSolver API exposes only per-job COUNTS, never usernames, so "already in the
# queue" is tracked locally via our own job ledger: an account sitting in one of
# our still-active jobs is skipped; once that job finishes (and if the account is
# still captcha — e.g. the solve failed) it becomes eligible again next cycle.
# Only successful solves are billed (0.0025 cr ≈ $0.0025 each); already-solved and
# failed accounts are free. Key lives in "Zp auto faceunlock/api_solver.txt"
# (gitignored) — same folder as the ZP face-unlock client.
ZP_SOLVER_BASE = "https://zeropoint.to/api/zerosolver-api"
ZP_SOLVER_DIR = os.path.join(BASE_DIR, "Zp auto faceunlock")
_zp_key_candidates = [
    os.environ.get("ZP_SOLVER_KEY_FILE") or "",
    os.path.join(ZP_SOLVER_DIR, "api_solver.txt"),
    os.path.join(ZP_SOLVER_DIR, "api_solver.txt.txt"),
]
ZP_SOLVER_KEY_FILE = next((p for p in _zp_key_candidates if p and os.path.exists(p)), _zp_key_candidates[1])
ZP_SOLVER_STATE_FILE = os.path.join(ZP_SOLVER_DIR, "_zp_solver_state.json")
ZP_SOLVER_PAUSE_FLAG = os.path.join(ZP_SOLVER_DIR, "_zp_solver_paused.flag")
ZP_SOLVER_CAPTCHA_TYPE = "ingame"          # "ingame" (0.0025 cr) | "captchalock" (0.005 cr)
ZP_SOLVER_COST_PER_SOLVE = 0.0025
ZP_SOLVER_INTERVAL = 20 * 60               # sweep every 20 min
ZP_SOLVER_MAX_PER_CYCLE = 9000             # safety cap (< API's 10k-per-request limit)
ZP_SOLVER_CREDITS_TTL = 60                 # cache /credits for the dashboard tile
ZP_SOLVER_ACCOUNTS_MAX_AGE = 25 * 60       # reuse automation's _state_accounts.json if younger

app = Flask(__name__)

# Platform logged-out flags (surface to UI)
eldorado_logged_out = False
g2g_logged_out = False
pa_logged_out = False

# ═══════════════════════════════════════════════════════════════
#  Chrome Selenium Driver (Eldorado, G2G, PlayerAuctions)
# ═══════════════════════════════════════════════════════════════

_chrome_driver = None
_chrome_driver_created_ts = 0.0   # timestamp the current driver was spawned
_chrome_lock = threading.Lock()
# After this many seconds of uptime, force a fresh Chrome process on the next
# session. Prevents the gradual file-handle / memory leaks that cause heavy
# scrapes (G2G's TreeWalker) to die at ~3-hour mark with "browser has closed
# the connection".
CHROME_MAX_AGE_SECONDS = 90 * 60   # 1h30m

# ─── Chrome debug log (forensic trace for diagnosing session deaths) ───
# Writes a high-resolution timestamped log of every Chrome lifecycle event
# plus a background sampler that snapshots Chrome process state every 15 s.
# When a "session deleted" error happens, this file tells you exactly what
# was running, how much RAM Chrome was using, and whether any crash dumps
# landed in the seconds before the failure.
CHROME_DEBUG_FILE = os.path.join(BASE_DIR, "web", "_chrome_debug.log")
CHROME_DEBUG_MAX_BYTES = 10 * 1024 * 1024   # 10 MB rolling cap
CHROME_CRASHPAD_DIR = os.path.join(CHROME_USER_DATA, CHROME_PROFILE, "Crashpad", "reports")
_chrome_dlog_lock = threading.Lock()
_chrome_dlog_seen_dumps = set()   # filenames we've already reported
_chrome_health_last = {}          # last-sampled snapshot for change-detection


def _chrome_dlog(msg, **kv):
    """Append a timestamped line to the Chrome debug log. Extra kwargs are
    rendered as key=value pairs (good for grep). Thread-safe + rolling cap."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        kvs = " ".join(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}"
                       for k, v in kv.items()) if kv else ""
        line = f"[{ts}] {msg}" + ((" | " + kvs) if kvs else "") + "\n"
        with _chrome_dlog_lock:
            # Roll over if the file grows past the cap
            try:
                if os.path.getsize(CHROME_DEBUG_FILE) > CHROME_DEBUG_MAX_BYTES:
                    os.replace(CHROME_DEBUG_FILE, CHROME_DEBUG_FILE + ".1")
            except OSError:
                pass
            with open(CHROME_DEBUG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass   # never let logging crash the caller


def _chrome_sample_processes():
    """Use wmic to enumerate Chrome processes tied to our automation profile.
    Returns dict {pid: working_set_bytes}. Empty dict if wmic fails."""
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='chrome.exe'",
             "get", "ProcessId,WorkingSetSize,CommandLine", "/format:csv"],
            stderr=subprocess.DEVNULL, timeout=8,
            creationflags=_NO_WINDOW).decode(errors="ignore")
    except Exception:
        return {}
    result = {}
    for line in out.splitlines():
        if CHROME_USER_DATA.lower() not in line.lower():
            continue
        # CSV: Node,CommandLine,ProcessId,WorkingSetSize
        parts = line.rsplit(",", 2)
        if len(parts) < 3:
            continue
        try:
            ws = int(parts[-1].strip())
            pid = int(parts[-2].strip())
            result[pid] = ws
        except (ValueError, IndexError):
            continue
    return result


def _chrome_sample_crashpad():
    """Return the count of crash dump files in Profile 3's crashpad dir,
    plus a list of any new dump filenames since last sample."""
    try:
        files = os.listdir(CHROME_CRASHPAD_DIR)
    except Exception:
        return 0, []
    new = []
    for f in files:
        if f not in _chrome_dlog_seen_dumps and f.endswith(".dmp"):
            new.append(f)
            _chrome_dlog_seen_dumps.add(f)
    return len(files), new


def _chrome_sample_system_memory():
    """Total system RAM usage% via wmic. Returns int 0-100 or None."""
    try:
        out = subprocess.check_output(
            ["wmic", "OS", "get", "FreePhysicalMemory,TotalVisibleMemorySize", "/format:csv"],
            stderr=subprocess.DEVNULL, timeout=4,
            creationflags=_NO_WINDOW).decode(errors="ignore")
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 3:
                try:
                    free = int(parts[-2])
                    total = int(parts[-1])
                    return int(100 - (free * 100 / total))
                except (ValueError, ZeroDivisionError):
                    continue
    except Exception:
        return None
    return None


def _chrome_health_monitor_loop():
    """Every 15 s: sample Chrome processes + system memory + crash dumps.
    Logs whenever any of those change (PIDs appear/disappear, RAM swing
    >10%, or a new crash dump file shows up)."""
    time.sleep(20)   # let startup settle
    # Initialize the crashpad set so we only report NEW dumps from here on
    try:
        existing = [f for f in os.listdir(CHROME_CRASHPAD_DIR) if f.endswith(".dmp")]
        _chrome_dlog_seen_dumps.update(existing)
        _chrome_dlog("health_monitor: started",
                     existing_crashdumps=len(existing))
    except Exception:
        _chrome_dlog("health_monitor: started (crashpad dir missing)")
    while True:
        try:
            procs = _chrome_sample_processes()
            total_ram_mb = sum(procs.values()) // (1024 * 1024)
            sys_mem_pct = _chrome_sample_system_memory()
            dump_count, new_dumps = _chrome_sample_crashpad()

            prev = _chrome_health_last
            interesting = False
            reasons = []
            if set(procs.keys()) != set(prev.get("pids", [])):
                appeared = set(procs.keys()) - set(prev.get("pids", []))
                gone     = set(prev.get("pids", [])) - set(procs.keys())
                if appeared: reasons.append(f"+pids={sorted(appeared)}")
                if gone:     reasons.append(f"-pids={sorted(gone)}")
                interesting = True
            if prev.get("ram_mb") is not None and abs(total_ram_mb - prev["ram_mb"]) > 200:
                reasons.append(f"chrome_ram_mb {prev['ram_mb']}→{total_ram_mb}")
                interesting = True
            if sys_mem_pct is not None and prev.get("sys_mem_pct") is not None and abs(sys_mem_pct - prev["sys_mem_pct"]) > 8:
                reasons.append(f"sys_mem_pct {prev['sys_mem_pct']}→{sys_mem_pct}")
                interesting = True
            if new_dumps:
                reasons.append(f"NEW_CRASH_DUMPS={new_dumps}")
                interesting = True
                # Crash dumps are critical — also push to the main auto_log
                _auto_log(f"CHROME CRASHED — new crash dumps: {', '.join(new_dumps)}")
            if interesting:
                _chrome_dlog("health_sample CHANGED",
                             chrome_procs=len(procs),
                             chrome_pids=sorted(procs.keys()),
                             chrome_ram_mb=total_ram_mb,
                             sys_mem_pct=sys_mem_pct,
                             crashdumps_total=dump_count,
                             reasons=reasons)
            _chrome_health_last.update({
                "pids":        list(procs.keys()),
                "ram_mb":      total_ram_mb,
                "sys_mem_pct": sys_mem_pct,
                "dump_count":  dump_count,
            })
        except Exception as e:
            _chrome_dlog("health_monitor: sample failed", err=str(e)[:120])
        time.sleep(15)


def _ensure_chrome_junction():
    return


def _is_chrome_frozen():
    """When True, every code path that would launch or kill an automation
    Chrome session becomes a no-op. Set via /api/chrome/freeze/toggle so the
    user can run login.bat without us taskkill-ing their browser."""
    return os.path.exists(CHROME_FREEZE_FLAG)


def _is_dead_chrome_error(msg):
    """Detect a Selenium error caused by Chrome dying mid-use. When seen, the
    cached _chrome_driver should be nulled so the next chrome_session() call
    builds a fresh one (instead of trying to talk to a corpse)."""
    if not msg:
        return False
    m = str(msg).lower()
    return ("invalid session id" in m
            or "not connected to devtools" in m
            or "session deleted" in m
            or "session not created" in m
            or "browser has closed" in m
            or "chrome not reachable" in m
            or "target window already closed" in m)


def _caller_label():
    """Best-effort 'who called me' label for the debug log. Walks the stack
    looking for our own function names that matter (presence loop, scrape,
    refresh, etc) and returns the first match."""
    try:
        import inspect
        for frame in inspect.stack()[1:8]:
            fn = frame.function
            if fn.startswith(("_chrome_", "_do_chrome_", "_eldorado_", "_g2g_",
                              "_refresh_live_offers", "api_", "_auto_sync_loop",
                              "_chrome_presence_loop")):
                return fn
    except Exception:
        pass
    return "?"


def _invalidate_chrome_driver():
    """Best-effort quit + null the global driver so the next chrome_session()
    call rebuilds. Caller is responsible for synchronisation — this does NOT
    acquire _chrome_lock so it's safe to call from inside a chrome_session()
    with-block (where the lock is already held)."""
    global _chrome_driver
    d = _chrome_driver
    age_s = int(time.time() - _chrome_driver_created_ts) if _chrome_driver_created_ts else None
    _chrome_dlog("invalidate_driver",
                 caller=_caller_label(),
                 had_driver=(d is not None),
                 driver_age_s=age_s)
    _chrome_driver = None
    if d is not None:
        try:
            d.quit()
        except Exception:
            pass


# Cached Chrome major version — detected from binary metadata once at startup
# and on demand. Avoids the UC default approach of running `chrome.exe --version`
# (which hangs on some VMs and silently falls back to "108").
_chrome_major_cache = None


def _cleanup_uc_bogus_default_prefs():
    """Remove the bogus Default/Preferences file UC creates and corrupts.

    UC's handle_prefs always writes to <user-data-dir>/Default/Preferences
    even when --profile-directory points elsewhere. If that file ends up
    empty/whitespace (which happens — observed after host reboots), every
    uc.Chrome() call throws JSONDecodeError. Since we don't actually use
    the Default profile, the safest move is to wipe the file if it's small
    enough to be bogus."""
    bogus = os.path.join(CHROME_USER_DATA, "Default", "Preferences")
    try:
        if not os.path.exists(bogus):
            return
        size = os.path.getsize(bogus)
        if size < 1024:   # a real Chrome prefs file is 20-50 KB
            os.remove(bogus)
            _chrome_dlog("cleanup_uc_bogus_default_prefs: removed",
                         path=bogus, size=size)
    except Exception as e:
        _chrome_dlog("cleanup_uc_bogus_default_prefs: failed", err=str(e)[:120])


def _detect_chrome_major():
    """Return the installed Chrome's major version number (e.g. 148) by
    reading the chrome.exe binary's ProductVersion via PowerShell's
    Get-Item. Cached after first call. Returns None on failure (UC then
    falls back to its own logic)."""
    global _chrome_major_cache
    if _chrome_major_cache is not None:
        return _chrome_major_cache
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{CHROME}').VersionInfo.ProductVersion"],
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=_NO_WINDOW).decode(errors="ignore").strip()
        # ProductVersion looks like "148.0.7778.179"
        if out and "." in out:
            major = int(out.split(".")[0])
            _chrome_major_cache = major
            _chrome_dlog("detect_chrome_major", version=out, major=major)
            return major
    except Exception as e:
        _chrome_dlog("detect_chrome_major: FAILED", err=str(e)[:120])
    return None


def _kill_orphan_automation_chrome():
    if _is_chrome_frozen():
        _chrome_dlog("kill_orphan: skipped (frozen)")
        return
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='chrome.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=_NO_WINDOW).decode(errors="ignore")
    except Exception as e:
        _chrome_dlog("kill_orphan: wmic failed", err=str(e)[:120])
        return
    killed = []
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
            killed.append(int(pid))
    _chrome_dlog("kill_orphan: done", killed_pids=killed, count=len(killed))


def _clear_chrome_session_restore():
    prefs_path = os.path.join(CHROME_USER_DATA, CHROME_PROFILE, "Preferences")
    if not os.path.exists(prefs_path):
        return
    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        changed = False
        if data.get("profile", {}).get("exit_type") != "Normal":
            data.setdefault("profile", {})["exit_type"] = "Normal"
            changed = True
        if data.get("profile", {}).get("exited_cleanly") is not True:
            data.setdefault("profile", {})["exited_cleanly"] = True
            changed = True
        if changed:
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
    except Exception:
        pass


def _chrome_make_driver():
    if _is_chrome_frozen():
        _chrome_dlog("make_driver: refused (frozen)")
        raise RuntimeError("Chrome is frozen (toggle the Chrome pill in the sidebar to resume)")
    _chrome_dlog("make_driver: start", caller=_caller_label())
    import undetected_chromedriver as uc
    _ensure_chrome_junction()
    _kill_orphan_automation_chrome()
    _clear_chrome_session_restore()
    _cleanup_uc_bogus_default_prefs()
    opts = uc.ChromeOptions()
    opts.binary_location = CHROME
    opts.add_argument(f"--user-data-dir={CHROME_USER_DATA}")
    opts.add_argument(f"--profile-directory={CHROME_PROFILE}")
    opts.add_argument("--disable-features=TranslateUI,AutofillServerCommunication")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-session-crashed-bubble")
    opts.add_argument("--hide-crash-restore-bubble")
    if not os.environ.get("SCRAPE_VISIBLE"):
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1280,800")
        opts.add_argument("--disable-gpu")
    # NOTE: we DON'T pass add_experimental_option("prefs", ...) here even
    # though it might seem like the natural place. UC's handle_prefs writes
    # to <user-data-dir>/Default/Preferences regardless of our
    # --profile-directory=Profile 3, which means:
    #   (a) the prefs are written to the WRONG profile (Default, not Profile 3)
    #   (b) the Default/Preferences file can get into a corrupt/empty state
    #       which makes EVERY subsequent uc.Chrome() call throw JSONDecodeError
    # _clear_chrome_session_restore() already writes the same prefs to the
    # correct file (Profile 3/Preferences) before each launch, so we're
    # covered without the UC-side prefs handling.
    # Detect the installed Chrome version from the binary's metadata so
    # undetected-chromedriver downloads the matching chromedriver. UC's
    # built-in auto-detect runs `chrome.exe --version` which can hang on
    # this VM and silently fall back to "Chrome 108" → catastrophic
    # mismatch (every make_driver fails with "Expecting value: line 1
    # column 1 (char 0)" because the v108 driver can't talk to v148 Chrome).
    chrome_major = _detect_chrome_major()
    try:
        drv = uc.Chrome(options=opts, use_subprocess=True, version_main=chrome_major)
    except Exception as e:
        import traceback
        _chrome_dlog("make_driver: uc.Chrome() FAILED",
                     version_main=chrome_major,
                     err_type=type(e).__name__,
                     err=str(e)[:200],
                     tb=traceback.format_exc()[:600])
        raise
    global _chrome_driver_created_ts
    _chrome_driver_created_ts = time.time()
    try:
        chrome_pid = drv.service.process.pid
        cdp_url    = drv.service.service_url
    except Exception:
        chrome_pid = None
        cdp_url    = None
    _chrome_dlog("make_driver: done",
                 chromedriver_pid=chrome_pid,
                 cdp_url=cdp_url,
                 headless=not bool(os.environ.get("SCRAPE_VISIBLE")))
    return drv


def _chrome_alive(drv):
    if drv is None:
        return False
    try:
        _ = drv.current_url
        return True
    except Exception:
        return False


def _get_chrome_driver():
    """Return the cached Chrome driver, building a fresh one if:
    - it's dead (selenium can't reach it), OR
    - it's older than CHROME_MAX_AGE_SECONDS (prophylactic rotation to
      sidestep the file-handle / memory leak that crashes Chrome on
      heavy G2G scrapes after a few hours of continuous use).
    """
    global _chrome_driver
    too_old = (_chrome_driver is not None
               and _chrome_driver_created_ts
               and (time.time() - _chrome_driver_created_ts) > CHROME_MAX_AGE_SECONDS)
    if too_old:
        age_min = int((time.time() - _chrome_driver_created_ts) / 60)
        _auto_log(f"Chrome driver is {age_min}m old — rotating to a fresh process")
    if too_old or not _chrome_alive(_chrome_driver):
        try:
            if _chrome_driver is not None:
                try:
                    _chrome_driver.quit()
                except Exception:
                    pass
        finally:
            _chrome_driver = None
        _chrome_driver = _chrome_make_driver()
    return _chrome_driver


@contextmanager
def chrome_session(page_load_timeout=45):
    _chrome_lock.acquire()
    caller = _caller_label()
    t0 = time.time()
    _chrome_dlog("session: acquired lock", caller=caller)
    try:
        drv = _get_chrome_driver()
        try:
            drv.set_page_load_timeout(page_load_timeout)
        except Exception:
            pass
        try:
            yield drv
        except Exception as e:
            _chrome_dlog("session: exception inside with-block",
                         caller=caller,
                         err=str(e)[:200],
                         duration_s=int(time.time() - t0))
            try:
                drv.quit()
            except Exception:
                pass
            global _chrome_driver
            _chrome_driver = None
            raise
    finally:
        _chrome_lock.release()
        _chrome_dlog("session: released lock",
                     caller=caller,
                     duration_s=int(time.time() - t0))


_PRESENCE_TABS = {
    "eldorado": "https://www.eldorado.gg/dashboard/orders/sold",
    "g2g": "https://www.g2g.com/g2g-user/sale",
    "playerauctions": "https://member.playerauctions.com/orders/selling",
    "funpay": f"https://funpay.com/users/{FUNPAY_USER_ID}/",
    "u7buy": "https://www.u7buy.com/account/orders",
}
_presence_tab_handles = {}


def _ensure_presence_tabs(drv):
    """Open one persistent tab per Chrome platform, each sitting on its dashboard."""
    global _presence_tab_handles
    live = set(drv.window_handles)
    # Drop any stored handles that no longer exist (browser was restarted)
    _presence_tab_handles = {k: v for k, v in _presence_tab_handles.items() if v in live}
    platforms = list(_PRESENCE_TABS.keys())
    missing = [p for p in platforms if p not in _presence_tab_handles]
    if not missing:
        return
    if not _presence_tab_handles:
        # Use initial tab for the first missing platform
        drv.switch_to.window(drv.window_handles[0])
        first = missing[0]
        try:
            drv.get(_PRESENCE_TABS[first])
        except Exception:
            pass
        _presence_tab_handles[first] = drv.current_window_handle
        missing = missing[1:]
    for p in missing:
        try:
            drv.switch_to.new_window("tab")
            drv.get(_PRESENCE_TABS[p])
        except Exception:
            continue
        _presence_tab_handles[p] = drv.current_window_handle


def _switch_to_tab(drv, platform):
    """Switch driver focus to the platform's dedicated tab. Opens it if missing."""
    _ensure_presence_tabs(drv)
    handle = _presence_tab_handles.get(platform)
    if handle and handle in drv.window_handles:
        drv.switch_to.window(handle)


def _chrome_wait_present(drv, by, selector, timeout=20):
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    return WebDriverWait(drv, timeout).until(EC.presence_of_element_located((by, selector)))


# ═══════════════════════════════════════════════════════════════
#  DB
# ═══════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER,
            username TEXT NOT NULL,
            platform TEXT NOT NULL,
            price REAL NOT NULL,
            sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
        CREATE INDEX IF NOT EXISTS idx_sales_platform ON sales(platform);
        CREATE TABLE IF NOT EXISTS cache (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    _add_category_column(db)
    _fix_bad_dates(db)
    db.commit()
    db.close()


def _add_category_column(db):
    """Add sales.category (revenue-by-category grouping). Safe no-op if it
    already exists — older DBs predate the column."""
    cols = [r["name"] for r in db.execute("PRAGMA table_info(sales)").fetchall()]
    if "category" not in cols:
        db.execute("ALTER TABLE sales ADD COLUMN category TEXT DEFAULT ''")
        db.execute("CREATE INDEX IF NOT EXISTS idx_sales_category ON sales(category)")
        print("[Migration] Added sales.category column")


def _fix_bad_dates(db):
    """Fix sales records with non-ISO dates (e.g. 'Apr 16, 2026, 9:27:10 AM')."""
    rows = db.execute(
        "SELECT id, sold_at FROM sales WHERE sold_at IS NOT NULL AND sold_at != '' AND sold_at NOT LIKE '____-__-%'"
    ).fetchall()
    if not rows:
        return
    fixed = 0
    for r in rows:
        old_val = r["sold_at"]
        new_val = None
        for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p", "%b %d, %Y"):
            try:
                dt = datetime.strptime(old_val.strip(), fmt)
                new_val = dt.strftime("%Y-%m-%dT%H:%M:%S")
                break
            except ValueError:
                continue
        if new_val:
            db.execute("UPDATE sales SET sold_at=? WHERE id=?", (new_val, r["id"]))
            fixed += 1
    if fixed:
        print(f"[Migration] Fixed {fixed}/{len(rows)} sales records with bad dates")


# ── Revenue-by-category classification ──────────────────────────────
# Sold orders are grouped by the same game/category label the live-offers
# sidebar uses. Most platforms expose it directly (FunPay 2nd order-desc div
# "Roblox, Adopt Me"; u7buy gameName; Eldorado .game-info <p>); G2G/PA don't,
# so we fall back to keyword-classifying the stored title. _canon_category
# merges cross-platform spelling drift into one canonical label.
_CATEGORY_ALIASES = {
    "adopt me": "Adopt Me",
    "grow a garden": "Grow a Garden 2",
    "grow a garden 2": "Grow a Garden 2",
    "murder mystery": "Murder Mystery 2",
    "murder mystery 2": "Murder Mystery 2",
    "mm2": "Murder Mystery 2",
    "pet simulator": "Pet Simulator 99",
    "pet simulator 99": "Pet Simulator 99",
    "pet simulator x": "Pet Simulator 99",
    "blox fruits": "Blox Fruits",
    "blox fruit": "Blox Fruits",
    "blade ball": "Blade Ball",
    "king legacy": "King Legacy",
    "da hood": "Da Hood",
    "fisch": "Fisch",
    "sailor piece": "Sailor Piece",
    "roblox": "Roblox",
    "rbl": "Roblox",
    "accounts": "Roblox",   # FunPay generic "Roblox, Accounts" breadcrumb leaf
}

# (canonical label, keyword tuple) — first keyword hit wins, so order most
# specific first. Discriminators chosen from real titles: Adopt Me uses
# Compass Coins / Potions / Legendary Pets; Grow a Garden uses Sheckles /
# Plants / Seeds (both have "Pets", so never key on that alone).
_CATEGORY_TITLE_RULES = [
    ("Grow a Garden 2", ("sheckle", "grow a garden", " seeds", " plants", "watering can")),
    ("Murder Mystery 2", ("murder mystery", "mm2", "godly", "chroma knife")),
    ("Pet Simulator 99", ("pet simulator", "pet sim", "huge ", "titanic", "enchant")),
    ("Blox Fruits", ("blox fruit", "bloxfruit")),
    # Adopt-Me markers last among the games: GAG/MM2/PetSim claim their
    # distinctive tokens first, so a plain "🐾 N Pets" title (Eldorado's format)
    # falls through to here. The seller's catalogue is ~82% Adopt Me, so a
    # bare pet/egg/potion title is overwhelmingly Adopt Me.
    ("Adopt Me", ("adopt me", "compass coin", "legendary pet", "potion", "neon",
                  "mega ", " pets", " egg", "ride potion", "fly potion", "bucks")),
    ("Roblox", ("robux", "limited")),
]


def _canon_category(raw):
    """Normalise a raw platform label to a canonical category name. Handles:
      • FunPay's "<game>, <category>" breadcrumb  ("Roblox, Adopt Me" → Adopt Me,
        "Roblox, Accounts" → Roblox)
      • u7buy's platform-prefixed gameName  ("Roblox Murder Mystery 2" /
        "Rbl King Legacy" → drop the leading "Roblox "/"Rbl ")
      • spelling variants via _CATEGORY_ALIASES
    Returns '' for blank input."""
    s = (raw or "").strip()
    if not s:
        return ""
    # FunPay packs as "<game>, <category>" — keep the more specific half.
    if "," in s:
        head, tail = (p.strip() for p in s.split(",", 1))
        if head.lower() in ("roblox", "rbl", "video games", "gaming", "games"):
            s = tail or head
        elif tail.lower() in ("accounts", "account") and head:
            s = head            # "<Game>, Accounts" → keep the game name
    # Drop a leading platform word: "Roblox Murder Mystery 2", "Rbl King Legacy".
    low = s.lower()
    for pre in ("roblox ", "rbl ", "rb "):
        if low.startswith(pre) and len(s) > len(pre):
            s = s[len(pre):].strip()
            break
    return _CATEGORY_ALIASES.get(s.lower(), s)


def _classify_category_from_title(title):
    """Best-effort category from a stored order title (used to backfill old
    rows and for platforms that don't expose a label). Heuristic — returns ''
    when nothing matches."""
    t = (title or "").lower()
    if not t.strip():
        return ""
    for label, kws in _CATEGORY_TITLE_RULES:
        if any(kw in t for kw in kws):
            return label
    return ""


def cache_set(key, value):
    db = get_db()
    db.execute(
        "INSERT INTO cache (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, default=str), datetime.now().isoformat())
    )
    db.commit()
    db.close()


def cache_get(key):
    db = get_db()
    row = db.execute("SELECT value, updated_at FROM cache WHERE key=?", (key,)).fetchone()
    db.close()
    if row:
        return json.loads(row["value"]), row["updated_at"]
    return None, None


# ═══════════════════════════════════════════════════════════════
#  FunPay — HTTP cookie session
# ═══════════════════════════════════════════════════════════════

def funpay_account_paths():
    """Auto-discover FunPay accounts under BASE_DIR.

    Returns a list of (label, cookie_file_path) tuples — one per `funpay/`,
    `funpay2/`, `funpay3/`, ... that contains a `cookie.txt` (or the Windows
    double-extension `cookie.txt.txt`). The primary account is always the
    bare `funpay/`, listed first.
    """
    found = []
    try:
        entries = os.listdir(BASE_DIR)
    except Exception:
        return found
    # Sort so funpay < funpay2 < funpay3 ...
    def _key(name):
        if name == "funpay":
            return (0, "")
        # funpay2 → 2, funpay10 → 10, funpay-foo → ignored (filtered below)
        suffix = name[6:]
        try:
            return (int(suffix), suffix)
        except ValueError:
            return (10**9, name)
    candidates = sorted(
        (e for e in entries
         if (e == "funpay" or (e.startswith("funpay") and e[6:].isdigit()))
         and os.path.isdir(os.path.join(BASE_DIR, e))),
        key=_key,
    )
    for entry in candidates:
        for fname in ("cookie.txt", "cookie.txt.txt"):
            cookie = os.path.join(BASE_DIR, entry, fname)
            if os.path.exists(cookie):
                found.append((entry, cookie))
                break
    return found


def funpay_load_cookies(cookie_file=None):
    """Load FunPay cookies from a specific file (defaults to FUNPAY_COOKIE_FILE
    for the primary account). Returns a RequestsCookieJar or None."""
    path = cookie_file or FUNPAY_COOKIE_FILE
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        cookie_list = json.load(f)
    jar = http_requests.cookies.RequestsCookieJar()
    for c in cookie_list:
        jar.set(c["name"], c["value"],
                domain=c.get("domain", ".funpay.com"),
                path=c.get("path", "/"))
    return jar


def funpay_session(cookie_file=None):
    """Build a requests.Session using the given account's cookies.
    `cookie_file=None` falls back to FUNPAY_COOKIE_FILE (primary account)."""
    jar = funpay_load_cookies(cookie_file)
    if jar is None:
        return None
    s = http_requests.Session()
    s.cookies = jar
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "Origin": "https://funpay.com",
        "X-Requested-With": "XMLHttpRequest",
    })
    return s


def _pad_time(t):
    parts = t.split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{parts[1]}"
    return t


def _funpay_to_local(dt):
    """Convert a FunPay datetime (Moscow UTC+3) to local time."""
    import time as _time
    local_offset = -(_time.timezone if _time.daylight == 0 else _time.altzone) / 3600
    funpay_offset = 3
    shift = local_offset - funpay_offset
    return dt + timedelta(hours=shift)


def _parse_funpay_date(date_text):
    """Convert FunPay date (Moscow time) to local time ISO format."""
    now = datetime.now()
    date_text = date_text.strip().lower()
    months = {"january": 1, "february": 2, "march": 3, "april": 4,
              "may": 5, "june": 6, "july": 7, "august": 8,
              "september": 9, "october": 10, "november": 11, "december": 12}
    fp_dt = None
    if date_text.startswith("today"):
        import time as _time
        local_offset = -(_time.timezone if _time.daylight == 0 else _time.altzone) / 3600
        moscow_now = now + timedelta(hours=3 - local_offset)
        time_part = _pad_time(date_text.replace("today,", "").strip())
        h, m = map(int, time_part.split(":"))
        fp_dt = moscow_now.replace(hour=h, minute=m, second=0, microsecond=0)
    elif date_text.startswith("yesterday"):
        import time as _time
        local_offset = -(_time.timezone if _time.daylight == 0 else _time.altzone) / 3600
        moscow_now = now + timedelta(hours=3 - local_offset)
        moscow_yesterday = moscow_now - timedelta(days=1)
        time_part = _pad_time(date_text.replace("yesterday,", "").strip())
        h, m = map(int, time_part.split(":"))
        fp_dt = moscow_yesterday.replace(hour=h, minute=m, second=0, microsecond=0)
    else:
        m = re.match(r"(\d+)\s+(\w+)\s+(\d{4}),?\s*(\d+:\d+)?", date_text)
        if m:
            day, month_name, year = int(m.group(1)), m.group(2), int(m.group(3))
            time_part = _pad_time(m.group(4) or "00:00")
            month_num = months.get(month_name, 1)
            h, mi = map(int, time_part.split(":"))
            fp_dt = datetime(year, month_num, day, h, mi)
        else:
            m = re.match(r"(\d+)\s+(\w+),?\s*(\d+:\d+)?", date_text)
            if m:
                day, month_name = int(m.group(1)), m.group(2)
                time_part = _pad_time(m.group(3) or "00:00")
                month_num = months.get(month_name, now.month)
                year = now.year
                if month_num > now.month or (month_num == now.month and day > now.day):
                    year -= 1
                h, mi = map(int, time_part.split(":"))
                fp_dt = datetime(year, month_num, day, h, mi)
    if fp_dt is None:
        return now.isoformat()
    local_dt = _funpay_to_local(fp_dt)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")


def funpay_get_orders(session, max_pages=3):
    """Fetch real sales/orders from FunPay."""
    all_orders = []
    xhr = session.headers.pop("X-Requested-With", None)
    try:
        resp = session.get("https://funpay.com/en/orders/trade", timeout=15)
    finally:
        if xhr:
            session.headers["X-Requested-With"] = xhr
    resp.raise_for_status()
    text = resp.text

    blocks = text.split('class="tc-item')
    for block in blocks[1:]:
        oid_m = re.search(r'tc-order[^>]*>#?([A-Z0-9]+)', block)
        if not oid_m:
            continue
        order_id = oid_m.group(1)
        price_m = re.search(r'tc-seller-sum[^>]*>([\d.]+)\s*<span', block)
        if not price_m:
            price_m = re.search(r'tc-price[^>]*>([\d.]+)', block)
        price = float(price_m.group(1)) if price_m else 0
        status_m = re.search(r'tc-status[^>]*>([^<]+)', block)
        status = status_m.group(1).strip() if status_m else ""
        date_m = re.search(r'tc-date-time[^>]*>([^<]+)', block)
        date = date_m.group(1).strip() if date_m else ""
        buyer_m = re.search(r'media-user-name[^>]*>\s*<span[^>]*>([^<]+)', block, re.DOTALL)
        buyer = buyer_m.group(1).strip() if buyer_m else ""
        desc_m = re.search(r'order-desc[^>]*>\s*<div>([^<]+)', block, re.DOTALL)
        desc = html_mod.unescape(desc_m.group(1).strip()) if desc_m else ""
        # 2nd order-desc div holds the listing category, e.g. "Roblox, Adopt Me"
        # / "Roblox, Grow a Garden 2" — same label as the live-offers sidebar.
        cat_m = re.search(r'order-desc.*?</div>\s*<div[^>]*>([^<]+)</div>', block, re.DOTALL)
        category = _canon_category(html_mod.unescape(cat_m.group(1).strip())) if cat_m else ""
        abs_date = _parse_funpay_date(date)
        all_orders.append({
            "order_id": order_id,
            "price": price,
            "status": status,
            "date": date,
            "abs_date": abs_date,
            "buyer": buyer,
            "description": desc[:120],
            "category": category,
        })
    return all_orders


# ═══════════════════════════════════════════════════════════════
#  FunPay — Auto-boost (raises offers in each active category on cooldown)
# ═══════════════════════════════════════════════════════════════

# Per-account boost state: keyed by account label ("funpay", "funpay2", ...).
# Each entry has its own discovered user_id, category nodes, cooldown trackers.
_funpay_boost_state_by_account = {}    # label -> dict (see _boost_state_for)
_funpay_boost_lock = threading.Lock()


def _boost_state_for(label):
    """Lazily-init per-account boost state."""
    with _funpay_boost_lock:
        if label not in _funpay_boost_state_by_account:
            _funpay_boost_state_by_account[label] = {
                "user_id": None,        # discovered on first tick from the live session
                "nodes": [],             # list of {"game_id","node_id","name"}
                "last_discover": 0.0,
                "next_eligible": {},     # key "game_id:node_id" -> epoch seconds
                "last_result": {},       # key -> latest msg
            }
        return _funpay_boost_state_by_account[label]


def _funpay_discover_user_id(session):
    """Hit /en/orders/trade (already authenticated) and parse the seller's
    own user_id from the page HTML.

    Why not funpay.com/ root: the home page contains random featured-user
    `/users/NNN/` links before the logged-in user's nav element, so the
    first match isn't necessarily us. The orders page reliably has the
    seller's own profile link near the top of the markup."""
    # Temporarily drop X-Requested-With so we get the HTML page (not JSON)
    xhr = session.headers.pop("X-Requested-With", None)
    try:
        r = session.get("https://funpay.com/en/orders/trade", timeout=15)
    except Exception:
        if xhr:
            session.headers["X-Requested-With"] = xhr
        return None
    if xhr:
        session.headers["X-Requested-With"] = xhr
    if r.status_code != 200:
        return None
    # data-user-id attribute (with or without quotes)
    m = re.search(r'data-user-id=["\']?(\d+)', r.text)
    if m:
        return int(m.group(1))
    # First /users/NNN/ in any href (absolute or relative)
    m = re.search(r'href=["\']\s*(?:https?://funpay\.com)?/users/(\d+)/', r.text)
    if m:
        return int(m.group(1))
    # Last resort: any /users/NNN/ substring (e.g. in JS / data URLs)
    m = re.search(r'/users/(\d+)/', r.text)
    if m:
        return int(m.group(1))
    return None


# node_id -> game_id, cached across discoveries. game_id is stable, so a one-off
# /trade hiccup no longer drops a category the boost should raise (the live loop
# was intermittently losing MM2/925 this way).
_funpay_node_game_cache = {}


def _funpay_discover_raisable(session, user_id):
    """Scrape this seller's profile for active categories, then resolve each
    to a game_id. `user_id` is the FunPay user ID for the active session."""
    if not user_id:
        return []
    r = session.get(f"https://funpay.com/users/{user_id}/", timeout=15)
    if r.status_code != 200:
        return []
    nodes = []
    seen = set()
    for m in re.finditer(r'href="https?://funpay\.com/lots/(\d+)/"[^>]*>([^<]+)</a>', r.text):
        nid = int(m.group(1))
        name = re.sub(r"\s+", " ", m.group(2)).strip()
        if nid in seen:
            continue
        seen.add(nid)
        nodes.append({"node_id": nid, "name": name,
                      "game_id": _funpay_node_game_cache.get(nid)})
    for n in nodes:
        if n["game_id"]:
            continue  # already resolved on an earlier pass — skip the re-fetch
        try:
            rr = session.get(f"https://funpay.com/lots/{n['node_id']}/trade", timeout=15)
            gm = re.search(r'data-game="(\d+)"', rr.text)
            if gm:
                n["game_id"] = int(gm.group(1))
                _funpay_node_game_cache[n["node_id"]] = n["game_id"]
        except Exception:
            pass
    return [n for n in nodes if n["game_id"]]


def _funpay_parse_cooldown_seconds(msg):
    """Pull 'X hours Y minutes' (en) or 'X час(а/ов) Y минут' (ru) out of FunPay cooldown msg."""
    if not msg:
        return None
    m = re.search(r"(\d+)\s*(?:hour|час)", msg, re.I)
    h = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)\s*(?:minute|мин)", msg, re.I)
    mi = int(m.group(1)) if m else 0
    m = re.search(r"(\d+)\s*(?:second|сек)", msg, re.I)
    sec = int(m.group(1)) if m else 0
    total = h * 3600 + mi * 60 + sec
    return total if total > 0 else None


def _funpay_raise_one(session, game_id, node_id):
    """Two-step FunPay raise:
       1. POST game_id+node_id → returns {modal: "..."} (requires confirm) or {msg,error} (already done / on cooldown)
       2. POST game_id+node_id+node_ids[]=node_id → actual raise, returns {msg,error}
       Returns (raised_bool, msg, cooldown_seconds).
    """
    try:
        r = session.post(
            FUNPAY_RAISE_URL,
            data={"game_id": game_id, "node_id": node_id},
            timeout=15,
        )
        try:
            j = r.json()
        except ValueError:
            return False, f"non-JSON response (HTTP {r.status_code})", None
        if "modal" in j:
            # Confirmation step required — post again with node_ids[]=node_id
            r2 = session.post(
                FUNPAY_RAISE_URL,
                data=[("game_id", game_id), ("node_id", node_id), ("node_ids[]", node_id)],
                timeout=15,
            )
            try:
                j = r2.json()
            except ValueError:
                return False, f"non-JSON confirm (HTTP {r2.status_code})", None
        msg = (j.get("msg") or j.get("message") or "").strip()
        err_raw = j.get("error")
        err = bool(err_raw) and err_raw not in (0, "0", False, None)
        if not err:
            return True, msg or "raised", None
        return False, msg or "error", _funpay_parse_cooldown_seconds(msg)
    except Exception as e:
        return False, str(e)[:120], None


def _funpay_raise_all(session, game_id, node_ids):
    """Raise EVERY given subcategory in ONE request via FunPay's node_ids[]
    array — the same call the site's "raise offers" modal makes. One POST for
    the whole game instead of one-per-category avoids FunPay's anti-flood HTTP
    428 on /lots/raise. Returns (raised_bool, msg, cooldown_seconds)."""
    if not node_ids:
        return False, "no nodes", None
    try:
        # Step 1 — first POST returns {modal} (confirm needed) or {msg,error}.
        r = session.post(FUNPAY_RAISE_URL,
                         data={"game_id": game_id, "node_id": node_ids[0]}, timeout=15)
        try:
            j = r.json()
        except ValueError:
            return False, f"non-JSON response (HTTP {r.status_code})", None
        if "modal" in j:
            # Step 2 — confirm with ALL node_ids[] at once.
            data = [("game_id", game_id), ("node_id", node_ids[0])]
            data += [("node_ids[]", nid) for nid in node_ids]
            r2 = session.post(FUNPAY_RAISE_URL, data=data, timeout=15)
            try:
                j = r2.json()
            except ValueError:
                return False, f"non-JSON confirm (HTTP {r2.status_code})", None
        msg = (j.get("msg") or j.get("message") or "").strip()
        err_raw = j.get("error")
        err = bool(err_raw) and err_raw not in (0, "0", False, None)
        if not err:
            return True, msg or "raised", None
        return False, msg or "error", _funpay_parse_cooldown_seconds(msg)
    except Exception as e:
        return False, str(e)[:120], None


# FunPay 428-throttles bare HTTP raise POSTs (the curl/requests TLS fingerprint
# looks bot-like) but lets a REAL browser's request straight through — confirmed
# live: HTTP gets 428, the browser gets a clean 200 with the proper cooldown.
# So the raise is driven through Selenium: inject the account's cookies, then run
# the raise as a fetch() from funpay.com's own JS context (same fingerprint as
# the site's "raise offers" button). Discovery stays plain HTTP (GETs aren't
# throttled). The legacy HTTP raise (_funpay_raise_all) is kept as a fallback.

def _funpay_load_cookie_list(cookie_file):
    """Raw cookie objects from a funpay cookie.txt (JSON list, or name->value)."""
    try:
        raw = json.load(open(cookie_file, encoding="utf-8"))
    except Exception:
        return []
    if isinstance(raw, list):
        return [c for c in raw if c.get("name")]
    return [{"name": k, "value": v} for k, v in raw.items()]


def _funpay_set_browser_cookies(drv, cookies):
    """Inject funpay.com cookies into the browser via CDP (handles HttpOnly)."""
    try:
        drv.execute_cdp_cmd("Network.enable", {})
    except Exception:
        pass
    for c in cookies:
        try:
            drv.execute_cdp_cmd("Network.setCookie", {
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain") or ".funpay.com",
                "path": c.get("path") or "/", "secure": True})
        except Exception:
            pass


_FUNPAY_RAISE_JS = r"""
var cb = arguments[arguments.length - 1];
var game_id = arguments[0], node_ids = arguments[1];
function post(body){ return fetch('/lots/raise', {method:'POST', credentials:'include',
  headers:{'X-Requested-With':'XMLHttpRequest','Content-Type':'application/x-www-form-urlencoded'},
  body: body}).then(function(r){ return r.text().then(function(t){ return {status:r.status, body:t}; }); }); }
post('game_id=' + game_id + '&node_id=' + node_ids[0]).then(function(s1){
  var j = {}; try { j = JSON.parse(s1.body); } catch(e) {}
  if (j.modal) {
    var b = 'game_id=' + game_id + '&node_id=' + node_ids[0] + '&' +
            node_ids.map(function(n){ return 'node_ids%5B%5D=' + n; }).join('&');
    post(b).then(function(s2){ cb(s2); });
  } else { cb(s1); }
}).catch(function(e){ cb({status: 0, body: 'fetch error: ' + String(e)}); });
"""


def _funpay_raise_all_browser(drv, game_id, node_ids):
    """Raise every node of a game in one request, run from funpay.com's JS so
    FunPay sees a real browser (no 428). Returns (raised, msg, cooldown_seconds).
    FunPay's JSON carries the exact remaining cooldown in `wait`."""
    try:
        res = drv.execute_async_script(_FUNPAY_RAISE_JS, str(game_id), [str(n) for n in node_ids]) or {}
    except Exception as e:
        return False, str(e)[:120], None
    if res.get("status") != 200:
        return False, "HTTP %s" % res.get("status"), None
    try:
        j = json.loads(res.get("body") or "")
    except ValueError:
        return False, "non-JSON (HTTP 200)", None
    msg = (j.get("msg") or "").strip()
    if not j.get("error"):
        return True, msg or "raised", None
    cd = j.get("wait")
    try:
        cd = int(cd) if cd is not None else _funpay_parse_cooldown_seconds(msg)
    except (TypeError, ValueError):
        cd = _funpay_parse_cooldown_seconds(msg)
    return False, msg or "error", cd


def _funpay_boost_tick_account(label, cookie_file):
    """Run one boost iteration for a single FunPay account."""
    s = funpay_session(cookie_file)
    if s is None:
        return
    now = time.time()
    state = _boost_state_for(label)

    # Discover user_id once per account (per-process lifetime — cheap to re-do on cookie rotation)
    if state["user_id"] is None:
        uid = _funpay_discover_user_id(s)
        if not uid:
            _auto_log(f"FunPay boost [{label}] could not discover user_id (session not logged in?)")
            return
        with _funpay_boost_lock:
            state["user_id"] = uid
        _auto_log(f"FunPay boost [{label}] user_id={uid}")

    # Re-discover categories every FUNPAY_RAISE_DISCOVERY_SECONDS
    with _funpay_boost_lock:
        need_discovery = (
            not state["nodes"]
            or now - state["last_discover"] > FUNPAY_RAISE_DISCOVERY_SECONDS
        )
    if need_discovery:
        try:
            nodes = _funpay_discover_raisable(s, state["user_id"])
        except Exception as e:
            _auto_log(f"FunPay boost [{label}] discovery failed: {str(e)[:80]}")
            return
        with _funpay_boost_lock:
            state["nodes"] = nodes
            state["last_discover"] = now
        names = ", ".join(f"{n['name']} (g={n['game_id']},n={n['node_id']})" for n in nodes) or "none"
        _auto_log(f"FunPay boost [{label}] categories: {names}")

    with _funpay_boost_lock:
        nodes = list(state["nodes"])
        eligible = dict(state["next_eligible"])

    # Group eligible categories by game and raise each game's offers in ONE
    # request (FunPay's node_ids[] array — the same call the site's "raise
    # offers" modal makes). One POST per game instead of one-per-category is
    # what avoids FunPay's anti-flood HTTP 428 on /lots/raise.
    by_game = {}
    for n in nodes:
        key = f"{n['game_id']}:{n['node_id']}"
        if eligible.get(key, 0) > now:
            continue
        by_game.setdefault(n["game_id"], []).append(n["node_id"])

    if not by_game:
        return

    # Raise via the browser — FunPay 428s bare HTTP raise POSTs but lets a real
    # browser through. Inject this account's cookies, then run the batched raise
    # as a fetch() from funpay.com's own JS context (real browser fingerprint).
    try:
        with chrome_session() as drv:
            _switch_to_tab(drv, "funpay")
            _funpay_set_browser_cookies(drv, _funpay_load_cookie_list(cookie_file))
            try:
                drv.get("https://funpay.com/en/")
            except Exception:
                pass
            time.sleep(1.5)
            for game_id, node_ids in by_game.items():
                raised, msg, cd = _funpay_raise_all_browser(drv, game_id, node_ids)
                if raised:
                    # Raised — wait the full cooldown; an offer raises ~once / 4h.
                    next_eligible = now + FUNPAY_RAISE_DEFAULT_COOLDOWN
                elif cd:
                    # FunPay gave the exact remaining cooldown (`wait`) — honour it.
                    next_eligible = now + min(cd + 30, FUNPAY_RAISE_DEFAULT_COOLDOWN)
                else:
                    # Unexpected error (browser/chrome issue) — back off 1h.
                    next_eligible = now + FUNPAY_RAISE_ERROR_BACKOFF
                with _funpay_boost_lock:
                    for nid in node_ids:
                        state["next_eligible"][f"{game_id}:{nid}"] = next_eligible
                        state["last_result"][f"{game_id}:{nid}"] = msg
                if raised:
                    _auto_log(f"FunPay boost [{label}] {len(node_ids)} categories raised: {msg[:100]}")
                elif cd:
                    _auto_log(f"FunPay boost [{label}] {len(node_ids)} categories on cooldown — next raise ~{int((cd or 0) // 60)}m")
                else:
                    _auto_log(f"FunPay boost [{label}] {len(node_ids)} categories error: {msg[:100]}")
                time.sleep(2)
    except Exception as e:
        _auto_log(f"FunPay boost [{label}] browser raise error: {str(e)[:80]}")


def _funpay_boost_tick():
    """Single iteration of the boost loop — runs across all FunPay accounts."""
    accounts = funpay_account_paths()
    if not accounts:
        return
    for label, cookie_file in accounts:
        try:
            _funpay_boost_tick_account(label, cookie_file)
        except Exception as e:
            _auto_log(f"FunPay boost [{label}] tick error: {str(e)[:80]}")


def _funpay_boost_loop():
    time.sleep(15)
    while True:
        try:
            _funpay_boost_tick()
        except Exception as e:
            _auto_log(f"FunPay boost loop error: {str(e)[:80]}")
        time.sleep(FUNPAY_RAISE_TICK_SECONDS)


def _funpay_heartbeat_loop():
    """Keep FunPay 'last online' fresh: one GET funpay.com/ every minute."""
    time.sleep(45)
    logged_fail_recently = False
    while True:
        try:
            s = funpay_session()
            if s is None:
                if not logged_fail_recently:
                    _auto_log("FunPay heartbeat: no session (run refresh_cookies.py)")
                    logged_fail_recently = True
            else:
                r = s.get("https://funpay.com/", timeout=15, allow_redirects=True)
                if r.status_code == 200 and "menu-item-login" not in r.text:
                    if logged_fail_recently:
                        _auto_log("FunPay heartbeat: session live")
                    logged_fail_recently = False
                else:
                    if not logged_fail_recently:
                        _auto_log(f"FunPay heartbeat: not logged in (HTTP {r.status_code})")
                        logged_fail_recently = True
        except Exception as e:
            if not logged_fail_recently:
                _auto_log(f"FunPay heartbeat error: {str(e)[:80]}")
                logged_fail_recently = True
        time.sleep(FUNPAY_HEARTBEAT_SECONDS)


def _auto_sync_loop():
    """Every N minutes, pull fresh orders from every platform so the dashboard
    updates without the user clicking Sync."""
    time.sleep(180)
    while True:
        syncs = [
            ("funpay", _sync_funpay_sales),
            ("u7buy", _sync_u7buy_sales),
            ("eldorado", _sync_eldorado_sales),
            ("g2g", _sync_g2g_sales),
            ("playerauctions", _sync_pa_sales),
        ]
        for platform, fn in syncs:
            try:
                n, msg = fn()
                if n > 0:
                    _auto_log(f"auto-sync {platform}: +{n} new ({msg})")
            except Exception as e:
                _auto_log(f"auto-sync {platform} error: {str(e)[:80]}")
        time.sleep(AUTO_SYNC_INTERVAL_SECONDS)


# Per-presence-cycle counter. Used to thin out Eldorado's scrape rate
# (every 3rd cycle = ~60 min) since Eldorado's anti-bot kicks the
# session if we hammer the dashboard every 20 min.
_presence_iteration_count = 0


def _do_chrome_presence_iteration():
    """One pass: refresh seller-online tabs + scrape Eldorado/G2G live offers.

    Returns True if Chrome died mid-iteration (caller should retry with a
    fresh driver). Returns False on a clean iteration."""
    global _presence_iteration_count
    _presence_iteration_count += 1
    # Eldorado: only scrape every 3rd iteration (≈ every 60 min)
    # G2G:      every iteration (≈ every 20 min, same as before)
    eldorado_due = (_presence_iteration_count % 3 == 1)
    _chrome_dlog("presence_iteration: start",
                 iter_n=_presence_iteration_count,
                 eldorado_due=eldorado_due,
                 chrome_frozen=_is_chrome_frozen())
    chrome_died = False
    try:
        with chrome_session() as drv:
            _ensure_presence_tabs(drv)
            refreshed = []
            for platform, url in _PRESENCE_TABS.items():
                try:
                    _switch_to_tab(drv, platform)
                    try:
                        drv.refresh()
                    except Exception:
                        drv.get(url)
                    refreshed.append(platform)
                except Exception as e:
                    msg = str(e)
                    if _is_dead_chrome_error(msg):
                        chrome_died = True
                        _invalidate_chrome_driver()
                        return True
                    _auto_log(f"presence {platform} error: {msg[:60]}")
            if refreshed:
                _auto_log(f"presence refreshed tabs: {', '.join(refreshed)}")

            # ─── Inline live-offer scrape (Eldorado + G2G) ──────────
            # Reuses the same driver/session as the presence refresh so
            # Chrome doesn't get killed and re-created between loops.
            # Eldorado is gated to every 3rd cycle (~60 min) to avoid
            # tripping its anti-bot session-kick.
            if not _is_chrome_frozen():
                scrape_plan = []
                # Both Eldorado and G2G offers now come via their JSON APIs (no
                # Chrome render). They run AFTER this chrome_session block (see
                # below) so the token/cookie harvest can take the non-reentrant
                # chrome lock without deadlocking.
                for plat, fn in scrape_plan:
                    try:
                        _switch_to_tab(drv, plat)
                    except Exception as e:
                        msg = str(e)
                        if _is_dead_chrome_error(msg):
                            chrome_died = True
                            _invalidate_chrome_driver()
                            return True
                        _auto_log(f"Live offers [{plat}] tab-switch: {msg[:80]}")
                        continue
                    t0 = time.time()
                    try:
                        offs, err = fn(drv)
                    except Exception as e:
                        offs, err = [], f"{plat}: {str(e)[:140]}"
                    _record_live_offers_inline(plat, offs, err,
                                               int((time.time() - t0) * 1000))
                    if err and _is_dead_chrome_error(err):
                        chrome_died = True
                        _invalidate_chrome_driver()
                        return True
                    # Send the tab back to the presence URL so the next
                    # iteration's refresh keeps signalling "online".
                    try:
                        drv.get(_PRESENCE_TABS[plat])
                    except Exception as e:
                        if _is_dead_chrome_error(str(e)):
                            chrome_died = True
                            _invalidate_chrome_driver()
                            return True
    except Exception as e:
        msg = str(e)
        if _is_dead_chrome_error(msg):
            _chrome_dlog("presence_iteration: chrome died at outer scope",
                         iter_n=_presence_iteration_count, err=msg[:160])
            _invalidate_chrome_driver()
            return True
        _chrome_dlog("presence_iteration: outer exception (non-chrome)",
                     iter_n=_presence_iteration_count, err=msg[:160])
        _auto_log(f"presence loop error: {msg[:80]}")
    # ─── Eldorado live offers via the JSON API (no Chrome render) ───────
    # Chrome is used only to harvest the session cookie (inside
    # _eldorado_fetch_live_offers_api → _eldorado_cookies). Runs OUTSIDE the
    # chrome_session block above so the cookie harvest can acquire the
    # non-reentrant chrome lock without deadlocking. Gated every 3rd cycle
    # (~60 min) to match the previous cadence.
    if eldorado_due and not _is_chrome_frozen() and not chrome_died:
        t0 = time.time()
        try:
            offs, err = _eldorado_fetch_live_offers_api()
        except Exception as e:
            offs, err = [], f"eldorado API: {str(e)[:120]}"
        _record_live_offers_inline("eldorado", offs, err, int((time.time() - t0) * 1000))
    # ─── G2G live offers via the JSON API (no Chrome render) ───────
    # Chrome is used only to harvest the accessToken (inside
    # _g2g_fetch_live_offers_api → _g2g_token). Same out-of-block placement as
    # Eldorado to avoid re-entering the chrome lock.
    if not _is_chrome_frozen() and not chrome_died:
        t0 = time.time()
        try:
            offs, err = _g2g_fetch_live_offers_api()
        except Exception as e:
            offs, err = [], f"g2g API: {str(e)[:120]}"
        _record_live_offers_inline("g2g", offs, err, int((time.time() - t0) * 1000))
    _chrome_dlog("presence_iteration: done",
                 iter_n=_presence_iteration_count,
                 chrome_died=chrome_died)
    return chrome_died


def _chrome_presence_loop():
    """Refresh persistent platform tabs every CHROME_PRESENCE_INTERVAL_SECONDS,
    keeping the seller "online" on Eldorado / G2G / PA. Also scrapes live
    offers inline using the same Chrome driver — one session does both jobs.

    Retries the iteration once if Chrome dies mid-flight (the classic
    "invalid session id: session deleted" error). After the retry the
    driver is fresh, so even a flaky Chrome should recover within a
    single cycle instead of waiting 20 min for the next."""
    time.sleep(90)
    while True:
        attempts = 0
        while attempts < 2:
            attempts += 1
            died = _do_chrome_presence_iteration()
            if not died:
                break
            if attempts < 2:
                _auto_log("presence: Chrome died mid-iteration — retrying with fresh driver")
                time.sleep(3)
            else:
                _auto_log("presence: Chrome died on retry too — giving up until next cycle")
        time.sleep(CHROME_PRESENCE_INTERVAL_SECONDS)


# ═══════════════════════════════════════════════════════════════
#  u7buy — HTTP JWT session + Edge CDP for sold orders
# ═══════════════════════════════════════════════════════════════
#  u7buy — OpenAPI (Basic auth, AppId/AppSecret from u7buy_apikey.txt)
# ═══════════════════════════════════════════════════════════════

def u7buy_auth_header():
    """Load AppId (line 1) + AppSecret (line 2) from u7buy_apikey.txt and
    return the 'Basic base64(id:secret)' header value. None if file missing."""
    import base64
    if not os.path.exists(U7BUY_APIKEY_FILE):
        return None
    try:
        with open(U7BUY_APIKEY_FILE, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f.readlines() if l.strip()]
        if len(lines) < 2:
            return None
        app_id, app_secret = lines[0], lines[1]
        return "Basic " + base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
    except Exception:
        return None


def u7buy_api_probe():
    """Ping the OpenAPI order list with pageSize=1 to confirm auth works."""
    auth = u7buy_auth_header()
    if not auth:
        return False
    try:
        r = http_requests.get(
            f"{U7BUY_OPENAPI_BASE}/order/list",
            headers={"Authorization": auth, "Accept": "application/json"},
            params={"page": 1, "pageSize": 1},
            timeout=8,
        )
        return r.status_code == 200 and r.json().get("code") == 200
    except Exception:
        return False


def u7buy_fetch_sold_orders(max_pages=60, page_size=10):
    """Fetch u7buy paid sold orders via the OpenAPI.
    Endpoint: GET /prod-api/open-api/order/list
    Auth:     Basic base64(AppId:AppSecret)
    Filter:   orderStatus IN U7BUY_ORDER_PAID_STATUSES — fetched as separate
              calls per status code (the API only accepts a single value).
    Note:     The API silently caps pageSize at 10 — paginate accordingly.
    Returns (orders, status_msg) where each order is:
      {order_id, name, price_num, time (ISO), status, buyer, qty}
    """
    auth = u7buy_auth_header()
    if not auth:
        return [], "u7buy OpenAPI key not configured (u7buy/u7buy_apikey.txt)"

    headers = {"Authorization": auth, "Accept": "application/json"}
    all_orders = []
    seen_ids = set()
    grand_total = 0

    for status_code in U7BUY_ORDER_PAID_STATUSES:
        status_total = 0
        status_fetched = 0
        for page in range(1, max_pages + 1):
            try:
                r = http_requests.get(
                    f"{U7BUY_OPENAPI_BASE}/order/list",
                    headers=headers,
                    params={"page": page, "pageSize": page_size,
                            "orderStatus": status_code},
                    timeout=15,
                )
            except Exception as e:
                return all_orders, f"u7buy API error on page {page}: {str(e)[:60]}"
            if r.status_code != 200:
                return all_orders, f"u7buy API HTTP {r.status_code}"
            data = r.json()
            if data.get("code") != 200:
                return all_orders, f"u7buy API: {str(data.get('msg',''))[:60]}"
            payload = data.get("data") or {}
            rows = payload.get("rows") or []
            status_total = payload.get("total", status_total)
            if not rows:
                break
            status_fetched += len(rows)
            for row in rows:
                oid = str(row.get("orderId") or row.get("orderNo") or "")
                if oid and oid in seen_ids:
                    continue
                placed_ms = row.get("placedTime") or row.get("orderTimestamp") or 0
                try:
                    placed_iso = datetime.fromtimestamp(int(placed_ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S") if placed_ms else ""
                except Exception:
                    placed_iso = ""
                price_val = row.get("amount") or row.get("price") or 0
                try:
                    price_num = float(price_val)
                except (TypeError, ValueError):
                    price_num = 0.0
                if oid:
                    seen_ids.add(oid)
                all_orders.append({
                    "order_id": oid,
                    "name": str(row.get("productName") or "")[:120],
                    "price_num": price_num,
                    "time": placed_iso,
                    "status": str(row.get("statusName") or ""),
                    "buyer": "",  # not exposed on list endpoint
                    "qty": row.get("quantity") or 1,
                    "category": _canon_category(str(row.get("gameName") or "")),
                    "_status_code": status_code,
                })
            if status_total and status_fetched >= status_total:
                break
        grand_total += status_total

    return all_orders, f"Got {len(all_orders)}/{grand_total} paid orders"


# ═══════════════════════════════════════════════════════════════
#  Eldorado — Chrome Selenium
# ═══════════════════════════════════════════════════════════════

def _is_login_url(url, host_marker):
    """Determine whether a URL belongs to a login page. Two checks:
      1. host_marker (e.g. 'login.eldorado.gg') is a substring of the URL —
         definitive logged-out signal.
      2. The PARSED PATH equals /login or starts with /login/ — guards
         against false positives where '/login' appears as a query-param
         value or hash fragment on an otherwise-valid dashboard URL."""
    try:
        cur = (url or "").lower()
        if host_marker and host_marker in cur:
            return True, "host"
        from urllib.parse import urlparse
        path = (urlparse(cur).path or "").lower()
        if path == "/login" or path.startswith("/login/"):
            return True, "path"
    except Exception:
        pass
    return False, None


def _eldorado_is_login_page(drv):
    is_login, reason = _is_login_url(drv.current_url, ELDORADO_LOGIN_HOST)
    if is_login:
        _chrome_dlog("eldorado: login page detected",
                     url=drv.current_url, reason=reason)
    return is_login


def eldorado_logged_in():
    """Login status via the API (curl /api/authentication/claims) — no Chrome
    render. Chrome is touched only by _eldorado_cookies() to (re)load the
    session cookie when it has gone stale."""
    global eldorado_logged_out
    try:
        j = _eldorado_api_get("/api/authentication/claims")
        ok = isinstance(j, dict) and bool(j.get("email"))
        eldorado_logged_out = not ok
        return ok
    except Exception:
        return False


def _utc_to_local(iso_str):
    if not iso_str:
        return iso_str
    try:
        import time as _time
        local_offset_hours = -(_time.timezone if _time.daylight == 0 else _time.altzone) / 3600
        dt = datetime.fromisoformat(iso_str.replace("Z", ""))
        dt_local = dt + timedelta(hours=local_offset_hours)
        return dt_local.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return iso_str


def _normalize_eldorado_date(date_str):
    if not date_str or not date_str.strip():
        return ""
    date_str = date_str.strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str
    for fmt in ("%b %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M %p", "%b %d, %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    return date_str


def eldorado_fetch_sold_orders(max_pages=10):
    """Sold orders for Eldorado — JSON API only (Chrome is used solely to
    harvest the session cookie). No Selenium render; returns ([], reason) when
    the API is unavailable. order_id is the real Eldorado order UUID. The legacy
    Chrome scraper (_eldorado_fetch_sold_orders_selenium) is retained as a
    manual escape hatch but is no longer called automatically."""
    api_orders = _eldorado_orders_api()
    if api_orders is not None:
        return api_orders, "API: %d orders" % len(api_orders)
    return [], "Eldorado API unavailable (cookie/login)"


def _eldorado_fetch_sold_orders_selenium(max_pages=10):
    """Legacy Chrome scrape — no longer called automatically; kept as a manual
    fallback if the JSON API path ever needs bypassing."""
    from selenium.webdriver.common.by import By
    global eldorado_logged_out
    try:
        with chrome_session() as drv:
            _switch_to_tab(drv, "eldorado")
            drv.get(ELDORADO_ORDERS_URL)
            if _eldorado_is_login_page(drv):
                eldorado_logged_out = True
                return [], "Eldorado logged out"
            try:
                _chrome_wait_present(drv, By.CSS_SELECTOR, ".grid-row", timeout=20)
            except Exception:
                return [], "No orders visible"
            time.sleep(2)

            def _scrape():
                raw = drv.execute_script("""
                    return JSON.stringify(Array.from(document.querySelectorAll('.grid-row')).map(function(row){
                        var gi = row.querySelector('.game-info.desktop--md');
                        var game = gi ? ((gi.querySelector('p') && gi.querySelector('p').textContent.trim()) || '') : '';
                        var title = gi ? ((gi.querySelector('h6') && gi.querySelector('h6').textContent.trim()) || '') : '';
                        var st = row.querySelector('.order-status');
                        var status = st ? st.textContent.trim() : '';
                        var usr = row.querySelector('.order-user');
                        var user = usr ? usr.textContent.trim().replace(/^Buyer\\s*/i, '') : '';
                        var allText = row.textContent.replace(/\\s+/g, ' ').trim();
                        var pm = allText.match(/USD\\s*([\\d,.]+)/i) || allText.match(/\\$([\\d,.]+)/);
                        var dateStr = '';
                        var dateCell = row.querySelector('.grid-cell.date');
                        if (dateCell) {
                            var ps = dateCell.querySelectorAll('p');
                            for (var d = 0; d < ps.length; d++) {
                                var t = ps[d].textContent.trim();
                                if (/\\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\b.*\\d{4}/.test(t)) {
                                    dateStr = t; break;
                                }
                            }
                        }
                        // Real Eldorado order id — the row is an <a href="/order/<uuid>...">.
                        var href = row.getAttribute('href') || '';
                        if (!href) { var aEl = row.querySelector('a[href]'); href = aEl ? (aEl.getAttribute('href') || '') : ''; }
                        var oidPos = href.indexOf('/order/');
                        var orderId = oidPos >= 0 ? href.substring(oidPos + 7).split('?')[0].split('/')[0] : '';
                        return {
                            name: (title || game).substring(0, 60),
                            offer_title: title,
                            category: game,
                            order_id: orderId,
                            price_num: pm ? parseFloat(pm[1].replace(',','')) : 0,
                            time: dateStr,
                            status: status,
                            buyer: user.substring(0, 30),
                            qty: 1
                        };
                    }));
                """)
                return json.loads(raw) if raw else []

            all_orders = _scrape()
            pages_done = 1
            prev_signature = tuple((o.get("name", ""), o.get("time", "")) for o in all_orders[:3])
            for _ in range(1, max_pages):
                try:
                    arrows = drv.find_elements(By.CSS_SELECTOR, ".pagination-arrow")
                    if len(arrows) < 2:
                        break
                    next_arrow = arrows[-1]
                    if "disable" in (next_arrow.get_attribute("class") or ""):
                        break
                    drv.execute_script("arguments[0].scrollIntoView({block:'center'});", next_arrow)
                    time.sleep(0.3)
                    next_arrow.click()
                except Exception:
                    break
                page_orders = []
                for _wait in range(12):
                    time.sleep(1)
                    page_orders = _scrape()
                    if not page_orders:
                        continue
                    signature = tuple((o.get("name", ""), o.get("time", "")) for o in page_orders[:3])
                    if signature != prev_signature:
                        break
                if not page_orders:
                    break
                signature = tuple((o.get("name", ""), o.get("time", "")) for o in page_orders[:3])
                if signature == prev_signature:
                    break
                prev_signature = signature
                all_orders.extend(page_orders)
                pages_done += 1
            def _clean(v):
                return v.encode("utf-8", "ignore").decode("utf-8") if isinstance(v, str) else v
            for o in all_orders:
                for k in ("name", "offer_title", "category", "time", "status", "buyer"):
                    if k in o:
                        o[k] = _clean(o[k])
            # Dedup using the REAL Eldorado order id, taken from each row's
            # href (/order/<uuid>). The id is unique per order, so collapsing
            # on it removes the duplicate / blank-title rows the dashboard
            # sometimes renders for a single order — those used to slip past
            # the old (time, price, buyer) heuristic and land as phantom
            # "sales" (blank title, no category). Rows with neither an id nor a
            # title are page artifacts → dropped. Where two rows share an id,
            # keep the one with the longest (most complete) title.
            deduped = {}
            for o in all_orders:
                rid = (o.get("order_id") or "").strip()      # real uuid from href
                title = (o.get("name") or "").strip()
                if not rid and not title:
                    continue
                key = rid or f"_c:{o.get('time','')}|{o.get('price_num',0)}|{o.get('buyer','')}"
                existing = deduped.get(key)
                if existing is None or len(o.get("name", "")) > len(existing.get("name", "")):
                    deduped[key] = o
            all_orders = list(deduped.values())
            # Keep the stored id as the content hash (unchanged scheme) so the
            # DB-side dedup stays continuous with rows written before this
            # change — switching the stored id to the uuid would make the next
            # sync re-insert every on-page order. The uuid above is used purely
            # to collapse the dashboard's duplicate rows.
            for o in all_orders:
                key = f"{o.get('name','')}|{o.get('time','')}|{o.get('buyer','')}|{o.get('price_num',0)}"
                o["order_id"] = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()[:12]
            return all_orders, f"Got {len(all_orders)} orders across {pages_done} pages"
    except Exception as e:
        return [], str(e)[:80]


# ═══════════════════════════════════════════════════════════════
#  Eldorado — internal JSON API (curl + harvested cookies)
# ═══════════════════════════════════════════════════════════════
# Replaces the Selenium scrape. The offers walk (31 pages / ~219s of Chrome
# render every cycle) and the sold-orders scrape collapse to a handful of
# sub-second curl calls. Chrome is still needed ONLY to hold the logged-in
# session and hand over fresh cookies (cf_clearance + the __Host-EldoradoIdToken
# JWT) — a quick navigate+read, not a render. Validated 2026-06-24 to return
# identical price/buyer/title data to the Selenium scrape (orders 260/260,
# offers 282/282) and to be MORE complete (no lazy-load page truncation).
# Selenium stays as a fallback for when the cookie session is unavailable.

ELDORADO_API_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
ELDORADO_COOKIE_TTL = 1200          # re-harvest cookies at most every 20 min
ELDORADO_COOKIE_FILE = os.path.join(BASE_DIR, "web", "_eldorado_cookies.json")
_eldorado_cookie_cache = {"hdr": "", "ts": 0.0}
_eldorado_cookie_lock = threading.Lock()


def _eldorado_cookies(force=False):
    """Cookie header for eldorado.gg, harvested from the live Chrome session
    (Profile 3) and cached 20 min + file-backed. The ONLY Chrome touch the API
    path needs. Returns '' when there's no logged-in session."""
    with _eldorado_cookie_lock:
        now = time.time()
        if not _eldorado_cookie_cache.get("hdr"):
            # Survive restarts without an immediate Chrome launch: load the
            # persisted cookie. The API then works straight away; Chrome is only
            # touched when this cached cookie goes stale (TTL) or 401s.
            try:
                with open(ELDORADO_COOKIE_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("hdr"):
                    _eldorado_cookie_cache.update(hdr=saved["hdr"], ts=saved.get("ts", 0))
            except Exception:
                pass
        cached = _eldorado_cookie_cache.get("hdr")
        if not force and cached and (now - _eldorado_cookie_cache["ts"]) < ELDORADO_COOKIE_TTL:
            return cached
        try:
            with chrome_session() as drv:
                _switch_to_tab(drv, "eldorado")
                if "eldorado.gg" not in (drv.current_url or "").lower():
                    drv.get("https://www.eldorado.gg/")
                    time.sleep(2)
                cookies = drv.get_cookies()
            eld = [c for c in cookies if "eldorado" in (c.get("domain") or "")]
            if not any(c.get("name") == "__Host-EldoradoIdToken" for c in eld):
                return cached          # not logged in — keep stale, let caller fall back
            hdr = "; ".join("%s=%s" % (c["name"], c["value"]) for c in eld)
            _eldorado_cookie_cache.update(hdr=hdr, ts=now)
            try:
                with open(ELDORADO_COOKIE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"hdr": hdr, "ts": now}, f)
            except Exception:
                pass
            return hdr
        except Exception as e:
            _chrome_dlog("eldorado cookies: harvest failed", err=str(e)[:120])
            return cached


def _eldorado_api_get(path, _retry=True):
    """GET an eldorado.gg/api JSON endpoint via curl + harvested cookies.
    Refreshes cookies once on 401/403. Returns parsed JSON or None."""
    hdr = _eldorado_cookies()
    if not hdr:
        return None
    try:
        r = subprocess.run(
            ["curl", "-sS", "-m", "30", "-A", ELDORADO_API_UA,
             "-H", "Cookie: " + hdr, "-H", "Accept: application/json",
             "-w", "\n__HTTP_%{http_code}__", "https://www.eldorado.gg" + path],
            capture_output=True, creationflags=_NO_WINDOW)
        body = r.stdout.decode("utf-8", "ignore")
        code = ""
        if "\n__HTTP_" in body:
            body, _, tail = body.rpartition("\n__HTTP_")
            code = tail.replace("__", "").strip()
        if code in ("401", "403") and _retry:
            _eldorado_cookies(force=True)
            return _eldorado_api_get(path, _retry=False)
        return json.loads(body)
    except Exception:
        return None


def _eldorado_offer_category(a):
    """Game/category for an offer from its gameSeoAlias (e.g.
    'grow-a-garden-2-accounts-for-sale' → 'Grow A Garden 2'); falls back to
    classifying the title."""
    alias = a.get("gameSeoAlias") or ""
    for suf in ("-accounts-for-sale", "-for-sale", "-accounts"):
        if alias.endswith(suf):
            alias = alias[:-len(suf)]
            break
    if alias:
        name = alias.replace("-", " ").title()
        return _canon_category(name) or name
    return _classify_category_from_title(a.get("offerTitle") or "")


def _eldorado_orders_api(max_orders=600):
    """Sold orders via the seller-orders API (cursor paginated). Returns a list
    shaped like the Selenium scraper's output, EXCEPT order_id is the real
    Eldorado order UUID (not a content hash) and `time` is a local-time ISO
    string from createdDate. Returns None when the API is unavailable."""
    from urllib.parse import quote
    if not _eldorado_cookies():
        return None
    out = []
    cursor = "9999-99-99 99:99:99.999999999999999-9999-9999-9999-999999999999"
    seen, got = set(), False
    while len(out) < max_orders:
        j = _eldorado_api_get("/api/orders/me/seller/orders?cursorValue=%s&pageSize=50" % quote(cursor))
        if not isinstance(j, dict):
            break
        got = True
        res = j.get("results") or []
        for a in res:
            ood = a.get("orderOfferDetails") or {}
            price = a.get("totalPrice")
            price = price.get("amount") if isinstance(price, dict) else price
            st = a.get("state")
            status = st.get("state") if isinstance(st, dict) else (st or "")
            title = ood.get("offerTitle") or ""
            # Eldorado's order category/gameCategoryTitle is the generic "Account(s)";
            # blank it so _sync_eldorado_sales classifies by the offer title (the actual
            # game — Adopt Me, MM2, …) exactly like the old Selenium scrape did.
            cat = (ood.get("category") or ood.get("gameCategoryTitle") or "").strip()
            if cat.lower() in ("account", "accounts"):
                cat = ""
            out.append({
                "name": title[:60],
                "offer_title": title,
                "category": cat,
                "order_id": a.get("id") or "",                       # real UUID
                "price_num": float(price or 0),
                "time": _utc_to_local(a.get("createdDate") or ""),   # local ISO
                "status": status,
                "buyer": (a.get("buyerUsername") or "")[:30],
                "qty": a.get("purchaseQuantity") or 1,
            })
        nxt = j.get("nextPageCursor")
        if not nxt or nxt in seen or not res:
            break
        seen.add(nxt)
        cursor = nxt
    return out if got else None


def _eldorado_fetch_live_offers_api():
    """Live offers via the flexibleOffers search API (pageSize=50). Returns
    (offers, error) shaped like _eldorado_scrape_offers_inner. Returns
    ([], reason) when the API is unavailable so the caller can fall back."""
    if not _eldorado_cookies():
        return [], "Eldorado API: no cookie session"
    offers, seen = [], set()
    for pi in range(1, 40):
        j = _eldorado_api_get(
            "/api/flexibleOffers/me/search?pageIndex=%d&pageSize=50&category=Account" % pi)
        if not isinstance(j, dict):
            if offers:
                break
            return [], "Eldorado API: offers fetch failed"
        rows = j.get("results") or []
        if not rows:
            break
        for a in rows:
            oid = a.get("id") or ""
            if oid and oid in seen:
                continue
            if oid:
                seen.add(oid)
            state = (a.get("offerState") or "").lower()
            if "clos" in state:          # match Selenium: skip closed offers
                continue
            pp = a.get("pricePerUnit")
            price = pp.get("amount") if isinstance(pp, dict) else pp
            offers.append({
                "platform": "eldorado",
                "offer_id": oid,
                "title": a.get("offerTitle") or "",
                "price": float(price) if price is not None else None,
                "paused": "paus" in state,
                "category": _eldorado_offer_category(a),
                "url": ("https://www.eldorado.gg/adopt-me-accounts-for-sale/oa/" + oid) if oid else None,
            })
        if len(rows) < 50:
            break
    return offers, None


# ═══════════════════════════════════════════════════════════════
#  G2G — Chrome Selenium
# ═══════════════════════════════════════════════════════════════
# Selectors are a best guess for the G2G order-history page. Tune on first run.

def _g2g_is_login_page(drv):
    is_login, reason = _is_login_url(drv.current_url, G2G_LOGIN_HOST)
    if is_login:
        _chrome_dlog("g2g: login page detected",
                     url=drv.current_url, reason=reason)
    return is_login


def g2g_logged_in():
    """Login status via the API (a lightweight sls.g2g.com call) — no Chrome
    render. Chrome is touched only by _g2g_token() to (re)harvest the token."""
    global g2g_logged_out
    try:
        j = _g2g_api_get("/order/count-my-orders?seller_id=%s" % G2G_SELLER_ID)
        ok = isinstance(j, dict) and j.get("code") == 2000
        g2g_logged_out = not ok
        return ok
    except Exception:
        return False


_G2G_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4}),?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?", re.I)
_G2G_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def _normalize_g2g_date(text):
    if not text:
        return ""
    m = _G2G_DATE_RE.search(text)
    if not m:
        return ""
    day, mon, year, hh, mm, ampm = m.groups()
    mi = _G2G_MONTHS.get(mon.capitalize())
    if not mi:
        return ""
    h = int(hh) % 12 if ampm else int(hh)
    if ampm and ampm.upper() == "PM":
        h += 12
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}T{h:02d}:{int(mm):02d}:00"


# ═══════════════════════════════════════════════════════════════
#  G2G — internal JSON API (curl + harvested accessToken)
# ═══════════════════════════════════════════════════════════════
# Replaces the Selenium scrape. G2G's seller dashboard (Quasar/Vue SPA) is
# backed by a JSON API at sls.g2g.com. Auth is the short-lived accessToken
# (~15 min TTL) read from localStorage — sent RAW in the Authorization header
# (no "Bearer"). Chrome is needed ONLY to harvest that token; all data comes
# via curl. Validated 2026-06-24: orders dedup by order_item_id (matches the
# Selenium scrape's stored id — clean, no cutoff), offers via my_offers.
# Selenium kept as a manual escape hatch.

G2G_API_UA = ELDORADO_API_UA
G2G_SELLER_ID = "1001852670"
G2G_TOKEN_TTL = 600          # re-harvest at most every 10 min (token TTL ~15 min)
G2G_TOKEN_FILE = os.path.join(BASE_DIR, "web", "_g2g_token.json")
_g2g_token_cache = {"token": "", "cookies": "", "ts": 0.0}
_g2g_token_lock = threading.Lock()


def _g2g_token(force=False):
    """Return (accessToken, cookie_header) for sls.g2g.com, harvested from the
    live Chrome session (localStorage.accessToken + cookies), cached 10 min +
    file-backed. The ONLY Chrome touch the G2G API path needs."""
    with _g2g_token_lock:
        now = time.time()
        if not _g2g_token_cache.get("token"):
            try:
                saved = json.load(open(G2G_TOKEN_FILE, encoding="utf-8"))
                if saved.get("token"):
                    _g2g_token_cache.update(token=saved["token"], cookies=saved.get("cookies", ""),
                                            ts=saved.get("ts", 0))
            except Exception:
                pass
        tok = _g2g_token_cache.get("token")
        if not force and tok and (now - _g2g_token_cache["ts"]) < G2G_TOKEN_TTL:
            return tok, _g2g_token_cache.get("cookies", "")
        try:
            with chrome_session() as drv:
                _switch_to_tab(drv, "g2g")
                if "g2g.com" not in (drv.current_url or "").lower():
                    drv.get(G2G_ORDERS_URL)
                    time.sleep(3)
                token = drv.execute_script("return localStorage.getItem('accessToken')")
                cookies = drv.get_cookies()
            if not token:
                return tok, _g2g_token_cache.get("cookies", "")
            ck = [c for c in cookies if "g2g" in (c.get("domain") or "")]
            chdr = "; ".join("%s=%s" % (c["name"], c["value"]) for c in ck)
            _g2g_token_cache.update(token=token, cookies=chdr, ts=now)
            try:
                with open(G2G_TOKEN_FILE, "w", encoding="utf-8") as f:
                    json.dump({"token": token, "cookies": chdr, "ts": now}, f)
            except Exception:
                pass
            return token, chdr
        except Exception as e:
            _chrome_dlog("g2g token: harvest failed", err=str(e)[:120])
            return tok, _g2g_token_cache.get("cookies", "")


def _g2g_api_get(path, _retry=True):
    """GET an sls.g2g.com JSON endpoint via curl + the harvested accessToken
    (raw Authorization header). Refreshes the token once on 401/403."""
    token, cookies = _g2g_token()
    if not token:
        return None
    try:
        args = ["curl", "-sS", "-m", "30", "-A", G2G_API_UA,
                "-H", "Authorization: " + token, "-H", "Accept: application/json"]
        if cookies:
            args += ["-H", "Cookie: " + cookies]
        args += ["-w", "\n__HTTP_%{http_code}__", "https://sls.g2g.com" + path]
        r = subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW)
        body = r.stdout.decode("utf-8", "ignore")
        code = ""
        if "\n__HTTP_" in body:
            body, _, tail = body.rpartition("\n__HTTP_")
            code = tail.replace("__", "").strip()
        if code in ("401", "403") and _retry:
            _g2g_token(force=True)
            return _g2g_api_get(path, _retry=False)
        return json.loads(body)
    except Exception:
        return None


def _g2g_orders_api(max_orders=600):
    """Sold orders via /order/list_my_order. order_id is the real order_item_id
    (matches the Selenium scrape's stored id, so dedup stays continuous — no
    cutoff needed). Maps payment/order status onto the words _sync_g2g_sales
    filters on. Returns a list, or None when the API is unavailable."""
    if not _g2g_token()[0]:
        return None
    out, page, got = [], 1, False
    while len(out) < max_orders and page <= 40:
        j = _g2g_api_get("/order/list_my_order?seller_id=%s&include_pending_proof_only=0&page=%d&page_size=50"
                         % (G2G_SELLER_ID, page))
        if not isinstance(j, dict):
            break
        got = True
        res = (j.get("payload") or {}).get("results") or []
        if not res:
            break
        for a in res:
            pay = (a.get("payment_status") or "").lower()
            ois = (a.get("order_item_status") or "").lower()
            if ois in ("cancelled", "canceled") or pay in ("cancelled", "canceled"):
                status = "cancelled"
            elif "refund" in ois:
                status = "refunded"
            elif ois in ("dispute", "disputed"):
                status = "disputed"
            elif pay != "paid":
                status = "unpaid"
            else:
                status = ois or "completed"          # delivering / completed → counts
            try:
                # G2G's amount/unit_price/sub_total/total are in the BUYER's checkout
                # currency (PHP/IDR/VND/…), NOT USD. offer_amount / offer_unit_price hold
                # the USD value the offer was listed & sold at — use those for revenue.
                price = float(a.get("offer_amount") or a.get("offer_unit_price") or a.get("amount") or 0)
            except (TypeError, ValueError):
                price = 0.0
            ms = a.get("created_at") or 0
            try:
                t = datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%dT%H:%M:%S") if ms else ""
            except (TypeError, ValueError, OSError):
                t = ""
            out.append({
                "order_id": a.get("order_item_id") or "",   # real id — matches stored rows
                "name": (a.get("offer_title") or "")[:80],
                "price_num": price,
                "time": t,
                "status": status,
                "buyer": "",
                "qty": a.get("purchased_qty") or 1,
            })
        if len(res) < 50:
            break
        page += 1
    return out if got else None


def _g2g_fetch_live_offers_api():
    """Seller live offers via /offer/seller/<id>/my_offers. Same shape as
    _g2g_scrape_offers_inner. Returns ([], reason) when unavailable."""
    if not _g2g_token()[0]:
        return [], "G2G API: no token"
    offers, seen, page = [], set(), 1
    while page <= 40:
        j = _g2g_api_get("/offer/seller/%s/my_offers?cat_id=%s&status=live&page=%d&page_size=50&v=v2"
                         % (G2G_SELLER_ID, G2G_ROBLOX_CAT, page))
        if not isinstance(j, dict):
            if offers:
                break
            return [], "G2G API: offers fetch failed"
        res = (j.get("payload") or {}).get("results") or []
        if not res:
            break
        for a in res:
            oid = a.get("offer_id") or ""
            if not oid or oid in seen:
                continue
            seen.add(oid)
            try:
                price = float(a.get("unit_price")) if a.get("unit_price") is not None else None
            except (TypeError, ValueError):
                price = None
            offers.append({
                "platform": "g2g",
                "offer_id": oid,
                "title": _G2G_ADMIN_RE.sub("Admin Abuse", a.get("title") or ""),
                "price": price,
                "category": "Roblox",
            })
        if len(res) < 50:
            break
        page += 1
    return offers, None


def g2g_fetch_sold_orders(max_pages=20):
    """Sold orders for G2G — JSON API only (sls.g2g.com). Chrome is used solely
    to harvest the accessToken. No Selenium render; returns ([], reason) if the
    API is unavailable. order_id is the real order_item_id. The legacy Chrome
    scraper (_g2g_fetch_sold_orders_selenium) is retained as a manual escape
    hatch but is no longer called automatically."""
    api_orders = _g2g_orders_api()
    if api_orders is not None:
        return api_orders, "API: %d orders" % len(api_orders)
    return [], "G2G API unavailable (token/login)"


def _g2g_fetch_sold_orders_selenium(max_pages=20):
    """Legacy Chrome scrape from /g2g-user/sale (Quasar/Vue card layout) — no
    longer called automatically; kept as a manual fallback if the JSON API path
    ever needs bypassing."""
    from selenium.webdriver.common.by import By
    global g2g_logged_out
    try:
        with chrome_session(page_load_timeout=60) as drv:
            _switch_to_tab(drv, "g2g")
            drv.get(G2G_ORDERS_URL)
            # Wait for sale-item anchors to render (Vue is async)
            ready = False
            for _ in range(45):
                if _g2g_is_login_page(drv):
                    g2g_logged_out = True
                    return [], "G2G logged out"
                count = drv.execute_script(
                    "return document.querySelectorAll('a[href*=\"/g2g-user/sale/order/item/\"]').length;")
                if count and count > 0:
                    ready = True
                    break
                time.sleep(1)
            if not ready:
                return [], "G2G sale list did not load"
            g2g_logged_out = False
            time.sleep(2)

            def _scrape():
                raw = drv.execute_script(r"""
                    var anchors = Array.from(document.querySelectorAll('a[href*="/g2g-user/sale/order/item/"]'));
                    return JSON.stringify(anchors.map(function(a){
                        var href = a.getAttribute('href') || '';
                        var card = a.closest('.q-card') || a.parentElement;
                        var headerText = card ? (card.querySelector('.bg-1') ? card.querySelector('.bg-1').textContent : card.textContent) : '';
                        var titleEl = a.querySelector('[data-attr="order-item-offer-title"]');
                        var qtyEl = a.querySelector('[data-attr="order-item-purchased-qty"]');
                        var priceEl = a.querySelector('[data="order-item-offer-amount"]');
                        var currEl = a.querySelector('[data="order-item-offer-currency"]');
                        var statusEl = a.querySelector('.g-chip-status');
                        var idEl = a.querySelector('[data-attr="order-item-order-item-id"]');
                        var idFromText = idEl ? idEl.textContent.trim().replace(/^#/, '') : '';
                        var idFromHref = (href.match(/\/item\/([^/?#]+)$/) || [])[1] || '';
                        return {
                            href: href,
                            order_id: idFromHref || idFromText,
                            title: titleEl ? titleEl.textContent.trim() : '',
                            qty: qtyEl ? qtyEl.textContent.trim() : '',
                            price: priceEl ? priceEl.textContent.trim() : '',
                            currency: currEl ? currEl.textContent.trim() : '',
                            status: statusEl ? statusEl.textContent.trim() : '',
                            header: headerText.replace(/\s+/g, ' ').trim()
                        };
                    }));
                """)
                return json.loads(raw) if raw else []

            all_orders = []
            seen_ids = set()
            pages_done = 0
            prev_count = -1
            for _ in range(max_pages):
                items = _scrape()
                # Append uniquely
                added = 0
                for it in items:
                    oid = it.get("order_id", "")
                    if not oid or oid in seen_ids:
                        continue
                    seen_ids.add(oid)
                    price_str = re.sub(r"[^\d.]", "", it.get("price", ""))
                    try:
                        price = float(price_str) if price_str else 0
                    except ValueError:
                        price = 0
                    if not price:
                        continue
                    # Date from header: "Số đơn hàng X Đặt vào 26 Apr 2026, 03:11 AM Đã thanh toán"
                    header = it.get("header", "")
                    sale_date = _normalize_g2g_date(header)
                    all_orders.append({
                        "order_id": oid,
                        "name": it.get("title", "")[:80],
                        "price_num": price,
                        "time": sale_date,
                        "status": it.get("status", ""),
                        "buyer": "",
                        "qty": 1,
                    })
                    added += 1
                pages_done += 1
                # If page anchor count didn't grow, we're done (or pagination didn't advance)
                if len(seen_ids) == prev_count:
                    break
                prev_count = len(seen_ids)
                # G2G's "next" is a Quasar button with Material icon text "navigate_next"
                clicked = drv.execute_script("""
                    var btns = document.querySelectorAll('button');
                    for (var i = 0; i < btns.length; i++) {
                        var b = btns[i];
                        var t = (b.textContent || '').trim();
                        if (t === 'navigate_next' || t.endsWith('navigate_next')) {
                            var disabled = b.disabled || b.getAttribute('aria-disabled') === 'true' ||
                                /q-btn--disable|disabled/i.test(b.className);
                            if (disabled) return false;
                            b.scrollIntoView({block:'center'});
                            b.click();
                            return true;
                        }
                    }
                    return false;
                """)
                if not clicked:
                    break
                time.sleep(4)
            return all_orders, f"Got {len(all_orders)} orders across {pages_done} pages"
    except Exception as e:
        return [], str(e)[:80]


# ═══════════════════════════════════════════════════════════════
#  PlayerAuctions — Chrome Selenium
# ═══════════════════════════════════════════════════════════════

def _pa_is_login_page(drv):
    cur = (drv.current_url or "").lower()
    return ("login" in cur and "member" not in cur) or "account.playerauctions.com" in cur


def pa_logged_in():
    global pa_logged_out
    try:
        with chrome_session() as drv:
            _switch_to_tab(drv, "playerauctions")
            drv.get(PA_ORDERS_URL)
            time.sleep(6)
            if _pa_is_login_page(drv):
                pa_logged_out = True
                return False
            pa_logged_out = False
            return True
    except Exception:
        return False


_PA_DATE_RE = re.compile(r"([A-Z][a-z]{2})-(\d{1,2})-(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*(AM|PM)", re.I)
_PA_MONTHS = {m: i+1 for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"])}


def _normalize_pa_date(text):
    if not text:
        return ""
    m = _PA_DATE_RE.search(text)
    if not m:
        return ""
    mon, day, year, hh, mm, ss, ampm = m.groups()
    mi = _PA_MONTHS.get(mon.capitalize())
    if not mi:
        return ""
    h = int(hh) % 12
    if ampm.upper() == "PM":
        h += 12
    return f"{int(year):04d}-{mi:02d}-{int(day):02d}T{h:02d}:{int(mm):02d}:{int(ss):02d}"


def pa_fetch_orders_selenium(max_pages=5):
    """Scrape PA sold orders via Chrome Selenium."""
    from selenium.webdriver.common.by import By
    global pa_logged_out
    try:
        with chrome_session(page_load_timeout=60) as drv:
            _switch_to_tab(drv, "playerauctions")
            drv.get(PA_ORDERS_URL)
            time.sleep(4)
            # Wait up to 40s for data rows (Angular + Cloudflare)
            for _ in range(40):
                if _pa_is_login_page(drv):
                    pa_logged_out = True
                    return [], "PA logged out"
                ready = drv.execute_script("""
                    var dr = document.querySelectorAll('table tbody tr:not(.ant-table-placeholder)');
                    if (!dr.length) return false;
                    var firstCell = dr[0].querySelector('td');
                    return firstCell && !/Loading/i.test(firstCell.textContent);
                """)
                if ready:
                    break
                time.sleep(1)
            else:
                return [], "Orders table did not load"
            all_orders = []
            for _ in range(max_pages):
                rows_data = drv.execute_script("""
                    var rows = document.querySelectorAll('table tbody tr:not(.ant-table-placeholder)');
                    var out = [];
                    rows.forEach(function(row){
                        var tds = row.querySelectorAll('td');
                        if (tds.length < 4) return;
                        var cell0 = tds[0];
                        var link = cell0.querySelector('a[href]');
                        var titleEl = link || cell0.querySelector('p.mb-0 a, p.mb-0');
                        var title = titleEl ? titleEl.textContent.trim() : '';
                        // Date lives in first span.hide-xs inside cell0
                        var dateEl = cell0.querySelector('span.hide-xs');
                        var dateStr = dateEl ? dateEl.textContent.trim() : '';
                        // Order ID: parse from href, or from "Order ID: NNN" text
                        var href = link ? link.getAttribute('href') : '';
                        var oidFromHref = (href.match(/\\/orders\\/detail\\/(\\d+)/) || [])[1] || '';
                        var oidFromText = (cell0.textContent.match(/Order ID:\\s*(\\d+)/i) || [])[1] || '';
                        var orderId = oidFromHref || oidFromText || '';
                        var cells = Array.from(tds).map(function(c){return (c.textContent||'').trim();});
                        out.push({
                            title: title,
                            dateStr: dateStr,
                            orderId: orderId,
                            cells: cells
                        });
                    });
                    return out;
                """) or []
                for rd in rows_data:
                    cells = rd.get("cells", []) or []
                    if len(cells) < 4:
                        continue
                    title = (rd.get("title") or cells[0] or "")[:80]
                    buyer = (cells[1] or "")[:30]
                    price_text = re.sub(r"[^\d.]", "", (cells[2] or "").replace("USD", ""))
                    try:
                        price = float(price_text) if price_text else 0
                    except ValueError:
                        price = 0
                    status = ""
                    for c in cells[3:]:
                        cl = (c or "").lower()
                        if any(k in cl for k in ("complet", "delivered", "cancel", "refund", "pending")):
                            status = re.sub(r"(?i)status\s*:\s*", "", c or "").strip()[:30]
                            break
                    all_orders.append({
                        "order_id": rd.get("orderId") or "",
                        "name": title,
                        "price_num": price,
                        "time": _normalize_pa_date(rd.get("dateStr") or ""),
                        "status": status,
                        "buyer": buyer,
                        "qty": 1,
                    })
                clicked = drv.execute_script("""
                    var nav = document.querySelector('.pagination .next a, a.next-page, [rel=next], .ant-pagination-next:not(.ant-pagination-disabled) a');
                    if (nav) { nav.click(); return true; }
                    return false;
                """)
                if not clicked:
                    break
                time.sleep(5)
            return all_orders, f"Got {len(all_orders)} orders"
    except Exception as e:
        return [], str(e)[:80]


def refresh_pa_token():
    ok = pa_logged_in()
    return ok, ("Logged in (Chrome Profile 3)" if ok
                else "Not logged in — sign into PlayerAuctions in Chrome Profile 3")


# ═══════════════════════════════════════════════════════════════
#  Automation log
# ═══════════════════════════════════════════════════════════════

automation_log = []


def _auto_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    automation_log.append(entry)
    if len(automation_log) > 50:
        automation_log.pop(0)
    print(f"  AUTO: {entry}")


# ═══════════════════════════════════════════════════════════════
#  Sync functions — pull sold orders into the sales DB
# ═══════════════════════════════════════════════════════════════

def _sync_funpay_sales():
    """Sync sold orders from every FunPay account (funpay/, funpay2/, ...).

    Each account stores rows under its own platform string ('funpay',
    'funpay2', ...) — the dashboard treats them as distinct platforms with
    their own revenue rows / colors. Dedup is per-platform on order_id."""
    accounts = funpay_account_paths()
    if not accounts:
        return 0, "No FunPay cookies"
    db = get_db()
    new_sales = 0
    summary_parts = []
    for label, cookie_file in accounts:
        s = funpay_session(cookie_file)
        if not s:
            summary_parts.append(f"{label}: no session")
            continue
        try:
            orders = funpay_get_orders(s)
        except Exception as e:
            summary_parts.append(f"{label}: error {str(e)[:60]}")
            continue
        account_new = 0
        for o in orders:
            if o["status"] not in ("Paid", "Closed"):
                continue
            category = o.get("category") or _classify_category_from_title(o.get("description", ""))
            exists = db.execute(
                "SELECT id, category FROM sales WHERE platform=? AND username LIKE ?",
                (label, f"%[{o['order_id']}]%")
            ).fetchone()
            if exists:
                if category and not (exists["category"] or "").strip():
                    db.execute("UPDATE sales SET category=? WHERE id=?", (category, exists["id"]))
                continue
            sold_at = o.get("abs_date", datetime.now().isoformat())
            username = f"{o['description'][:60]} [{o['order_id']}]"
            db.execute(
                "INSERT INTO sales (username, platform, price, sold_at, category) VALUES (?, ?, ?, ?, ?)",
                (username, label, o["price"], sold_at, category)
            )
            account_new += 1
            new_sales += 1
        summary_parts.append(f"{label}: +{account_new}/{len(orders)}")
    db.commit()
    db.close()
    return new_sales, " · ".join(summary_parts) if summary_parts else "no accounts synced"


def _sync_u7buy_sales():
    orders, msg = u7buy_fetch_sold_orders()
    if not orders:
        return 0, msg
    db = get_db()
    new_sales = 0
    for o in orders:
        status = (o.get("status") or "").lower()
        if "cancel" in status or "refund" in status:
            continue
        price = o.get("price_num", 0)
        if price <= 0:
            continue
        order_id = o.get("order_id") or ""
        order_time = (o.get("time") or "").strip() or datetime.now().isoformat()
        category = o.get("category") or _classify_category_from_title(o.get("name", ""))
        # Dedup by order_id when present (canonical), else by exact time+price
        if order_id:
            exists = db.execute(
                "SELECT id, category FROM sales WHERE platform='u7buy' AND username LIKE ?",
                (f"%[{order_id}]%",)
            ).fetchone()
        else:
            exists = db.execute(
                "SELECT id, category FROM sales WHERE platform='u7buy' AND sold_at=? AND price=?",
                (order_time, price)
            ).fetchone()
        if exists:
            if category and not (exists["category"] or "").strip():
                db.execute("UPDATE sales SET category=? WHERE id=?", (category, exists["id"]))
            continue
        name = o.get("name", "")[:60]
        record = f"{name} [{order_id}]" if order_id else name
        db.execute(
            "INSERT INTO sales (username, platform, price, sold_at, category) VALUES (?, 'u7buy', ?, ?, ?)",
            (record, price, order_time, category)
        )
        new_sales += 1
    db.commit()
    db.close()
    return new_sales, f"{len(orders)} orders checked"


def _sync_eldorado_sales():
    orders, msg = eldorado_fetch_sold_orders()
    if not orders:
        return 0, msg
    db = get_db()
    # High-water-mark: newest Eldorado sale already stored. The API path dedups
    # by the real order UUID, which won't match the content-hash IDs written by
    # the older Selenium sync — so without this cutoff the first API sync would
    # re-insert every historical order. Skipping orders at/older than the newest
    # stored sale prevents that double-count; genuinely new orders (always newer)
    # still flow in. Harmless for the Selenium fallback (it dedups by hash).
    hw = db.execute("SELECT MAX(sold_at) FROM sales WHERE platform='eldorado'").fetchone()[0]
    new_sales = 0
    for o in orders:
        status = o.get("status", "")
        if status.lower() in ("cancelled", "canceled", "refunded", "disputed", ""):
            continue
        order_id = o.get("order_id", "")
        order_time = _normalize_eldorado_date(o.get("time", ""))
        if hw and order_time and order_time <= hw:
            continue          # already covered by a prior sync — don't re-insert
        buyer = o.get("buyer", o.get("name", ""))[:60]
        price = round(o.get("price_num", 0) * (1 - ELDORADO_FEE_RATE), 2)
        if not price:
            continue
        if not order_time and not order_id:
            continue
        category = _canon_category(o.get("category", "")) or _classify_category_from_title(o.get("name", ""))
        if order_id:
            exists = db.execute(
                "SELECT id, category FROM sales WHERE platform='eldorado' AND username LIKE ?",
                (f"%[{order_id}]%",)
            ).fetchone()
        else:
            exists = db.execute(
                "SELECT id, category FROM sales WHERE platform='eldorado' AND sold_at=? AND price=?",
                (order_time, price)
            ).fetchone()
        if exists:
            if category and not (exists["category"] or "").strip():
                db.execute("UPDATE sales SET category=? WHERE id=?", (category, exists["id"]))
            continue
        name = o.get("name", buyer)[:60]
        if order_id:
            name = f"{name} [{order_id}]"
        db.execute(
            "INSERT INTO sales (username, platform, price, sold_at, category) VALUES (?, 'eldorado', ?, ?, ?)",
            (name, price, order_time or datetime.now().isoformat(), category)
        )
        new_sales += 1
    db.commit()
    db.close()
    return new_sales, f"{len(orders)} orders checked"


def _sync_g2g_sales():
    orders, msg = g2g_fetch_sold_orders()
    if not orders:
        return 0, msg
    db = get_db()
    new_sales = 0
    for o in orders:
        status = (o.get("status") or "").lower()
        # Skip cancel / refund / dispute / payment-not-received (English + Vietnamese)
        if any(k in status for k in (
            "cancel", "refund", "dispute", "unpaid", "not paid", "not received",
            "hủy", "hoàn tiền", "tranh chấp", "chưa nhận", "chưa thanh toán",
        )):
            continue
        oid = o.get("order_id", "")
        price = o.get("price_num", 0)
        if not price:
            continue
        category = _classify_category_from_title(o["name"]) or "Roblox"
        existing = db.execute("SELECT id, category FROM sales WHERE platform='g2g' AND username LIKE ?",
                              (f"%[{oid}]%",)).fetchone() if oid else None
        if existing:
            if category and not (existing["category"] or "").strip():
                db.execute("UPDATE sales SET category=? WHERE id=?", (category, existing["id"]))
            continue
        name = f"{o['name']} [{oid}]" if oid else o["name"]
        db.execute("INSERT INTO sales (username, platform, price, sold_at, category) VALUES (?, 'g2g', ?, ?, ?)",
                   (name, price, o.get("time") or datetime.now().isoformat(), category))
        new_sales += 1
    db.commit()
    db.close()
    return new_sales, f"{len(orders)} orders checked"


def _sync_pa_sales():
    orders, msg = pa_fetch_orders_selenium()
    if not orders:
        return 0, msg or "No PA orders"
    db = get_db()
    new_sales = 0
    for o in orders:
        status = (o.get("status") or "").lower()
        if "cancel" in status or "refund" in status:
            continue
        name = o.get("name", "")
        buyer = o.get("buyer", "")
        price = o.get("price_num", 0)
        order_id = o.get("order_id") or ""
        if not price:
            continue
        category = _classify_category_from_title(name)
        if order_id:
            existing = db.execute(
                "SELECT id, category FROM sales WHERE platform='playerauctions' AND username LIKE ?",
                (f"%[{order_id}]%",)).fetchone()
        else:
            existing = db.execute(
                "SELECT id, category FROM sales WHERE platform='playerauctions' AND username=? AND price=?",
                (f"{name} [{buyer}]", price)).fetchone()
        if existing:
            if category and not (existing["category"] or "").strip():
                db.execute("UPDATE sales SET category=? WHERE id=?", (category, existing["id"]))
            continue
        record = f"{name} [{buyer}] [{order_id}]" if order_id else f"{name} [{buyer}]"
        db.execute("INSERT INTO sales (username, platform, price, sold_at, category) VALUES (?, 'playerauctions', ?, ?, ?)",
                   (record, price, o.get("time") or datetime.now().isoformat(), category))
        new_sales += 1
    db.commit()
    db.close()
    return new_sales, f"{len(orders)} orders checked"


def _backfill_categories():
    """Populate sales.category for rows that predate the column.

    Two passes: (1) re-scrape the HTTP-cheap platforms (FunPay, u7buy) and tag
    rows by order-id with the exact platform label; (2) keyword-classify any
    rows still blank from their stored title. Chrome platforms (Eldorado / G2G
    / PA) pick up their exact labels for free on their next sync — the sync now
    refines blank-category rows in place. Returns the count of rows tagged."""
    db = get_db()
    pending = db.execute(
        "SELECT COUNT(*) FROM sales WHERE category IS NULL OR TRIM(category)=''"
    ).fetchone()[0]
    if not pending:
        db.close()
        return 0
    tagged = 0
    # Pass 1a — FunPay (per account), exact label by order-id.
    try:
        for label, cookie in funpay_account_paths():
            s = funpay_session(cookie)
            if not s:
                continue
            try:
                orders = funpay_get_orders(s)
            except Exception:
                continue
            for o in orders:
                cat, oid = o.get("category", ""), o.get("order_id")
                if cat and oid:
                    cur = db.execute(
                        "UPDATE sales SET category=? WHERE platform=? AND username LIKE ? "
                        "AND (category IS NULL OR TRIM(category)='')",
                        (cat, label, f"%[{oid}]%"))
                    tagged += cur.rowcount
    except Exception as e:
        print("[Backfill] FunPay pass error:", str(e)[:80])
    # Pass 1b — u7buy, exact label by order-id.
    try:
        orders, _ = u7buy_fetch_sold_orders()
        for o in orders:
            cat, oid = o.get("category", ""), o.get("order_id")
            if cat and oid:
                cur = db.execute(
                    "UPDATE sales SET category=? WHERE platform='u7buy' AND username LIKE ? "
                    "AND (category IS NULL OR TRIM(category)='')",
                    (cat, f"%[{oid}]%"))
                tagged += cur.rowcount
    except Exception as e:
        print("[Backfill] u7buy pass error:", str(e)[:80])
    # Pass 2 — keyword-classify whatever is still blank (Eldorado/G2G/PA history
    # + anything not matched above).
    rows = db.execute(
        "SELECT id, username, platform FROM sales WHERE category IS NULL OR TRIM(category)=''"
    ).fetchall()
    for r in rows:
        c = _classify_category_from_title(r["username"])
        if not c and r["platform"] == "g2g":
            c = "Roblox"
        if c:
            db.execute("UPDATE sales SET category=? WHERE id=?", (c, r["id"]))
            tagged += 1
    db.commit()
    db.close()
    if tagged:
        print(f"[Backfill] Tagged {tagged} sales rows with a category")
    return tagged


# ═══════════════════════════════════════════════════════════════
#  Cache refresh (FunPay / Eldorado / G2G only)
# ═══════════════════════════════════════════════════════════════

def _refresh_orders_cache(platform):
    try:
        if platform == "funpay":
            s = funpay_session()
            if s:
                orders = funpay_get_orders(s)
                cache_set("orders_funpay", orders)
        elif platform == "eldorado":
            orders, _ = eldorado_fetch_sold_orders()
            if orders:
                cache_set("orders_eldorado", orders)
        elif platform == "g2g":
            orders, _ = g2g_fetch_sold_orders()
            if orders:
                cache_set("orders_g2g", orders)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
#  Routes — Pages
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


def _collect_api_docs():
    """Build the live API index from Flask's URL map + each view's docstring."""
    groups = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        path = str(rule.rule)
        methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
        view = app.view_functions.get(rule.endpoint)
        doc = " ".join((view.__doc__ or "").split()) if view and view.__doc__ else ""
        if not path.startswith("/api/"):
            group = "pages"
        else:
            seg = [p for p in path.split("/") if p and not p.startswith("<")]
            group = seg[1] if len(seg) > 1 else "api"
        groups.setdefault(group, []).append({"path": path, "methods": methods, "doc": doc})
    for g in groups.values():
        g.sort(key=lambda r: r["path"])
    return dict(sorted(groups.items()))


def _render_api_docs_html(groups):
    count = sum(len(v) for v in groups.values())
    body = []
    for group in groups:
        body.append(f"<h2>{html_mod.escape(group)}</h2><table>")
        for r in groups[group]:
            body.append(
                f"<tr><td class=m>{html_mod.escape(' '.join(r['methods']))}</td>"
                f"<td class=p><code>{html_mod.escape(r['path'])}</code></td>"
                f"<td class=d>{html_mod.escape(r['doc'])}</td></tr>")
        body.append("</table>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>API Docs</title><style>
body{{font-family:'Segoe UI',sans-serif;background:#0c0c1d;color:#e0e0f0;margin:0;padding:24px}}
h1{{color:#ff5733;margin:0 0 4px}} h2{{color:#f5a623;margin:26px 0 2px;border-bottom:1px solid #2a2a4a;padding-bottom:4px;text-transform:capitalize}}
table{{width:100%;border-collapse:collapse}} td{{padding:6px 10px;border-bottom:1px solid #16162e;vertical-align:top;font-size:13px}}
.m{{color:#00dc82;font-weight:600;white-space:nowrap;font-family:Consolas,monospace}} .p code{{color:#3498db}} .d{{color:#8888aa}}
.sub{{color:#8888aa;margin:0 0 8px}}</style></head><body>
<h1>Revenue Dashboard — API</h1>
<p class=sub>{count} endpoints · auto-generated from live routes · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
{''.join(body)}</body></html>"""


@app.route("/api/docs")
def api_docs():
    """Self-documenting API index — auto-generated from the live Flask routes and
    their docstrings, so it always reflects the current API. JSON by default;
    `?format=html` renders a browsable page."""
    groups = _collect_api_docs()
    if (request.args.get("format") or "").lower() == "html":
        return Response(_render_api_docs_html(groups), mimetype="text/html")
    return jsonify({"generated_at": datetime.now().isoformat(),
                    "count": sum(len(v) for v in groups.values()), "groups": groups})


# ═══════════════════════════════════════════════════════════════
#  Routes — Dashboard Stats
# ═══════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    db = get_db()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    week_end = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    rev_today = db.execute("SELECT COALESCE(SUM(price),0) FROM sales WHERE date(sold_at)=?", (today,)).fetchone()[0]
    rev_week = db.execute("SELECT COALESCE(SUM(price),0) FROM sales WHERE date(sold_at)>=? AND date(sold_at)<=?", (week_start, week_end)).fetchone()[0]
    rev_month = db.execute("SELECT COALESCE(SUM(price),0) FROM sales WHERE date(sold_at)>=?", (month_start,)).fetchone()[0]
    rev_total = db.execute("SELECT COALESCE(SUM(price),0) FROM sales").fetchone()[0]

    def _rev(plat, when_clause="", when_params=()):
        return db.execute(
            f"SELECT COALESCE(SUM(price),0) FROM sales WHERE platform=? {when_clause}",
            (plat,) + when_params
        ).fetchone()[0]

    rev_funpay   = _rev("funpay")
    rev_funpay2  = _rev("funpay2")
    rev_u7buy    = _rev("u7buy")
    rev_eldorado = _rev("eldorado")
    rev_g2g      = _rev("g2g")
    rev_pa       = _rev("playerauctions")

    today_q = ("AND date(sold_at)=?", (today,))
    week_q  = ("AND date(sold_at)>=? AND date(sold_at)<=?", (week_start, week_end))
    month_q = ("AND date(sold_at)>=?", (month_start,))

    rev_funpay_today   = _rev("funpay",         *today_q)
    rev_funpay2_today  = _rev("funpay2",        *today_q)
    rev_u7buy_today    = _rev("u7buy",          *today_q)
    rev_eldorado_today = _rev("eldorado",       *today_q)
    rev_g2g_today      = _rev("g2g",            *today_q)
    rev_pa_today       = _rev("playerauctions", *today_q)

    rev_funpay_week   = _rev("funpay",         *week_q)
    rev_funpay2_week  = _rev("funpay2",        *week_q)
    rev_u7buy_week    = _rev("u7buy",          *week_q)
    rev_eldorado_week = _rev("eldorado",       *week_q)
    rev_g2g_week      = _rev("g2g",            *week_q)
    rev_pa_week       = _rev("playerauctions", *week_q)

    rev_funpay_month   = _rev("funpay",         *month_q)
    rev_funpay2_month  = _rev("funpay2",        *month_q)
    rev_u7buy_month    = _rev("u7buy",          *month_q)
    rev_eldorado_month = _rev("eldorado",       *month_q)
    rev_g2g_month      = _rev("g2g",            *month_q)
    rev_pa_month       = _rev("playerauctions", *month_q)

    sales_today = db.execute("SELECT COUNT(*) FROM sales WHERE date(sold_at)=?", (today,)).fetchone()[0]
    sales_week = db.execute("SELECT COUNT(*) FROM sales WHERE date(sold_at)>=? AND date(sold_at)<=?", (week_start, week_end)).fetchone()[0]
    sales_month = db.execute("SELECT COUNT(*) FROM sales WHERE date(sold_at)>=?", (month_start,)).fetchone()[0]
    sales_total = db.execute("SELECT COUNT(*) FROM sales").fetchone()[0]

    # Per-platform sales counts for today / week / month (for the Revenue
    # breakdown panel — drives the "N sales" column for each platform row).
    def _per_platform_counts(sql, params):
        out = {"funpay": 0, "funpay2": 0, "u7buy": 0, "eldorado": 0, "g2g": 0, "playerauctions": 0}
        for r in db.execute(sql, params).fetchall():
            out[r["platform"]] = r["cnt"]
        return out

    today_counts = _per_platform_counts(
        "SELECT platform, COUNT(*) cnt FROM sales WHERE date(sold_at)=? GROUP BY platform",
        (today,)
    )
    week_counts = _per_platform_counts(
        "SELECT platform, COUNT(*) cnt FROM sales WHERE date(sold_at)>=? AND date(sold_at)<=? GROUP BY platform",
        (week_start, week_end)
    )
    month_counts = _per_platform_counts(
        "SELECT platform, COUNT(*) cnt FROM sales WHERE date(sold_at)>=? GROUP BY platform",
        (month_start,)
    )

    monthly_rows = db.execute("""
        SELECT strftime('%Y-%m', sold_at) as m, platform, COALESCE(SUM(price),0) as rev, COUNT(*) as cnt
        FROM sales GROUP BY m, platform ORDER BY m DESC LIMIT 60
    """).fetchall()
    monthly = {}
    for r in monthly_rows:
        m = r["m"]
        if not m:
            continue
        if m not in monthly:
            monthly[m] = {"month": m, "funpay": 0, "funpay2": 0, "u7buy": 0,
                          "eldorado": 0, "g2g": 0, "playerauctions": 0,
                          "total": 0, "count": 0}
        # GROUP BY platform may include rows for platforms we don't list here
        # (legacy / spelling variations); ignore them so the dict stays clean.
        if r["platform"] in monthly[m]:
            monthly[m][r["platform"]] = round(r["rev"], 2)
        monthly[m]["count"] += r["cnt"]
        monthly[m]["total"] = round(
            monthly[m]["funpay"] + monthly[m]["funpay2"] + monthly[m]["u7buy"]
            + monthly[m]["eldorado"] + monthly[m]["g2g"] + monthly[m]["playerauctions"], 2)

    db.close()
    return jsonify({
        "revenue": {
            "today": round(rev_today, 2), "week": round(rev_week, 2),
            "month": round(rev_month, 2), "total": round(rev_total, 2),
            "funpay": round(rev_funpay, 2), "funpay2": round(rev_funpay2, 2),
            "u7buy": round(rev_u7buy, 2),
            "eldorado": round(rev_eldorado, 2), "g2g": round(rev_g2g, 2),
            "playerauctions": round(rev_pa, 2),
            "funpay_today": round(rev_funpay_today, 2), "funpay2_today": round(rev_funpay2_today, 2),
            "u7buy_today": round(rev_u7buy_today, 2),
            "eldorado_today": round(rev_eldorado_today, 2),
            "g2g_today": round(rev_g2g_today, 2), "pa_today": round(rev_pa_today, 2),
            "funpay_week": round(rev_funpay_week, 2), "funpay2_week": round(rev_funpay2_week, 2),
            "u7buy_week": round(rev_u7buy_week, 2),
            "eldorado_week": round(rev_eldorado_week, 2),
            "g2g_week": round(rev_g2g_week, 2), "pa_week": round(rev_pa_week, 2),
            "funpay_month": round(rev_funpay_month, 2), "funpay2_month": round(rev_funpay2_month, 2),
            "u7buy_month": round(rev_u7buy_month, 2),
            "eldorado_month": round(rev_eldorado_month, 2),
            "g2g_month": round(rev_g2g_month, 2), "pa_month": round(rev_pa_month, 2),
        },
        "sales_count": {
            "today": sales_today, "week": sales_week,
            "month": sales_month, "total": sales_total,
            "funpay_today":   today_counts["funpay"],
            "funpay2_today":  today_counts["funpay2"],
            "u7buy_today":    today_counts["u7buy"],
            "eldorado_today": today_counts["eldorado"],
            "g2g_today":      today_counts["g2g"],
            "pa_today":       today_counts["playerauctions"],
            "funpay_week":    week_counts["funpay"],
            "funpay2_week":   week_counts["funpay2"],
            "u7buy_week":     week_counts["u7buy"],
            "eldorado_week":  week_counts["eldorado"],
            "g2g_week":       week_counts["g2g"],
            "pa_week":        week_counts["playerauctions"],
            "funpay_month":   month_counts["funpay"],
            "funpay2_month":  month_counts["funpay2"],
            "u7buy_month":    month_counts["u7buy"],
            "eldorado_month": month_counts["eldorado"],
            "g2g_month":      month_counts["g2g"],
            "pa_month":       month_counts["playerauctions"],
        },
        "periods": {
            "today": today,
            "week": f"{week_start} to {week_end}",
            "month": f"{month_start} to {now.strftime('%Y-%m-%d')}",
        },
        "monthly_history": list(monthly.values()),
    })


@app.route("/api/revenue")
def api_revenue():
    period = request.args.get("period", "daily")
    db = get_db()
    if period == "daily":
        rows = db.execute("""
            SELECT date(sold_at) as d, platform, COALESCE(SUM(price),0) as rev, COUNT(*) as cnt
            FROM sales WHERE sold_at >= date('now','-30 days')
            GROUP BY d, platform ORDER BY d
        """).fetchall()
    elif period == "weekly":
        rows = db.execute("""
            SELECT strftime('%Y-W%W', sold_at) as d, platform,
                   COALESCE(SUM(price),0) as rev, COUNT(*) as cnt
            FROM sales WHERE sold_at >= date('now','-90 days')
            GROUP BY d, platform ORDER BY d
        """).fetchall()
    else:
        rows = db.execute("""
            SELECT strftime('%Y-%m', sold_at) as d, platform,
                   COALESCE(SUM(price),0) as rev, COUNT(*) as cnt
            FROM sales GROUP BY d, platform ORDER BY d
        """).fetchall()
    db.close()
    data = {}
    for r in rows:
        d = r["d"]
        if not d:
            continue
        if d not in data:
            data[d] = {"date": d, "funpay": 0, "funpay2": 0, "u7buy": 0, "eldorado": 0,
                       "g2g": 0, "playerauctions": 0, "total": 0,
                       "funpay_count": 0, "funpay2_count": 0, "u7buy_count": 0,
                       "eldorado_count": 0, "g2g_count": 0, "playerauctions_count": 0}
        if r["platform"] in data[d]:
            data[d][r["platform"]] = round(r["rev"], 2)
            data[d][f"{r['platform']}_count"] = r["cnt"]
        data[d]["total"] = round(
            data[d]["funpay"] + data[d]["funpay2"] + data[d]["u7buy"]
            + data[d]["eldorado"] + data[d]["g2g"] + data[d]["playerauctions"], 2)
    return jsonify(list(data.values()))


@app.route("/api/revenue/by-category")
def api_revenue_by_category():
    """Revenue grouped by game/category — the same label the live-offers
    sidebar uses. Each category carries its total, its % of the grand total,
    and a per-platform split. period = all | today | week | month."""
    period = request.args.get("period", "all")
    now = datetime.now()
    if period == "today":
        where, params = "WHERE date(sold_at)=?", (now.strftime("%Y-%m-%d"),)
    elif period == "week":
        ws = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        we = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime("%Y-%m-%d")
        where, params = "WHERE date(sold_at)>=? AND date(sold_at)<=?", (ws, we)
    elif period == "month":
        where, params = "WHERE date(sold_at)>=?", (now.strftime("%Y-%m-01"),)
    else:
        period, where, params = "all", "", ()
    db = get_db()
    rows = db.execute(
        "SELECT COALESCE(NULLIF(TRIM(category),''),'Uncategorized') AS cat, "
        "platform, COALESCE(SUM(price),0) AS rev, COUNT(*) AS cnt "
        f"FROM sales {where} GROUP BY cat, platform", params
    ).fetchall()
    db.close()
    cats = {}
    grand = 0.0
    for r in rows:
        c = r["cat"]
        entry = cats.setdefault(c, {"category": c, "total": 0.0, "count": 0, "platforms": {}})
        entry["platforms"][r["platform"]] = round(
            entry["platforms"].get(r["platform"], 0) + r["rev"], 2)
        entry["total"] += r["rev"]
        entry["count"] += r["cnt"]
        grand += r["rev"]
    out = []
    for c in sorted(cats.values(), key=lambda x: -x["total"]):
        c["total"] = round(c["total"], 2)
        c["pct"] = round(c["total"] / grand * 100, 1) if grand else 0
        out.append(c)
    return jsonify({"period": period, "total": round(grand, 2), "categories": out})


# ═══════════════════════════════════════════════════════════════
#  Routes — Sales
# ═══════════════════════════════════════════════════════════════

@app.route("/api/sales/today")
def api_get_sales_today():
    """All sales rows with sold_at on today's date — kept for backward compat.
    Prefer /api/sales/period?p=today."""
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT id, sold_at, username, platform, price FROM sales "
        "WHERE date(sold_at)=? ORDER BY sold_at DESC, id DESC",
        (today,)
    ).fetchall()
    db.close()
    return jsonify({"date": today, "count": len(rows), "sales": [dict(r) for r in rows]})


@app.route("/api/sales/period")
def api_get_sales_period():
    """Sales rows for a period or a specific date.

    Query params (one of):
      * `d=YYYY-MM-DD`  → sales for that exact day (takes precedence over p)
      * `p=today`       → sales for today
      * `p=week`        → this week (Mon-Sun containing today)
      * `p=month`       → this month so far
    Default: today."""
    custom_date = (request.args.get("d") or "").strip()
    if custom_date and re.match(r"^\d{4}-\d{2}-\d{2}$", custom_date):
        db = get_db()
        rows = db.execute(
            "SELECT id, sold_at, username, platform, price, category FROM sales "
            "WHERE date(sold_at)=? ORDER BY sold_at DESC, id DESC",
            (custom_date,)
        ).fetchall()
        db.close()
        return jsonify({"period": "date", "label": custom_date,
                        "date": custom_date, "count": len(rows),
                        "sales": [dict(r) for r in rows]})

    period = (request.args.get("p") or "today").lower()
    now = datetime.now()
    db = get_db()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        end = (now - timedelta(days=now.weekday()) + timedelta(days=6)).strftime("%Y-%m-%d")
        rows = db.execute(
            "SELECT id, sold_at, username, platform, price, category FROM sales "
            "WHERE date(sold_at)>=? AND date(sold_at)<=? ORDER BY sold_at DESC, id DESC",
            (start, end)
        ).fetchall()
        label = f"{start} to {end}"
    elif period == "month":
        start = now.strftime("%Y-%m-01")
        rows = db.execute(
            "SELECT id, sold_at, username, platform, price, category FROM sales "
            "WHERE date(sold_at)>=? ORDER BY sold_at DESC, id DESC",
            (start,)
        ).fetchall()
        label = f"{start} to {now.strftime('%Y-%m-%d')}"
    else:
        period = "today"
        start = now.strftime("%Y-%m-%d")
        rows = db.execute(
            "SELECT id, sold_at, username, platform, price, category FROM sales "
            "WHERE date(sold_at)=? ORDER BY sold_at DESC, id DESC",
            (start,)
        ).fetchall()
        label = start
    db.close()
    return jsonify({"period": period, "label": label, "count": len(rows),
                    "sales": [dict(r) for r in rows]})


@app.route("/api/stats/date")
def api_stats_date():
    """Per-platform revenue + counts for a specific date.

    Query: `d=YYYY-MM-DD` (required).  Response shape mirrors the `_today`
    slice of /api/stats — the frontend can drop the response into the same
    render code by treating "_today" suffix as "the picked date"."""
    d = (request.args.get("d") or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return jsonify({"error": "missing or invalid `d` (expected YYYY-MM-DD)"}), 400
    db = get_db()
    total_rev = db.execute("SELECT COALESCE(SUM(price),0) FROM sales WHERE date(sold_at)=?", (d,)).fetchone()[0]
    total_cnt = db.execute("SELECT COUNT(*) FROM sales WHERE date(sold_at)=?", (d,)).fetchone()[0]
    per_rev = {"funpay": 0, "funpay2": 0, "u7buy": 0, "eldorado": 0, "g2g": 0, "playerauctions": 0}
    per_cnt = dict(per_rev)
    for r in db.execute(
        "SELECT platform, COALESCE(SUM(price),0) rev, COUNT(*) cnt "
        "FROM sales WHERE date(sold_at)=? GROUP BY platform",
        (d,)
    ).fetchall():
        if r["platform"] in per_rev:
            per_rev[r["platform"]] = round(r["rev"], 2)
            per_cnt[r["platform"]] = r["cnt"]
    db.close()
    return jsonify({
        "date": d,
        "revenue": {
            "today":           round(total_rev, 2),
            "funpay_today":    per_rev["funpay"],
            "funpay2_today":   per_rev["funpay2"],
            "u7buy_today":     per_rev["u7buy"],
            "eldorado_today":  per_rev["eldorado"],
            "g2g_today":       per_rev["g2g"],
            "pa_today":        per_rev["playerauctions"],
        },
        "sales_count": {
            "today":          total_cnt,
            "funpay_today":   per_cnt["funpay"],
            "funpay2_today":  per_cnt["funpay2"],
            "u7buy_today":    per_cnt["u7buy"],
            "eldorado_today": per_cnt["eldorado"],
            "g2g_today":      per_cnt["g2g"],
            "pa_today":       per_cnt["playerauctions"],
        },
    })


@app.route("/api/sales")
def api_get_sales():
    platform = request.args.get("platform")
    limit = int(request.args.get("limit", 100))
    db = get_db()
    query = "SELECT * FROM sales WHERE 1=1"
    params = []
    if platform:
        query += " AND platform=?"
        params.append(platform)
    query += " ORDER BY sold_at DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ─── Sales log — full ledger, LAN-accessible (mirrors /api/farmsync/logs) ───
# The DB is the source of truth; these routes read it on every request so the
# response is always live. Filters compose: limit + platform + grep + dates.
#
# /api/sales/all              — text or JSON, with filters
# /api/sales/all/download     — full CSV attachment (raw download)
# /api/sales/all/summary      — JSON stats (totals + per-platform breakdown)
#
# Query params for /api/sales/all:
#   limit    - last N rows (default 500, max 50000).
#              Pass limit=0 / limit=all for the WHOLE table (no cap).
#   platform - filter by platform key (funpay, funpay2, u7buy, eldorado, g2g, playerauctions)
#   grep     - case-insensitive substring filter on username
#   from     - YYYY-MM-DD inclusive lower bound on sold_at
#   to       - YYYY-MM-DD inclusive upper bound on sold_at
#   format   - "text" (default, tab-separated) or "json"


def _parse_sales_limit(raw):
    """limit=N → int; limit=0 / all / full → None (no cap)."""
    if raw is None:
        return 500
    s = str(raw).strip().lower()
    if s in ("all", "full", "0", "-1", ""):
        return None
    try:
        n = int(s)
    except ValueError:
        return 500
    if n <= 0:
        return None
    return min(n, 50000)


def _build_sales_query(args):
    """Build (sql, params) from request.args. Used by /api/sales/all and
    /api/sales/all/download so they share filtering logic."""
    where = ["1=1"]
    params = []
    platform = args.get("platform")
    if platform:
        where.append("platform = ?")
        params.append(platform)
    grep = (args.get("grep") or "").strip()
    if grep:
        where.append("LOWER(username) LIKE ?")
        params.append("%" + grep.lower() + "%")
    date_from = args.get("from")
    if date_from:
        where.append("date(sold_at) >= ?")
        params.append(date_from)
    date_to = args.get("to")
    if date_to:
        where.append("date(sold_at) <= ?")
        params.append(date_to)
    sql = "SELECT id, sold_at, platform, username, price FROM sales WHERE " \
          + " AND ".join(where) + " ORDER BY sold_at DESC, id DESC"
    return sql, params


@app.route("/api/sales/all")
def api_sales_all():
    limit = _parse_sales_limit(request.args.get("limit"))
    fmt = (request.args.get("format") or "text").lower()
    sql, params = _build_sales_query(request.args)
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    db = get_db()
    rows = db.execute(sql, params).fetchall()
    db.close()

    if fmt == "json":
        return jsonify({
            "count":    len(rows),
            "limit":    "all" if limit is None else limit,
            "platform": request.args.get("platform") or None,
            "grep":     request.args.get("grep") or None,
            "from":     request.args.get("from") or None,
            "to":       request.args.get("to") or None,
            "sales":    [dict(r) for r in rows],
        })

    # Plain text: tab-separated, one row per line, ready for grep/awk piping
    lines = ["id\tsold_at\tplatform\tprice\tusername"]
    for r in rows:
        d = dict(r)
        lines.append(
            f"{d.get('id','')}\t"
            f"{d.get('sold_at','')}\t"
            f"{d.get('platform','')}\t"
            f"{d.get('price','')}\t"
            f"{(d.get('username') or '').replace(chr(9),' ')}"
        )
    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/api/sales/all/download")
def api_sales_all_download():
    """Stream the (filtered) sales ledger as CSV. Supports the same filters
    as /api/sales/all but with no row cap by default — meant for full
    backups / external analysis."""
    import csv, io
    sql, params = _build_sales_query(request.args)
    # Allow explicit limit, otherwise return everything
    limit = _parse_sales_limit(request.args.get("limit"))
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    db = get_db()
    rows = db.execute(sql, params).fetchall()
    db.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "sold_at", "platform", "price", "username"])
    for r in rows:
        d = dict(r)
        w.writerow([d.get("id", ""), d.get("sold_at", ""), d.get("platform", ""),
                    d.get("price", ""), d.get("username", "")])
    body = buf.getvalue()
    fname_parts = ["sales"]
    if request.args.get("platform"):
        fname_parts.append(request.args["platform"])
    if request.args.get("from"):
        fname_parts.append(request.args["from"])
    if request.args.get("to"):
        fname_parts.append(request.args["to"])
    filename = "_".join(fname_parts) + ".csv"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    return Response(body, mimetype="text/csv; charset=utf-8", headers=headers)


@app.route("/sales-log")
def page_sales_log():
    """Browser-friendly sales-ledger viewer. Self-contained HTML (no external
    deps) so it works from any device on the LAN without loading static files."""
    return Response(_SALES_LOG_VIEWER_HTML, mimetype="text/html; charset=utf-8")


_SALES_LOG_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sales Ledger</title>
<style>
  :root {
    --bg: #0f1117; --panel: #161922; --border: #262a36;
    --text: #d8dbe6; --muted: #7a8092; --accent: #5bc0eb;
    --green: #4caf50; --red: #ef5350; --yellow: #f5b800; --purple: #9c6cf2;
  }
  * { box-sizing: border-box; }
  html,body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
  }
  header {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 10px 16px; display: flex; flex-direction: column; gap: 8px;
    position: sticky; top: 0; z-index: 10;
  }
  .row-1 { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .row-2 { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  header h1 { margin: 0; font-size: 14px; font-weight: 600; }
  header h1 i { color: var(--accent); margin-right: 6px; }
  .summary-chip {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 3px 10px;
    font-size: 11px;
    color: var(--muted);
  }
  .summary-chip b { color: var(--text); font-weight: 600; }
  .row-2 select, .row-2 input, .row-2 button, .row-2 label {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 10px; font-size: 12px;
    font-family: inherit;
  }
  .row-2 input[type=date] { padding: 4px 8px; }
  .row-2 input[type=text] { width: 180px; }
  .row-2 button { cursor: pointer; }
  .row-2 button:hover { border-color: var(--accent); }
  .row-2 button.primary { background: var(--accent); color: var(--bg); border-color: var(--accent); font-weight: 600; }
  .row-2 label { display: inline-flex; align-items: center; gap: 5px;
                 background: transparent; border-color: transparent; padding: 5px 4px; }
  .status-dot { display: inline-block; width: 8px; height: 8px;
                border-radius: 50%; background: var(--muted); margin-right: 4px; }
  .status-dot.live { background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
  main { padding: 12px 16px; }
  table {
    width: 100%; border-collapse: collapse;
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden;
  }
  thead { position: sticky; top: 90px; background: var(--panel); }
  th, td { padding: 8px 10px; text-align: left; border-bottom: 1px solid var(--border); }
  th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
       color: var(--muted); font-weight: 600; }
  tbody tr:hover { background: rgba(91, 192, 235, 0.05); }
  td.date-cell { font-size: 11px; color: var(--muted); white-space: nowrap; font-family: "Cascadia Code", monospace; }
  td.id-cell { font-family: "Cascadia Code", monospace; font-size: 11px; color: var(--muted); width: 64px; }
  td.title-cell { font-family: "Cascadia Code", monospace; font-size: 12px; max-width: 600px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  td.price-cell { text-align: right; font-weight: 600; white-space: nowrap; width: 90px; }
  .empty { color: var(--muted); padding: 30px; text-align: center; }
  .platform-tag {
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 10px; font-weight: 600; text-transform: uppercase;
  }
  .platform-tag.funpay         { background: rgba(76,175,80,0.18);  color: #4caf50; }
  .platform-tag.funpay2        { background: rgba(44,82,130,0.32);  color: #5b9ddf; }
  .platform-tag.u7buy          { background: rgba(245,184,0,0.18);  color: #f5b800; }
  .platform-tag.eldorado       { background: rgba(91,192,235,0.18); color: #5bc0eb; }
  .platform-tag.g2g            { background: rgba(239,83,80,0.18);  color: #ef5350; }
  .platform-tag.playerauctions { background: rgba(156,108,242,0.18); color: #9c6cf2; }
  th.sortable { cursor: pointer; user-select: none; transition: background .12s; }
  th.sortable:hover { background: rgba(91,192,235,0.06); }
  th .sort-icon { margin-left: 4px; font-size: 10px; opacity: 0.4; }
  th.sort-active .sort-icon { opacity: 1; color: var(--accent); }
  mark { background: #f5b80055; color: inherit; padding: 0 2px; border-radius: 2px; }
</style>
</head>
<body>
<header>
  <div class="row-1">
    <h1>Sales Ledger</h1>
    <span class="summary-chip"><span class="status-dot live" id="livedot"></span><span id="meta-status">loading…</span></span>
    <span class="summary-chip"><b id="meta-count">—</b> sales</span>
    <span class="summary-chip"><b id="meta-revenue">—</b> revenue</span>
    <span class="summary-chip" id="meta-range">—</span>
  </div>
  <div class="row-2">
    <select id="platform" title="Platform filter">
      <option value="">All platforms</option>
      <option value="eldorado">Eldorado</option>
      <option value="funpay">FunPay</option>
      <option value="funpay2">FunPay 2</option>
      <option value="u7buy">u7buy</option>
      <option value="g2g">G2G</option>
      <option value="playerauctions">PlayerAuctions</option>
    </select>
    <input type="text" id="grep" placeholder="search description…">
    <input type="date" id="date-from" title="From date">
    <input type="date" id="date-to" title="To date">
    <select id="limit" title="Row cap">
      <option value="500">Last 500</option>
      <option value="2000">Last 2000</option>
      <option value="10000">Last 10000</option>
      <option value="all">All</option>
    </select>
    <label><input type="checkbox" id="autorefresh"> auto-refresh 60s</label>
    <button onclick="refreshSales()">Refresh</button>
    <button class="primary" onclick="downloadCsv()">Download CSV</button>
  </div>
</header>
<main>
  <table>
    <thead>
      <tr>
        <th class="sortable" data-sort="id">ID <i class="sort-icon">↕</i></th>
        <th class="sortable" data-sort="sold_at">Date <i class="sort-icon">↕</i></th>
        <th class="sortable" data-sort="platform">Platform <i class="sort-icon">↕</i></th>
        <th>Description</th>
        <th class="sortable" data-sort="price">Price <i class="sort-icon">↕</i></th>
      </tr>
    </thead>
    <tbody id="sales-body"><tr><td colspan="5" class="empty">Loading…</td></tr></tbody>
  </table>
</main>
<script>
const $ = (id) => document.getElementById(id);
const LIVE_DOT = $('livedot');
const META_STATUS = $('meta-status');
const META_COUNT = $('meta-count');
const META_REVENUE = $('meta-revenue');
const META_RANGE = $('meta-range');
const BODY = $('sales-body');
let timer = null;
let _rows = [];
let _sortKey = localStorage.getItem('salesSortKey') || 'sold_at';
let _sortDir = localStorage.getItem('salesSortDir') || 'desc';

function fmtPrice(p) {
  if (p == null || p === '') return '—';
  return '$' + Number(p).toFixed(2);
}
function fmtRevenue(p) {
  if (p == null) return '—';
  return '$' + Number(p).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function fmtDate(s) {
  if (!s) return '';
  // sqlite returns "2026-05-27T12:32:43" — split and prettify
  return s.replace('T', ' ').replace(/\\.\\d+$/, '');
}
function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function highlightGrep(html, needle) {
  if (!needle) return html;
  const re = new RegExp(needle.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'gi');
  return html.replace(re, m => '<mark>' + m + '</mark>');
}
function buildQs() {
  const qs = new URLSearchParams();
  const platform = $('platform').value;
  if (platform) qs.set('platform', platform);
  const grep = $('grep').value.trim();
  if (grep) qs.set('grep', grep);
  const from = $('date-from').value;
  if (from) qs.set('from', from);
  const to = $('date-to').value;
  if (to) qs.set('to', to);
  const limit = $('limit').value;
  if (limit) qs.set('limit', limit);
  return qs.toString();
}
function sortRows(rows) {
  const key = _sortKey, dir = _sortDir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    const va = a[key], vb = b[key];
    const aEmpty = (va == null || va === '');
    const bEmpty = (vb == null || vb === '');
    if (aEmpty && bEmpty) return 0;
    if (aEmpty) return 1;
    if (bEmpty) return -1;
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
  });
}
function renderTable() {
  const grep = $('grep').value.trim();
  const rows = sortRows(_rows);
  if (!rows.length) {
    BODY.innerHTML = '<tr><td colspan="5" class="empty">No sales match these filters</td></tr>';
    return;
  }
  const CAP = 1500;
  const capped = rows.slice(0, CAP);
  BODY.innerHTML = capped.map(r => {
    const tag = `<span class="platform-tag ${escHtml(r.platform)}">${escHtml(r.platform)}</span>`;
    let title = escHtml(r.username || '');
    title = highlightGrep(title, grep);
    return `<tr>
      <td class="id-cell">${escHtml(r.id)}</td>
      <td class="date-cell">${escHtml(fmtDate(r.sold_at))}</td>
      <td>${tag}</td>
      <td class="title-cell" title="${escHtml(r.username || '')}">${title}</td>
      <td class="price-cell">${escHtml(fmtPrice(r.price))}</td>
    </tr>`;
  }).join('');
  if (rows.length > CAP) {
    BODY.innerHTML += `<tr><td colspan="5" class="empty">(showing first ${CAP} of ${rows.length} — narrow filters to see more)</td></tr>`;
  }
  // sort indicator
  document.querySelectorAll('th.sortable').forEach(th => {
    const active = th.dataset.sort === _sortKey;
    th.classList.toggle('sort-active', active);
    const icon = th.querySelector('.sort-icon');
    if (icon) icon.textContent = !active ? '↕' : (_sortDir === 'asc' ? '↑' : '↓');
  });
}
async function refreshSales() {
  LIVE_DOT.classList.remove('live');
  META_STATUS.textContent = 'fetching…';
  const qs = buildQs();
  try {
    const r = await fetch('/api/sales/all?format=json&' + qs, { cache: 'no-store' });
    const j = await r.json();
    _rows = j.sales || [];
    renderTable();
    // Independent summary fetch (filters apply)
    const s = await fetch('/api/sales/all/summary?' + qs, { cache: 'no-store' });
    const sj = await s.json();
    META_COUNT.textContent = (sj.total_count || 0).toLocaleString();
    META_REVENUE.textContent = fmtRevenue(sj.total_revenue);
    META_RANGE.textContent = sj.first_sold
      ? `${fmtDate(sj.first_sold).slice(0,10)} → ${fmtDate(sj.last_sold).slice(0,10)}`
      : 'no sales';
    META_STATUS.textContent = `updated ${new Date().toLocaleTimeString()} · showing ${(_rows.length).toLocaleString()} rows`;
    LIVE_DOT.classList.add('live');
  } catch (e) {
    META_STATUS.textContent = 'fetch failed: ' + e.message;
  }
}
function downloadCsv() {
  const qs = buildQs();
  window.location.href = '/api/sales/all/download' + (qs ? '?' + qs : '');
}
function scheduleAutorefresh() {
  if (timer) { clearInterval(timer); timer = null; }
  if ($('autorefresh').checked) timer = setInterval(refreshSales, 60000);
}
// Filter handlers
['platform', 'limit'].forEach(id => $(id).addEventListener('change', refreshSales));
$('grep').addEventListener('input', () => {
  clearTimeout(window._grepT);
  window._grepT = setTimeout(refreshSales, 400);
});
['date-from', 'date-to'].forEach(id => $(id).addEventListener('change', refreshSales));
$('autorefresh').addEventListener('change', scheduleAutorefresh);
// Sort handlers
document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (key === _sortKey) {
      _sortDir = _sortDir === 'asc' ? 'desc' : 'asc';
    } else {
      _sortKey = key;
      _sortDir = (key === 'price' || key === 'sold_at' || key === 'id') ? 'desc' : 'asc';
    }
    try {
      localStorage.setItem('salesSortKey', _sortKey);
      localStorage.setItem('salesSortDir', _sortDir);
    } catch (e) {}
    renderTable();
  });
});
refreshSales();
scheduleAutorefresh();
</script>
</body>
</html>
"""


@app.route("/api/sales/all/summary")
def api_sales_all_summary():
    """JSON summary of the (filtered) sales ledger — totals + per-platform
    breakdown + min/max sold_at. Doesn't return individual rows; cheap to
    poll for status dashboards."""
    sql_base, params = _build_sales_query(request.args)
    db = get_db()
    where_clause = sql_base.split(" WHERE ", 1)[1].split(" ORDER BY", 1)[0]
    total_row = db.execute(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(price),0) AS revenue, "
        f"MIN(sold_at) AS first_sold, MAX(sold_at) AS last_sold "
        f"FROM sales WHERE {where_clause}",
        params,
    ).fetchone()
    plat_rows = db.execute(
        f"SELECT platform, COUNT(*) AS n, COALESCE(SUM(price),0) AS revenue "
        f"FROM sales WHERE {where_clause} GROUP BY platform "
        f"ORDER BY revenue DESC",
        params,
    ).fetchall()
    db.close()
    return jsonify({
        "filters": {
            "platform": request.args.get("platform") or None,
            "grep":     request.args.get("grep") or None,
            "from":     request.args.get("from") or None,
            "to":       request.args.get("to") or None,
        },
        "total_count":   total_row["n"],
        "total_revenue": round(float(total_row["revenue"]), 2),
        "first_sold":    total_row["first_sold"],
        "last_sold":     total_row["last_sold"],
        "by_platform": [
            {"platform": r["platform"], "count": r["n"], "revenue": round(float(r["revenue"]), 2)}
            for r in plat_rows
        ],
    })


# ═══════════════════════════════════════════════════════════════
#  Routes — Orders (cache-first, background refresh)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/orders/funpay")
def api_funpay_orders():
    cached, updated_at = cache_get("orders_funpay")
    if cached is not None:
        threading.Thread(target=_refresh_orders_cache, args=("funpay",), daemon=True).start()
        return jsonify({"orders": cached, "count": len(cached), "cached": updated_at})
    try:
        s = funpay_session()
        if not s:
            return jsonify({"error": "FunPay cookies not found"}), 401
        orders = funpay_get_orders(s)
        cache_set("orders_funpay", orders)
        return jsonify({"orders": orders, "count": len(orders)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/u7buy")
def api_u7buy_orders():
    """Live u7buy sold-order scrape via Edge CDP (no cache)."""
    try:
        orders, msg = u7buy_fetch_sold_orders()
        return jsonify({"orders": orders, "count": len(orders), "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/eldorado")
def api_eldorado_orders():
    cached, updated_at = cache_get("orders_eldorado")
    if cached is not None:
        threading.Thread(target=_refresh_orders_cache, args=("eldorado",), daemon=True).start()
        return jsonify({"orders": cached, "count": len(cached), "cached": updated_at})
    try:
        orders, msg = eldorado_fetch_sold_orders()
        if orders:
            cache_set("orders_eldorado", orders)
        return jsonify({"orders": orders, "count": len(orders), "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/g2g")
def api_g2g_orders():
    cached, updated_at = cache_get("orders_g2g")
    if cached is not None:
        threading.Thread(target=_refresh_orders_cache, args=("g2g",), daemon=True).start()
        return jsonify({"orders": cached, "count": len(cached), "cached": updated_at})
    try:
        orders, msg = g2g_fetch_sold_orders()
        if orders:
            cache_set("orders_g2g", orders)
        return jsonify({"orders": orders, "count": len(orders), "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/playerauctions")
def api_pa_orders():
    cached, updated_at = cache_get("orders_playerauctions")
    if cached is not None:
        return jsonify({"orders": cached, "count": len(cached), "cached": updated_at})
    try:
        orders, msg = pa_fetch_orders_selenium()
        if orders:
            cache_set("orders_playerauctions", orders)
        return jsonify({"orders": orders, "count": len(orders), "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  Routes — Sync to revenue DB
# ═══════════════════════════════════════════════════════════════

@app.route("/api/orders/funpay/sync-sales", methods=["POST"])
def api_sync_funpay_sales():
    try:
        new_sales, msg = _sync_funpay_sales()
        return jsonify({"new_sales": new_sales, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/categories/backfill", methods=["POST"])
def api_backfill_categories():
    """Re-tag sales rows missing a category (exact label by re-scrape where
    cheap, keyword-classify the rest). Safe to call repeatedly."""
    try:
        tagged = _backfill_categories()
        return jsonify({"tagged": tagged})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/u7buy/sync-sales", methods=["POST"])
def api_sync_u7buy_sales():
    try:
        new_sales, msg = _sync_u7buy_sales()
        return jsonify({"new_sales": new_sales, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/eldorado/sync-sales", methods=["POST"])
def api_sync_eldorado_sales():
    try:
        new_sales, msg = _sync_eldorado_sales()
        return jsonify({"new_sales": new_sales, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/g2g/sync-sales", methods=["POST"])
def api_sync_g2g_sales():
    try:
        new_sales, msg = _sync_g2g_sales()
        return jsonify({"new_sales": new_sales, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/playerauctions/sync-sales", methods=["POST"])
def api_sync_pa_sales():
    try:
        new_sales, msg = _sync_pa_sales()
        return jsonify({"new_sales": new_sales, "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  Routes — Platform Status
# ═══════════════════════════════════════════════════════════════

_platform_status_cache = {"funpay": "disconnected", "funpay2": "disconnected",
                          "u7buy": "disconnected",
                          "eldorado": "disconnected", "g2g": "disconnected",
                          "playerauctions": "disconnected"}
_platform_status_lock = threading.Lock()
# Shared status snapshot the FarmSync Automation reads at webhook time to
# include connection status in the Cycle Summary embed.
PLATFORM_STATUS_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "_platform_status.json")


def _update_platform_status():
    # Start with every FunPay account marked disconnected (handles funpay,
    # funpay2, funpay3, ...). Other platforms get their own keys below.
    status = {label: "disconnected" for label, _ in funpay_account_paths()}
    status.update({
        "u7buy": "disconnected",
        "eldorado": "disconnected",
        "g2g": "disconnected",
        "playerauctions": "disconnected",
    })
    # FunPay — HTTP. Probe every account independently.
    for label, cookie_file in funpay_account_paths():
        try:
            s = funpay_session(cookie_file)
            if not s:
                continue
            xhr = s.headers.pop("X-Requested-With", None)
            try:
                r = s.get("https://funpay.com/en/lots/927/trade", timeout=5)
                if "menu-item-login" not in r.text and r.status_code == 200:
                    status[label] = "connected"
            except Exception:
                pass
            finally:
                if xhr:
                    s.headers["X-Requested-With"] = xhr
        except Exception:
            pass
    # u7buy — OpenAPI Basic auth
    try:
        if u7buy_api_probe():
            status["u7buy"] = "connected"
    except Exception:
        pass
    # Eldorado — Chrome
    try:
        if eldorado_logged_in():
            status["eldorado"] = "connected"
        elif eldorado_logged_out:
            status["eldorado"] = "logged_out"
    except Exception:
        pass
    # G2G — Chrome
    try:
        if g2g_logged_in():
            status["g2g"] = "connected"
        elif g2g_logged_out:
            status["g2g"] = "logged_out"
    except Exception:
        pass
    # PA — Chrome
    try:
        if pa_logged_in():
            status["playerauctions"] = "connected"
        elif pa_logged_out:
            status["playerauctions"] = "logged_out"
    except Exception:
        pass
    status["eldorado_logged_out"] = bool(eldorado_logged_out)
    status["g2g_logged_out"] = bool(g2g_logged_out)
    status["pa_logged_out"] = bool(pa_logged_out)
    with _platform_status_lock:
        _platform_status_cache.clear()
        _platform_status_cache.update(status)
    # Write the snapshot to disk so the automation can pick it up in its webhook
    try:
        snapshot = {"ts": time.time(), **{k: v for k, v in status.items() if not k.endswith("_logged_out")}}
        with open(PLATFORM_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f)
    except Exception:
        pass


@app.route("/api/platform/status")
def api_platform_status():
    threading.Thread(target=_update_platform_status, daemon=True).start()
    with _platform_status_lock:
        return jsonify(dict(_platform_status_cache))


@app.route("/api/platform/refresh-all", methods=["POST"])
def api_refresh_all():
    results = {}
    # FunPay — cookie-file driven; just probe
    try:
        s = funpay_session()
        ok = False
        if s:
            xhr = s.headers.pop("X-Requested-With", None)
            try:
                r = s.get("https://funpay.com/en/lots/927/trade", timeout=5)
                ok = (r.status_code == 200 and "menu-item-login" not in r.text)
            except Exception:
                pass
            finally:
                if xhr:
                    s.headers["X-Requested-With"] = xhr
        results["funpay"] = {"ok": ok,
                             "message": "Session live" if ok else "FunPay cookies missing/expired — run funpay/refresh_cookies.py"}
    except Exception as e:
        results["funpay"] = {"ok": False, "message": str(e)[:80]}
    # u7buy — OpenAPI Basic auth (no token refresh needed)
    try:
        ok = u7buy_api_probe()
        results["u7buy"] = {"ok": ok,
                            "message": "OpenAPI auth OK" if ok else "OpenAPI auth failed — check u7buy/u7buy_apikey.txt"}
    except Exception as e:
        results["u7buy"] = {"ok": False, "message": str(e)[:80]}
    for plat, fn in [("eldorado", eldorado_logged_in),
                     ("g2g", g2g_logged_in),
                     ("playerauctions", pa_logged_in)]:
        try:
            ok = fn()
            results[plat] = {"ok": ok,
                             "message": "Logged in (Chrome Profile 3)" if ok
                             else "Not logged in — run login.bat"}
        except Exception as e:
            results[plat] = {"ok": False, "message": str(e)[:80]}
    return jsonify(results)


# ═══════════════════════════════════════════════════════════════
#  FarmSync — Roblox device-farm REST API (sibling repo, read-only)
# ═══════════════════════════════════════════════════════════════
#  Auth: Bearer token from FARMSYNC_APIKEY_FILE (first line).
#  Devices + accounts list are cached together for FARMSYNC_CACHE_TTL seconds
#  so dashboard + Devices page stay snappy without hammering the cloud API.
#  Read-only — no mutations to the FarmSync side from this app.

_farmsync_cache = {"devices": [], "accounts": [], "ts": 0.0, "error": None}
_farmsync_cache_lock = threading.Lock()


def farmsync_api_key():
    if not os.path.exists(FARMSYNC_APIKEY_FILE):
        return None
    try:
        with open(FARMSYNC_APIKEY_FILE, "r", encoding="utf-8") as f:
            key = f.readline().strip()
        return key or None
    except Exception:
        return None


def farmsync_fetch(path):
    """GET to FarmSync via curl subprocess. Returns (json_or_none, error_or_none).
    Uses curl because Python's `requests`/`urllib` take 30-45s per call on this
    Windows box (SChannel OCSP issue); curl returns in <1s. Same workaround
    we use for YesCaptcha."""
    key = farmsync_api_key()
    if not key:
        return None, f"FarmSync key missing ({FARMSYNC_APIKEY_FILE})"
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "20",
             "-H", f"Authorization: Bearer {key}",
             f"{FARMSYNC_API_BASE}{path}"],
            capture_output=True, timeout=22, creationflags=_NO_WINDOW,
        )
    except Exception as e:
        return None, f"FarmSync curl failed: {str(e)[:80]}"
    if proc.returncode != 0:
        err = (proc.stderr.decode(errors="ignore") or "")[:160]
        return None, f"FarmSync curl exit {proc.returncode}: {err}"
    try:
        return json.loads(proc.stdout.decode("utf-8")), None
    except Exception:
        return None, "FarmSync returned non-JSON via curl"


def _farmsync_read_state_file(path):
    """Read automation's shared state file if it exists and is < FARMSYNC_STATE_MAX_AGE old.
    Returns the parsed JSON list (or None if missing/stale/unreadable)."""
    try:
        age = time.time() - os.path.getmtime(path)
        if age > FARMSYNC_STATE_MAX_AGE:
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def farmsync_get_state(force=False):
    """Return (devices, accounts, error). Cached for FARMSYNC_CACHE_TTL seconds.
    Prefers automation's shared state file (when < FARMSYNC_STATE_MAX_AGE old)
    so we don't double-scrape the cloud API while the FarmSync Automation
    subprocess is also running.

    `accounts` is always returned as [] from this path — no website route
    actually consumes per-account data, and /api/self/accounts/ is a 20 MB
    payload of Roblox cookies we don't want flowing through Python needlessly.
    If a future feature needs accounts, fetch them separately."""
    with _farmsync_cache_lock:
        fresh = (time.time() - _farmsync_cache["ts"]) < FARMSYNC_CACHE_TTL
        if not force and fresh and _farmsync_cache["devices"] is not None:
            return (_farmsync_cache["devices"],
                    _farmsync_cache["accounts"],
                    _farmsync_cache["error"])

    # Path 1: automation's shared devices state file (free, no API hit)
    if not force:
        devices = _farmsync_read_state_file(FARMSYNC_STATE_DEVICES)
        if devices is not None:
            with _farmsync_cache_lock:
                _farmsync_cache.update({
                    "devices": devices,
                    "accounts": [],
                    "ts": time.time(),
                    "error": None,
                })
            return devices, [], None

    # Path 2: direct cloud API for devices only (~50 KB; fast via curl)
    devices, dev_err = farmsync_fetch("/api/devices/")
    devices = devices if isinstance(devices, list) else []
    with _farmsync_cache_lock:
        _farmsync_cache.update({
            "devices": devices,
            "accounts": [],
            "ts": time.time(),
            "error": dev_err,
        })
    return devices, [], dev_err


# ─── Per-device current backup (Devices page) ────────────────────────
# FarmSync exposes NO reliable "currently-applied backup" field: backup_file_id
# stays stale after a restore, and the Backup task that records an apply rotates
# out of the (capped) task list once newer restart tasks pile on. So we maintain
# our OWN device->backup map, derived from observed successful Backup tasks via a
# per-device scan, ACCUMULATED and PERSISTED to disk so it survives restarts and
# never blanks a device we've already seen. The scan runs in the background; the
# endpoint reads the cached/persisted map instantly.
DEVICE_BACKUPS_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "_device_backups.json")
FARMSYNC_BACKUP_TTL = 120
_farmsync_backup_cache = {"ts": 0, "map": {}, "latest": {}, "loaded": False}
_farmsync_backup_lock = threading.Lock()
_farmsync_backup_refreshing = False


def _load_device_backups():
    try:
        with open(DEVICE_BACKUPS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            m = data.get("map", data)
            if isinstance(m, dict):
                return m
    except Exception:
        pass
    return {}


def _save_device_backups(m, latest):
    try:
        tmp = DEVICE_BACKUPS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"map": m, "latest": latest}, f)
        os.replace(tmp, DEVICE_BACKUPS_FILE)
    except Exception:
        pass


def _refresh_backup_map():
    """Background: scan EACH device's task history for its newest successful
    Backup; accumulate into the persisted map (never blank a device we've seen)."""
    global _farmsync_backup_refreshing
    try:
        id2name, latest_id, latest_name = {}, "", ""
        try:
            files, _ = farmsync_fetch("/api/s3/files?type=backup")
            items = (files or {}).get("items") if isinstance(files, dict) else None
            items = items or []
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            for it in items:
                if it.get("id"):
                    id2name[it["id"]] = it.get("original_name") or ""
            if items:
                latest_id = items[0].get("id", "")
                latest_name = items[0].get("original_name") or ""
        except Exception:
            pass

        devs, _ = farmsync_fetch("/api/devices/")
        devs = devs if isinstance(devs, list) else []
        with _farmsync_backup_lock:
            result = {k: dict(v) for k, v in (_farmsync_backup_cache.get("map") or {}).items()}

        for d in devs:
            did = d.get("id")
            if not did:
                continue
            try:
                tk, _ = farmsync_fetch("/api/devices/%s/tasks" % did)
                rows = (tk or {}).get("data") if isinstance(tk, dict) else None
                rows = rows or []
            except Exception:
                continue
            rows.sort(key=lambda t: t.get("created_at", ""), reverse=True)
            rec = result.setdefault(did, {"id": "", "name": "", "is_latest": False, "installing": False})
            rec["installing"] = False
            got = False
            for t in rows:
                td = t.get("task_data")
                try:
                    obj = json.loads(td) if isinstance(td, str) else (td or {})
                except Exception:
                    continue
                if obj.get("task_type") != "Backup":
                    continue
                if t.get("is_processing"):
                    rec["installing"] = True
                if t.get("is_success") and not got:
                    got = True
                    fid = (obj.get("payload") or {}).get("file_id", "")
                    resp = t.get("response") or ""
                    rec["id"] = fid
                    rec["name"] = id2name.get(fid) or (
                        resp.split("installed successfully:")[-1].strip() if "installed" in resp else "")
            # keep prior id/name if none observed this pass (accumulate); refresh is_latest
            rec["is_latest"] = bool(rec.get("id") and rec["id"] == latest_id)

        with _farmsync_backup_lock:
            _farmsync_backup_cache.update({
                "ts": time.time(), "map": result,
                "latest": {"id": latest_id, "name": latest_name},
            })
        _save_device_backups(result, {"id": latest_id, "name": latest_name})
    finally:
        with _farmsync_backup_lock:
            _farmsync_backup_refreshing = False


def _farmsync_backup_map(force=False):
    """Return the persisted device->backup map instantly; refresh per-device in
    the background when stale (a full-fleet scan takes ~20-40s)."""
    global _farmsync_backup_refreshing
    with _farmsync_backup_lock:
        if not _farmsync_backup_cache["loaded"]:
            _farmsync_backup_cache["map"] = _load_device_backups()
            _farmsync_backup_cache["loaded"] = True
        stale = (time.time() - _farmsync_backup_cache["ts"]) >= FARMSYNC_BACKUP_TTL
        if (force or stale) and not _farmsync_backup_refreshing:
            _farmsync_backup_refreshing = True
            threading.Thread(target=_refresh_backup_map, daemon=True).start()
        return _farmsync_backup_cache["map"]


# ─── FarmSync Automation subprocess control ───────────────────
# Spawns farmsync-automation/farmsync_automation/automation.py alongside the
# website. Pause = write _paused.flag (automation's run_cycle skips while present).
# Stop = terminate the subprocess. Auto-launched at Flask startup; killed on exit.

_farmsync_automation_proc = None
_farmsync_automation_lock = threading.Lock()


def _kill_orphan_automation_processes(exclude_pid=None):
    """Kill every running automation.py whose PID we are NOT tracking. Prevents
    duplicate webhooks when a previous Flask crashed/was force-killed and left
    its automation child running, then we spawn a new one alongside it."""
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/format:csv"],
            stderr=subprocess.DEVNULL, timeout=10,
            creationflags=_NO_WINDOW).decode(errors="ignore")
    except Exception:
        return 0
    killed = 0
    for line in out.splitlines():
        if "automation.py" not in line.lower():
            continue
        parts = line.rsplit(",", 1)
        if len(parts) != 2:
            continue
        pid = parts[1].strip()
        if not pid.isdigit():
            continue
        if exclude_pid is not None and int(pid) == int(exclude_pid):
            continue
        try:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, creationflags=_NO_WINDOW, timeout=5)
            killed += 1
        except Exception:
            pass
    return killed


def farmsync_automation_start():
    """Spawn the automation subprocess. Idempotent."""
    global _farmsync_automation_proc
    with _farmsync_automation_lock:
        if _farmsync_automation_proc and _farmsync_automation_proc.poll() is None:
            return False, "already running"
        if not os.path.exists(FARMSYNC_AUTOMATION_SCRIPT):
            return False, f"automation.py not found at {FARMSYNC_AUTOMATION_SCRIPT}"
        # Wipe any orphan automation.py left over from a previous Flask crash
        # (taskkill on Flask doesn't clean up its subprocess on Windows).
        orphans = _kill_orphan_automation_processes()
        if orphans:
            _auto_log(f"Killed {orphans} orphan automation process(es) before spawning")
            time.sleep(1)
        try:
            # Resume: clear pause flag before launching
            if os.path.exists(FARMSYNC_PAUSE_FLAG):
                try:
                    os.remove(FARMSYNC_PAUSE_FLAG)
                except Exception:
                    pass
            _farmsync_automation_proc = subprocess.Popen(
                [sys.executable, FARMSYNC_AUTOMATION_SCRIPT],
                cwd=FARMSYNC_AUTOMATION_DIR,
                creationflags=_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _auto_log(f"FarmSync Automation started (PID {_farmsync_automation_proc.pid})")
            return True, f"started PID {_farmsync_automation_proc.pid}"
        except Exception as e:
            return False, f"failed to spawn: {str(e)[:120]}"


def farmsync_automation_stop():
    """Terminate the automation subprocess. Also writes pause flag so any
    other instance (e.g. user's run.bat) sees the pause."""
    global _farmsync_automation_proc
    with _farmsync_automation_lock:
        # Set pause flag so any concurrent instance halts at next cycle
        try:
            with open(FARMSYNC_PAUSE_FLAG, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except Exception:
            pass
        if _farmsync_automation_proc and _farmsync_automation_proc.poll() is None:
            try:
                _farmsync_automation_proc.terminate()
                _farmsync_automation_proc.wait(timeout=5)
            except Exception:
                try:
                    _farmsync_automation_proc.kill()
                except Exception:
                    pass
            _auto_log(f"FarmSync Automation stopped")
            _farmsync_automation_proc = None
            return True, "stopped"
        return False, "not running"


def farmsync_automation_running():
    with _farmsync_automation_lock:
        return _farmsync_automation_proc is not None and _farmsync_automation_proc.poll() is None


def _farmsync_kill_on_exit():
    """Best-effort cleanup so the automation child doesn't outlive Flask."""
    try:
        farmsync_automation_stop()
    except Exception:
        pass


import atexit
atexit.register(_farmsync_kill_on_exit)


# Matches farmsync-automation/farmsync_automation/automation.py::HEARTBEAT_FRESH
# (config.json: heartbeat_fresh_min, default 10). Tool counts as "dead" beyond this.
FARMSYNC_HEARTBEAT_FRESH_SECS = 10 * 60


def _farmsync_heartbeat_age_secs(dev):
    """Seconds since last heartbeat. inf if device never reported one."""
    last_updated_ms = dev.get("last_updated") or 0
    if not last_updated_ms:
        return float("inf")
    try:
        return max(0.0, time.time() - (int(last_updated_ms) / 1000))
    except Exception:
        return float("inf")


def _farmsync_device_status(dev):
    """Status mirrors the automation's heartbeat check:
       - disabled : !is_enabled
       - offline  : heartbeat stale (tool dead, can't trust client_running)
       - offline  : client not running
       - online   : enabled, heartbeat fresh, client running."""
    if not dev.get("is_enabled"):
        return "disabled"
    if _farmsync_heartbeat_age_secs(dev) >= FARMSYNC_HEARTBEAT_FRESH_SECS:
        return "offline"   # tool dead per automation's HEARTBEAT_FRESH threshold
    if dev.get("client_running"):
        return "online"
    return "offline"


def _farmsync_uptime_pct(dev):
    total = dev.get("total_accounts") or 0
    active = dev.get("active_accounts") or 0
    if not total:
        return 0.0
    return round(active / total * 100, 1)


def _farmsync_tier(pct):
    if pct < 30:
        return "0-29"
    if pct < 50:
        return "30-49"
    if pct < 70:
        return "50-69"
    if pct < 90:
        return "70-89"
    return "90+"


def _farmsync_os_string(dev):
    """Best-effort OS string. FarmSync field naming varies; try common ones."""
    for k in ("sys_os", "sys_os_name", "sys_platform", "os_release", "platform", "os"):
        v = dev.get(k)
        if v:
            return str(v)
    return ""


def _natural_sort_key(s):
    """Sort 'Device 1' < 'Device 2' < 'Device 10' instead of alpha order."""
    return [int(t) if t.isdigit() else t.lower() for t in re.findall(r"\d+|\D+", s or "")]


@app.route("/api/farmsync/summary")
def api_farmsync_summary():
    devices, _, err = farmsync_get_state()
    total_devices = len(devices)
    total_accounts = sum((d.get("total_accounts") or 0) for d in devices)
    running_accounts = sum((d.get("active_accounts") or 0) for d in devices)
    uptime = round(running_accounts / total_accounts * 100, 1) if total_accounts else 0.0
    return jsonify({
        "total_devices": total_devices,
        "total_accounts": total_accounts,
        "running_accounts": running_accounts,
        "uptime_pct": uptime,
        "error": err,
    })


def _pct(used, total):
    try:
        return round(used / total * 100, 1) if total else 0.0
    except Exception:
        return 0.0


@app.route("/api/farmsync/devices")
def api_farmsync_devices():
    force = request.args.get("force") == "1"
    devices, _, err = farmsync_get_state(force=force)
    bmap = _farmsync_backup_map(force=force)
    out = []
    for d in devices:
        binfo = bmap.get(d.get("id", ""), {})
        pct = _farmsync_uptime_pct(d)
        # Memory (GB)
        ram_total = d.get("sys_ram_total_gb") or 0
        ram_free = d.get("sys_ram_free_gb") or 0
        ram_used = max(0, ram_total - ram_free)
        # Disk (GB)
        disk_total = d.get("sys_disk_total_gb") or 0
        disk_free = d.get("sys_disk_free_gb") or 0
        disk_used = max(0, disk_total - disk_free)
        # Heartbeat freshness (used by the status decision)
        hb_age = _farmsync_heartbeat_age_secs(d)
        hb_age_min = None if hb_age == float("inf") else round(hb_age / 60, 1)
        out.append({
            "id": d.get("id", ""),
            "device_note": (d.get("device_note") or "").strip(),
            "device_name": (d.get("device_name") or "").strip(),
            "group_name": (d.get("group_name") or "").strip(),
            "os": _farmsync_os_string(d),
            "status": _farmsync_device_status(d),
            "heartbeat_age_min": hb_age_min,
            "heartbeat_fresh": hb_age < FARMSYNC_HEARTBEAT_FRESH_SECS,
            "active_accounts": d.get("active_accounts") or 0,
            "total_accounts": d.get("total_accounts") or 0,
            "uptime_pct": pct,
            "tier": _farmsync_tier(pct),
            # System stats — exposed for the device card
            "ram_used_gb": round(ram_used, 1),
            "ram_total_gb": round(ram_total, 1),
            "ram_pct": _pct(ram_used, ram_total),
            "disk_used_gb": round(disk_used, 1),
            "disk_total_gb": round(disk_total, 1),
            "disk_pct": _pct(disk_used, disk_total),
            "cpu_name": (d.get("sys_cpu_name") or "").strip(),
            "cpu_cores_physical": d.get("sys_cpu_cores_physical") or 0,
            "cpu_cores_logical": d.get("sys_cpu_cores_logical") or 0,
            # Current applied backup (derived from latest successful Backup task)
            "backup_id": binfo.get("id", ""),
            "backup_name": binfo.get("name", ""),
            "backup_is_latest": binfo.get("is_latest", False),
            "backup_installing": binfo.get("installing", False),
        })
    out.sort(key=lambda x: _natural_sort_key(x["device_name"] or x["device_note"]))
    return jsonify({"devices": out, "count": len(out), "error": err,
                    "latest_backup": _farmsync_backup_cache.get("latest", {})})


# ═══════════════════════════════════════════════════════════════
#  YummyTrackStat — per-account in-game stats (local proxy API)
# ═══════════════════════════════════════════════════════════════
# Proxies https://yummytrackstat.com/api/<game>/{statistics,trackings} using a
# Bearer token saved at yummytrackstat/token.txt (gitignored). The token is the
# `access_token` from a logged-in browser session and CAN expire — if calls
# start returning 401, re-copy it into the file.
TRACKSTAT_BASE = "https://yummytrackstat.com"
TRACKSTAT_TOKEN_FILE = os.path.join(BASE_DIR, "yummytrackstat", "token.txt")
TRACKSTAT_DEFAULT_FILTER = "filter=device_sort%3Ddefault%26status%3Dall"
TRACKSTAT_CACHE_TTL = 30
_trackstat_cache = {}            # path -> (ts, json)


def _trackstat_token():
    try:
        with open(TRACKSTAT_TOKEN_FILE, encoding="utf-8-sig") as f:
            return f.readline().strip()
    except Exception:
        return None


def _trackstat_get(path, nocache=False):
    """GET a yummytrackstat API path with the Bearer token (curl, like FarmSync).
    Returns (json_or_none, error_or_none, http_status). 30s per-path cache."""
    tok = _trackstat_token()
    if not tok:
        return None, f"token missing ({TRACKSTAT_TOKEN_FILE})", 0
    now = time.time()
    if not nocache:
        hit = _trackstat_cache.get(path)
        if hit and now - hit[0] < TRACKSTAT_CACHE_TTL:
            return hit[1], None, 200
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "25", "-w", "\n__H__%{http_code}",
             "-H", f"Authorization: Bearer {tok}", "-H", "Accept: application/json",
             f"{TRACKSTAT_BASE}{path}"],
            capture_output=True, timeout=28, creationflags=_NO_WINDOW)
    except Exception as e:
        return None, f"curl failed: {str(e)[:80]}", 0
    out = proc.stdout.decode("utf-8", "replace")
    body, _, code = out.rpartition("__H__")
    code = (code or "").strip()
    if code != "200":
        if code == "401":
            return None, "token rejected (401) — refresh yummytrackstat/token.txt", 401
        return None, f"HTTP {code}", int(code) if code.isdigit() else 0
    try:
        data = json.loads(body)
    except Exception:
        return None, "non-JSON response", 200
    _trackstat_cache[path] = (now, data)
    return data, None, 200


# Health/status of the YummyTrackStat API — refreshed by a 20-min background
# loop and shown on the Settings page. Catches token expiry early.
_trackstat_status = {"ok": None, "state": "unknown", "message": "not checked yet", "checked_at": None}


def _trackstat_refresh_status():
    """Ping /auth/me with the token (no cache) and record connection status."""
    data, err, status = _trackstat_get("/auth/me", nocache=True)
    if err:
        if status == 401:
            _trackstat_status.update(ok=False, state="token_expired",
                                     message="Token expired (401) — refresh yummytrackstat/token.txt")
        elif "token missing" in (err or ""):
            _trackstat_status.update(ok=False, state="no_token",
                                     message="No token file (yummytrackstat/token.txt)")
        else:
            _trackstat_status.update(ok=False, state="error", message=err)
    else:
        d = data or {}
        bits = ["Connected"]
        who = d.get("global_name") or d.get("username")
        if who:
            bits.append(str(who))
        plan = d.get("plan") or ("premium" if d.get("premium") else "")
        if plan:
            bits.append(str(plan))
        _trackstat_status.update(ok=True, state="ok", message=" · ".join(bits))
    _trackstat_status["checked_at"] = datetime.now().isoformat()
    return dict(_trackstat_status)


@app.route("/api/trackstat/status")
def api_trackstat_status():
    """Cached YummyTrackStat connection status (refreshed every 20 min); ?force=1
    re-checks now. Static rule, so it wins over /api/trackstat/<game>."""
    if _trackstat_status["checked_at"] is None or request.args.get("force"):
        _trackstat_refresh_status()
    return jsonify(_trackstat_status)


@app.route("/api/trackstat/<game>")
def api_trackstat(game):
    """Local proxy for YummyTrackStat per-account stats.
      GET /api/trackstat/adopt-me            -> statistics + first page of accounts
      GET /api/trackstat/adopt-me?all=1      -> statistics + every account (paginated)
      GET /api/trackstat/adopt-me?page=2&limit=50
    Returns {game, statistics, total, count, accounts}."""
    game = (game or "").strip().lower()
    if not re.match(r"^[a-z0-9-]{2,40}$", game):
        return jsonify({"error": "invalid game slug"}), 400
    filt = TRACKSTAT_DEFAULT_FILTER
    stats, err, status = _trackstat_get(f"/api/{game}/statistics?{filt}")
    if err:
        return jsonify({"error": err}), (401 if status == 401 else 502)
    fetch_all = request.args.get("all") in ("1", "true", "yes")
    try:
        limit = max(1, min(200, int(request.args.get("limit", 100 if fetch_all else 50))))
    except ValueError:
        limit = 100 if fetch_all else 50
    try:
        start_page = 0 if fetch_all else max(0, int(request.args.get("page", 0) or 0))
    except ValueError:
        start_page = 0
    accounts, page, total = [], start_page, None
    while True:
        data, err, status = _trackstat_get(f"/api/{game}/trackings?{filt}&page={page}&limit={limit}")
        if err:
            return jsonify({"error": err, "statistics": stats}), (401 if status == 401 else 502)
        rows = data.get("rows", []) if isinstance(data, dict) else []
        total = data.get("count", len(rows)) if isinstance(data, dict) else len(rows)
        accounts.extend(rows)
        if not fetch_all or len(rows) < limit or len(accounts) >= (total or 0) or page - start_page > 300:
            break
        page += 1
    return jsonify({"game": game, "statistics": stats,
                    "total": total, "count": len(accounts), "accounts": accounts})


# ── Auto-discovering game catalog for /api/trackstat/all ──────────────
# CATALOG = every game YummyTrackStat offered when this was written. The set
# you actually track is auto-detected every 20 min (probe each for accounts>0),
# and the catalog itself is periodically re-scraped from their site so games
# they launch later get picked up too — all without editing this file.
TRACKSTAT_CATALOG = [
    "adopt-me", "pet-simulator-99", "pets-go", "grow-a-garden", "grow-a-garden-2",
    "murder-mystery-2", "blox-fruits", "king-legacy", "da-hood", "blade-ball",
    "sailor-piece", "fisch", "fish-it", "steal-a-brainrot", "plants-vs-brainrots",
    "slime-rng", "creatures-of-sonaria", "dragon-adventures", "grand-piece-online",
    "your-bizzare-adventure", "escape-tsunami-for-brainrots", "kick-a-lucky-block",
    "tapping-simulator", "the-forge", "99-nights-in-the-forest",
    "attack-on-titan-revolution", "bee-swarm-simulator", "bubble-gum-simulator-infinity",
]
TRACKSTAT_NONGAME = {
    "login", "logout", "signin", "signup", "auth", "callback", "dashboard",
    "partner", "alerts", "purchase", "premium", "checkout", "histories",
    "settings", "profile", "action-logs", "admin", "share-dashboard",
    "game-charts", "yummy-auto", "events",
}
_trackstat_extra_games = []                            # games discovered via catalog re-scrape
_trackstat_counts = {}                                 # game -> current {total_accounts, online, statistics}
_trackstat_active = {"checked_at": None}               # timestamp of the last probe
# Stable "all games you track" set: seeded with the games found at build time,
# then accumulated (any game ever seen with accounts>0) and persisted, so the
# list stays complete even when the farm rotates a game to 0 — or after restart.
TRACKSTAT_SEED = [
    "adopt-me", "bubble-gum-simulator-infinity", "murder-mystery-2",
    "99-nights-in-the-forest", "fish-it", "tapping-simulator", "bee-swarm-simulator",
    "plants-vs-brainrots", "fisch", "grand-piece-online", "the-forge",
]
TRACKSTAT_SEEN_FILE = os.path.join(BASE_DIR, "yummytrackstat", "seen_games.json")
_trackstat_seen = list(TRACKSTAT_SEED)


def _trackstat_load_seen():
    global _trackstat_seen
    try:
        with open(TRACKSTAT_SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            _trackstat_seen = list(dict.fromkeys(TRACKSTAT_SEED + data))
    except Exception:
        pass


def _trackstat_save_seen():
    try:
        os.makedirs(os.path.dirname(TRACKSTAT_SEEN_FILE), exist_ok=True)
        with open(TRACKSTAT_SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(_trackstat_seen, f)
    except Exception:
        pass


def _trackstat_curl_text(url):
    try:
        p = subprocess.run(["curl", "-sS", "--max-time", "20", url],
                           capture_output=True, timeout=23, creationflags=_NO_WINDOW)
        return p.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def _trackstat_all_games():
    return list(dict.fromkeys(TRACKSTAT_CATALOG + _trackstat_extra_games))


def _trackstat_refresh_catalog():
    """Re-scrape the YummyTrackStat bundle for game slugs we don't know yet,
    validate each via its statistics endpoint, and remember the real ones.
    Best-effort — silently keeps the static catalog if anything fails."""
    shell = _trackstat_curl_text(f"{TRACKSTAT_BASE}/")
    m = re.search(r'src="(/assets/index-[A-Za-z0-9_.-]+\.js)"', shell)
    if not m:
        return
    bundle = _trackstat_curl_text(f"{TRACKSTAT_BASE}{m.group(1)}")
    known = set(_trackstat_all_games()) | TRACKSTAT_NONGAME
    for c in sorted(set(re.findall(r'"/([a-z0-9][a-z0-9-]{4,40})"', bundle))):
        if c in known:
            continue
        stats, err, status = _trackstat_get(f"/api/{c}/statistics?{TRACKSTAT_DEFAULT_FILTER}")
        if status == 401:
            return
        if not err and isinstance(stats, dict) and "total_account" in stats:
            _trackstat_extra_games.append(c)


def _trackstat_refresh_active():
    """Probe every catalog game for its CURRENT count, snapshot it, and add any
    game seen with accounts>0 to the persistent tracked set."""
    counts, changed = {}, False
    for g in _trackstat_all_games():
        stats, err, status = _trackstat_get(f"/api/{g}/statistics?{TRACKSTAT_DEFAULT_FILTER}")
        if status == 401:
            break
        if err or not isinstance(stats, dict):
            continue
        n = stats.get("total_account") or 0
        counts[g] = {"game": g, "total_accounts": n,
                     "online": stats.get("total_account_online") or 0, "statistics": stats}
        if n > 0 and g not in _trackstat_seen:
            _trackstat_seen.append(g)
            changed = True
    if counts:
        _trackstat_counts.clear()
        _trackstat_counts.update(counts)
    if changed:
        _trackstat_save_seen()
    _trackstat_active["checked_at"] = datetime.now().isoformat()


def _trackstat_account_rows(game):
    """Every trackings row for one game, each tagged with _game."""
    out, page = [], 0
    while True:
        data, err, _ = _trackstat_get(f"/api/{game}/trackings?{TRACKSTAT_DEFAULT_FILTER}&page={page}&limit=200")
        if err:
            break
        rows = data.get("rows", []) if isinstance(data, dict) else []
        for r in rows:
            r["_game"] = game
        out.extend(rows)
        tot = data.get("count", len(rows)) if isinstance(data, dict) else len(rows)
        if len(rows) < 200 or len(out) >= tot or page > 200:
            break
        page += 1
    return out


def _trackstat_merge(game_list, include_accounts):
    games, total_accounts, total_online = [], 0, 0
    for g in game_list:
        c = _trackstat_counts.get(g) or {"total_accounts": 0, "online": 0, "statistics": None}
        entry = {"game": g, "total_accounts": c["total_accounts"],
                 "online": c["online"], "statistics": c["statistics"]}
        if include_accounts and c["total_accounts"] > 0:
            accts = _trackstat_account_rows(g)
            entry["accounts"], entry["count"] = accts, len(accts)
        games.append(entry)
        total_accounts += c["total_accounts"]
        total_online += c["online"]
    games.sort(key=lambda x: -x["total_accounts"])
    return {"games": games, "game_count": len(games),
            "total_accounts": total_accounts, "total_online": total_online,
            "checked_at": _trackstat_active["checked_at"]}


@app.route("/api/trackstat/all")
def api_trackstat_all():
    """STABLE merge — every game you've ever tracked, each with its CURRENT count
    (0 when the farm is rotated off it). The set auto-grows + persists, so it
    stays complete across the farm's rotation and restarts.
      ?accounts=1 -> also every account per game (slow)   ?refresh=1 -> re-probe now"""
    if request.args.get("refresh") or _trackstat_active["checked_at"] is None:
        _trackstat_refresh_active()
    out = _trackstat_merge(_trackstat_seen, request.args.get("accounts") in ("1", "true", "yes"))
    out["view"] = "all-tracked"
    return jsonify(out)


@app.route("/api/trackstat/active")
def api_trackstat_active():
    """LIVE merge — only games with accounts being tracked RIGHT NOW, so the list
    and totals move with the farm's current rotation. Same options as /all."""
    if request.args.get("refresh") or _trackstat_active["checked_at"] is None:
        _trackstat_refresh_active()
    active = [g for g, c in _trackstat_counts.items() if c["total_accounts"] > 0]
    out = _trackstat_merge(active, request.args.get("accounts") in ("1", "true", "yes"))
    out["view"] = "active-now"
    return jsonify(out)


@app.route("/api/farmsync/devices/<device_id>/restart-vps", methods=["POST"])
def api_farmsync_restart_vps(device_id):
    """Send a 'Restart VPS' task to FarmSync for a single device.
    Mirrors farmsync-automation/farmsync_automation/automation.py::create_task()."""
    key = farmsync_api_key()
    if not key:
        return jsonify({"error": "FarmSync key missing"}), 400
    try:
        body = {
            "device_id": device_id,
            "task_data": json.dumps({"task_type": "Restart VPS", "payload": {}}),
        }
        r = http_requests.post(
            f"{FARMSYNC_API_BASE}/api/tasks/",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=15,
        )
        if r.status_code >= 400:
            return jsonify({"error": f"FarmSync HTTP {r.status_code}: {r.text[:200]}"}), r.status_code
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:200]}
        _auto_log(f"Restart VPS sent for device {device_id}")
        return jsonify({"ok": True, "response": data})
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


# ─── Per-group backup assignment (Devices page → automation enforcement) ──────
# {group_name: backup_id}. The automation reads this each cycle and force-applies
# the group's backup to any device in that group on a different one, plus
# re-applies after a Restart VPS. Off until a group is assigned.
GROUP_BACKUPS_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "_group_backups.json")


def _load_group_backups():
    try:
        with open(GROUP_BACKUPS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_group_backups(d):
    try:
        tmp = GROUP_BACKUPS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, GROUP_BACKUPS_FILE)
    except Exception:
        pass


@app.route("/api/farmsync/group-backups")
def api_get_group_backups():
    """Per-group backup assignments + available storage backups + group list."""
    assignments = _load_group_backups()
    backups = []
    try:
        files, _ = farmsync_fetch("/api/s3/files?type=backup")
        items = (files or {}).get("items") if isinstance(files, dict) else None
        for it in (items or []):
            if it.get("id"):
                backups.append({
                    "id": it.get("id"),
                    "name": it.get("original_name") or "",
                    "size": it.get("size") or 0,
                    "created_at": it.get("created_at") or "",
                })
        backups.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    except Exception:
        pass
    devices, _, _ = farmsync_get_state()
    counts = {}
    for d in devices:
        g = (d.get("group_name") or "").strip()
        if g:
            counts[g] = counts.get(g, 0) + 1
    groups = [{"name": g, "device_count": counts[g], "backup_id": assignments.get(g, "")}
              for g in sorted(counts)]
    return jsonify({"groups": groups, "backups": backups, "assignments": assignments})


@app.route("/api/farmsync/group-backups", methods=["POST"])
def api_set_group_backups():
    """Assign a backup to a group (backup_id='' clears it)."""
    data = request.get_json(silent=True) or {}
    group = (data.get("group") or "").strip()
    backup_id = (data.get("backup_id") or "").strip()
    if not group:
        return jsonify({"error": "group required"}), 400
    assignments = _load_group_backups()
    if backup_id:
        assignments[group] = backup_id
    else:
        assignments.pop(group, None)
    _save_group_backups(assignments)
    _auto_log("Group backup %s: %s%s" % (
        "set" if backup_id else "cleared", group,
        (" -> " + backup_id[:12]) if backup_id else ""))
    return jsonify({"ok": True, "group": group, "backup_id": backup_id, "assignments": assignments})


@app.route("/api/farmsync/automation/status")
def api_farmsync_automation_status():
    return jsonify({
        "running": farmsync_automation_running(),
        "paused": os.path.exists(FARMSYNC_PAUSE_FLAG),
        "script": FARMSYNC_AUTOMATION_SCRIPT,
        "script_exists": os.path.exists(FARMSYNC_AUTOMATION_SCRIPT),
    })


@app.route("/api/farmsync/automation/start", methods=["POST"])
def api_farmsync_automation_start():
    ok, msg = farmsync_automation_start()
    return jsonify({
        "ok": ok,
        "message": msg,
        "running": farmsync_automation_running(),
    })


@app.route("/api/farmsync/automation/stop", methods=["POST"])
def api_farmsync_automation_stop():
    ok, msg = farmsync_automation_stop()
    return jsonify({
        "ok": ok,
        "message": msg,
        "running": farmsync_automation_running(),
    })


@app.route("/api/accounts")
def api_accounts():
    """Per-account cumulative farm time, written each cycle by the FarmSync
    automation (_account_livetime.json). Returns username, the historical set of
    devices/groups the account has farmed on, and live_seconds — sorted by
    live_seconds desc. Only safe fields are exposed (no cookies/passwords)."""
    path = os.path.join(FARMSYNC_AUTOMATION_DIR, "_account_livetime.json")
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    accounts = []
    if isinstance(data, dict):
        for uname, rec in data.items():
            if not isinstance(rec, dict):
                continue
            accounts.append({
                "username": uname,
                "live_seconds": rec.get("live_seconds", 0) or 0,
                "devices": rec.get("devices", []) or [],
                "groups": rec.get("groups", []) or [],
                "last_update": rec.get("last_update"),
            })
    accounts.sort(key=lambda a: a["live_seconds"], reverse=True)
    total_seconds = sum(a["live_seconds"] for a in accounts)
    return jsonify({"accounts": accounts, "count": len(accounts), "total_seconds": total_seconds})


# ─── Automation logs (tail logs.txt, LAN-accessible) ─────────────
# Flask already binds 0.0.0.0 so anyone on the LAN can hit this. The
# log file is updated by the automation in real-time, and these routes
# read the file fresh on every request — no caching, always current.
#
# /api/farmsync/logs           — tail or full text, optional grep filter
# /api/farmsync/logs/download  — raw file, served as attachment for curl -O
# /api/farmsync/logs/cycle     — just the latest cycle block (most useful
#                                per-cycle diagnostic)
#
# Query params for /api/farmsync/logs:
#   tail   - last N lines (default 500, max 10000).
#            Pass tail=0 or tail=all for the WHOLE file (no cap).
#   grep   - case-insensitive substring filter
#   format - "text" (default) or "json"
FARMSYNC_LOG_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "logs.txt")


def _read_tail_lines(path, n):
    """Read up to N trailing lines from a text file without loading the whole
    file into memory. Reads from the end in 64 KB chunks. Pass n=0 / negative
    to read the entire file."""
    if not os.path.exists(path):
        return []
    try:
        if n <= 0:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            block = 65536
            data = b""
            while file_size > 0 and data.count(b"\n") <= n:
                read_size = min(block, file_size)
                file_size -= read_size
                f.seek(file_size)
                data = f.read(read_size) + data
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-n:]
    except Exception:
        return []


def _parse_tail_arg(raw):
    """tail=N → int, tail=0 / all / full → 0 (no cap)."""
    if raw is None:
        return 500
    s = str(raw).strip().lower()
    if s in ("all", "full", "0", "-1", ""):
        return 0
    try:
        n = int(s)
    except ValueError:
        return 500
    if n <= 0:
        return 0
    return min(n, 10000)


@app.route("/api/farmsync/logs")
def api_farmsync_logs():
    tail = _parse_tail_arg(request.args.get("tail"))
    grep = (request.args.get("grep") or "").lower()
    fmt = (request.args.get("format") or "text").lower()

    lines = _read_tail_lines(FARMSYNC_LOG_FILE, tail)
    if grep:
        lines = [ln for ln in lines if grep in ln.lower()]

    if fmt == "json":
        try:
            mtime = os.path.getmtime(FARMSYNC_LOG_FILE) if os.path.exists(FARMSYNC_LOG_FILE) else None
            size = os.path.getsize(FARMSYNC_LOG_FILE) if os.path.exists(FARMSYNC_LOG_FILE) else 0
        except Exception:
            mtime, size = None, 0
        return jsonify({
            "path": FARMSYNC_LOG_FILE,
            "exists": os.path.exists(FARMSYNC_LOG_FILE),
            "size_bytes": size,
            "modified_ts": mtime,
            "tail": tail or "all",
            "grep": grep,
            "count": len(lines),
            "lines": lines,
        })
    if not lines and not os.path.exists(FARMSYNC_LOG_FILE):
        return Response(f"log file not found at {FARMSYNC_LOG_FILE}\n",
                        status=404, mimetype="text/plain; charset=utf-8")
    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/api/farmsync/logs/download")
def api_farmsync_logs_download():
    """Raw file download (no tail, no grep). Streams logs.txt with a
    Content-Disposition: attachment header so `curl -O` saves it as
    logs.txt. Use this for full diagnostics each cycle."""
    if not os.path.exists(FARMSYNC_LOG_FILE):
        return Response(f"log file not found at {FARMSYNC_LOG_FILE}\n",
                        status=404, mimetype="text/plain; charset=utf-8")
    def _stream():
        with open(FARMSYNC_LOG_FILE, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    headers = {
        "Content-Disposition": 'attachment; filename="logs.txt"',
        "Content-Length": str(os.path.getsize(FARMSYNC_LOG_FILE)),
        "Cache-Control": "no-store",
    }
    return Response(_stream(), mimetype="text/plain; charset=utf-8", headers=headers)


@app.route("/logs")
def page_farmsync_logs():
    """Browser-friendly log viewer. Self-contained HTML — no external deps so
    it works from any device on the LAN without loading static files."""
    return Response(_LOGS_VIEWER_HTML, mimetype="text/html; charset=utf-8")


_LOGS_VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FarmSync Automation Log</title>
<style>
  :root {
    --bg: #0f1117; --panel: #161922; --border: #262a36;
    --text: #d8dbe6; --muted: #7a8092; --accent: #5bc0eb;
    --green: #4caf50; --red: #ef5350; --yellow: #f5b800;
  }
  * { box-sizing: border-box; }
  html,body { height: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 13px;
  }
  header {
    background: var(--panel); border-bottom: 1px solid var(--border);
    padding: 10px 16px; display: flex; flex-wrap: wrap; gap: 10px;
    align-items: center; position: sticky; top: 0; z-index: 10;
  }
  header h1 { margin: 0; font-size: 14px; font-weight: 600; }
  header h1 i { color: var(--accent); margin-right: 6px; }
  header .meta { color: var(--muted); font-size: 11px; }
  .toolbar { display: flex; gap: 6px; align-items: center; margin-left: auto; flex-wrap: wrap; }
  .toolbar select, .toolbar input, .toolbar button, .toolbar label {
    background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 10px; font-size: 12px;
    font-family: inherit;
  }
  .toolbar button { cursor: pointer; }
  .toolbar button:hover { border-color: var(--accent); }
  .toolbar input[type=text] { width: 200px; }
  .toolbar label { display: inline-flex; align-items: center; gap: 5px;
                   background: transparent; border-color: transparent; padding: 5px 4px; }
  .toolbar label input[type=checkbox] { margin: 0; }
  .status-dot { display: inline-block; width: 8px; height: 8px;
                border-radius: 50%; background: var(--muted); margin-right: 4px; }
  .status-dot.live { background: var(--green); animation: pulse 2s infinite; }
  @keyframes pulse {
    0%,100% { opacity: 1; } 50% { opacity: 0.4; }
  }
  main {
    padding: 12px 16px;
  }
  pre#log {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
    font-family: "Cascadia Code", "Consolas", "Menlo", monospace;
    font-size: 12px; line-height: 1.45;
    overflow-x: auto; white-space: pre;
    margin: 0; min-height: 70vh;
  }
  pre#log .line { display: block; }
  pre#log .line.hl-action { color: #cdd6f4; }
  pre#log .line.hl-failed { color: var(--red); }
  pre#log .line.hl-skipped { color: var(--yellow); }
  pre#log .line.hl-action-tag { color: var(--accent); }
  pre#log .line.hl-banner { color: var(--accent); font-weight: 600; }
  pre#log .line.hl-low { color: var(--yellow); }
  pre#log .line.hl-ramhigh { color: var(--red); }
  pre#log .line.hl-recovered { color: var(--green); }
  pre#log mark { background: #f5b80055; color: inherit; padding: 0 2px; border-radius: 2px; }
  .empty { color: var(--muted); padding: 30px; text-align: center; }
</style>
</head>
<body>
<header>
  <h1>FarmSync Automation Log</h1>
  <span class="meta" id="meta"><span class="status-dot live" id="livedot"></span>loading…</span>
  <div class="toolbar">
    <select id="mode" title="View mode">
      <option value="cycle">Latest cycle</option>
      <option value="500" selected>Last 500 lines</option>
      <option value="2000">Last 2000 lines</option>
      <option value="all">Full file</option>
    </select>
    <input type="text" id="grep" placeholder="filter (e.g. FAILED, Hoang17)">
    <label><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
    <label><input type="checkbox" id="autorefresh" checked> auto-refresh 15s</label>
    <button id="refresh">Refresh</button>
    <button id="download">Download</button>
  </div>
</header>
<main>
  <pre id="log"><span class="empty">Loading…</span></pre>
</main>
<script>
const LOG_EL = document.getElementById('log');
const META = document.getElementById('meta');
const LIVE_DOT = document.getElementById('livedot');
const MODE = document.getElementById('mode');
const GREP = document.getElementById('grep');
const AUTO_SCROLL = document.getElementById('autoscroll');
const AUTO_REFRESH = document.getElementById('autorefresh');
let timer = null;

function fmtBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
  return (n/1024/1024).toFixed(2) + ' MB';
}
function escHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function classifyLine(raw) {
  // Order matters — first match wins.
  if (raw.includes('FarmSync Automation | Cycle #')) return 'hl-banner';
  if (raw.includes('| FAILED')) return 'hl-failed';
  if (raw.includes('| SKIPPED')) return 'hl-skipped';
  if (raw.includes('| RELOGIN ALL') || raw.includes('| RESTART TOOL') || raw.includes('| MOVE ACCOUNTS')) return 'hl-action-tag';
  if (raw.includes('| RECOVERED')) return 'hl-recovered';
  if (raw.includes('RAM HIGH!')) return 'hl-ramhigh';
  if (/LOW x\\d+/.test(raw)) return 'hl-low';
  if (raw.startsWith('[') && raw.includes('ACTION')) return 'hl-action';
  return '';
}
function highlightGrep(html, needle) {
  if (!needle) return html;
  const re = new RegExp(needle.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'), 'gi');
  return html.replace(re, m => '<mark>' + m + '</mark>');
}
function renderLines(lines, grep) {
  if (!lines.length) {
    LOG_EL.innerHTML = '<span class="empty">No log lines</span>';
    return;
  }
  const out = lines.map(ln => {
    const cls = classifyLine(ln);
    let html = escHtml(ln);
    html = highlightGrep(html, grep);
    return '<span class="line' + (cls ? ' ' + cls : '') + '">' + html + '</span>';
  }).join('');
  LOG_EL.innerHTML = out;
  if (AUTO_SCROLL.checked) window.scrollTo(0, document.body.scrollHeight);
}
async function refresh() {
  const mode = MODE.value;
  const grep = GREP.value.trim();
  let url;
  if (mode === 'cycle') url = '/api/farmsync/logs/cycle';
  else if (mode === 'all') url = '/api/farmsync/logs?tail=all&format=json';
  else url = '/api/farmsync/logs?tail=' + encodeURIComponent(mode) + '&format=json';
  if (grep && mode !== 'cycle') url += (url.includes('?') ? '&' : '?') + 'grep=' + encodeURIComponent(grep);
  LIVE_DOT.classList.remove('live');
  try {
    const r = await fetch(url, { cache: 'no-store' });
    if (mode === 'cycle') {
      const txt = await r.text();
      const lines = txt.split('\\n');
      const filtered = grep ? lines.filter(l => l.toLowerCase().includes(grep.toLowerCase())) : lines;
      renderLines(filtered, grep);
      META.innerHTML = '<span class="status-dot live"></span>latest cycle • ' + new Date().toLocaleTimeString();
    } else {
      const j = await r.json();
      renderLines(j.lines || [], grep);
      META.innerHTML = '<span class="status-dot live"></span>'
        + j.count + ' lines'
        + (j.size_bytes ? ' • file ' + fmtBytes(j.size_bytes) : '')
        + ' • updated ' + new Date().toLocaleTimeString();
    }
    LIVE_DOT.classList.add('live');
  } catch (e) {
    META.textContent = 'fetch failed: ' + e.message;
  }
}
function scheduleAutorefresh() {
  if (timer) { clearInterval(timer); timer = null; }
  if (AUTO_REFRESH.checked) timer = setInterval(refresh, 15000);
}
document.getElementById('refresh').addEventListener('click', refresh);
document.getElementById('download').addEventListener('click', () => {
  window.location.href = '/api/farmsync/logs/download';
});
MODE.addEventListener('change', refresh);
GREP.addEventListener('input', () => {
  // small debounce
  clearTimeout(window._grepT); window._grepT = setTimeout(refresh, 250);
});
AUTO_REFRESH.addEventListener('change', scheduleAutorefresh);
refresh();
scheduleAutorefresh();
</script>
</body>
</html>
"""


@app.route("/api/farmsync/logs/cycle")
def api_farmsync_logs_cycle():
    """Return just the latest cycle block from logs.txt. The most useful
    per-cycle diagnostic — small payload, always reflects what the
    automation did in its last run."""
    if not os.path.exists(FARMSYNC_LOG_FILE):
        return Response(f"log file not found at {FARMSYNC_LOG_FILE}\n",
                        status=404, mimetype="text/plain; charset=utf-8")
    # Walk back from the end until we find the start of the most-recent
    # cycle banner (the "  FarmSync Automation | Cycle #" line). We grab a
    # generous 200-line tail and locate the last banner inside it.
    tail = _read_tail_lines(FARMSYNC_LOG_FILE, 200)
    start = None
    for i in range(len(tail) - 1, -1, -1):
        if "FarmSync Automation | Cycle #" in tail[i]:
            # The banner is preceded by a "===" separator line; include it.
            start = max(0, i - 1)
            break
    block = tail[start:] if start is not None else tail
    body = "\n".join(block) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


# ─── Chrome debug log (forensic trace — for diagnosing session deaths) ───
@app.route("/api/chrome/debug")
def api_chrome_debug():
    """Tail the Chrome debug log. Query params:
        tail=N        last N lines (default 500, max 20000)
        tail=all      no cap
        grep=KW       case-insensitive substring filter
        format=text|json (default text)"""
    tail = _parse_tail_arg(request.args.get("tail"))
    grep = (request.args.get("grep") or "").lower()
    fmt  = (request.args.get("format") or "text").lower()
    lines = _read_tail_lines(CHROME_DEBUG_FILE, tail)
    if grep:
        lines = [l for l in lines if grep in l.lower()]
    if fmt == "json":
        try:
            mtime = os.path.getmtime(CHROME_DEBUG_FILE) if os.path.exists(CHROME_DEBUG_FILE) else None
            size  = os.path.getsize(CHROME_DEBUG_FILE) if os.path.exists(CHROME_DEBUG_FILE) else 0
        except Exception:
            mtime, size = None, 0
        return jsonify({
            "path": CHROME_DEBUG_FILE,
            "exists": os.path.exists(CHROME_DEBUG_FILE),
            "size_bytes": size,
            "modified_ts": mtime,
            "tail": tail or "all",
            "grep": grep,
            "count": len(lines),
            "lines": lines,
        })
    if not lines and not os.path.exists(CHROME_DEBUG_FILE):
        return Response(f"chrome debug log not yet written at {CHROME_DEBUG_FILE}\n",
                        status=404, mimetype="text/plain; charset=utf-8")
    body = "\n".join(lines) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/api/chrome/debug/download")
def api_chrome_debug_download():
    """Raw download of the full Chrome debug log."""
    if not os.path.exists(CHROME_DEBUG_FILE):
        return Response("debug log not yet written\n", status=404,
                        mimetype="text/plain; charset=utf-8")
    def _stream():
        with open(CHROME_DEBUG_FILE, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk: break
                yield chunk
    headers = {
        "Content-Disposition": 'attachment; filename="chrome_debug.log"',
        "Content-Length": str(os.path.getsize(CHROME_DEBUG_FILE)),
        "Cache-Control": "no-store",
    }
    return Response(_stream(), mimetype="text/plain; charset=utf-8", headers=headers)


# ─── Chrome freeze (independent of Pause; for login.bat) ────────
@app.route("/api/chrome/freeze/status")
def api_chrome_freeze_status():
    return jsonify({"frozen": _is_chrome_frozen()})


@app.route("/api/chrome/freeze/toggle", methods=["POST"])
def api_chrome_freeze_toggle():
    global _chrome_driver
    if _is_chrome_frozen():
        # Unfreeze
        try:
            os.remove(CHROME_FREEZE_FLAG)
        except Exception:
            pass
        _auto_log("Chrome activity resumed")
        return jsonify({"ok": True, "frozen": False, "message": "Chrome activity resumed"})
    # Freeze: write flag, then release our current driver (if any) so the user
    # can immediately start login.bat without the existing session squatting
    # on Profile 3.
    try:
        with open(CHROME_FREEZE_FLAG, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception as e:
        return jsonify({"ok": False, "frozen": False, "message": str(e)[:120]}), 500
    # Try to release the existing driver immediately so Profile 3 is free for
    # login.bat. If a sync is mid-flight (lock held), skip — the flag is set,
    # so once it finishes nothing new will spawn.
    if _chrome_lock.acquire(blocking=False):
        try:
            if _chrome_driver is not None:
                try:
                    _chrome_driver.quit()
                except Exception:
                    pass
                _chrome_driver = None
        finally:
            _chrome_lock.release()
    _auto_log("Chrome activity frozen — login.bat is now safe to run")
    return jsonify({"ok": True, "frozen": True, "message": "Chrome activity frozen"})


# ═══════════════════════════════════════════════════════════════
#  YesCaptcha — balance lookup (replaces "All Time" tile)
# ═══════════════════════════════════════════════════════════════
#  Free endpoint per docs (no points cost). Cached for YESCAPTCHA_CACHE_TTL
#  seconds to avoid hitting api.yescaptcha.com on every dashboard refresh.

_yescaptcha_cache = {"payload": None, "ts": 0.0, "error": None}
_yescaptcha_cache_lock = threading.Lock()


def yescaptcha_api_key():
    if not os.path.exists(YESCAPTCHA_APIKEY_FILE):
        return None
    try:
        with open(YESCAPTCHA_APIKEY_FILE, "r", encoding="utf-8") as f:
            key = f.readline().strip()
        return key or None
    except Exception:
        return None


@app.route("/api/yescaptcha/balance")
def api_yescaptcha_balance():
    """Mirror of YesCaptcha's POST /getBalance. Returns:
      {balance, soft_balance, invite_balance, usd, cached_at, error}"""
    force = request.args.get("force") == "1"
    with _yescaptcha_cache_lock:
        fresh = (time.time() - _yescaptcha_cache["ts"]) < YESCAPTCHA_CACHE_TTL
        if not force and fresh and _yescaptcha_cache["payload"]:
            p = dict(_yescaptcha_cache["payload"])
            p["cached_at"] = _yescaptcha_cache["ts"]
            return jsonify(p)

    key = yescaptcha_api_key()
    if not key:
        return jsonify({"error": f"YesCaptcha key missing ({YESCAPTCHA_APIKEY_FILE})"}), 400
    # Shell out to curl. Python's `requests` AND `urllib` both take 30-64s per
    # call to api.yescaptcha.com on this Windows box (probably OCSP/SChannel
    # revocation check). Direct curl returns in <1s, so we delegate to it.
    body = json.dumps({"clientKey": key})
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "12", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", body,
             f"{YESCAPTCHA_API_BASE}/getBalance"],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW,
        )
    except Exception as e:
        return jsonify({"error": f"YesCaptcha curl failed: {str(e)[:120]}"}), 502
    if proc.returncode != 0:
        err = (proc.stderr.decode(errors="ignore") or "")[:160]
        return jsonify({"error": f"YesCaptcha curl exit {proc.returncode}: {err}"}), 502
    try:
        data = json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        return jsonify({"error": "YesCaptcha returned non-JSON via curl"}), 502

    if data.get("errorId") != 0:
        err_msg = data.get("errorDescription") or data.get("errorCode") or "unknown error"
        return jsonify({
            "error": err_msg,
            "errorCode": data.get("errorCode"),
            "errorId": data.get("errorId"),
        }), 502

    balance = int(data.get("balance") or 0)
    payload = {
        "balance": balance,
        "soft_balance": int(data.get("softBalance") or 0),
        "invite_balance": int(data.get("inviteBalance") or 0),
        "usd": round(balance * YESCAPTCHA_USD_PER_POINT, 2),
    }
    with _yescaptcha_cache_lock:
        _yescaptcha_cache.update({"payload": payload, "ts": time.time(), "error": None})
    payload["cached_at"] = _yescaptcha_cache["ts"]
    return jsonify(payload)


# ═══════════════════════════════════════════════════════════════
#  ZP ZeroSolver — auto-submit CAPTCHA-locked farm accounts
# ═══════════════════════════════════════════════════════════════
_zp_lock = threading.RLock()
_zp_cycle_lock = threading.Lock()          # serialize cycles (no overlapping submits)
# Ledger persisted to ZP_SOLVER_STATE_FILE. usernames live only here (never
# logged); cookies are never stored — they go straight to ZeroSolver and nowhere
# else. jobs[] holds one record per /submit: the usernames we sent + the live
# job counts we poll from /status.
_zp_ledger = {"jobs": [], "last_cycle_ts": 0, "last_submit_ts": 0,
              "last_error": None, "sent_total": 0}
_zp_credits_cache = {"data": None, "ts": 0.0, "error": None}
_zp_active_cache = {"jobs": None, "ts": 0.0}    # live /active jobs, for an accurate tile
_zp_loaded = False

_ZP_TERMINAL = ("completed", "failed", "cancelled")


def _zp_key():
    """First non-empty line of the ZeroSolver API key file, or None."""
    try:
        with open(ZP_SOLVER_KEY_FILE, "r", encoding="utf-8-sig") as f:
            for line in f:
                k = line.strip()
                if k:
                    return k
    except Exception:
        pass
    return None


def _zp_solver_paused():
    return os.path.exists(ZP_SOLVER_PAUSE_FLAG)


def _zp_load_state():
    """Restore the job ledger from disk (best effort)."""
    global _zp_ledger, _zp_loaded
    try:
        with open(ZP_SOLVER_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            with _zp_lock:
                _zp_ledger = {
                    "jobs": data.get("jobs", []),
                    "last_cycle_ts": data.get("last_cycle_ts", 0),
                    "last_submit_ts": data.get("last_submit_ts", 0),
                    "last_error": data.get("last_error"),
                    "sent_total": data.get("sent_total", 0),
                }
    except Exception:
        pass
    _zp_loaded = True


def _zp_save_state():
    try:
        with _zp_lock:
            blob = json.dumps(_zp_ledger)
        tmp = ZP_SOLVER_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, ZP_SOLVER_STATE_FILE)
    except Exception:
        pass


def _zp_set_last(err):
    with _zp_lock:
        _zp_ledger["last_error"] = err
        _zp_ledger["last_cycle_ts"] = time.time()
    _zp_save_state()


def _zp_curl(method, path, body=None, timeout=30):
    """Call ZeroSolver via curl (SChannel workaround, same as FarmSync/YesCaptcha).
    The request body is fed via STDIN (`--data-binary @-`) — a /submit batch can be
    ~1 MB, far over Windows' 32 KB command-line limit, so it can't be a `-d` arg.
    Returns (json_or_none, error_or_none, http_status)."""
    key = _zp_key()
    if not key:
        return None, f"ZP key missing ({ZP_SOLVER_KEY_FILE})", 0
    args = ["curl", "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}",
            "-H", f"X-API-Key: {key}", "-X", method]
    input_bytes = None
    if body is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", "@-"]
        input_bytes = json.dumps(body).encode("utf-8")
    args.append(f"{ZP_SOLVER_BASE}{path}")
    try:
        proc = subprocess.run(args, input=input_bytes, capture_output=True,
                              timeout=timeout + 8, creationflags=_NO_WINDOW)
    except Exception as e:
        return None, f"ZP curl failed: {str(e)[:120]}", 0
    if proc.returncode != 0:
        err = (proc.stderr.decode(errors="ignore") or "")[:160]
        return None, f"ZP curl exit {proc.returncode}: {err}", 0
    raw = proc.stdout.decode("utf-8", errors="ignore")
    nl = raw.rfind("\n")                      # http code is on the final line (from -w)
    code_str = raw[nl + 1:].strip() if nl >= 0 else ""
    body_str = raw[:nl] if nl >= 0 else raw
    try:
        status = int(code_str)
    except Exception:
        status = 0
    try:
        data = json.loads(body_str) if body_str.strip() else {}
    except Exception:
        return None, f"ZP non-JSON (HTTP {status}): {body_str[:120]}", status
    return data, None, status


def _zp_get_credits(force=False):
    """Cached GET /credits → (dict_or_none, error_or_none). 1 credit ≈ $1."""
    with _zp_lock:
        fresh = (time.time() - _zp_credits_cache["ts"]) < ZP_SOLVER_CREDITS_TTL
        if not force and fresh and _zp_credits_cache["data"]:
            return dict(_zp_credits_cache["data"]), _zp_credits_cache["error"]
    data, err, status = _zp_curl("GET", "/credits", timeout=15)
    if err or not isinstance(data, dict) or status != 200:
        msg = err or (data.get("error") if isinstance(data, dict) else None) or f"HTTP {status}"
        with _zp_lock:
            _zp_credits_cache["ts"] = time.time()
            _zp_credits_cache["error"] = msg
            prev = dict(_zp_credits_cache["data"]) if _zp_credits_cache["data"] else None
        return prev, msg
    with _zp_lock:
        _zp_credits_cache.update({"data": data, "ts": time.time(), "error": None})
    return dict(data), None


def _zp_load_accounts():
    """Raw FarmSync account list. Prefer the automation's local _state_accounts.json
    when fresh (the automation refetches it each cycle); else live-fetch. Returns
    (list, error)."""
    try:
        age = time.time() - os.path.getmtime(FARMSYNC_STATE_ACCOUNTS)
        if age < ZP_SOLVER_ACCOUNTS_MAX_AGE:
            with open(FARMSYNC_STATE_ACCOUNTS, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data, None
    except Exception:
        pass
    data, err = farmsync_fetch("/api/self/accounts/")
    if isinstance(data, list):
        return data, None
    return [], (err or "no accounts")


def _zp_captcha_lines(accounts):
    """{username: "user:pass:cookie"} for accounts that are CAPTCHA-errored,
    assigned to a device, with a live `_|WARNING` cookie. ZeroSolver detects the
    cookie by its `_|WARNING` marker, so a missing password is harmless."""
    out = {}
    for a in accounts:
        if (a.get("error") or "").strip().upper() != "CAPTCHA":
            continue
        if not a.get("device_id"):
            continue
        if a.get("dead_cookie"):
            continue
        cookie = a.get("cookie") or ""
        if "_|WARNING" not in cookie:
            continue
        user = (a.get("username") or "").strip()
        if not user:
            continue
        out[user] = f"{user}:{a.get('password') or ''}:{cookie}"
    return out


def _zp_refresh_jobs():
    """Poll each non-terminal tracked job; update its counts/status. Returns the
    set of usernames currently sitting in an active (non-terminal) job."""
    with _zp_lock:
        jobs = _zp_ledger["jobs"]
    for job in jobs:
        if job.get("status") in _ZP_TERMINAL:
            continue
        jid = job.get("job_id")
        if not jid:
            job["status"] = "failed"
            continue
        data, err, status = _zp_curl("GET", f"/status/{jid}", timeout=15)
        # A completed job is purged from ZeroSolver and returns 404 — often WITH a
        # JSON error body, so the status code must be checked BEFORE the dict check
        # below. Otherwise such a job is never reconciled to terminal: it stays
        # "active" forever, inflating in_queue and pinning its accounts so they're
        # never retried.
        if status in (404, 410):
            job["status"] = "completed"
            continue
        if err or status != 200 or not isinstance(data, dict):
            continue                          # transient/non-OK: keep prior state, retry next cycle
        for k in ("status", "total_accounts", "processed", "successful",
                  "already_solved", "failed", "charged_credits"):
            if data.get(k) is not None:
                job[k] = data[k]
    # Prune: keep every active job + the 50 most-recent terminal ones.
    with _zp_lock:
        active = [j for j in jobs if j.get("status") not in _ZP_TERMINAL]
        terminal = [j for j in jobs if j.get("status") in _ZP_TERMINAL]
        terminal.sort(key=lambda j: j.get("submitted_ts", 0))
        _zp_ledger["jobs"] = active + terminal[-50:]
        inq = set()
        for j in active:
            inq.update(j.get("usernames") or [])
    _zp_save_state()
    return inq


def _zp_run_cycle(manual=False):
    """One sweep: refresh active jobs → find captcha accounts NOT in the queue →
    submit them as one in-game /submit job. Serialized so a manual run and the
    20-min loop can't double-submit the same accounts."""
    res = {"submitted": 0, "candidates": 0, "captcha_total": 0,
           "skipped_in_queue": 0, "job_id": None, "error": None, "manual": manual}
    if not _zp_cycle_lock.acquire(blocking=False):
        res["error"] = "a cycle is already running"
        return res
    try:
        if not _zp_loaded:
            _zp_load_state()
        try:
            inq = _zp_refresh_jobs()
        except Exception as e:
            inq = set()
            _zp_set_last(f"refresh: {str(e)[:120]}")

        accounts, aerr = _zp_load_accounts()
        if aerr and not accounts:
            res["error"] = f"accounts: {aerr}"
            _zp_set_last(res["error"])
            return res

        # Device → group map so we can prioritise MM2 (Murder Mystery 2) accounts
        # when the budget only covers a partial batch.
        try:
            devices, _, _ = farmsync_get_state()
        except Exception:
            devices = []
        did2group = {d.get("id"): (d.get("group_name") or "") for d in (devices or [])}
        u2group = {(a.get("username") or ""): (did2group.get(a.get("device_id")) or "")
                   for a in accounts}

        lines = _zp_captcha_lines(accounts)
        res["captcha_total"] = len(lines)
        candidates = {u: ln for u, ln in lines.items() if u not in inq}
        res["skipped_in_queue"] = len(lines) - len(candidates)
        res["candidates"] = len(candidates)
        with _zp_lock:
            _zp_ledger["last_cycle_ts"] = time.time()

        if not candidates:
            _zp_save_state()
            return res

        # Priority order: MM2 (Murder Mystery 2) accounts first, then everything
        # else — so a budget-limited partial batch clears Murder Mystery 2 first.
        def _zp_is_mm2(u):
            g = u2group.get(u, "").lower()
            return "murder mystery 2" in g or "mm2" in g
        user_list = sorted(candidates.keys(), key=lambda u: (0 if _zp_is_mm2(u) else 1, u))
        res["mm2_candidates"] = sum(1 for u in user_list if _zp_is_mm2(u))
        if len(user_list) > ZP_SOLVER_MAX_PER_CYCLE:
            user_list = user_list[:ZP_SOLVER_MAX_PER_CYCLE]
            _auto_log(f"ZP solver: capping this cycle at {ZP_SOLVER_MAX_PER_CYCLE} "
                      f"accounts (the rest go next cycle)")

        # Budget: send as many as the balance affords (PARTIAL batch) rather than
        # skipping everything. user_list is already MM2-first, so a partial batch
        # clears Murder Mystery 2 before the rest.
        credits, _cerr = _zp_get_credits(force=True)
        eff = (credits or {}).get("effective")
        if isinstance(eff, (int, float)):
            # Spend ~97% of effective, not 100% — ZeroSolver rejects a submit whose
            # reserve equals the balance to the cent ("Insufficient Solver Credits"
            # at exact parity), so leave a small headroom.
            affordable = int(eff * 0.97 / ZP_SOLVER_COST_PER_SOLVE)
            if affordable < 1:
                msg = f"insufficient credits: have ${eff:.2f} (need ${ZP_SOLVER_COST_PER_SOLVE}/account)"
                res["error"] = msg
                _zp_set_last(msg)
                _auto_log(f"ZP solver: {msg} — skipped {len(user_list)} accounts")
                return res
            if len(user_list) > affordable:
                res["partial"] = True
                _auto_log(f"ZP solver: balance ${eff:.2f} covers {affordable}/{len(user_list)} "
                          f"candidates — sending partial batch, MM2 first")
                user_list = user_list[:affordable]

        payload = {"accounts": "\n".join(candidates[u] for u in user_list)}
        if ZP_SOLVER_CAPTCHA_TYPE and ZP_SOLVER_CAPTCHA_TYPE != "ingame":
            payload["captcha_type"] = ZP_SOLVER_CAPTCHA_TYPE
        data, err, status = _zp_curl("POST", "/submit", body=payload, timeout=90)
        if err or status != 200 or not isinstance(data, dict) or not data.get("job_id"):
            msg = err or (data.get("error") if isinstance(data, dict) else None) or f"HTTP {status}"
            res["error"] = f"submit: {msg}"
            _zp_set_last(res["error"])
            _auto_log(f"ZP solver submit failed: {str(msg)[:100]}")
            return res

        job = {
            "job_id": data["job_id"],
            "usernames": user_list,
            "submitted_ts": time.time(),
            "captcha_type": ZP_SOLVER_CAPTCHA_TYPE,
            "status": "pending",
            "total_accounts": data.get("total_accounts", len(user_list)),
            "processed": 0, "successful": 0, "already_solved": 0, "failed": 0,
            "charged_credits": 0, "estimated_cost": data.get("estimated_cost"),
        }
        with _zp_lock:
            _zp_ledger["jobs"].append(job)
            _zp_ledger["last_submit_ts"] = time.time()
            _zp_ledger["sent_total"] = _zp_ledger.get("sent_total", 0) + len(user_list)
            _zp_ledger["last_error"] = None
        _zp_save_state()
        res["submitted"] = len(user_list)
        res["job_id"] = data["job_id"]
        _auto_log(f"ZP solver: submitted {len(user_list)} captcha accounts "
                  f"(job {str(data['job_id'])[:8]}, est {data.get('estimated_cost')} cr)")
        return res
    finally:
        _zp_cycle_lock.release()


def _zp_get_active(force=False):
    """Cached GET /active → list of jobs ZeroSolver is actively working right now.
    This is the live source of truth for the queue, independent of our ledger
    (which only reconciles each 20-min cycle). Returns None on error (callers fall
    back to the ledger)."""
    with _zp_lock:
        if not force and _zp_active_cache["jobs"] is not None and \
                (time.time() - _zp_active_cache["ts"]) < 30:
            return _zp_active_cache["jobs"]
    data, err, status = _zp_curl("GET", "/active", timeout=15)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        return None
    with _zp_lock:
        _zp_active_cache.update({"jobs": jobs, "ts": time.time()})
    return jobs


@app.route("/api/zpsolver/status")
def api_zpsolver_status():
    """Balance + queue + recent jobs for the Dashboard tile / Settings card."""
    if not _zp_loaded:
        _zp_load_state()
    force = request.args.get("force") == "1"
    credits, cerr = _zp_get_credits(force=force)
    with _zp_lock:
        jobs = list(_zp_ledger["jobs"])
        last_cycle = _zp_ledger.get("last_cycle_ts", 0)
        last_submit = _zp_ledger.get("last_submit_ts", 0)
        sent_total = _zp_ledger.get("sent_total", 0)
        last_error = _zp_ledger.get("last_error")
    active = [j for j in jobs if j.get("status") not in _ZP_TERMINAL]
    # Prefer ZeroSolver's live /active list for the queue numbers — the ledger only
    # reconciles each 20-min cycle, so it lags (and over-counts purged jobs). Fall
    # back to the ledger if /active is unreachable.
    live = _zp_get_active(force=force)
    if live is not None:
        in_queue = sum(max(0, (j.get("total_accounts") or 0) - (j.get("processed") or 0)) for j in live)
        queued_accounts = sum((j.get("total_accounts") or 0) for j in live)
        active_count = len(live)
    else:
        in_queue = 0          # accounts ZeroSolver still has to work through
        queued_accounts = 0   # distinct accounts in our active jobs
        for j in active:
            tot = j.get("total_accounts") or len(j.get("usernames") or [])
            in_queue += max(0, tot - (j.get("processed") or 0))
            queued_accounts += len(j.get("usernames") or [])
        active_count = len(active)
    key_present = bool(_zp_key())
    if not key_present:
        st = "nokey"
    elif _zp_solver_paused():
        st = "paused"
    elif cerr or last_error:
        st = "error"
    else:
        st = "ok"
    bal = None
    if credits:
        bal = credits.get("effective", credits.get("balance"))
    return jsonify({
        "status": st,
        "paused": _zp_solver_paused(),
        "key_present": key_present,
        "balance": bal,
        "balance_total": (credits or {}).get("balance"),
        "reserved": (credits or {}).get("reserved"),
        "in_queue": in_queue,
        "queued_accounts": queued_accounts,
        "active_jobs": active_count,
        "sent_total": sent_total,
        "last_cycle_ts": last_cycle,
        "last_submit_ts": last_submit,
        "last_error": last_error or cerr,
        "cost_per_solve": ZP_SOLVER_COST_PER_SOLVE,
        "captcha_type": ZP_SOLVER_CAPTCHA_TYPE,
        "interval_min": ZP_SOLVER_INTERVAL // 60,
        "jobs": [
            {k: j.get(k) for k in ("job_id", "status", "total_accounts", "processed",
             "successful", "already_solved", "failed", "charged_credits", "submitted_ts")}
            for j in sorted(jobs, key=lambda x: x.get("submitted_ts", 0), reverse=True)[:10]
        ],
    })


@app.route("/api/zpsolver/run", methods=["POST"])
def api_zpsolver_run():
    """Manually trigger one ZP solver sweep now (runs even while paused)."""
    return jsonify(_zp_run_cycle(manual=True))


@app.route("/api/zpsolver/toggle", methods=["POST"])
def api_zpsolver_toggle():
    """Pause/resume the automatic 20-min ZP solver loop."""
    if _zp_solver_paused():
        try:
            os.remove(ZP_SOLVER_PAUSE_FLAG)
        except Exception:
            pass
        _auto_log("ZP solver resumed")
        return jsonify({"ok": True, "paused": False, "message": "ZP solver resumed"})
    try:
        with open(ZP_SOLVER_PAUSE_FLAG, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat())
    except Exception:
        pass
    _auto_log("ZP solver paused")
    return jsonify({"ok": True, "paused": True, "message": "ZP solver paused"})


# ═══════════════════════════════════════════════════════════════
#  Routes — Automation log
# ═══════════════════════════════════════════════════════════════

@app.route("/api/automation/log")
def api_automation_log():
    return jsonify({"log": automation_log, "count": len(automation_log)})


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Graceful shutdown — uses os._exit() (ExitProcess) so Windows
    releases the listening socket cleanly. Without this, taskkill /F
    (TerminateProcess) leaves zombie LISTEN entries and the port
    becomes unusable until reboot. After calling this, the next
    Flask boot can reuse the same port immediately."""
    def _shutdown_after_response():
        # Stop the automation child first if we own it (so it doesn't orphan)
        try:
            farmsync_automation_stop()
        except Exception:
            pass
        time.sleep(0.5)  # give the HTTP response time to flush
        os._exit(0)
    threading.Thread(target=_shutdown_after_response, daemon=True).start()
    return jsonify({"ok": True, "message": "shutting down in 0.5s"})


# ═══════════════════════════════════════════════════════════════
#  Live offers — per-platform inventory snapshot
# ═══════════════════════════════════════════════════════════════
#  Fetches the *currently-listed* (live) offers on each platform. This
#  is different from `sales` (orders that already closed).
#
#  Cheap (HTTP) platforms — funpay/funpay2/u7buy — refresh on a
#  background timer every LIVE_OFFERS_HTTP_INTERVAL seconds.
#  Selenium platforms — eldorado/g2g — only refresh on explicit user
#  request, because Chrome is contested by the sale-sync paths and the
#  Chrome-freeze flag.
#
#  Cache lives in `_live_offers_cache` keyed by platform. Each entry:
#    {"offers": [...], "count": int, "updated_ts": float,
#     "duration_ms": int, "error": str|None}

LIVE_OFFERS_HTTP_INTERVAL = 600    # 10 min — cheap platforms
LIVE_OFFERS_CHROME_INTERVAL = 1800 # 30 min — Selenium platforms
# Persisted cache file — survives Flask restarts so Eldorado/G2G counts don't
# vanish on every relaunch. Written after every successful refresh.
LIVE_OFFERS_CACHE_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "_live_offers.json")

# Persistent per-offer history. Each entry records when we first saw a
# (platform, offer_id) pair; live_seconds is computed on the fly from
# (now - first_seen_ts) so it reflects true wall-clock time and stays
# accurate even when scrapes are missed.
LIVE_OFFERS_HISTORY_FILE = os.path.join(FARMSYNC_AUTOMATION_DIR, "_live_offers_history.json")
_offer_live_history = {}
_offer_live_history_lock = threading.Lock()
FUNPAY_OFFER_NODES = ("927", "401")   # Adopt Me + Roblox
# Game/category each FunPay lot-node lists under — used to group live offers
FUNPAY_NODE_CATEGORY = {"927": "Adopt Me", "401": "Roblox"}
U7BUY_ADOPTME_ENTITY = "1888155277733253149"
U7BUY_ROBLOX_ENTITY  = "1888155277733253265"
U7BUY_BUSINESS_ID    = "1820693954263351302"
G2G_ROBLOX_CAT       = "5830014a-b974-45c6-9672-b51e83112fb7"

_live_offers_cache = {}
_live_offers_lock = threading.Lock()

# Backup directory for dated daily snapshots. Lives under web/data/backups so
# it's separated from runtime artefacts in farmsync_automation/.
LIVE_OFFERS_BACKUP_DIR = os.path.join(BASE_DIR, "web", "data", "backups")
LIVE_OFFERS_BACKUP_ROTATING = 5    # keep N rolling backups (.1, .2, ... .5)
LIVE_OFFERS_BACKUP_DAYS = 30       # keep dated snapshots for N days


def _atomic_write_with_rotation(path, payload_str):
    """Write payload to `path` atomically + rotate up to N old versions.

    Before overwriting `path`, sanity-check that the payload looks
    valid (non-empty JSON). Then shuffle existing backups:
        path.4 → (deleted)
        path.3 → path.4
        path.2 → path.3
        path.1 → path.2
        path   → path.1
    Then write new payload to path.tmp and atomically rename to path."""
    try:
        if not payload_str or len(payload_str.strip()) < 2:
            return   # don't overwrite a good file with empty data
        json.loads(payload_str)   # parses? if not, abort
    except Exception:
        return
    try:
        # Rotate existing backups
        for i in range(LIVE_OFFERS_BACKUP_ROTATING, 1, -1):
            src = f"{path}.{i-1}"
            dst = f"{path}.{i}"
            if os.path.exists(src):
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(src, dst)
                except Exception:
                    pass
        # Move current to .1 (if present)
        if os.path.exists(path):
            dst = f"{path}.1"
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(path, dst)
            except Exception:
                pass
        # Atomic write new payload
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload_str)
        os.replace(tmp, path)
    except Exception:
        pass


def _write_daily_snapshot(source_path, label):
    """Once per day, copy `source_path` into web/data/backups/<label>_YYYY-MM-DD.json.
    Idempotent — if today's snapshot already exists, no-op. Prunes snapshots
    older than LIVE_OFFERS_BACKUP_DAYS."""
    if not os.path.exists(source_path):
        return
    try:
        os.makedirs(LIVE_OFFERS_BACKUP_DIR, exist_ok=True)
    except Exception:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    target = os.path.join(LIVE_OFFERS_BACKUP_DIR, f"{label}_{today}.json")
    if os.path.exists(target):
        return   # already snapshotted today
    try:
        with open(source_path, "rb") as src, open(target, "wb") as dst:
            dst.write(src.read())
    except Exception:
        return
    # Prune snapshots older than the retention window
    cutoff = time.time() - (LIVE_OFFERS_BACKUP_DAYS * 86400)
    try:
        for name in os.listdir(LIVE_OFFERS_BACKUP_DIR):
            if not name.startswith(label + "_") or not name.endswith(".json"):
                continue
            full = os.path.join(LIVE_OFFERS_BACKUP_DIR, name)
            try:
                if os.path.getmtime(full) < cutoff:
                    os.remove(full)
            except Exception:
                pass
    except Exception:
        pass


def _load_with_fallback(path):
    """Try to load JSON from `path`. If it's missing or corrupt, walk through
    the rotating backups (.1 .. .N) and dated snapshots (newest first), and
    return the first one that parses. Returns (data, source) — source is a
    human-readable label for logging."""
    candidates = [(path, "current")]
    for i in range(1, LIVE_OFFERS_BACKUP_ROTATING + 1):
        candidates.append((f"{path}.{i}", f"rotating .{i}"))
    # Add dated snapshots (newest first). Strict pattern so the cache loader
    # doesn't accidentally match the history snapshot files (shared prefix).
    label = os.path.basename(path).rsplit(".", 1)[0]
    try:
        import re
        pattern = re.compile(r"^" + re.escape(label) + r"_\d{4}-\d{2}-\d{2}\.json$")
        dated = sorted(
            (f for f in os.listdir(LIVE_OFFERS_BACKUP_DIR) if pattern.match(f)),
            reverse=True
        )
        for f in dated:
            candidates.append((os.path.join(LIVE_OFFERS_BACKUP_DIR, f),
                               f"snapshot {f}"))
    except Exception:
        pass
    for candidate, source in candidates:
        if not os.path.exists(candidate):
            continue
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data, source
        except Exception:
            continue
    return None, "no recoverable backup"


def _save_live_offers_cache():
    """Persist offers cache with rotating backups (last 5 versions) plus a
    daily dated snapshot. Sanity-checks the payload before overwriting so a
    bad serialisation can never wipe the good data on disk."""
    try:
        with _live_offers_lock:
            payload = json.dumps(_live_offers_cache, default=str)
    except Exception:
        return
    _atomic_write_with_rotation(LIVE_OFFERS_CACHE_FILE, payload)
    _write_daily_snapshot(LIVE_OFFERS_CACHE_FILE, "_live_offers")


def _load_live_offers_cache():
    """Restore the cache from disk on Flask startup. Tries the primary file,
    then the rotating backups, then the dated snapshots — first one that
    parses wins. Marks every loaded platform as stale so the UI shows the
    data is from a prior run."""
    data, source = _load_with_fallback(LIVE_OFFERS_CACHE_FILE)
    if not isinstance(data, dict):
        return
    with _live_offers_lock:
        for plat, entry in data.items():
            if not isinstance(entry, dict):
                continue
            entry["stale"] = True
            _live_offers_cache[plat] = entry
    if source != "current":
        try:
            _auto_log(f"Live offers: recovered cache from backup ({source}) — primary file was corrupt")
        except Exception:
            pass


def _save_offer_live_history():
    """Persist per-offer live-time history with rotating + dated backups."""
    try:
        with _offer_live_history_lock:
            payload = json.dumps(_offer_live_history)
    except Exception:
        return
    _atomic_write_with_rotation(LIVE_OFFERS_HISTORY_FILE, payload)
    _write_daily_snapshot(LIVE_OFFERS_HISTORY_FILE, "_live_offers_history")


def _load_offer_live_history():
    """Restore the per-offer history with backup fallback chain."""
    data, source = _load_with_fallback(LIVE_OFFERS_HISTORY_FILE)
    if isinstance(data, dict):
        with _offer_live_history_lock:
            _offer_live_history.update(data)
        if source != "current":
            try:
                _auto_log(f"Live offers: recovered history from backup ({source})")
            except Exception:
                pass


def _update_offer_live_history(platform, offers):
    """Record sighting timestamps for every offer in THIS scrape.

    Algorithm:
        - First time we see (platform, offer_id) → store first_seen_ts = now.
          Decorate the offer with live_seconds = 0 (just started ticking).
        - Subsequent sightings → update last_seen_ts = now, bump scrape_count.
          Decorate with live_seconds = now - first_seen_ts (true wall-clock
          elapsed time since first sighting, INDEPENDENT of how many scrapes
          we ran in between).

    This means a missed scrape doesn't undercount, and the live time keeps
    growing smoothly between page reloads (the value is recomputed at every
    /api/offers/live serve via _decorate_offers_with_live_time)."""
    now = time.time()
    changed = False
    with _offer_live_history_lock:
        for offer in offers:
            oid = offer.get("offer_id") or ""
            if not oid:
                continue   # skip rows with no stable ID
            key = f"{platform}:{oid}"
            entry = _offer_live_history.get(key)
            if entry is None:
                entry = {
                    "first_seen_ts": now,
                    "last_seen_ts":  now,
                    "scrape_count":  1,
                }
                offer["live_seconds"] = 0
            else:
                entry["scrape_count"] = int(entry.get("scrape_count", 0)) + 1
                entry["last_seen_ts"] = now
                entry.setdefault("first_seen_ts", now)
                offer["live_seconds"] = max(0, int(now - entry["first_seen_ts"]))
            _offer_live_history[key] = entry
            offer["scrape_count"] = entry["scrape_count"]
            changed = True
    if changed:
        _save_offer_live_history()


def _decorate_offers_with_live_time(platform, offers):
    """Recompute live_seconds for cached offers on every serve. Without this,
    the value would freeze at whatever it was on the last scrape — but real
    wall-clock time is still ticking. Called from the /api/offers/live route."""
    if not offers:
        return
    now = time.time()
    with _offer_live_history_lock:
        for offer in offers:
            oid = offer.get("offer_id") or ""
            if not oid:
                continue
            entry = _offer_live_history.get(f"{platform}:{oid}")
            if entry and "first_seen_ts" in entry:
                offer["live_seconds"] = max(0, int(now - entry["first_seen_ts"]))
                offer["scrape_count"] = entry.get("scrape_count", 0)

# Regexes for FunPay HTML scrape (per liveoffers.txt skill)
_FP_ROW_RE = re.compile(
    r'<a[^>]*href="https://funpay\.com/en/lots/offerEdit[^"]*offer=(\d+)"'
    r'[^>]*class="(tc-item[^"]*)"[^>]*>(.*?)</a>',
    re.S | re.I,
)
_FP_DESC_RE  = re.compile(r'<div class="tc-desc-text">([^<]+)</div>')
_FP_PRICE_RE = re.compile(r'<div class="tc-price"[^>]*data-s="([\d.]+)"')
# G2G censors "Admin" → "A****" — restore before display
_G2G_ADMIN_RE = re.compile(r"A\*{2,}\s*Abuse", re.I)


def _funpay_discover_offer_nodes(session):
    """Discover every lot category on the active seller's FunPay profile as
    [(node_id_str, category_name)]. Lets the live-offers scrape pick up new
    categories (e.g. a freshly-created 'Grow a Garden 2' lot) automatically,
    with no hardcoded node list. Returns [] if the user_id / profile can't be
    resolved, so the caller can fall back to the static node list."""
    uid = _funpay_discover_user_id(session)
    if not uid:
        return []
    try:
        r = session.get(f"https://funpay.com/users/{uid}/", timeout=15)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    out, seen = [], set()
    for m in re.finditer(r'href="https?://funpay\.com/lots/(\d+)/"[^>]*>([^<]+)</a>', r.text):
        nid = m.group(1)
        if nid in seen:
            continue
        seen.add(nid)
        name = re.sub(r"\s+", " ", m.group(2)).strip()
        # FunPay labels each lot "<game> <platform>" (e.g. "Grow a Garden 2
        # Roblox"). Drop the trailing platform word so the category matches
        # the same game on the other platforms.
        name = re.sub(r"\s+Roblox$", "", name).strip() or name
        out.append((nid, name))
    return out


def _funpay_fetch_live_offers(label, cookie_file):
    """Scrape live (non-sold) offers from FunPay for one account.

    Lot categories are discovered from the seller's profile, so newly-created
    lots (e.g. a 'Grow a Garden 2' category) are picked up automatically and
    each offer is tagged with the category it lists under. Known nodes keep
    their canonical name (so they merge with the other platforms); brand-new
    lots use FunPay's own label. Falls back to the static FUNPAY_OFFER_NODES
    if discovery yields nothing. Returns (offers, error_str_or_None)."""
    s = funpay_session(cookie_file)
    if not s:
        return [], f"FunPay [{label}] cookie file missing/unreadable"
    # Discover the seller's lot categories FIRST — the profile listing only
    # returns the lots with the session's default headers (incl.
    # X-Requested-With) intact, so this must run before we strip that header
    # below. Fall back to the hardcoded nodes if the profile can't be read
    # (e.g. session not logged in).
    try:
        cats = _funpay_discover_offer_nodes(s)
    except Exception:
        cats = []
    if not cats:
        cats = [(node, FUNPAY_NODE_CATEGORY.get(node, "")) for node in FUNPAY_OFFER_NODES]
    # Now strip X-Requested-With — removing it keeps each /lots/<node>/trade
    # response shape consistent with what the row regex was tuned for.
    s.headers.pop("X-Requested-With", None)
    offers = []
    for node, disc_name in cats:
        # Known games keep their canonical label so they merge across
        # platforms; anything new uses the name FunPay shows for the lot.
        catname = FUNPAY_NODE_CATEGORY.get(node) or disc_name or ""
        try:
            r = s.get(f"https://funpay.com/en/lots/{node}/trade", timeout=15)
        except Exception as e:
            return offers, f"FunPay [{label}] node {node}: {str(e)[:120]}"
        if r.status_code != 200:
            return offers, f"FunPay [{label}] node {node}: HTTP {r.status_code}"
        if "menu-item-login" in r.text:
            return offers, f"FunPay [{label}] logged out (re-run refresh_cookies.py)"
        for m in _FP_ROW_RE.finditer(r.text):
            oid, cls, body = m.group(1), m.group(2), m.group(3)
            if "warning" in cls:
                continue   # sold / inactive — skip
            d = _FP_DESC_RE.search(body)
            p = _FP_PRICE_RE.search(body)
            offers.append({
                "platform": label,
                "offer_id": oid,
                "title": (d.group(1).strip() if d else "")[:200],
                "price": float(p.group(1)) if p else None,
                "node": node,
                "category": catname,
                "url": f"https://funpay.com/en/lots/offerEdit?offer={oid}",
            })
    return offers, None


def _u7buy_fetch_live_offers():
    """Scrape live offers from u7buy OpenAPI (Adopt Me + Roblox SPUs)."""
    auth = u7buy_auth_header()
    if not auth:
        return [], "u7buy OpenAPI key missing (u7buy/u7buy_apikey.txt)"
    headers = {"Authorization": auth, "Accept": "application/json"}
    offers = []
    for ent, sublabel in ((U7BUY_ADOPTME_ENTITY, "Adopt Me"),
                          (U7BUY_ROBLOX_ENTITY,  "Roblox")):
        for page in range(1, 25):
            url = f"{U7BUY_OPENAPI_BASE}/offer_common/list"
            params = {
                "businessId": U7BUY_BUSINESS_ID,
                "entityId":   ent,
                "pageNum":    page,
                "pageSize":   50,
            }
            try:
                r = http_requests.get(url, headers=headers, params=params,
                                      timeout=20, verify=False)
            except Exception as e:
                return offers, f"u7buy: {str(e)[:120]}"
            if r.status_code != 200:
                return offers, f"u7buy: HTTP {r.status_code}"
            try:
                body = r.json()
            except Exception:
                return offers, "u7buy: bad JSON response"
            data = body.get("data", {}) if isinstance(body, dict) else {}
            pr = data.get("pageResult") if isinstance(data, dict) else None
            page_rows = pr if isinstance(pr, list) else []
            if not page_rows:
                break
            for o in page_rows:
                if o.get("onSale") != 1:
                    continue
                try:
                    price = float(o.get("priceDouble") or o.get("priceUsd") or 0)
                except (TypeError, ValueError):
                    price = None
                offers.append({
                    "platform": "u7buy",
                    "offer_id": str(o.get("offerId") or ""),
                    "title": (o.get("offerName") or "")[:200],
                    "price": price,
                    "subtype": sublabel,
                    "category": sublabel,
                    "inventory": o.get("inventory"),
                })
    return offers, None


def _eldorado_scrape_offers_inner(drv, max_pages=20):
    """Inner scrape that operates on an already-acquired Chrome driver.
    Returns (offers, error). Callers must hold the chrome lock themselves.

    Split from _eldorado_fetch_live_offers so the same scrape can be folded
    into the presence loop without nesting chrome_session() (which would
    deadlock on the non-reentrant chrome lock).

    Per-page sleep uses random jitter (2-5s) instead of a fixed value so
    the navigation pattern looks less bot-like to Eldorado's anti-bot
    rules. Combined with the 1-hour scrape cadence (vs 20 min for the
    other Chrome platforms), this reduces session-kick frequency."""
    import random
    from selenium.webdriver.common.by import By
    offers = []
    _chrome_dlog("scrape eldorado: start", max_pages=max_pages)
    _t0 = time.time()
    try:
        # ─── Warm-up: hit the orders/sold URL first ─────────────
        # Same URL the sale-sync uses (which works reliably). Eldorado's
        # anti-bot treats `/dashboard/orders/sold` and
        # `/dashboard/offers/Account` differently — the orders page accepts
        # the cookie session more readily. By landing on orders first,
        # waiting a beat, then navigating to offers, we mimic the natural
        # in-app flow a logged-in seller would take, and the offers page
        # inherits the freshly-validated session.
        #
        # As a bonus: if orders/sold ALSO redirects to login, we know
        # the entire session is dead (not just an offers-specific kick)
        # and we can return a definitive "logged out" without wasting
        # time on the offers page.
        _chrome_dlog("scrape eldorado: warmup via orders URL")
        try:
            drv.get(ELDORADO_ORDERS_URL)
        except Exception as e:
            _chrome_dlog("scrape eldorado: warmup nav failed", err=str(e)[:160])
            return offers, f"Eldorado warmup: {str(e)[:120]}"
        is_login, reason = _is_login_url(drv.current_url, ELDORADO_LOGIN_HOST)
        if is_login:
            _chrome_dlog("scrape eldorado: warmup hit login page → logged out",
                         url=drv.current_url, reason=reason)
            return offers, "Eldorado logged out — run eldorado\\login.bat"
        # Settle on the orders page like a human would (read, hover...).
        # Jittered to avoid a uniform-spaced bot signature.
        time.sleep(random.uniform(2.0, 4.5))

        if True:   # preserve indentation from original
            # Eldorado now serves ~10 offers/page across ~25 pages (was ~40/page
            # / ~6 pages). Walk every page the pagination control advertises,
            # dedup by offer_id, and treat an unreachable mid page as INCOMPLETE
            # rather than a short success. safety_cap guards a misread control.
            seen_ids = set()
            last_page = None
            safety_cap = max(max_pages, 100)
            page = 0
            while True:
                page += 1
                if last_page is not None and page > last_page:
                    break          # covered every advertised page — complete
                if page > safety_cap:
                    break
                _chrome_dlog("scrape eldorado: page", page=page,
                             accumulated=len(offers), last_page=last_page)
                target = f"https://www.eldorado.gg/dashboard/offers/Account?pageIndex={page}"
                try:
                    drv.get(target)
                except Exception as e:
                    return offers, f"Eldorado page {page}: {str(e)[:120]}"
                is_login, reason = _is_login_url(drv.current_url, ELDORADO_LOGIN_HOST)
                if is_login:
                    _chrome_dlog("scrape eldorado: page hit login page",
                                 page=page, url=drv.current_url, reason=reason)
                    return offers, "Eldorado logged out — run eldorado\\login.bat"
                # Wait for offer rows to appear AND skeletons to clear.
                # Eldorado redesigned the offers page (.offer-list-item is gone,
                # replaced with .offer + .offer__top/.offer__bottom). The new
                # page also lazy-loads data — rendering empty placeholders with
                # 25+ <ngx-skeleton-loader> elements until the API fetch returns.
                # We must wait for the skeletons to disappear or we'll scrape
                # empty placeholders.
                #
                # Two acceptance signals (either is sufficient):
                #   A) `.offer-list-item` matches (legacy DOM — page hydrated)
                #   B) `.offer` matches AND `.skeleton-loader` count is 0
                #      (new DOM — page hydrated)
                # A "no offers" sentinel in body text counts as legitimate empty.
                wait_budget = 45 if page <= 2 else 35
                matched_kind = None    # "old" / "new" / None
                deadline = time.time() + wait_budget
                while time.time() < deadline:
                    try:
                        state = drv.execute_script(
                            "return { skel: document.querySelectorAll('.skeleton-loader').length, "
                            " ol: document.querySelectorAll('.offer-list-item').length, "
                            " on: document.querySelectorAll('.offer').length, "
                            " body: (document.body.innerText||'').toLowerCase() };"
                        )
                        if (state.get('ol') or 0) > 0:
                            matched_kind = 'old'
                            break
                        if (state.get('on') or 0) > 0 and (state.get('skel') or 0) == 0:
                            matched_kind = 'new'
                            break
                        body_text = state.get('body') or ''
                        if "no offers" in body_text or "you have no" in body_text:
                            return offers, None
                    except Exception:
                        pass
                    time.sleep(1.5)
                # Read the real last page from the pagination control once a page
                # hydrates — the authoritative end-of-pagination signal (max of the
                # `.pagination-item` numbers, e.g. 1 2 3 4 5 … 25 → 25).
                if last_page is None and matched_kind:
                    try:
                        lp = drv.execute_script(
                            "var mx=0;document.querySelectorAll('.pagination-item')"
                            ".forEach(function(el){var t=(el.textContent||'').trim();"
                            "if(/^\\d{1,4}$/.test(t)) mx=Math.max(mx,parseInt(t,10));});"
                            "return mx;")
                        if lp and int(lp) >= 1:
                            last_page = int(lp)
                            _chrome_dlog("scrape eldorado: last_page", last_page=last_page)
                    except Exception:
                        pass
                if not matched_kind:
                    # Page didn't hydrate. Retry the SAME page (refresh) up to 2
                    # more times — at ~10 offers/page any single throttled page
                    # used to truncate the whole scrape. No prev-page-size guard.
                    for _retry in range(2):
                        _auto_log(f"Eldorado page {page}: empty after {wait_budget}s "
                                  f"(retry {_retry + 1}/2)")
                        try:
                            time.sleep(3)
                            drv.refresh()
                            retry_deadline = time.time() + 45
                            while time.time() < retry_deadline:
                                try:
                                    st = drv.execute_script(
                                        "return { skel: document.querySelectorAll('.skeleton-loader').length, "
                                        " ol: document.querySelectorAll('.offer-list-item').length, "
                                        " on: document.querySelectorAll('.offer').length, "
                                        " body: (document.body.innerText||'').toLowerCase() };"
                                    )
                                    if (st.get('ol') or 0) > 0:
                                        matched_kind = 'old'; break
                                    if (st.get('on') or 0) > 0 and (st.get('skel') or 0) == 0:
                                        matched_kind = 'new'; break
                                    if "no offers" in (st.get('body') or ''):
                                        return offers, None
                                except Exception:
                                    pass
                                time.sleep(1.5)
                        except Exception:
                            pass
                        if matched_kind:
                            break
                if not matched_kind:
                    # Still empty after retries. If the control says more pages
                    # exist, this run is INCOMPLETE — return an error so the cache
                    # keeps the previous (good) count instead of a truncation.
                    if last_page is not None and page < last_page:
                        return offers, (
                            f"Eldorado: page {page}/{last_page} never hydrated "
                            f"after retries — incomplete ({len(offers)} so far)")
                    if offers:
                        return offers, None
                    return offers, (
                        f"Eldorado: no offer rows on page {page} "
                        f"(URL {drv.current_url})")
                # Jittered hydrate wait — 2-5s random, looks less bot-like
                # than a fixed 1.5s every time
                time.sleep(random.uniform(2.0, 5.0))
                raw = drv.execute_script(r"""
                    // Pick whichever selector hydrated. Old DOM: .offer-list-item.
                    // New DOM (June 2026 redesign): .offer (BEM-style with
                    // .offer__top / .offer__bottom / .offer-details / .offer-quantity).
                    // Try both — the same extraction logic works since most field
                    // locations are defensive (looking for relative selectors
                    // within the row, with several fallbacks).
                    var items = document.querySelectorAll('.offer-list-item');
                    if (!items.length) items = document.querySelectorAll('.offer');
                    var out = [];
                    items.forEach(function(it) {
                        // Skip rows that are still pure-skeleton (no real data
                        // hydrated yet). A row with only ngx-skeleton-loader
                        // descendants has empty .textContent except for icon glyphs.
                        if (it.querySelector('.skeleton-loader') && !it.querySelector('img,input,a[href*="/oa/"]')) {
                            return;
                        }
                        // Offer ID — public buyer-view link /<game>/oa/<uuid>
                        var oid = '';
                        var aPub = it.querySelector('a[href*="/oa/"]');
                        if (aPub) {
                            var href = aPub.getAttribute('href') || '';
                            var m = href.match(/\/oa\/([0-9a-f-]{8,})/i);
                            if (m) oid = m[1];
                        }
                        // Status detection — .chip-status-red marks closed,
                        // .chip-status-green marks active. Plus aria-label fallback.
                        var closed = !!it.querySelector('.chip-status-red, [class*="chip-status-red"]');
                        var paused = false;
                        it.querySelectorAll('[aria-label]').forEach(function(el){
                            var al = (el.getAttribute('aria-label') || '').toLowerCase();
                            if (al === 'paused' || al.indexOf('paused') === 0) paused = true;
                            if (al === 'closed' || al.indexOf('closed') === 0) closed = true;
                        });
                        // Title — extract from full row text. Old layout:
                        //   "Adopt Me Expires in 21 d 2 hActive🐾 751 Pets | ... | Quantity:1 ..."
                        // New layout splits across .offer-details + .offer-quantity but
                        // textContent of the wrapper still has it all concatenated.
                        var fullText = (it.textContent || '').replace(/\s+/g, ' ').trim();
                        var statusMatch = fullText.match(/(Active|Paused|Closed)/);
                        var afterStatus = statusMatch
                            ? fullText.substring(statusMatch.index + statusMatch[0].length)
                            : fullText;
                        var beforeQty = afterStatus.split(/Quantity:|Min qty|\$\/unit/i)[0] || afterStatus;
                        var title = beforeQty.trim().slice(0, 200);
                        // Category — the game/listing category the offer is
                        // uploaded under (e.g. "Adopt Me"). Old DOM: the <p> in
                        // .offer-bar-left. Fallback: the leading text chunk
                        // before the "Expires"/status word in the row.
                        var cat = '';
                        var catEl = it.querySelector('.offer-bar-left p');
                        if (catEl) cat = (catEl.textContent || '').replace(/\s+/g, ' ').trim();
                        if (!cat) {
                            var preStatus = fullText.split(/\s*(?:Expires|Active|Paused|Closed)/)[0] || '';
                            cat = preStatus.replace(/\s+/g, ' ').trim();
                        }
                        // Price — old layout had editable input in .offer-price-input.
                        // New layout may use a different input or a span. Try inputs
                        // first (value attribute), then any text matching $N.NN.
                        var price = null;
                        var priceInput = it.querySelector('.offer-price-input input')
                                       || it.querySelector('.offer-price input')
                                       || it.querySelector('input[type="number"]')
                                       || it.querySelector('input[inputmode="decimal"]');
                        if (priceInput) {
                            var pv = priceInput.value || priceInput.getAttribute('value');
                            if (pv) {
                                var pf = parseFloat(pv);
                                if (!isNaN(pf)) price = pf;
                            }
                        }
                        if (price === null) {
                            var pm = fullText.match(/\$\s*([0-9]+(?:\.[0-9]+)?)/);
                            if (pm) price = parseFloat(pm[1]);
                        }
                        out.push({
                            offer_id: oid,
                            title:    title,
                            price:    price,
                            category: cat,
                            closed:   closed,
                            paused:   paused
                        });
                    });
                    return JSON.stringify(out);
                """)
                try:
                    rows = json.loads(raw or "[]")
                except Exception:
                    rows = []
                if not rows:
                    # Hydrated but extracted nothing. Within the advertised range
                    # that's an INCOMPLETE scrape; past it, genuine end.
                    if last_page is not None and page < last_page:
                        return offers, (
                            f"Eldorado: page {page}/{last_page} yielded 0 rows "
                            f"— incomplete ({len(offers)} so far)")
                    if offers:
                        return offers, None
                    return offers, f"Eldorado: page {page} matched selector but yielded 0 rows"
                for o in rows:
                    if o.get("closed"):
                        continue
                    # The dashboard /edit/<uuid> URL no longer exists in the current
                    # Eldorado UI (Edit is a button, not a link). Link to the buyer-view
                    # page instead, which is reachable for any logged-in user.
                    oid = o.get("offer_id") or ""
                    # Dedup by offer_id so any page overlap can't inflate the count.
                    if oid and oid in seen_ids:
                        continue
                    if oid:
                        seen_ids.add(oid)
                    offers.append({
                        "platform": "eldorado",
                        "offer_id": oid,
                        "title":    o.get("title") or "",
                        "price":    o.get("price"),
                        "paused":   bool(o.get("paused")),
                        "category": (o.get("category") or "").strip(),
                        "url":      ("https://www.eldorado.gg/adopt-me-accounts-for-sale/oa/" + oid) if oid else None,
                    })
                # Loop continues; the while-loop's last_page check terminates it.
    except Exception as e:
        _chrome_dlog("scrape eldorado: EXCEPTION",
                     err=str(e)[:200],
                     accumulated=len(offers),
                     duration_s=int(time.time() - _t0))
        return offers, f"Eldorado: {str(e)[:160]}"
    _chrome_dlog("scrape eldorado: done",
                 offers=len(offers),
                 duration_s=int(time.time() - _t0))
    return offers, None


def _eldorado_fetch_live_offers(max_pages=20):
    """Live offers for Eldorado — JSON API only. Chrome is used solely to
    harvest the session cookie (inside _eldorado_fetch_live_offers_api →
    _eldorado_cookies); there is NO 31-page Chrome render. On API failure
    returns ([], reason) and callers keep the previously-cached offers. The
    legacy Selenium scraper (_eldorado_scrape_offers_inner) is retained as a
    manual escape hatch but is no longer called automatically."""
    return _eldorado_fetch_live_offers_api()


def _g2g_scrape_offers_inner(drv, max_pages=15):
    """Inner G2G scrape on an already-acquired Chrome driver. Callers must
    hold the chrome lock themselves.

    max_pages capped at 15: G2G typically has ~10-11 pages of offers (200 ÷ 20
    per page). Higher caps just give Chrome more time to accumulate state and
    crash mid-scrape. The loop also breaks on empty new-offer batches."""
    offers = []
    seen = set()
    _chrome_dlog("scrape g2g: start", max_pages=max_pages)
    _t0 = time.time()
    try:
        if True:
            for page in range(1, max_pages + 1):
                _chrome_dlog("scrape g2g: page", page=page, accumulated=len(offers))
                url = (f"https://www.g2g.com/offers/list?cat_id={G2G_ROBLOX_CAT}"
                       f"&status=live&page={page}")
                try:
                    drv.get(url)
                except Exception as e:
                    return offers, f"G2G page {page}: {str(e)[:120]}"
                cur = (drv.current_url or "").lower()
                if "g2g.com/login" in cur or "g2g.com/sign" in cur:
                    _chrome_dlog("scrape g2g: page hit login page",
                                 page=page, url=drv.current_url)
                    return offers, "G2G logged out — run g2g\\login.bat"
                # Wait for IDs to appear OR "No offers" sentinel
                deadline = time.time() + 25
                while time.time() < deadline:
                    has_data = drv.execute_script(
                        "return /#?G[A-Z0-9]{10,}/.test(document.body.textContent||'')"
                        " || document.body.textContent.indexOf('No offers')>=0;"
                    )
                    if has_data:
                        break
                    time.sleep(1)
                time.sleep(1.5)
                raw = drv.execute_script(r"""
                    var out=[], seen={};
                    var w=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
                    var n;
                    while ((n = w.nextNode())) {
                        var m = (n.nodeValue || '').match(/#?(G[A-Z0-9]{10,})/);
                        if (!m) continue;
                        var id = m[1];
                        if (seen[id]) continue;
                        seen[id] = true;
                        var row = n.parentElement;
                        for (var i = 0; i < 14; i++) {
                            if (!row || row === document.body) break;
                            var t = row.textContent || '';
                            if (t.indexOf(id) >= 0 && /USD\s*[0-9]/.test(t) && t.length < 1500) break;
                            row = row.parentElement;
                        }
                        if (!row) row = n.parentElement;
                        var titleEl = row.querySelector('.text-body1');
                        var title = titleEl ? (titleEl.textContent || '').replace(/\s+/g, ' ').trim() : '';
                        var pm = (row.textContent || '').match(/USD\s*([0-9]+(?:\.[0-9]+)?)/);
                        out.push({id: id, title: title.slice(0, 200), price: pm ? parseFloat(pm[1]) : null});
                    }
                    return JSON.stringify(out);
                """)
                try:
                    rows = json.loads(raw or "[]")
                except Exception:
                    rows = []
                new_count = 0
                for o in rows:
                    oid = o.get("id") or ""
                    if not oid or oid in seen:
                        continue
                    seen.add(oid)
                    title = _G2G_ADMIN_RE.sub("Admin Abuse", o.get("title") or "")
                    offers.append({
                        "platform": "g2g",
                        "offer_id": oid,
                        "title":    title,
                        "price":    o.get("price"),
                        "category": "Roblox",
                    })
                    new_count += 1
                if new_count == 0:
                    break
    except Exception as e:
        _chrome_dlog("scrape g2g: EXCEPTION",
                     err=str(e)[:200],
                     accumulated=len(offers),
                     duration_s=int(time.time() - _t0))
        return offers, f"G2G: {str(e)[:160]}"
    _chrome_dlog("scrape g2g: done",
                 offers=len(offers),
                 duration_s=int(time.time() - _t0))
    return offers, None


def _g2g_fetch_live_offers(max_pages=15):
    """Live offers for G2G — JSON API only (sls.g2g.com my_offers). Chrome is
    used solely to harvest the accessToken; no Selenium render. On API failure
    returns ([], reason) and callers keep the previously-cached offers. The
    legacy Selenium scraper (_g2g_scrape_offers_inner) is retained as a manual
    escape hatch but is no longer called automatically."""
    return _g2g_fetch_live_offers_api()


def _refresh_live_offers(platform):
    """Refresh one platform's offer cache. Logs result via _auto_log.

    For Chrome-driven platforms, retries once if the first attempt hits a
    transient Chrome-died error (invalid session id / DevTools disconnected).
    chrome_session() already nulls the dead driver after an exception, so the
    retry transparently builds a fresh Chrome."""
    t0 = time.time()
    fp_accounts = funpay_account_paths()
    fp_map = {label: cookie for label, cookie in fp_accounts}

    if platform in fp_map:
        offers, err = _funpay_fetch_live_offers(platform, fp_map[platform])
    elif platform == "u7buy":
        offers, err = _u7buy_fetch_live_offers()
    elif platform == "eldorado":
        if _is_chrome_frozen():
            return False, "Chrome frozen — unfreeze to refresh Eldorado"
        offers, err = _eldorado_fetch_live_offers()
        if err and _is_dead_chrome_error(err):
            _auto_log("Live offers [eldorado]: Chrome session died, retrying with fresh driver…")
            time.sleep(2)
            offers, err = _eldorado_fetch_live_offers()
    elif platform == "g2g":
        if _is_chrome_frozen():
            return False, "Chrome frozen — unfreeze to refresh G2G"
        offers, err = _g2g_fetch_live_offers()
        if err and _is_dead_chrome_error(err):
            _auto_log("Live offers [g2g]: Chrome session died, retrying with fresh driver…")
            time.sleep(2)
            offers, err = _g2g_fetch_live_offers()
    else:
        return False, f"unknown platform: {platform}"
    duration_ms = int((time.time() - t0) * 1000)
    if not err:
        # Bump per-offer live-time counter for everything we just saw.
        # Must happen BEFORE we cache the offers so the API returns the
        # decorated values.
        _update_offer_live_history(platform, offers)
    with _live_offers_lock:
        prev = _live_offers_cache.get(platform, {})
        if err:
            # Failure path: KEEP the previous successful scrape so the user
            # doesn't lose visibility of what was live before this attempt.
            # Mark stale=True so the UI can flag the freshness as suspect.
            _live_offers_cache[platform] = {
                "offers":      prev.get("offers", []),
                "count":       prev.get("count", 0),
                "updated_ts":  prev.get("updated_ts"),     # don't bump on failure
                "duration_ms": duration_ms,
                "error":       err,
                "error_ts":    time.time(),
                "stale":       bool(prev.get("offers")),
            }
        else:
            _live_offers_cache[platform] = {
                "offers":      offers,
                "count":       len(offers),
                "updated_ts":  time.time(),
                "duration_ms": duration_ms,
                "error":       None,
                "error_ts":    None,
                "stale":       False,
            }
    if err:
        kept = " (kept previous " + str(prev.get("count", 0)) + " offers)" if prev.get("offers") else ""
        _auto_log(f"Live offers [{platform}]: {err}{kept}")
    else:
        _auto_log(f"Live offers [{platform}]: {len(offers)} offers in {duration_ms}ms")
    # Persist after every refresh so a Flask restart preserves the last view.
    _save_live_offers_cache()
    return (err is None), err or f"{len(offers)} offers"


def _live_offers_http_loop():
    """Background loop: refresh cheap HTTP platforms (funpay*, u7buy) every
    LIVE_OFFERS_HTTP_INTERVAL seconds."""
    time.sleep(15)   # let startup settle
    while True:
        try:
            http_platforms = ["u7buy"] + [label for label, _ in funpay_account_paths()]
            for plat in http_platforms:
                try:
                    _refresh_live_offers(plat)
                except Exception as e:
                    _auto_log(f"Live offers [{plat}] loop error: {str(e)[:100]}")
        except Exception:
            pass
        time.sleep(LIVE_OFFERS_HTTP_INTERVAL)


# Live-offer Chrome scraping is now folded into _chrome_presence_loop below
# so the seller-online tabs and the offer-list scrape share the SAME long-
# lived Chrome driver. Avoids re-creating Chrome every 30 min (the old
# separate _live_offers_chrome_loop would trigger _kill_orphan_automation_chrome
# which sometimes killed the presence Chrome too).
def _record_live_offers_inline(platform, offers, err, duration_ms):
    """Helper for the presence loop: write the offer-scrape result into the
    same cache slot used by _refresh_live_offers. Preserves prior data on
    error and persists to disk."""
    if not err:
        _update_offer_live_history(platform, offers)
    with _live_offers_lock:
        prev = _live_offers_cache.get(platform, {})
        if err:
            _live_offers_cache[platform] = {
                "offers":      prev.get("offers", []),
                "count":       prev.get("count", 0),
                "updated_ts":  prev.get("updated_ts"),
                "duration_ms": duration_ms,
                "error":       err,
                "error_ts":    time.time(),
                "stale":       bool(prev.get("offers")),
            }
        else:
            _live_offers_cache[platform] = {
                "offers":      offers,
                "count":       len(offers),
                "updated_ts":  time.time(),
                "duration_ms": duration_ms,
                "error":       None,
                "error_ts":    None,
                "stale":       False,
            }
    if err:
        kept = " (kept previous " + str(prev.get("count", 0)) + " offers)" if prev.get("offers") else ""
        _auto_log(f"Live offers [{platform}] (inline): {err}{kept}")
    else:
        _auto_log(f"Live offers [{platform}] (inline): {len(offers)} offers in {duration_ms}ms")
    _save_live_offers_cache()


@app.route("/api/offers/live")
def api_offers_live():
    """Return the cached live-offer snapshot for every platform.

    Recomputes `live_seconds` on each serve so the value reflects true
    wall-clock elapsed time since the offer was first seen — not the value
    cached at the last scrape (which would freeze)."""
    out = {}
    with _live_offers_lock:
        for plat, entry in _live_offers_cache.items():
            offers = entry.get("offers", [])
            _decorate_offers_with_live_time(plat, offers)
            out[plat] = {
                "count":       entry.get("count", 0),
                "updated_ts":  entry.get("updated_ts"),
                "duration_ms": entry.get("duration_ms"),
                "error":       entry.get("error"),
                "error_ts":    entry.get("error_ts"),
                "stale":       bool(entry.get("stale")),
                "offers":      offers,
            }
    # Pre-populate platform keys we know about so the UI can render zero-rows
    fp_labels = [label for label, _ in funpay_account_paths()]
    for plat in fp_labels + ["u7buy", "eldorado", "g2g"]:
        out.setdefault(plat, {"count": 0, "updated_ts": None,
                              "duration_ms": None, "error": None,
                              "error_ts": None, "stale": False, "offers": []})
    return jsonify(out)


# ─── Detailed live offers (full enrichment + filters + aggregate stats) ────
# Mirrors /api/sales/all in structure: same query params syntax, same text/
# json output choice, same "filters compose" behaviour.
#
# Per-offer enrichment (over the bare /api/offers/live output):
#   - section + section_label   (Pet-first / Egg-first / Sailor / /70 Pet / etc)
#   - platform_label            (FunPay 2 instead of funpay2, etc)
#   - live_time_human           ("3h 45m", "2d 4h", "1m 12s")
#   - first_seen_ts / first_seen_iso  (when we first saw this offer)
#   - last_seen_ts  / last_seen_iso   (last successful scrape that included it)
#
# Aggregate stats included at top level:
#   - total_count, total_value_usd
#   - by_platform: count + total_value + avg_price + oldest/newest live_seconds
#   - by_section:  count + total_value
#   - oldest / newest first_seen_ts across the whole filtered set


PLATFORM_DISPLAY_LABELS = {
    "funpay":          "FunPay",
    "funpay2":         "FunPay 2",
    "funpay3":         "FunPay 3",
    "u7buy":           "u7buy",
    "eldorado":        "Eldorado",
    "g2g":             "G2G",
    "playerauctions":  "PlayerAuctions",
}
SECTION_DISPLAY_LABELS = {
    "pet":          "Pet-first",
    "egg":          "Egg-first",
    "sailor":       "Sailor Piece",
    "roblox70-pet": "/70 Roblox · Pet",
    "roblox70-egg": "/70 Roblox · Egg",
    "roblox70":     "/70 Roblox · Other",
    "other":        "Other",
}

_SAILOR_RE      = re.compile(r"⚔️|Sailor\s*Piece|Ascend|Race", re.I)
_ROBLOX70_RE    = re.compile(r"^Adopt\s+me\s*-\s*(.*)$", re.I | re.S)
_PET_FIRST_RE   = re.compile(r"🐾|^\s*\d+\s*Pets\b|^Pets\b|🧪", re.I)
_EGG_FIRST_RE   = re.compile(r"👑|🥚|Admin\s*Abuse|Endangered", re.I)
_PIPE_SPLIT_RE  = re.compile(r"\s*[|•·]\s*")


def _classify_offer_section(title):
    """Python port of classifyOfferSection in app.js — same buckets, same
    precedence rules. Pet-first / egg-first detection runs on either the
    full title or the /70-Roblox sub-body after stripping the prefix."""
    t = (title or "").strip()
    if not t:
        return "other"
    if _SAILOR_RE.search(t):
        return "sailor"
    m70 = _ROBLOX70_RE.match(t)
    if m70:
        body = m70.group(1) or ""
        sub = _classify_body(body)
        if sub == "pet":
            return "roblox70-pet"
        if sub == "egg":
            return "roblox70-egg"
        return "roblox70"
    sub = _classify_body(t)
    return sub or "other"


def _classify_body(body):
    first = (_PIPE_SPLIT_RE.split(body or "", 1) or [body])[0].strip()
    if _PET_FIRST_RE.search(first):
        return "pet"
    if _EGG_FIRST_RE.search(first):
        return "egg"
    # Fallback: whole-text scan
    if _EGG_FIRST_RE.search(body):
        return "egg"
    if re.search(r"🐾|Pets\b", body, re.I):
        return "pet"
    return None


def _fmt_duration_human(sec):
    """Match the JS _fmtDuration formatter — produces 'Xd Yh' / 'Xh Ym' /
    'Xm' / 'Xs' depending on magnitude. Returns 'never' for None / <= 0."""
    if sec is None or sec <= 0:
        return "0s"
    sec = int(sec)
    d = sec // 86400
    h = (sec % 86400) // 3600
    m = (sec % 3600) // 60
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m" if m else f"{h}h 0m"
    if m > 0:
        return f"{m}m"
    return f"{sec}s"


@app.route("/api/offers/live/detail")
def api_offers_live_detail():
    """Detailed live-offer listing with full per-offer enrichment + filters
    + aggregate stats. Filters compose.

    Query params:
      platform=KEY            filter to one platform (funpay, eldorado, ...)
      section=KEY             filter to one section (pet, egg, sailor, roblox70-pet, ...)
      grep=KW                 case-insensitive substring on title (and offer_id)
      min_price=N             only offers with price >= N
      max_price=N             only offers with price <= N
      min_live_hours=N        only offers live for at least N hours
      max_live_hours=N        only offers live for at most N hours
      sort_by=KEY             live_seconds | price | first_seen_ts | platform | title
                              (default: live_seconds)
      sort_dir=asc|desc       default desc (older / pricier first)
      limit=N                 cap output (default 5000, max 50000)
      format=json|text        json is the full structure; text is tab-separated
    """
    # ── Parse filters ────────────────────────────────────────────
    platform_filter = request.args.get("platform") or None
    section_filter  = request.args.get("section") or None
    grep_filter     = (request.args.get("grep") or "").lower().strip() or None
    try:    min_price = float(request.args.get("min_price")) if request.args.get("min_price") else None
    except Exception: min_price = None
    try:    max_price = float(request.args.get("max_price")) if request.args.get("max_price") else None
    except Exception: max_price = None
    try:    min_live_h = float(request.args.get("min_live_hours")) if request.args.get("min_live_hours") else None
    except Exception: min_live_h = None
    try:    max_live_h = float(request.args.get("max_live_hours")) if request.args.get("max_live_hours") else None
    except Exception: max_live_h = None
    sort_by   = (request.args.get("sort_by") or "live_seconds").lower()
    sort_dir  = (request.args.get("sort_dir") or "desc").lower()
    try:    limit = int(request.args.get("limit") or "5000")
    except Exception: limit = 5000
    limit = max(1, min(limit, 50000))
    fmt = (request.args.get("format") or "json").lower()

    now = time.time()
    out_offers = []
    platforms_meta = {}

    # ── Walk the cache, enriching each offer ─────────────────────
    with _live_offers_lock:
        cache_items = list(_live_offers_cache.items())
    with _offer_live_history_lock:
        history_snapshot = dict(_offer_live_history)
    for plat, entry in cache_items:
        platforms_meta[plat] = {
            "updated_ts":  entry.get("updated_ts"),
            "duration_ms": entry.get("duration_ms"),
            "error":       entry.get("error"),
            "stale":       bool(entry.get("stale")),
        }
        if platform_filter and plat != platform_filter:
            continue
        for o in entry.get("offers") or []:
            oid = o.get("offer_id") or ""
            hist = history_snapshot.get(f"{plat}:{oid}") if oid else None
            first_seen_ts = hist.get("first_seen_ts") if hist else None
            last_seen_ts  = hist.get("last_seen_ts")  if hist else None
            scrape_count  = hist.get("scrape_count", 0) if hist else 0
            live_seconds  = max(0, int(now - first_seen_ts)) if first_seen_ts else 0
            section = (o.get("category") or "").strip() or "Uncategorized"
            if section_filter and section != section_filter:
                continue
            if grep_filter:
                hay = (o.get("title") or "").lower() + " " + oid.lower()
                if grep_filter not in hay:
                    continue
            price = o.get("price")
            if min_price is not None and (price is None or price < min_price):
                continue
            if max_price is not None and (price is None or price > max_price):
                continue
            live_hours = live_seconds / 3600.0
            if min_live_h is not None and live_hours < min_live_h:
                continue
            if max_live_h is not None and live_hours > max_live_h:
                continue
            out_offers.append({
                "platform":         plat,
                "platform_label":   PLATFORM_DISPLAY_LABELS.get(plat, plat),
                "offer_id":         oid,
                "title":            o.get("title") or "",
                "price":            price,
                "url":              o.get("url"),
                "paused":           o.get("paused"),
                "section":          section,
                "section_label":    section,
                "live_seconds":     live_seconds,
                "live_time_human":  _fmt_duration_human(live_seconds),
                "scrape_count":     scrape_count,
                "first_seen_ts":    first_seen_ts,
                "first_seen_iso":   (datetime.fromtimestamp(first_seen_ts).isoformat()
                                     if first_seen_ts else None),
                "last_seen_ts":     last_seen_ts,
                "last_seen_iso":    (datetime.fromtimestamp(last_seen_ts).isoformat()
                                     if last_seen_ts else None),
            })

    # ── Sort ─────────────────────────────────────────────────────
    sort_dir_mult = -1 if sort_dir == "desc" else 1
    def _sort_key(o):
        v = o.get(sort_by)
        # None goes to the end regardless of direction
        if v is None or v == "":
            return (1, 0)
        if isinstance(v, (int, float)):
            return (0, sort_dir_mult * v)
        return (0, sort_dir_mult * 0, str(v).lower() if sort_dir_mult == 1 else "")
    try:
        if sort_by in ("live_seconds", "price", "first_seen_ts", "last_seen_ts", "scrape_count"):
            out_offers.sort(
                key=lambda o: (o.get(sort_by) is None, o.get(sort_by) or 0),
                reverse=(sort_dir != "asc"),
            )
        else:
            out_offers.sort(
                key=lambda o: (o.get(sort_by) is None, (o.get(sort_by) or "").lower()
                               if isinstance(o.get(sort_by), str) else 0),
                reverse=(sort_dir != "asc"),
            )
    except Exception:
        pass

    # ── Aggregate stats (over the matched set BEFORE the cap, so the
    #    totals reflect everything the filter found regardless of limit) ──
    total_matched = len(out_offers)
    by_platform = {}
    by_section  = {}
    total_value = 0.0
    oldest_first_seen = None
    newest_first_seen = None
    for o in out_offers:
        p = o["platform"]; s = o["section"]
        price = o.get("price") or 0.0
        live = o.get("live_seconds") or 0
        bp = by_platform.setdefault(p, {
            "count": 0, "total_value": 0.0, "max_live_seconds": 0,
            "min_live_seconds": None,
        })
        bp["count"] += 1
        bp["total_value"] += price
        bp["max_live_seconds"] = max(bp["max_live_seconds"], live)
        bp["min_live_seconds"] = live if bp["min_live_seconds"] is None else min(bp["min_live_seconds"], live)
        bs = by_section.setdefault(s, {"count": 0, "total_value": 0.0})
        bs["count"] += 1
        bs["total_value"] += price
        total_value += price
        fs = o.get("first_seen_ts")
        if fs:
            if oldest_first_seen is None or fs < oldest_first_seen:
                oldest_first_seen = fs
            if newest_first_seen is None or fs > newest_first_seen:
                newest_first_seen = fs
    # Round + decorate by-platform with avg_price
    for p, bp in by_platform.items():
        bp["total_value"]      = round(bp["total_value"], 2)
        bp["avg_price"]        = round(bp["total_value"] / bp["count"], 2) if bp["count"] else 0.0
        bp["max_live_human"]   = _fmt_duration_human(bp["max_live_seconds"])
        bp["min_live_human"]   = _fmt_duration_human(bp["min_live_seconds"] or 0)
    for s, bs in by_section.items():
        bs["total_value"] = round(bs["total_value"], 2)

    # Apply cap NOW so the stats above reflect the full filtered set
    out_offers = out_offers[:limit]

    body = {
        "filters": {
            "platform":       platform_filter,
            "section":        section_filter,
            "grep":           grep_filter,
            "min_price":      min_price,
            "max_price":      max_price,
            "min_live_hours": min_live_h,
            "max_live_hours": max_live_h,
            "sort_by":        sort_by,
            "sort_dir":       sort_dir,
            "limit":          limit,
        },
        "stats": {
            "total_count":           total_matched,
            "returned_count":        len(out_offers),
            "total_value_usd":       round(total_value, 2),
            "oldest_first_seen_ts":  oldest_first_seen,
            "oldest_first_seen_iso": (datetime.fromtimestamp(oldest_first_seen).isoformat()
                                      if oldest_first_seen else None),
            "newest_first_seen_ts":  newest_first_seen,
            "newest_first_seen_iso": (datetime.fromtimestamp(newest_first_seen).isoformat()
                                      if newest_first_seen else None),
            "by_platform":           by_platform,
            "by_section":            by_section,
        },
        "platforms": platforms_meta,
        "offers":   out_offers,
    }

    if fmt == "json":
        return jsonify(body)

    # Text format — tab-separated, one row per offer, no nested stats.
    # Header line so grep/awk consumers know which column is which.
    lines = ["platform\toffer_id\ttitle\tprice\tlive_time\tscrape_count\tfirst_seen_iso\tsection"]
    for o in out_offers:
        price = "" if o.get("price") is None else f"{o['price']:.2f}"
        title = (o.get("title") or "").replace("\t", " ").replace("\n", " ")
        lines.append(
            f"{o['platform']}\t"
            f"{o.get('offer_id','')}\t"
            f"{title}\t"
            f"{price}\t"
            f"{o.get('live_time_human','')}\t"
            f"{o.get('scrape_count','')}\t"
            f"{o.get('first_seen_iso','')}\t"
            f"{o.get('section','')}"
        )
    return Response("\n".join(lines) + "\n", mimetype="text/plain; charset=utf-8")


# ─── Backup inventory + recovery (offers cache & live-time history) ────────
def _enumerate_backups_for(path, label):
    """Return [{filename, path, size, mtime, source}] for every backup we
    could load `path` from — including the file itself, rotating versions,
    and dated snapshots."""
    out = []
    def _info(p, source):
        try:
            return {
                "filename": os.path.basename(p),
                "path":     p,
                "size":     os.path.getsize(p),
                "mtime":    os.path.getmtime(p),
                "source":   source,
            }
        except Exception:
            return None
    for p, src in [(path, "current")] + [
        (f"{path}.{i}", f"rotating .{i}") for i in range(1, LIVE_OFFERS_BACKUP_ROTATING + 1)
    ]:
        if os.path.exists(p):
            info = _info(p, src)
            if info: out.append(info)
    try:
        # Match exactly "<label>_YYYY-MM-DD.json" so "_live_offers_" doesn't
        # incorrectly grab "_live_offers_history_*.json".
        import re
        pattern = re.compile(r"^" + re.escape(label) + r"_\d{4}-\d{2}-\d{2}\.json$")
        for f in sorted(os.listdir(LIVE_OFFERS_BACKUP_DIR), reverse=True):
            if pattern.match(f):
                info = _info(os.path.join(LIVE_OFFERS_BACKUP_DIR, f),
                             f"snapshot {f}")
                if info: out.append(info)
    except Exception:
        pass
    return out


@app.route("/api/offers/sidebar")
def api_offers_sidebar():
    """Offers-sidebar tracker — live offers grouped by category exactly as the
    Offers sidebar groups them. Per category: count, % of all offers, total
    value, per-platform split, and how long its oldest offer has been live.
    A pollable snapshot for tracking the sidebar.   ?platform=KEY limits scope."""
    platform_filter = request.args.get("platform") or None
    now = time.time()
    with _live_offers_lock:
        cache_items = list(_live_offers_cache.items())
    with _offer_live_history_lock:
        history = dict(_offer_live_history)
    cats, platforms_meta, total_offers, total_value = {}, {}, 0, 0.0
    for plat, entry in cache_items:
        offers = entry.get("offers") or []
        platforms_meta[plat] = {
            "updated_ts": entry.get("updated_ts"),
            "updated_iso": (datetime.fromtimestamp(entry["updated_ts"]).isoformat()
                            if entry.get("updated_ts") else None),
            "error": entry.get("error"),
            "stale": bool(entry.get("stale")),
            "count": len(offers),
        }
        if platform_filter and plat != platform_filter:
            continue
        for o in offers:
            oid = o.get("offer_id") or ""
            cat = (o.get("category") or "").strip() or "Uncategorized"
            price = o.get("price") or 0.0
            hist = history.get(f"{plat}:{oid}") if oid else None
            fs = hist.get("first_seen_ts") if hist else None
            live = max(0, int(now - fs)) if fs else 0
            c = cats.setdefault(cat, {"category": cat, "count": 0, "total_value": 0.0,
                                      "platforms": {}, "max_live_seconds": 0,
                                      "oldest_first_seen": None})
            c["count"] += 1
            c["total_value"] += price
            if live > c["max_live_seconds"]:
                c["max_live_seconds"] = live
            if fs and (c["oldest_first_seen"] is None or fs < c["oldest_first_seen"]):
                c["oldest_first_seen"] = fs
            pp = c["platforms"].setdefault(plat, {"count": 0, "value": 0.0})
            pp["count"] += 1
            pp["value"] = round(pp["value"] + price, 2)
            total_offers += 1
            total_value += price
    out = []
    for cat, c in sorted(cats.items(), key=lambda kv: -kv[1]["count"]):
        c["total_value"] = round(c["total_value"], 2)
        c["pct"] = round(c["count"] / total_offers * 100, 1) if total_offers else 0
        c["oldest_live_human"] = _fmt_duration_human(c["max_live_seconds"])
        c["oldest_first_seen_iso"] = (datetime.fromtimestamp(c["oldest_first_seen"]).isoformat()
                                      if c["oldest_first_seen"] else None)
        out.append(c)
    return jsonify({
        "categories": out, "category_count": len(out),
        "total_offers": total_offers, "total_value": round(total_value, 2),
        "platforms": platforms_meta, "generated_at": datetime.now().isoformat(),
    })


@app.route("/api/offers/backup")
def api_offers_backup_list():
    """List every recoverable backup file for the offers cache + live-time
    history. Useful before calling restore — shows mtime, size, source."""
    return jsonify({
        "live_offers_cache": _enumerate_backups_for(LIVE_OFFERS_CACHE_FILE, "_live_offers"),
        "live_time_history": _enumerate_backups_for(LIVE_OFFERS_HISTORY_FILE, "_live_offers_history"),
        "config": {
            "rotating_backups": LIVE_OFFERS_BACKUP_ROTATING,
            "snapshot_retention_days": LIVE_OFFERS_BACKUP_DAYS,
            "backup_dir": LIVE_OFFERS_BACKUP_DIR,
        },
    })


@app.route("/api/offers/backup/restore", methods=["POST"])
def api_offers_backup_restore():
    """Restore offers cache and/or live-time history from a chosen backup
    file. Body: {"target": "live_offers_cache" | "live_time_history",
                 "filename": "_live_offers.json.2"}
    The filename can be a rotating backup (.1 .. .N) or a dated snapshot
    (_live_offers_2026-05-20.json). The current primary file is rotated
    out (becomes .1) before the chosen backup overwrites it, so the
    restore is itself reversible."""
    body = request.get_json(silent=True) or {}
    target = body.get("target")
    filename = body.get("filename")
    if target == "live_offers_cache":
        primary = LIVE_OFFERS_CACHE_FILE
        label   = "_live_offers"
    elif target == "live_time_history":
        primary = LIVE_OFFERS_HISTORY_FILE
        label   = "_live_offers_history"
    else:
        return jsonify({"ok": False, "message": "target must be live_offers_cache or live_time_history"}), 400
    if not filename:
        return jsonify({"ok": False, "message": "filename required"}), 400

    # Resolve the chosen backup. Try rotating first, then snapshot dir.
    candidates = [
        os.path.join(os.path.dirname(primary), filename),
        os.path.join(LIVE_OFFERS_BACKUP_DIR, filename),
    ]
    src = next((c for c in candidates if os.path.exists(c)), None)
    if not src:
        return jsonify({"ok": False, "message": f"backup not found: {filename}"}), 404

    # Validate the candidate parses as JSON before touching anything live
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "message": "backup is not a JSON object"}), 400
    except Exception as e:
        return jsonify({"ok": False, "message": f"backup not parseable: {str(e)[:120]}"}), 400

    # Rotate the current primary out (becomes .1), then copy the chosen
    # backup into place. Use the rotating-write helper so the chain stays
    # consistent.
    try:
        _atomic_write_with_rotation(primary, json.dumps(data))
    except Exception as e:
        return jsonify({"ok": False, "message": f"write failed: {str(e)[:120]}"}), 500

    # Hot-reload the in-memory state so the restore takes effect immediately
    if target == "live_offers_cache":
        with _live_offers_lock:
            _live_offers_cache.clear()
        _load_live_offers_cache()
    else:
        with _offer_live_history_lock:
            _offer_live_history.clear()
        _load_offer_live_history()

    _auto_log(f"Live offers: restored {target} from {filename}")
    return jsonify({"ok": True, "restored_from": filename, "target": target})


# ─── Clear cached offers (lets the user discard stale data after a failure) ─
@app.route("/api/offers/live/clear", methods=["POST"])
def api_offers_live_clear_all():
    cleared = []
    with _live_offers_lock:
        for plat in list(_live_offers_cache.keys()):
            _live_offers_cache[plat] = {
                "offers": [], "count": 0, "updated_ts": None,
                "duration_ms": None, "error": None, "error_ts": None, "stale": False,
            }
            cleared.append(plat)
    # Also wipe the per-offer live-time history — fresh start means counters reset.
    with _offer_live_history_lock:
        _offer_live_history.clear()
    _save_offer_live_history()
    _auto_log(f"Live offers: cleared {', '.join(cleared) or 'nothing'} (including live-time history)")
    _save_live_offers_cache()
    return jsonify({"ok": True, "cleared": cleared})


@app.route("/api/offers/live/clear/<platform>", methods=["POST"])
def api_offers_live_clear_one(platform):
    with _live_offers_lock:
        _live_offers_cache[platform] = {
            "offers": [], "count": 0, "updated_ts": None,
            "duration_ms": None, "error": None, "error_ts": None, "stale": False,
        }
    # Drop just this platform's live-time history entries.
    prefix = platform + ":"
    with _offer_live_history_lock:
        drop = [k for k in _offer_live_history if k.startswith(prefix)]
        for k in drop:
            del _offer_live_history[k]
    _save_offer_live_history()
    _auto_log(f"Live offers: cleared {platform} (and {len(drop)} live-time entries)")
    _save_live_offers_cache()
    return jsonify({"ok": True, "cleared": platform})


@app.route("/api/offers/live/refresh", methods=["POST"])
def api_offers_live_refresh_all():
    """Refresh cheap HTTP platforms synchronously, kick Chrome ones in the
    background (so the request returns fast)."""
    results = {}
    for plat in ["u7buy"] + [label for label, _ in funpay_account_paths()]:
        ok, msg = _refresh_live_offers(plat)
        results[plat] = {"ok": ok, "message": msg}
    if not _is_chrome_frozen():
        threading.Thread(target=_refresh_live_offers, args=("eldorado",), daemon=True).start()
        threading.Thread(target=_refresh_live_offers, args=("g2g",), daemon=True).start()
        results["eldorado"] = {"ok": True, "message": "refresh started (background)"}
        results["g2g"]      = {"ok": True, "message": "refresh started (background)"}
    else:
        results["eldorado"] = {"ok": False, "message": "Chrome frozen"}
        results["g2g"]      = {"ok": False, "message": "Chrome frozen"}
    return jsonify(results)


@app.route("/api/offers/diag/eldorado")
def api_offers_diag_eldorado():
    """One-shot DOM diagnostic — visits the dashboard, returns the top
    classes / element counts so we can pick the right offer selector
    without guessing. Safe to remove once the scraper is dialed in."""
    if _is_chrome_frozen():
        return jsonify({"error": "Chrome frozen"}), 409
    target = request.args.get("path") or "/dashboard/offers/Accounts?pageIndex=1"
    try:
        with chrome_session(page_load_timeout=60) as drv:
            drv.get("https://www.eldorado.gg" + target)
            # Wait for the skeleton-loader state to clear. Eldorado's new
            # offers page shows 25+ skeleton-loaders while data fetches.
            # Poll up to 90 s waiting for skeletons to clear (logging progress
            # so we can see whether it's making any progress at all).
            from selenium.webdriver.common.by import By
            wait_log = []
            deadline = time.time() + 90
            last_skel = -1
            last_offer = -1
            while time.time() < deadline:
                try:
                    state = drv.execute_script(
                        "return { skel: document.querySelectorAll('.skeleton-loader').length, "
                        "offer_list: document.querySelectorAll('.offer-list-item').length, "
                        "offer_new: document.querySelectorAll('.offer').length, "
                        "body_len: (document.body.innerText || '').length };"
                    )
                    if state.get('skel') != last_skel or state.get('offer_new') != last_offer:
                        wait_log.append({
                            'at': int(time.time() - (deadline - 90)),
                            'skel': state.get('skel'),
                            'offer_list': state.get('offer_list'),
                            'offer_new': state.get('offer_new'),
                            'body_len': state.get('body_len'),
                        })
                        last_skel = state.get('skel')
                        last_offer = state.get('offer_new')
                    if state.get('skel') == 0 and (state.get('offer_list') > 0 or state.get('offer_new') > 0):
                        break
                except Exception:
                    pass
                time.sleep(2)
            time.sleep(2)  # final settle
            data = drv.execute_script(r"""
                var info = {
                    url:    location.href,
                    title:  document.title,
                    body_len: (document.body.innerText || '').length,
                    body_head: (document.body.innerText || '').replace(/\s+/g,' ').slice(0, 400),
                };
                info.wait_log = arguments[0] || [];
                // Counts for the selectors we've tried so far
                info.candidate_counts = {};
                var candidates = ['.offer-list-item','[data-offer-id]','.offer-row',
                                  "div[class*='offer-list']", '.dashboard-offer-card',
                                  '.offer', '.offer__top', '.offer__bottom',
                                  '.offer-details', '.offer-quantity', '.skeleton-loader',
                                  'tr[data-offer-id]', '.MuiTableRow-root', 'tr',
                                  '[class*="Offer"]', '[class*="ListItem"]',
                                  'a[href*="/dashboard/offers/edit/"]',
                                  'a[href*="/oa/"]',
                                  '[class*="card"]'];
                candidates.forEach(function(s){
                    try { info.candidate_counts[s] = document.querySelectorAll(s).length; }
                    catch(e) { info.candidate_counts[s] = 'ERR'; }
                });
                // Top 30 most common class names on the page
                var classCounts = {};
                document.querySelectorAll('[class]').forEach(function(el){
                    (el.className.toString().split(/\s+/) || []).forEach(function(c){
                        if (!c) return;
                        classCounts[c] = (classCounts[c]||0) + 1;
                    });
                });
                var sorted = Object.entries(classCounts).sort(function(a,b){return b[1]-a[1];}).slice(0, 30);
                info.top_classes = sorted;
                // First anchor that points at an offer-edit URL — useful proof of life
                var sampleA = document.querySelector('a[href*="/dashboard/offers/edit/"]');
                if (sampleA) {
                    var row = sampleA.closest('div,tr,li');
                    info.sample_anchor_href = sampleA.getAttribute('href');
                    info.sample_row_classes = row ? (row.className || '') : '';
                    info.sample_row_html_len = row ? (row.outerHTML || '').length : 0;
                    info.sample_row_snippet = row ? (row.textContent||'').replace(/\s+/g,' ').slice(0,250) : '';
                }
                // Also grab first .offer element (new DOM) — its HTML structure
                var firstOLI = document.querySelector('.offer-list-item') ||
                               document.querySelector('.offer');
                if (firstOLI) {
                    info.first_oli_class = firstOLI.className || '';
                    info.first_oli_outerhtml = (firstOLI.outerHTML || '').slice(0, 8000);
                    info.first_oli_text = (firstOLI.textContent || '').replace(/\s+/g,' ').slice(0, 800);
                    // Hunt for price-looking elements
                    var priceCandidates = [];
                    firstOLI.querySelectorAll('input, [class*="price"], [class*="Price"]').forEach(function(el){
                        priceCandidates.push({
                            tag: el.tagName, cls: (el.className || '').toString().slice(0,80),
                            value: el.value, text: (el.textContent || '').trim().slice(0, 80),
                            placeholder: el.getAttribute('placeholder')
                        });
                    });
                    info.first_oli_price_candidates = priceCandidates;
                    // Try to find any anchor/button with id-looking text
                    var anchors = firstOLI.querySelectorAll('a, button');
                    info.first_oli_anchor_attrs = [];
                    anchors.forEach(function(a){
                        info.first_oli_anchor_attrs.push({
                            tag: a.tagName,
                            href: a.getAttribute('href'),
                            'aria-label': a.getAttribute('aria-label'),
                            'data-test': a.getAttribute('data-test'),
                            'data-id': a.getAttribute('data-id'),
                        });
                    });
                }
                return JSON.stringify(info);
            """, wait_log)
        return Response(data, mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500


@app.route("/api/offers/live/refresh/<platform>", methods=["POST"])
def api_offers_live_refresh_one(platform):
    """Refresh one platform on demand."""
    # Chrome platforms run in background since each takes 30+ seconds
    if platform in ("eldorado", "g2g"):
        if _is_chrome_frozen():
            return jsonify({"ok": False, "message": "Chrome frozen"}), 409
        threading.Thread(target=_refresh_live_offers, args=(platform,), daemon=True).start()
        return jsonify({"ok": True, "message": f"{platform} refresh started in background"})
    ok, msg = _refresh_live_offers(platform)
    return jsonify({"ok": ok, "message": msg})


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    _load_live_offers_cache()   # restore prior offer snapshots from disk
    _load_offer_live_history()  # restore per-offer live-time counters

    def _startup_sync():
        time.sleep(3)
        _auto_log("Startup sync starting...")
        for plat, fn in [("funpay", _sync_funpay_sales), ("u7buy", _sync_u7buy_sales),
                         ("eldorado", _sync_eldorado_sales), ("g2g", _sync_g2g_sales)]:
            try:
                n, msg = fn()
                _auto_log(f"Startup {plat}: {n} new sales ({msg})")
            except Exception as e:
                _auto_log(f"Startup {plat} error: {str(e)[:60]}")
        # Tag any sales rows still missing a category (history predating the
        # column). Cheap no-op once everything is classified.
        try:
            tagged = _backfill_categories()
            if tagged:
                _auto_log(f"Startup category backfill: tagged {tagged} rows")
        except Exception as e:
            _auto_log(f"Startup category backfill error: {str(e)[:60]}")

    def _startup_chrome_platform_check():
        time.sleep(10)
        for name, fn in [("Eldorado", eldorado_logged_in),
                         ("G2G", g2g_logged_in),
                         ("PA", pa_logged_in)]:
            try:
                _auto_log(f"{name}: {'logged in' if fn() else 'NOT logged in (sign into Chrome Profile 3)'}")
            except Exception as e:
                _auto_log(f"{name} check error: {str(e)[:60]}")

    def _platform_status_loop():
        # Keeps _platform_status.json fresh so the automation's webhook reader
        # (which ignores snapshots older than 10 min) always has a usable value.
        # First refresh runs after the Chrome probe thread has had time to load.
        time.sleep(20)
        while True:
            try:
                _update_platform_status()
            except Exception:
                pass
            time.sleep(300)  # every 5 min

    def _trackstat_status_loop():
        time.sleep(15)
        _trackstat_load_seen()                   # restore the persisted tracked-game set
        try:
            _trackstat_refresh_catalog()        # discover the game catalog on startup
        except Exception:
            pass
        n = 0
        while True:
            try:
                _trackstat_refresh_status()
                _trackstat_refresh_active()      # (a) auto-detect tracked games
                n += 1
                if n % 18 == 0:                  # (b) ~every 6h, re-scrape for new games
                    _trackstat_refresh_catalog()
            except Exception:
                pass
            time.sleep(1200)  # every 20 min

    def _zp_solver_loop():
        # Auto-submit CAPTCHA-locked farm accounts to ZeroSolver every 20 min.
        # Honors the pause flag (set via /api/zpsolver/toggle); while paused it
        # still polls job status so the dashboard tile stays live.
        time.sleep(25)
        _zp_load_state()
        while True:
            try:
                if _zp_solver_paused():
                    _zp_refresh_jobs()
                else:
                    _zp_run_cycle()
            except Exception as e:
                try:
                    _zp_set_last(f"loop: {str(e)[:120]}")
                except Exception:
                    pass
            time.sleep(ZP_SOLVER_INTERVAL)

    threading.Thread(target=_startup_sync, daemon=True).start()
    threading.Thread(target=_startup_chrome_platform_check, daemon=True).start()
    threading.Thread(target=_funpay_boost_loop, daemon=True).start()
    threading.Thread(target=_funpay_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_chrome_presence_loop, daemon=True).start()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    threading.Thread(target=_platform_status_loop, daemon=True).start()
    threading.Thread(target=_trackstat_status_loop, daemon=True).start()
    threading.Thread(target=_zp_solver_loop, daemon=True).start()
    threading.Thread(target=_live_offers_http_loop, daemon=True).start()
    threading.Thread(target=_chrome_health_monitor_loop, daemon=True).start()
    # Chrome offers loop merged into _chrome_presence_loop (above) so the
    # presence Chrome session is reused — keeps the seller-online tabs
    # alive instead of taskkilling them every 30 min.

    # Spawn FarmSync Automation subprocess (writes shared state files we read).
    # Skip with FARMSYNC_AUTOSTART=0 env var.
    if FARMSYNC_AUTOMATION_AUTOSTART:
        def _spawn_farmsync_automation_delayed():
            time.sleep(8)
            ok, msg = farmsync_automation_start()
            _auto_log(f"FarmSync Automation autostart: {'OK' if ok else 'SKIP'} — {msg}")
        threading.Thread(target=_spawn_farmsync_automation_delayed, daemon=True).start()

    def _pick_clean_port(start=5000, end=5099):
        """Find a port that's not bound by a live HTTP server AND not held
        hostage by a Windows orphan socket (LISTENING entry whose owning
        process is gone — accepts connections but never replies).

        Strategy: try to TCP-connect to localhost:N first. If the connect
        succeeds at all, something owns the port (alive or ghost) — skip it.
        Only if the connect is refused do we bind."""
        import socket
        for p in range(start, end + 1):
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.settimeout(0.25)
            try:
                taken = probe.connect_ex(("127.0.0.1", p)) == 0
            except Exception:
                taken = False
            finally:
                try: probe.close()
                except Exception: pass
            if taken:
                continue
            binder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                binder.bind(("0.0.0.0", p))
                binder.close()
                return p
            except OSError:
                continue
            finally:
                try: binder.close()
                except Exception: pass
        raise RuntimeError(f"no clean port available in {start}-{end}")

    _env_port = os.environ.get("DASHBOARD_PORT")
    if _env_port:
        _port = int(_env_port)
    else:
        try:
            _port = _pick_clean_port()
        except RuntimeError:
            _port = 5000   # fall back to legacy behavior; user will see the hang
    print(f"\n  Revenue Dashboard running at http://localhost:{_port}\n")
    app.run(debug=True, host="0.0.0.0", port=_port, use_reloader=False)
