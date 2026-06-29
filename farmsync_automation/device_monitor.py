import requests
import time
import os
import sys
from datetime import datetime

BASE_URL = "https://api.farmsync.cloud"
REFRESH_INTERVAL = 60  # seconds between updates

def load_api_key():
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.txt")
    if not os.path.exists(key_file):
        print("[ERROR] api_keys.txt not found!")
        sys.exit(1)
    with open(key_file, "r", encoding="utf-8") as f:
        key = f.readline().strip()
    if not key:
        print("[ERROR] api_keys.txt is empty!")
        sys.exit(1)
    return key


def fetch_devices(api_key):
    for attempt in range(3):
        resp = requests.get(f"{BASE_URL}/api/devices/", headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
        if resp.status_code == 503:
            wait = 5 * (attempt + 1)
            print(f"  [503] Server unavailable, retrying in {wait}s... ({attempt + 1}/3)")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()


def main():
    api_key = load_api_key()
    print(f"[OK] API key loaded ({api_key[:8]}...)")

    while True:
        try:
            devices = fetch_devices(api_key)
            if isinstance(devices, dict) and "error" in devices:
                os.system("cls" if os.name == "nt" else "clear")
                print(f"[ERROR] {devices['error']}")
                time.sleep(REFRESH_INTERVAL)
                continue
            if not isinstance(devices, list):
                devices = [devices]

            # Sort by device_note/device_name
            devices.sort(key=lambda d: (d.get("device_note") or d.get("device_name") or "").lower())

            # Calculate column widths
            rows = []
            for i, dev in enumerate(devices, 1):
                name = (dev.get("device_note") or dev.get("device_name") or "Unnamed").strip()
                group = (dev.get("group_name") or "None").strip()
                active = dev.get("active_accounts", 0)
                total = dev.get("total_accounts", 0)
                accounts = f"{active}/{total}"

                ram_free = dev.get("sys_ram_free_gb", 0)
                ram_total = dev.get("sys_ram_total_gb", 0)
                ram_used = ram_total - ram_free
                ram = f"{ram_used:.0f}/{ram_total:.0f} GB" if ram_total else "N/A"

                cpu_name = dev.get("sys_cpu_name", "")
                cores_p = dev.get("sys_cpu_cores_physical", 0)
                cores_l = dev.get("sys_cpu_cores_logical", 0)
                cpu = f"{cores_p}P/{cores_l}L" if cores_l else "N/A"

                disk_free = dev.get("sys_disk_free_gb", 0)
                disk_total = dev.get("sys_disk_total_gb", 0)
                disk_used = disk_total - disk_free
                disk = f"{disk_used:.0f}/{disk_total:.0f} GB" if disk_total else "N/A"

                client = dev.get("client_running", "")
                enabled = dev.get("is_enabled", False)
                if not enabled:
                    status = "DISABLED"
                elif client:
                    status = "ONLINE"
                else:
                    status = "OFFLINE"

                rows.append((str(i), name, group, accounts, ram, cpu, disk, status))

            headers = ("#", "Device Name", "Group", "Online/Total", "RAM Used/Total", "CPU", "Disk Used/Total", "Status")

            # Calculate widths
            widths = []
            for col in range(len(headers)):
                w = len(headers[col])
                for row in rows:
                    w = max(w, len(row[col]))
                widths.append(w)

            # Build table
            def fmt_row(vals):
                parts = []
                for v, w in zip(vals, widths):
                    parts.append(f" {v:<{w}} ")
                return "|" + "|".join(parts) + "|"

            sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

            os.system("cls" if os.name == "nt" else "clear")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # Summary
            total_devices = len(devices)
            online = sum(1 for d in devices if d.get("client_running") and d.get("is_enabled"))
            total_acc = sum(d.get("total_accounts", 0) for d in devices)
            active_acc = sum(d.get("active_accounts", 0) for d in devices)
            total_ram = sum(d.get("sys_ram_total_gb", 0) for d in devices)
            used_ram = total_ram - sum(d.get("sys_ram_free_gb", 0) for d in devices)

            print(f"  FarmSync Monitor | {now} | Refresh: {REFRESH_INTERVAL}s")
            print(f"  Devices: {online}/{total_devices} online | Accounts: {active_acc}/{total_acc} active | RAM: {used_ram:.0f}/{total_ram:.0f} GB")
            print()

            # Table
            print(sep)
            print(fmt_row(headers))
            print(sep)
            for row in rows:
                print(fmt_row(row))
            print(sep)

            print(f"\n  Next refresh in {REFRESH_INTERVAL}s... (Ctrl+C to stop)")

        except requests.exceptions.HTTPError as e:
            os.system("cls" if os.name == "nt" else "clear")
            status = e.response.status_code if e.response is not None else "?"
            print(f"[ERROR] HTTP {status}: {e}")
            if status == 401:
                print("  -> Check your API key in api_keys.txt")
            elif status == 503:
                print("  -> FarmSync server is down. Will retry next cycle.")
        except requests.exceptions.ConnectionError:
            os.system("cls" if os.name == "nt" else "clear")
            print("[ERROR] Connection failed. Check your internet.")
        except requests.exceptions.Timeout:
            os.system("cls" if os.name == "nt" else "clear")
            print("[ERROR] Request timed out.")

        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n[STOPPED] Device monitor stopped.")
