"""Get a single folder by ID with its accounts."""
import requests
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import BASE_URL, load_api_key, headers


def get_folder(folder_id):
    # Get folder details
    resp = requests.get(f"{BASE_URL}/api/self/folders/{folder_id}", headers=headers(), timeout=15)
    resp.raise_for_status()
    folder = resp.json()

    print(f"Folder: {folder.get('folder_name', '(unnamed)')}")
    print(f"ID:     {folder.get('id')}")
    print(f"Type:   {folder.get('type')}")
    print(f"Group:  {folder.get('group_id') or 'None'}")
    print(f"From Child Folder: {folder.get('from_child_folder_id')}")
    print(f"To Child Folder:   {folder.get('to_child_folder_id')}")
    print(f"Accounts From: {folder.get('total_accounts_from', 0)}")
    print(f"Accounts To:   {folder.get('total_accounts_to', 0)}")

    # Get accounts in from_child_folder
    from_id = folder.get("from_child_folder_id")
    to_id = folder.get("to_child_folder_id")

    resp = requests.get(f"{BASE_URL}/api/self/accounts/", headers=headers(), timeout=15)
    resp.raise_for_status()
    all_accounts = resp.json()

    from_accounts = [a for a in all_accounts if a.get("folder_id") == from_id]
    to_accounts = [a for a in all_accounts if a.get("folder_id") == to_id]

    if from_accounts:
        print(f"\n--- FROM accounts ({len(from_accounts)}) ---")
        for a in from_accounts[:20]:
            status = "enabled" if a.get("enabled") else "disabled"
            device = a.get("device_id", "unassigned") or "unassigned"
            print(f"  {a.get('username', '?'):<25} {status:<10} device:{device[:16]}")
        if len(from_accounts) > 20:
            print(f"  ... and {len(from_accounts) - 20} more")

    if to_accounts:
        print(f"\n--- TO accounts ({len(to_accounts)}) ---")
        for a in to_accounts[:20]:
            status = "enabled" if a.get("enabled") else "disabled"
            device = a.get("device_id", "unassigned") or "unassigned"
            print(f"  {a.get('username', '?'):<25} {status:<10} device:{device[:16]}")
        if len(to_accounts) > 20:
            print(f"  ... and {len(to_accounts) - 20} more")

    if not from_accounts and not to_accounts:
        print("\nNo accounts in this folder.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python get_folder.py <folder_id>")
        print("\nRun list_folders.py to see available folder IDs.")
        sys.exit(1)
    get_folder(sys.argv[1])
