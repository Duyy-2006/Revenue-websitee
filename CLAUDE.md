# Revenue Dashboard — Adopt Me! Account Sales Analytics

Aggregates sold-order data from **FunPay**, **u7buy**, **Eldorado.gg**, **G2G**, and **PlayerAuctions** into one local revenue dashboard. Read-only with respect to the platforms — no uploads, no listings management, no account database. The dashboard pulls orders, dedupes them, stores them in a local SQLite `sales` table, and renders revenue over time.

---

## 🚀 Brand-new-device setup (first run on a fresh Windows machine)

Prerequisites:
- **Windows 10/11**
- **Python 3.11+** (`python --version` — install from https://www.python.org/downloads/)
- **Google Chrome** (default install path `C:\Program Files\Google\Chrome\Application\chrome.exe`)

**Step 1 — Clone**
```powershell
cd %USERPROFILE%\Desktop
git clone https://github.com/NaraDuyy/Revenue-website.git Filter
cd Filter
```

> The code has paths hardcoded to `C:\Users\ADMIN\Desktop\Filter`. If your Windows username isn't `ADMIN` or you don't want the project at that path, do a find-replace on `C:\Users\ADMIN\Desktop\Filter` across `web/app.py` and every `funpay/*.py` / `*/login.bat` before running anything. (Or create a symlink: `mklink /D C:\Users\ADMIN\Desktop\Filter <your-real-path>`.)

**Step 2 — Install Python dependencies**
```powershell
pip install -r web\requirements.txt
```
This installs `flask`, `requests`, `selenium`, `undetected-chromedriver`. `web\run.bat` also runs this automatically on startup.

**Step 3 — Create the Chrome automation profile + sign in**
The Eldorado / G2G / PlayerAuctions scrapers and `funpay/refresh_cookies.py` all drive a **dedicated** Chrome profile at `C:\ChromeAutomation\Profile 3`. This keeps automation sessions isolated from your personal Chrome. Sign in once per platform:

```powershell
:: FunPay (cookies → funpay/cookie.txt)
funpay\login.bat
python funpay\refresh_cookies.py

:: Eldorado
eldorado\login.bat

:: G2G
g2g\login.bat

:: PlayerAuctions
playerauctions\login.bat
```

Each `login.bat` opens Chrome Profile 3 on that platform's sign-in page. Sign in, **close the Chrome window**, and the session persists on disk.

**Step 4 — Create the u7buy OpenAPI credentials file**
Get your AppId + AppSecret at u7buy Seller Panel → API Settings (or https://openapi.u7buy.com/), then save them as a 2-line file:
```powershell
notepad u7buy\u7buy_apikey.txt
```
Format (exactly two lines — AppId on line 1, AppSecret on line 2, no spaces, no prefix):
```
u7buy<your-app-id>
<your-app-secret>
```

**Step 5 — Launch**
```powershell
web\run.bat
```
Opens on http://localhost:5000. First startup:
- Creates `web\data.db` (empty)
- Spawns 5 background sync threads, one per platform — pulls whatever orders each dashboard still surfaces into the `sales` table
- Logs progress to the Dashboard's "Sync Log" panel

On subsequent launches the dashboard shows revenue immediately from the cached DB and re-syncs in the background.

**Step 6 — Verify**
- All 5 dots on the sidebar/Settings page should go green within ~30s of launch.
- Any red dot: check the Sync Log panel; the message points at the fix (e.g. "Eldorado: NOT logged in — run login.bat").

### When things break
| Symptom | Fix |
|---|---|
| FunPay dot red, "Session live" missing | Re-run `funpay\login.bat` → `python funpay\refresh_cookies.py`. Cookies last ~30 days. |
| u7buy dot red, "OpenAPI auth failed" | Check `u7buy\u7buy_apikey.txt` has AppId on line 1 + AppSecret on line 2, no trailing whitespace. |
| Eldorado / G2G / PA dot red, "NOT logged in" | Re-run that platform's `login.bat`. |
| Chrome driver version mismatch errors | `pip install --upgrade undetected-chromedriver` — it auto-pairs with the installed Chrome. |
| G2G orders stay empty even when logged in | DOM may have changed since this was written. See the **G2G** section below for the heuristic selectors; update in `g2g_fetch_sold_orders()`. |
| `web\data.db` has bad data | Stop the server, delete `web\data.db`, relaunch. Schema auto-recreates; sales re-sync from each platform dashboard (whatever still appears in pagination). |

---

## Auth strategy (per platform)

| Platform | Auth | Source of truth |
|---|---|---|
| FunPay | HTTP cookie session | `funpay/cookie.txt` (JSON list of cookies) |
| u7buy | HTTP OpenAPI Basic auth | `u7buy/u7buy_apikey.txt` (AppId line 1, AppSecret line 2) |
| Eldorado | Selenium Chrome on dedicated profile | `C:\ChromeAutomation\Profile 3` |
| G2G | Selenium Chrome on same profile | `C:\ChromeAutomation\Profile 3` |
| PlayerAuctions | Selenium Chrome on same profile | `C:\ChromeAutomation\Profile 3` |

**Chrome Profile 3** is a dedicated automation profile at `C:\ChromeAutomation\Profile 3` — separate from the user's personal Chrome. Sign in there once via each platform's `login.bat`; sessions persist. First-time setup:

```
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
    --user-data-dir="C:\ChromeAutomation" --profile-directory="Profile 3"
```

Then log into eldorado.gg / g2g.com / member.playerauctions.com inside that window.

## How to run

```
web\run.bat
```

Opens on `http://localhost:5000`. Auto-runs a startup sync across all 5 platforms, then a Chrome-profile login probe for Eldorado / G2G / PA. No automation loop — sync is on-demand via the Dashboard's **Sync All Sales** button or per-platform sync buttons.

## File structure

```
Filter/
├── .gitignore
├── CLAUDE.md                          # this file
├── eldorado/login.bat                 # one-time Chrome Profile 3 sign-in
├── funpay/
│   ├── login.bat                      # one-time Chrome Profile 3 sign-in
│   ├── refresh_cookies.py             # Chrome Profile 3 → cookie.txt (run after login.bat)
│   └── cookie.txt                     # (gitignored) HTTP session cookies
├── g2g/login.bat
├── playerauctions/login.bat
├── u7buy/
│   └── u7buy_apikey.txt               # (gitignored) OpenAPI Basic auth (AppId line 1, AppSecret line 2)
└── web/
    ├── app.py                         # Flask backend (~1500 lines)
    ├── requirements.txt               # flask, requests, selenium, undetected-chromedriver
    ├── run.bat                        # auto-restart launcher (installs deps, runs app.py, restarts on crash)
    ├── templates/index.html           # 3-page SPA
    └── static/
        ├── app.js                     # ~340 lines frontend
        ├── style.css
        ├── chart.min.js               # local Chart.js
        └── fa/                        # local Font Awesome
```

## Frontend (SPA, hash-routed)

4 pages:
1. **Dashboard** (`#dashboard`) — 4 revenue totals (Today / Week / Month / All Time), 5-platform revenue grid with per-platform sync buttons, **4 FarmSync device tiles** (Total Devices / Total Accounts / Running Accounts / Uptime %), sync log panel.
2. **Sales** (`#sales`) — filterable sold-order table (Date / Description / Platform / Price) with pagination.
3. **Devices** (`#devices`) — FarmSync device cards grouped by uptime tier (0-29 / 30-49 / 50-69 / 70-89 / 90%+), each collapsible. Each card shows name + status dot + uptime % + group · hostname + progress bar + **RAM/Disk/CPU stat chips** + OS string + active/total accounts + **Restart VPS button**. Header has an **Automation status pill** (click to pause/resume the FarmSync Automation subprocess), a Refresh button, and the device count.
4. **Settings** (`#settings`) — 5-platform connection status dots + Refresh All button.

Keyboard shortcuts: `1` = Dashboard, `2` = Sales, `3` = Devices, `4` = Settings, `R` = refresh current page.

Auto-refresh: dashboard + sales every 15s; devices every 30s (when active); platform status + automation status every 30s.

## Backend routes

- `GET /` — SPA shell
- `GET /api/stats` — aggregated revenue + sales counts + monthly history
- `GET /api/revenue?period=daily|weekly|monthly` — chart data (legacy; chart removed from UI but endpoint kept)
- `GET /api/sales?platform=&limit=` — sales ledger
- `GET /api/orders/<platform>` — live/cached order list (funpay / u7buy / eldorado / g2g / playerauctions)
- `POST /api/orders/<platform>/sync-sales` — fetch orders, insert new ones into `sales`
- `GET /api/platform/status` — connection status for all 5 platforms
- `POST /api/platform/refresh-all` — probes FunPay cookies, u7buy OpenAPI auth, and Chrome login state for Eldorado/G2G/PA. No per-platform refresh route exists — Chrome platforms self-heal via `login.bat`; FunPay cookies via `funpay/refresh_cookies.py`; u7buy keys don't expire
- `GET /api/automation/log` — last 50 sync-log entries
- `GET /api/farmsync/summary` — `{total_devices, total_accounts, running_accounts, uptime_pct}` for the dashboard tiles
- `GET /api/farmsync/devices[?force=1]` — enriched device list `{id, device_note, device_name, group_name, os, status, active_accounts, total_accounts, uptime_pct, tier, ram_used_gb, ram_total_gb, ram_pct, disk_used_gb, disk_total_gb, disk_pct, cpu_name, cpu_cores_physical, cpu_cores_logical}`. Reads automation's `_state_devices.json` if fresh (<30 min); else hits cloud API. Cached for 60s; `force=1` bypasses both.
- `POST /api/farmsync/devices/<device_id>/restart-vps` — forwards a `Restart VPS` task to `POST https://api.farmsync.cloud/api/tasks/` (matches `farmsync_automation/automation.py::create_task()` exactly).
- `GET /api/farmsync/automation/status` — `{running, paused, script, script_exists}`
- `POST /api/farmsync/automation/start` — spawn the FarmSync Automation subprocess (clears `_paused.flag`).
- `POST /api/farmsync/automation/stop` — terminate the subprocess + write `_paused.flag`.

## Database schema (SQLite, `web/data.db`)

```sql
CREATE TABLE sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,                -- always NULL in the slim build (kept for compat)
    username TEXT NOT NULL,            -- used for dedup via "[order_id]" suffix pattern
    platform TEXT NOT NULL,            -- funpay | u7buy | eldorado | g2g | playerauctions
    price REAL NOT NULL,
    sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_sales_sold_at ON sales(sold_at);
CREATE INDEX idx_sales_platform ON sales(platform);

CREATE TABLE cache (                   -- order caches per platform; not used for listings
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '[]',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

On startup, `init_db()` creates these if missing; it does **not** drop the legacy `accounts` table if one exists from a prior version of this project — safe no-op.

## Per-platform order fetch

### FunPay (HTTP)
`funpay_get_orders(session)` scrapes `funpay.com/en/orders/trade`. Parses `.tc-item` blocks via regex (no HTML parser dependency). Order fields: `order_id`, `price`, `status`, `date`, `buyer`, `description`, `abs_date` (local-time ISO).

Date parsing handles `today, HH:MM`, `yesterday, HH:MM`, `DD Month, HH:MM`, `DD Month YYYY, HH:MM`. FunPay times are Moscow (UTC+3); `_funpay_to_local()` shifts to local timezone.

Dedup on insert: `SELECT id FROM sales WHERE username LIKE '%[{order_id}]%'`.

Status filter: only `Paid` or `Closed` count as sales.

### u7buy (OpenAPI)
`u7buy_fetch_sold_orders()` calls `GET https://openapi.u7buy.com/prod-api/open-api/order/list?page=N&pageSize=10&orderStatus={4|5}` once per paid status code, then merges. Auth is `Authorization: Basic base64(AppId:AppSecret)` loaded from `u7buy/u7buy_apikey.txt` (AppId line 1, AppSecret line 2).

Both status `4` (To Receive — paid + delivered, awaiting buyer's receipt confirm) and `5` (Completed — buyer confirmed) count as revenue. Status 4 is critical: orders sit there for days/weeks before the buyer confirms, and excluding it loses every recent sale. The API silently caps `pageSize` at 10 regardless of what's requested, so paginate accordingly (default `max_pages=60`).

Response fields used: `orderId` / `orderNo`, `productName`, `amount` (price), `placedTime` (unix ms → ISO), `statusName`, `quantity`. Buyer is not exposed on the list endpoint.

Pagination break: when accumulated rows for a status reach the API-reported `total`, or the page returns no rows. Cross-status dedup via `seen_ids` (since the same `orderId` can theoretically transition between fetches). Status codes that are still skipped on sync (defense-in-depth): anything whose `statusName` contains `"cancel"` or `"refund"`.

Dedup: `SELECT id FROM sales WHERE platform='u7buy' AND username LIKE '%[{order_id}]%'` when an `orderId` is present (canonical); fallback to time+price match. No token refresh is needed — OpenAPI keys don't expire.

### Eldorado (Selenium Chrome)
`eldorado_fetch_sold_orders(max_pages=10)` drives the shared Chrome driver:
1. `drv.get("https://www.eldorado.gg/dashboard/orders/sold")`
2. If URL contains `login.eldorado.gg`, sets `eldorado_logged_out = True` and returns.
3. Scrapes `.grid-row` elements via `drv.execute_script()`.
4. Paginates via `.pagination-arrow` last-element click.

Dedup: the real Eldorado order id IS exposed — it's in each row's `href="/order/<uuid>"`. The scrape reads it and collapses the dashboard's duplicate / blank-title rows on it (rows with neither id nor title are dropped — these were the old phantom double-counts). The id STORED in `username` stays a `name|time|buyer|price` hash so DB-side dedup is continuous across the change. Category comes from `.game-info p` (the game label, e.g. "Adopt Me"/"Roblox"). Status filter rejects `cancelled / canceled / refunded / disputed / ""`.

Date format: `"Apr 16, 2026, 9:27:10 AM"` → ISO via `_normalize_eldorado_date()`.

### G2G (Selenium Chrome) ⚠️ heuristic selectors
`g2g_fetch_sold_orders(max_pages=10)` drives Chrome to `https://www.g2g.com/account/order-history?type=sell`.

**Selector heuristics** (may need DOM-inspection tuning on first real run):
- Rows: `table tr, .order-row, [data-order-id]` — whichever matches
- Cells: `td, .cell`
- Pagination: `.pagination .next a, a[rel=next], .page-next`
- Login check: URL contains `g2g.com/login`

Price extracted via regex `USD\s*([\d,.]+)|\$\s*([\d,.]+)` from joined cell text. Buyer/timestamp not reliably extracted — rows insert with `time=""` and sync falls back to `datetime.now()`.

If G2G's DOM differs from these guesses, update `g2g_fetch_sold_orders()` in `web/app.py` before trusting G2G revenue numbers.

### FarmSync (HTTPS, sibling repo)
The website talks to `https://api.farmsync.cloud` for the Devices page and the 4 dashboard tiles. Auth is `Authorization: Bearer <key>` where the key is the first line of `FARMSYNC_APIKEY_FILE` (default `farmsync-automation/api_keys.txt`).

**Shared state with the automation subprocess.** `farmsync-automation/farmsync_automation/automation.py` runs as a subprocess of Flask (auto-launched at boot unless `FARMSYNC_AUTOSTART=0`). Each cycle (default 20 min) it:
- Checks `_paused.flag` — if present, skips the cycle but still refreshes shared state.
- Writes `_state_devices.json` + `_state_accounts.json` next to itself (atomic JSON dumps).

The website's `farmsync_get_state()` reads those state files first (no API hit) if they're < 30 minutes old; falls back to direct API otherwise. Result: when the automation is running, the dashboard reads cached data → zero duplicate FarmSync API calls. When the automation is paused or absent, the website silently scrapes on its own at a 60s TTL.

**Restart VPS** from a device card sends `POST /api/tasks/` to FarmSync with `task_data = json.dumps({"task_type": "Restart VPS", "payload": {}})` — identical payload to `automation.py::create_task()`.

**Per-device fields derived** by the website:
- `status` — `disabled` (`!is_enabled`), `online` (`is_enabled && client_running`), else `offline`
- `uptime_pct` — `active_accounts / total_accounts * 100`, rounded to 1 dp
- `tier` — bucket: `0-29` | `30-49` | `50-69` | `70-89` | `90+`
- `ram_pct`, `disk_pct` — `(total - free) / total * 100`
- `os` — first non-empty of `sys_os` / `sys_os_name` / `sys_platform` / `os_release` / `platform` / `os`

### PlayerAuctions (Selenium Chrome)
`pa_fetch_orders_selenium(max_pages=5)` drives Chrome to `member.playerauctions.com/orders/selling`. Scrapes HTML `table` rows. Dedup: `username=? AND price=?` on insert (because no stable order_id surfaced).

PA is Cloudflare-protected; Chrome Profile 3 must have an active logged-in session. PA is NOT included in the background cache refresh (too slow) — only via explicit user sync.

## Timezones

- **FunPay** → Moscow UTC+3 → local via `_funpay_to_local()` (shift = local_offset − 3).
- **Eldorado** → API returns UTC → local via `_utc_to_local()` (`local_offset_hours` from `time.timezone`).
- **u7buy** → scraped UI time → stored as-is (treated as local).
- **G2G** → no reliable timestamp extracted → inserted as `datetime.now()`.
- **PA** → no reliable timestamp → inserted as `datetime.now()`.
- **Dashboard "today"** uses `datetime.now().strftime("%Y-%m-%d")` against `sales.sold_at`.

## Startup sequence (web/app.py `__main__`)

1. `init_db()` — create tables if missing.
2. Thread `_startup_sync` (after 3s) — sync FunPay + u7buy + Eldorado + G2G sales.
3. Thread `_startup_chrome_platform_check` (after 10s) — probe `eldorado_logged_in()` / `g2g_logged_in()` / `pa_logged_in()`, log to `/api/automation/log`.
4. `app.run(debug=True, host="0.0.0.0", port=5000, use_reloader=False)`.

No background automation loop. `use_reloader=False` so background threads survive — **restart `run.bat` after changing `app.py`**.

## Platform status cache

- `_platform_status_cache` dict protected by `_platform_status_lock`.
- `/api/platform/status` returns the cached dict instantly and spawns a background thread to refresh.
- Status values: `connected`, `disconnected`, `logged_out`.
- `u7buy_expires_in` (seconds until JWT expiry, or missing) surfaced alongside.
- `eldorado_logged_out` / `g2g_logged_out` / `pa_logged_out` booleans always explicitly set.

## Known limitations

1. **G2G selectors are heuristic** — see the G2G section above; verify against the real DOM on first run.
2. **G2G + PA lose per-order timestamps** — orders insert with `datetime.now()` if the scrape can't find a date cell, so the day-of-sale attribution can be off by up to a few minutes.
3. **u7buy buyer not exposed** — the `order/list` endpoint omits the buyer field (only the detail endpoint returns `memberName`). Revenue attribution still works; buyer column is blank on u7buy rows.
4. **No historical sales back-fill** — the `sales` table starts empty; it populates only from orders each platform still surfaces in its dashboard pagination. Orders that have rolled off those paginations are not recoverable.
5. **No deduplication across platforms** — each platform's orders are deduped on `platform + order_id` (or platform-specific heuristics), but nothing cross-checks a sale listed on two platforms.

## Seller info

- **FunPay** user ID 11554164
- **u7buy** username `Duyy`
- **Eldorado** username `Duyy` (email kimanhyt467@gmail.com)
- **G2G** username `Duyyyyyy`, seller ID `1001852670`
- **PlayerAuctions** username `KimAnhh`, memberId `5336867`
