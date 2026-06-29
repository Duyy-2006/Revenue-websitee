"""Shared config for FarmSync API scripts."""
import os
import sys

BASE_URL = "https://api.farmsync.cloud"
_api_key = None


def load_api_key():
    global _api_key
    if _api_key:
        return _api_key
    key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_keys.txt")
    if not os.path.exists(key_file):
        print("[ERROR] api_keys.txt not found!")
        sys.exit(1)
    with open(key_file, "r", encoding="utf-8") as f:
        _api_key = f.readline().strip()
    if not _api_key:
        print("[ERROR] api_keys.txt is empty!")
        sys.exit(1)
    return _api_key


def headers():
    return {"Authorization": f"Bearer {load_api_key()}", "Content-Type": "application/json"}
