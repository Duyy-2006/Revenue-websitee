"""One-shot: move all accounts in [Fisch, MM2, BSS, Fish It] to unassigned, then delete the folders."""
import requests
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import BASE_URL, headers

TARGETS = {"fisch", "mm2", "bss", "fish it"}


def save_account_data(acc):
    return {
        "username": acc.get("username", ""),
        "password": acc.get("password", ""),
        "cookie": acc.get("cookie", ""),
        "config_id": acc.get("config_id", ""),
        "private_server_link": acc.get("private_server_link", ""),
        "enabled": False,
    }


def move_to_unassigned(acc):
    uname = acc.get("username", "")
    saved = save_account_data(acc)
    requests.delete(f"{BASE_URL}/api/self/accounts/{uname}", headers=headers(), timeout=15).raise_for_status()
    saved["device_id"] = ""
    saved["folder_id"] = ""
    saved["unassigned"] = True
    resp = requests.post(f"{BASE_URL}/api/self/accounts", headers=headers(), json=saved, timeout=15)
    resp.raise_for_status()


def delete_folder(folder_id):
    resp = requests.delete(f"{BASE_URL}/api/self/folders/{folder_id}", headers=headers(), timeout=15)
    return resp.status_code, resp.text


def main():
    print("Fetching folders and accounts...")
    folders = requests.get(f"{BASE_URL}/api/self/folders/", headers=headers(), timeout=15).json()
    accounts = requests.get(f"{BASE_URL}/api/self/accounts/", headers=headers(), timeout=15).json()

    targets = [f for f in folders if (f.get("folder_name") or "").strip().lower() in TARGETS]
    found_names = {(f.get("folder_name") or "").strip().lower() for f in targets}
    missing = TARGETS - found_names
    if missing:
        print(f"[WARN] not found: {missing}")

    plan = []
    for f in targets:
        fid = f.get("id", "")
        from_id = f.get("from_child_folder_id", "")
        to_id = f.get("to_child_folder_id", "")
        accs = [a for a in accounts if a.get("folder_id") in (fid, from_id, to_id)]
        plan.append((f, accs))

    print(f"\n{'='*70}")
    print("PLAN")
    print(f"{'='*70}")
    total_accs = 0
    for f, accs in plan:
        name = f.get("folder_name", "?")
        print(f"  {name:<10} ({f.get('id','?')[:8]}...) - {len(accs)} accounts -> unassigned, then DELETE folder")
        total_accs += len(accs)
    print(f"\nTotal: {len(plan)} folders, {total_accs} accounts to move")

    confirm = input("\nProceed? (y/n): ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    # Step 1: move accounts to unassigned
    for f, accs in plan:
        name = f.get("folder_name", "?")
        if not accs:
            continue
        print(f"\nMoving {len(accs)} accounts from '{name}' -> unassigned...")
        ok, fail = 0, 0
        for acc in accs:
            try:
                move_to_unassigned(acc)
                ok += 1
            except Exception as e:
                fail += 1
                print(f"  [FAIL] {acc.get('username','?')}: {e}")
        print(f"  Done: {ok} moved, {fail} failed")

    # Step 2: delete folders (try children first, then parent)
    print(f"\nDeleting folders...")
    for f, _ in plan:
        name = f.get("folder_name", "?")
        fid = f.get("id", "")
        from_id = f.get("from_child_folder_id", "")
        to_id = f.get("to_child_folder_id", "")
        for child_id, label in ((from_id, "from_child"), (to_id, "to_child")):
            if child_id:
                code, body = delete_folder(child_id)
                print(f"  {name} {label} ({child_id[:8]}...): HTTP {code}")
        if fid:
            code, body = delete_folder(fid)
            print(f"  {name} parent  ({fid[:8]}...): HTTP {code} {body[:100] if code >= 400 else ''}")

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except requests.HTTPError as e:
        print(f"[ERROR] HTTP {e.response.status_code}: {e.response.text[:200]}")
