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
    renderChats('p1');
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
            sendMessage();
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

    document.getElementById('new-project-btn').addEventListener('click', () => {
        const name = prompt('請輸入專案名稱:');
        if (name) {
            const id = 'p' + Date.now();
            state.projects.push({ id, name });
            state.chats[id] = [];
            renderProjects();
        }
    });

    document.getElementById('new-chat-btn').addEventListener('click', () => {
        const title = 'New Chat ' + (state.chats[state.currentProjectId].length + 1);
        const id = 'c' + Date.now();
        state.chats[state.currentProjectId].push({ id, title });
        state.currentChatId = null;
        renderChats(state.currentProjectId);
        clearChatMessages();
    });
}

// --- 渲染 UI ---
function renderProjects() {
    const list = document.getElementById('project-list');
    list.innerHTML = '';
    state.projects.forEach(p => {
        const li = document.createElement('li');
        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', 'folder');
        const span = document.createElement('span');
        span.textContent = p.name;
        li.appendChild(icon);
        li.appendChild(span);
        if (p.id === state.currentProjectId) li.classList.add('active');
        li.onclick = () => {
            state.currentProjectId = p.id;
            renderProjects();
            renderChats(p.id);
        };
        list.appendChild(li);
    });
    lucide.createIcons();
}

function renderChats(projectId) {
    const list = document.getElementById('chat-list');
    list.innerHTML = '';
    const projectChats = state.chats[projectId] || [];
    projectChats.forEach(c => {
        const li = document.createElement('li');
        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', 'message-square');
        const span = document.createElement('span');
        span.textContent = c.title;
        li.appendChild(icon);
        li.appendChild(span);
        if (c.id === state.currentChatId) li.classList.add('active');
        li.onclick = () => {
            state.currentChatId = c.id;
            renderChats(projectId);
        };
        list.appendChild(li);
    });
    lucide.createIcons();
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
    const inputEl = document.getElementById('user-input');
    const query = inputEl.value.trim();
    if (!query) return;

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

    // 鎖定輸入
    const sendBtn = document.getElementById('send-btn');
    sendBtn.disabled = true;

    const statusBadge = document.getElementById('chat-status');
    statusBadge.textContent = 'Analyzing...';

    // 建立 AI 訊息容器（先放到畫面上，後續逐步填入）
    const { msgDiv, toolStatusEl, bubble, streamCursor } = createStreamingMessageUI();
    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();
    container.appendChild(msgDiv);
    scrollToBottom();

    // 暫存串流文字與最終 payload
    let rawStreamText = '';
    let donePayload = null;

    const cleanup = () => {
        streamCursor.remove();
        if (toolStatusEl.parentNode) toolStatusEl.remove();
    };

    try {
        const response = await fetch(`${state.apiBase}/chat/messages`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                query,
                chat_id: (state.currentChatId && !state.currentChatId.startsWith('c'))
                    ? state.currentChatId : null,
                agent_config: { enabled_tools }
            })
        });

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

                    // Router 思考中（小型文字，不是大框）
                    case 'thinking': {
                        updateToolStatus(toolStatusEl, payload.text || '思考中...', 'thinking');
                        break;
                    }

                    // 工具開始調用
                    case 'tool_start': {
                        updateToolStatus(toolStatusEl, formatToolName(payload.tool), 'running');
                        break;
                    }

                    // 工具完成
                    case 'tool_done': {
                        updateToolStatus(toolStatusEl, formatToolName(payload.tool), 'done');
                        break;
                    }

                    // LLM 逐字 token（僅 analyst 節點）
                    case 'token': {
                        if (rawStreamText === '') {
                            // 第一個 token：讓狀態列淡出
                            toolStatusEl.style.opacity = '0.35';
                        }
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

                        cleanup();
                        bubble.innerHTML = renderMarkdown(
                            payload.final_content || rawStreamText
                        );

                        appendStepsAndSources(msgDiv, payload.steps, payload.retrieval_sources);
                        lucide.createIcons();
                        scrollToBottom();
                        break;
                    }

                    case 'error': {
                        cleanup();
                        bubble.textContent = `錯誤：${payload.message}`;
                        break;
                    }
                }
            }
        }

    } catch (err) {
        console.error('Streaming error:', err);
        bubble.textContent = '伺服器連線失敗，請檢查 Docker 是否啟動。';
    } finally {
        // 確保游標與狀態列一定被清除，不管 done 有沒有成功收到
        cleanup();
        sendBtn.disabled = false;
        statusBadge.textContent = 'Ready';
        lucide.createIcons();
    }
}

// ============================================================
// 建立串流訊息 UI 骨架
// ============================================================

function createStreamingMessageUI() {
    const msgDiv = document.createElement('div');
    msgDiv.className = 'message ai';

    // 工具狀態列（初始：等待中）
    const toolStatusEl = document.createElement('div');
    toolStatusEl.className = 'tool-status';

    const iconWrap = document.createElement('span');
    iconWrap.className = 'tool-status-icon spinning';
    // 用 inline SVG 的旋轉圈圈
    iconWrap.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12a9 9 0 11-6.219-8.56"/>
    </svg>`;

    const statusText = document.createElement('span');
    statusText.textContent = '正在分析問題...';

    toolStatusEl.appendChild(iconWrap);
    toolStatusEl.appendChild(statusText);
    msgDiv.appendChild(toolStatusEl);

    // 氣泡（空的，等 token 進來後逐步填入）
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    msgDiv.appendChild(bubble);

    // 游標（串流時顯示）
    const streamCursor = document.createElement('span');
    streamCursor.className = 'stream-cursor';

    return { msgDiv, toolStatusEl, bubble, streamCursor };
}

// ============================================================
// 更新工具狀態列文字
// ============================================================

const TOOL_DISPLAY_NAMES = {
    search_stock_news:          '搜尋股市新聞',
    search_market_ai_analysis:  '搜尋 AI 市場分析',
    get_market_recommendations: '提取推薦標的',
};

function formatToolName(tool) {
    return TOOL_DISPLAY_NAMES[tool] || tool;
}

function updateToolStatus(el, text, statusType) {
    el.innerHTML = '';
    el.classList.toggle('done', statusType === 'done');

    if (statusType === 'thinking') {
        // 小字斜體，不顯示圖示
        el.style.fontStyle = 'italic';
        el.style.fontSize = '0.72rem';
        el.style.opacity = '0.65';
        el.style.borderStyle = 'dashed';
        const txt = document.createElement('span');
        txt.textContent = text;
        el.appendChild(txt);
        return;
    }

    // 其他狀態恢復預設字型
    el.style.fontStyle = '';
    el.style.fontSize = '';
    el.style.opacity = '';
    el.style.borderStyle = '';

    if (statusType === 'running') {
        const icon = document.createElement('span');
        icon.className = 'tool-status-icon spinning';
        icon.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 11-6.219-8.56"/>
        </svg>`;
        const label = document.createElement('span');
        label.className = 'tool-label';
        label.textContent = text;
        const suffix = document.createElement('span');
        suffix.textContent = '...';
        el.appendChild(icon);
        el.appendChild(label);
        el.appendChild(suffix);

    } else if (statusType === 'done') {
        const icon = document.createElement('span');
        icon.className = 'tool-status-icon';
        icon.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none"
            stroke="#00d68f" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="20 6 9 17 4 12"/>
        </svg>`;
        const label = document.createElement('span');
        label.className = 'tool-label';
        label.textContent = text;
        const suffix = document.createElement('span');
        suffix.textContent = ' ✓';
        el.appendChild(icon);
        el.appendChild(label);
        el.appendChild(suffix);
    }
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
