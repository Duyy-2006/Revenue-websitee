# Uploading `.ldbk` backups to FarmSync

**Answer: YES — `.ldbk` files can be uploaded to FarmSync via its API.** This was verified
end-to-end on 2026-06-26 (uploaded a test backup, confirmed it appeared in the backup list,
then deleted it — your real backups `724.ldbk` / `725...ldbk` were untouched).

`.ldbk` = an **LDPlayer backup** (a full image of an emulator instance). FarmSync stores them
on **Cloudflare R2** (S3-compatible object storage) and can re-image any device from one via a
`Backup` task. The upload uses a standard **presigned-URL** flow: ask the API for a one-time R2
upload URL, `PUT` the file straight to R2, then tell the API to register it.

---

## TL;DR — the 3-step flow

```
1. POST /api/s3/presign/file   {filename, content_type, type:"backup", size}  -> {key, put_url, get_url}
2. PUT  <put_url>              <raw .ldbk bytes>                               -> 200   (direct to R2)
3. POST /api/s3/confirm/file    {key}                                          -> {ok:true, ...}
```

After step 3 the file shows up in `GET /api/s3/files?type=backup` and can be applied to any device.

> ⚠️ **The one gotcha that matters:** the field that routes the file into the **backup** folder is
> **`type`**, *not* `file_type`. `{"type":"backup"}` → lands in `backup/` (a real, usable backup).
> `{"file_type":"backup"}`, `{"folder":"backup"}`, or omitting it → lands in `workspace/`, which is
> **not** a device backup and will never appear in `?type=backup`. (Discovered the hard way during
> testing — see "How this was verified".)

---

## Auth & base URL

| | |
|---|---|
| Base URL | `https://api.farmsync.cloud` |
| Auth header | `Authorization: Bearer <key>` |
| Key source | first line of `farmsync_automation/api_keys.txt` (same key the automation/dashboard already use) |
| Storage backend | Cloudflare R2 — `…r2.cloudflarestorage.com/farmsync/user/<user_id>/backup/…` |

> On this Windows box, prefer **`curl` subprocess** over Python `requests` — `requests` takes 30-45 s
> per call to `api.farmsync.cloud` (SChannel OCSP revocation check); `curl` returns in <1 s. This is
> the same reason `automation.py` wraps everything in `_curl()`.

---

## Step-by-step

### 1. Presign — get a one-time R2 upload URL

```
POST https://api.farmsync.cloud/api/s3/presign/file
Authorization: Bearer <key>
Content-Type: application/json

{
  "filename":     "Delta-726-VIP-Login-V1.ldbk",   // REQUIRED ("filename required" if missing)
  "type":         "backup",                          // REQUIRED for a backup (see gotcha above)
  "content_type": "application/octet-stream",
  "size":         146066071                           // bytes
}
```

Response:

```json
{
  "key":     "user/<user_id>/backup/20260626T092326_Delta-726-VIP-Login-V1.ldbk",
  "put_url": "https://<hash>.r2.cloudflarestorage.com/farmsync/.../...ldbk?X-Amz-Algorithm=...&X-Amz-Expires=900&...",
  "get_url": "https://<hash>.r2.cloudflarestorage.com/farmsync/.../...ldbk?...&x-id=GetObject"
}
```

- `key` — the R2 object key. **Keep it** — it's the only thing step 3 needs.
- `put_url` — presigned **PUT** URL, **expires in 900 s (15 min)**. Only the `host` header is signed,
  so a plain `PUT` of the bytes works — no `Content-MD5`, no AWS auth headers, no special handling.

### 2. PUT the file straight to R2

```
PUT <put_url>
Content-Type: application/octet-stream
<raw bytes of the .ldbk>
```

```bash
curl -X PUT --data-binary @"Delta-726-VIP-Login-V1.ldbk" \
     -H "Content-Type: application/octet-stream" \
     "<put_url>"
# -> 200
```

This bypasses the FarmSync API entirely and goes to R2. No `Authorization` header here — the
signature is baked into the URL.

### 3. Confirm — register the upload

```
POST https://api.farmsync.cloud/api/s3/confirm/file
Authorization: Bearer <key>
Content-Type: application/json

{ "key": "user/<user_id>/backup/20260626T092326_Delta-726-VIP-Login-V1.ldbk" }
```

Response:

```json
{ "ok": true, "key": "...", "size": 146066071, "etag": "4daa6b17...", "content_type": "application/octet-stream" }
```

The API HEADs the R2 object, reads its real size/etag, and creates the backup record. Done — the
file now appears in `GET /api/s3/files?type=backup`.

> **Naming note:** `confirm` with only `{key}` sets the record's `original_name` to the *storage key
> basename* (i.e. with the `YYYYMMDDThhmmss_` timestamp prefix, e.g.
> `20260626T092326_Delta-726-VIP-Login-V1.ldbk`). Your existing `724.ldbk` has a clean name because it
> was uploaded with one. The timestamped name is cosmetic and doesn't affect applying the backup.

---

## Apply a backup to a device

Re-imaging a device from an uploaded `.ldbk` is a **`Backup` task** (this is exactly what the
automation's group-backup enforcement does — `automation.py: create_task(did, "Backup", {"file_id": bid})`).

```
POST https://api.farmsync.cloud/api/tasks/
Authorization: Bearer <key>
Content-Type: application/json

{
  "device_id": "<device_id from GET /api/devices/>",
  "task_data": "{\"task_type\": \"Backup\", \"payload\": {\"file_id\": \"<backup id from GET /api/s3/files?type=backup>\"}}"
}
```

> ⚠️ **`task_data` is a JSON-encoded *string*, not a nested object** — note the escaped quotes. The
> inner object is `{"task_type": "Backup", "payload": {"file_id": "<id>"}}`. Pass the wrong shape and
> the task silently no-ops.

`file_id` = the `id` field of a `/api/s3/files?type=backup` item (NOT the `key`).

To push a backup to a whole **device group** at once (the dashboard's Group Backups feature) — including
straight from a `.ldbk` on local disk — see **§ Set a group's backup from a local `.ldbk`** below.

---

## Set a group's backup from a local `.ldbk` (dashboard)

The dashboard's **Group Backups** panel maps each device group to one backup, and the automation then
re-images every device in that group from it. To drive that from a `.ldbk` on local disk (e.g. the ones
already sitting in `farmsync_automation/`), the path is **upload → assign**.

Local `.ldbk` files in `farmsync_automation/` right now:
- `Delta-725-VIP-Login-V1.ldbk` — 143 MB (a real image; already uploaded as backup id `83adc790…`)
- `Delta-725-VIP-Login-V1-2c4g.ldbk` — 0 bytes

### Flow
1. **Upload** the local `.ldbk` as a backup (`type:"backup"` presign → PUT → confirm, § above), then
   read its `id` from `GET /api/s3/files?type=backup`.
2. **Assign** that `id` to a group — both paths write the same file:
   - **Dashboard UI:** Devices page → **Group Backups** → pick the group → choose the backup.
   - **Dashboard API:** `POST http://localhost:<port>/api/farmsync/group-backups`
     `{ "group": "<group name>", "backup_id": "<id>" }`
3. The dashboard writes `farmsync_automation/farmsync_automation/_group_backups.json`
   (`{group_name: backup_id}`); the running automation force-applies that backup to every device in the
   group on its next cycle.

The helper `set_group_backup_from_local(ldbk_path, group_name)` (Python section below) does steps 1-2 in
one call.

> The dashboard auto-increments its port on startup — it's been **5000-5003** this session (currently
> 5003). Use whichever port it's actually serving on.

### Dashboard group-backups API

**`GET /api/farmsync/group-backups`** returns everything the panel needs:
```json
{
  "groups":      [ { "name": "Blox Fruits", "device_count": 1, "backup_id": "" }, … ],
  "backups":     [ { "id": "83adc790…", "name": "20260623T093401_Delta-725-VIP-Login-V1.ldbk", "size": 146066071, "created_at": "…" } ],
  "assignments": { "Murder Mystery 2": "83adc790…", "Potion": "83adc790…", "Grow A Garden 2": "83adc790…" }
}
```
`backups` is pulled live from `GET /api/s3/files?type=backup`, so a freshly uploaded `.ldbk` appears
here automatically and becomes assignable.

**`POST /api/farmsync/group-backups`** `{ "group": "<name>", "backup_id": "<id>" }` sets the assignment
(empty `backup_id` clears it). `group` is the group **name**; `backup_id` is the S3 file **id** (from
`?type=backup`) — *not* the presign key and *not* the local filename.

### Offline alternative (no dashboard running)
The mapping is just a JSON file shared by the dashboard and the automation — write it directly:
```python
import json, os
GB = r"C:\Users\Duyy\Revenue-website\farmsync_automation\farmsync_automation\_group_backups.json"
m = json.load(open(GB)) if os.path.exists(GB) else {}
m["Blox Fruits"] = backup_id          # group NAME -> uploaded backup id
json.dump(m, open(GB, "w"))
```

### Caveats (group backup)
- **The `.ldbk` must be uploaded to FarmSync first** — the dashboard only lists/assigns FarmSync-side
  backups (`?type=backup`); a file only on local disk won't appear until uploaded.
- **Assigning a group backup is destructive and group-wide** — the automation re-images *every* device
  in the group (clears LDPlayer data). Double-check the group name.
- `group` = the group **name** ("Blox Fruits"); `backup_id` = the S3 file **id**, not the presign key
  or the local filename.

---

## Managing uploaded files

The full storage API (from `app.farmsync.cloud`'s route table):

| Action | Method + path |
|---|---|
| List (filter by type) | `GET /api/s3/files?type=backup` |
| Get one | `GET /api/s3/files/{id}` |
| **Delete** (R2 + DB) | `DELETE /api/s3/files/{id}` → `{"deleted":1,"ok":true,"s3_deleted":true}` |
| Fresh download URL | `GET /api/s3/files/{id}/url` |
| Direct download | `GET /api/s3/files/{id}/download` |
| Overwrite contents | `PUT /api/s3/files/{id}/overwrite` |

A backup-list item looks like:

```json
{
  "id": "4a0f53de128034b0...",
  "file_type": "backup",
  "file_path": "user/<user_id>/backup/20260618T053728_Delta-724-VIP-Login-V1.ldbk",
  "original_name": "724.ldbk",
  "content_type": "application/octet-stream",
  "upload_status": "uploaded",
  "checksum_md5": "Hhm1iXanGPUSpcOrL7Nrqg==",
  "size": 146066071,
  "created_at": "2026-06-18T05:41:19Z"
}
```

---

## Ready-to-use Python helper (curl-based)

Drop-in, matches the repo's `_curl` pattern (no `requests`). Reads the API key from `api_keys.txt`.

```python
import json, os, subprocess

BASE = "https://api.farmsync.cloud"
_KEY = None

def _api_key():
    global _KEY
    if _KEY:
        return _KEY
    f = os.path.join(os.path.dirname(__file__), "api_keys.txt")  # adjust path if needed
    _KEY = open(f, encoding="utf-8").readline().strip()
    return _KEY

def _api(method, path, body=None, timeout=60):
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

def upload_ldbk_backup(path):
    """Upload a .ldbk file as a FarmSync backup. Returns the new backup's FarmSync id."""
    filename = os.path.basename(path)
    size = os.path.getsize(path)

    # 1. presign (type='backup' is what routes it into the backup folder!)
    pres = _api("POST", "/api/s3/presign/file", {
        "filename": filename,
        "type": "backup",
        "content_type": "application/octet-stream",
        "size": size,
    })

    # 2. PUT bytes straight to R2 (presigned, expires in 15 min)
    put = subprocess.run(
        ["curl", "-sS", "--max-time", "600", "-X", "PUT", "--data-binary", "@" + path,
         "-H", "Content-Type: application/octet-stream", "-w", "%{http_code}", pres["put_url"]],
        capture_output=True)
    code = put.stdout.decode("utf-8", "ignore")[-3:]
    if not code.startswith("2"):
        raise RuntimeError(f"R2 PUT failed: HTTP {code}")

    # 3. confirm -> registers it as a backup
    _api("POST", "/api/s3/confirm/file", {"key": pres["key"]})

    # 4. resolve the new backup's id (confirm doesn't return it)
    for it in (_api("GET", "/api/s3/files?type=backup") or {}).get("items", []):
        if it.get("file_path") == pres["key"]:
            return it["id"]
    raise RuntimeError("uploaded but couldn't resolve the backup id")

def apply_backup_to_device(device_id, file_id):
    """Re-image a device from an uploaded backup (file_id = id from /api/s3/files?type=backup)."""
    task_data = json.dumps({"task_type": "Backup", "payload": {"file_id": file_id}})
    return _api("POST", "/api/tasks/", {"device_id": device_id, "task_data": task_data})

def set_group_backup_from_local(ldbk_path, group_name, dashboard="http://localhost:5003"):
    """Upload a local .ldbk, then wire it up as `group_name`'s backup in the dashboard.

    Steps 1-2 of the group-backup flow in one call: upload -> POST the dashboard's
    group-backups API (which writes _group_backups.json). Returns the backup id.
    """
    backup_id = upload_ldbk_backup(ldbk_path)
    body = json.dumps({"group": group_name, "backup_id": backup_id})
    subprocess.run(["curl", "-sS", "--max-time", "20", "-X", "POST",
                    "-H", "Content-Type: application/json", "-d", body,
                    dashboard.rstrip("/") + "/api/farmsync/group-backups"], capture_output=True)
    return backup_id

# Example — upload a local .ldbk and apply it to ONE device:
#   bid = upload_ldbk_backup(r"C:\path\to\Delta-726-VIP-Login-V1.ldbk")  # -> backup id
#   apply_backup_to_device("<device_id>", bid)
# Example — push the local 725 image to the WHOLE Blox Fruits group:
#   set_group_backup_from_local(
#       r"C:\Users\Duyy\Revenue-website\farmsync_automation\Delta-725-VIP-Login-V1.ldbk",
#       "Blox Fruits")
```

---

## How this was verified (2026-06-26)

1. Reverse-engineered the storage route table from `app.farmsync.cloud`'s JS bundles:
   `presign:/api/s3/presign/file`, `confirm:/api/s3/confirm/file`, `files:/api/s3/files`,
   `file/overwrite/url/download:/api/s3/files/{id}/…`.
2. Ran a full **presign → PUT → confirm** with a tiny 111-byte dummy `.ldbk`. First attempt used
   `file_type:"backup"` → landed in `workspace/` (not a backup). Re-probed the presign params and
   found **`type:"backup"`** routes to `backup/`.
3. Re-ran with `type:"backup"`: the test file appeared in `?type=backup` (backups 2 → 3), then
   `DELETE /api/s3/files/{id}` removed it (backups 3 → 2). Your real backups were never touched.
4. Read the dashboard's group-backup routes (`web/app.py` → `/api/farmsync/group-backups`) and the live
   `_group_backups.json`: confirmed it stores `{group_name: backup_id}` at
   `farmsync_automation/farmsync_automation/_group_backups.json` and lists assignable backups straight
   from `?type=backup`. (Currently MM2, Potion, and Grow A Garden 2 are all assigned the 725 image.)

## Caveats

- **Presigned PUT expires in 900 s.** For a slow upload of a multi-hundred-MB `.ldbk`, presign
  immediately before the `PUT`; if it expires, just presign again (it's cheap, creates nothing).
- **A freshly uploaded backup is not auto-applied to anything.** The automation only force-applies a
  backup that's been *assigned to a group* (`_group_backups.json`). Uploading alone is safe.
- **`file_id` (apply) ≠ `key` (confirm).** Apply-to-device uses the list item's `id`; confirm uses
  the presign `key`.
- Verified against the API as it existed 2026-06-26. If FarmSync changes the presign contract,
  re-inspect `app.farmsync.cloud`'s JS storage route table.
