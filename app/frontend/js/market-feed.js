/**
 * 新聞瀏覽／AI 分析瀏覽
 * =====================
 * 從原 stock_insight 專案移植：直接讀 MongoDB 的 news 與 AI_news_analysis。
 * 後端端點見 app/backend/api/market_feed.py（MongoDB 連線沿用 MONGO_URI）。
 *
 * 視圖切換與 index.js 的 showChatView / showExploreView、deep-research、notes 互斥，
 * 規則與既有模組一致：顯示自己時把別人藏起來，別人顯示時呼叫 hideFeedViews()。
 *
 * 所有資料一律以 textContent 寫入 DOM，不經過 innerHTML。
 */

const FEED_DEFAULT_START = '2020-01-01';

const feedConfig = {
    news: {
        viewId: 'news-feed-view',
        btnId: 'news-feed-btn',
        title: '新聞',
        endpoint: '/feed/news',
        render: renderNewsCard,
    },
    ai: {
        viewId: 'ai-feed-view',
        btnId: 'ai-feed-btn',
        title: 'AI 分析',
        endpoint: '/feed/ai-analysis',
        render: renderAiCard,
    },
};

/** 每個 feed 的執行期狀態 */
const feedState = {
    news: { page: 1, hasMore: true, loading: false, loaded: false, params: {}, seq: 0 },
    ai:   { page: 1, hasMore: true, loading: false, loaded: false, params: {}, seq: 0 },
};

const feedView = (kind) => document.getElementById(feedConfig[kind].viewId);
const feedPart = (kind, role) => feedView(kind)?.querySelector(`[data-role="${role}"]`);

// ============================================================
// 視圖切換
// ============================================================

function showFeedView(kind) {
    if (typeof maybeParkViewportForLeavingChat === 'function') {
        maybeParkViewportForLeavingChat(
            typeof state !== 'undefined' ? state.currentChatId : null
        );
    }
    if (typeof hideExploreView === 'function') hideExploreView();
    if (typeof hideDeepResearchView === 'function') hideDeepResearchView();
    if (typeof hideNotesView === 'function') hideNotesView();
    if (typeof exitNoteSelectMode === 'function') exitNoteSelectMode();
    hideFeedViews();
    if (typeof hidePortfolioView === 'function') hidePortfolioView();

    document.getElementById('chat-messages').style.display = 'none';
    document.getElementById('project-view').style.display = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.querySelector('.chat-input-area').style.display = 'none';

    feedView(kind).style.display = 'flex';
    document.getElementById(feedConfig[kind].btnId)?.classList.add('active');
    if (typeof setMainChatTitle === 'function') setMainChatTitle(feedConfig[kind].title);

    if (!feedState[kind].loaded) {
        feedState[kind].loaded = true;
        applyFeedFilters(kind);
    }
}

/** 隱藏兩個 feed 視圖（其他模組切換視圖時呼叫） */
function hideFeedViews() {
    Object.keys(feedConfig).forEach((kind) => {
        const view = feedView(kind);
        if (view) view.style.display = 'none';
        document.getElementById(feedConfig[kind].btnId)?.classList.remove('active');
    });
}

// ============================================================
// 篩選與載入
// ============================================================

/** yyyy-mm-dd（使用者本地時區）→ Unix 秒 */
function feedDateToUnix(value, endOfDay) {
    if (!value) return null;
    const d = new Date(`${value}T${endOfDay ? '23:59:59' : '00:00:00'}`);
    const t = Math.floor(d.getTime() / 1000);
    return Number.isFinite(t) ? t : null;
}

/** 讀表單 → 重設分頁 → 從第一頁重新載入 */
function applyFeedFilters(kind) {
    const form = feedPart(kind, 'filters');
    const st = feedState[kind];
    const prevIndustry = st.params.industry;

    const params = {
        keyword: form.elements.keyword.value.trim(),
        start_time: feedDateToUnix(form.elements.start.value || FEED_DEFAULT_START, false),
        end_time: form.elements.end.value
            ? feedDateToUnix(form.elements.end.value, true)
            : Math.floor(Date.now() / 1000),
    };
    if (kind === 'ai') {
        params.is_summary = form.elements.is_summary.value;
        if (prevIndustry) params.industry = prevIndustry;
    }

    st.params = params;
    st.page = 1;
    st.hasMore = true;
    st.loading = false;
    st.seq += 1;            // 讓還在路上的舊請求結果作廢
    feedPart(kind, 'list').textContent = '';
    feedPart(kind, 'scroll').scrollTop = 0;
    renderIndustryChip();
    loadFeedPage(kind);
}

async function loadFeedPage(kind) {
    const st = feedState[kind];
    if (st.loading || !st.hasMore) return;
    st.loading = true;
    const seq = st.seq;
    setFeedStatus(kind, '載入中…');

    const qs = new URLSearchParams({ page: String(st.page) });
    Object.entries(st.params).forEach(([k, v]) => {
        if (v !== null && v !== undefined && v !== '') qs.set(k, String(v));
    });

    try {
        const res = await authFetch(`${state.apiBase}${feedConfig[kind].endpoint}?${qs}`);
        if (seq !== st.seq) return;           // 篩選條件已變更，丟掉舊結果
        if (!res) { setFeedStatus(kind, ''); return; }
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
            throw new Error(detail);
        }
        const json = await res.json();
        if (seq !== st.seq) return;

        const list = feedPart(kind, 'list');
        const frag = document.createDocumentFragment();
        (json.data || []).forEach((item) => frag.appendChild(feedConfig[kind].render(item)));
        list.appendChild(frag);
        if (typeof lucide !== 'undefined') lucide.createIcons();

        st.hasMore = json.nextPage != null;
        if (st.hasMore) st.page = json.nextPage;

        if (!list.childElementCount) setFeedStatus(kind, '沒有符合條件的資料');
        else if (!st.hasMore) setFeedStatus(kind, '已經到底了');
        else setFeedStatus(kind, '');
    } catch (err) {
        if (seq !== st.seq) return;
        console.error(`[FEED] 載入 ${kind} 失敗：`, err);
        setFeedStatus(kind, `載入失敗：${err.message || err}`, true);
    } finally {
        if (seq === st.seq) {
            st.loading = false;
            // 第一頁內容不夠撐出捲軸時，sentinel 一直在視窗內，observer 不會再觸發
            maybeLoadMoreFeed(kind);
        }
    }
}

function maybeLoadMoreFeed(kind) {
    const st = feedState[kind];
    if (st.loading || !st.hasMore) return;
    const view = feedView(kind);
    if (!view || view.style.display === 'none') return;
    const scroll = feedPart(kind, 'scroll');
    const sentinel = feedPart(kind, 'sentinel');
    if (sentinel.getBoundingClientRect().top <= scroll.getBoundingClientRect().bottom + 200) {
        loadFeedPage(kind);
    }
}

function setFeedStatus(kind, text, isError = false) {
    const el = feedPart(kind, 'status');
    el.textContent = text;
    el.classList.toggle('error', isError);
}

// ============================================================
// 卡片
// ============================================================

function feedEl(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
}

function feedFormatTime(unixSeconds) {
    if (!unixSeconds) return '';
    return new Date(unixSeconds * 1000).toLocaleString('zh-TW', {
        timeZone: 'Asia/Taipei',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

/** 只放行 http(s) 連結，避免資料裡的 javascript: 之類被開啟 */
function feedSafeUrl(raw) {
    try {
        const u = new URL(String(raw || '').trim());
        return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : null;
    } catch (_) {
        return null;
    }
}

const NEWS_CATEGORY_LABEL = { headline: '頭條新聞' };
const NEWS_SOURCE_LABEL = { anue: '鉅亨網' };

function renderNewsCard(item) {
    const url = feedSafeUrl(item.url);
    const card = feedEl(url ? 'a' : 'article', 'feed-card news-card');
    if (url) {
        card.href = url;
        card.target = '_blank';
        card.rel = 'noopener noreferrer';
    }

    const meta = feedEl('div', 'feed-meta');
    const category = NEWS_CATEGORY_LABEL[item.category] || item.category;
    const source = NEWS_SOURCE_LABEL[item.source] || item.source;
    if (category) meta.appendChild(feedEl('span', 'feed-tag', category));
    if (source) meta.appendChild(feedEl('span', 'feed-source', source));
    meta.appendChild(feedEl('span', 'feed-time', feedFormatTime(item.publishAt)));

    card.appendChild(meta);
    card.appendChild(feedEl('h3', 'feed-card-title', item.title || '（無標題）'));
    if (item.content) card.appendChild(feedEl('p', 'feed-card-body', `${item.content}…`));
    return card;
}

/** 把多行文字加進節點：每行一個 <p>，空行略過 */
function feedAppendLines(parent, text, splitter) {
    String(text || '')
        .split(splitter)
        .map((s) => s.trim())
        .filter(Boolean)
        .forEach((line) => parent.appendChild(feedEl('p', null, line)));
}

function feedAiSection(title, bodyNode) {
    const sec = feedEl('section', 'ai-section');
    sec.appendChild(feedEl('h4', 'ai-section-title', title));
    sec.appendChild(bodyNode);
    return sec;
}

function renderAiCard(item) {
    const card = feedEl('article', 'feed-card ai-card');

    const meta = feedEl('div', 'feed-meta');
    meta.appendChild(feedEl('span', 'feed-tag', item.is_summary ? '統整分析' : '逐條分析'));
    meta.appendChild(feedEl('span', 'feed-time', feedFormatTime(item.publishAt)));
    card.appendChild(meta);

    if (item.article_title) card.appendChild(feedEl('h3', 'feed-card-title', item.article_title));

    if (item.summary) {
        const body = feedEl('div', 'ai-text');
        // 原專案以「。」斷句分段
        feedAppendLines(body, item.summary.replace(/。/g, '。\n'), '\n');
        card.appendChild(feedAiSection('洞察結果', body));
    }
    if (item.important_news) {
        const body = feedEl('div', 'ai-text');
        feedAppendLines(body, item.important_news, '\n');
        card.appendChild(feedAiSection('重點新聞', body));
    }
    if (item.sentiment) {
        card.appendChild(feedAiSection('情緒分析', feedEl('div', 'ai-text', item.sentiment)));
    }
    if (item.potential_stocks_and_industries) {
        const body = feedEl('div', 'ai-text');
        feedAppendLines(body, item.potential_stocks_and_industries, '\n');
        card.appendChild(feedAiSection('產業分析', body));
    }

    const stocks = Array.isArray(item.stock_list) ? item.stock_list : [];
    if (stocks.length) {
        const chips = feedEl('div', 'feed-chips');
        stocks.forEach((s) => {
            if (!Array.isArray(s)) return;
            const [market, code, name] = s;
            const chip = feedEl('button', 'feed-chip stock-chip');
            chip.type = 'button';
            chip.appendChild(feedEl('span', null, name || code || ''));
            if (code) chip.appendChild(feedEl('span', 'feed-chip-sub', `${String(market || '').toUpperCase()} ${code}`));
            chip.title = '在 Google 搜尋這檔股票';
            chip.addEventListener('click', () => {
                const q = [name, code].filter(Boolean).join(' ') + ' 股價';
                window.open(`https://www.google.com/search?q=${encodeURIComponent(q)}`, '_blank', 'noopener,noreferrer');
            });
            chips.appendChild(chip);
        });
        card.appendChild(feedAiSection('股票', chips));
    }

    const industries = Array.isArray(item.industry_list) ? item.industry_list : [];
    if (industries.length) {
        const chips = feedEl('div', 'feed-chips');
        industries.forEach((ind) => {
            if (!ind) return;
            const chip = feedEl('button', 'feed-chip industry-chip', ind);
            chip.type = 'button';
            chip.title = `只看「${ind}」相關分析`;
            chip.addEventListener('click', () => setIndustryFilter(ind));
            chips.appendChild(chip);
        });
        card.appendChild(feedAiSection('潛力產業', chips));
    }

    const sources = Array.isArray(item.source_news) ? item.source_news : [];
    if (sources.length) {
        const list = feedEl('ul', 'ai-source-list');
        sources.forEach((news) => {
            const li = feedEl('li');
            const btn = feedEl('button', 'ai-source-link', news.title || '（無標題）');
            btn.type = 'button';
            if (news._id) btn.addEventListener('click', () => openSourceNews(news._id, btn));
            else btn.disabled = true;
            li.appendChild(btn);
            list.appendChild(li);
        });
        card.appendChild(feedAiSection('相關新聞', list));
    }

    return card;
}

async function openSourceNews(newsId, btn) {
    // 先同步開視窗，避免 await 之後被瀏覽器當成彈出視窗擋掉
    const win = window.open('', '_blank');
    btn.disabled = true;
    try {
        const res = await authFetch(`${state.apiBase}/feed/news/${encodeURIComponent(newsId)}`);
        if (!res || !res.ok) throw new Error(res ? `HTTP ${res.status}` : 'no response');
        const json = await res.json();
        const url = feedSafeUrl(json.data && json.data.url);
        if (!url) throw new Error('no url');
        if (win) {
            win.opener = null;
            win.location.href = url;
        } else {
            window.open(url, '_blank', 'noopener,noreferrer');
        }
    } catch (err) {
        if (win) win.close();
        console.error('[FEED] 開啟原始新聞失敗：', err);
        if (typeof showToast === 'function') {
            showToast('無法開啟原始新聞，請用標題搜尋原文', 'error');
        }
    } finally {
        btn.disabled = false;
    }
}

// ============================================================
// 產業篩選（點 AI 分析卡片上的產業 chip）
// ============================================================

function setIndustryFilter(industry) {
    feedState.ai.params.industry = industry || undefined;
    applyFeedFilters('ai');
}

function renderIndustryChip() {
    const box = feedPart('ai', 'industry-chip');
    if (!box) return;
    const industry = feedState.ai.params.industry;
    box.textContent = '';
    box.classList.toggle('hidden', !industry);
    if (!industry) return;

    box.appendChild(feedEl('span', null, '產業：'));
    box.appendChild(feedEl('strong', null, industry));
    const clear = feedEl('button', 'feed-chip-clear');
    clear.type = 'button';
    clear.setAttribute('aria-label', '清除產業篩選');
    clear.textContent = '×';
    clear.addEventListener('click', () => setIndustryFilter(null));
    box.appendChild(clear);
}

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    Object.keys(feedConfig).forEach((kind) => {
        const btn = document.getElementById(feedConfig[kind].btnId);
        if (btn) {
            btn.addEventListener('click', () => {
                showFeedView(kind);
                if (typeof closeSidebarDrawer === 'function') closeSidebarDrawer();
            });
        }

        const form = feedPart(kind, 'filters');
        if (form) {
            form.addEventListener('submit', (e) => {
                e.preventDefault();
                applyFeedFilters(kind);
            });
        }

        const scroll = feedPart(kind, 'scroll');
        const sentinel = feedPart(kind, 'sentinel');
        if (scroll && sentinel && 'IntersectionObserver' in window) {
            new IntersectionObserver(
                (entries) => {
                    if (entries.some((e) => e.isIntersecting)) loadFeedPage(kind);
                },
                { root: scroll, rootMargin: '200px' }
            ).observe(sentinel);
        }
    });
});
