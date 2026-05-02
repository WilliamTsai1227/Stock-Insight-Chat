// --- 串流鎖定旗標（防止上一輪回答未完成時重複送出）---
let isStreaming = false;

// --- 全域狀態 ---
// 注意：所有資料皆從後端載入，不放任何假資料
//   projects   : { id, name, created_at }[]   ← /api/project/all 載入
//   chats      : { [projectId]: { id, title }[] }       ← /api/project?project_id=... 載入
//   files      : { [projectId]: File[] }                ← 同上
//   pendingDeleteProject : 暫存「刪除確認 modal」要刪除的專案物件
let state = {
    projects: [],
    chats: {},
    files: {},
    currentProjectId: null,
    currentChatId: null,
    pendingDeleteProject: null,
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
document.addEventListener('DOMContentLoaded', async () => {
    // 先渲染空白骨架（確保「新增專案」按鈕立刻可見）
    renderProjects();
    renderRecentChats();
    initEventListeners();
    initProjectViewTabs();

    // auth.js 的 DOMContentLoaded 會觸發 tryRefreshToken() 取得 AT；
    // 這裡再呼叫一次（受 _isRefreshing 並發鎖保護，會共用同一個 Promise，
    // 不會重複打 /refresh），確保我們在 AT 就緒後才載入專案列表。
    if (typeof tryRefreshToken === 'function') {
        await tryRefreshToken();
    }

    await loadProjectsFromServer();
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
        }));

        // 清掉已不存在的 chats / files 快取
        const validIds = new Set(state.projects.map(p => p.id));
        for (const id of Object.keys(state.chats)) {
            if (!validIds.has(id)) delete state.chats[id];
        }
        for (const id of Object.keys(state.files)) {
            if (!validIds.has(id)) delete state.files[id];
        }

        renderProjects();
        renderRecentChats();
        lucide.createIcons();
    } catch (err) {
        console.error('載入專案列表時發生錯誤：', err);
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

        row.addEventListener('click', async () => {
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
            showProjectView(p, { detail });
        });

        li.appendChild(row);
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
        li.id = `recent-chat-${c.id}`;
        li.dataset.chatId = c.id;
        li.dataset.projectId = c.projectId;

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
            showChatView();
        });

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

        // 2. 若刪掉的是目前顯示中的專案，回到歡迎畫面
        if (state.currentProjectId === project.id) {
            state.currentProjectId = null;
            state.currentChatId    = null;
            showChatView();
            clearChatMessages();
        }

        closeAllModals();
        state.pendingDeleteProject = null;

        // 3. 重新從後端載入專案列表（與真相同步），同時更新 UI
        await loadProjectsFromServer();

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

        const newProject = {
            id: data.data.id,
            name: data.data.name,
            created_at: data.data.created_at,
        };
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
    document.querySelector('.chat-header').style.display    = 'none';
    document.getElementById('chat-messages').style.display  = 'none';
    document.querySelector('.chat-input-area').style.display = 'none';

    const pv = document.getElementById('project-view');
    pv.style.display = 'flex';

    document.getElementById('pv-project-name').textContent   = project.name;
    document.getElementById('pv-new-chat-text').textContent  = `在 ${project.name} 的新聊天`;
    document.getElementById('pv-empty-subtitle').textContent = `${project.name} 中的聊天將顯示在此處`;
    const filesEmptySub = document.getElementById('pv-files-empty-subtitle');
    if (filesEmptySub) {
        filesEmptySub.textContent = `${project.name} 中的資料來源將顯示在此處`;
    }

    // 重置為「聊天」分頁
    setActivePvTab('chats');

    if (options.loading) {
        // 載入中：先清掉舊列表，避免閃爍上一個專案的資料
        clearPvLists();
        return;
    }

    if (options.detail) {
        renderPvChats(options.detail.chats || [], project.id);
        renderPvFiles(options.detail.files || [], project.id);
    } else {
        clearPvLists();
    }

    lucide.createIcons();
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

        li.addEventListener('click', () => {
            state.currentChatId    = c.id;
            state.currentProjectId = projectId;
            renderProjects();
            renderRecentChats();
            lucide.createIcons();
            showChatView();
        });

        list.appendChild(li);
    });
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
                        appendCopyBar(msgDiv, finalText, payload.retrieval_sources);
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

/**
 * 在 AI 訊息底部附加複製按鈕列。
 * @param {HTMLElement} msgDiv   訊息容器
 * @param {string}      rawText  原始 Markdown 回答
 * @param {Array}       sources  參考來源陣列（可選），格式：{ tool, title, publishAt, url }[]
 */
function appendCopyBar(msgDiv, rawText, sources) {
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
        // 回答本文：去除 Markdown 符號，轉為純文字
        const plainText = rawText
            .replace(/#{1,6}\s+/g, '')
            .replace(/\*\*(.+?)\*\*/g, '$1')
            .replace(/\*(.+?)\*/g, '$1')
            .replace(/`{1,3}[^`]*`{1,3}/g, '')
            .replace(/\[(.+?)\]\(.+?\)/g, '$1')
            .trim();

        // 參考來源：格式化為純文字附加到回答後方
        let sourcesText = '';
        if (sources && sources.length > 0) {
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

        navigator.clipboard.writeText(fullText).then(() => {
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
