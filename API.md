# Revenue Dashboard — API Reference

All endpoints are served by `web/app.py` (Flask) on the local host, e.g.
`http://localhost:<port>` and `http://192.168.100.158:<port>` on the LAN. The
port hops on restart (`_pick_clean_port`); check the console or `Get-NetTCPConnection`.
Responses are JSON unless noted. No auth on the dashboard itself — it binds to
`0.0.0.0`, so anything on the LAN can reach it.

---

## Pages (HTML)

| Method | Path | Description |
|---|---|---|
| GET | `/` | Single-page app shell (Dashboard / Devices / Offers / Accounts / Settings) |
| GET | `/sales-log` | Standalone full sales browser (sortable/filterable, not in the sidebar) |
| GET | `/api/sales/all` | Same standalone sales page (HTML) |
| GET | `/logs` | Standalone FarmSync logs viewer |

## Revenue & stats

| Method | Path | Description |
|---|---|---|
| GET | `/api/stats` | Headline revenue (today/week/month/all) + per-platform + per-platform counts + 12-month history |
| GET | `/api/revenue?period=daily\|weekly\|monthly` | Time-series revenue per platform for charts |
| GET | `/api/revenue/by-category?period=all\|today\|week\|month` | **Revenue grouped by game/category** — total, % of grand total, per-platform split per category |
| GET | `/api/stats/date?d=YYYY-MM-DD` | Revenue + counts for one specific day |

## Sales ledger

| Method | Path | Description |
|---|---|---|
| GET | `/api/sales/today` | Today's sales rows |
| GET | `/api/sales/period?p=today\|week\|month` or `?d=YYYY-MM-DD` | Sales rows for a period/date — includes `category` per row |
| GET | `/api/sales?platform=&limit=` | Raw sales rows (`SELECT *`), newest first |
| GET | `/api/sales/all/download?from=&to=` | CSV export of the sales ledger |
| GET | `/api/sales/all/summary` | Aggregate counts + min/max `sold_at` (cheap, no rows) |

## Orders & sync (per platform: `funpay` `u7buy` `eldorado` `g2g` `playerauctions`)

| Method | Path | Description |
|---|---|---|
| GET | `/api/orders/<platform>` | Live/cached sold-order list for one platform |
| POST | `/api/orders/<platform>/sync-sales` | Fetch orders → insert new ones into the `sales` table (captures `category`) |
| POST | `/api/categories/backfill` | Re-tag sales rows missing a `category` (re-scrape FunPay/u7buy by order-id + keyword-classify the rest) |

## Platform connection status

| Method | Path | Description |
|---|---|---|
| GET | `/api/platform/status` | Cached connect/disconnect/logged-out per platform (refreshes in background) |
| POST | `/api/platform/refresh-all` | Re-probe FunPay cookies, u7buy auth, and Chrome login state for Eldorado/G2G/PA |

## FarmSync (device farm)

| Method | Path | Description |
|---|---|---|
| GET | `/api/farmsync/summary` | `{total_devices, total_accounts, running_accounts, uptime_pct}` for the Dashboard tiles |
| GET | `/api/farmsync/devices[?force=1]` | Enriched device list (name, group, os, status, uptime %, tier, RAM/disk, accounts); 60s cache |
| POST | `/api/farmsync/devices/<id>/restart-vps` | Send a "Restart VPS" task to one device |
| GET | `/api/farmsync/group-backups` | Per-group backup assignment map |
| POST | `/api/farmsync/group-backups` | Set a group's backup (force-applied to its devices) |
| GET | `/api/farmsync/automation/status` | Automation subprocess status (running / paused / stopped) |
| POST | `/api/farmsync/automation/start` · `/stop` | Start / pause the FarmSync automation |
| GET | `/api/farmsync/logs[?...]` · `/logs/download` · `/logs/cycle` | Automation logs (view / download / per-cycle) |
| GET | `/api/accounts` | Per-account farm-time table from FarmSync |

## YummyTrackStat (per-account in-game stats) — *new*

Proxies `yummytrackstat.com/api/...` with the Bearer token in `yummytrackstat/token.txt`
(gitignored). Token can expire → re-copy it (no restart). 30s per-call cache.

| Method | Path | Description |
|---|---|---|
| GET | `/api/trackstat/status[?force=1]` | API connection status (pings `/auth/me`); auto-checked every 20 min, shown on Settings |
| GET | `/api/trackstat/<game>[?all=1&page=&limit=]` | One game's `{statistics, accounts}` (e.g. `adopt-me`, `murder-mystery-2`). `?all=1` = every account |
| GET | `/api/trackstat/all[?accounts=1&refresh=1]` | **Stable merge** — every game you've ever tracked, each with its current count (0 when idle) |
| GET | `/api/trackstat/active[?accounts=1]` | **Live merge** — only games with accounts being tracked right now |

The tracked-game set auto-discovers (20-min probe + ~6h catalog re-scrape) and persists to `yummytrackstat/seen_games.json` (gitignored).

## ZP ZeroSolver (auto-solve CAPTCHA-locked accounts) — *new*

A background loop (every 20 min) collects FarmSync accounts whose per-account
`error == "CAPTCHA"` (assigned to a device, live `_|WARNING` cookie) and submits
the ones **not already in the ZeroSolver queue** to ZeroSolver's in-game solver
(`POST https://zeropoint.to/api/zerosolver-api/submit`). ZeroSolver's API returns
only per-job counts (never usernames), so "already in the queue" is tracked in a
local job ledger (`Zp auto faceunlock/_zp_solver_state.json`, gitignored); when a
job finishes, any account that's still captcha is re-sent the next cycle. Only
successful solves are billed (0.0025 cr ≈ $0.0025 each); already-solved and failed
accounts are free. Key in `Zp auto faceunlock/api_solver.txt` (gitignored).

| Method | Path | Description |
|---|---|---|
| GET | `/api/zpsolver/status[?force=1]` | Balance, status (ok/paused/error/nokey), accounts in-queue, sent-total, recent jobs — feeds the Dashboard "ZP Solver" tile + Settings card |
| POST | `/api/zpsolver/run` | Trigger one sweep now (runs even while paused) |
| POST | `/api/zpsolver/toggle` | Pause / resume the automatic 20-min loop (writes `_zp_solver_paused.flag`) |

## Live offers

| Method | Path | Description |
|---|---|---|
| GET | `/api/offers/live` | Cached live offers per platform (counts + freshness) |
| GET | `/api/offers/live/detail?platform=&section=&grep=&min_price=&max_price=&min_live_hours=&max_live_hours=&sort_by=&sort_dir=&limit=&format=` | Full per-offer listing with live-time enrichment + filters + aggregate stats |
| GET | `/api/offers/sidebar[?platform=]` | **Offers-sidebar tracker** — live offers grouped by category (count, %, value, per-platform split, oldest-live) |
| GET | `/api/offers/backup` | List recoverable offers-cache backups |
| POST | `/api/offers/backup/restore` | Restore the offers cache from a backup |
| POST | `/api/offers/live/refresh` · `/refresh/<platform>` | Re-scrape offers (all / one platform) |
| POST | `/api/offers/live/clear` · `/clear/<platform>` | Clear cached offers (all / one platform) |
| GET | `/api/offers/diag/eldorado` | Eldorado offers scrape diagnostics |

## Chrome control

| Method | Path | Description |
|---|---|---|
| GET | `/api/chrome/freeze/status` | Whether the automation Chrome (Profile 3) is frozen |
| POST | `/api/chrome/freeze/toggle` | Freeze/resume Chrome activity (so you can use Profile 3 manually / run login.bat) |
| GET | `/api/chrome/debug` · `/debug/download` | Chrome forensic debug log (view / download) |

## Misc

| Method | Path | Description |
|---|---|---|
| GET | `/api/yescaptcha/balance` | YesCaptcha point balance (≈ USD) — drives the "Points" Dashboard tile |
| GET | `/api/automation/log` | Last ~50 sync-log entries (Dashboard "Sync Log" panel) |
| POST | `/api/shutdown` | Shut the Flask app down |

---

### Built in this iteration
`/api/revenue/by-category` · `/api/categories/backfill` · the whole `/api/trackstat/*`
family · `/api/offers/sidebar` · the `/api/zpsolver/*` family (auto-submit captcha
accounts to ZeroSolver every 20 min). Sales rows now carry a `category`; the
Settings page shows YummyTrackStat + ZP ZeroSolver status; the Dashboard has a
"ZP Solver" tile (balance / status / in-queue).
