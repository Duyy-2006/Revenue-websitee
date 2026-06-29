"""List all folders with account counts."""
import requests
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, load_api_key, headers


def list_folders():
    resp = requests.get(f"{BASE_URL}/api/self/folders/", headers=headers(), timeout=15)
    resp.raise_for_status()
    folders = resp.json()

    if not folders:
        print("No folders found.")
        return

    # Table
    rows = []
    for i, f in enumerate(folders, 1):
        rows.append((
            str(i),
            f.get("folder_name") or "(unnamed)",
            str(f.get("total_accounts_from", 0)),
            str(f.get("total_accounts_to", 0)),
            str(f.get("type", 0)),
            f.get("group_id") or "None",
            f.get("id", ""),
        ))

    hdrs = ("#", "Folder Name", "Accounts From", "Accounts To", "Type", "Group ID", "Folder ID")
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
    print(f"\nTotal: {len(folders)} folders")


if __name__ == "__main__":
    list_folders()
