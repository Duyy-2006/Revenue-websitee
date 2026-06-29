"""List all accounts in a folder (from + to child folders)."""
import requests
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, load_api_key, headers


def folder_accounts(folder_id):
    # Get folder
    resp = requests.get(f"{BASE_URL}/api/self/folders/{folder_id}", headers=headers(), timeout=15)
    resp.raise_for_status()
    folder = resp.json()

    from_id = folder.get("from_child_folder_id")
    to_id = folder.get("to_child_folder_id")

    # Get all accounts
    resp = requests.get(f"{BASE_URL}/api/self/accounts/", headers=headers(), timeout=15)
    resp.raise_for_status()
    all_accounts = resp.json()

    from_accounts = [a for a in all_accounts if a.get("folder_id") == from_id]
    to_accounts = [a for a in all_accounts if a.get("folder_id") == to_id]

    print(f"Folder: {folder.get('folder_name', '(unnamed)')}")
    print(f"{'=' * 60}")

    def print_table(title, accounts):
        if not accounts:
            print(f"\n{title}: 0 accounts")
            return

        rows = []
        for a in accounts:
            enabled = "ON" if a.get("enabled") else "OFF"
            dead = "DEAD" if a.get("dead_cookie") else "OK"
            running = "RUN" if a.get("running") else "-"
            device = (a.get("device_id") or "unassigned")[:16]
            error = a.get("error") or ""
            rows.append((
                a.get("username", "?"),
                enabled,
                dead,
                running,
                device,
                error[:30],
            ))

        hdrs = ("Username", "Enabled", "Cookie", "Running", "Device ID", "Error")
        widths = [max(len(hdrs[c]), max(len(r[c]) for r in rows)) for c in range(len(hdrs))]

        def fmt(vals):
            return "| " + " | ".join(f"{v:<{w}}" for v, w in zip(vals, widths)) + " |"

        sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"

        print(f"\n{title}: {len(accounts)} accounts")
        print(sep)
        print(fmt(hdrs))
        print(sep)
        for r in rows:
            print(fmt(r))
        print(sep)

    print_table("FROM (source)", from_accounts)
    print_table("TO (destination)", to_accounts)

    total = len(from_accounts) + len(to_accounts)
    print(f"\nTotal accounts in folder: {total}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python folder_accounts.py <folder_id>")
        print("\nRun list_folders.py to see available folder IDs.")
        sys.exit(1)
    folder_accounts(sys.argv[1])
