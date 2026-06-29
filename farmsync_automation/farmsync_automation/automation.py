"""
FarmSync Automation - Device Health Manager
Runs every 20 minutes. Each cycle:
  * All tasks require heartbeat <10min (tool alive). Dead tools are skipped.
  A. Auto-fix: enable all device accounts + assign correct config per group
  0. Pre-stock: if Potion/Pet Farm from_child folder is empty, stock 100 unassigned accs
  1. Disk full (>=95%) -> Restart Tool (reclone clears LDPlayer data)
  2. 0 online + heartbeat <10min -> clear all tasks, then Restart VPS
     (then ~1h grace before the next Restart VPS; after 3 -> attention + pause 3h)
     0 online + heartbeat stale (>10min) -> skip (tool dead)
  3. RAM >90% -> move excess accounts to folder matching group name
  4. RAM <90% AND online >90% -> fill accounts from group folder (max 5/device/cycle)
  5. Accounts <40% for 3 cycles or <60% for 6 cycles -> Relogin All
  6. Dead-folder cap: if a group's to_child ("dead cookie") folder exceeds that
     group's live on-device account total -> WARN + block any automated add to
     that folder (the folder is filled by hand in the FarmSync UI).
"""
import requests
import time
import os
import sys
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, load_api_key, headers

# --- Shared state for the Revenue Dashboard (so the website doesn't re-scrape) ---
_STATE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATE_DEVICES_FILE = os.path.join(_STATE_DIR, "_state_devices.json")
_STATE_ACCOUNTS_FILE = os.path.join(_STATE_DIR, "_state_accounts.json")
_PAUSE_FLAG_FILE = os.path.join(_STATE_DIR, "_paused.flag")

# Revenue Dashboard SQLite DB — for the webhook's revenue + new-sales summary.
# Path mirrors web/app.py's BASE_DIR + web/data.db; override via env var if your
# layout differs.
_REVENUE_DB_FILE = os.environ.get("REVENUE_DB_FILE") or \
    os.path.join(os.path.dirname(os.path.dirname(_STATE_DIR)), "web", "data.db")
_PLATFORM_KEYS = ("eldorado", "funpay", "funpay2", "u7buy", "g2g", "playerauctions")
_last_seen_sale_id = None  # set on first cycle to MAX(id); diff each cycle gives new sales


def _write_shared_state(devices, accounts):
    """Persist the cycle's fetched devices+accounts so the Revenue Dashboard
    can reuse them instead of hitting api.farmsync.cloud independently."""
    try:
        with open(_STATE_DEVICES_FILE, "w", encoding="utf-8") as f:
            json.dump(devices, f)
        with open(_STATE_ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f)
    except Exception:
        pass


def _is_paused():
    """Return True if the dashboard has paused us via _paused.flag."""
    return os.path.exists(_PAUSE_FLAG_FILE)


_PLATFORM_STATUS_FILE = os.path.join(_STATE_DIR, "_platform_status.json")


def _read_platform_status(max_age_sec=600):
    """Read the website's _platform_status.json snapshot if it's recent.
    Returns the dict or None if missing / stale / unreadable."""
    try:
        if not os.path.exists(_PLATFORM_STATUS_FILE):
            return None
        if time.time() - os.path.getmtime(_PLATFORM_STATUS_FILE) > max_age_sec:
            return None
        with open(_PLATFORM_STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _query_revenue_snapshot():
    """Read today's revenue per platform + total from the Revenue Dashboard DB.
    Returns ({platform: (revenue, count), ...}, total_rev, total_count) or
    (None, 0, 0) if the DB is missing or unreadable."""
    import sqlite3
    if not os.path.exists(_REVENUE_DB_FILE):
        return None, 0.0, 0
    try:
        conn = sqlite3.connect(_REVENUE_DB_FILE, timeout=2)
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = cur.execute(
            "SELECT platform, COALESCE(SUM(price),0), COUNT(*) FROM sales "
            "WHERE date(sold_at)=? GROUP BY platform",
            (today,)
        ).fetchall()
        per_platform = {k: (0.0, 0) for k in _PLATFORM_KEYS}
        for plat, rev, cnt in rows:
            per_platform[plat] = (float(rev or 0), int(cnt or 0))
        total = cur.execute(
            "SELECT COALESCE(SUM(price),0), COUNT(*) FROM sales WHERE date(sold_at)=?",
            (today,)
        ).fetchone()
        conn.close()
        return per_platform, float(total[0] or 0), int(total[1] or 0)
    except Exception:
        return None, 0.0, 0


def _query_category_revenue(limit=15):
    """Read today's revenue per category (game) from the Revenue Dashboard DB.
    Returns a list of (category, revenue, count) sorted by revenue desc, or
    None if the DB is missing/unreadable."""
    import sqlite3
    if not os.path.exists(_REVENUE_DB_FILE):
        return None
    try:
        conn = sqlite3.connect(_REVENUE_DB_FILE, timeout=2)
        cur = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = cur.execute(
            "SELECT COALESCE(NULLIF(TRIM(category),''),'(uncategorized)'), "
            "COALESCE(SUM(price),0), COUNT(*) FROM sales "
            "WHERE date(sold_at)=? GROUP BY 1 ORDER BY 2 DESC LIMIT ?",
            (today, limit)
        ).fetchall()
        conn.close()
        return [(str(c), float(r or 0), int(n or 0)) for c, r, n in rows]
    except Exception:
        return None


def _query_new_sales_since(since_id):
    """Return sales rows with id > since_id (most recent first), plus the new
    max id observed. Tuple-of-tuples for embed-friendly use."""
    import sqlite3
    if not os.path.exists(_REVENUE_DB_FILE):
        return [], since_id
    try:
        conn = sqlite3.connect(_REVENUE_DB_FILE, timeout=2)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, sold_at, username, platform, price FROM sales "
            "WHERE id > ? ORDER BY id DESC",
            (since_id or 0,)
        ).fetchall()
        max_id = cur.execute("SELECT COALESCE(MAX(id),0) FROM sales").fetchone()[0]
        conn.close()
        return rows, int(max_id or 0)
    except Exception:
        return [], since_id


def _query_yescaptcha_balance():
    """Fetch the current YesCaptcha balance via curl subprocess.
    Returns int points or None if the call fails. Curl bypasses the slow
    Python+Windows OCSP check that makes `requests` take 64s per call."""
    import subprocess
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(_STATE_DIR)), "yescapcha", "apikey.txt"),
        os.path.join(os.path.dirname(os.path.dirname(_STATE_DIR)), "yescapcha", "apikey.txt.txt"),
    ]
    key_file = next((p for p in candidates if os.path.exists(p)), None)
    if not key_file:
        return None
    try:
        with open(key_file, "r", encoding="utf-8") as f:
            key = f.readline().strip()
        if not key:
            return None
    except Exception:
        return None
    body = json.dumps({"clientKey": key})
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "10", "-X", "POST",
             "-H", "Content-Type: application/json",
             "-d", body,
             "https://api.yescaptcha.com/getBalance"],
            capture_output=True, timeout=12,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout.decode("utf-8"))
        if data.get("errorId") != 0:
            return None
        return int(data.get("balance") or 0)
    except Exception:
        return None


def _init_last_seen_sale_id():
    """On first cycle, set the baseline to current MAX(id) so we don't dump
    every historical sale into Discord on automation startup."""
    global _last_seen_sale_id
    if _last_seen_sale_id is not None:
        return
    import sqlite3
    try:
        if os.path.exists(_REVENUE_DB_FILE):
            conn = sqlite3.connect(_REVENUE_DB_FILE, timeout=2)
            row = conn.cursor().execute("SELECT COALESCE(MAX(id),0) FROM sales").fetchone()
            conn.close()
            _last_seen_sale_id = int(row[0] or 0)
        else:
            _last_seen_sale_id = 0
    except Exception:
        _last_seen_sale_id = 0


# --- Load config.json ---
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

CFG = load_config()
_auto = CFG.get("automation", {})
_rules = _auto.get("rules", {})
_restart = _auto.get("restart", {})
_stock = _auto.get("stock", {})

CYCLE_INTERVAL = _auto.get("cycle_interval_min", 20) * 60
RAM_HIGH = _rules.get("ram_high_pct", 95) / 100
RAM_TARGET = _rules.get("ram_target_pct", 92) / 100
ONLINE_WARN = _rules.get("online_warn_pct", 40) / 100
ONLINE_WARN_CYCLES = _rules.get("online_warn_cycles", 3)
ONLINE_LOW = _rules.get("online_low_pct", 60) / 100
ONLINE_LOW_CYCLES = _rules.get("online_low_cycles", 6)
DISK_FULL_PCT = _rules.get("disk_full_pct", 95) / 100
FILL_MAX_PER_CYCLE = _rules.get("fill_max_per_cycle", 10)
# Hard cap on a single MOVE ACCOUNTS action — prevents the planner from
# panic-shedding dozens of accounts when a transient RAM spike triggers
# the rule before active% has stabilised. Per device, per cycle.
MOVE_MAX_PER_CYCLE = _rules.get("move_max_per_cycle", 10)
SWAP_OFFLINE_ENABLED = _rules.get("swap_offline_enabled", False)
SWAP_OFFLINE_THRESHOLD = _rules.get("swap_offline_threshold_hours", 3) * 3600
SWAP_MAX_PER_DEVICE = _rules.get("swap_max_per_device", 5)
HEARTBEAT_FRESH = _rules.get("heartbeat_fresh_min", 10) * 60
FOLDER_STOCK_AMOUNT = _stock.get("amount", 100)
STOCK_GROUPS = set(_stock.get("groups", ["Potion", "Pet Farm"]))

DISK_FULL_ENABLED = _rules.get("disk_full_enabled", True)
RESTART_VPS_ENABLED = _rules.get("restart_vps_enabled", True)
RAM_MOVE_ENABLED = _rules.get("ram_move_enabled", True)
RAM_FILL_ENABLED = _rules.get("ram_fill_enabled", True)
EMPTY_FILL_ENABLED = _rules.get("empty_fill_enabled", True)
EMPTY_FILL_AMOUNT = _rules.get("empty_fill_amount", 20)
LOW_ONLINE_ENABLED = _rules.get("low_online_enabled", True)
# Dead-cookie folder cap: warn (and block automated adds) when a group's
# to_child folder holds more accounts than that group's live on-device total.
DEAD_FOLDER_CAP_ENABLED = _rules.get("dead_folder_cap_enabled", True)
# Per-group backup enforcement: apply each group's assigned backup to its devices
# and re-apply after a Restart VPS. Assignments come from the dashboard.
GROUP_BACKUP_ENABLED = _rules.get("group_backup_enabled", True)
STOCK_ENABLED = _stock.get("enabled", True)

DEVICE_MAX_ACCOUNTS = _auto.get("device_max_accounts", {})
SKIP_DEVICES = set(s.strip().lower() for s in _auto.get("skip_devices", []))

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs.txt")
DISCORD_WEBHOOK = CFG.get("discord_webhook", "")
# Per-account cumulative farm time (powers the dashboard Accounts page).
ACCOUNT_LIVETIME_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_account_livetime.json")
# Per-group backup: dashboard writes {group: backup_id}; we track what we've
# pushed per device in _group_applied.json so we don't re-apply every cycle.
GROUP_BACKUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_group_backups.json")
GROUP_APPLIED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_group_applied.json")
# Each device's current backup (written by the dashboard); used to skip devices
# already on the group's assigned backup so we don't needlessly re-image them.
DEVICE_BACKUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_device_backups.json")

# Track consecutive low-online cycles per device
low_online_tracker = {}    # device_id -> consecutive_count
restart_tracker = {}       # device_id -> consecutive restart count (resets on recovery)
attention_tracker = {}     # device_id -> timestamp when marked attention (pause 3h then recheck)
post_restart_tracker = {}  # device_id -> cycle_num when last Restart VPS was sent (1-cycle grace)
group_drained_tracker = set()  # group_names currently warned as drained (TO count == total group accounts)
dead_folder_over_tracker = set()  # group_names currently warned as dead-folder-over-cap (alert on transition)
_dead_folder_over_cap_ids = set()  # to_child folder IDs currently over cap; move_accounts_to_folder blocks adds to these
# Per-account cumulative farm time (Accounts page). Keyed by USERNAME, which is
# stable across moves (moves delete+recreate the account, changing its id).
# live_seconds accumulates while the account is assigned to ANY device; devices/
# groups are the historical union of everywhere it has farmed. _acct_last_on_device
# is in-memory only (never persisted) so a restart can't credit downtime as farm time.
_acct_livetime = {}        # username -> {live_seconds, devices[], groups[], first_seen, last_update}
_acct_last_on_device = {}  # username -> last wall-clock ts seen on a device (this run only)
action_log = []            # (timestamp, device_name, action, detail)
RESTART_WARN = _restart.get("warn_after", 3)
RESTART_COOLDOWN = _restart.get("cooldown_after", 3)
ATTENTION_PAUSE = _restart.get("attention_pause_hours", 3) * 3600
# Minimum wait between Restart VPS attempts on the same device (RULE 2 grace),
# expressed in minutes and converted to whole cycles.
RESTART_GRACE_MIN = _restart.get("restart_grace_min", 60)
RESTART_GRACE_CYCLES = max(1, round(RESTART_GRACE_MIN / max(1, CYCLE_INTERVAL // 60)))

# --- Group/Folder mapping ---
# Maps group_name -> folder from_child_folder_id (where removed accounts go)
GROUP_FOLDER_MAP = {}       # built dynamically each cycle


def write_log(text):
    """Append a line to logs.txt."""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


def _load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except Exception:
        return default


def _save_json_file(path, d):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _load_account_livetime():
    """Load the persisted per-account farm-time tracker (best-effort)."""
    global _acct_livetime
    try:
        with open(ACCOUNT_LIVETIME_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _acct_livetime = data
    except Exception:
        _acct_livetime = {}


def _save_account_livetime():
    """Persist the tracker atomically (tmp + replace)."""
    try:
        tmp = ACCOUNT_LIVETIME_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_acct_livetime, f)
        os.replace(tmp, ACCOUNT_LIVETIME_FILE)
    except Exception:
        pass


def _update_account_livetime(devices, accounts):
    """Accumulate per-account farm time for this cycle.

    For every account currently assigned to a device (any state), add the
    wall-clock elapsed since we last saw it on a device, and record the device
    + group into its historical sets. Accounts not on a device pause (their
    in-memory timestamp is dropped) so benched time isn't counted. The per-cycle
    delta is capped at 2x the cycle interval so a long gap (downtime, slow cycle)
    can never credit hours of phantom farm time.
    """
    try:
        if not isinstance(devices, list):
            devices = [devices] if devices else []
        if not isinstance(accounts, list):
            return
        now = time.time()
        dev_name, dev_group = {}, {}
        for d in devices:
            did = d.get("id")
            if not did:
                continue
            dev_name[did] = (d.get("device_note") or d.get("device_name") or "?").strip()
            dev_group[did] = (d.get("group_name") or "").strip()
        max_delta = CYCLE_INTERVAL * 2
        seen = set()
        for acc in accounts:
            uname = acc.get("username")
            did = acc.get("device_id")
            if not uname or not did:
                continue
            seen.add(uname)
            rec = _acct_livetime.get(uname)
            if rec is None:
                rec = {"live_seconds": 0.0, "devices": [], "groups": [], "first_seen": now, "last_update": now}
                _acct_livetime[uname] = rec
            dn, gn = dev_name.get(did), dev_group.get(did)
            if dn and dn not in rec["devices"]:
                rec["devices"].append(dn)
            if gn and gn not in rec["groups"]:
                rec["groups"].append(gn)
            last = _acct_last_on_device.get(uname)
            if last is not None:
                delta = now - last
                if 0 < delta <= max_delta:
                    rec["live_seconds"] = rec.get("live_seconds", 0.0) + delta
            _acct_last_on_device[uname] = now
            rec["last_update"] = now
        # Accounts no longer on a device → drop their timestamp so accumulation pauses
        for uname in list(_acct_last_on_device.keys()):
            if uname not in seen:
                _acct_last_on_device.pop(uname, None)
        _save_account_livetime()
    except Exception as e:
        try:
            write_log(f"[{datetime.now().strftime('%H:%M:%S')}] livetime update error: {e}")
        except Exception:
            pass


def _curl(method, url, json_body=None, timeout=15, with_auth=True):
    """Run an HTTP request via curl subprocess.

    Replaces Python's `requests` library which takes 30-45s per call to
    api.farmsync.cloud on this Windows box (SChannel OCSP revocation check).
    curl bypasses that and returns in <1s — typical full cycle drops from
    ~30 minutes to ~30 seconds.

    Returns parsed JSON (dict/list) or None for empty bodies.
    Raises RuntimeError("HTTP <code>: <body>") on 4xx/5xx, or
    RuntimeError on transport-level failures (timeout / curl missing / etc).
    """
    import subprocess as _sp
    full_url = url if url.startswith("http") else f"{BASE_URL}{url}"
    cmd = ["curl", "-sS", "--max-time", str(timeout),
           "-X", method.upper(),
           "-w", "\n%{http_code}",
           "-H", "Content-Type: application/json"]
    if with_auth:
        cmd += ["-H", f"Authorization: Bearer {load_api_key()}"]
    if json_body is not None:
        cmd += ["-d", json.dumps(json_body)]
    cmd.append(full_url)
    try:
        proc = _sp.run(cmd, capture_output=True, timeout=timeout + 5)
    except _sp.TimeoutExpired:
        raise RuntimeError(f"curl timeout after {timeout}s on {method} {full_url}")
    except FileNotFoundError:
        raise RuntimeError("curl not found in PATH")
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="ignore")[:200]
        raise RuntimeError(f"curl exit {proc.returncode}: {err}")
    out = proc.stdout.decode("utf-8", errors="ignore")
    nl = out.rfind("\n")
    body = out[:nl] if nl >= 0 else ""
    status_str = (out[nl + 1:] if nl >= 0 else out).strip()
    try:
        status = int(status_str)
    except ValueError:
        raise RuntimeError(f"curl returned non-numeric status: {status_str!r}")
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {body[:200]}")
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except Exception:
        raise RuntimeError(f"non-JSON response (HTTP {status}): {body[:200]}")


def _discord_post(payload):
    """Post to Discord webhook with error handling."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _curl("POST", DISCORD_WEBHOOK, json_body=payload, timeout=10, with_auth=False)
    except RuntimeError as e:
        msg = str(e)
        if "HTTP 429" in msg:
            write_log(f"[{now}] DISCORD | Rate limited, skipping")
        else:
            write_log(f"[{now}] DISCORD | Failed: {msg[:120]}")
    except Exception as e:
        write_log(f"[{now}] DISCORD | Error: {e}")


def send_discord(cycle_num, devices, actions_this_cycle):
    """Send summary embed + action log embed to Discord."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_dev = len(devices)
    online_dev = sum(1 for d in devices if d.get("client_running") and d.get("is_enabled"))
    total_acc = sum(d.get("total_accounts", 0) for d in devices)
    active_acc = sum(d.get("active_accounts", 0) for d in devices)
    total_ram = sum(d.get("sys_ram_total_gb", 0) for d in devices)
    used_ram = total_ram - sum(d.get("sys_ram_free_gb", 0) for d in devices)
    ram_pct_total = (used_ram / total_ram * 100) if total_ram > 0 else 0
    online_pct_total = (active_acc / total_acc * 100) if total_acc > 0 else 0

    ICONS = {
        "RELOGIN ALL": "\U0001F504", "RESTART TOOL": "\U0001F6E0",
        "OFFLINE RESTART": "\U0001F534", "MOVE ACCOUNTS": "\U0001F4E4",
        "FILL ACCOUNTS": "\U0001F4E5", "STOCK FOLDER": "\U0001F4E6",
        "NO ACCOUNTS": "\U0001F4ED", "FAILED": "\U0000274C", "SWAP OFFLINE": "\U0001F500", "DEVICE CAP": "\U0001F4CF",
        "ATTENTION": "\U0001F6A8", "PAUSED": "\U000023F8",
        "RECHECK": "\U0001F50D", "RECOVERED": "\U00002705",
    }

    # Count action types
    action_counts = {}
    for _, _, action, _ in actions_this_cycle:
        action_counts[action] = action_counts.get(action, 0) + 1

    # Color
    has_fail = "FAILED" in action_counts
    has_restart = any(a in action_counts for a in ("RELOGIN ALL", "RESTART TOOL", "OFFLINE RESTART"))
    if has_fail:
        color = 0xED4245
    elif has_restart:
        color = 0xFEE75C
    elif actions_this_cycle:
        color = 0x57F287
    else:
        color = 0x5865F2

    # --- Action count description ---
    if action_counts:
        count_parts = []
        for a, c in sorted(action_counts.items()):
            count_parts.append(f"{ICONS.get(a, '')} {a}: **{c}**")
        count_desc = " \u2022 ".join(count_parts)
    else:
        count_desc = "\U00002705 All devices healthy. No actions needed."

    # --- EMBED 1: Summary with all devices ---
    yc_balance = _query_yescaptcha_balance()
    YC_LOW_THRESHOLD = 100   # points
    yc_low = yc_balance is not None and yc_balance < YC_LOW_THRESHOLD
    if yc_balance is None:
        yc_field_name = "\U0001FA99 YesCaptcha Balance"
        yc_value = "_(unavailable)_"
    elif yc_low:
        # Highlight: warning emoji in field name, bold "LOW" prefix in value
        yc_field_name = "\U000026A0\U0000FE0F YesCaptcha LOW"
        yc_value = f"**\U000026A0\U0000FE0F LOW \u2014 {yc_balance:,} Points** (< {YC_LOW_THRESHOLD}, top up soon)"
        color = 0xED4245   # red \u2014 escalate the whole summary embed
    else:
        yc_field_name = "\U0001FA99 YesCaptcha Balance"
        yc_value = f"**{yc_balance:,}** Points"
    summary_embed = {
        "title": f"\U0001F4CA Cycle #{cycle_num} Summary",
        "color": color,
        "description": count_desc,
        "fields": [
            {"name": "\U0001F5A5 Devices", "value": f"**{online_dev}**/{total_dev} online", "inline": True},
            {"name": "\U0001F464 Accounts", "value": f"**{active_acc}**/{total_acc} ({online_pct_total:.0f}%)", "inline": True},
            {"name": "\U0001F4BE RAM", "value": f"**{used_ram:.0f}**/{total_ram:.0f} GB ({ram_pct_total:.0f}%)", "inline": True},
            {"name": yc_field_name, "value": yc_value, "inline": True},
        ],
        "footer": {"text": f"Next cycle in {CYCLE_INTERVAL // 60}min \u2022 {now}"},
    }

    # Send summary
    _discord_post({"embeds": [summary_embed]})

    # --- EMBED 2: Action Log grouped by type (only if there is anything to report) ---
    # Build offline devices list
    offline_lines = []
    for dev in devices:
        enabled = dev.get("is_enabled", False)
        client = dev.get("client_running", "")
        if enabled and not client:
            dname = (dev.get("device_note") or dev.get("device_name") or "?").strip()
            group = (dev.get("group_name") or "?").strip()
            tc = dev.get("total_accounts", 0)
            offline_lines.append(f"\U0001F534 **{dname}** ({group}) - {tc} accounts")

    if actions_this_cycle or offline_lines:
        time.sleep(1)  # rate limit between messages

        # Group actions
        groups = {
            "Needs Attention": [],
            "Paused (3h recheck)": [],
            "Rechecking": [],
            "Recovered": [],
            "Restarts": [],
            "Failures": [],
            "Moved Out (RAM High)": [],
            "Stocked Folder": [],
            "Filled In (RAM Low)": [],
            "No Accounts Available": [],
            "Swapped Offline (>3h)": [],
            "Device Cap Enforced": [],
            "Offline Devices": offline_lines,
        }
        group_icons = {
            "Needs Attention": "\U0001F6A8",
            "Paused (3h recheck)": "\U000023F8",
            "Rechecking": "\U0001F50D",
            "Recovered": "\U00002705",
            "Restarts": "\U0001F504",
            "Failures": "\U0000274C",
            "Moved Out (RAM High)": "\U0001F4E4",
            "Stocked Folder": "\U0001F4E6",
            "Filled In (RAM Low)": "\U0001F4E5",
            "No Accounts Available": "\U0001F4ED",
            "Swapped Offline (>3h)": "\U0001F500",
            "Device Cap Enforced": "\U0001F4CF",
            "Offline Devices": "\U0001F534",
        }
        for ts, dname, action, detail in actions_this_cycle:
            line = f"`{ts}` **{dname}** - {detail}"
            if action == "ATTENTION":
                groups["Needs Attention"].append(f"\U0001F6A8 {line}")
            elif action == "PAUSED":
                groups["Paused (3h recheck)"].append(f"\U000023F8 {line}")
            elif action == "RECHECK":
                groups["Rechecking"].append(f"\U0001F50D {line}")
            elif action == "RECOVERED":
                groups["Recovered"].append(f"\U00002705 {line}")
            elif action in ("RELOGIN ALL", "RESTART TOOL", "OFFLINE RESTART"):
                icon = ICONS.get(action, "\U0001F504")
                groups["Restarts"].append(f"{icon} {line}")
            elif action == "FAILED":
                groups["Failures"].append(f"\U0000274C {line}")
            elif action == "MOVE ACCOUNTS":
                groups["Moved Out (RAM High)"].append(f"\U0001F4E4 {line}")
            elif action == "STOCK FOLDER":
                groups["Stocked Folder"].append(f"\U0001F4E6 {line}")
            elif action == "FILL ACCOUNTS":
                groups["Filled In (RAM Low)"].append(f"\U0001F4E5 {line}")
            elif action == "NO ACCOUNTS":
                groups["No Accounts Available"].append(f"\U0001F4ED {line}")
            elif action == "SWAP OFFLINE":
                groups["Swapped Offline (>3h)"].append(f"\U0001F500 {line}")
            elif action == "DEVICE CAP":
                groups["Device Cap Enforced"].append(f"\U0001F4CF {line}")
            else:
                groups["Failures"].append(f"\U00002753 {line}")

        total_entries = len(actions_this_cycle) + len(offline_lines)
        action_embed = {
            "title": f"\U0001F4DD Action Log ({total_entries})",
            "color": color,
            "fields": [],
        }

        for group_name, lines in groups.items():
            if not lines:
                continue
            icon = group_icons.get(group_name, "")
            text = "\n".join(lines)
            # Compact if over 1024 chars
            if len(text) > 1024:
                # Shorten lines: device name + key detail
                short_lines = []
                for line in lines:
                    parts = line.split("**")
                    dname = parts[1] if len(parts) >= 3 else "?"
                    # Extract the detail after " - "
                    detail_part = line.split(" - ", 1)[1] if " - " in line else ""
                    if "need " in detail_part:
                        need = detail_part.split("need ")[1].split(" ")[0]
                        short_lines.append(f"{icon} **{dname}** - need {need}")
                    elif detail_part:
                        short_lines.append(f"{icon} **{dname}** - {detail_part[:40]}")
                    else:
                        short_lines.append(f"{icon} **{dname}**")
                text = "\n".join(short_lines)
                if len(text) > 1024:
                    text = text[:1020] + "\n..."
            action_embed["fields"].append({"name": f"{icon} {group_name}", "value": text, "inline": False})

        # Discord embed max 25 fields, 6000 chars total
        # If too many fields, send multiple embeds
        if len(action_embed["fields"]) <= 25:
            _discord_post({"embeds": [action_embed]})
        else:
            # Split into batches of 25 fields
            fields = action_embed["fields"]
            for i in range(0, len(fields), 25):
                batch = fields[i:i+25]
                part_embed = {
                    "title": f"\U0001F4DD Action Log (cont.)" if i > 0 else action_embed["title"],
                    "color": color,
                    "fields": batch,
                }
                if i > 0:
                    time.sleep(1)
                _discord_post({"embeds": [part_embed]})

    # --- EMBED 3: Revenue Today + new sales since last cycle (always sent) ---
    _init_last_seen_sale_id()
    rev_snapshot, rev_total, rev_total_count = _query_revenue_snapshot()
    new_sales, new_max_id = _query_new_sales_since(_last_seen_sale_id)
    # Bump baseline so next cycle only emits truly-new sales
    if new_max_id and new_max_id > (_last_seen_sale_id or 0):
        globals()["_last_seen_sale_id"] = new_max_id

    rev_lines = []
    if rev_snapshot is not None:
        rev_lines.append(f"**Total: ${rev_total:,.2f}** (Sales: {rev_total_count})")
        # Order rows by actual revenue (highest first) so the dominant platform leads.
        # Includes funpay2 as its own platform alongside funpay.
        platform_labels = [
            ("eldorado", "Eldorado"),
            ("funpay", "FunPay"),
            ("funpay2", "FunPay 2"),
            ("u7buy", "u7buy"),
            ("g2g", "G2G"),
            ("playerauctions", "PlayerAuctions"),
        ]
        platform_labels.sort(
            key=lambda kv: rev_snapshot.get(kv[0], (0.0, 0))[0],
            reverse=True,
        )
        # Prepend a colored dot reflecting each platform's current connection
        # status (mirrors the Cycle Summary "Platforms" field, but inline).
        _rev_pstatus = _read_platform_status() or {}
        _REV_STATUS_DOTS = {
            "connected":    "\U0001F7E2",  # 🟢
            "disconnected": "\U0001F7E1",  # 🟡
            "logged_out":   "\U0001F534",  # 🔴
        }
        for key, name in platform_labels:
            rev, cnt = rev_snapshot.get(key, (0.0, 0))
            dot = _REV_STATUS_DOTS.get(_rev_pstatus.get(key), "\U0001F7E2")
            rev_lines.append(f"• {dot} {name}: ${rev:,.2f} (Sales: {cnt})")
    else:
        rev_lines.append("_(Revenue DB not found at " + _REVENUE_DB_FILE + ")_")

    # New sales table — Discord field max ~1024 chars
    if new_sales:
        new_lines = []
        for sid, sold_at, username, platform, price in new_sales[:12]:
            t = (sold_at or "")[-8:-3] if sold_at and len(sold_at) >= 16 else (sold_at or "")
            desc = (username or "")[:38].replace("`", "")
            plat = (platform or "")[:6]
            new_lines.append(f"`{t}` `{plat:<5}` ${float(price or 0):>6.2f} {desc}")
        extra = len(new_sales) - 12
        if extra > 0:
            new_lines.append(f"_…and {extra} more_")
        new_value = "\n".join(new_lines) if new_lines else "_(none)_"
    else:
        new_value = "_(no new sales since last cycle)_"

    # Revenue by category (game) — today, highest first.
    cat_rev = _query_category_revenue()
    cat_lines = []
    if cat_rev:
        for cat, rev, cnt in cat_rev:
            if rev <= 0 and cnt == 0:
                continue
            cat_lines.append(f"• {cat}: ${rev:,.2f} (Sales: {cnt})")
    cat_value = "\n".join(cat_lines) if cat_lines else "_(no sales today)_"

    revenue_embed = {
        "title": f"\U0001F4B5 Revenue Today",
        "color": 0x57F287,
        "fields": [
            {"name": "Revenue", "value": "\n".join(rev_lines)[:1024], "inline": False},
            {"name": "By Category", "value": cat_value[:1024], "inline": False},
            {"name": f"New Sales ({len(new_sales)})", "value": new_value[:1024], "inline": False},
        ],
        "footer": {"text": f"Since last cycle • {now}"},
    }
    time.sleep(1)
    _discord_post({"embeds": [revenue_embed]})


def log_action(device_name, action, detail=""):
    ts = datetime.now().strftime("%H:%M:%S")
    entry = (ts, device_name, action, detail)
    action_log.append(entry)
    if len(action_log) > 200:
        action_log.pop(0)
    write_log(f"[{ts}] ACTION  | {device_name:<15} | {action:<20} | {detail}")


def fetch_devices():
    return _curl("GET", "/api/devices/", timeout=15) or []


def fetch_accounts():
    # /api/self/accounts/ is ~20 MB; give it a generous timeout
    return _curl("GET", "/api/self/accounts/", timeout=30) or []


def fetch_folders():
    return _curl("GET", "/api/self/folders/", timeout=15) or []


def fetch_configs():
    return _curl("GET", "/api/self/configs/", timeout=15) or []


def fetch_device_groups():
    data = _curl("GET", "/api/self/device-groups/", timeout=15) or {}
    return data.get("data", data) if isinstance(data, dict) else data


def try_restart(dev_id, name, task_type, reason, actions, last_updated=0):
    """Restart with tracking. Returns True if restart sent, False if skipped.
    Checks heartbeat first - if tool is dead (>10min), skip sending task.
    """
    now = time.time()

    # Check heartbeat - skip if tool is dead
    heartbeat_age = (now - last_updated / 1000) if last_updated else 999999
    if heartbeat_age >= HEARTBEAT_FRESH:
        detail = f"SKIPPED: {reason} (heartbeat {int(heartbeat_age/60)}min ago, tool dead)"
        log_action(name, "SKIPPED", detail)
        actions.append((datetime.now().strftime("%H:%M:%S"), name, "SKIPPED", detail))
        return False

    count = restart_tracker.get(dev_id, 0)

    # If device is in attention pause (3h), skip until timer expires
    if dev_id in attention_tracker:
        paused_at = attention_tracker[dev_id]
        remaining = ATTENTION_PAUSE - (now - paused_at)
        if remaining > 0:
            mins = int(remaining / 60)
            detail = f"PAUSED: {reason} (recheck in {mins}min)"
            log_action(name, "PAUSED", detail)
            actions.append((datetime.now().strftime("%H:%M:%S"), name, "PAUSED", detail))
            return False
        else:
            # 3h expired, reset and try again
            attention_tracker.pop(dev_id)
            restart_tracker[dev_id] = 0
            count = 0
            detail = f"3h pause expired, rechecking"
            log_action(name, "RECHECK", detail)
            actions.append((datetime.now().strftime("%H:%M:%S"), name, "RECHECK", detail))

    # After RESTART_COOLDOWN restarts, mark attention and pause 3h
    if count >= RESTART_COOLDOWN:
        attention_tracker[dev_id] = now
        detail = f"ATTENTION x{count}: {reason} (pausing 3h)"
        log_action(name, "ATTENTION", detail)
        actions.append((datetime.now().strftime("%H:%M:%S"), name, "ATTENTION", detail))
        return False

    try:
        create_task(dev_id, task_type)
        restart_tracker[dev_id] = count + 1
        count_now = count + 1

        if count_now >= RESTART_WARN:
            detail = f"ATTENTION x{count_now}: {reason}"
            log_action(name, "ATTENTION", detail)
            actions.append((datetime.now().strftime("%H:%M:%S"), name, "ATTENTION", detail))
        else:
            action_name = "OFFLINE RESTART" if "offline" in reason.lower() else ("RESTART TOOL" if task_type == "Restart Tool" else "RELOGIN ALL")
            log_action(name, action_name, reason)
            actions.append((datetime.now().strftime("%H:%M:%S"), name, action_name, reason))
        return True
    except Exception as e:
        log_action(name, "FAILED", f"{task_type}: {e}")
        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", f"{task_type}: {e}"))
        return False


def fetch_unassigned():
    return _curl("GET", "/api/self/accounts/unassigned", timeout=15) or []


def create_task(device_id, task_type, payload=None):
    """Create a device task (Relogin All, Restart Tool, Restart VPS)."""
    task_data = json.dumps({"task_type": task_type, "payload": payload or {}})
    body = {"device_id": device_id, "task_data": task_data}
    return _curl("POST", "/api/tasks/", json_body=body, timeout=15)


def fetch_device_tasks(device_id):
    """Get all tasks for a device."""
    data = _curl("GET", f"/api/devices/{device_id}/tasks", timeout=15) or {}
    return data.get("data") or []


def clear_device_tasks(device_id, device_name=""):
    """Delete all tasks on a device and verify they're gone. Returns True if cleared."""
    now_str = datetime.now().strftime("%H:%M:%S")
    for attempt in range(3):
        tasks = fetch_device_tasks(device_id)
        if not tasks:
            return True
        for t in tasks:
            try:
                _curl("DELETE", f"/api/tasks/{t['id']}", timeout=10)
            except Exception:
                pass
        # Verify
        remaining = fetch_device_tasks(device_id)
        if not remaining:
            write_log(f"[{now_str}] CLEAR   | {device_name:<15} | Cleared {len(tasks)} tasks (attempt {attempt + 1})")
            return True
        write_log(f"[{now_str}] CLEAR   | {device_name:<15} | {len(remaining)} tasks remain, retrying ({attempt + 1}/3)")
        time.sleep(2)
    write_log(f"[{now_str}] CLEAR   | {device_name:<15} | Failed to clear all tasks after 3 attempts")
    return False


def update_account(username, updates):
    """Update account fields by username."""
    return _curl("PUT", f"/api/self/accounts/{username}", json_body=updates, timeout=15)


def delete_account(username):
    """Delete account by username."""
    return _curl("DELETE", f"/api/self/accounts/{username}", timeout=15)


def create_account(account_data):
    """Create account with saved data."""
    return _curl("POST", "/api/self/accounts", json_body=account_data, timeout=15)


def _save_account_data(acc):
    """Extract fields needed to recreate an account."""
    return {
        "username": acc.get("username", ""),
        "password": acc.get("password", ""),
        "cookie": acc.get("cookie", ""),
        "config_id": acc.get("config_id", ""),
        "private_server_link": acc.get("private_server_link", ""),
        "enabled": acc.get("enabled", False),
    }


def move_accounts_to_folder(account_objects, folder_id):
    """Move accounts: delete from device, create in folder.
    account_objects = list of full account dicts from API.
    """
    # Dead-folder cap guard: never push accounts into a group's to_child ("dead
    # cookie") folder while it's already over its group's live-device total.
    # That folder is normally filled by hand in the FarmSync UI; this just stops
    # any automated path from making an over-cap folder bigger. Populated each
    # cycle by the dead-folder cap check; only ever holds to_child IDs, so the
    # legitimate from_child stocking/cap moves are unaffected.
    if folder_id and folder_id in _dead_folder_over_cap_ids:
        write_log(f"[{datetime.now().strftime('%H:%M:%S')}] DEAD CAP | Blocked add of "
                  f"{len(account_objects)} acc(s) to over-cap dead folder {folder_id[:8]}")
        return {"moved": 0, "blocked": True}
    moved = 0
    for acc in account_objects:
        uname = acc.get("username", "")
        saved = _save_account_data(acc)
        try:
            delete_account(uname)
            saved["folder_id"] = folder_id
            saved["device_id"] = ""
            saved["unassigned"] = True
            create_account(saved)
            moved += 1
        except Exception as e:
            # If delete succeeded but create failed, try to restore to original device
            try:
                saved["device_id"] = acc.get("device_id", "")
                saved["folder_id"] = acc.get("folder_id", "")
                saved["unassigned"] = False
                create_account(saved)
            except Exception:
                write_log(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL | Lost account {uname} during move: {e}")
    return {"moved": moved}


def move_accounts_to_device(account_objects, device_id, config_id=None, enable=False):
    """Move accounts: delete from folder, create on device.
    account_objects = list of full account dicts from API.
    config_id: if set, assign this config to the account.
    enable: if True, enable the account.
    """
    moved = 0
    for acc in account_objects:
        uname = acc.get("username", "")
        saved = _save_account_data(acc)
        try:
            delete_account(uname)
            saved["device_id"] = device_id
            saved["folder_id"] = ""
            saved["unassigned"] = False
            if config_id:
                saved["config_id"] = config_id
            if enable:
                saved["enabled"] = True
            create_account(saved)
            moved += 1
        except Exception as e:
            # If delete succeeded but create failed, try to restore to original folder
            try:
                saved["device_id"] = acc.get("device_id", "")
                saved["folder_id"] = acc.get("folder_id", "")
                saved["unassigned"] = acc.get("unassigned", False)
                create_account(saved)
            except Exception:
                write_log(f"[{datetime.now().strftime('%H:%M:%S')}] CRITICAL | Lost account {uname} during move: {e}")
    return {"moved": moved}


def build_group_folder_map(device_groups, folders):
    """Build mapping: group_name -> (from_child_folder_id, to_child_folder_id)."""
    GROUP_FOLDER_MAP.clear()
    folder_by_name = {}
    for f in folders:
        fname = (f.get("folder_name") or "").lower().strip()
        folder_by_name[fname] = f

    for g in device_groups:
        gname = g.get("name", "")
        # Try exact match first, then partial
        key = gname.lower().strip()
        matched = folder_by_name.get(key)
        if not matched:
            # Try partial match (e.g. "Pet Farm" matches "Adopt me pet farm")
            for fname, fdata in folder_by_name.items():
                if key in fname or fname in key:
                    matched = fdata
                    break
        if matched:
            GROUP_FOLDER_MAP[gname] = (
                matched.get("from_child_folder_id", ""),
                matched.get("to_child_folder_id", ""),
            )


def calc_ram_per_account(dev):
    """Estimate per-account RAM cost for move/fill sizing.

    Dividing by `active` alone is wrong when the device is still warming up:
    many accounts exist but few are running yet, so RAM/active overestimates
    the real per-account cost and the move planner panic-sheds dozens.

    Blend in a "at least 70% of total are assumed to run" floor so the
    denominator can't collapse to a tiny number during warm-up. Once the
    device is healthy (active >= 70% of total), the formula behaves like
    the old per-active estimate."""
    active = dev.get("active_accounts", 0)
    total = dev.get("total_accounts", 0)
    ram_total = dev.get("sys_ram_total_gb", 0)
    ram_free = dev.get("sys_ram_free_gb", 0)
    ram_used = ram_total - ram_free
    os_overhead = 4.0
    denom = max(active, int(total * 0.7))
    if denom > 0:
        return max(0.5, (ram_used - os_overhead) / denom)
    return 1.5  # default estimate for empty / brand-new devices


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_table(devices, cycle_num, actions_this_cycle):
    clear_screen()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Summary
    total_dev = len(devices)
    online_dev = sum(1 for d in devices if d.get("client_running") and d.get("is_enabled"))
    total_acc = sum(d.get("total_accounts", 0) for d in devices)
    active_acc = sum(d.get("active_accounts", 0) for d in devices)
    total_ram = sum(d.get("sys_ram_total_gb", 0) for d in devices)
    used_ram = total_ram - sum(d.get("sys_ram_free_gb", 0) for d in devices)

    header1 = f"  FarmSync Automation | Cycle #{cycle_num} | {now}"
    header2 = f"  Devices: {online_dev}/{total_dev} online | Accounts: {active_acc}/{total_acc} active | RAM: {used_ram:.0f}/{total_ram:.0f} GB"
    header3 = f"  Next cycle in {CYCLE_INTERVAL // 60}min"

    print(header1)
    print(header2)
    print(header3)
    print()

    # Device table
    rows = []
    for dev in devices:
        name = (dev.get("device_note") or dev.get("device_name") or "?").strip()
        group = (dev.get("group_name") or "None").strip()
        active = dev.get("active_accounts", 0)
        total = dev.get("total_accounts", 0)
        online_pct = (active / total * 100) if total > 0 else 0
        accounts = f"{active}/{total} ({online_pct:.0f}%)"

        ram_total = dev.get("sys_ram_total_gb", 0)
        ram_free = dev.get("sys_ram_free_gb", 0)
        ram_used = ram_total - ram_free
        ram_pct = (ram_used / ram_total * 100) if ram_total > 0 else 0
        ram = f"{ram_used:.0f}/{ram_total:.0f} GB ({ram_pct:.0f}%)"

        disk_total = dev.get("sys_disk_total_gb", 0)
        disk_free = dev.get("sys_disk_free_gb", 0)
        disk_used = disk_total - disk_free
        disk_pct = (disk_used / disk_total * 100) if disk_total > 0 else 0
        disk = f"{disk_used:.0f}/{disk_total:.0f} GB ({disk_pct:.0f}%)"

        # Status with warnings
        dev_id = dev.get("id", "")
        low_count = low_online_tracker.get(dev_id, 0)
        client = dev.get("client_running", "")
        enabled = dev.get("is_enabled", False)

        restart_count = restart_tracker.get(dev_id, 0)
        is_paused = dev_id in attention_tracker

        if not enabled:
            status = "DISABLED"
        elif is_paused:
            remaining = ATTENTION_PAUSE - (time.time() - attention_tracker[dev_id])
            mins = max(0, int(remaining / 60))
            status = f"PAUSED {mins}m"
        elif restart_count >= RESTART_WARN:
            status = f"ATTN x{restart_count}"
        elif not client and total > 0:
            status = "OFFLINE!"
        elif not client:
            status = "OFFLINE"
        elif disk_pct >= DISK_FULL_PCT * 100:
            status = "DISK FULL!"
        elif ram_pct >= RAM_HIGH * 100 and active == 0:
            status = "RAM STUCK!"
        elif ram_pct >= RAM_HIGH * 100:
            status = "RAM HIGH!"
        elif online_pct < ONLINE_WARN * 100 and low_count >= ONLINE_WARN_CYCLES:
            status = f"CRIT x{low_count}"
        elif online_pct < ONLINE_LOW * 100 and low_count >= ONLINE_LOW_CYCLES:
            status = f"LOW x{low_count}"
        elif online_pct < ONLINE_LOW * 100 and total > 0 and low_count > 0:
            status = f"LOW x{low_count}"
        else:
            status = "OK"

        rows.append((name, group, accounts, ram, disk, status))

    hdrs = ("Device", "Group", "Online/Total", "RAM Used/Total", "Disk Used/Total", "Status")
    widths = [max(len(hdrs[c]), max(len(r[c]) for r in rows)) for c in range(len(hdrs))]

    def fmt(vals):
        return "| " + " | ".join(f"{v:<{w}}" for v, w in zip(vals, widths)) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    print(sep)
    print(fmt(hdrs))
    print(sep)
    for r in rows:
        print(fmt(r))
    print(sep)

    # Actions this cycle
    if actions_this_cycle:
        print(f"\n  Actions taken this cycle:")
        for ts, dname, action, detail in actions_this_cycle:
            print(f"    [{ts}] {dname:<15} {action:<20} {detail}")
    else:
        print(f"\n  No actions needed this cycle.")

    # Recent action log (last 10)
    if action_log:
        print(f"\n  Recent action log (last 10):")
        for ts, dname, action, detail in action_log[-10:]:
            print(f"    [{ts}] {dname:<15} {action:<20} {detail}")

    # --- Write cycle to logs.txt ---
    write_log("")
    write_log("=" * 90)
    write_log(header1)
    write_log(header2)
    write_log(sep)
    write_log(fmt(hdrs))
    write_log(sep)
    for r in rows:
        write_log(fmt(r))
    write_log(sep)
    if actions_this_cycle:
        write_log(f"  Actions this cycle:")
        for ts, dname, action, detail in actions_this_cycle:
            write_log(f"    [{ts}] {dname:<15} | {action:<20} | {detail}")
    else:
        write_log(f"  No actions this cycle.")
    write_log("=" * 90)

    # Send to Discord
    send_discord(cycle_num, devices, actions_this_cycle)


def run_cycle(cycle_num):
    """Run one automation cycle. Returns list of actions taken."""
    actions = []

    # Paused by the Revenue Dashboard → skip cycle (still refresh shared state)
    if _is_paused():
        try:
            devices = fetch_devices()
            accounts = fetch_accounts()
            _write_shared_state(devices, accounts)
            _update_account_livetime(devices, accounts)
        except Exception:
            pass
        write_log(f"[{datetime.now().strftime('%H:%M:%S')}] PAUSED  | Cycle #{cycle_num} skipped (_paused.flag present)")
        return actions

    # Fetch all data
    devices = fetch_devices()
    accounts = fetch_accounts()
    # Publish shared state for the website dashboard (avoids duplicate cloud-API calls)
    _write_shared_state(devices, accounts)
    folders = fetch_folders()
    device_groups = fetch_device_groups()
    unassigned_accounts = fetch_unassigned()
    configs = fetch_configs()

    if not isinstance(devices, list):
        devices = [devices]

    # Accumulate per-account farm time (Accounts page) before any moves this cycle
    _update_account_livetime(devices, accounts)

    # Build group->folder map
    build_group_folder_map(device_groups, folders)

    # Build group_name -> config_id map (partial match: group name in config name)
    group_config_map = {}
    config_list = [(cfg.get("name", "").strip(), cfg.get("id", "")) for cfg in configs if cfg.get("name") and cfg.get("id")]
    for g in device_groups:
        gname = (g.get("name") or "").strip()
        gkey = gname.lower()
        # Try exact match first, then partial
        matched = None
        for cname, cid in config_list:
            if cname.lower() == gkey:
                matched = cid
                break
        if not matched:
            for cname, cid in config_list:
                if gkey in cname.lower() or cname.lower() in gkey:
                    matched = cid
                    break
        if matched:
            group_config_map[gname] = matched

    # Build device_id -> group_name map
    dev_group_map = {}
    for dev in devices:
        dev_group_map[dev.get("id", "")] = (dev.get("group_name") or "").strip()

    # Build device_id -> accounts map
    dev_accounts = {}
    for acc in accounts:
        did = acc.get("device_id", "")
        if did:
            if did not in dev_accounts:
                dev_accounts[did] = []
            dev_accounts[did].append(acc)

    # --- Group backup enforcement: force-apply each group's assigned backup to
    # every device in that group we haven't pushed it to yet (RULE 2 resets a
    # device's marker after a Restart VPS so it re-applies on the next pass). ---
    group_assign = _load_json_file(GROUP_BACKUPS_FILE, {}) if GROUP_BACKUP_ENABLED else {}
    if group_assign:
        gb_applied = _load_json_file(GROUP_APPLIED_FILE, {})
        gb_current = (_load_json_file(DEVICE_BACKUPS_FILE, {}) or {}).get("map", {})
        gb_name = {}
        try:
            _gbf = _curl("GET", "/api/s3/files?type=backup", timeout=15) or {}
            for _it in (_gbf.get("items") or []):
                if _it.get("id"):
                    gb_name[_it["id"]] = _it.get("original_name") or ""
        except Exception:
            pass
        gb_changed = False
        for dev in devices:
            did = dev.get("id", "")
            bid = group_assign.get(dev_group_map.get(did, ""))
            if not did or not bid:
                continue
            # Skip if the device is ALREADY on this backup (current), or we've
            # already pushed it this assignment — don't needlessly re-image.
            if (gb_current.get(did) or {}).get("id") == bid or gb_applied.get(did) == bid:
                continue
            try:
                create_task(did, "Backup", {"file_id": bid})
                gb_applied[did] = bid
                gb_changed = True
                _nm = (dev.get("device_note") or dev.get("device_name") or "?").strip()
                _detail = f"applied group backup {gb_name.get(bid, bid[:12])}"
                log_action(_nm, "GROUP BACKUP", _detail)
                actions.append((datetime.now().strftime("%H:%M:%S"), _nm, "GROUP BACKUP", _detail))
            except Exception:
                pass
        if gb_changed:
            _save_json_file(GROUP_APPLIED_FILE, gb_applied)

    # --- Auto-fix: enable all device accounts + assign correct config per group ---
    fix_enabled = 0
    fix_config = 0
    fix_errors = 0
    for dev in devices:
        did = dev.get("id", "")
        gname = dev_group_map.get(did, "")
        correct_config = group_config_map.get(gname)
        my_accs = dev_accounts.get(did, [])
        for acc in my_accs:
            uname = acc.get("username", "")
            needs_enable = not acc.get("enabled", False)
            needs_config = correct_config and acc.get("config_id") != correct_config
            if needs_enable or needs_config:
                updates = {}
                if needs_enable:
                    updates["enabled"] = True
                if needs_config:
                    updates["config_id"] = correct_config
                try:
                    update_account(uname, updates)
                    if needs_enable:
                        fix_enabled += 1
                    if needs_config:
                        fix_config += 1
                except Exception:
                    fix_errors += 1
    if fix_enabled > 0 or fix_config > 0:
        detail = f"Enabled {fix_enabled} accs, fixed config on {fix_config} accs"
        if fix_errors > 0:
            detail += f", {fix_errors} errors"
        log_action("SYSTEM", "AUTO-FIX", detail)
        actions.append((datetime.now().strftime("%H:%M:%S"), "SYSTEM", "AUTO-FIX", detail))

    # Track accounts freed this cycle (username -> folder_id) so fill can use them
    freed_accounts = []  # list of (username, folder_from_id)
    used_this_cycle = set()  # usernames already moved to a device this cycle

    # --- Group-drain detection: if accounts-on-devices-in-group == accounts-in-TO-folder,
    # block pre-stocking unassigned -> from_child for that group and warn on transition. ---
    group_drained = set()
    for gname, (folder_from_id, folder_to_id) in GROUP_FOLDER_MAP.items():
        if not folder_to_id:
            continue
        group_dev_ids = {did for did, g in dev_group_map.items() if g == gname}
        to_count = 0
        on_device_count = 0
        for a in accounts:
            fid = a.get("folder_id")
            did = a.get("device_id")
            if fid == folder_to_id:
                to_count += 1
            elif did and did in group_dev_ids:
                on_device_count += 1

        if to_count > 0 and on_device_count == to_count:
            group_drained.add(gname)
            if gname not in group_drained_tracker:
                group_drained_tracker.add(gname)
                detail = f"Group '{gname}' ATTENTION: on-device accounts ({on_device_count}) == to_child folder ({to_count}), blocking unassigned->from_child stock"
                log_action("SYSTEM", "ATTENTION", detail)
                actions.append((datetime.now().strftime("%H:%M:%S"), f"[{gname}]", "ATTENTION", detail))
        else:
            group_drained_tracker.discard(gname)

    # --- Dead-cookie folder cap: the to_child ("dead cookie") folder is filled by
    # hand in the FarmSync UI, so we can't intercept a manual move. Each cycle we
    # (1) WARN when a group's dead folder exceeds that group's live on-device
    # account total (loose name match, via GROUP_FOLDER_MAP), and (2) record the
    # folder id so move_accounts_to_folder blocks any automated add to it. ---
    _dead_folder_over_cap_ids.clear()
    if DEAD_FOLDER_CAP_ENABLED:
        # group_name -> sum of live on-device accounts across that group's devices
        group_live_total = {}
        for d in devices:
            gname = (d.get("group_name") or "").strip()
            if gname:
                group_live_total[gname] = group_live_total.get(gname, 0) + (d.get("total_accounts") or 0)
        for gname, (folder_from_id, folder_to_id) in GROUP_FOLDER_MAP.items():
            if not folder_to_id:
                continue
            cap = group_live_total.get(gname, 0)
            if cap <= 0:
                continue  # no live devices in this group -> nothing to cap against
            dead_count = sum(1 for a in accounts if a.get("folder_id") == folder_to_id)
            if dead_count > cap:
                over = dead_count - cap
                _dead_folder_over_cap_ids.add(folder_to_id)
                # Local log every cycle; loud Discord/action alert only on transition into over-cap.
                write_log(f"[{datetime.now().strftime('%H:%M:%S')}] DEAD CAP | {gname}: "
                          f"dead folder {dead_count}/{cap} (over by {over})")
                if gname not in dead_folder_over_tracker:
                    dead_folder_over_tracker.add(gname)
                    detail = f"Dead folder OVER cap: {dead_count}/{cap} (over by {over}) - blocking automated adds, remove {over} manually"
                    log_action(f"[{gname}]", "DEAD CAP", detail)
                    actions.append((datetime.now().strftime("%H:%M:%S"), f"[{gname}]", "DEAD CAP", detail))
            else:
                if gname in dead_folder_over_tracker:
                    dead_folder_over_tracker.discard(gname)
                    detail = f"Dead folder back under cap: {dead_count}/{cap}"
                    log_action(f"[{gname}]", "DEAD CAP", detail)
                    actions.append((datetime.now().strftime("%H:%M:%S"), f"[{gname}]", "DEAD CAP", detail))

    # --- Pre-stock: if from_child folder is empty, stock 100 unassigned accounts into it ---
    for gname, (folder_from_id, folder_to_id) in GROUP_FOLDER_MAP.items():
        if not STOCK_ENABLED:
            continue
        if not folder_from_id or gname not in STOCK_GROUPS:
            continue
        if gname in group_drained:
            continue  # drained guard: don't pull fresh unassigned accounts when on-device == to_child
        from_count = sum(1 for a in accounts if a.get("folder_id") == folder_from_id)
        if from_count > 0:
            continue
        available = [a for a in unassigned_accounts
                     if not a.get("folder_id") and not a.get("device_id")
                     and a.get("username") not in used_this_cycle]
        to_stock = available[:FOLDER_STOCK_AMOUNT]
        if not to_stock:
            continue
        result = move_accounts_to_folder(to_stock, folder_from_id)
        stocked = result.get("moved", 0)
        if stocked > 0:
            for acc in to_stock[:stocked]:
                used_this_cycle.add(acc.get("username", ""))
                # Update in-memory accounts so fill logic can see them
                updated = dict(acc)
                updated["folder_id"] = folder_from_id
                updated["device_id"] = ""
                updated["unassigned"] = True
                accounts.append(updated)
            detail = f"Pre-stocked {stocked} unassigned accs into {gname} from_child folder (was empty)"
            log_action("SYSTEM", "STOCK FOLDER", detail)
            actions.append((datetime.now().strftime("%H:%M:%S"), "SYSTEM", "STOCK FOLDER", detail))

    # Sort devices by name
    devices.sort(key=lambda d: (d.get("device_note") or d.get("device_name") or "").strip().lower())

    for dev in devices:
        dev_id = dev.get("id", "")
        name = (dev.get("device_note") or dev.get("device_name") or "?").strip()
        enabled = dev.get("is_enabled", False)
        client = dev.get("client_running", "")
        active = dev.get("active_accounts", 0)
        total = dev.get("total_accounts", 0)

        if not enabled:
            continue

        if name.lower() in SKIP_DEVICES:
            continue          # device opted out via config "skip_devices" — leave it alone

        # --- RULE 0: Offline device with accounts -> Relogin All ---
        if not client and total > 0:
            try_restart(dev_id, name, "Relogin All", f"Device offline with {total} accounts", actions, dev.get("last_updated", 0))
            continue

        if not client:
            continue

        # --- Recovery detection: device is online and healthy -> reset restart counter ---
        if active > 0 and dev_id in restart_tracker:
            old_count = restart_tracker.pop(dev_id)
            post_restart_tracker.pop(dev_id, None)
            if old_count >= RESTART_WARN:
                detail = f"Recovered after {old_count} restarts ({active}/{total} online)"
                log_action(name, "RECOVERED", detail)
                actions.append((datetime.now().strftime("%H:%M:%S"), name, "RECOVERED", detail))

        ram_total = dev.get("sys_ram_total_gb", 0)
        ram_free = dev.get("sys_ram_free_gb", 0)
        ram_used = ram_total - ram_free
        ram_pct = (ram_used / ram_total) if ram_total > 0 else 0

        disk_total = dev.get("sys_disk_total_gb", 0)
        disk_free = dev.get("sys_disk_free_gb", 0)
        disk_pct = ((disk_total - disk_free) / disk_total) if disk_total > 0 else 0

        online_pct = (active / total) if total > 0 else 0

        group_name = dev_group_map.get(dev_id, "")
        my_accounts = dev_accounts.get(dev_id, [])

        # --- DEVICE CAP: enforce max account limit per device ---
        max_cap = DEVICE_MAX_ACCOUNTS.get(name)
        if max_cap and total > max_cap and group_name and group_name in GROUP_FOLDER_MAP:
            excess = total - max_cap
            folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
            non_running = [a for a in my_accounts if not a.get("running")]
            running_accs = [a for a in my_accounts if a.get("running")]
            to_move = non_running[:excess]
            if len(to_move) < excess:
                to_move += running_accs[:excess - len(to_move)]
            if to_move:
                result = move_accounts_to_folder(to_move, folder_from_id)
                moved = result.get("moved", 0)
                if moved > 0:
                    for acc in to_move[:moved]:
                        freed_accounts.append((acc.get("username", ""), folder_from_id))
                    total -= moved
                    detail = f"CAP {max_cap}: moved {moved}/{len(to_move)} excess accs to {group_name} folder ({total} remaining)"
                    log_action(name, "DEVICE CAP", detail)
                    actions.append((datetime.now().strftime("%H:%M:%S"), name, "DEVICE CAP", detail))

        # --- RULE 1: Disk full (>=95%) -> Restart Tool (triggers reclone) ---
        if DISK_FULL_ENABLED and disk_pct >= DISK_FULL_PCT:
            try_restart(dev_id, name, "Restart Tool", f"Disk {disk_pct*100:.0f}% full", actions, dev.get("last_updated", 0))
            continue  # skip other checks

        # --- RULE 2: 0 accounts online -> check heartbeat -> Restart VPS or Relogin All ---
        if RESTART_VPS_ENABLED and active == 0 and total > 0:
            last_restart_cycle = post_restart_tracker.get(dev_id, -999)
            cycles_since = cycle_num - last_restart_cycle
            if cycles_since < RESTART_GRACE_CYCLES:
                # Recently sent a Restart VPS — hold off for the grace window (~1h)
                mins_left = (RESTART_GRACE_CYCLES - cycles_since) * (CYCLE_INTERVAL // 60)
                detail = f"0/{total} online, RAM {ram_pct*100:.0f}% (Restart VPS at cycle #{last_restart_cycle}, {RESTART_GRACE_MIN}min grace, ~{mins_left}min left)"
                log_action(name, "GRACE", detail)
                actions.append((datetime.now().strftime("%H:%M:%S"), name, "GRACE", detail))
                low_online_tracker.pop(dev_id, None)
                continue

            last_updated = dev.get("last_updated", 0)
            heartbeat_age = (time.time() - last_updated / 1000) if last_updated else 999999

            if heartbeat_age < HEARTBEAT_FRESH:
                # Heartbeat fresh (<10min) -> clear tasks then Restart VPS
                cleared = clear_device_tasks(dev_id, name)
                if cleared:
                    post_restart_tracker[dev_id] = cycle_num
                    try_restart(dev_id, name, "Restart VPS", f"0/{total} online, RAM {ram_pct*100:.0f}%, heartbeat {int(heartbeat_age/60)}min ago, tasks cleared -> Restart VPS", actions, dev.get("last_updated", 0))
                    # Group backup: re-apply the group's backup directly after the
                    # Restart VPS (the reboot can reset the emulator). The reconcile
                    # alone would skip it since the device is still tracked as on it.
                    _gbid = group_assign.get(dev_group_map.get(dev_id, ""))
                    if _gbid:
                        try:
                            create_task(dev_id, "Backup", {"file_id": _gbid})
                            _ga = _load_json_file(GROUP_APPLIED_FILE, {})
                            _ga[dev_id] = _gbid
                            _save_json_file(GROUP_APPLIED_FILE, _ga)
                            log_action(name, "GROUP BACKUP", "re-applied after Restart VPS")
                            actions.append((datetime.now().strftime("%H:%M:%S"), name, "GROUP BACKUP", "re-applied after Restart VPS"))
                        except Exception:
                            pass
                else:
                    detail = f"0/{total} online, heartbeat {int(heartbeat_age/60)}min ago, failed to clear tasks, skipping Restart VPS"
                    log_action(name, "FAILED", detail)
                    actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))
            else:
                # Heartbeat stale (>10min) -> tool is dead, skip
                detail = f"0/{total} online, heartbeat {int(heartbeat_age/60)}min ago (tool dead, skipping)"
                log_action(name, "SKIPPED", detail)
                actions.append((datetime.now().strftime("%H:%M:%S"), name, "SKIPPED", detail))
            low_online_tracker.pop(dev_id, None)
            continue

        # --- RULE 3: RAM >90% -> delete from device, create in from_child folder ---
        if RAM_MOVE_ENABLED and ram_pct > RAM_HIGH and active > 0:
            ram_per_acc = calc_ram_per_account(dev)
            available_ram = ram_total * RAM_TARGET - 4.0  # OS overhead
            target_accounts = max(1, int(available_ram / ram_per_acc))
            excess = total - target_accounts
            # Defensive belt: never shed more than MOVE_MAX_PER_CYCLE in one
            # cycle even if the planner thinks it should. A real overload will
            # trigger again next cycle and trim a bit more.
            excess = min(excess, MOVE_MAX_PER_CYCLE)

            if excess > 0 and group_name and group_name in GROUP_FOLDER_MAP:
                folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
                non_running = [a for a in my_accounts if not a.get("running")]
                running = [a for a in my_accounts if a.get("running")]
                to_move = non_running[:excess]
                if len(to_move) < excess:
                    to_move += running[:excess - len(to_move)]

                if to_move:
                    result = move_accounts_to_folder(to_move, folder_from_id)
                    moved = result.get("moved", 0)
                    if moved > 0:
                        for acc in to_move[:moved]:
                            freed_accounts.append((acc.get("username", ""), folder_from_id))
                        detail = f"RAM {ram_pct*100:.0f}%, moved {moved}/{len(to_move)} accs to {group_name} folder"
                        log_action(name, "MOVE ACCOUNTS", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "MOVE ACCOUNTS", detail))
                    if moved < len(to_move):
                        failed = len(to_move) - moved
                        detail = f"Move to folder: {failed}/{len(to_move)} failed"
                        log_action(name, "FAILED", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))

        # --- RULE 4: RAM <90% -> delete from from_child folder, create on device ---
        elif RAM_FILL_ENABLED and ram_pct < RAM_HIGH and total > 0 and online_pct > 0.85 and group_name and group_name in GROUP_FOLDER_MAP:
            folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
            ram_per_acc = calc_ram_per_account(dev)
            available_ram = ram_total * RAM_TARGET - 4.0
            target_accounts = max(total, int(available_ram / ram_per_acc))
            # Respect device max account cap
            max_cap = DEVICE_MAX_ACCOUNTS.get(name)
            if max_cap:
                target_accounts = min(target_accounts, max_cap)
            can_add = min(target_accounts - total, FILL_MAX_PER_CYCLE)

            if can_add > 0:
                # Find all accounts in from_child folder
                folder_accounts_list = [a for a in accounts
                                        if a.get("folder_id") == folder_from_id]
                # Remove accounts already moved this cycle
                folder_accounts_list = [a for a in folder_accounts_list
                                        if a.get("username") not in used_this_cycle]

                if not folder_accounts_list:
                    # Try to fill from_child with unassigned accounts first
                    available_unassigned = [a for a in unassigned_accounts
                                            if not a.get("folder_id")
                                            and not a.get("device_id")
                                            and a.get("username") not in used_this_cycle]
                    if available_unassigned:
                        to_stock = available_unassigned[:can_add]
                        result = move_accounts_to_folder(to_stock, folder_from_id)
                        stocked = result.get("moved", 0)
                        if stocked > 0:
                            for acc in to_stock[:stocked]:
                                used_this_cycle.add(acc.get("username", ""))
                            detail = f"Stocked {stocked} unassigned accs into {group_name} from_child folder"
                            log_action(name, "STOCK FOLDER", detail)
                            actions.append((datetime.now().strftime("%H:%M:%S"), name, "STOCK FOLDER", detail))
                            # Now use those freshly stocked accounts to fill the device
                            # Re-fetch is expensive, so build from what we just moved
                            folder_accounts_list = to_stock[:stocked]
                        else:
                            detail = f"RAM {ram_pct*100:.0f}%, need {can_add} accs but {group_name} from_child folder is empty, stock failed"
                            log_action(name, "NO ACCOUNTS", detail)
                            actions.append((datetime.now().strftime("%H:%M:%S"), name, "NO ACCOUNTS", detail))
                    else:
                        detail = f"RAM {ram_pct*100:.0f}%, need {can_add} accs but {group_name} from_child folder is empty, no unassigned available"
                        log_action(name, "NO ACCOUNTS", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "NO ACCOUNTS", detail))

                if folder_accounts_list:
                    to_assign = folder_accounts_list[:can_add]
                    fill_config_id = group_config_map.get(group_name)
                    result = move_accounts_to_device(to_assign, dev_id, config_id=fill_config_id, enable=True)
                    moved = result.get("moved", 0)
                    if moved > 0:
                        for acc in to_assign[:moved]:
                            used_this_cycle.add(acc.get("username", ""))
                        cfg_note = f" + config '{group_name}' + enabled" if fill_config_id else " + enabled (no config found)"
                        detail = f"RAM {ram_pct*100:.0f}%, filled {moved}/{len(to_assign)} accs from {group_name} folder{cfg_note}"
                        log_action(name, "FILL ACCOUNTS", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FILL ACCOUNTS", detail))
                    elif moved == 0 and len(to_assign) > 0:
                        detail = f"Fill from folder: 0/{len(to_assign)} succeeded"
                        log_action(name, "FAILED", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))

        # --- RULE 4b: Empty device (0 accounts) -> fill with EMPTY_FILL_AMOUNT accounts ---
        elif EMPTY_FILL_ENABLED and total == 0 and group_name and group_name in GROUP_FOLDER_MAP:
            folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
            can_add = EMPTY_FILL_AMOUNT
            max_cap = DEVICE_MAX_ACCOUNTS.get(name)
            if max_cap:
                can_add = min(can_add, max_cap)

            if can_add > 0:
                folder_accounts_list = [a for a in accounts
                                        if a.get("folder_id") == folder_from_id
                                        and a.get("username") not in used_this_cycle]

                if not folder_accounts_list:
                    available_unassigned = [a for a in unassigned_accounts
                                            if not a.get("folder_id")
                                            and not a.get("device_id")
                                            and a.get("username") not in used_this_cycle]
                    if available_unassigned:
                        to_stock = available_unassigned[:can_add]
                        result = move_accounts_to_folder(to_stock, folder_from_id)
                        stocked = result.get("moved", 0)
                        if stocked > 0:
                            for acc in to_stock[:stocked]:
                                used_this_cycle.add(acc.get("username", ""))
                            detail = f"Stocked {stocked} unassigned accs into {group_name} from_child folder (empty device bootstrap)"
                            log_action(name, "STOCK FOLDER", detail)
                            actions.append((datetime.now().strftime("%H:%M:%S"), name, "STOCK FOLDER", detail))
                            folder_accounts_list = to_stock[:stocked]
                        else:
                            detail = f"Empty device, need {can_add} accs but {group_name} from_child folder is empty, stock failed"
                            log_action(name, "NO ACCOUNTS", detail)
                            actions.append((datetime.now().strftime("%H:%M:%S"), name, "NO ACCOUNTS", detail))
                    else:
                        detail = f"Empty device, need {can_add} accs but {group_name} from_child folder is empty, no unassigned available"
                        log_action(name, "NO ACCOUNTS", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "NO ACCOUNTS", detail))

                if folder_accounts_list:
                    to_assign = folder_accounts_list[:can_add]
                    fill_config_id = group_config_map.get(group_name)
                    result = move_accounts_to_device(to_assign, dev_id, config_id=fill_config_id, enable=True)
                    moved = result.get("moved", 0)
                    if moved > 0:
                        for acc in to_assign[:moved]:
                            used_this_cycle.add(acc.get("username", ""))
                        cfg_note = f" + config '{group_name}' + enabled" if fill_config_id else " + enabled (no config found)"
                        detail = f"Empty device, filled {moved}/{len(to_assign)} accs from {group_name} folder{cfg_note}"
                        log_action(name, "FILL EMPTY", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FILL EMPTY", detail))
                    elif moved == 0 and len(to_assign) > 0:
                        detail = f"Fill empty device: 0/{len(to_assign)} succeeded"
                        log_action(name, "FAILED", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))

        # --- RULE 5: Two-tier online check ---
        # Skip if no accounts available to fill (relogin won't help with empty pool)
        if LOW_ONLINE_ENABLED and total > 0 and online_pct < ONLINE_LOW:
            # Check if there are any accounts to work with
            has_folder_accounts = False
            has_unassigned = False
            if group_name and group_name in GROUP_FOLDER_MAP:
                folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
                has_folder_accounts = any(a.get("folder_id") == folder_from_id
                                          and a.get("username") not in used_this_cycle
                                          for a in accounts)
            has_unassigned = any(not a.get("folder_id") and not a.get("device_id")
                                and a.get("username") not in used_this_cycle
                                for a in unassigned_accounts)

            if not has_folder_accounts and not has_unassigned:
                # No accounts to fill, skip relogin
                low_online_tracker.pop(dev_id, None)
            else:
                low_online_tracker[dev_id] = low_online_tracker.get(dev_id, 0) + 1
                count = low_online_tracker[dev_id]

                if online_pct < ONLINE_WARN and count >= ONLINE_WARN_CYCLES:
                    try_restart(dev_id, name, "Relogin All", f"{active}/{total} online ({online_pct*100:.0f}%) <40% for {count} cycles", actions, dev.get("last_updated", 0))
                    low_online_tracker[dev_id] = 0
                elif count >= ONLINE_LOW_CYCLES:
                    try_restart(dev_id, name, "Relogin All", f"{active}/{total} online ({online_pct*100:.0f}%) <60% for {count} cycles", actions, dev.get("last_updated", 0))
                    low_online_tracker[dev_id] = 0
        else:
            low_online_tracker.pop(dev_id, None)

        # --- RULE 6: Swap individual accounts offline >3h with newest from from_child folder ---
        if SWAP_OFFLINE_ENABLED and group_name and group_name in GROUP_FOLDER_MAP and active > 0 and total > 0:
            folder_from_id, _ = GROUP_FOLDER_MAP[group_name]
            now_ms = time.time() * 1000

            # Find accounts on this device that are offline >3h
            offline_accs = []
            for acc in my_accounts:
                if acc.get("running"):
                    continue
                login_at = acc.get("login_at", 0) or 0
                if login_at > 0:
                    offline_s = (now_ms - login_at) / 1000
                else:
                    offline_s = float('inf')  # never logged in
                if offline_s >= SWAP_OFFLINE_THRESHOLD:
                    offline_accs.append((acc, offline_s))

            if not offline_accs:
                pass  # no stale accounts on this device
            else:
                # Sort longest offline first
                offline_accs.sort(key=lambda x: x[1], reverse=True)
                offline_accs = offline_accs[:SWAP_MAX_PER_DEVICE]

                # Get from_child folder pool (newest first by last_updated, skip dead cookies)
                folder_pool = [a for a in accounts
                               if a.get("folder_id") == folder_from_id
                               and not a.get("device_id")
                               and not a.get("dead_cookie")
                               and a.get("username") not in used_this_cycle]
                folder_pool.sort(key=lambda a: a.get("last_updated", "") or "", reverse=True)

                swapped = 0
                for acc, offline_s in offline_accs:
                    if not folder_pool:
                        break
                    replacement = folder_pool.pop(0)
                    off_uname = acc.get("username", "")
                    rep_uname = replacement.get("username", "")
                    config_id = acc.get("config_id", "")

                    # Move offline acc to folder
                    off_result = move_accounts_to_folder([acc], folder_from_id)
                    if off_result.get("moved", 0) == 0:
                        detail = f"Swap failed: couldn't move {off_uname} to folder"
                        log_action(name, "FAILED", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))
                        continue

                    # Move replacement to device
                    rep_result = move_accounts_to_device([replacement], dev_id, config_id=config_id, enable=True)
                    if rep_result.get("moved", 0) == 0:
                        detail = f"Swap failed: moved {off_uname} out but couldn't move {rep_uname} in"
                        log_action(name, "FAILED", detail)
                        actions.append((datetime.now().strftime("%H:%M:%S"), name, "FAILED", detail))
                        continue

                    used_this_cycle.add(rep_uname)
                    swapped += 1

                    if offline_s == float('inf'):
                        dur = "never"
                    else:
                        h = int(offline_s // 3600)
                        m = int((offline_s % 3600) // 60)
                        dur = f"{h}h{m}m"

                    detail = f"Swapped {off_uname} (offline {dur}) -> {rep_uname} (from {group_name} folder)"
                    log_action(name, "SWAP OFFLINE", detail)
                    actions.append((datetime.now().strftime("%H:%M:%S"), name, "SWAP OFFLINE", detail))

                if swapped > 0:
                    # Summary log if multiple swaps
                    if swapped > 1:
                        remaining_offline = len(offline_accs) - swapped
                        detail = f"Swapped {swapped} offline >3h accounts ({remaining_offline} remaining without replacement)"
                        if remaining_offline <= 0:
                            detail = f"Swapped {swapped} offline >3h accounts (all handled)"
                        log_action(name, "SWAP OFFLINE", detail)

    # Print table
    print_table(devices, cycle_num, actions)
    return actions


def main():
    load_api_key()
    _load_account_livetime()
    stock_groups = ", ".join(STOCK_GROUPS)
    print("[OK] FarmSync Automation starting...")
    print(f"[OK] Cycle interval: {CYCLE_INTERVAL // 60} minutes")
    print(f"[OK] Rules: Disk>={DISK_FULL_PCT*100:.0f}%->RestartTool | 0 online+heartbeat<{HEARTBEAT_FRESH//60}min->ClearTasks+RestartVPS | 0 online+stale->Skip | RAM>{RAM_HIGH*100:.0f}%->MoveAccounts | <{ONLINE_WARN*100:.0f}% x{ONLINE_WARN_CYCLES}->Relogin | <{ONLINE_LOW*100:.0f}% x{ONLINE_LOW_CYCLES}->Relogin | Offline>{SWAP_OFFLINE_THRESHOLD//3600}h->SwapFromFolder")
    print(f"[OK] Auto-fix: enable all device accounts + assign config per group each cycle")
    print(f"[OK] Fill: only when online>90% | max {FILL_MAX_PER_CYCLE}/device/cycle")
    print(f"[OK] Pre-stock: {FOLDER_STOCK_AMOUNT} accs into empty folders for [{stock_groups}]")
    print(f"[OK] Logging to: {LOG_FILE}")
    print()
    write_log("")
    write_log(f"{'#' * 90}")
    write_log(f"  FarmSync Automation started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    write_log(f"  Cycle: {CYCLE_INTERVAL // 60}min | RAM>{RAM_HIGH*100:.0f}% | Disk>{DISK_FULL_PCT*100:.0f}% | Online<{ONLINE_LOW*100:.0f}% x{ONLINE_LOW_CYCLES}")
    write_log(f"  Fill: only when online>90% | Pre-stock: {FOLDER_STOCK_AMOUNT} accs [{stock_groups}]")
    write_log(f"{'#' * 90}")

    cycle = 0
    while True:
        cycle += 1
        try:
            run_cycle(cycle)
        except RuntimeError as e:
            # Raised by _curl for HTTP errors, timeouts, transport failures
            clear_screen()
            msg = str(e)
            print(f"[ERROR] {msg}")
            write_log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR   | Cycle #{cycle} | {msg[:200]}")
            if "HTTP 401" in msg:
                print("  -> Check your API key in api_keys.txt")
            elif "HTTP 503" in msg:
                print("  -> FarmSync server is down. Will retry next cycle.")
            elif "timeout" in msg.lower():
                print("  -> Request timed out. Will retry next cycle.")
            elif "curl not found" in msg:
                print("  -> curl executable not in PATH. Install curl or fix PATH.")
        except Exception as e:
            clear_screen()
            print(f"[ERROR] Unexpected: {e}")
            write_log(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR   | Cycle #{cycle} | {e}")

        # Countdown
        for remaining in range(CYCLE_INTERVAL, 0, -1):
            mins, secs = divmod(remaining, 60)
            print(f"\r  Next cycle in {mins:02d}:{secs:02d}...", end="", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[STOPPED] Automation stopped.")
        write_log(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] STOPPED | User stopped automation")
        if action_log:
            print(f"\nSession log ({len(action_log)} actions):")
            for ts, dname, action, detail in action_log:
                print(f"  [{ts}] {dname:<15} {action:<20} {detail}")
    except Exception as e:
        print(f"\n\n[CRASH] {e}")
        write_log(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] CRASH   | {e}")
        import traceback
        traceback.print_exc()
