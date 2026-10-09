/**
 * 筆記模組
 * ========
 * 1. 在對話中勾選訊息 → 交給後端 LLM 整理成一則筆記
 * 2. 筆記視圖：左側筆記頁清單，右側該頁的筆記
 *
 * 視圖切換與 index.js 的 showChatView / showExploreView、deep-research 的
 * showDeepResearchView 互斥，規則與既有模組一致。
 *
 * 白板是下一階段；本階段先把「整理品質」與資料模型跑順，
 * 後端 notes.canvas_* 欄位已預留，長成白板時不需要再 migration。
 */

const notesState = {
    pages: [],
    currentPageId: null,
    notes: [],
    selecting: false,
    /** @type {Set<string>} 已勾選的 message id */
    selected: new Set(),
    loaded: false,
};

const noteEl = (id) => document.getElementById(id);

// ============================================================
// 視圖切換
// ============================================================

function showNotesView() {
    if (typeof maybeParkViewportForLeavingChat === 'function') {
        maybeParkViewportForLeavingChat(
            typeof state !== 'undefined' ? state.currentChatId : null
        );
    }
    if (typeof hideExploreView === 'function') hideExploreView();
    if (typeof hideDeepResearchView === 'function') hideDeepResearchView();
    exitNoteSelectMode();

    noteEl('chat-messages').style.display = 'none';
    noteEl('project-view').style.display = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.querySelector('.chat-input-area').style.display = 'none';
    noteEl('notes-view').style.display = 'flex';

    const btn = noteEl('notes-btn');
    if (btn) btn.classList.add('active');
    if (typeof setMainChatTitle === 'function') setMainChatTitle('筆記');

    if (!notesState.loaded) {
        notesState.loaded = true;
        loadNotePages();
    }
}

function hideNotesView() {
    const view = noteEl('notes-view');
    if (view) view.style.display = 'none';
    const btn = noteEl('notes-btn');
    if (btn) btn.classList.remove('active');
}

// ============================================================
// 筆記頁
// ============================================================

async function loadNotePages(preferPageId) {
    try {
        const res = await authFetch(`${state.apiBase}/notes/pages`);
        if (!res || !res.ok) return;
        const json = await res.json();
        notesState.pages = (json.data && json.data.pages) || [];

        const wanted = preferPageId || notesState.currentPageId;
        const exists = notesState.pages.some((p) => p.id === wanted);
        notesState.currentPageId = exists
            ? wanted
            : (notesState.pages[0] ? notesState.pages[0].id : null);

        renderNotePages();
        if (notesState.currentPageId) {
            await loadNotesOfPage(notesState.currentPageId);
        } else {
            notesState.notes = [];
            renderNotes();
        }
    } catch (err) {
        console.error('[NOTES] 載入筆記頁失敗：', err);
        showToast('載入筆記頁失敗', 'error');
    }
}

function renderNotePages() {
    const list = noteEl('note-page-list');
    if (!list) return;
    list.textContent = '';

    notesState.pages.forEach((page) => {
        const li = document.createElement('li');
        li.className = 'notes-page-item' + (page.id === notesState.currentPageId ? ' active' : '');
        li.tabIndex = 0;

        const name = document.createElement('span');
        name.className = 'notes-page-name';
        name.textContent = page.title;

        const count = document.createElement('span');
        count.className = 'notes-page-count';
        count.textContent = page.note_count;

        li.append(name, count);
        li.addEventListener('click', () => {
            notesState.currentPageId = page.id;
            renderNotePages();
            loadNotesOfPage(page.id);
        });
        list.appendChild(li);
    });

    const page = notesState.pages.find((p) => p.id === notesState.currentPageId);
    const title = noteEl('note-page-title');
    if (title) title.textContent = page ? page.title : '筆記';
    // 沒有任何頁時，重新命名／刪除沒有對象
    const hasPage = !!page;
    ['note-page-rename', 'note-page-delete'].forEach((id) => {
        const btn = noteEl(id);
        if (btn) btn.disabled = !hasPage;
    });
}

async function createNotePage() {
    const title = (prompt('新筆記頁名稱？', '新筆記頁') || '').trim();
    if (!title) return;
    try {
        const res = await authFetch(`${state.apiBase}/notes/pages`, {
            method: 'POST',
            body: JSON.stringify({ title }),
        });
        if (!res) return;
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || '建立筆記頁失敗', 'error');
            return;
        }
        const json = await res.json();
        await loadNotePages(json.data.id);
        showToast('已建立筆記頁', 'success');
    } catch (err) {
        showToast('建立筆記頁失敗', 'error');
    }
}

async function renameNotePage() {
    const page = notesState.pages.find((p) => p.id === notesState.currentPageId);
    if (!page) return;
    const title = (prompt('新的頁面名稱？', page.title) || '').trim();
    if (!title || title === page.title) return;
    try {
        const res = await authFetch(`${state.apiBase}/notes/pages/${page.id}`, {
            method: 'PATCH',
            body: JSON.stringify({ title }),
        });
        if (!res) return;
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || '重新命名失敗', 'error');
            return;
        }
        await loadNotePages(page.id);
    } catch (err) {
        showToast('重新命名失敗', 'error');
    }
}

async function deleteNotePage() {
    const page = notesState.pages.find((p) => p.id === notesState.currentPageId);
    if (!page) return;
    if (!confirm(`刪除「${page.title}」？這一頁的 ${page.note_count} 則筆記會一起刪除，無法復原。`)) return;
    try {
        const res = await authFetch(`${state.apiBase}/notes/pages/${page.id}`, { method: 'DELETE' });
        if (!res || !res.ok) {
            showToast('刪除失敗', 'error');
            return;
        }
        notesState.currentPageId = null;
        await loadNotePages();
        showToast('已刪除筆記頁', 'success');
    } catch (err) {
        showToast('刪除失敗', 'error');
    }
}

// ============================================================
// 筆記
// ============================================================

async function loadNotesOfPage(pageId) {
    try {
        const res = await authFetch(`${state.apiBase}/notes?page_id=${encodeURIComponent(pageId)}`);
        if (!res || !res.ok) return;
        const json = await res.json();
        notesState.notes = (json.data && json.data.notes) || [];
        renderNotes();
    } catch (err) {
        console.error('[NOTES] 載入筆記失敗：', err);
    }
}

function renderNotes() {
    const list = noteEl('note-list');
    const empty = noteEl('notes-empty');
    if (!list) return;
    list.textContent = '';

    if (!notesState.notes.length) {
        if (empty) empty.classList.remove('hidden');
        if (typeof lucide !== 'undefined') lucide.createIcons();
        return;
    }
    if (empty) empty.classList.add('hidden');

    notesState.notes.forEach((note) => {
        const card = document.createElement('article');
        card.className = 'note-card';

        const head = document.createElement('header');
        head.className = 'note-card-head';

        const title = document.createElement('h4');
        title.textContent = note.title;

        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'notes-icon-btn danger';
        del.title = '刪除筆記';
        del.setAttribute('aria-label', '刪除筆記');
        del.innerHTML = '<i data-lucide="trash-2"></i>';
        del.addEventListener('click', (e) => {
            e.stopPropagation();
            deleteNote(note);
        });

        head.append(title, del);

        const body = document.createElement('div');
        body.className = 'note-card-body';
        // 內容是 LLM 產生的 Markdown，沿用聊天氣泡那條已消毒的渲染路徑
        if (typeof applyMarkdown === 'function') {
            applyMarkdown(body, note.content || '');
        } else {
            body.textContent = note.content || '';
        }

        const foot = document.createElement('footer');
        foot.className = 'note-card-foot';
        const when = note.created_at ? new Date(note.created_at).toLocaleString('zh-TW') : '';
        foot.textContent = `${when}　·　整理自 ${note.source_message_ids.length} 則訊息`;

        card.append(head, body, foot);
        list.appendChild(card);
    });

    if (typeof lucide !== 'undefined') lucide.createIcons();
}

async function deleteNote(note) {
    if (!confirm(`刪除筆記「${note.title}」？無法復原。`)) return;
    try {
        const res = await authFetch(`${state.apiBase}/notes/${note.id}`, { method: 'DELETE' });
        if (!res || !res.ok) {
            showToast('刪除失敗', 'error');
            return;
        }
        await loadNotePages(notesState.currentPageId);
        showToast('已刪除', 'success');
    } catch (err) {
        showToast('刪除失敗', 'error');
    }
}


// ============================================================
// 選取模式：在對話中勾選訊息
// ============================================================

/**
 * 補齊 DOM 上缺少的 message id。
 *
 * 串流中的 'done' 事件在 assistant 訊息寫進 DB 之前就送出，所以剛對話完的
 * 氣泡沒有 id。這裡向後端取權威清單，依序補上（DOM 與 API 的訊息順序一致）。
 * 數量對不起來就不猜，回 false 讓呼叫端提示使用者重載。
 */
async function syncMessageIds() {
    const chatId = typeof state !== 'undefined' ? state.currentChatId : null;
    if (!chatId) return false;

    const els = Array.from(document.querySelectorAll('#chat-messages .message'));
    if (!els.length) return false;
    if (els.every((el) => el.dataset.messageId)) return true;

    try {
        const res = await authFetch(
            `${state.apiBase}/chat?chat_id=${encodeURIComponent(chatId)}`
        );
        if (!res || !res.ok) return false;
        const json = await res.json();
        const msgs = ((json.data && json.data.messages) || [])
            .filter((m) => m.role === 'user' || m.role === 'assistant');
        if (msgs.length !== els.length) return false;
        els.forEach((el, i) => { el.dataset.messageId = msgs[i].id; });
        return true;
    } catch (err) {
        console.error('[NOTES] 對齊訊息 id 失敗：', err);
        return false;
    }
}

async function enterNoteSelectMode() {
    if (notesState.selecting) return;

    const container = noteEl('chat-messages');
    if (!container || container.style.display === 'none') {
        showToast('請先開啟一則對話', 'info');
        return;
    }
    if (!document.querySelector('#chat-messages .message')) {
        showToast('這則對話還沒有訊息', 'info');
        return;
    }
    if (typeof streamingChatIds !== 'undefined' &&
        streamingChatIds.has(state.currentChatId)) {
        showToast('等這則回覆產生完再整理成筆記', 'info');
        return;
    }

    const ok = await syncMessageIds();
    if (!ok) {
        showToast('訊息尚未同步，請重新整理頁面後再試', 'error');
        return;
    }

    notesState.selecting = true;
    notesState.selected.clear();
    container.classList.add('note-selecting');

    document.querySelectorAll('#chat-messages .message').forEach((el) => {
        if (el.querySelector('.note-pick')) return;
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'note-pick';
        pick.setAttribute('aria-label', '選取此訊息');
        pick.innerHTML = '<i data-lucide="check"></i>';
        pick.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleMessageSelection(el);
        });
        el.appendChild(pick);
    });

    noteEl('note-select-bar').classList.remove('hidden');
    noteEl('note-select-btn').classList.add('active');
    updateNoteSelectCount();
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function exitNoteSelectMode() {
    if (!notesState.selecting) {
        // 即使沒在選取模式也要確保 UI 是乾淨的（切視圖時會呼叫）
        const bar = noteEl('note-select-bar');
        if (bar) bar.classList.add('hidden');
        return;
    }
    notesState.selecting = false;
    notesState.selected.clear();

    const container = noteEl('chat-messages');
    if (container) container.classList.remove('note-selecting');
    document.querySelectorAll('#chat-messages .message').forEach((el) => {
        el.classList.remove('note-picked');
        const pick = el.querySelector('.note-pick');
        if (pick) pick.remove();
    });
    noteEl('note-select-bar').classList.add('hidden');
    noteEl('note-select-btn').classList.remove('active');
}

function toggleMessageSelection(el) {
    const id = el.dataset.messageId;
    if (!id) return;
    if (notesState.selected.has(id)) {
        notesState.selected.delete(id);
        el.classList.remove('note-picked');
    } else {
        notesState.selected.add(id);
        el.classList.add('note-picked');
    }
    updateNoteSelectCount();
}

function updateNoteSelectCount() {
    const label = noteEl('note-select-count');
    if (label) label.textContent = `已選 ${notesState.selected.size} 則`;
    const confirm = noteEl('note-select-confirm');
    if (confirm) confirm.disabled = notesState.selected.size === 0;
}

async function confirmGenerateNote() {
    if (!notesState.selected.size) return;

    const btn = noteEl('note-select-confirm');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '整理中…';

    // 依畫面順序送出，後端也會照 created_at 排序，但先排好比較不會誤解
    const ordered = Array.from(document.querySelectorAll('#chat-messages .message'))
        .map((el) => el.dataset.messageId)
        .filter((id) => id && notesState.selected.has(id));

    try {
        const res = await authFetch(`${state.apiBase}/notes/generate`, {
            method: 'POST',
            body: JSON.stringify({
                chat_id: state.currentChatId,
                message_ids: ordered,
                page_id: notesState.currentPageId || undefined,
            }),
        });
        if (!res) return;
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            showToast(err.detail || `整理失敗（HTTP ${res.status}）`, 'error');
            return;
        }
        const json = await res.json();
        exitNoteSelectMode();
        notesState.loaded = true;
        await loadNotePages(json.data.page_id);
        showToast(`已整理成筆記：${json.data.title}`, 'success');
    } catch (err) {
        console.error('[NOTES] 整理失敗：', err);
        showToast('整理失敗，請稍後再試', 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = original;
        updateNoteSelectCount();
    }
}

// ============================================================
// 初始化
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    const notesBtn = noteEl('notes-btn');
    if (notesBtn) notesBtn.addEventListener('click', showNotesView);

    const selectBtn = noteEl('note-select-btn');
    if (selectBtn) {
        selectBtn.addEventListener('click', () => {
            if (notesState.selecting) exitNoteSelectMode();
            else enterNoteSelectMode();
        });
    }

    const bind = (id, fn) => {
        const el = noteEl(id);
        if (el) el.addEventListener('click', fn);
    };
    bind('note-page-add', createNotePage);
    bind('note-page-rename', renameNotePage);
    bind('note-page-delete', deleteNotePage);
    bind('note-select-cancel', exitNoteSelectMode);
    bind('note-select-confirm', confirmGenerateNote);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && notesState.selecting) exitNoteSelectMode();
    });
});
