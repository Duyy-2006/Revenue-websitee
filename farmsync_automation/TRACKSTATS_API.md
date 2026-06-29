# FarmSync TrackStats API

**TrackStats** is FarmSync's per‑game live‑stats system. The farming client on each device reports a
blob of in‑game numbers per account (sheckles, tokens, plants, pets, seeds, …); a **TrackStats config**
— tied to a Roblox game by `place_id` — decides how that blob is *displayed* in the dashboard: table
**columns**, aggregate **stat** cards, **filters**, and **inventory** views. Configs are sharable through
a built‑in **catalog / marketplace** (clone, publish, even sell). This doc covers the whole config API.

> Reverse‑engineered from `app.farmsync.cloud` + probed live on 2026‑06‑27. Your own config list is
> currently empty; the catalog has 7 community configs (mostly Grow‑a‑Garden / face‑unlock).

---

## Auth & base URL

| | |
|---|---|
| Base URL | `https://api.farmsync.cloud` |
| Auth header | `Authorization: Bearer <key>` |
| Key source | first line of `farmsync_automation/api_keys.txt` |

Use `curl` (subprocess), not Python `requests` — `requests` is 30‑45 s/call on this box (SChannel OCSP).

---

## Full route table

All under `/api/self/trackstats-configs`. (From the web app's JS `trackstats_configs` route object.)

| Action | Method + path | Notes |
|---|---|---|
| **List** your configs | `GET /api/self/trackstats-configs` | `{ "data": [ … ], "success": true }` |
| **Create** | `POST /api/self/trackstats-configs` | body needs at least `data` (see below) |
| **Get** one | `GET /api/self/trackstats-configs/{id}` | |
| **Update** | `PUT /api/self/trackstats-configs/{id}` | |
| **Delete** | `DELETE /api/self/trackstats-configs/{id}` | |
| **Publish** to catalog | `POST /api/self/trackstats-configs/{id}/publish` | makes your config public |
| **Unpublish** | `POST /api/self/trackstats-configs/{id}/unpublish` | |
| **Upload image** | `POST /api/self/trackstats-configs/images` | icon / thumbnail assets |
| **Catalog** (browse) | `GET /api/self/trackstats-configs/catalog` | community/marketplace configs |
| **Catalog item** | `GET /api/self/trackstats-configs/catalog/{id}` | |
| **Clone** a catalog item | `POST /api/self/trackstats-configs/catalog/{id}/clone` | copies it into *your* configs |
| **Report** a catalog item | `POST /api/self/trackstats-configs/catalog/{id}/report` | moderation |

---

## The config object

Top‑level fields (same shape for your configs and catalog items):

```jsonc
{
  "id": "bf927f18…",
  "user_id": "5b02a3c1…",
  "name": "GAG OC CHO",
  "place_id": "97598239454123",      // Roblox place id, OR "custom-<digits>" for a non-game config
  "icon": "https://tr.rbxcdn.com/…", // game thumbnail
  "color": "success",                // theme color (see palette below)
  "data": "{…}",                     // ⚠️ a JSON-ENCODED STRING — the actual config (see next section)

  // --- catalog / marketplace metadata ---
  "visibility": "public",            // public | private
  "version": "2",
  "forked_from": "",                 // id of the config this was cloned from
  "published": "True",
  "installs": "96",                  // clone count
  "title": "gag oc cho",
  "description_md": "…",             // markdown description
  "thumbnail_url": "", "thumbnail_file_id": "",
  "images": "[]", "image_file_ids": "[]",
  "tags": "[]",
  "category": "gag",                 // e.g. gag, face-unlock, …
  "price": "0", "currency": "USD", "is_paid": "False",   // configs CAN be sold
  "seller_user_id": "5b02a3c1…",
  "published_at": "1781536564", "last_modified": "1781536546"
}
```

> ⚠️ **`data` is a stringified JSON**, not a nested object — `json.dumps(...)` it when writing, and
> `json.loads(config["data"])` to read it (same gotcha as the `Backup` task's `task_data`).

`color` palette (HeroUI): `default` · `primary` · `secondary` · `success` · `warning` · `error` · `info`.

---

## The `data` blob (the real config)

`json.loads(config["data"])` →

```jsonc
{
  "meta":    { "name": "GAG OC CHO", "placeID": "97598239454123", "icon": "https://…", "color": "success" },
  "columns": [ … ],   // per-device table cells
  "stats":   [ … ],   // aggregate cards across all devices
  "filters": [ … ],   // filter controls
  "filterFunction": "(e,a)=>{ … }",   // JS string: applies the filters to a device row
  "gridView": { … },              // optional grid/gallery of items (pets, etc.)
  "inventoryModalCells": { … },   // optional: the per-device inventory popup
  "inventoryBar": { … }           // optional: inventory summary bar w/ resolved Roblox asset icons
}
```

### `columns` — per‑device table cells
Each column turns the device's reported stat blob into a labelled, coloured chip.

```jsonc
{ "id": "sheckles", "label": "💰 Sheckles",
  "render": "e => [{ label: '💰 ' + r(e.sheckles || 0), color: 'success' }]" }
```

- **`render`** is a JS arrow‑function **string**, evaluated by the dashboard. It receives the device's
  parsed stat object and returns **`{label, color}`** *or* an array of them (multiple chips).
- Render helpers seen in catalog configs (provided by FarmSync's sandbox):
  - `get(data, 'field')` — safe field getter
  - `r(n)` / `t(n)` — number formatters (abbreviate, e.g. `1.2M`)
  - `c(data, 'Gold')` — count items whose name contains a substring (e.g. gold‑variant seeds)
- Real examples from the GAG config:
  - `e => [{ label: '🪙 ' + r(e.tokens || 0), color: 'warning' }]`
  - `data => { let val = String(get(data,'info') ?? ''); let l = val.toLowerCase(); let col = l.includes('error')||l.includes('fail') ? 'error' : l.includes('stuck')||l.includes('wait')||l.includes('full') ? 'warning' : 'success'; return { label: 'ℹ️ '+val, color: col }; }`
  - `e => { let a = c(e,'Gold'); return [{ label:'✨ '+t(a), color: a>0 ? 'warning':'default' }]; }`

### `stats` — aggregate cards (declarative, no JS)
```jsonc
{ "id": "totalSheckles", "label": "💰 Total Sheckles", "format": "number",
  "color": "success", "path": "sheckles", "aggregation": "sum" }
```
- `path` — field in each device's stat blob; `aggregation` — `sum` (observed; avg/max/count likely exist);
  `format` — `number` (observed).

### `filters` + `filterFunction`
```jsonc
"filters": [ { "id": "minSheckles", "label": "Min Sheckles", "type": "number" }, … ],
"filterFunction": "(e,a)=>{ let l = JSON.parse(e.data||'{}'); if (a.minSheckles && (l.sheckles||0) < a.minSheckles) return false; … return true; }"
```
`filters` render the controls; `filterFunction(deviceRow, activeFilters)` returns `true`/`false` to keep/drop a device.

### `gridView`, `inventoryModalCells`, `inventoryBar` (optional)
```jsonc
"gridView": { "enabled": true, "itemTypes": [
  { "key": "pets", "label": "Pets", "itemsPath": "pets", "nameField": "name", "rarityField": "rarity", "assetIdField": "image" } ] },
"inventoryModalCells": { "pets": { "icon": "solar:cat-bold-duotone", "color": "info", "label": "🐾 Pet Collection" }, … },
"inventoryBar": { "resolveAssets": true, "groups": [
  { "label": "Seeds", "source": "seeds", "items": [ { "key": "dragons-breath", "name": "Dragon's Breath", "icon": "rbxassetid://89388779740389" }, … ] } ] }
```
These drive the richer per‑device inventory popups/bars; `resolveAssets:true` turns `rbxassetid://…` into thumbnails.

> **Where the raw numbers come from:** the farming client reports each account's in‑game stats as the
> device/account `data` blob (e.g. `{sheckles, tokens, plants_count, pets:[…], seeds_count, …}`). TrackStats
> is purely the **view** over that data — matched to the running game by `place_id`.

---

## CRUD examples

**Create** (minimum is `data`; include `name`/`place_id`/`icon`/`color` so it's usable):
```bash
curl -X POST "https://api.farmsync.cloud/api/self/trackstats-configs" \
  -H "Authorization: Bearer $(head -1 farmsync_automation/api_keys.txt)" \
  -H "Content-Type: application/json" \
  -d '{
        "name":"My GAG view","place_id":"97598239454123","color":"success",
        "data":"{\"meta\":{\"name\":\"My GAG view\",\"placeID\":\"97598239454123\",\"color\":\"success\"},\"columns\":[{\"id\":\"sheckles\",\"label\":\"💰 Sheckles\",\"render\":\"e => [{ label: \\\"💰 \\\" + (e.sheckles||0), color: \\\"success\\\" }]\"}],\"stats\":[],\"filters\":[]}"
      }'
```
*(Note the double‑stringified `data`. A bare `{}` returns `{"message":"config data is empty","success":false}`.)*

**Clone a catalog config into yours** (easiest way to start):
```bash
curl -X POST "https://api.farmsync.cloud/api/self/trackstats-configs/catalog/<catalog_id>/clone" \
  -H "Authorization: Bearer $(head -1 farmsync_automation/api_keys.txt)"
```
Update / delete / publish are the obvious `PUT` / `DELETE` / `POST …/publish` on `…/trackstats-configs/{id}`.

---

## Ready‑to‑use Python helper (curl‑based)

```python
import json, os, subprocess

BASE = "https://api.farmsync.cloud"; _KEY = None
def _key():
    global _KEY
    if not _KEY:
        _KEY = open(os.path.join(os.path.dirname(__file__), "api_keys.txt"), encoding="utf-8").readline().strip()
    return _KEY
def _api(method, path, body=None, t=30):
    cmd = ["curl","-sS","--max-time",str(t),"-X",method,"-H",f"Authorization: Bearer {_key()}","-H","Accept: application/json","-w","\n%{http_code}"]
    if body is not None: cmd += ["-H","Content-Type: application/json","-d",json.dumps(body)]
    cmd.append(BASE+path)
    out = subprocess.run(cmd, capture_output=True).stdout.decode("utf-8","ignore")
    raw,_,code = out.rpartition("\n")
    if not code.strip().startswith("2"): raise RuntimeError(f"HTTP {code.strip()}: {raw[:200]}")
    return json.loads(raw) if raw.strip() else None

def list_configs():        return _api("GET",  "/api/self/trackstats-configs")["data"]
def get_config(cid):       return _api("GET",  f"/api/self/trackstats-configs/{cid}")["data"]
def catalog():             return _api("GET",  "/api/self/trackstats-configs/catalog")["data"]
def clone_from_catalog(catalog_id):
    return _api("POST", f"/api/self/trackstats-configs/catalog/{catalog_id}/clone")
def create_config(name, place_id, data_obj, color="success", icon=""):
    return _api("POST", "/api/self/trackstats-configs",
                {"name": name, "place_id": place_id, "color": color, "icon": icon,
                 "data": json.dumps(data_obj)})          # data must be a JSON *string*
def delete_config(cid):    return _api("DELETE", f"/api/self/trackstats-configs/{cid}")

# Read a catalog config's display schema:
#   c = catalog()[0]; cfg = json.loads(c["data"]); print([col["id"] for col in cfg["columns"]])
```

---

## How this was verified (2026‑06‑27)

- Pulled the `trackstats_configs` route object from `app.farmsync.cloud`'s JS bundle (full CRUD + catalog
  + clone/report + publish/unpublish + image upload).
- `GET …/trackstats-configs` → `{"data":[],"success":true}` (you have none yet).
- `GET …/catalog` → 7 community configs; decoded one (`GAG OC CHO`, place `97598239454123`, 96 installs) to
  map the full `data` schema (meta/columns/stats/filters/filterFunction/gridView/inventoryModalCells/inventoryBar).
- `POST …/trackstats-configs {}` → `{"message":"config data is empty"}` (confirms `data` is required).

## Caveats
- **`data` is a stringified JSON** — double‑encode on write, `json.loads` on read.
- **`render` / `filterFunction` are JS code strings** run by the dashboard. They use FarmSync's sandbox
  helpers (`get`, `r`, `t`, `c`); the exact helper implementations are dashboard‑side.
- Configs match a running game by **`place_id`** (`custom-…` ids are for non‑game/utility views like
  face‑unlock dashboards).
- The catalog is a **marketplace**: `installs`, `published`, and `price`/`is_paid`/`seller_user_id` mean
  some configs may be paid. Cloning a paid config may require purchase (not exercised here).
- Schema/route accurate as of 2026‑06‑27; re‑inspect `app.farmsync.cloud`'s JS if FarmSync changes it.
