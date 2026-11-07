/**
 * 投資組合（Portfolio Insight）
 * =============================
 * 庫存／觀察清單維護、排程設定、分析報告。
 * 後端端點見 app/backend/api/portfolio.py；排程與 Zentra 呼叫都在後端，這裡只負責顯示。
 *
 * 視圖切換規則與其他模組一致：顯示自己時把別人藏起來，別人顯示時呼叫 hidePortfolioView()。
 * 所有來自 API 的字串一律以 textContent 寫入，不經過 innerHTML。
 */

const pfState = {
    tab: 'HOLDING',
    items: { HOLDING: [], WATCHLIST: [] },
    limit: 30,
    editingId: null,
    loaded: false,
    schedule: null,
    service: { zentra_configured: false, scheduler_running: false },
    jobs: [],
    jobId: null,
    pollTimer: null,
    chartUrls: [],
};

const pfEl = (id) => document.getElementById(id);

const PF_TOOL_LABEL = {
    'stock-news': '新聞',
    'stock-chart': 'K 線',
    'stock-technical': '技術面',
    'stock-fundamental': '財報',
};
const PF_STATUS_LABEL = {
    QUEUED: '排隊中', RUNNING: '分析中', SUCCEEDED: '完成', PARTIAL: '部分完成', FAILED: '失敗',
};
const PF_ACTION_LABEL = { ADD: '加碼', HOLD: '續抱', TRIM: '減碼', EXIT: '出清' };
const PF_FREQ_LABEL = { DAILY: '每天', WEEKDAYS: '每個交易日', WEEKLY: '每週', MONTHLY: '每月' };
const PF_WEEKDAY = ['', '週一', '週二', '週三', '週四', '週五', '週六', '週日'];

/** 建立元素並以 textContent 填字 */
function pfNode(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
}

function pfNum(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
    return Number(value).toLocaleString('zh-TW', { maximumFractionDigits: digits });
}

function pfTime(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString('zh-TW', {
        timeZone: 'Asia/Taipei', year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

/** 呼叫後端；失敗時拋出可直接顯示的訊息 */
async function pfApi(path, options = {}) {
    const res = await authFetch(`${state.apiBase}${path}`, options);
    if (!res) throw new Error('尚未登入');
    let body = {};
    try { body = await res.json(); } catch (_) { /* 非 JSON */ }
    if (!res.ok) {
        const detail = body.detail;
        const message = Array.isArray(detail)
            ? detail.map((d) => d.msg).join('；')
            : (detail || `請求失敗（HTTP ${res.status}）`);
        throw new Error(message);
    }
    return body.data;
}

// ============================================================
// 視圖切換
// ============================================================

function showPortfolioView() {
    if (typeof maybeParkViewportForLeavingChat === 'function') {
        maybeParkViewportForLeavingChat(typeof state !== 'undefined' ? state.currentChatId : null);
    }
    if (typeof hideExploreView === 'function') hideExploreView();
    if (typeof hideDeepResearchView === 'function') hideDeepResearchView();
    if (typeof hideNotesView === 'function') hideNotesView();
    if (typeof hideFeedViews === 'function') hideFeedViews();
    if (typeof exitNoteSelectMode === 'function') exitNoteSelectMode();

    pfEl('chat-messages').style.display = 'none';
    pfEl('project-view').style.display = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.querySelector('.chat-input-area').style.display = 'none';

    pfEl('portfolio-view').style.display = 'flex';
    pfEl('portfolio-btn')?.classList.add('active');
    if (typeof setMainChatTitle === 'function') setMainChatTitle('投資組合');

    if (!pfState.loaded) {
        pfState.loaded = true;
        loadPortfolioItems();
        loadInsightSchedule();
    }
    if (pfState.tab === 'report') loadInsightJobs();
}

function hidePortfolioView() {
    const view = pfEl('portfolio-view');
    if (view) view.style.display = 'none';
    pfEl('portfolio-btn')?.classList.remove('active');
    pfStopPolling();
}

function switchPortfolioTab(tab) {
    pfState.tab = tab;
    document.querySelectorAll('.pf-tab').forEach((btn) => {
        btn.classList.toggle('active', btn.dataset.tab === tab);
    });
    const isList = tab === 'HOLDING' || tab === 'WATCHLIST';
    pfEl('pf-list-panel').classList.toggle('hidden', !isList);
    pfEl('pf-schedule-panel').classList.toggle('hidden', tab !== 'schedule');
    pfEl('pf-report-panel').classList.toggle('hidden', tab !== 'report');
    if (isList) {
        cancelPortfolioEdit();
        renderPortfolioItems();
    }
    if (tab === 'report') loadInsightJobs();
    else pfStopPolling();
}

// ============================================================
// 清單
// ============================================================

async function loadPortfolioItems() {
    try {
        const data = await pfApi('/portfolio/items');
        pfState.items = { HOLDING: data.holdings, WATCHLIST: data.watchlist };
        pfState.limit = data.limit_per_list;
        renderPortfolioItems();
        loadPortfolioSummary();
    } catch (err) {
        showToast(`載入清單失敗：${err.message}`, 'error');
    }
}

function renderPortfolioItems() {
    const listType = pfState.tab === 'WATCHLIST' ? 'WATCHLIST' : 'HOLDING';
    const isHolding = listType === 'HOLDING';
    const items = pfState.items[listType] || [];
    pfEl('pf-overview').classList.toggle('hidden', !isHolding || items.length === 0);
    const form = pfEl('pf-item-form');
    form.querySelectorAll('[data-holding-only]').forEach((el) => el.classList.toggle('hidden', !isHolding));
    form.cost.required = isHolding;
    form.shares.required = isHolding;

    pfEl('pf-list-hint').textContent = isHolding
        ? `已買進的股票（${items.length}/${pfState.limit}）。排程會分析每一檔，並給出整體配置調整建議。`
        : `還沒買、想追蹤的股票（${items.length}/${pfState.limit}）。排程會分析每一檔，但不納入整體配置建議。`;

    const head = pfEl('pf-item-head');
    head.replaceChildren();
    const headRow = pfNode('tr');
    const cols = isHolding ? ['市場', '代碼', '名稱', '成本價', '股數', ''] : ['市場', '代碼', '名稱', ''];
    cols.forEach((c) => headRow.appendChild(pfNode('th', null, c)));
    head.appendChild(headRow);

    const body = pfEl('pf-item-body');
    body.replaceChildren();
    items.forEach((item) => {
        const row = pfNode('tr');
        row.appendChild(pfNode('td', null, item.market === 'TW' ? '台股' : '美股'));
        row.appendChild(pfNode('td', 'pf-mono', item.symbol));
        row.appendChild(pfNode('td', null, item.name));
        if (isHolding) {
            row.appendChild(pfNode('td', 'pf-num', pfNum(item.cost, 4)));
            row.appendChild(pfNode('td', 'pf-num', pfNum(item.shares, 4)));
        }
        const actions = pfNode('td', 'pf-row-actions');
        const edit = pfNode('button', 'pf-text-btn', '編輯');
        edit.type = 'button';
        edit.addEventListener('click', () => editPortfolioItem(item));
        const move = pfNode('button', 'pf-text-btn', isHolding ? '移到觀察' : '已買進');
        move.type = 'button';
        move.addEventListener('click', () => movePortfolioItem(item));
        const del = pfNode('button', 'pf-text-btn danger', '刪除');
        del.type = 'button';
        del.addEventListener('click', () => deletePortfolioItem(item));
        actions.append(edit, move, del);
        row.appendChild(actions);
        body.appendChild(row);
    });

    const empty = pfEl('pf-list-empty');
    empty.textContent = isHolding ? '還沒有庫存。用上面的表單加入。' : '觀察清單是空的。';
    empty.classList.toggle('hidden', items.length > 0);
}

function editPortfolioItem(item, asListType) {
    const targetList = asListType || item.list_type;
    if (pfState.tab !== targetList) switchPortfolioTab(targetList);   // 切 tab 會清掉編輯狀態，所以先切
    const form = pfEl('pf-item-form');
    pfState.editingId = item.id;
    pfState.editingListType = targetList;
    form.market.value = item.market;
    form.symbol.value = item.symbol;
    form.market.disabled = true;
    form.symbol.disabled = true;                 // 市場與代碼不能改，要改就刪掉重加
    form.name.value = item.name;
    form.cost.value = item.cost ?? '';
    form.shares.value = item.shares ?? '';
    pfEl('pf-item-submit').textContent = targetList !== item.list_type ? '移到庫存' : '儲存';
    pfEl('pf-item-cancel').classList.remove('hidden');
    (targetList === 'HOLDING' && item.cost === null ? form.cost : form.name).focus();
}

function cancelPortfolioEdit() {
    const form = pfEl('pf-item-form');
    pfState.editingId = null;
    pfState.editingListType = null;
    form.reset();
    form.market.disabled = false;
    form.symbol.disabled = false;
    pfEl('pf-item-submit').textContent = '加入';
    pfEl('pf-item-cancel').classList.add('hidden');
}

async function submitPortfolioItem(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const listType = pfState.editingListType || (pfState.tab === 'WATCHLIST' ? 'WATCHLIST' : 'HOLDING');
    const num = (v) => (v === '' ? null : Number(v));
    const payload = {
        name: form.name.value.trim(),
        cost: listType === 'HOLDING' ? num(form.cost.value) : null,
        shares: listType === 'HOLDING' ? num(form.shares.value) : null,
    };
    try {
        if (pfState.editingId) {
            await pfApi(`/portfolio/items/${pfState.editingId}`, {
                method: 'PATCH', body: JSON.stringify({ ...payload, list_type: listType }),
            });
            showToast('已更新', 'success');
        } else {
            await pfApi('/portfolio/items', {
                method: 'POST',
                body: JSON.stringify({
                    ...payload, list_type: listType,
                    market: form.market.value, symbol: form.symbol.value.trim(),
                }),
            });
            showToast('已加入', 'success');
        }
        cancelPortfolioEdit();
        await loadPortfolioItems();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function movePortfolioItem(item) {
    if (item.list_type === 'WATCHLIST') {
        // 移到庫存需要成本與股數：打開編輯表單讓使用者填
        editPortfolioItem(item, 'HOLDING');
        showToast('填上成本價與股數後按「移到庫存」', 'info');
        return;
    }
    try {
        await pfApi(`/portfolio/items/${item.id}`, {
            method: 'PATCH', body: JSON.stringify({ list_type: 'WATCHLIST' }),
        });
        showToast(`${item.name} 已移到觀察清單`, 'success');
        await loadPortfolioItems();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function deletePortfolioItem(item) {
    if (!window.confirm(`確定要刪除 ${item.name}（${item.symbol}）？`)) return;
    try {
        await pfApi(`/portfolio/items/${item.id}`, { method: 'DELETE' });
        showToast('已刪除', 'success');
        await loadPortfolioItems();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// ============================================================
// 庫存總覽（甜甜圈圖）
// ============================================================
//
// 顏色跟著「那一檔股票」走，不跟著排名走：依加入順序（created_at）分配
// 類別色 1–8，市值變動、排序改變都不會換色。超過 8 檔時，市值最小的併成「其他」（灰），
// 留下來的保留自己的色號；加入順序在第 9 檔以後的，補上被併掉那幾檔空出來的色號。
// 配色已用 dataviz 的 validate_palette.js 對深／淺底色驗證（CVD ΔE ≥ 8.4）。
// 淺色模式有三色對比 < 3:1，所以圖旁一定有名稱＋比例＋金額的清單（不只靠顏色辨識）。

const PF_SLOTS = 8;
const PF_MAX_SEGMENTS = 8;          // 含「其他」

function pfSignPct(value) {
    if (value === null || value === undefined) return '';
    const sign = value > 0 ? '+' : '';
    return `${sign}${pfNum(value)}%`;
}

/** 台股慣例：漲紅跌綠；同時帶正負號，不只靠顏色 */
function pfTrendClass(value) {
    if (!value) return 'pf-flat';
    return value > 0 ? 'pf-up' : 'pf-down';
}

async function loadPortfolioSummary() {
    if (!(pfState.items.HOLDING || []).length) return;
    pfEl('pf-ov-asof').textContent = '更新報價中…';
    try {
        const data = await pfApi('/portfolio/summary');
        renderPortfolioSummary(data);
    } catch (err) {
        pfEl('pf-ov-asof').textContent = `報價載入失敗：${err.message}`;
    }
}

function pfSegments(holdings) {
    const priced = holdings.filter((h) => h.value_twd !== null && h.value_twd > 0);
    // holdings 已依加入順序排好；先決定誰留下
    let kept = priced;
    let other = [];
    if (priced.length > PF_MAX_SEGMENTS) {
        const bySize = [...priced].sort((a, b) => b.value_twd - a.value_twd);
        const keepIds = new Set(bySize.slice(0, PF_MAX_SEGMENTS - 1).map((h) => h.id));
        kept = priced.filter((h) => keepIds.has(h.id));
        other = priced.filter((h) => !keepIds.has(h.id));
    }
    // 色號：加入順序 < 8 的用自己的序號；其餘依加入順序補空位
    const orderIndex = new Map(holdings.map((h, i) => [h.id, i]));
    const used = new Set();
    const slotOf = new Map();
    kept.forEach((h) => {
        const idx = orderIndex.get(h.id);
        if (idx < PF_SLOTS) { slotOf.set(h.id, idx + 1); used.add(idx + 1); }
    });
    kept.forEach((h) => {
        if (slotOf.has(h.id)) return;
        for (let slot = 1; slot <= PF_SLOTS; slot += 1) {
            if (!used.has(slot)) { slotOf.set(h.id, slot); used.add(slot); break; }
        }
    });
    const segments = kept.map((h) => ({
        key: h.id, label: h.name, sub: h.symbol, value: h.value_twd, weight: h.weight_pct,
        color: `var(--pf-s${slotOf.get(h.id)})`,
    }));
    if (other.length) {
        const value = other.reduce((sum, h) => sum + h.value_twd, 0);
        const weight = other.reduce((sum, h) => sum + (h.weight_pct || 0), 0);
        segments.push({ key: 'other', label: `其他 ${other.length} 檔`, sub: '', value, weight,
                        color: 'var(--pf-other)' });
    }
    return segments;
}

function renderPortfolioSummary(data) {
    pfEl('pf-ov-value').textContent = pfNum(data.total_value_twd, 0);
    const day = pfEl('pf-ov-day');
    day.textContent = data.day_change_pct === null ? '' : `(${pfSignPct(data.day_change_pct)})`;
    day.className = `pf-ov-day ${pfTrendClass(data.day_change_pct)}`;
    pfEl('pf-ov-cost').textContent = pfNum(data.total_cost_twd, 0);
    const pnl = pfEl('pf-ov-pnl');
    const pnlSign = data.unrealized_pnl_twd > 0 ? '+' : '';
    pnl.textContent = `${pnlSign}${pfNum(data.unrealized_pnl_twd, 0)}（${pfSignPct(data.unrealized_pnl_pct)}）`;
    pnl.className = `pf-ov-num ${pfTrendClass(data.unrealized_pnl_twd)}`;
    const time = new Date(data.as_of).toLocaleTimeString('zh-TW', { timeZone: 'Asia/Taipei', hour: '2-digit', minute: '2-digit', hour12: false });
    pfEl('pf-ov-asof').textContent = data.usd_twd
        ? `即時報價 ${time}・USD/TWD ${pfNum(data.usd_twd, 3)}`
        : `即時報價 ${time}`;

    // 台股／美股：細的分段條，文字在下方清單（不在條上）
    const split = pfEl('pf-ov-split');
    const splitLegend = pfEl('pf-ov-split-legend');
    split.replaceChildren();
    splitLegend.replaceChildren();
    const parts = [['TW', '台股', 'var(--pf-mkt-tw)'], ['US', '美股', 'var(--pf-mkt-us)']]
        .filter(([m]) => data.market_split_pct[m]);
    split.setAttribute('aria-label', parts.map(([m, label]) => `${label} ${pfNum(data.market_split_pct[m])}%`).join('，'));
    parts.forEach(([m, label, color]) => {
        const seg = pfNode('span', 'pf-split-seg');
        seg.style.flexGrow = String(data.market_split_pct[m]);
        seg.style.background = color;
        split.appendChild(seg);
        const item = pfNode('span', 'pf-split-item');
        const sw = pfNode('i', 'pf-swatch');
        sw.style.background = color;
        item.append(sw, pfNode('span', null, label), pfNode('strong', null, `${pfNum(data.market_split_pct[m])}%`));
        splitLegend.appendChild(item);
    });

    // 甜甜圈
    pfEl('pf-donut-total').textContent = pfNum(data.total_value_twd, 0);
    const donutDay = pfEl('pf-donut-day');
    donutDay.textContent = data.day_change_pct === null ? '' : `今日 ${pfSignPct(data.day_change_pct)}`;
    donutDay.className = `pf-donut-day ${pfTrendClass(data.day_change_pct)}`;
    const segments = pfSegments(data.holdings);
    renderPortfolioDonut(segments);

    const legend = pfEl('pf-donut-legend');
    legend.replaceChildren();
    [...segments].sort((a, b) => b.value - a.value).forEach((seg) => {
        const li = pfNode('li', 'pf-legend-row');
        li.dataset.key = seg.key;
        const sw = pfNode('i', 'pf-swatch');
        sw.style.background = seg.color;
        const name = pfNode('span', 'pf-legend-name', seg.label);
        if (seg.sub) name.appendChild(pfNode('span', 'pf-legend-sub', seg.sub));
        li.append(sw, pfNode('span', 'pf-legend-pct', `${pfNum(seg.weight, 1)}%`), name,
                  pfNode('span', 'pf-legend-val', pfNum(seg.value, 0)));
        li.addEventListener('mouseenter', () => pfHighlight(seg.key));
        li.addEventListener('mouseleave', () => pfHighlight(null));
        legend.appendChild(li);
    });

    const note = pfEl('pf-ov-note');
    note.textContent = data.unpriced.length
        ? `抓不到報價、未計入：${data.unpriced.join('、')}`
        : '';
}

function renderPortfolioDonut(segments) {
    const svg = pfEl('pf-donut');
    const ns = 'http://www.w3.org/2000/svg';
    svg.replaceChildren();
    const r = 78;
    const width = 26;
    const circumference = 2 * Math.PI * r;
    const total = segments.reduce((sum, s) => sum + s.value, 0);
    const gap = segments.length > 1 ? 2 : 0;          // 區塊之間 2px 底色間隙
    svg.setAttribute('aria-label', segments.map((s) => `${s.label} ${pfNum(s.weight, 1)}%`).join('，'));

    let offset = 0;
    segments.forEach((seg) => {
        const length = total ? (seg.value / total) * circumference : 0;
        const circle = document.createElementNS(ns, 'circle');
        circle.setAttribute('cx', '100');
        circle.setAttribute('cy', '100');
        circle.setAttribute('r', String(r));
        circle.setAttribute('fill', 'none');
        circle.setAttribute('class', 'pf-donut-seg');
        circle.setAttribute('stroke-width', String(width));
        circle.style.stroke = seg.color;
        circle.setAttribute('stroke-dasharray', `${Math.max(length - gap, 0.5)} ${circumference}`);
        circle.setAttribute('stroke-dashoffset', String(-offset));
        circle.setAttribute('transform', 'rotate(-90 100 100)');
        circle.dataset.key = seg.key;
        circle.addEventListener('mouseenter', (e) => { pfHighlight(seg.key); pfShowTip(seg, e); });
        circle.addEventListener('mousemove', (e) => pfShowTip(seg, e));
        circle.addEventListener('mouseleave', () => { pfHighlight(null); pfEl('pf-donut-tip').classList.add('hidden'); });
        svg.appendChild(circle);
        offset += length;
    });
}

function pfHighlight(key) {
    document.querySelectorAll('#pf-donut .pf-donut-seg').forEach((el) => {
        el.classList.toggle('dim', key !== null && el.dataset.key !== key);
    });
    document.querySelectorAll('#pf-donut-legend .pf-legend-row').forEach((el) => {
        el.classList.toggle('active', el.dataset.key === key);
    });
}

function pfShowTip(seg, event) {
    const tip = pfEl('pf-donut-tip');
    const wrap = tip.parentElement.getBoundingClientRect();
    tip.replaceChildren(
        pfNode('strong', null, seg.sub ? `${seg.label} ${seg.sub}` : seg.label),
        pfNode('span', null, `${pfNum(seg.weight, 1)}%　NT$ ${pfNum(seg.value, 0)}`),
    );
    tip.style.left = `${event.clientX - wrap.left + 12}px`;
    tip.style.top = `${event.clientY - wrap.top + 12}px`;
    tip.classList.remove('hidden');
}

// ============================================================
// 排程
// ============================================================

async function loadInsightSchedule() {
    try {
        const data = await pfApi('/insights/schedule');
        pfState.schedule = data.schedule;
        pfState.service = { zentra_configured: data.zentra_configured, scheduler_running: data.scheduler_running };
        renderInsightSchedule();
    } catch (err) {
        showToast(`載入排程失敗：${err.message}`, 'error');
    }
}

function syncScheduleFields() {
    const form = pfEl('pf-schedule-form');
    const freq = form.frequency.value;
    form.querySelectorAll('[data-freq]').forEach((el) => {
        el.classList.toggle('hidden', el.dataset.freq !== freq);
    });
}

function renderInsightSchedule() {
    const form = pfEl('pf-schedule-form');
    const s = pfState.schedule;
    if (s) {
        form.enabled.checked = s.enabled;
        form.frequency.value = s.frequency;
        form.run_time.value = s.run_time;
        if (s.weekday) form.weekday.value = String(s.weekday);
        if (s.month_day) form.month_day.value = String(s.month_day);
        form.risk_tolerance.value = s.risk_tolerance || '';
        form.cash_twd.value = s.cash_twd ?? '';
    }
    syncScheduleFields();

    const svc = pfState.service;
    const stateText = !svc.zentra_configured
        ? '⚠️ 分析服務（Zentra）尚未設定，排程與立即分析都無法使用。'
        : !svc.scheduler_running
            ? '⚠️ 排程器目前沒有在執行。'
            : '';
    pfEl('pf-service-state').textContent = stateText;
    pfEl('pf-service-state').classList.toggle('hidden', !stateText);
    pfEl('pf-run-now').disabled = !(svc.zentra_configured && svc.scheduler_running);

    const next = pfEl('pf-next-run');
    if (!s) {
        next.textContent = '尚未設定排程。';
    } else if (!s.enabled || !s.next_run_at) {
        next.textContent = '排程已停用。';
    } else {
        const when = s.frequency === 'WEEKLY' ? PF_WEEKDAY[s.weekday]
            : s.frequency === 'MONTHLY' ? `${s.month_day} 號` : '';
        next.textContent = `${PF_FREQ_LABEL[s.frequency]}${when} ${s.run_time}（台北）執行。下一次：${pfTime(s.next_run_at)}`;
    }
}

async function saveInsightSchedule(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
        enabled: form.enabled.checked,
        frequency: form.frequency.value,
        run_time: form.run_time.value,
        weekday: form.frequency.value === 'WEEKLY' ? Number(form.weekday.value) : null,
        month_day: form.frequency.value === 'MONTHLY' ? Number(form.month_day.value) : null,
        risk_tolerance: form.risk_tolerance.value || null,
        cash_twd: form.cash_twd.value === '' ? null : Number(form.cash_twd.value),
    };
    try {
        const data = await pfApi('/insights/schedule', { method: 'PUT', body: JSON.stringify(payload) });
        pfState.schedule = data.schedule;
        pfState.service = { zentra_configured: data.zentra_configured, scheduler_running: data.scheduler_running };
        renderInsightSchedule();
        showToast('排程已儲存', 'success');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

async function runInsightNow() {
    try {
        const job = await pfApi('/insights/jobs', { method: 'POST' });
        showToast('已開始分析，完成約需 1–3 分鐘', 'success');
        pfState.jobId = job.id;
        switchPortfolioTab('report');
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// ============================================================
// 報告
// ============================================================

function pfStopPolling() {
    if (pfState.pollTimer) clearTimeout(pfState.pollTimer);
    pfState.pollTimer = null;
}

function pfJobLabel(job) {
    return `${pfTime(job.scheduled_for)}　${job.trigger === 'MANUAL' ? '手動' : '排程'}　${PF_STATUS_LABEL[job.status] || job.status}`;
}

async function loadInsightJobs() {
    try {
        const data = await pfApi('/insights/jobs?limit=30');
        pfState.jobs = data.jobs;
    } catch (err) {
        showToast(`載入分析紀錄失敗：${err.message}`, 'error');
        return;
    }
    const select = pfEl('pf-job-select');
    select.replaceChildren();
    pfState.jobs.forEach((job) => {
        const option = pfNode('option', null, pfJobLabel(job));
        option.value = job.id;
        select.appendChild(option);
    });
    const empty = pfState.jobs.length === 0;
    pfEl('pf-report-empty').classList.toggle('hidden', !empty);
    if (empty) {
        pfEl('pf-report').replaceChildren();
        pfEl('pf-job-status').textContent = '';
        pfEl('pf-job-meta').textContent = '';
        return;
    }
    if (!pfState.jobId || !pfState.jobs.some((j) => j.id === pfState.jobId)) pfState.jobId = pfState.jobs[0].id;
    select.value = pfState.jobId;
    loadInsightJob(pfState.jobId);
}

async function loadInsightJob(jobId) {
    pfStopPolling();
    pfState.jobId = jobId;
    let job;
    try {
        job = await pfApi(`/insights/jobs/${jobId}`);
    } catch (err) {
        showToast(err.message, 'error');
        return;
    }
    if (pfState.jobId !== jobId) return;         // 使用者已經切到別的紀錄
    renderInsightJob(job);

    // 下拉選單裡的狀態文字跟著更新
    const listed = pfState.jobs.find((j) => j.id === jobId);
    if (listed && listed.status !== job.status) {
        listed.status = job.status;
        const option = [...pfEl('pf-job-select').options].find((o) => o.value === jobId);
        if (option) option.textContent = pfJobLabel(listed);
    }
    if (job.status === 'QUEUED' || job.status === 'RUNNING') {
        pfState.pollTimer = setTimeout(() => {
            if (pfState.tab === 'report' && pfState.jobId === jobId) loadInsightJob(jobId);
        }, 5000);
    }
}

function renderInsightJob(job) {
    pfState.chartUrls.forEach((url) => URL.revokeObjectURL(url));
    pfState.chartUrls = [];

    const badge = pfEl('pf-job-status');
    badge.textContent = PF_STATUS_LABEL[job.status] || job.status;
    badge.dataset.status = job.status;
    const counts = job.counts || {};
    const meta = [`開始 ${pfTime(job.started_at)}`];
    if (job.finished_at) meta.push(`結束 ${pfTime(job.finished_at)}`);
    if (counts.total !== undefined) meta.push(`個股分析 ${counts.succeeded}/${counts.total} 成功`);
    pfEl('pf-job-meta').textContent = meta.join('　·　');

    const root = pfEl('pf-report');
    root.replaceChildren();
    if (job.error) root.appendChild(pfNode('div', 'pf-alert', job.error));

    root.appendChild(renderPortfolioAdvice(job));

    // 依股票分組
    const groups = new Map();
    (job.results || []).forEach((r) => {
        const key = `${r.list_type}|${r.market}|${r.symbol}`;
        if (!groups.has(key)) groups.set(key, { ...r, tools: {} });
        groups.get(key).tools[r.tool] = r;
    });
    ['HOLDING', 'WATCHLIST'].forEach((listType) => {
        const stocks = [...groups.values()].filter((g) => g.list_type === listType);
        if (!stocks.length) return;
        root.appendChild(pfNode('h3', 'pf-section-title', listType === 'HOLDING' ? '庫存個股' : '觀察清單'));
        stocks.forEach((g) => root.appendChild(renderStockCard(g)));
    });
    if (job.status === 'QUEUED' || job.status === 'RUNNING') {
        root.appendChild(pfNode('p', 'pf-hint', '分析進行中，頁面會自動更新…'));
    }
}

function renderPortfolioAdvice(job) {
    const card = pfNode('section', 'pf-card pf-advice');
    card.appendChild(pfNode('h3', 'pf-card-title', '整體庫存調整建議'));
    if (job.portfolio_status === 'SKIPPED') {
        card.appendChild(pfNode('p', 'pf-hint', '庫存是空的，這次沒有整體建議（觀察清單不納入）。'));
        return card;
    }
    if (job.portfolio_status === 'FAILED') {
        card.appendChild(pfNode('div', 'pf-alert', job.portfolio_error || '整體建議產生失敗'));
        return card;
    }
    const out = job.portfolio_output;
    if (!out) {
        card.appendChild(pfNode('p', 'pf-hint', job.status === 'RUNNING' ? '個股分析完成後產生…' : '沒有資料'));
        return card;
    }
    const pf = out.data?.portfolio || {};
    const stats = pfNode('div', 'pf-stats');
    [
        ['總市值（台幣）', pfNum(pf.total_market_value_twd, 0)],
        ['未實現損益', `${pfNum(pf.unrealized_pnl_twd, 0)}（${pfNum(pf.unrealized_pnl_pct)}%）`],
        ['台股／美股', `${pfNum(pf.market_split_pct?.TW)}% / ${pfNum(pf.market_split_pct?.US)}%`],
        ['USD/TWD', pfNum(pf.usd_twd, 3)],
    ].forEach(([label, value]) => {
        const stat = pfNode('div', 'pf-stat');
        stat.append(pfNode('span', 'pf-stat-label', label), pfNode('span', 'pf-stat-value', value));
        stats.appendChild(stat);
    });
    card.appendChild(stats);

    const a = out.analysis;
    if (!a) {
        card.appendChild(pfNode('div', 'pf-alert', (out.warnings || []).join('；') || '分析師 LLM 沒有回傳建議'));
        return card;
    }
    const risk = pfNode('p', 'pf-line');
    risk.append(pfNode('strong', null, '風險等級：'), pfNode('span', `pf-risk pf-risk-${a.risk_level}`, a.risk_level));
    card.appendChild(risk);
    card.appendChild(pfNode('p', 'pf-summary', a.summary));
    if (a.overall_assessment) card.appendChild(pfNode('p', 'pf-text', a.overall_assessment));
    if (a.diversification) card.appendChild(pfNode('p', 'pf-text', `分散度：${a.diversification}`));

    if (Array.isArray(a.allocation) && a.allocation.length) {
        const wrap = pfNode('div', 'pf-table-wrap');
        const table = pfNode('table', 'pf-table pf-alloc');
        const head = pfNode('tr');
        ['標的', '動作', '目前權重', '建議權重', '理由'].forEach((h) => head.appendChild(pfNode('th', null, h)));
        const thead = pfNode('thead');
        thead.appendChild(head);
        const tbody = pfNode('tbody');
        a.allocation.forEach((row) => {
            const tr = pfNode('tr');
            tr.appendChild(pfNode('td', null, row.symbol === 'CASH' ? '現金' : `${row.name || ''} ${row.symbol}`));
            tr.appendChild(pfNode('td', `pf-action pf-action-${row.action}`, PF_ACTION_LABEL[row.action] || row.action));
            tr.appendChild(pfNode('td', 'pf-num', `${pfNum(row.current_weight_pct)}%`));
            tr.appendChild(pfNode('td', 'pf-num', `${pfNum(row.suggested_weight_pct)}%`));
            tr.appendChild(pfNode('td', 'pf-reason', row.reason));
            tbody.appendChild(tr);
        });
        table.append(thead, tbody);
        wrap.appendChild(table);
        card.appendChild(wrap);
    }
    pfAppendList(card, '調整步驟', a.rebalancing_steps, 'ol');
    pfAppendList(card, '接下來要盯的', a.watch_points);
    card.appendChild(pfNode('p', 'pf-disclaimer', out.disclaimer));
    return card;
}

function pfAppendList(parent, title, items, tag = 'ul') {
    if (!Array.isArray(items) || !items.length) return;
    parent.appendChild(pfNode('h4', 'pf-sub', title));
    const list = pfNode(tag, 'pf-list');
    items.forEach((item) => list.appendChild(pfNode('li', null, item)));
    parent.appendChild(list);
}

function renderStockCard(group) {
    const card = pfNode('section', 'pf-card pf-stock');
    const head = pfNode('div', 'pf-stock-head');
    head.appendChild(pfNode('h4', 'pf-card-title', `${group.name}　${group.symbol}`));
    head.appendChild(pfNode('span', 'pf-tag', group.market === 'TW' ? '台股' : '美股'));
    const tech = group.tools['stock-technical']?.output;
    const position = tech?.data?.position;
    if (position) {
        head.appendChild(pfNode('span', 'pf-position',
            `收盤 ${pfNum(position.last_close)}　損益 ${pfNum(position.unrealized_pnl)}（${pfNum(position.unrealized_pnl_pct)}%）`));
    } else if (tech?.data?.indicators) {
        head.appendChild(pfNode('span', 'pf-position', `收盤 ${pfNum(tech.data.indicators.last_close)}`));
    }
    card.appendChild(head);

    const grid = pfNode('div', 'pf-stock-grid');
    const chart = group.tools['stock-chart'];
    const chartBox = pfNode('div', 'pf-chart');
    if (chart?.has_chart) {
        const img = pfNode('img');
        img.alt = `${group.symbol} K 線圖`;
        chartBox.appendChild(img);
        pfLoadChart(chart.id, img);
    } else {
        chartBox.appendChild(pfNode('p', 'pf-hint', chart?.status === 'FAILED' ? 'K 線圖產生失敗' : '沒有 K 線圖'));
    }
    const chartAnalysis = chart?.output?.analysis;
    if (chartAnalysis?.summary) chartBox.appendChild(pfNode('p', 'pf-text', chartAnalysis.summary));
    grid.appendChild(chartBox);

    const side = pfNode('div', 'pf-stock-side');
    side.appendChild(renderToolBlock(group.tools['stock-technical'], (o) => {
        const a = o.analysis;
        return a ? [`${a.trend || ''}　建議 ${a.signal || '—'}（信心 ${a.confidence ?? '—'}）`, a.summary, a.position_advice]
            : (o.data?.signals || []).map((s) => `${s.name}：${s.detail}`);
    }));
    side.appendChild(renderToolBlock(group.tools['stock-fundamental'], (o) => {
        const a = o.analysis;
        const v = o.data?.valuation || {};
        const val = `本益比 ${pfNum(v.pe)}　股價淨值比 ${pfNum(v.pb)}`;
        return a ? [`評等 ${a.rating}　${val}`, a.summary] : [val];
    }));
    side.appendChild(renderToolBlock(group.tools['stock-news'], (o) => {
        const a = o.analysis;
        const lines = a ? [`消息面 ${a.sentiment}`, a.summary, ...(a.catalysts || []).map((c) => `◆ ${c}`)]
            : (o.data?.company_news?.last_1m || []).slice(0, 5).map((n) => `${n.date} ${n.title}`);
        return lines;
    }));
    grid.appendChild(side);
    card.appendChild(grid);
    return card;
}

function renderToolBlock(result, lines) {
    const block = pfNode('div', 'pf-tool');
    if (!result) return block;
    block.appendChild(pfNode('h5', 'pf-tool-title', PF_TOOL_LABEL[result.tool] || result.tool));
    if (result.status === 'FAILED') {
        block.appendChild(pfNode('p', 'pf-alert', result.error || '失敗'));
        return block;
    }
    lines(result.output || {}).filter(Boolean).forEach((line) => block.appendChild(pfNode('p', 'pf-text', line)));
    (result.output?.warnings || []).forEach((w) => block.appendChild(pfNode('p', 'pf-warn', w)));
    return block;
}

/** 圖片端點需要帶 Authorization，所以用 authFetch 取 blob 再給 <img> */
async function pfLoadChart(resultId, img) {
    try {
        const res = await authFetch(`${state.apiBase}/insights/results/${resultId}/chart`);
        if (!res || !res.ok) throw new Error();
        const url = URL.createObjectURL(await res.blob());
        pfState.chartUrls.push(url);
        img.src = url;
    } catch (_) {
        img.replaceWith(pfNode('p', 'pf-hint', 'K 線圖已超過保留期'));
    }
}

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    pfEl('portfolio-btn')?.addEventListener('click', () => {
        showPortfolioView();
        if (typeof closeSidebarDrawer === 'function') closeSidebarDrawer();
    });
    document.querySelectorAll('.pf-tab').forEach((btn) => {
        btn.addEventListener('click', () => switchPortfolioTab(btn.dataset.tab));
    });
    pfEl('pf-item-form')?.addEventListener('submit', submitPortfolioItem);
    pfEl('pf-item-cancel')?.addEventListener('click', cancelPortfolioEdit);
    pfEl('pf-schedule-form')?.addEventListener('submit', saveInsightSchedule);
    pfEl('pf-schedule-form')?.frequency.addEventListener('change', syncScheduleFields);
    pfEl('pf-run-now')?.addEventListener('click', runInsightNow);
    pfEl('pf-job-select')?.addEventListener('change', (e) => loadInsightJob(e.target.value));
    pfEl('pf-report-refresh')?.addEventListener('click', loadInsightJobs);
});
