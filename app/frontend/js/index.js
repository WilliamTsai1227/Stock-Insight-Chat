// --- 多對話 SSE：同一頁可有不同 chat_id 並行串流 ---
// streamingChatIds   : Set<string chatId> — 已發出 POST /chat/messages（await fetch 中）或 reader 仍在 read() 的對話，供離開視圖時 park 避免 loadHistory 拆掉 DOM
// parkedPaneByChatId : Map<chatId, HTMLElement> — 離開對話視景時將 #chat-messages 整段暫存在隱藏區，回來再接回
const streamingChatIds = new Set();
const parkedPaneByChatId = new Map();
// 每個並行 SSE 對應的 AbortController — parked 超標剔除或刪除專案 CASCADE 時可中止 fetch
const streamAbortByChatId = new Map();

/** parked 區最多保留幾段（FIFO：超過則剔除最舊封存並 abort 其串流） */
const MAX_PARKED_STAGING_CHATS = 8;

let newChatComposeLock = false;

/** localStorage key：對話模式（`general` / `stock_agent`） */
const CHAT_MODE_STORAGE_KEY = 'sicChatMode';
/** localStorage：`thinking`（LangGraph）／`flash`（單輪新聞向量檢索） */
const RESPONSE_MODE_STORAGE_KEY = 'sicChatResponseMode';
/** localStorage：界面主題 `dark`（預設）／`light` */
const THEME_STORAGE_KEY = 'insightUiTheme';
const HLJS_THEME_DARK = 'https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/atom-one-dark.min.css';
const HLJS_THEME_LIGHT = 'https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github.min.css';

/** 與後端 project.py _NAME_MAX_LEN 一致 */
const PROJECT_NAME_MAX_CHARS = 40;
/** 與後端 chat.py _TITLE_MAX_LEN 一致 */
const CHAT_TITLE_MAX_CHARS = 50;
/** 與後端 chat.py QUERY_MAX_CHARS（預設 2000）一致 */
const CHAT_QUERY_MAX_CHARS = 2000;

function applyUiTheme(theme) {
    const isLight = theme === 'light';
    document.body.classList.add('theme-switching');
    document.body.classList.toggle('dark-theme', !isLight);
    document.body.classList.toggle('light-theme', isLight);

    const hljsLink = document.getElementById('hljs-theme');
    if (hljsLink) {
        hljsLink.href = isLight ? HLJS_THEME_LIGHT : HLJS_THEME_DARK;
    }

    try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (_) {}

    requestAnimationFrame(() => {
        document.body.classList.remove('theme-switching');
    });
}

function initUiTheme() {
    let theme = 'dark';
    try {
        const saved = localStorage.getItem(THEME_STORAGE_KEY);
        if (saved === 'light' || saved === 'dark') theme = saved;
    } catch (_) {}
    applyUiTheme(theme);
}

function initThemeToggle() {
    const btn = document.getElementById('theme-toggle-btn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const next = document.body.classList.contains('light-theme') ? 'dark' : 'light';
        applyUiTheme(next);
    });
}
/** 回覆模式：收合為短名，下拉為主標題 + 副行說明 */
const RESPONSE_MODE_META = {
    thinking: {
        short: '思考',
        subtitle: '搜尋執行時間較長，回覆較完整',
    },
    flash: {
        short: '快捷',
        subtitle: '搜尋執行時間較短，回覆較簡潔',
    },
};
/** @type {'general' | 'stock_agent'} */
let chatMode = 'general';
/** @type {'thinking' | 'flash'} */
let chatResponseMode = 'thinking';

function getStreamStagingRoot() {
    let el = document.getElementById('stream-staging');
    if (!el) {
        el = document.createElement('div');
        el.id = 'stream-staging';
        el.className = 'stream-staging';
        el.setAttribute('aria-hidden', 'true');
        document.body.appendChild(el);
    }
    return el;
}

/** 強制釋放某段 parked：拆 DOM、中止尚未結束的 SSE（若有）。 */
function evictParkedPane(chatId) {
    if (!chatId) return;
    const abort = streamAbortByChatId.get(chatId);
    if (abort) {
        try { abort.abort(); } catch (_) { /* noop */ }
        streamAbortByChatId.delete(chatId);
    }
    streamingChatIds.delete(chatId);

    const wrapper = parkedPaneByChatId.get(chatId);
    if (wrapper && wrapper.parentNode) {
        wrapper.parentNode.removeChild(wrapper);
    }
    parkedPaneByChatId.delete(chatId);
}

/**
 * Map 依插入順序保存 parked；最舊佔用者為 keys().next()。
 * 超過上限時反覆剔除最舊，直到 ≤ MAX_PARKED_STAGING_CHATS。
 */
function enforceParkedStagingLimit() {
    while (parkedPaneByChatId.size > MAX_PARKED_STAGING_CHATS) {
        const oldest = parkedPaneByChatId.keys().next().value;
        if (oldest === undefined) break;
        evictParkedPane(oldest);
    }
    updateSendButtonForStreamingState();
}

/** 離開對話視景時將目前聊天區封存到 staging（稍後再接回 DOM，SSE 對仍掛載的節點仍可更新） */
function parkViewportFor(chatId) {
    if (parkedPaneByChatId.has(chatId)) return;
    const container = document.getElementById('chat-messages');
    if (!container || !container.firstChild) return;

    const root = getStreamStagingRoot();
    const wrapper = document.createElement('div');
    wrapper.className = 'parked-chat-viewport';
    wrapper.dataset.parkedChatId = chatId;
    while (container.firstChild) {
        wrapper.appendChild(container.firstChild);
    }
    root.appendChild(wrapper);
    parkedPaneByChatId.set(chatId, wrapper);
    enforceParkedStagingLimit();
}

/** 回到某個對話：若封存中有 UI（串流進行中或離開後剛結束未完成 unpark），接回 #chat-messages */
function unparkViewportFor(chatId) {
    const wrapper = parkedPaneByChatId.get(chatId);
    if (!wrapper) return;
    const container = document.getElementById('chat-messages');
    while (container.firstChild) container.removeChild(container.firstChild);
    while (wrapper.firstChild) {
        container.appendChild(wrapper.firstChild);
    }
    wrapper.remove();
    parkedPaneByChatId.delete(chatId);
}

/** 離開對話（切到其他 chat / 進專案 / 開新對話）時若此 chat 仍有進行中串流（含 fetch 尚未回應），封存畫面以防 loadHistory 拆掉 DOM */
function maybeParkViewportForLeavingChat(chatId) {
    if (!chatId) return;
    if (!streamingChatIds.has(chatId)) return;
    parkViewportFor(chatId);
}

// --- RWD：平板／手機側欄抽屜（與 CSS `(max-width: 1024px)` 對齊）---
const mqSidebarDrawer = typeof window.matchMedia !== 'undefined'
    ? window.matchMedia('(max-width: 1024px)')
    : { matches: false, addEventListener: null, addListener: null };

function sidebarDrawerActive() {
    return mqSidebarDrawer.matches;
}

function setSidebarToggleExpanded(open) {
    const btn = document.getElementById('sidebar-toggle-btn');
    if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function closeSidebarDrawer() {
    const app = document.querySelector('.app-container');
    const bd = document.getElementById('sidebar-backdrop');
    if (!app || !app.classList.contains('sidebar-open')) return;
    app.classList.remove('sidebar-open');
    if (bd) bd.setAttribute('aria-hidden', 'true');
    setSidebarToggleExpanded(false);
}

function toggleSidebarDrawer() {
    if (!sidebarDrawerActive()) return;
    const app = document.querySelector('.app-container');
    const bd = document.getElementById('sidebar-backdrop');
    if (!app) return;
    const open = app.classList.toggle('sidebar-open');
    if (bd) bd.setAttribute('aria-hidden', open ? 'false' : 'true');
    setSidebarToggleExpanded(open);
}

function initMobileSidebar() {
    const app = document.querySelector('.app-container');
    const btn = document.getElementById('sidebar-toggle-btn');
    const bd = document.getElementById('sidebar-backdrop');
    if (!app || !btn || !bd) return;

    btn.addEventListener('click', () => toggleSidebarDrawer());
    bd.addEventListener('click', () => closeSidebarDrawer());

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeSidebarDrawer();
    });

    const onBreak = () => {
        if (!sidebarDrawerActive()) closeSidebarDrawer();
    };
    if (typeof mqSidebarDrawer.addEventListener === 'function') {
        mqSidebarDrawer.addEventListener('change', onBreak);
    } else if (typeof mqSidebarDrawer.addListener === 'function') {
        mqSidebarDrawer.addListener(onBreak);
    }
}

const SIDEBAR_WIDTH_MIN = 220;
const SIDEBAR_WIDTH_MAX = 420;
const SIDEBAR_WIDTH_DEFAULT = 260;
const SIDEBAR_WIDTH_STORAGE_KEY = 'insight-sidebar-width';

function readSidebarWidthPx() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue('--sidebar-width').trim();
    const n = parseInt(raw, 10);
    return Number.isFinite(n) ? n : SIDEBAR_WIDTH_DEFAULT;
}

function applySidebarWidth(px) {
    const clamped = Math.round(Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, px)));
    document.documentElement.style.setProperty('--sidebar-width', `${clamped}px`);
    return clamped;
}

function initSidebarResize() {
    const handle = document.getElementById('sidebar-resize-handle');
    if (!handle) return;

    try {
        const saved = parseInt(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY), 10);
        if (Number.isFinite(saved)) applySidebarWidth(saved);
    } catch (_) { /* noop */ }

    let dragging = false;
    let startX = 0;
    let startWidth = SIDEBAR_WIDTH_DEFAULT;

    const finishResize = () => {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove('sidebar-resizing');
        try {
            localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(readSidebarWidthPx()));
        } catch (_) { /* noop */ }
    };

    handle.addEventListener('pointerdown', (e) => {
        if (sidebarDrawerActive()) return;
        if (e.button !== 0) return;
        dragging = true;
        startX = e.clientX;
        startWidth = readSidebarWidthPx();
        document.body.classList.add('sidebar-resizing');
        handle.setPointerCapture(e.pointerId);
        e.preventDefault();
    });

    handle.addEventListener('pointermove', (e) => {
        if (!dragging) return;
        applySidebarWidth(startWidth + (e.clientX - startX));
    });

    handle.addEventListener('pointerup', finishResize);
    handle.addEventListener('pointercancel', finishResize);
}

/**
 * 點側欄進入某一則對話（含封存還原 + GET /api/chat 載入）。
 * 若在別的對話離開後「完成」並封存在 staging，unpark 即顯示，不再打 API。
 *
 * @param {string} chatId
 * @param {{ projectId?: string|null }} [options]
 *   - projectId 有傳入：同步 sidebar 專案高亮（從專案內 chat 進入時保留）
 *   - 未傳入：清除專案選取（從「最近」等非專案入口進入）
 */
async function navigateToChat(chatId, options = {}) {
    closeSidebarDrawer();
    const prevId = state.currentChatId;
    if (prevId === chatId) return;

    if (prevId && prevId !== chatId) {
        maybeParkViewportForLeavingChat(prevId);
    }

    if (Object.prototype.hasOwnProperty.call(options, 'projectId')) {
        state.currentProjectId = options.projectId;
    } else {
        state.currentProjectId = null;
    }

    state.currentChatId = chatId;
    renderProjects();
    renderRecentChats();
    lucide.createIcons();
    showChatView();
    setMainChatTitle(resolveChatTitleForId(chatId));

    if (parkedPaneByChatId.has(chatId)) {
        unparkViewportFor(chatId);
        setChatStatusLoading(false);
        scrollToBottom();
        lucide.createIcons();
        updateSendButtonForStreamingState();
        return;
    }

    setChatStatusLoading(true);
    showChatMessagesLoading();
    await loadChatHistoryIntoView(chatId);
    updateSendButtonForStreamingState();
}

/** 一般聊天視圖（非專案頁）輸入框 placeholder */
const CHAT_INPUT_PLACEHOLDER = {
    general: '問我任何事…',
    stock_agent: '問問台積電的供應商風險…',
};

/** 歡迎畫面依模式顯示的建議問題（點擊即送出） */
const SUGGESTED_PROMPTS = {
    general: [
        '幫我找找台北聚餐好去處',
        '幫我找找大安區咖啡廳',
        '松山火車站附近有什麼上班族中午可以吃的',
        '幫我整理這週科技產業的重要新聞',
        '最近有哪些演唱會？',
    ],
    stock_agent: [
        '台積電近期財報有哪些重點？',
        '半導體產業目前的供應鏈風險有哪些？',
        '幫我搜尋聯發科最近一週的重要新聞',
        '分析台股加權指數近期的走勢與市場情緒',
    ],
};

function getMainChatInputPlaceholder() {
    return CHAT_INPUT_PLACEHOLDER[chatMode] || CHAT_INPUT_PLACEHOLDER.general;
}

function isProjectViewVisible() {
    const pv = document.getElementById('project-view');
    if (!pv) return false;
    return getComputedStyle(pv).display !== 'none';
}

function getPvComposePlaceholder() {
    const p = state.projects.find(x => x.id === state.currentProjectId);
    const name = (p && p.name) ? p.name : '此專案';
    return `在 ${name} 的新聊天`;
}

/** 非串流鎖定時，依目前畫面（專案頁 / 聊天頁）套用對應 placeholder */
function applyIdleInputPlaceholder() {
    const inputEl = document.getElementById('user-input');
    if (!inputEl) return;
    inputEl.placeholder = isProjectViewVisible()
        ? getPvComposePlaceholder()
        : getMainChatInputPlaceholder();
}

function setMainChatTitle(text) {
    const el = document.getElementById('current-chat-title');
    if (el) el.textContent = text || '歡迎回來';
}

function resolveChatTitleForId(chatId) {
    const sid = String(chatId);
    const r = (state.recentChats || []).find(c => String(c.id) === sid);
    if (r && r.title) return r.title;
    for (const pid of Object.keys(state.chats || {})) {
        const found = (state.chats[pid] || []).find(c => String(c.id) === sid);
        if (found && found.title) return found.title;
    }
    return '對話';
}

/** 建立三點跳動載入指示（側欄 / 主區共用） */
function createLoadingDots(className) {
    const dots = document.createElement('span');
    dots.className = className;
    dots.setAttribute('aria-hidden', 'true');
    for (let i = 0; i < 3; i += 1) {
        dots.appendChild(document.createElement('span'));
    }
    return dots;
}

function setChatStatusLoading(isLoading) {
    const badge = document.getElementById('chat-status');
    if (!badge) return;
    if (isLoading) {
        badge.textContent = '載入中';
        badge.classList.add('is-loading');
    } else {
        badge.textContent = 'Ready';
        badge.classList.remove('is-loading');
    }
}

/** 側欄列表載入占位（專案區保留「新增專案」） */
function renderProjectsLoading() {
    const list = document.getElementById('project-list');
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);

    const newLi = document.createElement('li');
    newLi.className = 'new-project-item';
    const newIcon = document.createElement('i');
    newIcon.setAttribute('data-lucide', 'folder-plus');
    const newText = document.createElement('span');
    newText.textContent = '新增專案';
    newLi.appendChild(newIcon);
    newLi.appendChild(newText);
    newLi.addEventListener('click', openCreateProjectModal);
    list.appendChild(newLi);

    const loadingLi = document.createElement('li');
    loadingLi.className = 'sidebar-loading-item';
    loadingLi.setAttribute('aria-busy', 'true');
    const label = document.createElement('span');
    label.className = 'sidebar-loading-label';
    label.textContent = '載入專案';
    loadingLi.appendChild(label);
    loadingLi.appendChild(createLoadingDots('sidebar-loading-dots'));
    list.appendChild(loadingLi);
    lucide.createIcons();
}

function renderRecentChatsLoading() {
    const list = document.getElementById('recent-chat-list');
    if (!list) return;
    while (list.firstChild) list.removeChild(list.firstChild);

    const li = document.createElement('li');
    li.className = 'sidebar-loading-item';
    li.setAttribute('aria-busy', 'true');
    const label = document.createElement('span');
    label.className = 'sidebar-loading-label';
    label.textContent = '載入紀錄';
    li.appendChild(label);
    li.appendChild(createLoadingDots('sidebar-loading-dots'));
    list.appendChild(li);
}

/** 主聊天區：清空舊訊息並顯示「載入對話紀錄」+ 跳動點 */
function showChatMessagesLoading() {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);

    const wrap = document.createElement('div');
    wrap.className = 'chat-loading-state';
    wrap.setAttribute('role', 'status');
    wrap.setAttribute('aria-live', 'polite');
    wrap.setAttribute('aria-label', '載入對話紀錄');

    const text = document.createElement('span');
    text.className = 'chat-loading-text';
    text.textContent = '載入對話紀錄';

    wrap.appendChild(text);
    wrap.appendChild(createLoadingDots('chat-loading-dots'));
    container.appendChild(wrap);
}

function showPvListsLoading() {
    const chatList = document.getElementById('pv-chat-list');
    const fileList = document.getElementById('pv-file-list');
    const chatEmpty = document.getElementById('pv-chats-empty');
    const filesEmpty = document.getElementById('pv-files-empty');
    if (chatEmpty) chatEmpty.style.display = 'none';
    if (filesEmpty) filesEmpty.style.display = 'none';
    if (chatList) while (chatList.firstChild) chatList.removeChild(chatList.firstChild);
    if (fileList) while (fileList.firstChild) fileList.removeChild(fileList.firstChild);

    if (!chatList) return;
    const li = document.createElement('li');
    li.className = 'pv-loading-item';
    li.setAttribute('aria-busy', 'true');
    const label = document.createElement('span');
    label.className = 'pv-loading-label';
    label.textContent = '載入中';
    li.appendChild(label);
    li.appendChild(createLoadingDots('sidebar-loading-dots'));
    chatList.appendChild(li);
}

/** 發送／輸入欄鎖：串流中、空白、或超過字數上限時不可送出 */
function updateSendButtonForStreamingState() {
    const sendBtn = document.getElementById('send-btn');
    const inputEl = document.getElementById('user-input');
    if (!sendBtn || !inputEl) return;

    const cur = state.currentChatId;
    const busy = !!(cur && streamingChatIds.has(cur));
    const tooLong = inputEl.value.length > CHAT_QUERY_MAX_CHARS;
    const empty = inputEl.value.trim().length === 0;

    sendBtn.disabled = busy || tooLong || empty;
    inputEl.disabled = busy;
    inputEl.placeholder = busy ? '等待回覆中...' : '';
    if (!busy) applyIdleInputPlaceholder();
}

// --- 全域狀態 ---
// 注意：所有資料皆從後端載入，不放任何假資料
//   projects     : { id, name, created_at, updated_at }[]              ← /api/project/all 載入
//   chats        : { [projectId]: { id, title }[] }               ← /api/project?project_id=... 載入
//   files        : { [projectId]: File[] }                        ← 同上
//   recentChats  : { id, title, created_at, updated_at }[]（未在 project 內；GET /api/chat/all）
//   pendingDeleteProject : 暫存「刪除確認 modal」要刪除的專案物件
//   pendingDeleteChat    : 暫存「刪除聊天 modal」要刪除的 chat 物件
//   pendingEditChat      : 暫存「重新命名 modal」要編輯的 chat 物件
let state = {
    projects: [],
    chats: {},
    files: {},
    recentChats: [],
    currentProjectId: null,
    currentChatId: null,
    pendingDeleteProject: null,
    pendingDeleteChat: null,
    pendingEditChat: null,
    apiBase: resolveStockInsightApiBase(),
};

// --- Marked.js 設定 ---
marked.setOptions({
    gfm: true,
    breaks: true,
    smartLists: true,
});

// 讓 Markdown 正文中的所有連結都在新分頁開啟，避免跳轉當前頁面
// marked.js v5+ 的 renderer 改用 token 物件傳入
marked.use({
    renderer: {
        link(token) {
            const href  = token.href  || '';
            const title = token.title || '';
            const text  = token.text  || href;
            const titleAttr = title ? ` title="${title}"` : '';
            return `<a href="${href}"${titleAttr} target="_blank" rel="noopener noreferrer">${text}</a>`;
        },
        // 程式碼區塊：語法高亮 + 語言標籤 + 複製按鈕
        code(token) {
            const lang    = (token.lang || '').trim();
            const rawCode = token.text || '';
            let highlighted;
            if (lang && window.hljs && hljs.getLanguage(lang)) {
                highlighted = hljs.highlight(rawCode, { language: lang }).value;
            } else if (window.hljs) {
                highlighted = hljs.highlightAuto(rawCode).value;
            } else {
                // hljs 未載入時 fallback 純文字
                highlighted = rawCode
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;');
            }
            const langLabel = lang
                ? `<span class="code-lang-label">${lang}</span>`
                : '';
            const copyBtn =
                `<button class="code-copy-btn" data-action="copy-code" title="複製">
                    <i data-lucide="copy" size="14"></i>
                 </button>`;
            return (
                `<div class="code-block-wrapper">` +
                    `<div class="code-block-header">${langLabel}${copyBtn}</div>` +
                    `<pre><code class="hljs ${lang ? `language-${lang}` : ''}">${highlighted}</code></pre>` +
                `</div>`
            );
        }
    }
});

/** 複製程式碼區塊內容（事件委派，DOMPurify 安全） */
document.addEventListener('click', function (e) {
    const btn = e.target.closest('[data-action="copy-code"]');
    if (!btn) return;
    const code = btn.closest('.code-block-wrapper')?.querySelector('code');
    if (!code) return;
    navigator.clipboard.writeText(code.innerText).then(() => {
        const icon = btn.querySelector('i');
        if (icon) {
            icon.setAttribute('data-lucide', 'check');
            lucide.createIcons();
        }
        setTimeout(() => {
            if (icon) {
                icon.setAttribute('data-lucide', 'copy');
                lucide.createIcons();
            }
        }, 1500);
    });
});

/**
 * 進階 Markdown 渲染
 */
function renderMarkdown(raw) {
    // 前處理：偵測裸 JSON 區塊（LLM 未加 code fence 時的 fallback）
    // 規則：段落以 { 或 [ 開頭，內含多行，且結尾為 } 或 ]，視為 JSON 自動包 fence
    const preprocessed = (raw || '').replace(
        /(^|\n)([ \t]*[{\[][^`][\s\S]*?[}\]][ \t]*)(?=\n|$)/gm,
        (match, prefix, block) => {
            // 若已在 code fence 內則跳過
            if (/^\s*```/.test(block)) return match;
            // 若 block 看起來像 JSON（至少有一個 key: value 或 array item）
            const trimmed = block.trim();
            try {
                JSON.parse(trimmed);
                return `${prefix}\`\`\`json\n${trimmed}\n\`\`\``;
            } catch {
                return match;
            }
        }
    );
    let html = marked.parse(preprocessed);
    html = html.replace(
        /<strong>(\d{4,5})<\/strong>/g,
        '<strong class="stock-ticker">$1</strong>'
    );
    return html;
}

/**
 * bubble 元素寫入 Markdown 並補初始化 lucide icon（程式碼區塊複製按鈕用）。
 * 使用 DOMPurify 消毒 HTML，防止 XSS 攻擊。
 * Markdown 渲染需插入 HTML 結構（語法高亮、連結、表格等），
 * 因此使用 DOMPurify.sanitize() 消毒後再寫入，禁止直接寫入未消毒內容。
 */
function applyMarkdown(el, raw) {
    const html = renderMarkdown(raw);
    // FORCE_BODY 確保片段 HTML 不被包進 <body> 包裝；
    // ADD_ATTR 允許 target / rel（link renderer）、data-action（複製按鈕事件委派用）
    const clean = window.DOMPurify
        ? DOMPurify.sanitize(html, {
            ADD_ATTR: ['target', 'rel', 'data-action'],
            FORCE_BODY: true,
        })
        : html;
    el.innerHTML = clean;
    if (window.lucide) lucide.createIcons({ el });
}

function applyResponseModeLabel() {
    const label = document.getElementById('response-mode-label');
    if (!label) return;
    const m = RESPONSE_MODE_META[chatResponseMode] || RESPONSE_MODE_META.flash;
    label.textContent = m.short;
    const btn = document.getElementById('response-mode-btn');
    if (btn) {
        btn.setAttribute('title', `${m.short}（${m.subtitle}）`);
    }
}

/** 下拉選項：第一行標題 + 第二行輔助說明（單一資料來源） */
function renderResponseModeMenuOptions() {
    const menu = document.getElementById('response-mode-menu');
    if (!menu) return;

    menu.textContent = '';
    /** @type {('thinking' | 'flash')[]} */
    const order = ['thinking', 'flash'];
    order.forEach((key) => {
        const m = RESPONSE_MODE_META[key];
        const li = document.createElement('li');
        li.setAttribute('tabindex', '0');
        li.setAttribute('role', 'option');
        li.dataset.value = key;
        li.className = 'response-mode-option';

        const title = document.createElement('span');
        title.className = 'response-mode-option-title';
        title.textContent = m.short;

        const desc = document.createElement('span');
        desc.className = 'response-mode-option-desc';
        desc.textContent = `（${m.subtitle}）`;

        li.appendChild(title);
        li.appendChild(desc);
        menu.appendChild(li);
    });
}

function syncResponseModeMenuSelection() {
    const menu = document.getElementById('response-mode-menu');
    if (!menu) return;
    menu.querySelectorAll('.response-mode-option').forEach((li) => {
        const v = li.getAttribute('data-value');
        li.classList.toggle('selected', v === chatResponseMode);
    });
}

// ── Chat Mode（一般對話 / 股市 Agent）──────────────────────────────────────

const CHAT_MODE_META = {
    general:     { label: '一般對話' },
    stock_agent: { label: '股市 Agent' },
};

function applyChatModeLabel() {
    const label = document.getElementById('chat-mode-label');
    if (!label) return;
    const m = CHAT_MODE_META[chatMode] || CHAT_MODE_META.stock_agent;
    label.textContent = m.label;
}

function syncChatModeMenuSelection() {
    const menu = document.getElementById('chat-mode-menu');
    if (!menu) return;
    menu.querySelectorAll('.chat-mode-option').forEach((li) => {
        li.classList.toggle('selected', li.getAttribute('data-value') === chatMode);
    });
}

/** 套用 chatMode：同步 response-mode 反灰、工具鎖定、placeholder */
function applyChatModeUI() {
    const isGeneral = chatMode === 'general';

    // response-mode wrap：一般對話反灰（mode-locked），不可點擊
    const rmWrap = document.querySelector('.response-mode-wrap');
    if (rmWrap) {
        rmWrap.classList.toggle('mode-locked', isGeneral);
        // 若切到一般對話，把已展開的 response-mode 選單收起
        if (isGeneral) {
            const rmMenu = document.getElementById('response-mode-menu');
            const rmBtn  = document.getElementById('response-mode-btn');
            if (rmMenu) rmMenu.classList.add('hidden');
            if (rmBtn)  rmBtn.setAttribute('aria-expanded', 'false');
        }
    }

    // 工具設定：一般對話永遠鎖定
    syncToolSettingsWithResponseMode();

    // placeholder
    const textarea = document.getElementById('user-input');
    if (textarea && !textarea.disabled) {
        if (isProjectViewVisible()) {
            textarea.placeholder = getPvComposePlaceholder();
        } else {
            textarea.placeholder = getMainChatInputPlaceholder();
        }
    }

    applyChatModeLabel();
    syncChatModeMenuSelection();
    refreshWelcomeHeroIfVisible();

    try { localStorage.setItem(CHAT_MODE_STORAGE_KEY, chatMode); } catch (_) {}
}

/** 建立或更新歡迎畫面（含模式對應的建議問題） */
function renderWelcomeHero(title) {
    const container = document.getElementById('chat-messages');
    if (!container) return;

    const existing = container.querySelector('.welcome-hero');
    if (existing) existing.remove();

    const hero = document.createElement('div');
    hero.className = 'welcome-hero';
    hero.dataset.mode = chatMode;

    const h1 = document.createElement('h1');
    h1.textContent = title || '您今天想問些什麼？';
    hero.appendChild(h1);

    const subtitle = document.createElement('p');
    subtitle.className = 'welcome-subtitle';
    subtitle.textContent = chatMode === 'general'
        ? '試試這些問題，或直接輸入你想問的'
        : '試試這些分析方向，或直接輸入你的問題';
    hero.appendChild(subtitle);

    const suggestionsWrap = document.createElement('div');
    suggestionsWrap.className = 'welcome-suggestions';
    suggestionsWrap.setAttribute('role', 'list');

    const prompts = SUGGESTED_PROMPTS[chatMode] || SUGGESTED_PROMPTS.general;
    prompts.forEach((text) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'suggestion-chip';
        btn.textContent = text;
        btn.setAttribute('role', 'listitem');
        btn.addEventListener('click', () => {
            const input = document.getElementById('user-input');
            if (!input || input.disabled) return;
            input.value = text;
            input.dispatchEvent(new Event('input'));
            sendMessage();
        });
        suggestionsWrap.appendChild(btn);
    });
    hero.appendChild(suggestionsWrap);

    container.appendChild(hero);
}

/** 若目前仍顯示歡迎畫面，依 chatMode 更新建議問題 */
function refreshWelcomeHeroIfVisible() {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    const hero = container.querySelector('.welcome-hero');
    if (!hero) return;
    const h1 = hero.querySelector('h1');
    const title = h1 ? h1.textContent : '您今天想問些什麼？';
    renderWelcomeHero(title);
}

function initChatModeSelector() {
    try {
        const s = localStorage.getItem(CHAT_MODE_STORAGE_KEY);
        if (s === 'general' || s === 'stock_agent') chatMode = s;
    } catch (_) {}

    const btn  = document.getElementById('chat-mode-btn');
    const menu = document.getElementById('chat-mode-menu');
    if (!btn || !menu) return;

    menu.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        syncChatModeMenuSelection();
        const willOpen = menu.classList.contains('hidden');
        menu.classList.toggle('hidden', !willOpen);
        btn.setAttribute('aria-expanded', String(willOpen));
    });
    menu.addEventListener('click', (e) => e.stopPropagation());

    menu.querySelectorAll('.chat-mode-option').forEach((li) => {
        li.addEventListener('click', () => {
            const v = li.getAttribute('data-value');
            if (v !== 'general' && v !== 'stock_agent') return;
            chatMode = v;
            applyChatModeUI();
            menu.classList.add('hidden');
            btn.setAttribute('aria-expanded', 'false');
        });
    });

    applyChatModeUI();
}

/** 快捷模式僅檢索新聞向量庫，與「工具權限」無關；反灰並關閉選單。 */
function syncToolSettingsWithResponseMode() {
    const toggle = document.getElementById('tool-toggle-btn');
    const popover = document.getElementById('tool-popover');
    const wrap = document.querySelector('.chat-input-area .tool-settings');

    // 一般對話 或 flash 模式都鎖定工具
    const lock = chatMode === 'general' || chatResponseMode === 'flash';
    if (wrap) wrap.classList.toggle('tool-settings--flash-lock', lock);

    if (toggle) {
        toggle.disabled = lock;
        if (lock) toggle.setAttribute('aria-disabled', 'true');
        else toggle.removeAttribute('aria-disabled');
        toggle.setAttribute(
            'title',
            chatMode === 'general'
                ? '一般對話不使用工具'
                : chatResponseMode === 'flash'
                    ? '快捷模式僅檢索新聞，無須調整工具'
                    : '切換工具權限'
        );
    }
    if (lock && popover) popover.classList.add('hidden');
}

function initResponseModeSelector() {
    try {
        const s = localStorage.getItem(RESPONSE_MODE_STORAGE_KEY);
        if (s === 'flash' || s === 'thinking') chatResponseMode = s;
    } catch (_) { /* noop */ }
    renderResponseModeMenuOptions();
    applyResponseModeLabel();
    syncResponseModeMenuSelection();
    initChatModeSelector();   // chat-mode tab 初始化（含首次 syncToolSettings）

    const btn = document.getElementById('response-mode-btn');
    const menu = document.getElementById('response-mode-menu');
    if (!btn || !menu) return;

    menu.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        syncResponseModeMenuSelection();
        menu.classList.toggle('hidden');
        btn.setAttribute('aria-expanded', String(!menu.classList.contains('hidden')));
    });
    menu.addEventListener('click', (e) => e.stopPropagation());

    menu.querySelectorAll('.response-mode-option').forEach((li) => {
        li.addEventListener('click', () => {
            const v = li.getAttribute('data-value');
            if (v !== 'flash' && v !== 'thinking') return;
            chatResponseMode = v;
            try {
                localStorage.setItem(RESPONSE_MODE_STORAGE_KEY, v);
            } catch (_) { /* noop */ }
            applyResponseModeLabel();
            syncResponseModeMenuSelection();
            syncToolSettingsWithResponseMode();
            menu.classList.add('hidden');
            btn.setAttribute('aria-expanded', 'false');
        });
    });

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

// --- 初始化 ---
document.addEventListener('DOMContentLoaded', async () => {
    initUiTheme();
    // 側欄先顯示載入指示，等 API 回來再渲染真實列表
    renderProjectsLoading();
    renderRecentChatsLoading();
    initEventListeners();
    initProjectViewTabs();

    // auth.js 的 DOMContentLoaded 會觸發 tryRefreshToken() 取得 AT；
    // 這裡再呼叫一次（受 _isRefreshing 並發鎖保護，會共用同一個 Promise，
    // 不會重複打 /refresh），確保我們在 AT 就緒後才載入專案列表。
    if (typeof tryRefreshToken === 'function') {
        await tryRefreshToken();
    }

    // 兩支 list API 沒有相依關係，並行載入
    await Promise.all([
        loadProjectsFromServer(),
        loadRecentChatsFromServer(),
        refreshUserQuotaFromServer(),
    ]);
    updateSendButtonForStreamingState();
});

function initEventListeners() {
    document.getElementById('send-btn').addEventListener('click', sendMessage);

    const userInput = document.getElementById('user-input');
    userInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
        updateSendButtonForStreamingState();
    });

    userInput.addEventListener('keydown', (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            const sendBtn = document.getElementById('send-btn');
            if (sendBtn && sendBtn.disabled) return;
            const cur = state.currentChatId;
            if (cur && streamingChatIds.has(cur)) return;
            if (!cur && newChatComposeLock) return;
            sendMessage();
        }
    });

    const toolToggle = document.getElementById('tool-toggle-btn');
    const toolPopover = document.getElementById('tool-popover');
    if (toolToggle && toolPopover) {
        toolToggle.onclick = (e) => {
            if (chatMode === 'general' || chatResponseMode === 'flash') {
                e.preventDefault();
                e.stopPropagation();
                return;
            }
            e.stopPropagation();
            toolPopover.classList.toggle('hidden');
        };
    }
    document.addEventListener('click', () => {
        if (toolPopover) toolPopover.classList.add('hidden');
        const rm = document.getElementById('response-mode-menu');
        const rb = document.getElementById('response-mode-btn');
        if (rm) rm.classList.add('hidden');
        if (rb) rb.setAttribute('aria-expanded', 'false');
        // 同步關閉 chat-mode 選單
        const cm = document.getElementById('chat-mode-menu');
        const cb = document.getElementById('chat-mode-btn');
        if (cm) cm.classList.add('hidden');
        if (cb) cb.setAttribute('aria-expanded', 'false');
    });
    if (toolPopover) {
        toolPopover.onclick = (e) => e.stopPropagation();
    }

    const toolAuto = document.getElementById('tool-auto');
    const manualGroup = document.getElementById('manual-tools');
    if (toolAuto) {
        toolAuto.onchange = () => {
            manualGroup.classList.toggle('active', !toolAuto.checked);
        };
    }

    document.getElementById('new-chat-btn').addEventListener('click', () => {
        // 重置 chat / project context：
        //   - currentChatId = null  → 下次 sendMessage 才會走 POST /api/chat 建立新 chat
        //   - currentProjectId = null → sidebar 的「新對話」屬於全域層級，不歸任何 project
        // 若不清這兩個值，從歷史 chat 切過來再按「新對話」會繼續沿用舊 chat_id，
        // 訊息會被塞進舊 chat，新 chat 也永遠不會被建立。
        maybeParkViewportForLeavingChat(state.currentChatId);
        state.currentChatId = null;
        state.currentProjectId = null;

        showChatView();
        clearChatMessages();

        // 取消 sidebar 的 active 高亮
        renderProjects();
        renderRecentChats();
        lucide.createIcons();
        setMainChatTitle('歡迎回來');
        updateSendButtonForStreamingState();
        closeSidebarDrawer();
    });

    initMobileSidebar();
    initSidebarResize();

    initThemeToggle();
    initResponseModeSelector();
}

// ============================================================
// 後端整合：載入 / 重新整理專案列表與詳情
// ============================================================

/**
 * 從後端載入目前登入使用者的所有專案，並重新渲染左側列表。
 * 對應端點：GET /api/project/all
 *
 * 若 user 還沒有任何專案，state.projects 會是空陣列，
 * 左側只會剩下「新增專案」按鈕（符合需求：不顯示假資料）。
 */
async function loadProjectsFromServer() {
    try {
        const res = await authFetch(`${state.apiBase}/project/all`);
        if (!res) return;                  // authFetch 401 → 已導向 login

        if (!res.ok) {
            console.error('載入專案列表失敗：', res.status);
            return;
        }

        const json = await res.json();
        const projects = (json && json.data) ? json.data : [];

        // 替換 state.projects
        state.projects = projects.map(p => ({
            id: p.id,
            name: p.name,
            created_at: p.created_at,
            updated_at: p.updated_at,
        }));

        // 清掉已不存在的 chats / files 快取
        const validIds = new Set(state.projects.map(p => p.id));
        for (const id of Object.keys(state.chats)) {
            if (!validIds.has(id)) delete state.chats[id];
        }
        for (const id of Object.keys(state.files)) {
            if (!validIds.has(id)) delete state.files[id];
        }
    } catch (err) {
        console.error('載入專案列表時發生錯誤：', err);
    } finally {
        renderProjects();
        lucide.createIcons();
    }
}

/**
 * 載入指定專案詳情（含 chats / files），更新 state 並回傳 detail 物件。
 * 對應端點：GET /api/project?project_id=xxx
 *
 * 失敗（404 / 500）回傳 null。
 */
async function loadProjectDetail(projectId) {
    try {
        const url = `${state.apiBase}/project?project_id=${encodeURIComponent(projectId)}`;
        const res = await authFetch(url);
        if (!res) return null;

        if (!res.ok) {
            console.error('載入專案詳情失敗：', res.status);
            return null;
        }

        const json = await res.json();
        const detail = json && json.data ? json.data : null;
        if (!detail) return null;

        // 同步寫回 state，以便其他地方（例如最近聊天）能讀到
        state.chats[detail.id] = (detail.chats || []).map(c => ({
            id: c.id,
            title: c.title,
        }));
        state.files[detail.id] = (detail.files || []).map(f => ({
            id: f.id,
            file_name: f.file_name,
            s3_url: f.s3_url,
            file_type: f.file_type,
            status: f.status,
            created_at: f.created_at,
        }));

        return detail;
    } catch (err) {
        console.error('載入專案詳情時發生錯誤：', err);
        return null;
    }
}

// --- 渲染 UI ---

/**
 * 渲染專案列表（頂端固定「新增專案」項目，下方為可展開的專案列表）
 */
function renderProjects() {
    const list = document.getElementById('project-list');
    while (list.firstChild) list.removeChild(list.firstChild);

    // ── 「新增專案」固定首項 ──
    const newLi = document.createElement('li');
    newLi.className = 'new-project-item';

    const newIcon = document.createElement('i');
    newIcon.setAttribute('data-lucide', 'folder-plus');

    const newText = document.createElement('span');
    newText.textContent = '新增專案';

    newLi.appendChild(newIcon);
    newLi.appendChild(newText);
    newLi.addEventListener('click', openCreateProjectModal);
    list.appendChild(newLi);

    // ── 各專案 ──
    // 點擊專案行 → 直接進入專案視圖（不再下拉展開，因為視圖內已能看到所有 chats / files）
    // 滑鼠 hover / 該專案 active 時，右側會出現三點按鈕，點下開啟操作選單（目前只有「刪除專案」）
    state.projects.forEach(p => {
        const li = document.createElement('li');
        li.id = `project-li-${p.id}`;
        li.dataset.projectId = p.id;

        const isActive = p.id === state.currentProjectId;

        // 專案行
        const row = document.createElement('div');
        row.className = 'project-row' + (isActive ? ' active' : '');
        row.id = `project-row-${p.id}`;
        row.dataset.projectId = p.id;

        const folderIcon = document.createElement('i');
        folderIcon.setAttribute('data-lucide', 'folder');
        folderIcon.className = 'project-row-icon';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'project-row-name';
        nameSpan.textContent = p.name;

        // 三點選單按鈕（hover / active 時顯示）
        const menuBtn = document.createElement('button');
        menuBtn.type = 'button';
        menuBtn.className = 'project-row-menu-btn';
        menuBtn.id = `project-menu-btn-${p.id}`;
        menuBtn.setAttribute('aria-label', '專案操作選單');
        menuBtn.dataset.projectId = p.id;
        const dotsIcon = document.createElement('i');
        dotsIcon.setAttribute('data-lucide', 'more-horizontal');
        menuBtn.appendChild(dotsIcon);

        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();   // 不要觸發 row 的點擊（避免進入 project view）
            openProjectMenu(p, menuBtn);
        });

        row.appendChild(folderIcon);
        row.appendChild(nameSpan);
        row.appendChild(menuBtn);

        attachProjectDropTarget(row, p.id);

        row.addEventListener('click', async () => {
            closeSidebarDrawer();
            maybeParkViewportForLeavingChat(state.currentChatId);

            state.currentProjectId = p.id;
            state.currentChatId = null;

            // 先打開 project view（顯示 hero）並標記載入中
            showProjectView(p, { loading: true });

            // 抓取最新詳情，再渲染 chats / files
            const detail = await loadProjectDetail(p.id);

            // 若使用者在請求過程中已切到別的專案，就不覆蓋畫面
            if (state.currentProjectId !== p.id) return;

            renderProjects();
            renderRecentChats();
            lucide.createIcons();
            updateSendButtonForStreamingState();
            showProjectView(p, { detail });
        });

        li.appendChild(row);
        list.appendChild(li);
    });

    lucide.createIcons();
}

/**
 * 從後端載入「最近」chats（不含已加入專案者），
 * 對應端點：GET /api/chat/all
 */
async function loadRecentChatsFromServer() {
    try {
        const res = await authFetch(`${state.apiBase}/chat/all`);
        if (!res) return;                 // authFetch 401 → 已導向 login
        if (!res.ok) {
            console.error('載入最近聊天失敗：', res.status);
            return;
        }
        const json = await res.json();
        const chats = (json && json.data) ? json.data : [];

        state.recentChats = chats
            .filter(c => !c.project_id)
            .map(c => ({
                id: c.id,
                title: c.title,
                created_at: c.created_at,
                updated_at: c.updated_at,
            }));
    } catch (err) {
        console.error('載入最近聊天時發生錯誤：', err);
    } finally {
        renderRecentChats();
        lucide.createIcons();
    }
}

/**
 * 渲染左側 sidebar「最近」區塊。
 * 資料來源：state.recentChats（由 GET /api/chat/all 載入，後端已按 updated_at 由近到遠排序）
 *
 * 每個 <li>：
 *   - id="recent-chat-${chat.id}"
 *   - dataset.chatId = chat.id
 *   - 顯示 chat title
 */
function renderRecentChats() {
    const list = document.getElementById('recent-chat-list');
    while (list.firstChild) list.removeChild(list.firstChild);

    (state.recentChats || []).forEach(c => {
        const li = document.createElement('li');
        li.className = 'recent-chat-item';
        li.id = `recent-chat-${c.id}`;
        li.dataset.chatId = c.id;

        const isActive = c.id === state.currentChatId;

        const row = document.createElement('div');
        row.className = 'project-row' + (isActive ? ' active' : '');
        row.dataset.chatId = c.id;

        const msgIcon = document.createElement('i');
        msgIcon.setAttribute('data-lucide', 'message-square');
        msgIcon.className = 'project-row-icon';

        const titleSpan = document.createElement('span');
        titleSpan.className = 'project-row-name';
        titleSpan.textContent = c.title || '(未命名聊天)';

        const menuBtn = document.createElement('button');
        menuBtn.type = 'button';
        menuBtn.className = 'project-row-menu-btn';
        menuBtn.setAttribute('aria-label', '聊天操作選單');
        menuBtn.dataset.chatId = c.id;
        const dotsIcon = document.createElement('i');
        dotsIcon.setAttribute('data-lucide', 'more-horizontal');
        menuBtn.appendChild(dotsIcon);

        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openChatMenu(c, menuBtn);
        });

        menuBtn.setAttribute('draggable', 'false');

        row.appendChild(msgIcon);
        row.appendChild(titleSpan);
        row.appendChild(menuBtn);

        attachRecentChatDrag(row, li, c.id);

        row.addEventListener('click', async () => {
            await navigateToChat(c.id);
        });

        li.appendChild(row);
        list.appendChild(li);
    });

    lucide.createIcons();
}

// ============================================================
// 專案右鍵 Popover 選單（三點按鈕）
// ============================================================

let _activeProjectPopover = null;   // 目前顯示中的 popover element
let _activePopoverAnchor  = null;   // 觸發 popover 的按鈕（用於切換 .open class）

/**
 * 打開「專案操作」popover。
 * 以 fixed 定位貼在 anchor（三點按鈕）右下方，超出視窗邊界時會自動翻到左側 / 上方。
 */
function openProjectMenu(project, anchor) {
    // 切換：點同一個按鈕第二次 → 關閉
    if (_activeProjectPopover && _activePopoverAnchor === anchor) {
        closeProjectMenu();
        return;
    }
    closeProjectMenu();
    closeChatMenu();

    const pop = document.createElement('div');
    pop.className = 'project-popover';
    pop.id = `project-popover-${project.id}`;

    // 「刪除專案」
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'project-popover-item danger';
    const trashIcon = document.createElement('i');
    trashIcon.setAttribute('data-lucide', 'trash-2');
    const delLabel = document.createElement('span');
    delLabel.textContent = '刪除專案';
    delBtn.appendChild(trashIcon);
    delBtn.appendChild(delLabel);
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeProjectMenu();
        openDeleteProjectModal(project);
    });
    pop.appendChild(delBtn);

    // 計算位置：先放在按鈕右下，超出視窗則翻到左 / 上
    document.body.appendChild(pop);
    const rect = anchor.getBoundingClientRect();
    const popRect = pop.getBoundingClientRect();
    const margin = 6;

    let left = rect.right + margin;
    let top  = rect.bottom + margin;
    if (left + popRect.width > window.innerWidth - 8) {
        left = rect.left - popRect.width - margin;     // 翻到左側
        if (left < 8) left = 8;
    }
    if (top + popRect.height > window.innerHeight - 8) {
        top = rect.top - popRect.height - margin;      // 翻到上方
        if (top < 8) top = 8;
    }
    pop.style.left = `${left}px`;
    pop.style.top  = `${top}px`;

    anchor.classList.add('open');
    _activeProjectPopover = pop;
    _activePopoverAnchor  = anchor;

    lucide.createIcons();
}

function closeProjectMenu() {
    if (_activeProjectPopover && _activeProjectPopover.parentNode) {
        _activeProjectPopover.parentNode.removeChild(_activeProjectPopover);
    }
    if (_activePopoverAnchor) {
        _activePopoverAnchor.classList.remove('open');
    }
    _activeProjectPopover = null;
    _activePopoverAnchor  = null;
}

// 點擊其他地方 / Esc / 視窗大小改變時關閉 popover
document.addEventListener('click', (e) => {
    if (!_activeProjectPopover) return;
    // 點到 popover 內 or anchor 不算外部
    if (_activeProjectPopover.contains(e.target)) return;
    if (_activePopoverAnchor && _activePopoverAnchor.contains(e.target)) return;
    closeProjectMenu();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeProjectMenu();
});
window.addEventListener('resize',  () => closeProjectMenu());
window.addEventListener('scroll',  () => closeProjectMenu(), true);


// ============================================================
// 聊天操作 Popover 選單（三點按鈕）
// ============================================================

let _activeChatPopover = null;
let _activeChatPopoverAnchor = null;

/**
 * 在 state 中更新 chat title（recentChats + 專案內 chats）。
 */
function updateChatTitleInState(chatId, newTitle) {
    const sid = String(chatId);
    const rc = (state.recentChats || []).find(c => String(c.id) === sid);
    if (rc) rc.title = newTitle;

    for (const pid of Object.keys(state.chats || {})) {
        const found = (state.chats[pid] || []).find(c => String(c.id) === sid);
        if (found) found.title = newTitle;
    }
}

/**
 * 從 state 移除 chat（recentChats + 專案內 chats）。
 */
function removeChatFromState(chatId) {
    const sid = String(chatId);
    state.recentChats = (state.recentChats || []).filter(c => String(c.id) !== sid);

    for (const pid of Object.keys(state.chats || {})) {
        if (!state.chats[pid]) continue;
        state.chats[pid] = state.chats[pid].filter(c => String(c.id) !== sid);
    }
}

function openChatMenu(chat, anchor, options = {}) {
    if (_activeChatPopover && _activeChatPopoverAnchor === anchor) {
        closeChatMenu();
        return;
    }
    closeChatMenu();
    closeProjectMenu();

    const pop = document.createElement('div');
    pop.className = 'project-popover';
    pop.id = `chat-popover-${chat.id}`;

    const renameBtn = document.createElement('button');
    renameBtn.type = 'button';
    renameBtn.className = 'project-popover-item';
    const pencilIcon = document.createElement('i');
    pencilIcon.setAttribute('data-lucide', 'pencil');
    const renameLabel = document.createElement('span');
    renameLabel.textContent = '重新命名';
    renameBtn.appendChild(pencilIcon);
    renameBtn.appendChild(renameLabel);
    renameBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeChatMenu();
        openEditChatTitleModal(chat);
    });
    pop.appendChild(renameBtn);

    if (options.projectId) {
        const removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'project-popover-item';
        const unlinkIcon = document.createElement('i');
        unlinkIcon.setAttribute('data-lucide', 'folder-minus');
        const removeLabel = document.createElement('span');
        removeLabel.textContent = '從專案移除';
        removeBtn.appendChild(unlinkIcon);
        removeBtn.appendChild(removeLabel);
        removeBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            closeChatMenu();
            await removeChatFromProject(chat.id, options.projectId);
        });
        pop.appendChild(removeBtn);
    }

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'project-popover-item danger';
    const trashIcon = document.createElement('i');
    trashIcon.setAttribute('data-lucide', 'trash-2');
    const delLabel = document.createElement('span');
    delLabel.textContent = '刪除聊天';
    delBtn.appendChild(trashIcon);
    delBtn.appendChild(delLabel);
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        closeChatMenu();
        openDeleteChatModal(chat);
    });
    pop.appendChild(delBtn);

    document.body.appendChild(pop);
    const rect = anchor.getBoundingClientRect();
    const popRect = pop.getBoundingClientRect();
    const margin = 6;

    let left = rect.right + margin;
    let top  = rect.bottom + margin;
    if (left + popRect.width > window.innerWidth - 8) {
        left = rect.left - popRect.width - margin;
        if (left < 8) left = 8;
    }
    if (top + popRect.height > window.innerHeight - 8) {
        top = rect.top - popRect.height - margin;
        if (top < 8) top = 8;
    }
    pop.style.left = `${left}px`;
    pop.style.top  = `${top}px`;

    anchor.classList.add('open');
    _activeChatPopover = pop;
    _activeChatPopoverAnchor = anchor;

    lucide.createIcons();
}

// ============================================================
// Chat ↔ Project 關聯（拖曳加入 / 選單移除）
// ============================================================

function isChatInProject(chatId, projectId) {
    return (state.chats[projectId] || []).some(c => String(c.id) === String(chatId));
}

function syncChatInProjectState(chatId, projectId, title) {
    const sid = String(chatId);
    for (const pid of Object.keys(state.chats || {})) {
        state.chats[pid] = (state.chats[pid] || []).filter(c => String(c.id) !== sid);
    }
    if (projectId) {
        if (!state.chats[projectId]) state.chats[projectId] = [];
        state.chats[projectId].unshift({
            id: chatId,
            title: title || resolveChatTitleForId(chatId),
        });
    }
}

/** 將 chat 移出所有專案，加入「最近」側欄 state */
function moveChatToRecentState(chatId, meta = {}) {
    const sid = String(chatId);
    for (const pid of Object.keys(state.chats || {})) {
        state.chats[pid] = (state.chats[pid] || []).filter(c => String(c.id) !== sid);
    }

    const prev = (state.recentChats || []).find(c => String(c.id) === sid);
    const entry = {
        id: chatId,
        title: meta.title || (prev && prev.title) || resolveChatTitleForId(chatId),
        created_at: meta.created_at || (prev && prev.created_at) || null,
        updated_at: meta.updated_at || (prev && prev.updated_at) || new Date().toISOString(),
    };

    state.recentChats = (state.recentChats || []).filter(c => String(c.id) !== sid);
    state.recentChats.unshift(entry);
    renderRecentChats();
    lucide.createIcons();
}

/** 將 chat 從「最近」移入指定專案 state（並自 recent 移除） */
function moveChatToProjectState(chatId, projectId, meta = {}) {
    const sid = String(chatId);
    state.recentChats = (state.recentChats || []).filter(c => String(c.id) !== sid);
    syncChatInProjectState(chatId, projectId, meta.title);
    renderRecentChats();
    lucide.createIcons();
}

async function addChatToProject(chatId, projectId) {
    if (isChatInProject(chatId, projectId)) {
        showToast('這則聊天已在該專案中', 'info');
        return;
    }

    const project = state.projects.find(p => String(p.id) === String(projectId));
    const projectName = project ? project.name : '專案';

    try {
        const url = `${state.apiBase}/chat/project?chat_id=${encodeURIComponent(chatId)}`;
        const res = await authFetch(url, {
            method: 'POST',
            body: JSON.stringify({ project_id: projectId }),
        });
        if (!res) return;

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = (data && data.detail) ? data.detail : `加入失敗（HTTP ${res.status}）`;
            showToast(detail, 'error');
            return;
        }

        const row = data && data.data ? data.data : {};
        moveChatToProjectState(chatId, projectId, { title: row.title });

        if (isProjectViewVisible() && state.currentProjectId === projectId) {
            renderPvChats(state.chats[projectId] || [], projectId);
        }
        showToast(`已加入「${projectName}」`, 'success');
    } catch (err) {
        showToast(err.message || '加入專案失敗，請稍後再試', 'error');
    }
}

async function removeChatFromProject(chatId, projectId) {
    try {
        const url = `${state.apiBase}/chat/project?chat_id=${encodeURIComponent(chatId)}`;
        const res = await authFetch(url, { method: 'DELETE' });
        if (!res) return;

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = (data && data.detail) ? data.detail : `移除失敗（HTTP ${res.status}）`;
            showToast(detail, 'error');
            return;
        }

        const row = data && data.data ? data.data : {};
        const sid = String(chatId);
        if (state.chats[projectId]) {
            state.chats[projectId] = state.chats[projectId].filter(c => String(c.id) !== sid);
        }

        moveChatToRecentState(chatId, {
            title: row.title,
            updated_at: row.updated_at,
        });

        if (isProjectViewVisible() && state.currentProjectId === projectId) {
            renderPvChats(state.chats[projectId] || [], projectId);
        }
        showToast('已從專案移除', 'success');
    } catch (err) {
        showToast(err.message || '移除失敗，請稍後再試', 'error');
    }
}

const SIDEBAR_DRAG_SCROLL_EDGE_PX = 52;
const SIDEBAR_DRAG_SCROLL_MAX_STEP = 12;

let _sidebarDragScrollActive = false;
let _sidebarDragPointerX = 0;
let _sidebarDragPointerY = 0;
let _sidebarDragScrollRaf = null;

function getSidebarScrollEl() {
    return document.querySelector('.sidebar-scroll');
}

function getSidebarEl() {
    return document.querySelector('.sidebar');
}

function onDocumentDragOverForSidebarScroll(e) {
    if (!_sidebarDragScrollActive) return;
    _sidebarDragPointerX = e.clientX;
    _sidebarDragPointerY = e.clientY;
}

function tickSidebarDragScroll() {
    if (!_sidebarDragScrollActive) {
        _sidebarDragScrollRaf = null;
        return;
    }

    const scroller = getSidebarScrollEl();
    const sidebar = getSidebarEl();
    if (scroller && sidebar) {
        const scrollRect = scroller.getBoundingClientRect();
        const sidebarRect = sidebar.getBoundingClientRect();
        const y = _sidebarDragPointerY;
        const x = _sidebarDragPointerX;
        const inSidebarColumn = x >= sidebarRect.left - 12
            && x <= sidebarRect.right + 12
            && y >= sidebarRect.top
            && y <= sidebarRect.bottom;

        if (inSidebarColumn) {
            if (y < scrollRect.top + SIDEBAR_DRAG_SCROLL_EDGE_PX) {
                const dist = scrollRect.top + SIDEBAR_DRAG_SCROLL_EDGE_PX - y;
                const intensity = Math.min(1, dist / SIDEBAR_DRAG_SCROLL_EDGE_PX);
                scroller.scrollTop -= SIDEBAR_DRAG_SCROLL_MAX_STEP * Math.max(0.3, intensity);
            } else if (y > scrollRect.bottom - SIDEBAR_DRAG_SCROLL_EDGE_PX) {
                const dist = y - (scrollRect.bottom - SIDEBAR_DRAG_SCROLL_EDGE_PX);
                const intensity = Math.min(1, dist / SIDEBAR_DRAG_SCROLL_EDGE_PX);
                scroller.scrollTop += SIDEBAR_DRAG_SCROLL_MAX_STEP * Math.max(0.3, intensity);
            }
        }
    }

    _sidebarDragScrollRaf = requestAnimationFrame(tickSidebarDragScroll);
}

function startSidebarDragScroll(clientX, clientY) {
    _sidebarDragPointerX = clientX;
    _sidebarDragPointerY = clientY;
    if (_sidebarDragScrollActive) return;
    _sidebarDragScrollActive = true;
    document.addEventListener('dragover', onDocumentDragOverForSidebarScroll);
    if (!_sidebarDragScrollRaf) {
        _sidebarDragScrollRaf = requestAnimationFrame(tickSidebarDragScroll);
    }
}

function stopSidebarDragScroll() {
    _sidebarDragScrollActive = false;
    document.removeEventListener('dragover', onDocumentDragOverForSidebarScroll);
    if (_sidebarDragScrollRaf) {
        cancelAnimationFrame(_sidebarDragScrollRaf);
        _sidebarDragScrollRaf = null;
    }
}

function attachRecentChatDrag(row, li, chatId) {
    row.setAttribute('draggable', 'true');
    row.addEventListener('dragstart', (e) => {
        if (e.target.closest('.project-row-menu-btn')) {
            e.preventDefault();
            return;
        }
        e.dataTransfer.setData('text/chat-id', String(chatId));
        e.dataTransfer.effectAllowed = 'move';
        li.classList.add('is-dragging');
        startSidebarDragScroll(e.clientX, e.clientY);
    });
    row.addEventListener('dragend', () => {
        li.classList.remove('is-dragging');
        stopSidebarDragScroll();
        document.querySelectorAll('.project-row.drop-target').forEach((el) => {
            el.classList.remove('drop-target');
        });
    });
}

function attachProjectDropTarget(row, projectId) {
    row.addEventListener('dragover', (e) => {
        if (!e.dataTransfer.types.includes('text/chat-id')) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', (e) => {
        if (row.contains(e.relatedTarget)) return;
        row.classList.remove('drop-target');
    });
    row.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        row.classList.remove('drop-target');
        const chatId = e.dataTransfer.getData('text/chat-id');
        if (!chatId) return;
        await addChatToProject(chatId, projectId);
    });
}

function closeChatMenu() {
    if (_activeChatPopover && _activeChatPopover.parentNode) {
        _activeChatPopover.parentNode.removeChild(_activeChatPopover);
    }
    if (_activeChatPopoverAnchor) {
        _activeChatPopoverAnchor.classList.remove('open');
    }
    _activeChatPopover = null;
    _activeChatPopoverAnchor = null;
}

document.addEventListener('click', (e) => {
    if (!_activeChatPopover) return;
    if (_activeChatPopover.contains(e.target)) return;
    if (_activeChatPopoverAnchor && _activeChatPopoverAnchor.contains(e.target)) return;
    closeChatMenu();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeChatMenu();
});
window.addEventListener('resize',  () => closeChatMenu());
window.addEventListener('scroll',  () => closeChatMenu(), true);


// ============================================================
// 編輯聊天標題 Modal + API
// ============================================================

function openEditChatTitleModal(chat) {
    state.pendingEditChat = chat;

    const modal = document.getElementById('edit-chat-title-modal');
    const input = document.getElementById('edit-chat-title-input');
    const submitBtn = document.getElementById('edit-chat-title-submit-btn');
    const msg = document.getElementById('edit-chat-title-msg');

    input.value = chat.title || '';
    submitBtn.disabled = input.value.trim().length === 0;
    submitBtn.textContent = '儲存';
    msg.className = 'modal-msg';
    msg.textContent = '';

    closeAllModals();
    closeChatMenu();
    modal.classList.add('show');
    lucide.createIcons();
    setTimeout(() => input.focus(), 100);
}

function closeEditChatTitleModal() {
    document.getElementById('edit-chat-title-modal').classList.remove('show');
    state.pendingEditChat = null;
}

function onChatTitleInput() {
    const input = document.getElementById('edit-chat-title-input');
    const submitBtn = document.getElementById('edit-chat-title-submit-btn');
    if (!input || !submitBtn) return;
    const val = input.value.trim();
    const tooLong = input.value.length > CHAT_TITLE_MAX_CHARS;
    submitBtn.disabled = val.length === 0 || tooLong;
}

async function submitEditChatTitle() {
    const chat = state.pendingEditChat;
    if (!chat) return;

    const nameInput = document.getElementById('edit-chat-title-input');
    const submitBtn = document.getElementById('edit-chat-title-submit-btn');
    const msg = document.getElementById('edit-chat-title-msg');

    const title = nameInput.value.trim();
    if (!title) return;

    submitBtn.disabled = true;
    submitBtn.textContent = '儲存中…';
    msg.className = 'modal-msg';
    msg.textContent = '';

    try {
        const url = `${state.apiBase}/chat?chat_id=${encodeURIComponent(chat.id)}`;
        const res = await authFetch(url, {
            method: 'PATCH',
            body: JSON.stringify({ title }),
        });

        if (!res) return;

        const data = await res.json();

        if (!res.ok) {
            const detail = data.detail || '更新失敗，請稍後再試。';
            msg.textContent = detail;
            msg.className = 'modal-msg error';
            submitBtn.disabled = false;
            submitBtn.textContent = '儲存';
            return;
        }

        const newTitle = (data.data && data.data.title) ? data.data.title : title;
        updateChatTitleInState(chat.id, newTitle);

        if (String(state.currentChatId) === String(chat.id)) {
            setMainChatTitle(newTitle);
        }

        renderRecentChats();
        if (state.currentProjectId && state.chats[state.currentProjectId]) {
            renderPvChats(state.chats[state.currentProjectId], state.currentProjectId);
        }

        closeEditChatTitleModal();
        showToast('聊天標題已更新', 'success');

    } catch (err) {
        const detail = err && err.message ? err.message : '網路錯誤，請稍後再試';
        msg.textContent = detail;
        msg.className = 'modal-msg error';
        submitBtn.disabled = false;
        submitBtn.textContent = '儲存';
    }
}


// ============================================================
// 刪除聊天 Modal + API
// ============================================================

function openDeleteChatModal(chat) {
    state.pendingDeleteChat = chat;

    const modal = document.getElementById('delete-chat-modal');
    const btn = document.getElementById('delete-chat-confirm-btn');
    const msg = document.getElementById('delete-chat-msg');

    btn.disabled = false;
    btn.textContent = '刪除';
    msg.className = 'modal-msg';
    msg.textContent = '';

    closeAllModals();
    closeChatMenu();
    modal.classList.add('show');
    lucide.createIcons();
}

async function confirmDeleteChat() {
    const chat = state.pendingDeleteChat;
    if (!chat) return;

    const btn = document.getElementById('delete-chat-confirm-btn');
    const msg = document.getElementById('delete-chat-msg');

    btn.disabled = true;
    btn.textContent = '刪除中…';
    msg.className = 'modal-msg';
    msg.textContent = '';

    const chatIdStr = String(chat.id);

    try {
        const url = `${state.apiBase}/chat?chat_id=${encodeURIComponent(chat.id)}`;
        const res = await authFetch(url, { method: 'DELETE' });

        if (!res) return;

        if (!res.ok) {
            let detail = `刪除失敗（HTTP ${res.status}）`;
            try {
                const data = await res.json();
                if (data && data.detail) detail = data.detail;
            } catch { /* ignore */ }

            msg.textContent = detail;
            msg.className = 'modal-msg error';
            showToast(`刪除失敗：${detail}`, 'error');
            btn.disabled = false;
            btn.textContent = '刪除';
            return;
        }

        evictParkedPane(chatIdStr);
        removeChatFromState(chat.id);

        if (String(state.currentChatId) === chatIdStr) {
            state.currentChatId = null;
            showChatView();
            clearChatMessages();
            setMainChatTitle('歡迎回來');
        }

        updateSendButtonForStreamingState();
        closeAllModals();
        state.pendingDeleteChat = null;

        renderRecentChats();
        if (state.currentProjectId && state.chats[state.currentProjectId]) {
            renderPvChats(state.chats[state.currentProjectId], state.currentProjectId);
        }

        const label = chat.title || '(未命名聊天)';
        showToast(`已刪除聊天「${label}」`, 'success');

    } catch (err) {
        const detail = err && err.message ? err.message : '網路錯誤，請稍後再試';
        msg.textContent = detail;
        msg.className = 'modal-msg error';
        showToast(`刪除失敗：${detail}`, 'error');
        btn.disabled = false;
        btn.textContent = '刪除';
    }
}


// ============================================================
// 刪除專案 Modal + API
// ============================================================

/**
 * 開啟「確認刪除專案」modal。
 *   - 因為刪除會 CASCADE 連帶清掉所有底下的 chats / messages / files，
 *     這裡多一道確認，避免使用者誤點。
 *   - state.pendingDeleteProject 暫存目標專案，confirmDeleteProject() 會用到。
 */
function openDeleteProjectModal(project) {
    state.pendingDeleteProject = project;

    const modal = document.getElementById('delete-project-modal');
    const btn   = document.getElementById('delete-project-confirm-btn');
    const msg   = document.getElementById('delete-project-msg');

    btn.disabled = false;
    btn.textContent = '刪除';
    msg.className = 'modal-msg';
    msg.textContent = '';

    closeAllModals();              // 關掉其它 modal
    modal.classList.add('show');
    lucide.createIcons();
}

/**
 * 點擊「刪除」→ 呼叫 DELETE /api/project?project_id=xxx
 *
 * 端點需求（見 app/backend/api/project.py:342）：
 *   - Header : Authorization: Bearer <AT>     ← authFetch 自動處理
 *   - Query  : project_id=<UUID>              ← 必填
 *   - 失敗回應：401 / 403 / 404 / 500，以 detail 字串說明原因
 */
async function confirmDeleteProject() {
    const project = state.pendingDeleteProject;
    if (!project) return;

    const btn = document.getElementById('delete-project-confirm-btn');
    const msg = document.getElementById('delete-project-msg');

    btn.disabled = true;
    btn.textContent = '刪除中…';
    msg.className = 'modal-msg';
    msg.textContent = '';

    const cascadeChatIds = (state.chats[project.id] || []).map(c => String(c.id));

    try {
        const url = `${state.apiBase}/project?project_id=${encodeURIComponent(project.id)}`;
        const res = await authFetch(url, { method: 'DELETE' });

        if (!res) return;          // authFetch 已導向 login

        if (!res.ok) {
            // 嘗試解析後端 detail；有些錯誤可能不是 JSON
            let detail = `刪除失敗（HTTP ${res.status}）`;
            try {
                const data = await res.json();
                if (data && data.detail) detail = data.detail;
            } catch { /* ignore JSON parse error */ }

            // Modal 內顯示錯誤；Toast 也彈一個（雙保險，使用者一定看得到）
            msg.textContent = detail;
            msg.className = 'modal-msg error';
            showToast(`刪除失敗：${detail}`, 'error');

            btn.disabled = false;
            btn.textContent = '刪除';
            return;
        }

        // ── 成功 ──
        // 1. 從本地 state 移除（避免重新 fetch 前畫面殘留）
        state.projects = state.projects.filter(p => p.id !== project.id);
        delete state.chats[project.id];
        delete state.files[project.id];

        for (const cid of cascadeChatIds) evictParkedPane(cid);

        if (state.currentChatId && cascadeChatIds.includes(String(state.currentChatId))) {
            state.currentChatId = null;
            showChatView();
            clearChatMessages();
        }

        // 2. 若刪掉的是目前顯示中的專案，回到歡迎畫面
        if (state.currentProjectId === project.id) {
            state.currentProjectId = null;
            state.currentChatId    = null;
            showChatView();
            clearChatMessages();
        }

        updateSendButtonForStreamingState();

        closeAllModals();
        state.pendingDeleteProject = null;

        // 3. 重新從後端載入專案列表 + 最近聊天列表（與真相同步）
        // 刪 project 會 CASCADE 把底下所有 chats 一起刪掉，所以最近列表也要重抓
        await Promise.all([
            loadProjectsFromServer(),
            loadRecentChatsFromServer(),
        ]);

        showToast(`已刪除專案「${project.name}」`, 'success');

    } catch (err) {
        const detail = err && err.message ? err.message : '網路錯誤，請稍後再試';
        msg.textContent = detail;
        msg.className = 'modal-msg error';
        showToast(`刪除失敗：${detail}`, 'error');
        btn.disabled = false;
        btn.textContent = '刪除';
    }
}


// ============================================================
// Toast 工具（成功 / 失敗 / 一般訊息）
// ============================================================

/**
 * 在右上角顯示一個會自動消失的小框提示。
 * @param {string} message
 * @param {'error'|'success'|'info'} type
 * @param {number} duration  毫秒，預設 4000
 */
function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');

    const icon = document.createElement('i');
    const iconName = type === 'error'   ? 'alert-circle'
                  : type === 'success' ? 'check-circle-2'
                  : 'info';
    icon.setAttribute('data-lucide', iconName);

    const msgEl = document.createElement('span');
    msgEl.className = 'toast-msg';
    msgEl.textContent = message;

    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'toast-close';
    closeBtn.setAttribute('aria-label', '關閉');
    const xIcon = document.createElement('i');
    xIcon.setAttribute('data-lucide', 'x');
    xIcon.setAttribute('width', '14');
    xIcon.setAttribute('height', '14');
    closeBtn.appendChild(xIcon);

    const dismiss = () => {
        if (!toast.parentNode) return;
        toast.classList.add('fading');
        setTimeout(() => toast.remove(), 200);
    };
    closeBtn.addEventListener('click', dismiss);

    toast.appendChild(icon);
    toast.appendChild(msgEl);
    toast.appendChild(closeBtn);
    container.appendChild(toast);
    lucide.createIcons();

    if (duration > 0) {
        setTimeout(dismiss, duration);
    }
}


// ============================================================
// 建立專案 Modal
// ============================================================

function openCreateProjectModal() {
    const modal = document.getElementById('create-project-modal');
    const input = document.getElementById('create-project-name');
    const submitBtn = document.getElementById('create-project-submit-btn');
    const msg = document.getElementById('create-project-msg');

    // 重置狀態（包含按鈕文字，防止上次「建立中…」殘留）
    input.value = '';
    submitBtn.disabled = true;
    submitBtn.textContent = '建立專案';
    msg.className = 'modal-msg';
    msg.textContent = '';

    modal.classList.add('show');
    setTimeout(() => input.focus(), 100);
}

function closeCreateProjectModal() {
    document.getElementById('create-project-modal').classList.remove('show');
}

/** input 即時驗證：有字且未超過上限才啟用「建立專案」按鈕 */
function onProjectNameInput() {
    const input = document.getElementById('create-project-name');
    const submitBtn = document.getElementById('create-project-submit-btn');
    if (!input || !submitBtn) return;
    const val = input.value.trim();
    const tooLong = input.value.length > PROJECT_NAME_MAX_CHARS;
    submitBtn.disabled = val.length === 0 || tooLong;
}

/**
 * 點擊「建立專案」— 呼叫後端 POST /api/project
 *
 * user_id 由後端從 JWT 解析，前端只需傳 name。
 * authFetch 自動帶上 Authorization: Bearer <AT>。
 */
async function submitCreateProject() {
    const nameInput = document.getElementById('create-project-name');
    const submitBtn = document.getElementById('create-project-submit-btn');
    const msg       = document.getElementById('create-project-msg');

    const name = nameInput.value.trim();
    if (!name) return;

    submitBtn.disabled = true;
    submitBtn.textContent = '建立中…';
    msg.className = 'modal-msg';
    msg.textContent = '';

    try {
        const res = await authFetch(`${state.apiBase}/project`, {
            method: 'POST',
            body: JSON.stringify({ name })   // user_id 由後端從 JWT 取得
        });

        if (!res) return;   // authFetch 已處理 401 → 跳轉 login

        const data = await res.json();

        if (!res.ok) {
            // 後端回傳 422（名稱非法）/ 401 / 403 / 500 等
            const detail = data.detail || '建立失敗，請稍後再試。';
            msg.textContent = detail;
            msg.className = 'modal-msg error';
            submitBtn.disabled = false;
            submitBtn.textContent = '建立專案';   // ← 復原按鈕文字
            return;
        }

        // ── 成功 ──
        // 按鈕文字先復原，再關閉 modal（視覺上更流暢）
        submitBtn.textContent = '建立專案';

        const newProject = {
            id: data.data.id,
            name: data.data.name,
            created_at: data.data.created_at,
        };
        // 若正在某則對話的 SSE／await fetch 中途去建立新專案，必須先 park，
        // 否則 currentChatId 被清掉後無法封存主視區，回該對話會 loadHistory 拆掉串流 DOM。
        const suspendChatId = state.currentChatId;
        if (suspendChatId) {
            maybeParkViewportForLeavingChat(suspendChatId);
        }

        state.currentProjectId = newProject.id;
        state.currentChatId    = null;

        closeCreateProjectModal();

        // 依使用者要求：重新呼叫 GET /api/project/all 並重新渲染左側
        await loadProjectsFromServer();

        // 進入剛建立的專案視圖（新專案沒有 chats / files，會顯示空態）
        const detail = await loadProjectDetail(newProject.id);
        showProjectView(newProject, { detail });
        renderProjects();
        renderRecentChats();
        lucide.createIcons();

    } catch (err) {
        msg.textContent = `網路錯誤：${err.message}`;
        msg.className = 'modal-msg error';
        submitBtn.disabled = false;
        submitBtn.textContent = '建立專案';   // ← 復原按鈕文字
    }
}

// ============================================================
// 主內容區：chat view ⇆ project view 切換
// ============================================================

/**
 * 顯示專案視圖。
 *
 * @param {{id:string,name:string}} project   專案基本資料
 * @param {{loading?:boolean, detail?:object|null}} options
 *   - loading=true       : 第一次點開、詳情還沒回來時，先顯示骨架（清空舊列表）
 *   - detail=<object>    : 已拿到後端回傳的 detail，渲染 chats / files
 *   - 兩者皆未提供時      : 退化為僅顯示 hero（向後相容）
 */
function showProjectView(project, options = {}) {
    const main = document.querySelector('.main-content');
    if (main) main.classList.add('project-view-mode');
    document.getElementById('chat-messages').style.display  = 'none';
    document.querySelector('.chat-input-area').style.display = '';

    const pv = document.getElementById('project-view');
    pv.style.display = 'flex';

    setMainChatTitle(project.name);

    document.getElementById('pv-project-name').textContent   = project.name;
    document.getElementById('pv-empty-subtitle').textContent = `${project.name} 中的聊天將顯示在此處`;
    const filesEmptySub = document.getElementById('pv-files-empty-subtitle');
    if (filesEmptySub) {
        filesEmptySub.textContent = `${project.name} 中的資料來源將顯示在此處`;
    }

    // 重置為「聊天」分頁
    setActivePvTab('chats');

    if (options.loading) {
        // 載入中：清掉舊列表並顯示跳動點，避免閃爍上一個專案的資料
        showPvListsLoading();
        lucide.createIcons();
        updateSendButtonForStreamingState();
        return;
    }

    if (options.detail) {
        renderPvChats(options.detail.chats || [], project.id);
        renderPvFiles(options.detail.files || [], project.id);
    } else {
        clearPvLists();
    }

    lucide.createIcons();
    updateSendButtonForStreamingState();
}

/** 清空聊天 / 資料來源列表（顯示空態） */
function clearPvLists() {
    const chatList  = document.getElementById('pv-chat-list');
    const fileList  = document.getElementById('pv-file-list');
    while (chatList.firstChild) chatList.removeChild(chatList.firstChild);
    while (fileList.firstChild) fileList.removeChild(fileList.firstChild);
    document.getElementById('pv-chats-empty').style.display = '';
    document.getElementById('pv-files-empty').style.display = '';
}

/**
 * 渲染專案視圖的聊天列表。
 * 每個 <li> 都帶有 id (`pv-chat-${chat.id}`) 與 dataset.chatId，
 * 點擊後 state.currentChatId 設為該 UUID 並切回 chat view。
 */
function renderPvChats(chats, projectId) {
    const list  = document.getElementById('pv-chat-list');
    const empty = document.getElementById('pv-chats-empty');

    while (list.firstChild) list.removeChild(list.firstChild);

    if (!chats || chats.length === 0) {
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    chats.forEach(c => {
        const li = document.createElement('li');
        li.className = 'pv-list-item';
        li.id = `pv-chat-${c.id}`;
        li.dataset.chatId = c.id;
        li.dataset.projectId = projectId;

        const main = document.createElement('div');
        main.className = 'pv-list-item-main';

        const titleEl = document.createElement('div');
        titleEl.className = 'pv-list-item-title';
        titleEl.textContent = c.title || '(未命名聊天)';

        main.appendChild(titleEl);
        li.appendChild(main);

        const menuBtn = document.createElement('button');
        menuBtn.type = 'button';
        menuBtn.className = 'project-row-menu-btn';
        menuBtn.setAttribute('aria-label', '聊天操作選單');
        menuBtn.dataset.chatId = c.id;
        const dotsIcon = document.createElement('i');
        dotsIcon.setAttribute('data-lucide', 'more-horizontal');
        menuBtn.appendChild(dotsIcon);

        menuBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            openChatMenu(c, menuBtn, { projectId });
        });

        li.appendChild(menuBtn);

        li.addEventListener('click', async () => {
            await navigateToChat(c.id, { projectId });
        });

        list.appendChild(li);
    });

    lucide.createIcons();
}

/**
 * 渲染專案視圖的資料來源列表。
 * 每個 <li> 都帶有 id (`pv-file-${file.id}`) 與 dataset.fileId，
 * 顯示 file_name / file_type / status / created_at。
 */
function renderPvFiles(files, projectId) {
    const list  = document.getElementById('pv-file-list');
    const empty = document.getElementById('pv-files-empty');

    while (list.firstChild) list.removeChild(list.firstChild);

    if (!files || files.length === 0) {
        empty.style.display = '';
        return;
    }
    empty.style.display = 'none';

    files.forEach(f => {
        const li = document.createElement('li');
        li.className = 'pv-list-item';
        li.id = `pv-file-${f.id}`;
        li.dataset.fileId = f.id;
        li.dataset.projectId = projectId;

        // 主體：檔名 + 類型
        const main = document.createElement('div');
        main.className = 'pv-list-item-main';

        const titleEl = document.createElement('div');
        titleEl.className = 'pv-list-item-title';
        titleEl.textContent = f.file_name || '(未命名檔案)';

        const subEl = document.createElement('div');
        subEl.className = 'pv-list-item-sub';
        subEl.textContent = f.file_type || '';

        main.appendChild(titleEl);
        main.appendChild(subEl);

        // 右側：狀態 pill + 建立時間
        const meta = document.createElement('div');
        meta.className = 'pv-list-item-meta';

        if (f.status) {
            const statusEl = document.createElement('span');
            statusEl.className = `pv-file-status ${f.status}`;
            statusEl.textContent = f.status;
            meta.appendChild(statusEl);
        }

        if (f.created_at) {
            const dateEl = document.createElement('span');
            const d = new Date(f.created_at);
            dateEl.textContent = isNaN(d.getTime())
                ? f.created_at
                : d.toLocaleDateString('zh-TW', { month: 'numeric', day: 'numeric' }) + '日';
            meta.appendChild(dateEl);
        }

        li.appendChild(main);
        li.appendChild(meta);

        li.addEventListener('click', () => {
            // 後續可導向「檔案詳情」頁；目前先記錄並 console
            console.log('Open file detail:', f.id);
        });

        list.appendChild(li);
    });
}

/**
 * 初始化專案視圖的分頁切換（聊天 ⇆ 資料來源）。
 * 只需註冊一次（在 DOMContentLoaded）。
 */
function initProjectViewTabs() {
    document.querySelectorAll('.pv-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            const tab = btn.dataset.tab;   // 'chats' | 'files'
            if (!tab) return;
            setActivePvTab(tab);
        });
    });
}

/** 切換目前作用中的 pv tab */
function setActivePvTab(tab) {
    document.querySelectorAll('.pv-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    const chatsPanel = document.getElementById('pv-chats-panel');
    const filesPanel = document.getElementById('pv-files-panel');
    if (chatsPanel) chatsPanel.classList.toggle('hidden', tab !== 'chats');
    if (filesPanel) filesPanel.classList.toggle('hidden', tab !== 'files');
}

/** 切回聊天視圖 */
function showChatView() {
    document.getElementById('project-view').style.display   = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.getElementById('chat-messages').style.display  = '';
    document.querySelector('.chat-input-area').style.display = '';
}

function clearChatMessages() {
    const container = document.getElementById('chat-messages');
    if (!container) return;
    while (container.firstChild) container.removeChild(container.firstChild);
    renderWelcomeHero('準備好開始新對話了嗎？');
}

/**
 * 從後端載入指定 chat 的訊息歷史並渲染主視窗（對應 GET /api/chat）。
 * API 回傳已按時間舊→新排序，這裡依序繪成一問一答。
 */
async function loadChatHistoryIntoView(chatId) {
    const container = document.getElementById('chat-messages');

    try {
        const url =
            `${state.apiBase}/chat?chat_id=${encodeURIComponent(chatId)}`;
        const res = await authFetch(url);
        if (!res) return;
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try {
                const err = await res.json();
                detail = err.detail || detail;
            } catch { /* ignore */ }
            showToast(`載入對話紀錄失敗：${detail}`, 'error');
            if (String(state.currentChatId) === String(chatId)) {
                clearChatMessages();
            }
            return;
        }
        const json = await res.json();
        const data = json && json.data ? json.data : {};
        const msgs = Array.isArray(data.messages) ? data.messages : [];

        // 請求過程中使用者若已切到別則對話，不可再覆寫目前主視窗（避免歷史互串）
        if (String(state.currentChatId) !== String(chatId)) return;

        while (container.firstChild) {
            container.removeChild(container.firstChild);
        }

        if (msgs.length === 0) {
            clearChatMessages();
            return;
        }

        msgs.forEach(m => {
            const role = m.role || '';
            if (role === 'user') {
                addMessageToUI('user', m.content || '', { skipScroll: true });
            } else if (role === 'assistant') {
                appendAssistantHistoryMessage(m);
            }
        });
        scrollToBottom();
        lucide.createIcons();
    } catch (err) {
        console.error(err);
        showToast(`載入對話紀錄失敗：${err.message}`, 'error');
        if (String(state.currentChatId) === String(chatId)) {
            clearChatMessages();
        }
    } finally {
        if (String(state.currentChatId) === String(chatId)) {
            setChatStatusLoading(false);
        }
        updateSendButtonForStreamingState();
    }
}

/**
 * 將一則後端 assistant 紀錄掛入主訊息列（Markdown + 軌跡/來源/複製列，與即時 SSE 結束態一致）。
 */
function appendAssistantHistoryMessage(record) {
    const container = document.getElementById('chat-messages');
    const msgDiv = document.createElement('div');
    // 須與串流 SSE 使用的 `message ai` 一致，否則 .ai .bubble 的 Markdown／列表樣式不會套用
    msgDiv.className = 'message ai';
    if (record.id) msgDiv.dataset.messageId = record.id;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    applyMarkdown(bubble, record.content || '');
    msgDiv.appendChild(bubble);

    const meta = record.metadata;
    const steps = meta && Array.isArray(meta.steps) ? meta.steps : [];
    const sources = Array.isArray(record.context_refs) ? record.context_refs : [];

    appendStepsAndSources(msgDiv, steps.length ? steps : null, sources.length ? sources : null);
    appendCopyBar(msgDiv, record.content || '', sources.length ? sources : []);

    container.appendChild(msgDiv);
}

// ============================================================
// 配額錯誤：結構化解析 + 中文顯示
// ============================================================

/**
 * @param {unknown} detail
 * @returns {{ used: number, limit: number } | null}
 */
function extractQuotaInfoFromDetail(detail) {
    if (detail && typeof detail === 'object') {
        if (detail.code === 'quota_exceeded') {
            const used = Number(detail.used_tokens);
            const limit = Number(detail.monthly_token_limit);
            if (Number.isFinite(used) && Number.isFinite(limit)) {
                return {
                    used,
                    limit,
                    quota_resets_at: detail.quota_resets_at || null,
                };
            }
        }
        // FastAPI 有時會包一層
        if (detail.detail) {
            return extractQuotaInfoFromDetail(detail.detail);
        }
    }
    if (typeof detail === 'string') {
        if (detail === 'HTTP 429') return null;
        const m = detail.match(/\((\d+)\s*\/\s*(\d+)/);
        if (m) {
            return { used: parseInt(m[1], 10), limit: parseInt(m[2], 10) };
        }
    }
    return null;
}

/** @returns {{ used: number, limit: number } | null} */
function getQuotaFromUserProfile() {
    const user = typeof getUser === 'function' ? getUser() : null;
    if (!user) return null;
    const used = Number(user.used_tokens);
    const limit = Number(user.monthly_token_limit);
    if (!Number.isFinite(used) || !Number.isFinite(limit) || limit <= 0) return null;
    return { used, limit, quota_resets_at: user.quota_resets_at || null };
}

/** 429 時依序：response detail → localStorage profile → 預設值 */
function resolveQuotaForHttp429(detail) {
    return extractQuotaInfoFromDetail(detail)
        || getQuotaFromUserProfile()
        || { used: 0, limit: 200_000, quota_resets_at: null };
}

/**
 * @param {Response} response
 * @returns {Promise<unknown>}
 */
async function parseApiErrorBody(response) {
    let raw = '';
    try {
        raw = await response.text();
    } catch (_) {
        return null;
    }
    if (!raw.trim()) return null;
    try {
        const body = JSON.parse(raw);
        if (body && typeof body === 'object' && 'detail' in body) {
            return body.detail;
        }
        return body;
    } catch (_) {
        return raw;
    }
}

/**
 * @param {number} used
 * @param {number} limit
 * @returns {string}
 */
function formatQuotaToastMessage(used, limit) {
    const usedFmt = used.toLocaleString('zh-TW');
    const limitFmt = limit.toLocaleString('zh-TW');
    return `本月 Token 配額已用盡（${usedFmt} / ${limitFmt}）`;
}

/** 配額 429 且為新 chat 時，從側欄 state 移除（後端亦會刪除空 chat） */
function removeChatFromSidebarState(chatId) {
    if (!chatId) return;
    state.recentChats = state.recentChats.filter(
        (c) => String(c.id) !== String(chatId)
    );
    for (const pid of Object.keys(state.chats || {})) {
        state.chats[pid] = (state.chats[pid] || []).filter(
            (c) => String(c.id) !== String(chatId)
        );
        if (isProjectViewVisible() && state.currentProjectId === pid) {
            renderPvChats(state.chats[pid] || [], pid);
        }
    }
    if (String(state.currentChatId) === String(chatId)) {
        state.currentChatId = null;
        setMainChatTitle('新對話');
    }
    renderProjects();
    renderRecentChats();
    lucide.createIcons();
}

/**
 * @param {HTMLElement} bubble
 * @param {number} used
 * @param {number} limit
 * @param {string|null|undefined} quotaResetsAt
 */
function renderQuotaExceededInBubble(bubble, used, limit, quotaResetsAt) {
    while (bubble.firstChild) bubble.removeChild(bubble.firstChild);

    const wrap = document.createElement('div');
    wrap.className = 'quota-error-card';

    const title = document.createElement('p');
    title.className = 'quota-error-title';
    title.textContent = '本月 Token 配額已用盡';

    const barWrap = document.createElement('div');
    barWrap.className = 'quota-error-bar-wrap';
    const bar = document.createElement('div');
    bar.className = 'quota-error-bar';
    const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 100;
    bar.style.width = `${pct}%`;
    barWrap.appendChild(bar);

    const stats = document.createElement('p');
    stats.className = 'quota-error-stats';
    stats.textContent =
        `已使用 ${used.toLocaleString('zh-TW')} / ${limit.toLocaleString('zh-TW')} tokens` +
        `（${pct}%）`;

    const hint = document.createElement('p');
    hint.className = 'quota-error-hint';
    hint.textContent =
        '升級 Pro 或 Ultra 方案可獲得更高上限，或等待下個計費週期重置。';

    wrap.appendChild(title);
    wrap.appendChild(barWrap);
    wrap.appendChild(stats);
    wrap.appendChild(hint);
    const resetLabel =
        typeof formatQuotaResetLabel === 'function'
            ? formatQuotaResetLabel(quotaResetsAt)
            : '';
    if (resetLabel) {
        const resetEl = document.createElement('p');
        resetEl.className = 'quota-error-hint';
        resetEl.textContent = resetLabel;
        wrap.appendChild(resetEl);
    }
    bubble.appendChild(wrap);
}

/** 在對話區追加配額用盡 AI 回覆（僅畫面，不寫 DB） */
function appendQuotaExceededAssistantMessage(quota) {
    const container = document.getElementById('chat-messages');
    if (!container || !quota) return null;
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = 'message ai';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    msgDiv.appendChild(bubble);
    container.appendChild(msgDiv);
    renderQuotaExceededInBubble(
        bubble, quota.used, quota.limit, quota.quota_resets_at
    );
    scrollToBottom();
    return msgDiv;
}

/** 從後端同步用量（供 429 後更新 localStorage，不阻擋送出） */
async function refreshUserQuotaFromServer() {
    if (typeof authFetch !== 'function' || typeof AUTH_API === 'undefined') return;
    try {
        const res = await authFetch(`${AUTH_API}/user`);
        if (!res || !res.ok) return;
        const profile = await res.json();
        localStorage.setItem('user', JSON.stringify(profile));
        if (typeof applyUserTierBadge === 'function') applyUserTierBadge(profile);
        updateSendButtonForStreamingState();
    } catch (err) {
        console.error('Failed to refresh user quota:', err);
    }
}

/**
 * @param {number} httpStatus
 * @param {unknown} detail
 * @returns {{ isQuota: boolean, text: string, quota: { used: number, limit: number } | null }}
 */
function mapChatMessagesErrorForDisplay(httpStatus, detail) {
    // 429 一律視為配額用盡，絕不顯示裸 "HTTP 429"
    if (httpStatus === 429) {
        const quota = resolveQuotaForHttp429(detail);
        return {
            isQuota: true,
            text: formatQuotaToastMessage(quota.used, quota.limit),
            quota,
        };
    }
    const text =
        typeof detail === 'string' && detail !== `HTTP ${httpStatus}`
            ? detail
            : '請求失敗，請稍後再試';
    return { isQuota: false, text, quota: null };
}

// ============================================================
// 核心：支援 SSE 串流的 sendMessage
// ============================================================

async function sendMessage() {
    const curSid = state.currentChatId;
    if (curSid && streamingChatIds.has(curSid)) {
        showToast('此對話正在生成回覆，請稍候…', 'info');
        return;
    }
    if (!curSid && newChatComposeLock) return;

    const sendBtn = document.getElementById('send-btn');
    const inputEl = document.getElementById('user-input');

    sendBtn.disabled = true;
    inputEl.disabled = true;
    inputEl.placeholder = '等待回覆中...';

    const query = inputEl.value.trim();
    if (!query) {
        sendBtn.disabled = false;
        inputEl.disabled = false;
        updateSendButtonForStreamingState();
        return;
    }
    if (inputEl.value.length > CHAT_QUERY_MAX_CHARS) {
        showToast(`訊息不可超過 ${CHAT_QUERY_MAX_CHARS} 字`, 'error');
        updateSendButtonForStreamingState();
        return;
    }

    if (isProjectViewVisible()) {
        showChatView();
        clearChatMessages();
        setMainChatTitle('新對話');
    }

    // 取得工具設定
    let enabled_tools = [];
    const toolAuto = document.getElementById('tool-auto');
    const isAuto = toolAuto ? toolAuto.checked : true;
    if (!isAuto) {
        document.querySelectorAll('.tool-check:checked').forEach(cb => {
            enabled_tools.push(cb.value);
        });
    }

    // 顯示使用者訊息（先記錄送出前是否已有歷史，供 429 判斷是否清除側欄孤兒 chat）
    const chatHadPriorMessages = !!document.querySelector('#chat-messages .message');
    const userMsgEl = addMessageToUI('user', query);
    inputEl.value = '';
    inputEl.style.height = 'auto';

    const statusBadge = document.getElementById('chat-status');
    statusBadge.textContent = 'Analyzing...';

    let wasNewChatThisSend = false;

    // ── 若還沒有 chat_id，先打 POST /api/chat 建立 chat ──
    // 後端會回傳 placeholder title（截斷 query），LLM 正式 title 在
    // 後續 /api/chat/messages 並行產生，透過 SSE 'title_update' 回推
    if (!state.currentChatId) {
        newChatComposeLock = true;
        try {
            const createRes = await authFetch(`${state.apiBase}/chat`, {
                method: 'POST',
                body: JSON.stringify({
                    query,
                    project_id: state.currentProjectId || null,
                }),
            });
            if (!createRes) {
                newChatComposeLock = false;
                sendBtn.disabled = false;
                inputEl.disabled = false;
                updateSendButtonForStreamingState();
                return;
            }
            if (!createRes.ok) {
                if (createRes.status === 429) {
                    await refreshUserQuotaFromServer();
                }
                const detail = await parseApiErrorBody(createRes);
                const mapped = mapChatMessagesErrorForDisplay(createRes.status, detail);
                showToast(
                    mapped.isQuota ? mapped.text : `建立聊天失敗：${mapped.text}`,
                    'error'
                );
                if (createRes.status === 429 && mapped.quota) {
                    appendQuotaExceededAssistantMessage(mapped.quota);
                    state.currentChatId = null;
                }
                statusBadge.textContent = 'Ready';
                newChatComposeLock = false;
                sendBtn.disabled = false;
                inputEl.disabled = false;
                updateSendButtonForStreamingState();
                return;
            }
            const createJson = await createRes.json();
            const newChat = createJson.data;
            state.currentChatId = newChat.id;
            wasNewChatThisSend = true;

            // 寫入 state.chats 對應 project（若有）
            const pid = state.currentProjectId;
            if (pid) {
                if (!state.chats[pid]) state.chats[pid] = [];
                state.chats[pid].unshift({
                    id: newChat.id,
                    title: newChat.title,
                });
            } else {
                // 僅非專案 chat 出現在「最近」
                state.recentChats.unshift({
                    id: newChat.id,
                    title: newChat.title,
                    created_at: newChat.created_at,
                });
            }

            renderProjects();
            renderRecentChats();
            lucide.createIcons();

            setMainChatTitle(newChat.title || '新對話');
            if (pid) renderPvChats(state.chats[pid] || [], pid);
        } catch (err) {
            showToast(`網路錯誤：${err.message}`, 'error');
            statusBadge.textContent = 'Ready';
            newChatComposeLock = false;
            sendBtn.disabled = false;
            inputEl.disabled = false;
            updateSendButtonForStreamingState();
            return;
        }
    }

    // 建立 AI 訊息容器（先放到畫面上，後續逐步填入）
    // 依模式決定等待提示文字：一般對話用「思考中...」，股市 Agent 用「正在分析問題...」
    const initPlaceholderText = chatMode === 'general' ? '思考中...' : '正在分析問題...';
    const { msgDiv, toolsContainer, bubble, streamCursor, initialPlaceholder } = createStreamingMessageUI(initPlaceholderText);
    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();
    container.appendChild(msgDiv);
    
    // 這次 SSE 對應的 chat（發送完成後若在別的對話視景，不要被 done 又把 state.currentChatId 搶回去）
    const streamTargetChatId = state.currentChatId;
    scrollToBottom(streamTargetChatId, true);

    // 暫存串流文字、工具行清單、思考計時器
    let rawStreamText = '';
    let donePayload = null;
    let bubbleAdded = false;
    const toolRows = [];       // { toolName, element, status }
    let thinkingTimer = null;
    let thinkingRow = null;

    // 確保 bubble 已掛到 DOM（第一個 token 到來時才加入，避免空白灰框）
    function addBubbleIfNeeded() {
        if (!bubbleAdded) {
            bubbleAdded = true;
            msgDiv.appendChild(bubble);
        }
    }

    // 在 toolsContainer 底部顯示「思考中....」波浪動畫
    function showThinkingRow() {
        if (thinkingRow) return;
        thinkingRow = document.createElement('div');
        thinkingRow.className = 'thinking-wave-row';
        const text = document.createElement('span');
        text.textContent = '思考中';
        const dots = document.createElement('span');
        dots.className = 'thinking-dots';
        for (let i = 0; i < 3; i++) {
            const dot = document.createElement('span');
            dot.textContent = '.';
            dots.appendChild(dot);
        }
        thinkingRow.appendChild(text);
        thinkingRow.appendChild(dots);
        toolsContainer.appendChild(thinkingRow);
        scrollToBottom(streamTargetChatId);
    }

    // 清除思考中指示
    function hideThinkingRow() {
        if (thinkingTimer) { clearTimeout(thinkingTimer); thinkingTimer = null; }
        if (thinkingRow && thinkingRow.parentNode) { thinkingRow.remove(); }
        thinkingRow = null;
    }

    const cleanup = () => {
        streamCursor.remove();
        hideThinkingRow();
        if (toolsContainer.parentNode) toolsContainer.remove();
    };

    const streamAbortCtrl = new AbortController();
    streamAbortByChatId.set(streamTargetChatId, streamAbortCtrl);
    // fetch 一回應前就允許離開對話並 park，否則 loadHistory 會拆掉尚未加入 Set 的串流 DOM。
    streamingChatIds.add(streamTargetChatId);
    updateSendButtonForStreamingState();

    try {
        // authFetch 自動注入 Authorization: Bearer AT，
        // 並在 AT 即將過期（≤ 90s）時先靜默換 Token 再送請求（機制 B）。
        // AT 已過期且換 Token 失敗時回傳 undefined 並導向登入頁，
        // 此時直接 return 以結束函式，finally 區塊仍會負責解鎖 UI。
        const response = await authFetch(`${state.apiBase}/chat/messages`, {
            method: 'POST',
            signal: streamAbortCtrl.signal,
            body: JSON.stringify({
                query,
                chat_id: streamTargetChatId,   // 與發送瞬間鎖定，勿用 state.currentChatId（使用者可能 await 時已換對話）
                agent_config: { enabled_tools },
                chat_mode: chatMode,
                response_mode: chatResponseMode === 'flash' ? 'flash' : 'thinking',
            })
        });

        if (!response) return;  // authFetch 已處理 401 → 跳轉 login.html
        if (!response.ok) {
            if (response.status === 429) {
                await refreshUserQuotaFromServer();
            }
            const detail = await parseApiErrorBody(response);
            const mapped = mapChatMessagesErrorForDisplay(response.status, detail);
            if (response.status === 429) {
                refreshUserQuotaFromServer();
            }
            const err = new Error(mapped.text);
            err.httpStatus = response.status;
            if (mapped.isQuota && mapped.quota) {
                err.isQuota = true;
                err.quota = mapped.quota;
            }
            throw err;
        }
        if (!response.body) throw new Error('瀏覽器不支援 Streaming');

        newChatComposeLock = false;
        updateSendButtonForStreamingState();

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const parts = buffer.split('\n\n');
            buffer = parts.pop();

            for (const part of parts) {
                const eventMatch = part.match(/^event:\s*(\w+)/m);
                const dataMatch  = part.match(/^data:\s*(.+)/ms);
                if (!eventMatch || !dataMatch) continue;

                const eventType = eventMatch[1];
                let payload;
                try { payload = JSON.parse(dataMatch[1]); } catch { continue; }

                switch (eventType) {

                    // Router 思考中（更新初始佔位文字）
                    case 'thinking': {
                        const txt = initialPlaceholder.querySelector('span:last-child');
                        if (txt) txt.textContent = payload.text || '思考中...';
                        break;
                    }

                    // 工具開始調用 → 移除初始佔位、新增一行 tool row
                    case 'tool_start': {
                        if (initialPlaceholder.parentNode) initialPlaceholder.remove();
                        hideThinkingRow();

                        const row = document.createElement('div');
                        row.className = 'tool-status';

                        const iconSpan = document.createElement('span');
                        iconSpan.className = 'tool-status-icon spinning';
                        iconSpan.appendChild(makeSvgSpinner(12));

                        const label = document.createElement('span');
                        label.className = 'tool-label';
                        label.textContent = formatToolName(payload.tool);

                        const suffix = document.createElement('span');
                        suffix.textContent = '...';

                        row.appendChild(iconSpan);
                        row.appendChild(label);
                        row.appendChild(suffix);
                        toolsContainer.appendChild(row);
                        toolRows.push({ toolName: payload.tool, element: row, status: 'running' });
                        scrollToBottom(streamTargetChatId);
                        break;
                    }

                    // 工具完成 → 更新對應行為勾選狀態，0.5s 後若無 token 則顯示思考中
                    case 'tool_done': {
                        const entry = toolRows.find(r => r.toolName === payload.tool && r.status === 'running');
                        if (entry) {
                            entry.status = 'done';
                            const row = entry.element;
                            row.classList.add('done');
                            while (row.firstChild) row.removeChild(row.firstChild);

                            const iconSpan = document.createElement('span');
                            iconSpan.className = 'tool-status-icon';
                            iconSpan.appendChild(makeSvgCheck(12));

                            const label = document.createElement('span');
                            label.className = 'tool-label';
                            label.textContent = formatToolName(payload.tool);

                            const suffix = document.createElement('span');
                            suffix.textContent = ' ✓';

                            row.appendChild(iconSpan);
                            row.appendChild(label);
                            row.appendChild(suffix);
                        }

                        // 0.5s 後若後端還未開始串流則顯示「思考中...」
                        if (thinkingTimer) clearTimeout(thinkingTimer);
                        thinkingTimer = setTimeout(() => {
                            thinkingTimer = null;
                            showThinkingRow();
                        }, 500);
                        break;
                    }

                    // LLM 逐字 token（僅 analyst 節點）
                    case 'token': {
                        // 第一個 token 進來時移除初始等待提示（一般對話不經過 tool_start，需在此清除）
                        if (initialPlaceholder.parentNode) initialPlaceholder.remove();
                        hideThinkingRow();
                        addBubbleIfNeeded();
                        rawStreamText += payload.text || '';
                        const streamHtml = window.DOMPurify
                            ? DOMPurify.sanitize(renderMarkdown(rawStreamText), {
                                ADD_ATTR: ['target', 'rel', 'data-action'],
                                FORCE_BODY: true,
                              })
                            : renderMarkdown(rawStreamText);
                        bubble.innerHTML = streamHtml;
                        bubble.appendChild(streamCursor);
                        scrollToBottom(streamTargetChatId);
                        break;
                    }

                    // 全部完成
                    case 'done': {
                        donePayload = payload;
                        // 使用者若已切到別個 chat，不要覆寫 currentChatId（避免狀態與畫面錯位）
                        if (state.currentChatId === streamTargetChatId) {
                            state.currentChatId = payload.chat_id;
                        }
                        addBubbleIfNeeded();
                        cleanup();
                        const finalText = payload.final_content || rawStreamText;
                        applyMarkdown(bubble, finalText);
                        appendStepsAndSources(msgDiv, payload.steps, payload.retrieval_sources);
                        appendCopyBar(msgDiv, finalText, payload.retrieval_sources);
                        lucide.createIcons();
                        scrollToBottom(streamTargetChatId);
                        refreshUserQuotaFromServer();
                        break;
                    }

                    // ── LLM 產出正式 title（僅第一次訊息會收到）──
                    // 後端只會在 title_generated=FALSE 時 spawn task，
                    // 之後一律不再送，所以這裡無需再做去重。
                    case 'title_update': {
                        const cid = payload.chat_id;
                        const newTitle = payload.title;
                        if (!cid || !newTitle) break;

                        // 1. 更新 state.chats 各 project 中對應的 chat
                        for (const pid of Object.keys(state.chats)) {
                            const found = (state.chats[pid] || []).find(c => String(c.id) === String(cid));
                            if (found) {
                                found.title = newTitle;
                                break;
                            }
                        }

                        // 2. 更新 state.recentChats 對應的 chat
                        const recent = state.recentChats.find(c => String(c.id) === String(cid));
                        if (recent) recent.title = newTitle;

                        // 3. 若正在檢視該對話，同步頂標；若在專案頁且有該專案列表，順便刷新列表標題
                        if (String(state.currentChatId) === String(cid)) {
                            setMainChatTitle(newTitle);
                        }
                        for (const pid of Object.keys(state.chats || {})) {
                            const list = state.chats[pid] || [];
                            if (!list.some(c => String(c.id) === String(cid))) continue;
                            if (isProjectViewVisible() && state.currentProjectId === pid) {
                                renderPvChats(list, pid);
                            }
                            break;
                        }

                        renderProjects();
                        renderRecentChats();
                        lucide.createIcons();
                        break;
                    }

                    case 'error': {
                        hideThinkingRow();
                        addBubbleIfNeeded();
                        cleanup();
                        const quota = extractQuotaInfoFromDetail(
                            payload.quota || payload.message || payload
                        );
                        if (quota) {
                            renderQuotaExceededInBubble(
                                bubble, quota.used, quota.limit, quota.quota_resets_at
                            );
                            showToast(formatQuotaToastMessage(quota.used, quota.limit), 'error');
                            refreshUserQuotaFromServer();
                        } else {
                            bubble.textContent = `錯誤：${payload.message || '未知錯誤'}`;
                        }
                        break;
                    }
                }
            }
        }

    } catch (err) {
        console.error('Streaming error:', err);
        if ((err && err.isQuota && err.quota) || err?.httpStatus === 429) {
            const quota = err.quota || resolveQuotaForHttp429(null);
            showToast(formatQuotaToastMessage(quota.used, quota.limit), 'error');
            await refreshUserQuotaFromServer();
            addBubbleIfNeeded();
            renderQuotaExceededInBubble(
                bubble, quota.used, quota.limit, quota.quota_resets_at
            );
            // 新 chat / 空 chat：只清側欄孤兒，對話框保留使用者訊息 + 配額回覆
            if (wasNewChatThisSend || !chatHadPriorMessages) {
                removeChatFromSidebarState(streamTargetChatId);
            }
        } else {
            addBubbleIfNeeded();
            const msg =
                err && typeof err.message === 'string' && err.message.length > 0
                    ? err.message
                    : '伺服器連線失敗，請檢查 Docker 是否啟動。';
            bubble.textContent = msg;
            showToast(msg, 'error');
        }
    } finally {
        try {
            cleanup();
        } catch (_) { /* 串流節點可能已不在文件樹 */ }
        streamingChatIds.delete(streamTargetChatId);
        newChatComposeLock = false;
        streamAbortByChatId.delete(streamTargetChatId);

        sendBtn.disabled = false;
        inputEl.disabled = false;
        statusBadge.textContent = 'Ready';
        lucide.createIcons();
        updateSendButtonForStreamingState();
    }
}

// ============================================================
// SVG 圖示輔助函式
// ============================================================

function makeSvgSpinner(size) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', 'M21 12a9 9 0 11-6.219-8.56');
    svg.appendChild(path);
    return svg;
}

function makeSvgCheck(size) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', size);
    svg.setAttribute('height', size);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', '#00d68f');
    svg.setAttribute('stroke-width', '2.5');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const poly = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    poly.setAttribute('points', '20 6 9 17 4 12');
    svg.appendChild(poly);
    return svg;
}

// ============================================================
// 建立串流訊息 UI 骨架
// ============================================================

function createStreamingMessageUI(placeholderText) {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message ai';

    // 工具列容器（多個 tool row 垂直堆疊）
    const toolsContainer = document.createElement('div');
    toolsContainer.className = 'tools-container';

    // 初始佔位列（等待 LLM 回應時的轉圈提示）
    const initialPlaceholder = document.createElement('div');
    initialPlaceholder.className = 'tool-status';
    const initIconWrap = document.createElement('span');
    initIconWrap.className = 'tool-status-icon spinning';
    initIconWrap.appendChild(makeSvgSpinner(14));
    const initText = document.createElement('span');
    initText.textContent = placeholderText || '正在分析問題...';
    initialPlaceholder.appendChild(initIconWrap);
    initialPlaceholder.appendChild(initText);
    toolsContainer.appendChild(initialPlaceholder);

    msgDiv.appendChild(toolsContainer);

    // 氣泡（先不加入 DOM，等第一個 token 進來才掛上去，避免空白灰框）
    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    // 游標（串流時顯示）
    const streamCursor = document.createElement('span');
    streamCursor.className = 'stream-cursor';

    return { msgDiv, toolsContainer, bubble, streamCursor, initialPlaceholder };
}

// ============================================================
// 工具名稱對照表
// ============================================================

const TOOL_DISPLAY_NAMES = {
    search_stock_news:          '搜尋股市新聞',
    search_market_ai_analysis:  '搜尋 AI 市場分析',
    get_market_recommendations: '提取潛力標的',
    tavily_global_search:         '網路搜尋',
};

function formatToolName(tool) {
    return TOOL_DISPLAY_NAMES[tool] || '資料檢索';
}

/** 將 Router thought 等文字中的原始 tool id 替換為使用者可讀名稱 */
function maskToolNamesInText(text) {
    if (!text) return '';
    let result = text;
    const entries = Object.entries(TOOL_DISPLAY_NAMES).sort((a, b) => b[0].length - a[0].length);
    for (const [key, label] of entries) {
        result = result.split(key).join(label);
    }
    return result;
}

// ============================================================
// 最終附加 ReAct Trace 與來源（沿用原有邏輯，保持不變）
// ============================================================

function appendStepsAndSources(msgDiv, steps, sources) {
    // --- ReAct Trace ---
    if (steps && steps.length > 0) {
        const stepsContainer = document.createElement('div');
        stepsContainer.className = 'steps-container';

        const header = document.createElement('div');
        header.className = 'step-header';
        const headerLeft = document.createElement('span');
        const cpuIcon = document.createElement('i');
        cpuIcon.setAttribute('data-lucide', 'cpu');
        cpuIcon.setAttribute('size', '14');
        headerLeft.appendChild(cpuIcon);
        headerLeft.appendChild(document.createTextNode(' 執行軌跡 (ReAct Trace)'));
        const chevronIcon = document.createElement('i');
        chevronIcon.setAttribute('data-lucide', 'chevron-down');
        chevronIcon.setAttribute('size', '14');
        header.appendChild(headerLeft);
        header.appendChild(chevronIcon);

        const body = document.createElement('div');
        body.className = 'step-body';
        body.style.display = 'none';

        steps.forEach((s, idx) => {
            const stepDiv = document.createElement('div');
            stepDiv.className = 'step-item';

            const nodeLabel = s.node === 'router' ? 'Router 決策' : 'Analyst 撰寫';

            const meta = document.createElement('div');
            meta.className = 'step-meta';

            const nodeSpan = document.createElement('span');
            nodeSpan.className = 'step-node';
            nodeSpan.textContent = `#${idx + 1} ${nodeLabel}`;

            const timeSpan = document.createElement('span');
            timeSpan.className = 'step-time';
            const clockIcon = document.createElement('i');
            clockIcon.setAttribute('data-lucide', 'clock');
            clockIcon.setAttribute('size', '10');
            timeSpan.appendChild(clockIcon);
            timeSpan.appendChild(document.createTextNode(` ${s.execution_time}s`));

            meta.appendChild(nodeSpan);
            meta.appendChild(timeSpan);

            stepDiv.appendChild(meta);

            // Analyst 正文已在上方氣泡串流顯示；軌跡只保留標題與耗時
            const thoughtText = s.thought || (s.node !== 'analyst' ? s.content : '') || '';
            if (thoughtText) {
                const thought = document.createElement('div');
                thought.className = 'step-thought';
                thought.textContent = maskToolNamesInText(thoughtText);
                stepDiv.appendChild(thought);
            }

            if (s.tool_calls && s.tool_calls.length > 0) {
                const callsWrap = document.createElement('div');
                callsWrap.className = 'step-tool-calls';
                s.tool_calls.forEach(tc => {
                    const card = document.createElement('div');
                    card.className = 'tool-call-card';

                    const nameEl = document.createElement('div');
                    nameEl.className = 'tool-name';
                    nameEl.textContent = `調用工具: ${formatToolName(tc.name)}`;

                    const queryEl = document.createElement('div');
                    queryEl.className = 'tool-query';
                    const strongEl = document.createElement('strong');
                    strongEl.textContent = tc.query || '';
                    queryEl.appendChild(document.createTextNode('搜尋詞: '));
                    queryEl.appendChild(strongEl);

                    const datesEl = document.createElement('div');
                    datesEl.className = 'tool-dates';
                    datesEl.textContent = `區間: ${tc.start_date || 'N/A'} ~ ${tc.end_date || 'N/A'}`;

                    card.appendChild(nameEl);
                    card.appendChild(queryEl);
                    card.appendChild(datesEl);
                    callsWrap.appendChild(card);
                });
                stepDiv.appendChild(callsWrap);
            }

            body.appendChild(stepDiv);
        });

        header.onclick = () => {
            body.style.display = body.style.display === 'none' ? 'block' : 'none';
        };

        stepsContainer.appendChild(header);
        stepsContainer.appendChild(body);
        msgDiv.appendChild(stepsContainer);
    }

    // --- 參考來源 ---
    if (sources && sources.length > 0) {
        const sourcesSection = document.createElement('div');
        sourcesSection.className = 'sources-container';

        const header = document.createElement('div');
        header.className = 'sources-header';

        const headerLeft = document.createElement('span');
        const libIcon = document.createElement('i');
        libIcon.setAttribute('data-lucide', 'library');
        libIcon.setAttribute('size', '14');
        headerLeft.appendChild(libIcon);
        headerLeft.appendChild(document.createTextNode(` 參考來源 (${sources.length})`));
        const chevronIcon = document.createElement('i');
        chevronIcon.setAttribute('data-lucide', 'chevron-down');
        chevronIcon.setAttribute('size', '14');
        header.appendChild(headerLeft);
        header.appendChild(chevronIcon);

        const body = document.createElement('div');
        body.className = 'sources-body';
        body.style.display = 'none';

        sources.forEach(src => {
            const item = document.createElement('div');
            item.className = 'source-list-item';

            const info = document.createElement('div');
            info.className = 'source-info';

            const tag = document.createElement('span');
            tag.className = 'source-tag';
            tag.textContent = (src.tool || '').toUpperCase();

            const title = document.createElement('span');
            title.className = 'source-title';
            title.textContent = src.title || '';

            const date = document.createElement('span');
            date.className = 'source-date';
            date.textContent = src.publishAt
                ? new Date(src.publishAt).toLocaleDateString() : '';

            info.appendChild(tag);
            info.appendChild(title);
            info.appendChild(date);
            item.appendChild(info);

            if (src.url) {
                const link = document.createElement('a');
                link.href = src.url;
                link.target = '_blank';
                link.className = 'source-action-link';
                const extIcon = document.createElement('i');
                extIcon.setAttribute('data-lucide', 'external-link');
                extIcon.setAttribute('size', '14');
                link.appendChild(extIcon);
                item.appendChild(link);
            }

            body.appendChild(item);
        });

        header.onclick = () => {
            body.style.display = body.style.display === 'none' ? 'block' : 'none';
        };

        sourcesSection.appendChild(header);
        sourcesSection.appendChild(body);
        msgDiv.appendChild(sourcesSection);
    }
}

// ============================================================
// 使用者訊息 UI（不變）
// ============================================================

function addMessageToUI(role, content, options) {
    const opt = options || {};
    const skipScroll = !!opt.skipScroll;

    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = content;
    msgDiv.appendChild(bubble);

    if (role === 'user') {
        appendCopyBar(msgDiv, content, null, { variant: 'user' });
    }

    container.appendChild(msgDiv);
    if (!skipScroll) scrollToBottom();
    if (role === 'user' && typeof lucide !== 'undefined') lucide.createIcons();
    return msgDiv;
}

let isUserScrolledUp = false;

document.addEventListener('DOMContentLoaded', () => {
    const container = document.getElementById('chat-messages');
    if (container) {
        container.addEventListener('scroll', () => {
            const distanceToBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
            // 如果距離底部大於 50px，代表使用者往上滑了
            isUserScrolledUp = distanceToBottom > 50;
        });
    }
});

function scrollToBottom(targetChatId = null, force = false) {
    const container = document.getElementById('chat-messages');
    if (!container) return;

    // 1. 若目前畫面已經不在該對話，不自動滾動（防止切換對話後被拉走）
    if (targetChatId !== null && state.currentChatId !== targetChatId) return;

    // 2. 若使用者往上看歷史訊息，暫停自動滾動（除非強制滾動）
    if (!force && isUserScrolledUp) return;

    container.scrollTop = container.scrollHeight;
    isUserScrolledUp = false; // 重置
}

// ============================================================
// 複製按鈕列（回答完成後附加在氣泡下方）
// ============================================================

/**
 * 在訊息底部附加複製按鈕列。
 * @param {HTMLElement} msgDiv   訊息容器
 * @param {string}      rawText  原始文字（AI 為 Markdown；user 為純文字）
 * @param {Array|null}  sources  參考來源（僅 AI）
 * @param {{ variant?: 'user'|'ai' }} [options]
 */
function appendCopyBar(msgDiv, rawText, sources, options) {
    const opt = options || {};
    const isUser = opt.variant === 'user';

    const bar = document.createElement('div');
    bar.className = 'copy-bar';

    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.title = isUser ? '複製訊息' : '複製回答';

    const iconCopy = document.createElement('i');
    iconCopy.setAttribute('data-lucide', 'copy');
    iconCopy.setAttribute('size', '13');

    const label = document.createElement('span');
    label.textContent = '複製';

    btn.appendChild(iconCopy);
    btn.appendChild(label);
    bar.appendChild(btn);
    msgDiv.appendChild(bar);

    btn.addEventListener('click', () => {
        const plainText = isUser
            ? (rawText || '').trim()
            : rawText
                .replace(/#{1,6}\s+/g, '')
                .replace(/\*\*(.+?)\*\*/g, '$1')
                .replace(/\*(.+?)\*/g, '$1')
                .replace(/`{1,3}[^`]*`{1,3}/g, '')
                .replace(/\[(.+?)\]\(.+?\)/g, '$1')
                .trim();

        // 參考來源：僅 AI 回答附加
        let sourcesText = '';
        if (!isUser && sources && sources.length > 0) {
            const lines = sources.map((src, idx) => {
                const num    = idx + 1;
                const title  = src.title  || '(無標題)';
                const date   = src.publishAt
                    ? new Date(src.publishAt).toLocaleDateString() : '';
                const url    = src.url    || '';
                const parts  = [`${num}. ${title}`];
                if (date) parts.push(`   日期：${date}`);
                if (url)  parts.push(`   連結：${url}`);
                return parts.join('\n');
            });
            sourcesText = '\n\n---\n參考來源\n' + lines.join('\n\n');
        }

        const fullText = plainText + sourcesText;

        const copyToClipboard = (text) => {
            if (navigator.clipboard && window.isSecureContext) {
                return navigator.clipboard.writeText(text);
            } else {
                return new Promise((resolve, reject) => {
                    try {
                        const textArea = document.createElement("textarea");
                        textArea.value = text;
                        textArea.style.position = "fixed"; // 避免畫面捲動
                        textArea.style.left = "-999999px";
                        textArea.style.top = "-999999px";
                        document.body.appendChild(textArea);
                        textArea.focus();
                        textArea.select();
                        const successful = document.execCommand('copy');
                        textArea.remove();
                        if (successful) resolve();
                        else reject(new Error('Fallback copy failed'));
                    } catch (err) {
                        reject(err);
                    }
                });
            }
        };

        copyToClipboard(fullText).then(() => {
            // 短暫顯示「已複製」勾勾確認
            btn.classList.add('copied');
            while (btn.firstChild) btn.removeChild(btn.firstChild);

            const iconCheck = document.createElement('i');
            iconCheck.setAttribute('data-lucide', 'check');
            iconCheck.setAttribute('size', '13');
            const doneLabel = document.createElement('span');
            doneLabel.textContent = '已複製';
            btn.appendChild(iconCheck);
            btn.appendChild(doneLabel);
            lucide.createIcons();

            setTimeout(() => {
                btn.classList.remove('copied');
                while (btn.firstChild) btn.removeChild(btn.firstChild);
                const iconBack = document.createElement('i');
                iconBack.setAttribute('data-lucide', 'copy');
                iconBack.setAttribute('size', '13');
                const labelBack = document.createElement('span');
                labelBack.textContent = '複製';
                btn.appendChild(iconBack);
                btn.appendChild(labelBack);
                lucide.createIcons();
            }, 2000);
        }).catch(err => {
            console.error('複製失敗:', err);
            // 失敗時也可做簡單提示 (改個文字或顏色)
            const oldText = label.textContent;
            label.textContent = '複製失敗';
            setTimeout(() => { label.textContent = oldText; }, 2000);
        });
    });
}

/** 供建議回饋自動附帶當前頁面上下文（auth.js 會讀取） */
window.getFeedbackPageContext = function () {
    return {
        chat_id: state.currentChatId || null,
        project_id: state.currentProjectId || null,
        chat_mode: typeof chatMode !== 'undefined' ? chatMode : null,
        response_mode: typeof chatResponseMode !== 'undefined' ? chatResponseMode : null,
    };
};
