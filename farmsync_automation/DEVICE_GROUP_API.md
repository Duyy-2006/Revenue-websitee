# Moving a device between groups (FarmSync API)

**Goal:** reassign a device from one device-group to another (e.g. move **device 70 → Blox Fruits**,
out of Murder Mystery 2). FarmSync exposes a dedicated endpoint for this; verified 2026-06-26.

```
PUT https://api.farmsync.cloud/api/self/device-groups/device
Authorization: Bearer <key>
Content-Type: application/json

{ "device_id": "<device uuid>", "group_id": "<target group uuid>" }
```

- **PUT only** — `POST`/`PATCH` return `405 Method Not Allowed`.
- Overwrites the device's group (a device belongs to exactly one group). To "move", you just point it
  at a different `group_id`.
- A device record carries **both** `group_id` (the FK you set) and `group_name` (denormalized,
  read-only — it follows `group_id`).

> ⚠️ **Not purely cosmetic.** On its next cycle the running automation applies the *new* group's config
> and, if that group has an assigned backup (`_group_backups.json`), **re-images the device** from it.
> So a group move can trigger a re-clone. Move deliberately; revert is easy but the re-clone is not.

---

## Auth & base URL

| | |
|---|---|
| Base URL | `https://api.farmsync.cloud` |
| Auth header | `Authorization: Bearer <key>` |
| Key source | first line of `farmsync_automation/api_keys.txt` |

Use **`curl`**, not Python `requests` — `requests` takes 30-45 s/call to `api.farmsync.cloud` on this
box (SChannel OCSP); curl returns in <1 s. Same reason `automation.py` wraps everything in `_curl()`.

---

## Step 1 — get the device id and the target group id

**Groups** (`GET /api/self/device-groups/` → `{ "data": [ {id, name, …}, … ] }`):

```bash
curl -s "https://api.farmsync.cloud/api/self/device-groups/" \
     -H "Authorization: Bearer $(head -1 farmsync_automation/api_keys.txt)"
```

**Devices** (`GET /api/devices/`) — each item has `id`, `device_note`, `device_name`, `group_id`,
`group_name`. Find the one you want by its note/name.

### Current groups — snapshot 2026-06-26

> Fetch live for the authoritative list; ids are stable unless a group is deleted/recreated.

| Group | `group_id` |
|---|---|
| Potion | `47d6d571-9b45-4b14-9de0-cd0b42ee39a4` |
| Pet Farm | `866caad8-c738-4bd4-aec4-044f99c85931` |
| **Murder Mystery 2** (MM2) | `61857191-fe36-46e9-8e6d-5af4e092060c` |
| 99NITF | `10503ea4-f87e-4ae5-80e5-aa268f9edb31` |
| BSS | `51af909c-2ada-41cd-84c4-2c98191426c6` |
| Grow A Garden 2 | `2bb5aa04-4a3c-496b-90f1-23605f81e74d` |
| **Blox Fruits** | `b66eed78-4118-485a-a841-bd42ffc92348` |

---

## Step 2 — move it

### Worked example: device 70 → Blox Fruits

`device 70` resolves to device-note **`Hoang70`** (device_name `Device 59`):

| | value |
|---|---|
| `device_id` | `190dddf1-6df7-42b7-9502-c2aa17a316bc` |
| from (current) | Murder Mystery 2 — `61857191-fe36-46e9-8e6d-5af4e092060c` |
| to (target) | Blox Fruits — `b66eed78-4118-485a-a841-bd42ffc92348` |

```bash
curl -X PUT "https://api.farmsync.cloud/api/self/device-groups/device" \
  -H "Authorization: Bearer $(head -1 farmsync_automation/api_keys.txt)" \
  -H "Content-Type: application/json" \
  -d '{"device_id":"190dddf1-6df7-42b7-9502-c2aa17a316bc","group_id":"b66eed78-4118-485a-a841-bd42ffc92348"}'
```

**Revert** (back to MM2): same call with `group_id` = `61857191-fe36-46e9-8e6d-5af4e092060c`.

**Verify:** re-`GET /api/devices/`, find `Hoang70`, confirm `group_name` == `"Blox Fruits"`.

---

## Ready-to-use Python helper (curl-based)

Resolves names → ids, moves, and verifies. Matches the repo's `_curl` pattern (no `requests`).

```python
import json, os, subprocess

BASE = "https://api.farmsync.cloud"
_KEY = None

def _api_key():
    global _KEY
    if not _KEY:
        f = os.path.join(os.path.dirname(__file__), "api_keys.txt")  # adjust if needed
        _KEY = open(f, encoding="utf-8").readline().strip()
    return _KEY

def _api(method, path, body=None, timeout=30):
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-X", method,
           "-H", f"Authorization: Bearer {_api_key()}", "-H", "Accept: application/json",
           "-w", "\n%{http_code}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    cmd.append(BASE + path)
    out = subprocess.run(cmd, capture_output=True).stdout.decode("utf-8", "ignore")
    raw, _, code = out.rpartition("\n")
    if not code.strip().startswith("2"):
        raise RuntimeError(f"HTTP {code.strip()}: {raw[:200]}")
    return json.loads(raw) if raw.strip() else None

def _unwrap(d):
    if isinstance(d, list):
        return d
    for k in ("data", "items", "results"):
        if isinstance(d.get(k), list):
            return d[k]
    return d

def get_groups():
    """name -> group_id"""
    return {g["name"]: g["id"] for g in _unwrap(_api("GET", "/api/self/device-groups/"))}

def find_device(label):
    """Find a device by its note or name (exact match), e.g. 'Hoang70'."""
    for d in _unwrap(_api("GET", "/api/devices/")):
        if label in (str(d.get("device_note") or "").strip(), str(d.get("device_name") or "").strip()):
            return d
    raise LookupError(f"no device matching {label!r}")

def move_device_to_group(device_label, group_name):
    """Move a device (by note/name) into a group (by name). Returns the new group_name (verified)."""
    dev = find_device(device_label)
    gid = get_groups()[group_name]
    _api("PUT", "/api/self/device-groups/device", {"device_id": dev["id"], "group_id": gid})
    after = find_device(device_label)            # verify
    if after.get("group_name") != group_name:
        raise RuntimeError(f"move not reflected: still {after.get('group_name')!r}")
    return after["group_name"]

# Example:
#   move_device_to_group("Hoang70", "Blox Fruits")
```

---

## Full device / device-group route table

Reverse-engineered from `app.farmsync.cloud`'s JS bundles.

**Devices**

| Action | Method + path |
|---|---|
| List | `GET /api/devices/` |
| Get one | `GET /api/devices/{id}` |
| Update one | `PUT /api/devices/{id}` *(full-object PUT — riskier; for group changes use the group endpoint below)* |
| Bulk update | `PUT /api/devices/` |
| Bulk delete | `DELETE /api/devices/bulk` |
| Accounts on device | `GET /api/devices/{id}/accounts` |
| Tags | `… /api/devices/{id}/tags`, bulk `… /api/devices/tags/bulk` |
| Reset device key | `… /api/devices/{id}/reset-key` |

**Device groups**

| Action | Method + path |
|---|---|
| List | `GET /api/self/device-groups/` |
| Create | `POST /api/self/device-groups` |
| Update | `PUT /api/self/device-groups/{id}` |
| Delete | `DELETE /api/self/device-groups/{id}` |
| **Move device → group** | `PUT /api/self/device-groups/device` `{device_id, group_id}` |
| Bulk group ops | `… /api/self/device-groups/bulk` |

Parallel CRUD families exist with the same shape: `smart-groups`, `folder-groups`, `script-groups`,
`autochange-config-groups`, `configs`.

---

## How this was verified (2026-06-26)

- Listed groups + devices; confirmed **device 70 = `Hoang70`** (`190dddf1-…`), currently
  `group_id 61857191-…` = **Murder Mystery 2**.
- Probed the move endpoint with **fake UUIDs** (so nothing real moved):
  - `PUT /api/self/device-groups/device {device_id, group_id}` → `404 {"message":"Device not found","success":false}`
    — proves the method + body shape are correct (it parsed the body and looked up the device).
  - `POST` / `PATCH` same path → `405 Method Not Allowed` — so **PUT only**.
- Device 70's real record was never modified.

## Caveats

- **PUT only.** `POST`/`PATCH` → 405.
- **You set `group_id`, not `group_name`.** `group_name` on the device record is read-only and follows
  `group_id`.
- **The move can trigger a re-clone** via the automation's group config/backup enforcement (see warning
  up top). It's not a no-op.
- Group/device UUIDs above are this account's snapshot on 2026-06-26 — fetch live before scripting.
