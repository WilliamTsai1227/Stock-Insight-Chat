// --- 串流鎖定旗標（防止上一輪回答未完成時重複送出）---
let isStreaming = false;

// --- 全域狀態 ---
let state = {
    projects: [
        { id: 'p1', name: '半導體研究' },
        { id: 'p2', name: '能源產業追蹤' }
    ],
    chats: {
        'p1': [
            { id: 'c1', title: '台積電供應商分析' }
        ]
    },
    currentProjectId: 'p1',
    currentChatId: 'c1',
    expandedProjects: new Set(['p1']),  // 預設展開第一個專案
    apiBase: 'http://localhost:8000/api'
};

// --- Marked.js 設定 ---
marked.setOptions({
    gfm: true,
    breaks: true,
    smartLists: true,
});

/**
 * 進階 Markdown 渲染
 */
function renderMarkdown(raw) {
    let html = marked.parse(raw || '');
    html = html.replace(
        /<strong>(\d{4,5})<\/strong>/g,
        '<strong class="stock-ticker">$1</strong>'
    );
    return html;
}

// --- 初始化 ---
document.addEventListener('DOMContentLoaded', () => {
    renderProjects();
    renderRecentChats();
    initEventListeners();
});

function initEventListeners() {
    document.getElementById('send-btn').addEventListener('click', sendMessage);

    const userInput = document.getElementById('user-input');
    userInput.addEventListener('input', function () {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
    });

    userInput.addEventListener('keydown', (e) => {
        if (e.isComposing || e.keyCode === 229) return;
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            if (!isStreaming) sendMessage();
        }
    });

    const toolToggle = document.getElementById('tool-toggle-btn');
    const toolPopover = document.getElementById('tool-popover');
    if (toolToggle) {
        toolToggle.onclick = (e) => {
            e.stopPropagation();
            toolPopover.classList.toggle('hidden');
        };
    }
    document.addEventListener('click', () => toolPopover.classList.add('hidden'));
    toolPopover.onclick = (e) => e.stopPropagation();

    const toolAuto = document.getElementById('tool-auto');
    const manualGroup = document.getElementById('manual-tools');
    if (toolAuto) {
        toolAuto.onchange = () => {
            manualGroup.classList.toggle('active', !toolAuto.checked);
        };
    }

    document.getElementById('new-chat-btn').addEventListener('click', () => {
        showChatView();
        clearChatMessages();
    });
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
    state.projects.forEach(p => {
        const li = document.createElement('li');
        const isExpanded = state.expandedProjects.has(p.id);
        const isActive   = p.id === state.currentProjectId;

        // 專案行
        const row = document.createElement('div');
        row.className = 'project-row'
            + (isActive   ? ' active'   : '')
            + (isExpanded ? ' expanded' : '');

        const folderIcon = document.createElement('i');
        folderIcon.setAttribute('data-lucide', 'folder');
        folderIcon.className = 'project-row-icon';

        const nameSpan = document.createElement('span');
        nameSpan.className = 'project-row-name';
        nameSpan.textContent = p.name;

        const chevron = document.createElement('i');
        chevron.setAttribute('data-lucide', 'chevron-right');
        chevron.className = 'project-row-chevron';

        row.appendChild(folderIcon);
        row.appendChild(nameSpan);
        row.appendChild(chevron);

        row.addEventListener('click', () => {
            if (state.expandedProjects.has(p.id)) {
                state.expandedProjects.delete(p.id);
            } else {
                state.expandedProjects.add(p.id);
            }
            state.currentProjectId = p.id;
            state.currentChatId = null;
            renderProjects();
            renderRecentChats();
            lucide.createIcons();
            showProjectView(p);
        });

        // 對話子列表（展開後顯示）
        const chatList = document.createElement('ul');
        chatList.className = 'project-chats' + (isExpanded ? ' open' : '');

        const projectChats = state.chats[p.id] || [];
        projectChats.forEach(c => {
            const chatLi = document.createElement('li');
            chatLi.className = 'project-chat-item'
                + (c.id === state.currentChatId ? ' active' : '');

            const msgIcon = document.createElement('i');
            msgIcon.setAttribute('data-lucide', 'message-square');

            const titleSpan = document.createElement('span');
            titleSpan.textContent = c.title;

            chatLi.appendChild(msgIcon);
            chatLi.appendChild(titleSpan);

            chatLi.addEventListener('click', (e) => {
                e.stopPropagation();
                state.currentChatId = c.id;
                state.currentProjectId = p.id;
                renderProjects();
                renderRecentChats();
                lucide.createIcons();
                showChatView();
            });

            chatList.appendChild(chatLi);
        });

        li.appendChild(row);
        li.appendChild(chatList);
        list.appendChild(li);
    });

    lucide.createIcons();
}

/**
 * 渲染最近對話（彙整所有專案的 chats，依加入順序由新到舊）
 */
function renderRecentChats() {
    const list = document.getElementById('recent-chat-list');
    while (list.firstChild) list.removeChild(list.firstChild);

    // 收集所有 chats，並記錄所屬 project
    const allChats = [];
    state.projects.forEach(p => {
        (state.chats[p.id] || []).forEach(c => {
            allChats.push({ ...c, projectId: p.id });
        });
    });

    // 反轉：最新加入的排在最前面
    allChats.reverse().forEach(c => {
        const li = document.createElement('li');
        li.className = 'recent-chat-item'
            + (c.id === state.currentChatId ? ' active' : '');

        const msgIcon = document.createElement('i');
        msgIcon.setAttribute('data-lucide', 'message-square');

        const titleSpan = document.createElement('span');
        titleSpan.textContent = c.title;

        li.appendChild(msgIcon);
        li.appendChild(titleSpan);

        li.addEventListener('click', () => {
            state.currentChatId = c.id;
            state.currentProjectId = c.projectId;
            renderProjects();
            renderRecentChats();
            lucide.createIcons();
        });

        list.appendChild(li);
    });

    lucide.createIcons();
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

/** input 即時驗證：有字才啟用「建立專案」按鈕 */
function onProjectNameInput() {
    const val = document.getElementById('create-project-name').value.trim();
    document.getElementById('create-project-submit-btn').disabled = val.length === 0;
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

        const newProject = { id: data.data.id, name: data.data.name };
        state.projects.unshift(newProject);
        state.chats[newProject.id] = [];
        state.expandedProjects.add(newProject.id);
        state.currentProjectId = newProject.id;
        state.currentChatId    = null;

        closeCreateProjectModal();
        renderProjects();
        renderRecentChats();
        lucide.createIcons();
        showProjectView(newProject);

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

/** 顯示專案視圖（選擇專案但尚無對話時） */
function showProjectView(project) {
    document.querySelector('.chat-header').style.display    = 'none';
    document.getElementById('chat-messages').style.display = 'none';
    document.querySelector('.chat-input-area').style.display = 'none';

    const pv = document.getElementById('project-view');
    pv.style.display = 'flex';

    document.getElementById('pv-project-name').textContent  = project.name;
    document.getElementById('pv-new-chat-text').textContent  = `在 ${project.name} 的新聊天`;
    document.getElementById('pv-empty-subtitle').textContent = `${project.name} 中的聊天將顯示在此處`;
}

/** 切回聊天視圖 */
function showChatView() {
    document.getElementById('project-view').style.display   = 'none';
    document.querySelector('.chat-header').style.display    = '';
    document.getElementById('chat-messages').style.display  = '';
    document.querySelector('.chat-input-area').style.display = '';
}

function clearChatMessages() {
    const container = document.getElementById('chat-messages');
    container.innerHTML = '';
    const hero = document.createElement('div');
    hero.className = 'welcome-hero';
    const h1 = document.createElement('h1');
    h1.textContent = '準備好開始新的研究了嗎？';
    hero.appendChild(h1);
    container.appendChild(hero);
}

// ============================================================
// 核心：支援 SSE 串流的 sendMessage
// ============================================================

async function sendMessage() {
    // ── 防止重複送出：旗標在任何邏輯之前立即佔位 ──
    if (isStreaming) return;
    isStreaming = true;

    const sendBtn = document.getElementById('send-btn');
    const inputEl = document.getElementById('user-input');

    // 立即鎖定 UI（雙重保險，防快速連按）
    sendBtn.disabled = true;
    inputEl.disabled = true;
    inputEl.placeholder = '等待回覆中...';

    const query = inputEl.value.trim();
    if (!query) {
        // 空白輸入：解鎖後直接返回
        isStreaming = false;
        sendBtn.disabled = false;
        inputEl.disabled = false;
        inputEl.placeholder = '輸入您的問題...';
        return;
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

    // 顯示使用者訊息
    addMessageToUI('user', query);
    inputEl.value = '';
    inputEl.style.height = 'auto';

    const statusBadge = document.getElementById('chat-status');
    statusBadge.textContent = 'Analyzing...';

    // 建立 AI 訊息容器（先放到畫面上，後續逐步填入）
    const { msgDiv, toolsContainer, bubble, streamCursor, initialPlaceholder } = createStreamingMessageUI();
    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();
    container.appendChild(msgDiv);
    scrollToBottom();

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
        scrollToBottom();
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

    try {
        // authFetch 自動注入 Authorization: Bearer AT，
        // 並在 AT 即將過期（≤ 90s）時先靜默換 Token 再送請求（機制 B）。
        // AT 已過期且換 Token 失敗時回傳 undefined 並導向登入頁，
        // 此時直接 return 以結束函式，finally 區塊仍會負責解鎖 UI。
        const response = await authFetch(`${state.apiBase}/chat/messages`, {
            method: 'POST',
            body: JSON.stringify({
                query,
                chat_id: (state.currentChatId && !state.currentChatId.startsWith('c'))
                    ? state.currentChatId : null,
                agent_config: { enabled_tools }
            })
        });

        if (!response) return;  // authFetch 已處理 401 → 跳轉 login.html
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        if (!response.body) throw new Error('瀏覽器不支援 Streaming');

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
                        scrollToBottom();
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
                        hideThinkingRow();
                        addBubbleIfNeeded();
                        rawStreamText += payload.text || '';
                        bubble.innerHTML = renderMarkdown(rawStreamText);
                        bubble.appendChild(streamCursor);
                        scrollToBottom();
                        break;
                    }

                    // 全部完成
                    case 'done': {
                        donePayload = payload;
                        state.currentChatId = payload.chat_id;
                        addBubbleIfNeeded();
                        cleanup();
                        const finalText = payload.final_content || rawStreamText;
                        bubble.innerHTML = renderMarkdown(finalText);
                        appendStepsAndSources(msgDiv, payload.steps, payload.retrieval_sources);
                        appendCopyBar(msgDiv, finalText);
                        lucide.createIcons();
                        scrollToBottom();
                        break;
                    }

                    case 'error': {
                        hideThinkingRow();
                        addBubbleIfNeeded();
                        cleanup();
                        bubble.textContent = `錯誤：${payload.message}`;
                        break;
                    }
                }
            }
        }

    } catch (err) {
        console.error('Streaming error:', err);
        addBubbleIfNeeded();
        bubble.textContent = '伺服器連線失敗，請檢查 Docker 是否啟動。';
    } finally {
        // 確保游標與狀態列一定被清除，不管 done 有沒有成功收到
        cleanup();
        isStreaming = false;
        sendBtn.disabled = false;
        inputEl.disabled = false;
        inputEl.placeholder = '輸入您的問題...';
        statusBadge.textContent = 'Ready';
        lucide.createIcons();
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

function createStreamingMessageUI() {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message ai';

    // 工具列容器（多個 tool row 垂直堆疊）
    const toolsContainer = document.createElement('div');
    toolsContainer.className = 'tools-container';

    // 初始佔位列（分析中...）
    const initialPlaceholder = document.createElement('div');
    initialPlaceholder.className = 'tool-status';
    const initIconWrap = document.createElement('span');
    initIconWrap.className = 'tool-status-icon spinning';
    initIconWrap.appendChild(makeSvgSpinner(14));
    const initText = document.createElement('span');
    initText.textContent = '正在分析問題...';
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
    get_market_recommendations: '提取推薦標的',
};

function formatToolName(tool) {
    return TOOL_DISPLAY_NAMES[tool] || tool;
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

            const thought = document.createElement('div');
            thought.className = 'step-thought';
            thought.textContent = s.thought || s.content || '';

            stepDiv.appendChild(meta);
            stepDiv.appendChild(thought);

            if (s.tool_calls && s.tool_calls.length > 0) {
                const callsWrap = document.createElement('div');
                callsWrap.className = 'step-tool-calls';
                s.tool_calls.forEach(tc => {
                    const card = document.createElement('div');
                    card.className = 'tool-call-card';

                    const nameEl = document.createElement('div');
                    nameEl.className = 'tool-name';
                    nameEl.textContent = `調用工具: ${tc.name}`;

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

function addMessageToUI(role, content) {
    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = content;
    msgDiv.appendChild(bubble);

    container.appendChild(msgDiv);
    scrollToBottom();
}

function scrollToBottom() {
    const container = document.getElementById('chat-messages');
    container.scrollTop = container.scrollHeight;
}

// ============================================================
// 複製按鈕列（回答完成後附加在氣泡下方）
// ============================================================

function appendCopyBar(msgDiv, rawText) {
    const bar = document.createElement('div');
    bar.className = 'copy-bar';

    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.title = '複製回答';

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
        // 複製純文字（去除 markdown 符號）
        const plainText = rawText
            .replace(/#{1,6}\s+/g, '')
            .replace(/\*\*(.+?)\*\*/g, '$1')
            .replace(/\*(.+?)\*/g, '$1')
            .replace(/`{1,3}[^`]*`{1,3}/g, '')
            .replace(/\[(.+?)\]\(.+?\)/g, '$1')
            .trim();

        navigator.clipboard.writeText(plainText).then(() => {
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
        });
    });
}
