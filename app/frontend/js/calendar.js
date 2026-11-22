/**
 * 投資行事曆（Market Calendar）
 * =============================
 * 通用重要時機點（美台總經數據、FOMC、巨頭財報）／庫存股／觀察清單。
 * 事件由 Lambda（data/calendar_macro）寫進 MongoDB，後端 GET /api/calendar/events 依使用者清單組合。
 *
 * 視圖切換規則與其他模組一致：顯示自己時把別人藏起來，別人顯示時呼叫 hideCalendarView()。
 * 所有來自 API 的字串一律以 textContent 寫入，不經過 innerHTML。
 * 日期一律以台北時間解讀（event_date 已是台北日期；event_at 是 UTC，顯示時轉台北）。
 */

const calState = {
    tab: 'MACRO',
    view: null,                 // 'month' | 'list'；第一次開啟時依螢幕寬度決定
    month: null,                // { y, m }（m: 1–12）
    importance: 1,
    selected: null,             // 月曆模式選取的日期 'YYYY-MM-DD'
    events: [],
    counts: { holding: 0, watchlist: 0 },
    cache: new Map(),
    requestId: 0,
};

const CAL_PREF_KEY = 'calendar.prefs';
const CAL_WEEKDAY = ['週日', '週一', '週二', '週三', '週四', '週五', '週六'];
const CAL_TAB_LABEL = { MACRO: '通用重要時機點', HOLDING: '庫存股', WATCHLIST: '觀察清單', ALL: '全部' };
const CAL_STATUS = {
    ESTIMATED: { text: '預估', cls: 'est' },
    CONFIRMED: { text: '已排定', cls: 'conf' },
    RELEASED: { text: '已公布', cls: 'rel' },
    CANCELLED: { text: '取消', cls: 'cancel' },
};
const CAL_IMPORTANCE = { 3: '高', 2: '中', 1: '低' };
const CAL_READING_LABEL = {
    'CPI': 'CPI', 'Core CPI': '核心 CPI',
    'Nonfarm Payrolls': '非農就業人數', 'Unemployment Rate': '失業率', 'Average Hourly Earnings': '平均時薪',
    'PCE Price Index': 'PCE 物價', 'Core PCE Price Index': '核心 PCE 物價',
    'Fed Interest Rate Decision': '聯邦基金利率',
    'GDP': 'GDP', 'PPI': 'PPI', 'Core PPI': '核心 PPI',
    'Retail Sales': '零售銷售', 'Core Retail Sales': '核心零售銷售',
    'ISM Manufacturing PMI': 'ISM 製造業', 'ISM Non-Manufacturing PMI': 'ISM 服務業', 'ISM Services PMI': 'ISM 服務業',
    'CB Consumer Confidence': '消費者信心', 'Michigan Consumer Sentiment': '密大信心',
};
/** 'CPI MoM' → 'CPI 月增'；Lambda 只在能分辨時才加 MoM／YoY 後綴 */
function calReadingLabel(label) {
    const m = String(label || '').match(/^(.*) (MoM|YoY)$/);
    const base = m ? m[1] : label;
    const text = CAL_READING_LABEL[base] || base;
    return m ? `${text} ${m[2] === 'MoM' ? '月增' : '年增'}` : text;
}

const CAL_BIG_TECH_TEXT = 'Google、Meta、NVIDIA、AMD、Broadcom、Intel、台積電';

const calEl = (id) => document.getElementById(id);

function calNode(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function calIcon(name) {
    const i = document.createElement('i');
    i.setAttribute('data-lucide', name);
    return i;
}

function calRefreshIcons() {
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// ── 日期工具（全部用台北日期字串 'YYYY-MM-DD'） ─────────────────────

function calTodayTaipei() {
    // en-CA 的格式剛好是 YYYY-MM-DD
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Taipei' }).format(new Date());
}

function calIso(y, m, d) {
    return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
}

function calParse(iso) {
    const [y, m, d] = iso.split('-').map(Number);
    return { y, m, d };
}

/** 用 UTC 正午運算日期，避免瀏覽器時區與日光節約時間影響 */
function calAddDays(iso, n) {
    const { y, m, d } = calParse(iso);
    const t = new Date(Date.UTC(y, m - 1, d, 12));
    t.setUTCDate(t.getUTCDate() + n);
    return calIso(t.getUTCFullYear(), t.getUTCMonth() + 1, t.getUTCDate());
}

function calWeekday(iso) {
    const { y, m, d } = calParse(iso);
    return new Date(Date.UTC(y, m - 1, d, 12)).getUTCDay();
}

/** 月曆格子的範圍：從當月 1 日所在週的週一，到月底所在週的週日 */
function calGridRange(y, m) {
    const first = calIso(y, m, 1);
    const start = calAddDays(first, -((calWeekday(first) + 6) % 7));
    const lastDay = new Date(Date.UTC(y, m, 0, 12)).getUTCDate();
    const last = calIso(y, m, lastDay);
    const end = calAddDays(last, (7 - calWeekday(last)) % 7);
    return { start, end };
}

function calDayLabel(iso, withYear = false) {
    const { y, m, d } = calParse(iso);
    return `${withYear ? `${y} 年 ` : ''}${m} 月 ${d} 日（${CAL_WEEKDAY[calWeekday(iso)]}）`;
}

function calTimeTaipei(isoDateTime) {
    return new Date(isoDateTime).toLocaleTimeString('zh-TW', {
        timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

function calStamp(isoDateTime) {
    return new Date(isoDateTime).toLocaleString('zh-TW', {
        timeZone: 'Asia/Taipei', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

// ── 事件呈現規則 ───────────────────────────────────────────────────

function calCategory(e) {
    if (e.type === 'CENTRAL_BANK') return 'cb';
    if (e.type === 'EARNINGS') return 'earn';
    return e.country === 'TW' ? 'tw' : 'us';
}

function calShortTitle(e) {
    if (e.type === 'EARNINGS') return e.market === 'TW' ? `${e.name || e.symbol} 法說會` : `${e.symbol} 財報`;
    return (e.title || '').replace(/（.*?）/g, '').replace(/^美國\s*/, '美 ').replace(/^台灣\s*/, '台 ');
}

function calTimeText(e) {
    if (e.event_at) return calTimeTaipei(e.event_at);
    if (e.time_hint === 'AMC') return '美股盤後';
    if (e.time_hint === 'BMO') return '美股盤前';
    return e.type === 'EARNINGS' ? '時間未定' : '全天';
}

function calTimeNote(e) {
    if (e.time_hint === 'AMC' && e.market === 'US') return `美東 ${e.market_date.slice(5).replace('-', '/')} 收盤後，台北清晨`;
    if (e.type === 'EARNINGS' && e.market === 'TW') return '台北時間下午舉行，以公司公告為準';
    if (e.type === 'EARNINGS' && e.status === 'ESTIMATED') return '日期以公司公告為準';
    return '';
}

/** '0.3%' / '165K' / '$3.45' → 數字；無法比較時回 null */
function calNumber(text) {
    if (!text) return null;
    const m = String(text).replace(/,/g, '').match(/-?\d+(\.\d+)?/);
    return m ? Number(m[0]) : null;
}

// ── 視圖切換 ───────────────────────────────────────────────────────

function calLoadPrefs() {
    try {
        const saved = JSON.parse(localStorage.getItem(CAL_PREF_KEY) || '{}');
        if (CAL_TAB_LABEL[saved.tab]) calState.tab = saved.tab;
        if (saved.view === 'month' || saved.view === 'list') calState.view = saved.view;
        if ([1, 2, 3].includes(saved.importance)) calState.importance = saved.importance;
    } catch (_) { /* 無痕模式或被封鎖時用預設值 */ }
}

function calSavePrefs() {
    try {
        localStorage.setItem(CAL_PREF_KEY, JSON.stringify({
            tab: calState.tab, view: calState.view, importance: calState.importance,
        }));
    } catch (_) { /* 忽略 */ }
}

function showCalendarView() {
    if (typeof maybeParkViewportForLeavingChat === 'function') {
        maybeParkViewportForLeavingChat(typeof state !== 'undefined' ? state.currentChatId : null);
    }
    if (typeof hideExploreView === 'function') hideExploreView();
    if (typeof hideDeepResearchView === 'function') hideDeepResearchView();
    if (typeof hideNotesView === 'function') hideNotesView();
    if (typeof hideFeedViews === 'function') hideFeedViews();
    if (typeof hidePortfolioView === 'function') hidePortfolioView();
    if (typeof exitNoteSelectMode === 'function') exitNoteSelectMode();

    calEl('chat-messages').style.display = 'none';
    calEl('project-view').style.display = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.querySelector('.chat-input-area').style.display = 'none';

    calEl('calendar-view').style.display = 'flex';
    calEl('calendar-btn')?.classList.add('active');
    if (typeof setMainChatTitle === 'function') setMainChatTitle('行事曆');

    if (!calState.month) {
        calLoadPrefs();
        if (!calState.view) calState.view = window.matchMedia('(max-width: 768px)').matches ? 'list' : 'month';
        const today = calParse(calTodayTaipei());
        calState.month = { y: today.y, m: today.m };
        calSyncControls();
    }
    calLoad();
}

function hideCalendarView() {
    const view = calEl('calendar-view');
    if (view) view.style.display = 'none';
    calEl('calendar-btn')?.classList.remove('active');
}

function calSyncControls() {
    document.querySelectorAll('.cal-tab').forEach((btn) => {
        const on = btn.dataset.tab === calState.tab;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    document.querySelectorAll('.cal-seg button').forEach((btn) => {
        const on = btn.dataset.view === calState.view;
        btn.classList.toggle('active', on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    calEl('cal-importance').value = String(calState.importance);
    const { y, m } = calState.month;
    calEl('cal-month-label').textContent = `${y} 年 ${m} 月`;
    calEl('cal-month').classList.toggle('hidden', calState.view !== 'month');
    calEl('cal-list').classList.toggle('hidden', calState.view !== 'list');
}

// ── 讀取 ───────────────────────────────────────────────────────────

async function calLoad() {
    const { y, m } = calState.month;
    const { start, end } = calGridRange(y, m);
    const cacheKey = `${calState.tab}|${start}|${calState.importance}`;
    calSyncControls();

    const cached = calState.cache.get(cacheKey);
    if (cached && Date.now() - cached.at < 5 * 60 * 1000) {
        calApply(cached.data);
        return;
    }
    const requestId = ++calState.requestId;
    calEl('calendar-view').classList.add('cal-loading');
    try {
        const params = new URLSearchParams({
            from: start, to: end, tab: calState.tab, min_importance: String(calState.importance),
        });
        const res = await authFetch(`${state.apiBase}/calendar/events?${params}`);
        if (!res) throw new Error('尚未登入');
        let body = {};
        try { body = await res.json(); } catch (_) { /* 非 JSON */ }
        if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `讀取失敗（HTTP ${res.status}）`);
        if (requestId !== calState.requestId) return;
        calState.cache.set(cacheKey, { at: Date.now(), data: body.data });
        calApply(body.data);
    } catch (err) {
        if (requestId !== calState.requestId) return;
        calState.events = [];
        calRender();
        calShowNotice(err.message || '行事曆讀取失敗', 'error');
    } finally {
        if (requestId === calState.requestId) calEl('calendar-view').classList.remove('cal-loading');
    }
}

function calApply(data) {
    calState.events = data.events || [];
    calState.counts = data.counts || calState.counts;
    calRenderFreshness(data.data_freshness || {});
    calRenderCounts();
    calRender();
}

function calRenderCounts() {
    document.querySelectorAll('.cal-tab-count').forEach((node) => {
        const n = calState.counts[node.dataset.count] || 0;
        node.textContent = n ? String(n) : '';
    });
}

function calRenderFreshness(f) {
    const node = calEl('cal-freshness');
    node.className = 'cal-freshness';
    if (!f.macro_fetched_at) {
        node.textContent = '行事曆資料尚未建立';
        node.classList.add('warn');
    } else if (f.stale) {
        node.textContent = `資料已超過 36 小時未更新（最後更新 ${calStamp(f.macro_fetched_at)}）`;
        node.classList.add('warn');
    } else {
        node.textContent = `時間皆為台北時間 · 資料更新於 ${calStamp(f.macro_fetched_at)}`;
    }
}

function calShowNotice(text, kind) {
    const node = calEl('cal-notice');
    node.textContent = text || '';
    node.className = `cal-notice${kind ? ` ${kind}` : ''}`;
    node.classList.toggle('hidden', !text);
}

// ── 繪製 ───────────────────────────────────────────────────────────

function calByDay() {
    const map = new Map();
    calState.events.forEach((e) => {
        if (!map.has(e.event_date)) map.set(e.event_date, []);
        map.get(e.event_date).push(e);
    });
    return map;
}

function calRender() {
    calShowNotice(calEmptyNotice(), 'info');
    if (calState.view === 'month') calRenderMonth();
    else calRenderList();
    calRefreshIcons();
}

/** 分頁沒有東西時，說明為什麼（而不是只顯示空白） */
function calEmptyNotice() {
    if (calState.tab === 'HOLDING' && !calState.counts.holding) {
        return '還沒有庫存股。到「投資組合」新增後，這裡會出現它們的財報與重要事件。';
    }
    if (calState.tab === 'WATCHLIST' && !calState.counts.watchlist) {
        return '觀察清單是空的。到「投資組合」加入想買的股票，這裡會提醒它們的重要時間點。';
    }
    if (calState.tab === 'HOLDING' || calState.tab === 'WATCHLIST') {
        return `目前只追蹤巨頭（${CAL_BIG_TECH_TEXT}）的財報日；其他個股的財報、法說會與除權息即將支援。`;
    }
    return '';
}

function calMonthPrefix() {
    const { y, m } = calState.month;
    return `${y}-${String(m).padStart(2, '0')}`;
}

function calRenderMonth() {
    const grid = calEl('cal-grid');
    grid.replaceChildren();
    const { y, m } = calState.month;
    const { start, end } = calGridRange(y, m);
    const today = calTodayTaipei();
    const byDay = calByDay();
    const prefix = calMonthPrefix();

    if (!calState.selected || !calState.selected.startsWith(prefix)) {
        // 預設選「今天或之後第一個有事件的日子」；本月都沒有就選今天（或 1 號）
        const upcoming = [...byDay.keys()].filter((d) => d.startsWith(prefix) && d >= today).sort()[0];
        calState.selected = upcoming || (today.startsWith(prefix) ? today : `${prefix}-01`);
    }

    for (let day = start; day <= end; day = calAddDays(day, 1)) {
        const events = byDay.get(day) || [];
        const cell = calNode('button', 'cal-cell');
        cell.type = 'button';
        cell.dataset.date = day;
        if (!day.startsWith(prefix)) cell.classList.add('outside');
        if (day === today) cell.classList.add('today');
        if (day < today) cell.classList.add('past');
        if (day === calState.selected) cell.classList.add('selected');
        if (events.some((e) => e.importance >= 3)) cell.classList.add('has-high');
        cell.setAttribute('aria-label', `${calDayLabel(day)}${events.length ? `，${events.length} 個事件` : '，沒有事件'}`);
        cell.setAttribute('aria-pressed', day === calState.selected ? 'true' : 'false');

        const head = calNode('span', 'cal-cell-head');
        head.append(calNode('span', 'cal-cell-num', calParse(day).d));
        if (day === today) head.append(calNode('span', 'cal-cell-today', '今天'));
        cell.append(head);

        const chips = calNode('span', 'cal-cell-events');
        const sorted = [...events].sort((a, b) => b.importance - a.importance);
        sorted.slice(0, 3).forEach((e) => {
            const chip = calNode('span', `cal-chip cat-${calCategory(e)} imp-${e.importance}`);
            chip.append(calNode('span', 'cal-chip-text', calShortTitle(e)));
            chips.append(chip);
        });
        if (events.length > 3) chips.append(calNode('span', 'cal-chip-more', `+${events.length - 3}`));
        const dots = calNode('span', 'cal-cell-dots');
        sorted.slice(0, 4).forEach((e) => dots.append(calNode('span', `cal-dot cat-${calCategory(e)}`)));
        cell.append(chips, dots);

        cell.addEventListener('click', () => {
            calState.selected = day;
            grid.querySelectorAll('.cal-cell').forEach((c) => {
                const on = c.dataset.date === day;
                c.classList.toggle('selected', on);
                c.setAttribute('aria-pressed', on ? 'true' : 'false');
            });
            calRenderAgenda(byDay);
            if (window.matchMedia('(max-width: 900px)').matches) {
                calEl('cal-agenda').scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
        grid.append(cell);
    }
    calRenderAgenda(byDay);
}

function calRenderAgenda(byDay) {
    const box = calEl('cal-agenda');
    box.replaceChildren();
    const day = calState.selected;
    const events = byDay.get(day) || [];
    const head = calNode('div', 'cal-agenda-head');
    head.append(calNode('h3', 'cal-agenda-title', calDayLabel(day)));
    if (day === calTodayTaipei()) head.append(calNode('span', 'cal-badge today', '今天'));
    box.append(head);
    if (!events.length) {
        box.append(calNode('p', 'cal-agenda-empty', '這天沒有追蹤中的事件。'));
        const next = [...byDay.keys()].filter((d) => d > day).sort()[0];
        if (next) {
            const btn = calNode('button', 'cal-text-btn', `下一個事件：${calDayLabel(next)}`);
            btn.type = 'button';
            btn.addEventListener('click', () => {
                calEl('cal-grid').querySelector(`.cal-cell[data-date="${next}"]`)?.click();
            });
            box.append(btn);
        }
        return;
    }
    events.forEach((e) => box.append(calCard(e)));
    calRefreshIcons();
}

function calRenderList() {
    const box = calEl('cal-list');
    box.replaceChildren();
    const prefix = calMonthPrefix();
    const today = calTodayTaipei();
    const byDay = calByDay();
    const days = [...byDay.keys()].filter((d) => d.startsWith(prefix)).sort();
    if (!days.length) {
        box.append(calNode('p', 'cal-list-empty', `${calState.month.m} 月沒有追蹤中的事件。`));
        return;
    }
    let anchor = null;
    days.forEach((day) => {
        const group = calNode('section', 'cal-day');
        if (day < today) group.classList.add('past');
        const head = calNode('h3', 'cal-day-head');
        head.append(calNode('span', 'cal-day-label', calDayLabel(day)));
        if (day === today) head.append(calNode('span', 'cal-badge today', '今天'));
        group.append(head);
        byDay.get(day).forEach((e) => group.append(calCard(e)));
        box.append(group);
        if (!anchor && day >= today) anchor = group;
    });
    if (anchor && prefix === today.slice(0, 7)) {
        requestAnimationFrame(() => anchor.scrollIntoView({ block: 'start' }));
    }
}

function calCard(e) {
    const card = calNode('article', `cal-card cat-${calCategory(e)}`);
    if (e.status === 'CANCELLED') card.classList.add('cancelled');

    const time = calNode('div', 'cal-card-time');
    time.append(calNode('span', 'cal-card-clock', calTimeText(e)));
    card.append(time);

    const body = calNode('div', 'cal-card-body');
    const titleRow = calNode('div', 'cal-card-title-row');
    titleRow.append(calNode('h4', 'cal-card-title', e.title));
    const imp = calNode('span', `cal-imp imp-${e.importance}`);
    imp.setAttribute('aria-label', `重要度：${CAL_IMPORTANCE[e.importance] || ''}`);
    imp.title = `重要度：${CAL_IMPORTANCE[e.importance] || ''}`;
    for (let i = 1; i <= 3; i += 1) imp.append(calNode('span', i <= e.importance ? 'on' : ''));
    titleRow.append(imp);
    body.append(titleRow);

    const badges = calNode('div', 'cal-card-badges');
    const st = CAL_STATUS[e.status];
    if (st) badges.append(calNode('span', `cal-badge ${st.cls}`, st.text));
    if (e.type === 'EARNINGS' && e.symbol) badges.append(calNode('span', 'cal-badge ticker', `${e.market} ${e.symbol}`));
    (e.tags || []).filter((t) => t !== 'MACRO').forEach((t) => {
        badges.append(calNode('span', 'cal-badge mine', t === 'HOLDING' ? '庫存' : '觀察'));
    });
    if (e.rescheduled && e.reschedule_from) {
        const from = e.reschedule_from.slice(5).replace('-', '/');
        badges.append(calNode('span', 'cal-badge moved', `已改期（原 ${from}）`));
    }
    const note = calTimeNote(e);
    if (note) badges.append(calNode('span', 'cal-card-note', note));
    body.append(badges);

    if (e.detail && e.type !== 'EARNINGS') body.append(calNode('p', 'cal-card-detail', e.detail));
    if (e.readings && e.readings.length) body.append(calReadings(e.readings));
    if (e.eps && (e.eps.estimate || e.eps.actual)) body.append(calEps(e.eps, e.detail));

    const foot = calNode('div', 'cal-card-foot');
    const ask = calNode('button', 'cal-ask');
    ask.type = 'button';
    ask.append(calIcon('sparkles'), calNode('span', null, '問 AI'));
    ask.addEventListener('click', () => calAskAI(e));
    foot.append(ask);
    if (e.source_url) {
        const link = calNode('a', 'cal-source', '來源');
        link.href = e.source_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        foot.append(link);
    }
    body.append(foot);
    card.append(body);
    return card;
}

function calReadings(readings) {
    const table = calNode('table', 'cal-readings');
    const head = calNode('tr');
    ['項目', '預期', '前值', '實際'].forEach((h) => head.append(calNode('th', null, h)));
    const thead = calNode('thead');
    thead.append(head);
    const tbody = calNode('tbody');
    readings.slice(0, 4).forEach((r) => {
        const row = calNode('tr');
        row.append(calNode('td', 'label', calReadingLabel(r.label)));
        row.append(calNode('td', null, r.forecast || '—'));
        row.append(calNode('td', null, r.previous || '—'));
        const actual = calNode('td', 'actual', r.actual || '—');
        const a = calNumber(r.actual);
        const f = calNumber(r.forecast);
        if (a !== null && f !== null && a !== f) {
            actual.classList.add(a > f ? 'cal-up' : 'cal-down');
            actual.textContent = `${r.actual} ${a > f ? '▲' : '▼'}`;
            actual.title = a > f ? '高於預期' : '低於預期';
        }
        row.append(actual);
        tbody.append(row);
    });
    table.append(thead, tbody);
    return table;
}

function calEps(eps, detail) {
    const box = calNode('dl', 'cal-eps');
    const add = (k, v, cls) => {
        const wrap = calNode('div', cls);
        wrap.append(calNode('dt', null, k), calNode('dd', null, v));
        box.append(wrap);
    };
    add('EPS 預估', eps.estimate || '—');
    if (eps.actual) {
        const a = calNumber(eps.actual);
        const f = calNumber(eps.estimate);
        add('EPS 實際', eps.actual, a !== null && f !== null && a !== f ? (a > f ? 'cal-up' : 'cal-down') : '');
    }
    if (detail) add('期別', detail.replace('財季截至 ', ''));
    return box;
}

/** 打開懸浮 AI 助理並帶入事件內容（不自動送出，讓使用者可以改問題） */
function calAskAI(e) {
    if (!window.AI_ASSISTANT) return;
    const when = `${calDayLabel(e.event_date, true)} ${calTimeText(e)}`;
    const values = (e.readings || [])
        .map((r) => {
            const parts = [['預期', r.forecast], ['前值', r.previous], ['實際', r.actual]]
                .filter(([, v]) => v).map(([k, v]) => `${k} ${v}`);
            return parts.length ? `${calReadingLabel(r.label)}：${parts.join('、')}` : '';
        })
        .filter(Boolean)
        .join('；');
    const eps = e.eps && e.eps.estimate ? `EPS 預估 ${e.eps.estimate}${e.eps.actual ? `、實際 ${e.eps.actual}` : ''}` : '';
    const facts = [values, eps].filter(Boolean).join('；');
    const question = e.status === 'RELEASED'
        ? `「${e.title}」（${when}）已公布${facts ? `，${facts}` : ''}。這個結果代表什麼？對台股、美股和我的持股可能有什麼影響？`
        : `「${e.title}」將在 ${when} 公布${facts ? `（${facts}）` : ''}。市場在關注什麼？我該留意哪些情境與對持股的影響？`;
    window.AI_ASSISTANT.open();
    const input = document.querySelector('.ai-assistant-window [data-role="input"]');
    if (input) {
        input.value = question;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
    }
}

// ── 初始化 ─────────────────────────────────────────────────────────

function calShiftMonth(delta) {
    let { y, m } = calState.month;
    m += delta;
    if (m < 1) { m = 12; y -= 1; }
    if (m > 12) { m = 1; y += 1; }
    calState.month = { y, m };
    calState.selected = null;
    calLoad();
}

document.addEventListener('DOMContentLoaded', () => {
    calEl('calendar-btn')?.addEventListener('click', () => {
        showCalendarView();
        if (typeof closeSidebarDrawer === 'function') closeSidebarDrawer();
    });
    document.querySelectorAll('.cal-tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            if (calState.tab === btn.dataset.tab) return;
            calState.tab = btn.dataset.tab;
            calSavePrefs();
            calLoad();
        });
    });
    document.querySelectorAll('.cal-seg button').forEach((btn) => {
        btn.addEventListener('click', () => {
            if (calState.view === btn.dataset.view) return;
            calState.view = btn.dataset.view;
            calSavePrefs();
            calSyncControls();
            calRender();
        });
    });
    calEl('cal-importance')?.addEventListener('change', (ev) => {
        calState.importance = Number(ev.target.value) || 1;
        calSavePrefs();
        calLoad();
    });
    calEl('cal-prev')?.addEventListener('click', () => calShiftMonth(-1));
    calEl('cal-next')?.addEventListener('click', () => calShiftMonth(1));
    calEl('cal-today')?.addEventListener('click', () => {
        const today = calParse(calTodayTaipei());
        calState.month = { y: today.y, m: today.m };
        calState.selected = calTodayTaipei();
        calLoad();
    });
});
