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
    apiBase: 'http://localhost:8000/api' // 指向 FastApi 後端
};

// --- Marked.js 設定 ---
marked.setOptions({
    gfm: true,          // GitHub Flavored Markdown (表格、刪除線)
    breaks: true,        // \n 轉為 <br>
    smartLists: true,    // 智慧列表縮排
});

/**
 * 進階 Markdown 渲染 — 將原始 markdown 轉換為帶有增強視覺效果的 HTML。
 * 後處理步驟：
 *   1. 將股票代碼 (4 位數字) 包裝成 .stock-ticker 高亮標籤
 *   2. 為常見的金融區段標題加上 emoji 圖示
 */
function renderMarkdown(raw) {
    let html = marked.parse(raw || '');

    // 後處理: 將 <strong>xxxx</strong> (4位數字的股票代碼) 加上特殊樣式
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
    // 傳送按鈕
    document.getElementById('send-btn').addEventListener('click', sendMessage);
    
    // 輸入框: 自動增高與 Enter 傳送
    const userInput = document.getElementById('user-input');
    userInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = (this.scrollHeight) + 'px';
    });
    
    userInput.addEventListener('keydown', (e) => {
        // 如果正在選字 (IME composition)，不執行發送邏輯
        if (e.isComposing || e.keyCode === 229) return;
        
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });

    // 工具選單切換
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

    // 工具邏輯: 自動開關
    const toolAuto = document.getElementById('tool-auto');
    const manualGroup = document.getElementById('manual-tools');
    if (toolAuto) {
        toolAuto.onchange = () => {
            manualGroup.classList.toggle('active', !toolAuto.checked);
        };
    }

    // 新增專案
    document.getElementById('new-project-btn').addEventListener('click', () => {
        const name = prompt('請輸入專案名稱:');
        if (name) {
            const id = 'p' + Date.now();
            state.projects.push({ id, name });
            state.chats[id] = [];
            renderProjects();
        }
    });

    // 新增聊天
    document.getElementById('new-chat-btn').addEventListener('click', () => {
        const title = 'New Chat ' + (state.chats[state.currentProjectId].length + 1);
        const id = 'c' + Date.now();
        state.chats[state.currentProjectId].push({ id, title });
        state.currentChatId = null; // 重置 chat_id
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
        li.innerHTML = `<i data-lucide="folder"></i> ${p.name}`;
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
        li.innerHTML = `<i data-lucide="message-square"></i> ${c.title}`;
        if (c.id === state.currentChatId) li.classList.add('active');
        li.onclick = () => {
            state.currentChatId = c.id;
            renderChats(projectId);
            // 這裡可以做讀取歷史紀錄的邏輯
        };
        list.appendChild(li);
    });
    lucide.createIcons();
}

function clearChatMessages() {
    const container = document.getElementById('chat-messages');
    container.innerHTML = '<div class="welcome-hero"><h1>準備好開始新的研究了嗎？</h1></div>';
}

// --- API 溝通 ---
async function sendMessage() {
    const inputEl = document.getElementById('user-input');
    const query = inputEl.value.trim();
    if (!query) return;

    // 工具權限獲取
    let enabled_tools = [];
    const toolAuto = document.getElementById('tool-auto');
    const isAuto = toolAuto ? toolAuto.checked : true;
    
    if (!isAuto) {
        document.querySelectorAll('.tool-check:checked').forEach(cb => {
            enabled_tools.push(cb.value);
        });
    }
    
    // 1. 顯示使用者訊息並重設輸入框
    addMessageToUI('user', query);
    inputEl.value = '';
    inputEl.style.height = 'auto'; // 回復原始高度
    
    // 2. 顯示思考動畫
    const statusBadge = document.getElementById('chat-status');
    statusBadge.innerText = 'Analyzing...';
    const typingId = showTypingIndicator();

    try {
        const response = await fetch(`${state.apiBase}/chat/messages`, {
            method: 'POST',
            headers: { 
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            },
            body: JSON.stringify({
                query: query,
                chat_id: (state.currentChatId && !state.currentChatId.startsWith('c')) ? state.currentChatId : null,
                agent_config: { enabled_tools: enabled_tools }
            })
        });

        if (!response.ok) throw new Error(`HTTP Error ${response.status}`);

        const data = await response.json();
        removeTypingIndicator(typingId);
        
        if (data.status === 'success') {
            state.currentChatId = data.chat_id; 
            addMessageToUI('ai', data.final_content, data.steps, data.retrieval_sources);
        } else {
            addMessageToUI('ai', 'Error: ' + (data.detail || 'Unknown error'));
        }
    } catch (err) {
        if (typingId) removeTypingIndicator(typingId);
        console.error("Fetch error:", err);
        addMessageToUI('ai', '伺服器連線失敗，請檢查 Docker 是否啟動。');
    } finally {
        statusBadge.innerText = 'Ready';
        statusBadge.classList.remove('loading');
    }
}

function showTypingIndicator() {
    const container = document.getElementById('chat-messages');
    const id = 'typing-' + Date.now();
    const div = document.createElement('div');
    div.id = id;
    div.className = 'message ai';
    div.innerHTML = `<div class="typing-indicator"><span></span><span></span><span></span></div>`;
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return id;
}

function removeTypingIndicator(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

function addMessageToUI(role, content, steps, sources) {
    const container = document.getElementById('chat-messages');
    const welcome = container.querySelector('.welcome-hero');
    if (welcome) welcome.remove();

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${role}`;
    
    // 1. 渲染主內容 (final_content)
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = renderMarkdown(content);
    msgDiv.appendChild(bubble);

    // 2. 如果是 AI 且有 steps，增加詳細思考歷程
    if (role === 'ai' && steps && steps.length > 0) {
        const stepsContainer = document.createElement('div');
        stepsContainer.className = 'steps-container';
        
        const header = document.createElement('div');
        header.className = 'step-header';
        header.innerHTML = `<span><i data-lucide="cpu" size="14"></i> 執行軌跡 (ReAct Trace)</span> <i data-lucide="chevron-down" size="14"></i>`;
        
        const body = document.createElement('div');
        body.className = 'step-body';
        body.style.display = 'none';
        
        steps.forEach((s, idx) => {
            const stepDiv = document.createElement('div');
            stepDiv.className = 'step-item';
            const nodeLabel = s.node === 'router' ? 'Router 決策' : 'Analyst 撰寫';
            
            // 處理工具調用內容
            let toolCallsHtml = '';
            if (s.tool_calls && s.tool_calls.length > 0) {
                toolCallsHtml = '<div class="step-tool-calls">';
                s.tool_calls.forEach(tc => {
                    toolCallsHtml += `
                        <div class="tool-call-card">
                            <div class="tool-name">調用工具: ${tc.name}</div>
                            <div class="tool-query">搜尋詞: <strong>${tc.query}</strong></div>
                            <div class="tool-dates">區間: ${tc.start_date || 'N/A'} ~ ${tc.end_date || 'N/A'}</div>
                        </div>
                    `;
                });
                toolCallsHtml += '</div>';
            }

            stepDiv.innerHTML = `
                <div class="step-meta">
                    <span class="step-node">#${idx+1} ${nodeLabel}</span>
                    <span class="step-time"><i data-lucide="clock" size="10"></i> ${s.execution_time}s</span>
                </div>
                <div class="step-thought">${s.thought || s.content || ''}</div>
                ${toolCallsHtml}
            `;
            body.appendChild(stepDiv);
        });

        header.onclick = () => {
            body.style.display = body.style.display === 'none' ? 'block' : 'none';
        };

        stepsContainer.appendChild(header);
        stepsContainer.appendChild(body);
        msgDiv.appendChild(stepsContainer);
    }

    // 3. 渲染檢索來源 (retrieval_sources)
    if (role === 'ai' && sources && sources.length > 0) {
        const sourcesSection = document.createElement('div');
        sourcesSection.className = 'sources-container'; // 改用 container 統一命名空間
        
        const header = document.createElement('div');
        header.className = 'sources-header';
        header.innerHTML = `<span><i data-lucide="library" size="14"></i> 參考來源 (${sources.length})</span> <i data-lucide="chevron-down" size="14"></i>`;
        
        const body = document.createElement('div');
        body.className = 'sources-body';
        body.style.display = 'none';
        
        sources.forEach(src => {
            const item = document.createElement('div');
            item.className = 'source-list-item';
            item.innerHTML = `
                <div class="source-info">
                    <span class="source-tag">${src.tool.toUpperCase()}</span>
                    <span class="source-title">${src.title}</span>
                    <span class="source-date">${new Date(src.publishAt).toLocaleDateString()}</span>
                </div>
                ${src.url ? `<a href="${src.url}" target="_blank" class="source-action-link"><i data-lucide="external-link" size="14"></i></a>` : ''}
            `;
            body.appendChild(item);
        });

        header.onclick = () => {
            body.style.display = body.style.display === 'none' ? 'block' : 'none';
        };

        sourcesSection.appendChild(header);
        sourcesSection.appendChild(body);
        msgDiv.appendChild(sourcesSection);
    }

    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;
    lucide.createIcons();
}
