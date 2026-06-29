/* Revenue Dashboard — Frontend */

let _prevPlatformStatus = {};

// ─── Nav ───
function navigateTo(page) {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    const navItem = document.querySelector('.nav-item[data-page="' + page + '"]');
    if (navItem) navItem.classList.add('active');
    const pageEl = document.getElementById('page-' + page);
    if (pageEl) pageEl.classList.add('active');
    location.hash = page;
    if (page === 'dashboard') loadDashboard();
    else if (page === 'devices') { loadDevices(); loadGroupBackups(); }
    else if (page === 'offers') loadOffers();
    else if (page === 'accounts') loadAccounts();
    else if (page === 'settings') { checkPlatformStatus(); checkTrackstatStatus(); checkZpSolver(); }
}
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', e => { e.preventDefault(); navigateTo(item.dataset.page); });
});
window.addEventListener('hashchange', () => {
    const page = location.hash.replace('#', '');
    if (page && document.getElementById('page-' + page)) navigateTo(page);
});

// ─── Toast ───
function toast(msg, type = 'info') {
    const c = document.getElementById('toast-container');
    const t = document.createElement('div');
    t.className = 'toast ' + type;
    const icons = { success:'fa-check-circle', error:'fa-exclamation-circle', info:'fa-info-circle', warning:'fa-exclamation-triangle' };
    t.innerHTML = `<i class="fas ${icons[type]||icons.info}"></i> ${msg}`;
    c.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 4000);
}

// ─── API ───
async function api(url, opts = {}) {
    const resp = await fetch(url, {
        headers: opts.body ? { 'Content-Type': 'application/json' } : {},
        ...opts,
        body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('application/json')) throw new Error(`HTTP ${resp.status} — non-JSON response`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

function esc(s) { return s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : ''; }

// ─── Pagination ───
const PAGE_SIZE = 20;
const _pages = { sales: 1 };

function paginate(items, key) {
    const total = items.length;
    const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    if (!_pages[key]) _pages[key] = 1;
    if (_pages[key] > totalPages) _pages[key] = totalPages;
    const start = (_pages[key] - 1) * PAGE_SIZE;
    return { items: items.slice(start, start + PAGE_SIZE), total, page: _pages[key], totalPages };
}

function renderPagination(containerId, key, total, page, totalPages, onPageChange) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (totalPages <= 1) { el.innerHTML = ''; return; }
    let html = '';
    const addBtn = (label, n, dis, act) => {
        html += `<button class="page-btn ${act?'active':''}" ${dis?'disabled':''} onclick="${onPageChange}(${n})">${label}</button>`;
    };
    addBtn('‹', page - 1, page <= 1);
    const max = 7;
    let start = Math.max(1, page - 3);
    let end = Math.min(totalPages, start + max - 1);
    start = Math.max(1, end - max + 1);
    if (start > 1) { addBtn(1, 1); if (start > 2) html += '<span class="page-ellipsis">…</span>'; }
    for (let i = start; i <= end; i++) addBtn(i, i, false, i === page);
    if (end < totalPages) { if (end < totalPages - 1) html += '<span class="page-ellipsis">…</span>'; addBtn(totalPages, totalPages); }
    addBtn('›', page + 1, page >= totalPages);
    el.innerHTML = html;
}

// ─── Dashboard ───
async function loadDashboard() {
    try {
        const s = await api('/api/stats');
        const r = s.revenue || {};
        const c = s.sales_count || {};

        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        set('rev-today', '$' + (r.today || 0).toFixed(2));
        set('rev-week', '$' + (r.week || 0).toFixed(2));
        set('rev-month', '$' + (r.month || 0).toFixed(2));
        set('rev-today-sales', (c.today || 0) + ' sales');
        set('rev-week-sales', (c.week || 0) + ' sales');
        set('rev-month-sales', (c.month || 0) + ' sales');

        // Stash the stats payload for the Revenue panel re-render
        _statsRevenue = r;
        _statsCount = c;

        const navSales = document.getElementById('nav-sales');
        if (navSales) navSales.textContent = c.total || 0;

        renderRevenuePanel();
        loadSalesPanel();
        loadCategoryRevenue();
        loadFarmsyncSummary();
        loadYescaptchaBalance();
        loadZpSolver();
        loadLog();
    } catch (e) {
        toast(e.message, 'error');
    }
}

async function loadYescaptchaBalance() {
    try {
        const d = await api('/api/yescaptcha/balance');
        const balEl = document.getElementById('yc-balance');
        const usdEl = document.getElementById('yc-balance-usd');
        if (balEl) balEl.textContent = (d.balance || 0).toLocaleString();
        if (usdEl) usdEl.textContent = '≈ $' + (d.usd || 0).toFixed(2) + ' USD';
    } catch (e) {
        const balEl = document.getElementById('yc-balance');
        const usdEl = document.getElementById('yc-balance-usd');
        if (balEl) balEl.textContent = '—';
        if (usdEl) usdEl.textContent = (e.message || 'error').slice(0, 50);
    }
}

// ─── ZP ZeroSolver (auto-submit CAPTCHA-locked accounts) ───
function _renderZpTile(d) {
    const bal = document.getElementById('zp-balance');
    const dot = document.getElementById('zp-dot');
    const sub = document.getElementById('zp-sub');
    if (bal) bal.textContent = (d.balance == null) ? '—' : ('$' + Number(d.balance).toFixed(2));
    const cls = { ok: 'ok', paused: 'paused', error: 'err', nokey: 'err' }[d.status] || 'err';
    if (dot) {
        dot.className = 'zp-dot ' + cls;
        dot.title = d.status === 'nokey' ? 'ZeroSolver API key missing'
            : d.status === 'paused' ? 'Auto-loop paused'
            : d.status === 'error' ? (d.last_error || 'error')
            : 'Auto-submitting every ' + (d.interval_min || 20) + ' min';
    }
    if (sub) {
        let s = (d.in_queue || 0) + ' in queue';
        if (d.sent_total) s += ' · ' + d.sent_total + ' sent';
        if (d.paused) s += ' · paused';
        sub.textContent = s;
    }
}

function _renderZpSettings(d) {
    const dot = document.getElementById('status-zpsolver');
    const hint = document.getElementById('zp-settings-hint');
    const tbtn = document.getElementById('zp-toggle-btn');
    if (dot) dot.className = 'dot ' + (d.status === 'ok' ? 'connected'
        : (d.status === 'paused' || d.status === 'nokey') ? 'logged-out' : 'disconnected');
    if (hint) {
        const parts = [];
        if (d.status === 'nokey') parts.push('API key missing');
        else if (d.balance != null) parts.push('$' + Number(d.balance).toFixed(2) + ' credits');
        parts.push((d.in_queue || 0) + ' in queue');
        if (d.sent_total) parts.push(d.sent_total + ' sent total');
        if (d.paused) parts.push('PAUSED');
        if (d.last_error && d.status === 'error') parts.push(d.last_error);
        if (d.last_cycle_ts) {
            const t = new Date(d.last_cycle_ts * 1000);
            if (!isNaN(t)) parts.push('last sweep ' + t.toLocaleTimeString());
        }
        hint.textContent = parts.join(' · ');
    }
    if (tbtn) tbtn.innerHTML = d.paused
        ? '<i class="fas fa-play"></i> Resume' : '<i class="fas fa-pause"></i> Pause';
}

async function loadZpSolver() {
    try {
        const d = await api('/api/zpsolver/status');
        _renderZpTile(d); _renderZpSettings(d);
    } catch (e) {
        const bal = document.getElementById('zp-balance');
        const dot = document.getElementById('zp-dot');
        const sub = document.getElementById('zp-sub');
        if (bal) bal.textContent = '—';
        if (dot) { dot.className = 'zp-dot err'; dot.title = e.message || 'error'; }
        if (sub) sub.textContent = (e.message || 'error').slice(0, 40);
    }
}

async function checkZpSolver(force) {
    const hint = document.getElementById('zp-settings-hint');
    if (force && hint) hint.textContent = 'checking…';
    try {
        const d = await api('/api/zpsolver/status' + (force ? '?force=1' : ''));
        _renderZpTile(d); _renderZpSettings(d);
    } catch (e) {
        const dot = document.getElementById('status-zpsolver');
        if (dot) dot.className = 'dot disconnected';
        if (hint) hint.textContent = e.message || 'error';
    }
}

async function toggleZpSolver() {
    try {
        const d = await api('/api/zpsolver/toggle', { method: 'POST' });
        toast(d.message || (d.paused ? 'Paused' : 'Resumed'), 'success');
        checkZpSolver(true);
    } catch (e) { toast(e.message, 'error'); }
}

async function runZpSolver(btn) {
    if (btn) btn.disabled = true;
    toast('ZP solver: running a sweep…', 'info');
    try {
        const d = await api('/api/zpsolver/run', { method: 'POST' });
        if (d.error) toast('ZP solver: ' + d.error, 'warning');
        else if (d.submitted) toast(`ZP solver: submitted ${d.submitted} accounts` +
            (d.skipped_in_queue ? ` (${d.skipped_in_queue} already in queue)` : ''), 'success');
        else toast(`ZP solver: nothing new to send` +
            ` (${d.captcha_total} captcha, ${d.skipped_in_queue} in queue)`, 'info');
        checkZpSolver(true);
    } catch (e) { toast(e.message, 'error'); }
    finally { if (btn) btn.disabled = false; }
}

// ─── Revenue by category (Dashboard panel) ───
// Period + view-mode are independent of the other panels; persisted across reloads.
// Panel defaults to By Platform + Today. The one-time reset bumps existing
// users (with older saved prefs) to the new default once; their later choices
// still persist after that.
(function () {
    try {
        if (localStorage.getItem('dashboard.catDefaults') !== 'platform-today') {
            localStorage.setItem('dashboard.catMode', 'platform');
            localStorage.setItem('dashboard.catPeriod', 'today');
            localStorage.setItem('dashboard.catDefaults', 'platform-today');
        }
    } catch (e) {}
})();
let _catRevPeriod = (function () {
    try { return localStorage.getItem('dashboard.catPeriod') || 'today'; } catch (e) { return 'today'; }
})();
let _catRevMode = (function () {
    try { return localStorage.getItem('dashboard.catMode') || 'platform'; } catch (e) { return 'platform'; }
})();
let _catRevData = null;   // last fetched payload, reused when only the mode flips

// Stable colour per category (for the By-Platform stacked bars/dots, where
// segments are categories rather than platforms).
const _CAT_COLORS = {
    'Adopt Me': '#ff5733', 'Roblox': '#4a76a8', 'Grow a Garden 2': '#00dc82',
    'Murder Mystery 2': '#e74c3c', 'Steal a Brainrot': '#9b59b6', 'Fisch': '#00b4d8',
    'King Legacy': '#f1c40f', 'Da Hood': '#e67e22', 'Blade Ball': '#1abc9c',
    'Sailor Piece': '#fd79a8', 'Uncategorized': '#8888aa',
};
const _CAT_FALLBACK = ['#16a085', '#c0392b', '#8e44ad', '#2980b9', '#d35400', '#27ae60', '#7f8c8d'];
function categoryColor(name) {
    if (_CAT_COLORS[name]) return _CAT_COLORS[name];
    let h = 0;
    for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
    return _CAT_FALLBACK[h % _CAT_FALLBACK.length];
}

function setCategoryPeriod(p) {
    _catRevPeriod = p;
    try { localStorage.setItem('dashboard.catPeriod', p); } catch (e) {}
    loadCategoryRevenue();                      // period changed → refetch
}
function setCategoryMode(m) {
    _catRevMode = m;
    try { localStorage.setItem('dashboard.catMode', m); } catch (e) {}
    if (_catRevData) renderCategoryPanel(_catRevData);   // same data, just re-render
    else loadCategoryRevenue();
}

async function loadCategoryRevenue() {
    const el = document.getElementById('cat-rev-rows');
    if (!el) return;
    try {
        _catRevData = await api('/api/revenue/by-category?period=' + encodeURIComponent(_catRevPeriod));
        renderCategoryPanel(_catRevData);
    } catch (e) {
        el.innerHTML = `<div class="empty">${esc(e.message || 'error')}</div>`;
    }
}

function renderCategoryPanel(d) {
    const el = document.getElementById('cat-rev-rows');
    if (!el) return;
    // Reflect the active period + mode tabs (covers the initial load too).
    document.querySelectorAll('#cat-rev-tabs .period-tab')
        .forEach(b => b.classList.toggle('active', b.dataset.catPeriod === _catRevPeriod));
    document.querySelectorAll('#cat-rev-mode-tabs .period-tab')
        .forEach(b => b.classList.toggle('active', b.dataset.catMode === _catRevMode));
    const titleEl = document.getElementById('cat-rev-title');
    if (titleEl) titleEl.textContent = _catRevMode === 'platform' ? 'Revenue by Platform' : 'Revenue by Category';
    if (_catRevMode === 'platform') renderByPlatform(d, el);
    else renderByCategory(d, el);
}

function renderByCategory(d, el) {
    const byKey = {};
    _PLATFORMS.forEach(p => { byKey[p.key] = p; });
    const pLabel = k => (byKey[k] ? byKey[k].label : k);
    const pCls   = k => (byKey[k] ? byKey[k].cls : 'other');
    const cats = d.categories || [];
    const totalEl = document.getElementById('cat-rev-total');
    if (totalEl) totalEl.textContent = `$${(d.total || 0).toFixed(2)} · ${cats.length} categor${cats.length === 1 ? 'y' : 'ies'}`;
    if (!cats.length) { el.innerHTML = '<div class="empty">No sales in this period</div>'; return; }
    let html = '';
    for (const c of cats) {
        const plats = Object.entries(c.platforms || {})
            .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
        // Stacked bar — each segment is a platform's share WITHIN this category.
        let seg = '';
        for (const [k, v] of plats) {
            const w = c.total > 0 ? (v / c.total * 100) : 0;
            seg += `<div class="cat-seg bar-${pCls(k)}" style="width:${w.toFixed(2)}%" title="${esc(pLabel(k))} $${v.toFixed(2)}"></div>`;
        }
        const chips = plats.map(([k, v]) =>
            `<span class="cat-chip"><i class="cat-dot bar-${pCls(k)}"></i>${esc(pLabel(k))} <b>$${v.toFixed(2)}</b></span>`
        ).join('');
        html += `
            <div class="cat-row">
                <div class="cat-row-head">
                    <span class="cat-name">${esc(c.category)}</span>
                    <span class="cat-figs"><b class="cat-amt">$${(c.total || 0).toFixed(2)}</b><span class="cat-pct">${(c.pct || 0).toFixed(1)}%</span></span>
                </div>
                <div class="cat-bar">${seg}</div>
                <div class="cat-chips">${chips}</div>
            </div>`;
    }
    el.innerHTML = html;
}

function renderByPlatform(d, el) {
    const byKey = {};
    _PLATFORMS.forEach(p => { byKey[p.key] = p; });
    const pLabel = k => (byKey[k] ? byKey[k].label : k);
    // Pivot the by-category payload into platform → { total, cats:{category:$} }.
    const plats = {};
    for (const c of (d.categories || [])) {
        for (const [pk, v] of Object.entries(c.platforms || {})) {
            if (!(v > 0)) continue;
            const e = plats[pk] || (plats[pk] = { total: 0, cats: {} });
            e.cats[c.category] = (e.cats[c.category] || 0) + v;
            e.total += v;
        }
    }
    const rows = Object.entries(plats).sort((a, b) => b[1].total - a[1].total);
    const totalEl = document.getElementById('cat-rev-total');
    if (totalEl) totalEl.textContent = `$${(d.total || 0).toFixed(2)} · ${rows.length} platform${rows.length === 1 ? '' : 's'}`;
    if (!rows.length) { el.innerHTML = '<div class="empty">No sales in this period</div>'; return; }
    let html = '';
    for (const [pk, info] of rows) {
        const catList = Object.entries(info.cats)
            .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
        // Stacked bar — each segment is a CATEGORY's share within this platform.
        let seg = '';
        for (const [cat, v] of catList) {
            const w = info.total > 0 ? (v / info.total * 100) : 0;
            seg += `<div class="cat-seg" style="width:${w.toFixed(2)}%;background:${categoryColor(cat)}" title="${esc(cat)} $${v.toFixed(2)} (${w.toFixed(1)}%)"></div>`;
        }
        const chips = catList.map(([cat, v]) => {
            const w = info.total > 0 ? (v / info.total * 100) : 0;
            return `<span class="cat-chip"><i class="cat-dot" style="background:${categoryColor(cat)}"></i>${esc(cat)} <b>${w.toFixed(1)}%</b> <span class="cat-chip-amt">$${v.toFixed(2)}</span></span>`;
        }).join('');
        const platPct = d.total > 0 ? (info.total / d.total * 100) : 0;   // share of grand total
        html += `
            <div class="cat-row">
                <div class="cat-row-head">
                    <span class="cat-name">${esc(pLabel(pk))}</span>
                    <span class="cat-figs"><b class="cat-amt">$${info.total.toFixed(2)}</b><span class="cat-pct">${platPct.toFixed(1)}%</span></span>
                </div>
                <div class="cat-bar">${seg}</div>
                <div class="cat-chips">${chips}</div>
            </div>`;
    }
    el.innerHTML = html;
}

// Stats payload from the last /api/stats call, used to re-render the
// Revenue panel when the user toggles the period tabs.
let _statsRevenue = {};
let _statsCount = {};
// When a custom date is picked, we fetch /api/stats/date and store the
// response here. Render code checks _revenueDate first; if set, uses these.
let _statsRevenueDate = {};
let _statsCountDate = {};

// Independent period state for each panel (Today / Week / Month / custom date).
const _PERIOD_KEY_REV   = 'dashboard.revenuePeriod';
const _PERIOD_KEY_SALES = 'dashboard.salesPeriod';
const _DATE_KEY_REV     = 'dashboard.revenueDate';
const _DATE_KEY_SALES   = 'dashboard.salesDate';
function _loadPeriod(key, fallback) {
    try { return localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
}
function _savePeriod(key, val) {
    try { localStorage.setItem(key, val); } catch (e) {}
}
function _clearPeriod(key) {
    try { localStorage.removeItem(key); } catch (e) {}
}
let _revenuePeriod = _loadPeriod(_PERIOD_KEY_REV, 'today');
let _salesPeriod   = _loadPeriod(_PERIOD_KEY_SALES, 'today');
let _revenueDate   = _loadPeriod(_DATE_KEY_REV, '') || null;     // null when not in date mode
let _salesDate     = _loadPeriod(_DATE_KEY_SALES, '') || null;

const _PLATFORMS = [
    { key: 'funpay',         label: 'FunPay',         cls: 'funpay'    },
    { key: 'funpay2',        label: 'FunPay 2',       cls: 'funpay2'   },
    { key: 'u7buy',          label: 'u7buy',          cls: 'u7buy'     },
    { key: 'eldorado',       label: 'Eldorado',       cls: 'eldorado'  },
    { key: 'g2g',            label: 'G2G',            cls: 'g2g'       },
    { key: 'playerauctions', label: 'PlayerAuctions', cls: 'pa'        },
];
// Map period → field suffix in the /api/stats payload.
// 'today' → r.funpay_today / c.funpay_today
// 'week'  → r.funpay_week  / c.funpay_week
// 'month' → r.funpay_month / c.funpay_month
const _SUFFIX = { today: 'today', week: 'week', month: 'month' };

function renderRevenuePanel() {
    const el = document.getElementById('today-rev-rows');
    if (!el) return;
    // Date mode wins over period tabs. In date mode we use the
    // _statsRevenueDate / _statsCountDate cache (response from /api/stats/date)
    // and always read the "_today" suffix (because that's what the API returns
    // — those fields represent the picked date).
    let r, c, suf, total, totalCnt, headerLabel;
    if (_revenueDate) {
        r = _statsRevenueDate;
        c = _statsCountDate;
        suf = 'today';
        total = r.today || 0;
        totalCnt = c.today || 0;
        headerLabel = _revenueDate;
    } else {
        const period = _revenuePeriod;
        r = _statsRevenue;
        c = _statsCount;
        suf = _SUFFIX[period] || 'today';
        total = r[period] || 0;
        totalCnt = c[period] || 0;
        headerLabel = null;   // no extra label — period tab is self-evident
    }

    // Pull per-platform numbers for the chosen period.
    const rows = _PLATFORMS.map(p => {
        const fieldKey = p.key === 'playerauctions' ? `pa_${suf}` : `${p.key}_${suf}`;
        const rev = r[fieldKey] || 0;
        const cnt = c[fieldKey] || 0;
        return { ...p, rev, cnt };
    });
    rows.sort((a, b) => (b.rev || 0) - (a.rev || 0));   // highest revenue first

    // Update the panel header summary
    const totalEl = document.getElementById('rev-period-total');
    if (totalEl) {
        const summary = `$${total.toFixed(2)} · ${totalCnt} sales`;
        totalEl.textContent = headerLabel ? `${summary} · ${headerLabel}` : summary;
    }

    // Sync tab / date-input active state (mutually exclusive)
    document.querySelectorAll('.period-tabs[data-target="revenue"] .period-tab')
        .forEach(b => b.classList.toggle('active', !_revenueDate && b.dataset.period === _revenuePeriod));
    const dateInput = document.querySelector('.period-date[data-target="revenue"]');
    if (dateInput) {
        dateInput.value = _revenueDate || '';
        dateInput.classList.toggle('active', !!_revenueDate);
    }

    // Bar width = share of total revenue (so Eldorado's ~85% bar actually looks
    // ~85% wide, distinctly different from the 100% Total bar). The percentage
    // text overlays inside the bar.
    const fmtPct = (p) => p < 1 ? p.toFixed(1) : p.toFixed(0);
    let html = `
        <div class="rev-row rev-total">
            <span class="rev-label">Total</span>
            <div class="rev-bar">
                <div class="rev-bar-fill rev-bar-total" style="width:100%"></div>
                <span class="rev-bar-pct">100%</span>
            </div>
            <span class="rev-value">$${total.toFixed(2)}</span>
            <span class="rev-count">${totalCnt} sales</span>
        </div>`;
    for (const p of rows) {
        const pctOfTotal = total > 0 ? (p.rev / total * 100) : 0;
        // Clamp to 0.5% minimum so a >0 platform still shows a sliver of color
        const barW = p.rev > 0 ? Math.max(0.5, pctOfTotal) : 0;
        html += `
            <div class="rev-row platform-${p.cls}">
                <span class="rev-label">${esc(p.label)}</span>
                <div class="rev-bar" title="${fmtPct(pctOfTotal)}% of total">
                    <div class="rev-bar-fill bar-${p.cls}" style="width:${barW.toFixed(2)}%"></div>
                    <span class="rev-bar-pct">${fmtPct(pctOfTotal)}%</span>
                </div>
                <span class="rev-value">$${p.rev.toFixed(2)}</span>
                <span class="rev-count">${p.cnt} sales</span>
            </div>`;
    }
    el.innerHTML = html;
}

// Period rows are filtered client-side by category (the dropdown beside the
// period tabs). _salesAll holds the unfiltered fetch; _renderSalesPanel applies
// the filter so switching category doesn't refetch.
let _salesCategory = (function () {
    try { return localStorage.getItem('dashboard.salesCat') || 'all'; } catch (e) { return 'all'; }
})();
let _salesAll = [];
let _salesPlatform = (function () {
    try { return localStorage.getItem('dashboard.salesPlat') || 'all'; } catch (e) { return 'all'; }
})();
let _salesSearch = '';
let _salesSortKey = (function () { try { return localStorage.getItem('dashboard.salesSortKey') || 'sold_at'; } catch (e) { return 'sold_at'; } })();
let _salesSortDir = (function () { try { return localStorage.getItem('dashboard.salesSortDir') || 'desc'; } catch (e) { return 'desc'; } })();
const _saleCat = s => ((s.category || '').trim() || 'Uncategorized');

async function loadSalesPanel() {
    try {
        // Sync tab + date-input active state (mutually exclusive)
        document.querySelectorAll('.period-tabs[data-target="sales"] .period-tab')
            .forEach(b => b.classList.toggle('active', !_salesDate && b.dataset.period === _salesPeriod));
        const dateInput = document.querySelector('.period-date[data-target="sales"]');
        if (dateInput) {
            dateInput.value = _salesDate || '';
            dateInput.classList.toggle('active', !!_salesDate);
        }
        const qs = _salesDate
            ? '?d=' + encodeURIComponent(_salesDate)
            : '?p=' + encodeURIComponent(_salesPeriod);
        const d = await api('/api/sales/period' + qs);
        _salesAll = d.sales || [];
        _renderSalesPanel();
    } catch (e) { /* fail silently */ }
}

function setSalesCategory(c) {
    _salesCategory = c || 'all';
    try { localStorage.setItem('dashboard.salesCat', _salesCategory); } catch (e) {}
    _renderSalesPanel();
}

function setSalesPlatform(p) {
    _salesPlatform = p || 'all';
    try { localStorage.setItem('dashboard.salesPlat', _salesPlatform); } catch (e) {}
    _renderSalesPanel();
}

function _fillSalesFilter(sel, values, current, labelFn, allLabel) {
    // Rebuild options only when the value set changes, so the 15s auto-refresh
    // doesn't reset an open menu. Returns the (possibly reset) selection.
    if (!sel) return current;
    const sig = values.join('|');
    if (sel.dataset.sig !== sig) {
        sel.innerHTML = `<option value="all">${allLabel || 'All'}</option>` +
            values.map(v => `<option value="${esc(v)}">${esc(labelFn ? labelFn(v) : v)}</option>`).join('');
        sel.dataset.sig = sig;
    }
    const next = (current === 'all' || values.includes(current)) ? current : 'all';
    sel.value = next;
    return next;
}

function setSalesSearch(v) {
    _salesSearch = (v || '').trim();
    _renderSalesPanel();
}

function setSalesSort(key) {
    if (key === _salesSortKey) {
        _salesSortDir = _salesSortDir === 'asc' ? 'desc' : 'asc';
    } else {
        _salesSortKey = key;
        _salesSortDir = (key === 'price' || key === 'sold_at') ? 'desc' : 'asc';
    }
    try {
        localStorage.setItem('dashboard.salesSortKey', _salesSortKey);
        localStorage.setItem('dashboard.salesSortDir', _salesSortDir);
    } catch (e) {}
    _renderSalesPanel();
}

function _sortedSalesRows(rows) {
    const key = _salesSortKey, mul = _salesSortDir === 'asc' ? 1 : -1;
    const val = s => {
        if (key === 'price') return s.price || 0;
        if (key === 'sold_at') return s.sold_at || '';
        if (key === 'category') return _saleCat(s).toLowerCase();
        if (key === 'platform') return (s.platform || '').toLowerCase();
        if (key === 'username') return (s.username || '').toLowerCase();
        return '';
    };
    return rows.slice().sort((a, b) => {
        const va = val(a), vb = val(b);
        return va < vb ? -mul : va > vb ? mul : 0;
    });
}

function _renderSalesPanel() {
    const body = document.getElementById('today-sales-body');
    if (!body) return;
    const byKey = {};
    _PLATFORMS.forEach(p => { byKey[p.key] = p; });
    const pLabel = k => (byKey[k] ? byKey[k].label : k);
    // Populate the category + platform filters from what's present this period.
    _salesCategory = _fillSalesFilter(document.getElementById('sales-cat-filter'),
        Array.from(new Set(_salesAll.map(_saleCat))).sort(), _salesCategory, null, 'All categories');
    _salesPlatform = _fillSalesFilter(document.getElementById('sales-plat-filter'),
        Array.from(new Set(_salesAll.map(s => s.platform))).sort(), _salesPlatform, pLabel, 'All platforms');
    // Apply filters (category AND platform AND search), then sort.
    let rows = _salesAll;
    if (_salesCategory !== 'all') rows = rows.filter(s => _saleCat(s) === _salesCategory);
    if (_salesPlatform !== 'all') rows = rows.filter(s => s.platform === _salesPlatform);
    if (_salesSearch) {
        const q = _salesSearch.toLowerCase();
        rows = rows.filter(s => `${s.username || ''} ${s.platform || ''} ${_saleCat(s)}`.toLowerCase().includes(q));
    }
    rows = _sortedSalesRows(rows);
    // Reflect the active sort column in the header icons.
    document.querySelectorAll('.today-sales-wrap th.sortable').forEach(th => {
        const active = th.dataset.sort === _salesSortKey;
        th.classList.toggle('sort-active', active);
        const ic = th.querySelector('.sort-icon');
        if (ic) ic.textContent = active ? (_salesSortDir === 'asc' ? '▲' : '▼') : '↕';
    });
    const filtered = _salesCategory !== 'all' || _salesPlatform !== 'all' || !!_salesSearch;
    const cntEl = document.getElementById('today-sales-count');
    if (cntEl) {
        const total = rows.reduce((sum, s) => sum + (s.price || 0), 0);
        const parts = [filtered ? `(${rows.length} of ${_salesAll.length})` : `(${rows.length})`];
        parts.push(`$${total.toFixed(2)}`);
        if (_salesDate) parts.push(_salesDate);
        cntEl.textContent = parts.join(' · ');
    }
    if (!rows.length) {
        body.innerHTML = `<tr><td colspan="5" class="empty">No ${filtered ? 'matching ' : ''}sales in this period</td></tr>`;
        return;
    }
    body.innerHTML = rows.map(s => {
        const cat = _saleCat(s);
        return `
            <tr>
                <td>${_fmtSaleDate(s.sold_at)}</td>
                <td>${esc(s.username || '')}</td>
                <td><span class="badge platform-${s.platform}">${esc(s.platform)}</span></td>
                <td><span class="sales-cat-cell"><i class="cat-dot" style="background:${categoryColor(cat)}"></i>${esc(cat)}</span></td>
                <td><strong>$${(s.price || 0).toFixed(2)}</strong></td>
            </tr>`;
    }).join('');
}

function setRevenuePeriod(p) {
    _revenuePeriod = p;
    _revenueDate = null;                  // tab takes over → clear date mode
    _savePeriod(_PERIOD_KEY_REV, p);
    _clearPeriod(_DATE_KEY_REV);
    renderRevenuePanel();
}

function setSalesPeriod(p) {
    _salesPeriod = p;
    _salesDate = null;                    // tab takes over → clear date mode
    _savePeriod(_PERIOD_KEY_SALES, p);
    _clearPeriod(_DATE_KEY_SALES);
    loadSalesPanel();
}

async function setRevenueDate(d) {
    if (!d) {
        _revenueDate = null;
        _clearPeriod(_DATE_KEY_REV);
        renderRevenuePanel();
        return;
    }
    _revenueDate = d;
    _savePeriod(_DATE_KEY_REV, d);
    try {
        const resp = await api('/api/stats/date?d=' + encodeURIComponent(d));
        _statsRevenueDate = resp.revenue || {};
        _statsCountDate = resp.sales_count || {};
    } catch (e) {
        toast('Failed to load ' + d + ': ' + e.message, 'error');
        _statsRevenueDate = {}; _statsCountDate = {};
    }
    renderRevenuePanel();
}

function setSalesDate(d) {
    if (!d) {
        _salesDate = null;
        _clearPeriod(_DATE_KEY_SALES);
    } else {
        _salesDate = d;
        _savePeriod(_DATE_KEY_SALES, d);
    }
    loadSalesPanel();
}

async function loadFarmsyncSummary() {
    try {
        const d = await api('/api/farmsync/summary');
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
        set('fs-total-devices', (d.total_devices || 0).toLocaleString());
        set('fs-total-accounts', (d.total_accounts || 0).toLocaleString());
        set('fs-running-accounts', (d.running_accounts || 0).toLocaleString());
        set('fs-uptime', (d.uptime_pct || 0).toFixed(1) + '%');
        const navDevices = document.getElementById('nav-devices');
        if (navDevices) navDevices.textContent = d.total_devices || 0;
    } catch (e) {
        // FarmSync may be optional; fail silently
    }
}

async function loadLog() {
    try {
        const d = await api('/api/automation/log');
        const el = document.getElementById('automation-log');
        if (!el) return;
        el.innerHTML = (d.log || []).slice(-15).map(l => `<div class="log-line">${esc(l)}</div>`).join('') || '<div class="empty">No log entries yet</div>';
    } catch (e) {}
}

// ─── Sync ───
async function syncOne(platform) {
    toast(`Syncing ${platform}…`, 'info');
    try {
        const d = await api('/api/orders/' + platform + '/sync-sales', { method: 'POST' });
        toast(`${platform}: +${d.new_sales || 0} new sales`, (d.new_sales || 0) > 0 ? 'success' : 'info');
        loadDashboard();
    } catch (e) {
        toast(`${platform} sync failed: ${e.message}`, 'error');
    }
}

async function syncAllSales() {
    toast('Syncing all platforms…', 'info');
    const plats = ['funpay', 'funpay2', 'u7buy', 'eldorado', 'g2g', 'playerauctions'];
    let total = 0;
    for (const p of plats) {
        try {
            const d = await api('/api/orders/' + p + '/sync-sales', { method: 'POST' });
            total += (d.new_sales || 0);
        } catch (e) {
            toast(`${p}: ${e.message.slice(0, 60)}`, 'warning');
        }
    }
    toast(`All platforms synced — ${total} new sales total`, total > 0 ? 'success' : 'info');
    loadDashboard();
}

// ─── Sales date formatter (used by the dashboard's Today's Sales table) ───
function _fmtSaleDate(s) {
    if (!s) return '';
    try {
        const d = new Date(s);
        if (isNaN(d)) return s;
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
    } catch (e) { return s; }
}

// ─── Devices ───
let _devices = [];
const _tierOrder = ['0-29', '30-49', '50-69', '70-89', '90+'];
const _tierMeta = {
    '0-29':  { label: '0-29%',  icon: 'fa-triangle-exclamation', cls: 'tier-red' },
    '30-49': { label: '30-49%', icon: 'fa-cloud',                cls: 'tier-gray' },
    '50-69': { label: '50-69%', icon: 'fa-chart-line',           cls: 'tier-blue' },
    '70-89': { label: '70-89%', icon: 'fa-chart-column',         cls: 'tier-yellow' },
    '90+':   { label: '90%+',   icon: 'fa-circle-check',         cls: 'tier-green' },
};
// Persist tier collapse state across reloads via localStorage.
// First load: collapse only 90%+ to match the original design screenshot.
const _COLLAPSE_KEY = 'devices.collapsedTiers';
const _collapsedTiers = (() => {
    try {
        const stored = JSON.parse(localStorage.getItem(_COLLAPSE_KEY) || 'null');
        if (Array.isArray(stored)) return new Set(stored);
    } catch (e) { /* fall through to default */ }
    return new Set(['90+']);
})();
function _saveCollapsedTiers() {
    try { localStorage.setItem(_COLLAPSE_KEY, JSON.stringify([..._collapsedTiers])); }
    catch (e) { /* localStorage may be disabled in private mode */ }
}

function _uptimeCls(pct) {
    if (pct < 30) return 'tier-red';
    if (pct < 50) return 'tier-gray';
    if (pct < 70) return 'tier-blue';
    if (pct < 90) return 'tier-yellow';
    return 'tier-green';
}

// ─── Group Backups (Devices page) ───
async function loadGroupBackups() {
    try {
        const d = await api('/api/farmsync/group-backups');
        renderGroupBackups(d);
    } catch (e) {
        const body = document.getElementById('group-backups-body');
        if (body) body.innerHTML = `<div class="empty">Failed: ${esc(e.message)}</div>`;
    }
}

function renderGroupBackups(d) {
    const body = document.getElementById('group-backups-body');
    if (!body) return;
    const groups = d.groups || [], backups = d.backups || [];
    const sum = document.getElementById('gb-summary');
    if (sum) {
        const assigned = groups.filter(g => g.backup_id).length;
        sum.textContent = `(${assigned}/${groups.length} assigned · ${backups.length} backup${backups.length === 1 ? '' : 's'} in storage)`;
    }
    if (!groups.length) { body.innerHTML = '<div class="empty">No groups</div>'; return; }
    body.innerHTML = groups.map(g => {
        const opts = ['<option value="">— none —</option>'].concat(backups.map(b => {
            const nm = _fmtBackupName(b.name) || (b.id || '').slice(0, 8);
            const sel = b.id === g.backup_id ? ' selected' : '';
            return `<option value="${esc(b.id)}"${sel}>${esc(nm)} · ${esc((b.id || '').slice(0, 8))}</option>`;
        })).join('');
        const assignedCls = g.backup_id ? 'has-backup' : '';
        return `<div class="gb-row ${assignedCls}">
            <span class="gb-group">${esc(g.name)} <small>${g.device_count} dev</small></span>
            <select class="gb-select" data-group="${esc(g.name)}" data-count="${g.device_count}" onchange="setGroupBackup(this)">${opts}</select>
        </div>`;
    }).join('');
}

async function setGroupBackup(sel) {
    const group = sel.dataset.group;
    const count = parseInt(sel.dataset.count, 10) || 0;
    const backup_id = sel.value;
    if (backup_id) {
        const nm = sel.options[sel.selectedIndex].textContent;
        if (!confirm(`Assign "${nm}" to group "${group}"?\n\nThis FORCE-APPLIES it to all ${count} devices in the group that are on a different backup — re-imaging them (interrupting any that are farming) — and re-applies after each Restart VPS.`)) {
            loadGroupBackups();
            return;
        }
    }
    try {
        await api('/api/farmsync/group-backups', { method: 'POST', body: { group, backup_id } });
        toast(backup_id ? `Group "${group}" → backup set; automation will apply it` : `Group "${group}" backup cleared`, 'success');
        loadGroupBackups();
    } catch (e) {
        toast('Group backup: ' + e.message, 'error');
        loadGroupBackups();
    }
}

async function loadDevices(force) {
    const cont = document.getElementById('devices-container');
    try {
        const d = await api('/api/farmsync/devices' + (force ? '?force=1' : ''));
        _devices = d.devices || [];
        const navDevices = document.getElementById('nav-devices');
        if (navDevices) navDevices.textContent = _devices.length;
        const cnt = document.getElementById('devices-count');
        if (cnt) cnt.textContent = _devices.length + (_devices.length === 1 ? ' device' : ' devices');
        if (d.error) toast('FarmSync: ' + d.error, 'warning');
        renderDevicesView();
        refreshAutomationStatus();
    } catch (e) {
        if (cont) cont.innerHTML = `<div class="empty">Failed to load devices: ${esc(e.message)}</div>`;
    }
}

async function refreshAutomationStatus() {
    try {
        const s = await api('/api/farmsync/automation/status');
        const pill = document.getElementById('automation-pill');
        const dot = document.getElementById('automation-dot');
        const lbl = document.getElementById('automation-label');
        if (!pill || !dot || !lbl) return;
        if (s.running && !s.paused) {
            dot.className = 'dot connected';
            lbl.textContent = 'Automation: running';
            pill.dataset.state = 'running';
        } else if (s.paused) {
            dot.className = 'dot logged-out';
            lbl.textContent = 'Automation: paused';
            pill.dataset.state = 'paused';
        } else {
            dot.className = 'dot disconnected';
            lbl.textContent = s.script_exists ? 'Automation: stopped' : 'Automation: not installed';
            pill.dataset.state = 'stopped';
        }
    } catch (e) { /* ignore */ }
}

async function toggleAutomation() {
    const pill = document.getElementById('automation-pill');
    const state = pill?.dataset.state;
    const action = (state === 'running') ? 'stop' : 'start';
    toast(`${action === 'start' ? 'Starting' : 'Pausing'} automation…`, 'info');
    try {
        const r = await api('/api/farmsync/automation/' + action, { method: 'POST' });
        toast(r.message || (r.running ? 'Automation running' : 'Automation stopped'), r.ok ? 'success' : 'warning');
        refreshAutomationStatus();
    } catch (e) {
        toast('Automation: ' + e.message, 'error');
    }
}

// ─── Chrome freeze (sidebar pill — lets you run login.bat safely) ───
async function refreshChromeFreezeStatus() {
    try {
        const r = await api('/api/chrome/freeze/status');
        const pill = document.getElementById('freeze-pill');
        const lbl = document.getElementById('freeze-label');
        if (!pill || !lbl) return;
        if (r.frozen) {
            pill.dataset.state = 'frozen';
            lbl.textContent = 'Chrome: frozen';
        } else {
            pill.dataset.state = 'active';
            lbl.textContent = 'Chrome: active';
        }
    } catch (e) { /* ignore */ }
}

async function toggleChromeFreeze() {
    const pill = document.getElementById('freeze-pill');
    const wasFrozen = pill?.dataset.state === 'frozen';
    toast(wasFrozen ? 'Resuming Chrome activity…' : 'Freezing Chrome activity…', 'info');
    try {
        const r = await api('/api/chrome/freeze/toggle', { method: 'POST' });
        toast(r.message || (r.frozen ? 'Chrome frozen' : 'Chrome resumed'), r.ok ? 'success' : 'warning');
        refreshChromeFreezeStatus();
    } catch (e) {
        toast('Chrome freeze: ' + e.message, 'error');
    }
}

let _devicesSort = localStorage.getItem('devicesSort') || 'tier';  // 'tier' (offline-first within tiers) | 'status' (flat offline→online)

function toggleDevicesSort() {
    _devicesSort = (_devicesSort === 'status') ? 'tier' : 'status';
    try { localStorage.setItem('devicesSort', _devicesSort); } catch (e) {}
    renderDevicesView();
}

let _deviceSearch = '';
function setDeviceSearch(v) {
    _deviceSearch = (v || '').trim().toLowerCase();
    renderDevicesView();
}
function _deviceMatchesSearch(d) {
    if (!_deviceSearch) return true;
    return `${d.device_name || ''} ${d.device_note || ''} ${d.group_name || ''} ${d.os || ''} ${d.status || ''}`
        .toLowerCase().includes(_deviceSearch);
}

// Ordering rank for the offline-first sort: offline → dead-tool → online → disabled.
function _deviceRank(d) {
    if (d.status === 'offline') return 0;
    if (d.status === 'disabled') return 3;
    return d.heartbeat_fresh ? 2 : 1;   // online: tool-dead before healthy
}

function renderDevicesView() {
    const cont = document.getElementById('devices-container');
    if (!cont) return;
    const lbl = document.getElementById('devices-sort-label');
    if (lbl) lbl.textContent = _devicesSort === 'status' ? 'Flat (offline)' : 'Tier + offline';
    const list = _devices.filter(_deviceMatchesSearch);
    const cnt = document.getElementById('devices-count');
    if (cnt) cnt.textContent = _deviceSearch
        ? `${list.length} of ${_devices.length}`
        : `${_devices.length} ${_devices.length === 1 ? 'device' : 'devices'}`;
    if (!_devices.length) { cont.innerHTML = '<div class="empty">No devices</div>'; return; }
    if (!list.length) { cont.innerHTML = `<div class="empty">No devices match "${esc(_deviceSearch)}"</div>`; return; }
    if (_devicesSort === 'tier') {
        cont.className = 'devices-by-tier';
        cont.innerHTML = _renderByTier(list);
        return;
    }
    // Offline → online. Stable sort keeps the backend's name order within each rank.
    cont.className = 'device-grid';
    const sorted = list.slice().sort((a, b) => _deviceRank(a) - _deviceRank(b));
    cont.innerHTML = sorted.map(_deviceCard).join('');
}

function _renderByTier(devices) {
    const buckets = {};
    _tierOrder.forEach(t => buckets[t] = []);
    devices.forEach(d => { (buckets[d.tier] || (buckets[d.tier] = [])).push(d); });
    return _tierOrder.map(tier => {
        const meta = _tierMeta[tier];
        // offline → dead-tool → online within each tier
        const items = (buckets[tier] || []).slice().sort((a, b) => _deviceRank(a) - _deviceRank(b));
        const collapsed = _collapsedTiers.has(tier);
        return `
            <div class="tier-section ${meta.cls} ${collapsed ? 'collapsed' : ''}">
                <div class="tier-header" onclick="_toggleTier('${tier}')">
                    <span class="tier-badge"><i class="fas ${meta.icon}"></i> ${meta.label}</span>
                    <span class="tier-count">${items.length} ${items.length === 1 ? 'device' : 'devices'}</span>
                    <i class="fas fa-chevron-up tier-chevron"></i>
                </div>
                <div class="tier-body">
                    <div class="device-grid">${items.map(_deviceCard).join('') || '<div class="empty">None</div>'}</div>
                </div>
            </div>`;
    }).join('');
}

function _toggleTier(tier) {
    if (_collapsedTiers.has(tier)) _collapsedTiers.delete(tier);
    else _collapsedTiers.add(tier);
    _saveCollapsedTiers();
    renderDevicesView();
}

function _statClass(pct) {
    if (pct >= 90) return 'stat-hot';
    if (pct >= 70) return 'stat-warm';
    return 'stat-cool';
}

function _heartbeatLabel(d) {
    const m = d.heartbeat_age_min;
    if (m === null || m === undefined) return 'no heartbeat reported';
    if (m < 1) return 'heartbeat <1 min ago';
    if (m < 60) return `heartbeat ${Math.round(m)} min ago`;
    if (m < 60 * 24) return `heartbeat ${(m / 60).toFixed(1)} h ago`;
    return `heartbeat ${(m / 60 / 24).toFixed(1)} days ago`;
}

function _statusTooltip(d) {
    const hb = _heartbeatLabel(d);
    if (d.status === 'disabled') return 'Disabled';
    if (!d.heartbeat_fresh) return `Tool dead — ${hb}`;
    if (d.status === 'offline') return `Client not running — ${hb}`;
    return `Online — ${hb}`;
}

// Strip the "20260618T053728_" timestamp prefix + ".ldbk" for a clean label.
function _fmtBackupName(name) {
    if (!name) return '';
    return String(name).replace(/^\d{8}T\d{6}_/, '').replace(/\.ldbk$/i, '');
}

// One-line "current backup" indicator for a device card (name + short id + state).
function _backupLine(d) {
    const id = d.backup_id || '';
    if (!id) {
        const txt = d.backup_installing ? 'installing backup…' : 'no backup';
        return `<div class="dc-backup none"><i class="fas fa-box-archive"></i> <span>${txt}</span></div>`;
    }
    const nm = _fmtBackupName(d.backup_name) || '(unknown)';
    const sid = id.slice(0, 8);
    const cls = d.backup_is_latest ? 'latest' : 'outdated';
    const tag = d.backup_installing
        ? '<span class="bk-tag installing">installing…</span>'
        : (d.backup_is_latest ? '<span class="bk-tag latest">latest</span>'
                              : '<span class="bk-tag outdated">outdated</span>');
    return `<div class="dc-backup ${cls}" title="${esc(d.backup_name)}&#10;id: ${esc(id)}">
        <i class="fas fa-box-archive"></i>
        <span class="bk-name">${esc(nm)}</span>
        <span class="bk-id">${esc(sid)}</span>
        ${tag}
    </div>`;
}

function _deviceCard(d) {
    const name = esc(d.device_name || d.device_note || 'Unnamed');
    const cls = _uptimeCls(d.uptime_pct);
    const dotCls = d.status === 'online' ? 'online' : (d.status === 'disabled' ? 'disabled' : 'offline');
    const barW = Math.max(0, Math.min(100, d.uptime_pct));
    const statusTip = esc(_statusTooltip(d));
    return `
        <div class="device-card" data-device-id="${esc(d.id)}">
            <div class="dc-head">
                <div class="dc-name">${name} <span class="dc-status ${dotCls}" title="${statusTip}"></span></div>
                <div class="dc-pct ${cls}" title="${statusTip}">${(d.uptime_pct || 0).toFixed(0)}%</div>
            </div>
            <div class="dc-sub"><span class="dc-group">${esc(d.group_name || 'No group')}</span>${d.device_note ? ' • ' + esc(d.device_note) : ''}</div>
            <div class="dc-bar"><div class="dc-bar-fill ${cls}" style="width:${barW}%"></div></div>
            <div class="dc-stats">
                <div class="dc-stat ${_statClass(d.ram_pct)}" title="RAM ${(d.ram_pct||0).toFixed(0)}%">
                    <i class="fas fa-memory"></i>
                    <span>${(d.ram_used_gb||0).toFixed(1)}/${(d.ram_total_gb||0).toFixed(0)}G</span>
                </div>
                <div class="dc-stat ${_statClass(d.disk_pct)}" title="Disk ${(d.disk_pct||0).toFixed(0)}%">
                    <i class="fas fa-hard-drive"></i>
                    <span>${(d.disk_used_gb||0).toFixed(0)}/${(d.disk_total_gb||0).toFixed(0)}G</span>
                </div>
            </div>
            ${_backupLine(d)}
            <div class="dc-foot">
                <span class="dc-os" title="${esc(d.os || '')}">${esc(d.os || '')}</span>
                <span class="dc-accounts">${d.active_accounts}/${d.total_accounts}</span>
            </div>
            <button class="btn btn-xs btn-restart" onclick="restartVps('${esc(d.id)}', this)" title="Restart VPS via FarmSync"><i class="fas fa-power-off"></i> Restart VPS</button>
        </div>`;
}

async function restartVps(deviceId, btn) {
    if (!deviceId) return;
    if (!confirm('Send a Restart VPS task to FarmSync for this device?')) return;
    if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Sending…'; }
    try {
        const d = await api('/api/farmsync/devices/' + encodeURIComponent(deviceId) + '/restart-vps', { method: 'POST' });
        toast(d.ok ? 'Restart VPS queued' : ('Failed: ' + (d.error || 'unknown')), d.ok ? 'success' : 'error');
    } catch (e) {
        toast('Restart VPS failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-power-off"></i> Restart VPS'; }
    }
}

// ─── Settings / Platform ───
async function checkPlatformStatus() {
    try {
        const s = await api('/api/platform/status');
        const setDot = (id, status) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.className = 'dot ' + (status === 'connected' ? 'connected' : status === 'logged_out' ? 'logged-out' : 'disconnected');
        };
        ['funpay', 'funpay2', 'u7buy', 'eldorado', 'g2g', 'playerauctions'].forEach(p => {
            setDot('dot-' + p, s[p]);
            setDot('status-' + p, s[p]);
        });


        // Transition toasts
        ['funpay', 'funpay2', 'u7buy', 'eldorado', 'g2g', 'playerauctions'].forEach(p => {
            const was = _prevPlatformStatus[p];
            const now = s[p];
            if (was === 'connected' && now === 'disconnected') toast(`${p} disconnected`, 'warning');
            if (was !== 'logged_out' && now === 'logged_out') toast(`${p} logged out — run login.bat`, 'error');
            _prevPlatformStatus[p] = now;
        });
    } catch (e) {}
}

async function checkTrackstatStatus(force) {
    const dot = document.getElementById('status-trackstat');
    const hint = document.getElementById('trackstat-hint');
    if (force && hint) hint.textContent = 'checking…';
    try {
        const d = await api('/api/trackstat/status' + (force ? '?force=1' : ''));
        if (dot) dot.className = 'dot ' + (d.ok ? 'connected'
            : (d.state === 'token_expired' || d.state === 'no_token') ? 'logged-out' : 'disconnected');
        if (hint) {
            let msg = d.message || '';
            if (d.checked_at) {
                const t = new Date(d.checked_at);
                if (!isNaN(t)) msg += ' · ' + t.toLocaleTimeString();
            }
            hint.textContent = msg;
        }
    } catch (e) {
        if (dot) dot.className = 'dot disconnected';
        if (hint) hint.textContent = e.message || 'error';
    }
}

async function refreshPlatform(platform) {
    toast(`Refreshing ${platform}…`, 'info');
    try {
        const d = await api('/api/platform/' + platform + '/refresh', { method: 'POST' });
        // Endpoint exists only for u7buy in the slim build — generic 404 handled at caller
        toast(`${platform}: ${d.message || 'done'}`, d.ok ? 'success' : 'warning');
        checkPlatformStatus();
    } catch (e) {
        toast(`${platform}: ${e.message}`, 'error');
    }
}

async function refreshAllPlatforms() {
    toast('Refreshing all…', 'info');
    try {
        const results = await api('/api/platform/refresh-all', { method: 'POST' });
        let ok = 0, fail = 0;
        Object.values(results).forEach(r => r.ok ? ok++ : fail++);
        toast(`Refresh done — ${ok} ok, ${fail} failed`, fail === 0 ? 'success' : 'warning');
        checkPlatformStatus();
    } catch (e) {
        toast(e.message, 'error');
    }
}

// ═══════════════════════════════════════════════════════════════
//  Live Offers page
// ═══════════════════════════════════════════════════════════════

const PLATFORM_LABELS = {
    funpay: 'FunPay',
    funpay2: 'FunPay 2',
    u7buy: 'u7buy',
    eldorado: 'Eldorado',
    g2g: 'G2G',
};
const HTTP_PLATFORMS = new Set(['funpay', 'funpay2', 'funpay3', 'u7buy']);
let _offersCache = {};
let _offersFilter = '';
let _offersTab = localStorage.getItem('offersTab') || 'all';
let _offersSection = localStorage.getItem('offersSection') || 'all';
let _offersSortKey = localStorage.getItem('offersSortKey') || 'price';
let _offersSortDir = localStorage.getItem('offersSortDir') || 'desc';

// Offers are grouped by the *category* each one was uploaded under on its
// platform (Eldorado: the "Adopt Me" listing label; u7buy / FunPay: the game
// the SPU / lot-node lists under; G2G: the Roblox accounts category). Offers
// whose category couldn't be read fall into "Uncategorized".
const UNCATEGORIZED = 'Uncategorized';
function offerSection(o) {
    return (((o && o.category) || '') + '').trim() || UNCATEGORIZED;
}

function _fmtAgo(ts) {
    if (!ts) return 'never';
    const sec = Math.floor(Date.now() / 1000 - ts);
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
}

function _fmtDuration(sec) {
    if (sec == null || sec <= 0) return '—';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + (m > 0 ? m + 'm' : '0m');
    if (m > 0) return m + 'm';
    return sec + 's';
}
function _liveTimeClass(sec) {
    if (sec == null) return '';
    if (sec < 6 * 3600)   return 'fresh';
    if (sec < 24 * 3600)  return 'warming';
    return 'old';
}

async function loadOffers() {
    try {
        const data = await api('/api/offers/live');
        _offersCache = data;
        // Pre-classify every offer's section once so renders are cheap
        Object.values(_offersCache).forEach(entry => {
            (entry.offers || []).forEach(o => {
                o._section = offerSection(o);
            });
        });
        renderOffersStats();
        renderOffersTabs();
        renderOffersSectionTabs();
        renderOffersTable();
        refreshOffersBadge();
    } catch (e) {
        document.getElementById('offers-stats').innerHTML =
            `<div class="empty">Failed to load offers: ${esc(e.message)}</div>`;
    }
}

function _offerPlatformOrder() {
    const known = ['funpay', 'funpay2', 'funpay3', 'u7buy', 'eldorado', 'g2g'];
    const present = Object.keys(_offersCache);
    return known.filter(p => present.includes(p))
        .concat(present.filter(p => !known.includes(p)));
}

function renderOffersTabs() {
    const wrap = document.getElementById('offers-tabs');
    if (!wrap) return;
    const platforms = _offerPlatformOrder();
    let total = 0;
    platforms.forEach(p => { total += (_offersCache[p]?.count || 0); });
    const tabs = [
        { key: 'all', label: 'All', count: total },
        ...platforms.map(p => ({
            key: p,
            label: PLATFORM_LABELS[p] || p,
            count: _offersCache[p]?.count || 0,
        })),
    ];
    wrap.innerHTML = tabs.map(t => `
        <button class="offers-tab ${t.key === _offersTab ? 'active' : ''}"
                data-tab="${esc(t.key)}"
                onclick="setOffersTab('${esc(t.key)}')">
            ${esc(t.label)}<span class="tab-count">${t.count.toLocaleString()}</span>
        </button>`).join('');
}

function setOffersTab(key) {
    _offersTab = key;
    try { localStorage.setItem('offersTab', key); } catch (e) {}
    renderOffersTabs();
    renderOffersSectionTabs();   // section counts depend on platform
    renderOffersTable();
}

function renderOffersSectionTabs() {
    const wrap = document.getElementById('offers-section-tabs');
    if (!wrap) return;
    // Count offers per section within the currently-selected platform
    const platforms = (_offersTab === 'all')
        ? Object.keys(_offersCache)
        : [_offersTab];
    const counts = {};
    let total = 0;
    platforms.forEach(p => {
        (_offersCache[p]?.offers || []).forEach(o => {
            const k = o._section || offerSection(o);
            counts[k] = (counts[k] || 0) + 1;
            total++;
        });
    });
    // Category tabs are built dynamically from whatever categories the live
    // offers were uploaded under. Order by count desc; "Uncategorized" last.
    const keys = Object.keys(counts);
    if (!keys.length) {
        wrap.innerHTML = '';
        return;
    }
    keys.sort((a, b) => {
        if (a === UNCATEGORIZED) return 1;
        if (b === UNCATEGORIZED) return -1;
        return (counts[b] - counts[a]) || a.localeCompare(b);
    });
    // Reset to "all" if the current section has 0 offers in this platform
    if (_offersSection !== 'all' && !counts[_offersSection]) {
        _offersSection = 'all';
        try { localStorage.setItem('offersSection', 'all'); } catch (e) {}
    }
    const tabs = [{ key: 'all', label: 'All categories', count: total }]
        .concat(keys.map(k => ({ key: k, label: k, count: counts[k] })));
    wrap.innerHTML = tabs.map(t => `
        <button class="offers-tab ${t.key === _offersSection ? 'active' : ''}"
                data-section="${esc(t.key)}"
                onclick="setOffersSection(this.dataset.section)">
            ${esc(t.label)}<span class="tab-count">${t.count.toLocaleString()}</span>
        </button>`).join('');
}

function setOffersSection(key) {
    _offersSection = key;
    try { localStorage.setItem('offersSection', key); } catch (e) {}
    renderOffersSectionTabs();
    renderOffersTable();
}

function renderOffersStats() {
    const stats = document.getElementById('offers-stats');
    if (!stats) return;
    const platforms = _offerPlatformOrder();
    let total = 0;
    stats.innerHTML = platforms.map(plat => {
        const entry = _offersCache[plat] || {};
        const cnt = entry.count || 0;
        total += cnt;
        const label = PLATFORM_LABELS[plat] || plat;
        const updated = _fmtAgo(entry.updated_ts);
        const duration = entry.duration_ms ? `, ${(entry.duration_ms / 1000).toFixed(1)}s` : '';
        const err = entry.error;
        const stale = !!entry.stale;
        const cardCls = stale ? 'is-stale' : (err ? 'has-error' : '');
        const staleBadge = stale ? `<span class="stale-badge" title="Last scrape failed — showing prior data">stale</span>` : '';
        return `
          <div class="offer-stat-card ${cardCls}">
              <div class="stat-btn-row">
                  <button class="refresh-btn" title="Refresh ${esc(label)}" onclick="refreshOnePlatform('${esc(plat)}', this)">
                      <i class="fas fa-sync-alt"></i>
                  </button>
                  <button class="clear-btn" title="Clear cached ${esc(label)} offers" onclick="clearOnePlatform('${esc(plat)}')">
                      <i class="fas fa-trash"></i>
                  </button>
              </div>
              <div class="offer-stat-head">
                  <span class="offer-stat-name">${esc(label)}${staleBadge}</span>
              </div>
              <span class="offer-stat-count">${cnt.toLocaleString()}</span>
              <span class="offer-stat-meta">updated ${esc(updated)}${esc(duration)}</span>
              ${err ? `<span class="offer-stat-err">${esc(err)}</span>` : ''}
          </div>`;
    }).join('');
    const totalEl = document.getElementById('offers-total');
    if (totalEl) totalEl.textContent = total.toLocaleString() + ' offers';
}

function renderOffersTable() {
    const body = document.getElementById('offers-body');
    if (!body) return;
    // Flatten with platform + section filters, then text filter
    const filter = _offersFilter.toLowerCase().trim();
    const platforms = (_offersTab === 'all')
        ? Object.keys(_offersCache)
        : [_offersTab];
    const rows = [];
    platforms.forEach(plat => {
        const entry = _offersCache[plat] || {};
        (entry.offers || []).forEach(o => {
            const section = o._section || offerSection(o);
            if (_offersSection !== 'all' && section !== _offersSection) return;
            rows.push({
                platform:     plat,
                section:      section,
                offer_id:     o.offer_id || '',
                title:        o.title || '',
                price:        o.price,
                updated_ts:   entry.updated_ts,
                live_seconds: o.live_seconds,
                scrape_count: o.scrape_count,
                url:          o.url || null,
            });
        });
    });
    let filtered = rows;
    if (filter) {
        filtered = rows.filter(r =>
            r.title.toLowerCase().includes(filter) ||
            r.offer_id.toLowerCase().includes(filter) ||
            r.platform.toLowerCase().includes(filter) ||
            (r.section || '').toLowerCase().includes(filter)
        );
    }
    // Sort by the current column, direction respected. Empty/null values
    // always sort last regardless of direction so they don't crowd the top.
    const key = _offersSortKey, dir = _offersSortDir === 'asc' ? 1 : -1;
    filtered.sort((a, b) => {
        const va = a[key], vb = b[key];
        const aEmpty = (va == null || va === '');
        const bEmpty = (vb == null || vb === '');
        if (aEmpty && bEmpty) return 0;
        if (aEmpty) return 1;
        if (bEmpty) return -1;
        if (typeof va === 'number' && typeof vb === 'number') {
            return (va - vb) * dir;
        }
        return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
    });
    // Visual sort indicator on the header
    document.querySelectorAll('.offers-wrap th.sortable').forEach(th => {
        const active = th.dataset.sort === key;
        th.classList.toggle('sort-active', active);
        const icon = th.querySelector('.sort-icon');
        if (icon) {
            icon.className = 'fas sort-icon ' + (
                !active ? 'fa-sort' :
                dir === 1 ? 'fa-sort-up' : 'fa-sort-down'
            );
        }
    });
    const shownEl = document.getElementById('offers-shown');
    if (shownEl) shownEl.textContent = filter
        ? `(${filtered.length} of ${rows.length})`
        : `(${rows.length})`;
    if (!filtered.length) {
        body.innerHTML = '<tr><td colspan="5" class="empty">No offers</td></tr>';
        return;
    }
    // Cap render to first 600 rows so the DOM stays snappy
    const HARD_CAP = 600;
    const capped = filtered.slice(0, HARD_CAP);
    body.innerHTML = capped.map(r => {
        const tag = `<span class="platform-tag ${esc(r.platform)}">${esc(PLATFORM_LABELS[r.platform] || r.platform)}</span>`;
        const idCell = r.url
            ? `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.offer_id.slice(-12))}</a>`
            : esc(r.offer_id.slice(-12));
        const price = (r.price != null && !isNaN(r.price))
            ? '$' + Number(r.price).toFixed(2)
            : '—';
        const liveTxt = _fmtDuration(r.live_seconds);
        const liveCls = _liveTimeClass(r.live_seconds);
        const liveTooltip = r.scrape_count
            ? `seen in ${r.scrape_count} scrape${r.scrape_count === 1 ? '' : 's'}`
            : '';
        return `<tr>
            <td>${tag}</td>
            <td class="id-cell">${idCell}</td>
            <td class="title-cell" title="${esc(r.title)}">${esc(r.title)}</td>
            <td class="price-cell">${esc(price)}</td>
            <td class="live-time-cell ${liveCls}" title="${esc(liveTooltip)}">${esc(liveTxt)}</td>
            <td class="updated-cell">${esc(_fmtAgo(r.updated_ts))}</td>
        </tr>`;
    }).join('');
    if (filtered.length > HARD_CAP) {
        body.innerHTML += `<tr><td colspan="6" class="empty">(showing first ${HARD_CAP} of ${filtered.length} — narrow the filter to see more)</td></tr>`;
    }
}

async function refreshOnePlatform(platform, btnEl) {
    if (btnEl) btnEl.classList.add('spinning');
    toast(`Refreshing ${PLATFORM_LABELS[platform] || platform}…`, 'info');
    try {
        const r = await api('/api/offers/live/refresh/' + encodeURIComponent(platform),
                            { method: 'POST' });
        toast(r.message || 'done', r.ok ? 'success' : 'warning');
        // For Chrome platforms, refresh fires in background — re-poll after 35s
        if (platform === 'eldorado' || platform === 'g2g') {
            setTimeout(loadOffers, 35000);
        } else {
            await loadOffers();
        }
    } catch (e) {
        toast(`Refresh ${platform}: ${e.message}`, 'error');
    } finally {
        if (btnEl) btnEl.classList.remove('spinning');
    }
}

async function refreshAllOffers() {
    toast('Refreshing all platforms…', 'info');
    try {
        await api('/api/offers/live/refresh', { method: 'POST' });
        await loadOffers();
        toast('Refreshed (Chrome platforms continue in background)', 'success');
        // Re-poll once Chrome scrapes should be done
        setTimeout(loadOffers, 45000);
    } catch (e) {
        toast('Refresh all: ' + e.message, 'error');
    }
}

async function clearOnePlatform(platform) {
    const entry = _offersCache[platform] || {};
    const cnt = entry.count || 0;
    if (cnt > 0 && !confirm(`Clear cached ${PLATFORM_LABELS[platform] || platform} offers (${cnt})? `
                          + `The next refresh will re-scrape from scratch.`)) {
        return;
    }
    try {
        await api('/api/offers/live/clear/' + encodeURIComponent(platform), { method: 'POST' });
        toast(`${PLATFORM_LABELS[platform] || platform} cleared`, 'success');
        await loadOffers();
    } catch (e) {
        toast('Clear ' + platform + ': ' + e.message, 'error');
    }
}

async function clearAllOffers() {
    const total = Object.values(_offersCache).reduce((s, e) => s + (e.count || 0), 0);
    if (total > 0 && !confirm(`Clear ALL cached offers (${total} across all platforms)?`)) {
        return;
    }
    try {
        await api('/api/offers/live/clear', { method: 'POST' });
        toast('All offers cleared', 'success');
        await loadOffers();
    } catch (e) {
        toast('Clear all: ' + e.message, 'error');
    }
}

async function refreshOffersBadge() {
    // Lightweight: pull stat counts (the API is cheap, doesn't re-scrape)
    try {
        const data = await api('/api/offers/live');
        const total = Object.values(data).reduce((s, e) => s + (e.count || 0), 0);
        const badge = document.getElementById('nav-offers');
        if (badge) badge.textContent = total;
    } catch (e) { /* ignore */ }
}

document.addEventListener('input', e => {
    if (e.target && e.target.id === 'offers-filter') {
        _offersFilter = e.target.value || '';
        renderOffersTable();
    }
});

// Click handler for sortable column headers in the Offers table.
// Same column twice = toggle direction; new column = use sensible default
// (numeric/timestamp columns start desc, text columns start asc).
document.addEventListener('click', e => {
    const th = e.target.closest('.offers-wrap th.sortable');
    if (!th) return;
    const key = th.dataset.sort;
    if (key === _offersSortKey) {
        _offersSortDir = (_offersSortDir === 'asc') ? 'desc' : 'asc';
    } else {
        _offersSortKey = key;
        _offersSortDir = (key === 'price' || key === 'updated_ts' || key === 'live_seconds')
            ? 'desc' : 'asc';
    }
    try {
        localStorage.setItem('offersSortKey', _offersSortKey);
        localStorage.setItem('offersSortDir', _offersSortDir);
    } catch (e) {}
    renderOffersTable();
});

// ═══════════════════════════════════════════════════════════════
//  Accounts page — per-account cumulative farm (live) time
// ═══════════════════════════════════════════════════════════════

let _accountsCache = [];        // array of {username, devices[], groups[], live_seconds, last_update}
let _accountsFilter = '';
let _accountsSortKey = localStorage.getItem('accountsSortKey') || 'live_seconds';
let _accountsSortDir = localStorage.getItem('accountsSortDir') || 'desc';

async function loadAccounts() {
    try {
        const d = await api('/api/accounts');
        _accountsCache = d.accounts || [];
        const totalEl = document.getElementById('accounts-total');
        if (totalEl) totalEl.textContent = (d.count || 0).toLocaleString() + ' accounts';
        const navBadge = document.getElementById('nav-accounts');
        if (navBadge) navBadge.textContent = (d.count || 0).toLocaleString();
        renderAccountsTable();
    } catch (e) {
        const body = document.getElementById('accounts-body');
        if (body) body.innerHTML = `<tr><td colspan="4" class="empty">Failed to load accounts: ${esc(e.message)}</td></tr>`;
    }
}

function renderAccountsTable() {
    const body = document.getElementById('accounts-body');
    if (!body) return;
    const filter = _accountsFilter.toLowerCase().trim();
    let rows = _accountsCache;
    if (filter) {
        rows = rows.filter(a =>
            (a.username || '').toLowerCase().includes(filter) ||
            (a.devices || []).join(' ').toLowerCase().includes(filter) ||
            (a.groups || []).join(' ').toLowerCase().includes(filter)
        );
    }
    // Sort by current column (devices/groups compare on their joined text).
    const key = _accountsSortKey, dir = _accountsSortDir === 'asc' ? 1 : -1;
    rows = rows.slice().sort((a, b) => {
        let va = a[key], vb = b[key];
        if (key === 'devices' || key === 'groups') { va = (a[key] || []).join(', '); vb = (b[key] || []).join(', '); }
        const aE = (va == null || va === ''), bE = (vb == null || vb === '');
        if (aE && bE) return 0;
        if (aE) return 1;
        if (bE) return -1;
        if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
        return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
    });
    // Header sort indicator
    document.querySelectorAll('.accounts-wrap th.sortable').forEach(th => {
        const active = th.dataset.sort === key;
        th.classList.toggle('sort-active', active);
        const icon = th.querySelector('.sort-icon');
        if (icon) icon.className = 'fas sort-icon ' + (!active ? 'fa-sort' : dir === 1 ? 'fa-sort-up' : 'fa-sort-down');
    });
    const shownEl = document.getElementById('accounts-shown');
    if (shownEl) shownEl.textContent = filter ? `(${rows.length} of ${_accountsCache.length})` : `(${_accountsCache.length})`;
    const pager = document.getElementById('accounts-pagination');
    if (!rows.length) {
        body.innerHTML = '<tr><td colspan="4" class="empty">No accounts have farmed yet</td></tr>';
        if (pager) pager.innerHTML = '';
        return;
    }
    const pg = paginate(rows, 'accounts');
    body.innerHTML = pg.items.map(a => {
        const devs = a.devices || [], grps = a.groups || [];
        const devCell = devs.length
            ? (devs.length > 1 ? `<span class="multi-badge" title="${esc(devs.join(', '))}">${devs.length}</span> ` : '') + esc(devs.join(', '))
            : '—';
        const grpCell = grps.length ? esc(grps.join(', ')) : '—';
        const lu = Number(a.last_update) || 0;
        // Credited < ~1.5 cycles ago → account is still on a device → tick live
        const active = lu > 0 && (Date.now() / 1000 - lu) < 1800;
        const shown = (a.live_seconds || 0) + (active ? Math.max(0, Date.now() / 1000 - lu) : 0);
        return `<tr>
            <td class="title-cell">${esc(a.username)}</td>
            <td class="acct-devs" title="${esc(devs.join(', '))}">${devCell}</td>
            <td class="acct-grps" title="${esc(grps.join(', '))}">${grpCell}</td>
            <td class="live-time-cell acct-live" data-base="${a.live_seconds || 0}" data-lu="${lu}" data-active="${active ? 1 : 0}" title="${active ? 'On a device — ticking live' : 'Not on a device right now — frozen'}">${esc(_fmtLiveTime(shown))}</td>
        </tr>`;
    }).join('');
    renderPagination('accounts-pagination', 'accounts', pg.total, pg.page, pg.totalPages, 'gotoAccountsPage');
}

function gotoAccountsPage(n) { _pages.accounts = n; renderAccountsTable(); }

// Farm-time formatter: shows seconds under 1h so the value visibly ticks live.
function _fmtLiveTime(sec) {
    if (sec == null || sec <= 0) return '0s';
    sec = Math.floor(sec);
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    return `${m}m ${s}s`;
}

// Tick live-time cells every second for accounts still on a device, so the value
// climbs smoothly between the ~20-min automation cycles (committed base +
// elapsed since the last cycle credited it). Updates text only — no re-sort.
function tickAccountLiveTimes() {
    const nowSec = Date.now() / 1000;
    document.querySelectorAll('.accounts-wrap td.acct-live[data-active="1"]').forEach(td => {
        const base = parseFloat(td.dataset.base) || 0;
        const lu = parseFloat(td.dataset.lu) || 0;
        td.textContent = _fmtLiveTime(base + Math.max(0, nowSec - lu));
    });
}

document.addEventListener('input', e => {
    if (e.target && e.target.id === 'accounts-filter') {
        _accountsFilter = e.target.value || '';
        _pages.accounts = 1;
        renderAccountsTable();
    }
});
document.addEventListener('click', e => {
    const th = e.target.closest('.accounts-wrap th.sortable');
    if (!th) return;
    const key = th.dataset.sort;
    if (key === _accountsSortKey) {
        _accountsSortDir = (_accountsSortDir === 'asc') ? 'desc' : 'asc';
    } else {
        _accountsSortKey = key;
        _accountsSortDir = (key === 'live_seconds') ? 'desc' : 'asc';
    }
    try {
        localStorage.setItem('accountsSortKey', _accountsSortKey);
        localStorage.setItem('accountsSortDir', _accountsSortDir);
    } catch (e) {}
    _pages.accounts = 1;
    renderAccountsTable();
});

// ─── Boot ───
(function init() {
    const page = location.hash.replace('#', '') || 'dashboard';
    navigateTo(document.getElementById('page-' + page) ? page : 'dashboard');
    checkPlatformStatus();
    refreshChromeFreezeStatus();
    // Period tab click handlers (Revenue + Sales panels each independent)
    document.querySelectorAll('.period-tabs').forEach(group => {
        const target = group.dataset.target;  // 'revenue' or 'sales'
        group.addEventListener('click', e => {
            const btn = e.target.closest('.period-tab');
            if (!btn) return;
            const p = btn.dataset.period;
            if (target === 'revenue') setRevenuePeriod(p);
            else if (target === 'sales') setSalesPeriod(p);
        });
    });
    // Date input change handlers (mutually exclusive with the period tabs)
    document.querySelectorAll('.period-date').forEach(input => {
        // Cap the max date at today so a future date can't be picked
        const today = new Date();
        const ymd = today.getFullYear() + '-' +
            String(today.getMonth() + 1).padStart(2, '0') + '-' +
            String(today.getDate()).padStart(2, '0');
        input.max = ymd;
        const target = input.dataset.target;
        input.addEventListener('change', () => {
            const d = input.value || null;
            if (target === 'revenue') setRevenueDate(d);
            else if (target === 'sales') setSalesDate(d);
        });
    });
    // Re-hydrate date-mode from localStorage on page load (if applicable)
    if (_revenueDate) setRevenueDate(_revenueDate);
    if (_salesDate)   setSalesDate(_salesDate);
    // Auto-refresh: dashboard every 15s, devices every 30s
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-dashboard') loadDashboard();
    }, 15000);
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-devices') loadDevices();
    }, 30000);
    // Offers page auto-refresh every 60s (when active)
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-offers') loadOffers();
    }, 60000);
    // Accounts page auto-refresh every 30s (when active)
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-accounts') loadAccounts();
    }, 30000);
    // Tick the Accounts live-time cells every second so they climb smoothly
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-accounts') tickAccountLiveTimes();
    }, 1000);
    // Sidebar offers count refresh every 5 min (regardless of active page)
    setInterval(refreshOffersBadge, 300000);
    refreshOffersBadge();
    // Platform status poll every 30s
    setInterval(checkPlatformStatus, 30000);
    setInterval(checkTrackstatStatus, 1200000);   // YummyTrackStat API status — every 20 min
    setInterval(loadZpSolver, 60000);             // ZP solver balance/queue — every 60s
    // Chrome freeze pill poll every 30s (so an out-of-band toggle reflects in the UI)
    setInterval(refreshChromeFreezeStatus, 30000);
    // Automation status poll every 30s (only while on Devices page)
    setInterval(() => {
        const active = document.querySelector('.page.active')?.id;
        if (active === 'page-devices') refreshAutomationStatus();
    }, 30000);
    // Keyboard shortcuts
    document.addEventListener('keydown', e => {
        if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '')) return;
        if (e.key === '1') navigateTo('dashboard');
        else if (e.key === '2') navigateTo('devices');
        else if (e.key === '3') navigateTo('offers');
        else if (e.key === '4') navigateTo('accounts');
        else if (e.key === '5') navigateTo('settings');
        else if (e.key.toLowerCase() === 'r') {
            const active = document.querySelector('.page.active')?.id;
            if (active === 'page-dashboard') loadDashboard();
            else if (active === 'page-devices') loadDevices(true);
            else if (active === 'page-offers') loadOffers();
            else if (active === 'page-accounts') loadAccounts();
            else checkPlatformStatus();
            toast('Refreshed', 'info');
        }
    });
})();
