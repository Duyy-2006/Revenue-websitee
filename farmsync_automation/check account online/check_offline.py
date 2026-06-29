"""Check offline accounts on Potion/Pet Farm devices.
Accounts offline >3 hours are swapped with the newest account from the from_child folder.
"""
import requests
import os
import sys
import time
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, headers

CONFIG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "farmsync_automation", "config.json")
with open(CONFIG_FILE, "r", encoding="utf-8") as _f:
    _CFG = json.load(_f)
_check = _CFG.get("check_offline", {})

OFFLINE_THRESHOLD = _check.get("offline_threshold_hours", 3) * 3600
TARGET_GROUPS = set(_check.get("target_groups", ["Potion", "Pet Farm"]))
DISCORD_WEBHOOK = _CFG.get("discord_webhook", "")


def format_duration(seconds):
    if seconds <= 0:
        return "just now"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m"
    return f"{m}m"


def print_table(title, hdrs, table_rows):
    if not table_rows:
        print(f"\n{title}: (none)")
        return
    widths = [max(len(hdrs[c]), max((len(r[c]) for r in table_rows), default=0)) for c in range(len(hdrs))]
    def fmt(vals):
        return "| " + " | ".join(f"{v:<{w}}" for v, w in zip(vals, widths)) + " |"
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(f"\n{title} ({len(table_rows)}):")
    print(sep)
    print(fmt(hdrs))
    print(sep)
    for r in table_rows:
        print(fmt(r))
    print(sep)


def discord_post(payload):
    try:
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if resp.status_code == 429:
            print("  [DISCORD] Rate limited, skipping")
        elif resp.status_code not in (200, 204):
            print(f"  [DISCORD] Failed: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [DISCORD] Error: {e}")


def send_discord_scan(running_count, offline_count, offline_3h_count, folder_pools, group_stats):
    """Send scan summary embed showing current offline status."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = running_count + offline_count
    online_pct = (running_count / total * 100) if total > 0 else 0

    if offline_3h_count == 0:
        color = 0x57F287  # green
        status_text = "\u2705 All accounts healthy. No swaps needed."
    elif offline_3h_count <= 10:
        color = 0xFEE75C  # yellow
        status_text = f"\u26a0\ufe0f {offline_3h_count} accounts offline >3h, swap needed"
    else:
        color = 0xED4245  # red
        status_text = f"\U0001F6A8 {offline_3h_count} accounts offline >3h, swap needed"

    # Pool status per group
    pool_lines = []
    for gname in sorted(TARGET_GROUPS):
        pool = folder_pools.get(gname, [])
        stats = group_stats.get(gname, {})
        running = stats.get("running", 0)
        offline = stats.get("offline", 0)
        over_3h = stats.get("over_3h", 0)
        pool_lines.append(
            f"**{gname}**: {len(pool)} in pool | "
            f"{running} running | {offline} offline | {over_3h} >3h"
        )
    pool_text = "\n".join(pool_lines) if pool_lines else "No pools available"

    embed = {
        "title": "\U0001F50D Offline Account Scan",
        "color": color,
        "description": status_text,
        "fields": [
            {"name": "\U0001F464 Accounts", "value": f"**{running_count}**/{total} running ({online_pct:.0f}%)", "inline": True},
            {"name": "\U0001F534 Offline", "value": f"**{offline_count}** total | **{offline_3h_count}** >3h", "inline": True},
            {"name": "\U0001F4C1 Folder Pools", "value": pool_text, "inline": False},
        ],
        "footer": {"text": f"Potion + Pet Farm | {now}"},
    }
    discord_post({"embeds": [embed]})


def send_discord_swap_results(swap_results, failed_results, no_replacement_list):
    """Send swap results embed showing what was swapped."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    success_count = len(swap_results)
    failed_count = len(failed_results)
    no_rep_count = len(no_replacement_list)

    if failed_count == 0 and success_count > 0:
        color = 0x57F287  # green
    elif failed_count > 0 and success_count > 0:
        color = 0xFEE75C  # yellow
    elif failed_count > 0:
        color = 0xED4245  # red
    else:
        color = 0x5865F2  # blue

    desc_parts = []
    if success_count:
        desc_parts.append(f"\u2705 Swapped: **{success_count}**")
    if failed_count:
        desc_parts.append(f"\u274c Failed: **{failed_count}**")
    if no_rep_count:
        desc_parts.append(f"\U0001F4ED No replacement: **{no_rep_count}**")

    embed = {
        "title": f"\U0001F504 Account Swap Results ({success_count + failed_count} total)",
        "color": color,
        "description": " \u2022 ".join(desc_parts),
        "fields": [],
        "footer": {"text": f"Potion + Pet Farm | {now}"},
    }

    # Successful swaps grouped by device
    if swap_results:
        device_swaps = {}
        for off_name, rep_name, dev_name, group, dur in swap_results:
            device_swaps.setdefault(dev_name, []).append(
                f"\u2705 `{off_name}` ({dur}) \u2192 `{rep_name}`"
            )
        swap_lines = []
        for dev, lines in sorted(device_swaps.items()):
            swap_lines.append(f"**{dev}**:")
            swap_lines.extend(lines)
        text = "\n".join(swap_lines)
        if len(text) > 1024:
            text = text[:1020] + "\n..."
        embed["fields"].append({"name": "\U0001F504 Swapped", "value": text, "inline": False})

    # Failed swaps
    if failed_results:
        fail_lines = [f"\u274c `{off}` on **{dev}** ({dur})" for off, dev, dur in failed_results]
        text = "\n".join(fail_lines)
        if len(text) > 1024:
            text = text[:1020] + "\n..."
        embed["fields"].append({"name": "\u274c Failed", "value": text, "inline": False})

    # No replacement available
    if no_replacement_list:
        norep_lines = [f"\U0001F4ED `{uname}` on **{dev}** ({group}, {dur})" for uname, dev, group, dur in no_replacement_list]
        text = "\n".join(norep_lines)
        if len(text) > 1024:
            text = text[:1020] + "\n..."
        embed["fields"].append({"name": "\U0001F4ED No Replacement", "value": text, "inline": False})

    discord_post({"embeds": [embed]})


def delete_account(username):
    resp = requests.delete(f"{BASE_URL}/api/self/accounts/{username}", headers=headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def create_account(data):
    resp = requests.post(f"{BASE_URL}/api/self/accounts", headers=headers(), json=data, timeout=15)
    resp.raise_for_status()
    return resp.json()


def save_account_data(acc):
    return {
        "username": acc.get("username", ""),
        "password": acc.get("password", ""),
        "cookie": acc.get("cookie", ""),
        "config_id": acc.get("config_id", ""),
        "private_server_link": acc.get("private_server_link", ""),
        "enabled": acc.get("enabled", False),
    }


def swap_accounts(offline_acc, replacement_acc, device_id, folder_id, config_id):
    """Swap: move offline account to folder, move replacement to device."""
    off_uname = offline_acc.get("username", "")
    rep_uname = replacement_acc.get("username", "")

    # Step 1: Remove offline account from device -> create in folder
    off_saved = save_account_data(offline_acc)
    try:
        delete_account(off_uname)
    except Exception as e:
        print(f"  [FAIL] Delete {off_uname} from device: {e}")
        return False
    try:
        off_saved["device_id"] = ""
        off_saved["folder_id"] = folder_id
        off_saved["unassigned"] = True
        off_saved["enabled"] = False
        create_account(off_saved)
    except Exception as e:
        # Rollback: put it back on device
        print(f"  [FAIL] Create {off_uname} in folder, rolling back: {e}")
        try:
            off_saved["device_id"] = device_id
            off_saved["folder_id"] = offline_acc.get("folder_id", "")
            off_saved["unassigned"] = False
            off_saved["enabled"] = offline_acc.get("enabled", False)
            create_account(off_saved)
        except Exception:
            print(f"  [CRITICAL] Lost account {off_uname} during rollback!")
        return False

    # Step 2: Remove replacement from folder -> create on device
    rep_saved = save_account_data(replacement_acc)
    try:
        delete_account(rep_uname)
    except Exception as e:
        print(f"  [FAIL] Delete {rep_uname} from folder: {e}")
        return False
    try:
        rep_saved["device_id"] = device_id
        rep_saved["folder_id"] = ""
        rep_saved["unassigned"] = False
        rep_saved["enabled"] = True
        if config_id:
            rep_saved["config_id"] = config_id
        create_account(rep_saved)
    except Exception as e:
        # Rollback: put replacement back in folder
        print(f"  [FAIL] Create {rep_uname} on device, rolling back: {e}")
        try:
            rep_saved["device_id"] = ""
            rep_saved["folder_id"] = folder_id
            rep_saved["unassigned"] = True
            rep_saved["enabled"] = False
            create_account(rep_saved)
        except Exception:
            print(f"  [CRITICAL] Lost account {rep_uname} during rollback!")
        return False

    return True


def main():
    print("Fetching devices, accounts, folders, device groups...")
    devices = requests.get(f"{BASE_URL}/api/devices/", headers=headers(), timeout=15).json()
    accounts = requests.get(f"{BASE_URL}/api/self/accounts/", headers=headers(), timeout=15).json()
    folders = requests.get(f"{BASE_URL}/api/self/folders/", headers=headers(), timeout=15).json()
    groups_resp = requests.get(f"{BASE_URL}/api/self/device-groups/", headers=headers(), timeout=15).json()
    device_groups = groups_resp.get("data", groups_resp)

    # Build device lookup: id -> (name, group_name, group_id)
    dev_map = {}
    for d in devices:
        did = d.get("id", "")
        name = (d.get("device_note") or d.get("device_name") or "?").strip()
        group = (d.get("group_name") or "?").strip()
        group_id = d.get("group_id", "")
        dev_map[did] = (name, group, group_id)

    # Build group -> (from_child_folder_id, to_child_folder_id)
    folder_by_name = {}
    for f in folders:
        fname = (f.get("folder_name") or "").lower().strip()
        folder_by_name[fname] = f

    group_folder_map = {}
    for g in device_groups:
        gname = g.get("name", "")
        key = gname.lower().strip()
        matched = folder_by_name.get(key)
        if not matched:
            for fname, fdata in folder_by_name.items():
                if key in fname or fname in key:
                    matched = fdata
                    break
        if matched:
            group_folder_map[gname] = (
                matched.get("from_child_folder_id", ""),
                matched.get("to_child_folder_id", ""),
            )

    now_ms = time.time() * 1000
    now_s = time.time()

    # Filter to Potion/Pet Farm device accounts only
    target_accounts = []
    for acc in accounts:
        did = acc.get("device_id", "")
        if not did:
            continue
        _, group, _ = dev_map.get(did, ("?", "?", ""))
        if group not in TARGET_GROUPS:
            continue
        target_accounts.append(acc)

    # Separate running vs offline, calculate offline duration
    running_rows = []
    offline_rows = []
    offline_over_3h = []

    for acc in target_accounts:
        uname = acc.get("username", "?")
        did = acc.get("device_id", "")
        dev_name, group, _ = dev_map.get(did, ("?", "?", ""))
        running = acc.get("running", False)
        enabled = acc.get("enabled", False)
        dead_cookie = acc.get("dead_cookie", False)
        error = acc.get("error", "") or ""
        login_at = acc.get("login_at", 0) or 0

        en = "ON" if enabled else "OFF"
        ck = "DEAD" if dead_cookie else "OK"

        if running:
            running_rows.append((uname, dev_name, group, "RUNNING", en, ck, "-", error[:40]))
        else:
            if login_at > 0:
                offline_s = (now_ms - login_at) / 1000
            else:
                offline_s = float('inf')

            dur = "never" if offline_s == float('inf') else format_duration(offline_s)
            offline_rows.append((uname, dev_name, group, "OFFLINE", en, ck, dur, error[:40]))

            if offline_s >= OFFLINE_THRESHOLD:
                offline_over_3h.append((acc, dev_name, group, offline_s))

    # Sort offline by duration (longest first)
    offline_rows.sort(key=lambda r: r[6], reverse=True)
    offline_over_3h.sort(key=lambda r: r[3], reverse=True)

    # Per-group stats for Discord
    group_stats = {}
    for acc, dev_name, group, offline_s in offline_over_3h:
        group_stats.setdefault(group, {"running": 0, "offline": 0, "over_3h": 0})
        group_stats[group]["over_3h"] += 1
    for uname, dev_name, group, status, en, ck, dur, error in running_rows:
        group_stats.setdefault(group, {"running": 0, "offline": 0, "over_3h": 0})
        group_stats[group]["running"] += 1
    for uname, dev_name, group, status, en, ck, dur, error in offline_rows:
        group_stats.setdefault(group, {"running": 0, "offline": 0, "over_3h": 0})
        group_stats[group]["offline"] += 1

    # Stats
    total = len(target_accounts)
    print(f"\nPotion + Pet Farm device accounts: {total}")
    print(f"Running: {len(running_rows)} | Offline: {len(offline_rows)} | Offline >3h: {len(offline_over_3h)}")

    # Display offline table
    hdrs = ("Username", "Device", "Group", "Status", "Enabled", "Cookie", "Offline For", "Error")
    print_table("Offline accounts", hdrs, offline_rows)

    if not offline_over_3h:
        print("\nNo accounts offline >3 hours. Nothing to swap.")
        return

    # Build from_child folder account pools per group (sorted newest first by last_updated)
    folder_pools = {}  # group_name -> [acc, ...]
    for gname, (from_folder_id, _) in group_folder_map.items():
        if gname not in TARGET_GROUPS or not from_folder_id:
            continue
        pool = []
        for acc in accounts:
            if acc.get("folder_id") == from_folder_id and not acc.get("device_id"):
                if acc.get("dead_cookie"):
                    continue  # skip dead cookies
                pool.append(acc)
        # Sort by last_updated descending (newest first = most recently added to folder)
        pool.sort(key=lambda a: a.get("last_updated", "") or "", reverse=True)
        folder_pools[gname] = pool

    # Show folder pool status
    print("\nFrom-child folder pools:")
    for gname in TARGET_GROUPS:
        pool = folder_pools.get(gname, [])
        print(f"  {gname}: {len(pool)} available accounts")
        if pool:
            newest = pool[0]
            oldest = pool[-1]
            print(f"    Newest: {newest.get('username', '?')} (updated: {newest.get('last_updated', '?')})")
            print(f"    Oldest: {oldest.get('username', '?')} (updated: {oldest.get('last_updated', '?')})")

    # Send scan summary to Discord
    send_discord_scan(len(running_rows), len(offline_rows), len(offline_over_3h), folder_pools, group_stats)

    # Confirm swap
    print(f"\n{'='*60}")
    print(f"Found {len(offline_over_3h)} accounts offline >3 hours to swap.")
    print(f"{'='*60}")

    swap_hdrs = ("Username", "Device", "Group", "Offline For", "Replacement")
    swap_preview = []
    swap_plan = []  # (offline_acc, replacement_acc, device_id, folder_id, config_id)

    for acc, dev_name, group, offline_s in offline_over_3h:
        pool = folder_pools.get(group, [])
        dur = "never" if offline_s == float('inf') else format_duration(offline_s)

        if pool:
            replacement = pool[0]
            rep_name = replacement.get("username", "?")
            swap_preview.append((acc.get("username", "?"), dev_name, group, dur, rep_name))

            device_id = acc.get("device_id", "")
            from_folder_id, _ = group_folder_map.get(group, ("", ""))
            config_id = acc.get("config_id", "")
            swap_plan.append((acc, replacement, device_id, from_folder_id, config_id))

            # Remove used replacement from pool
            pool.pop(0)
        else:
            swap_preview.append((acc.get("username", "?"), dev_name, group, dur, "(no replacement)"))

    print_table("Swap plan", swap_hdrs, swap_preview)

    actual_swaps = [s for s in swap_plan]
    no_replacement_list = []
    for acc, dev_name, group, offline_s in offline_over_3h:
        dur = "never" if offline_s == float('inf') else format_duration(offline_s)
        if not any(s[0].get("username") == acc.get("username") for s in actual_swaps):
            no_replacement_list.append((acc.get("username", "?"), dev_name, group, dur))

    if no_replacement_list:
        print(f"\n  {len(no_replacement_list)} accounts have no replacement available in folder pool.")

    if not actual_swaps:
        print("\nNo swaps possible (folder pools empty).")
        send_discord_swap_results([], [], no_replacement_list)
        return

    confirm = input(f"\nProceed with {len(actual_swaps)} swaps? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    # Execute swaps
    print(f"\nSwapping {len(actual_swaps)} accounts...")
    swap_results = []     # (off_name, rep_name, dev_name, group, dur)
    failed_results = []   # (off_name, dev_name, dur)

    for offline_acc, replacement_acc, device_id, folder_id, config_id in actual_swaps:
        off_name = offline_acc.get("username", "?")
        rep_name = replacement_acc.get("username", "?")
        did = offline_acc.get("device_id", "")
        dev_name, group, _ = dev_map.get(did, ("?", "?", ""))
        login_at = offline_acc.get("login_at", 0) or 0
        offline_s = (now_ms - login_at) / 1000 if login_at > 0 else float('inf')
        dur = "never" if offline_s == float('inf') else format_duration(offline_s)

        print(f"  {off_name} -> folder | {rep_name} -> device ...", end=" ")

        ok = swap_accounts(offline_acc, replacement_acc, device_id, folder_id, config_id)
        if ok:
            print("OK")
            swap_results.append((off_name, rep_name, dev_name, group, dur))
        else:
            print("FAILED")
            failed_results.append((off_name, dev_name, dur))

    print(f"\nDone. Swapped: {len(swap_results)} | Failed: {len(failed_results)}")

    # Send swap results to Discord
    time.sleep(1)
    send_discord_swap_results(swap_results, failed_results, no_replacement_list)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except requests.exceptions.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}: {e}")
    except Exception as e:
        print(f"[ERROR] {e}")
