"""Full summary of all folders with account breakdown."""
import requests
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, load_api_key, headers


def folder_summary():
    # Get all folders
    resp = requests.get(f"{BASE_URL}/api/self/folders/", headers=headers(), timeout=15)
    resp.raise_for_status()
    folders = resp.json()

    # Get all accounts
    resp = requests.get(f"{BASE_URL}/api/self/accounts/", headers=headers(), timeout=15)
    resp.raise_for_status()
    all_accounts = resp.json()

    # Build folder_id -> account count map
    folder_map = {}
    for a in all_accounts:
        fid = a.get("folder_id")
        if fid:
            if fid not in folder_map:
                folder_map[fid] = {"total": 0, "enabled": 0, "running": 0, "dead": 0}
            folder_map[fid]["total"] += 1
            if a.get("enabled"):
                folder_map[fid]["enabled"] += 1
            if a.get("running"):
                folder_map[fid]["running"] += 1
            if a.get("dead_cookie"):
                folder_map[fid]["dead"] += 1

    rows = []
    for f in folders:
        name = f.get("folder_name") or "(unnamed)"
        from_id = f.get("from_child_folder_id", "")
        to_id = f.get("to_child_folder_id", "")

        from_stats = folder_map.get(from_id, {"total": 0, "enabled": 0, "running": 0, "dead": 0})
        to_stats = folder_map.get(to_id, {"total": 0, "enabled": 0, "running": 0, "dead": 0})

        from_str = f"{from_stats['running']}/{from_stats['total']}"
        to_str = f"{to_stats['running']}/{to_stats['total']}"
        dead = from_stats["dead"] + to_stats["dead"]
        total = from_stats["total"] + to_stats["total"]

        rows.append((name, from_str, to_str, str(total), str(dead), str(f.get("type", 0))))

    hdrs = ("Folder Name", "From (run/total)", "To (run/total)", "Total Accounts", "Dead Cookies", "Type")
    widths = [max(len(hdrs[c]), max((len(r[c]) for r in rows), default=0)) for c in range(len(hdrs))]

    def fmt(vals):
        return "| " + " | ".join(f"{v:<{w}}" for v, w in zip(vals, widths)) + " |"

    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

    print("FarmSync Folder Summary")
    print("=" * 60)
    print(sep)
    print(fmt(hdrs))
    print(sep)
    for r in rows:
        print(fmt(r))
    print(sep)

    total_accounts = sum(int(r[3]) for r in rows)
    total_dead = sum(int(r[4]) for r in rows)
    print(f"\nTotal: {len(folders)} folders | {total_accounts} accounts | {total_dead} dead cookies")


if __name__ == "__main__":
    folder_summary()
