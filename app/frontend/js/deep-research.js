/**
 * 深度研究（Deep Research）前端
 * ==========================================================
 * 對應後端 /api/deep-research/*（OpenAI Agents SDK：Web Search / File Search）。
 *
 * 三段流程，全部只活在記憶體裡 —— 重新整理頁面即回到空白狀態，
 * 這是 MVP 刻意的取捨（後端同樣不落地，session 只放記憶體）：
 *   1. 選模型 + 附檔案 + 輸入題目  → POST /runs（multipart，回 SSE）
 *   2. 研究完成後選擇產出          → POST /runs/{sid}/artifacts（回 SSE）
 *   3. 下載                        → GET  /runs/{sid}/artifacts/{kind}（authFetch → Blob）
 *
 * SSE 走 POST + ReadableStream 而非 EventSource，因為 EventSource 帶不了
 * Authorization header；解析方式與 index.js 的聊天串流一致。
 */

// ─── 產出 skill 的前端描述（kind 需與後端 SKILLS 一致）────────────
const DR_SKILL_META = {
    report: {
        label: '研究報告',
        icon: 'file-text',
        desc: '結構化長文：摘要、關鍵數字、分節論述與來源清單。可直接列印成 PDF。',
        cta: '產生研究報告',
    },
    deck: {
        label: '簡報',
        icon: 'presentation',
        desc: '可放映的投影片，一頁一個論點，附講稿（放映時按 N 顯示）。',
        cta: '產生簡報',
    },
};

const drState = {
    config: null,
    model: null,
    /** @type {File[]} */
    files: [],
    sessionId: null,
    running: false,
    /** @type {AbortController|null} */
    abort: null,
    resultMarkdown: '',
    citations: [],
    /** kind → { filename, downloadPath, blobUrl } */
    artifacts: {},
    /** kind → true 表示產生中 */
    generating: {},
    /** kind → 選定的視覺風格 id（報告與簡報可以各選各的） */
    themeByKind: {},
    /** kind → 指定的篇幅（簡報頁數 / 報告小節數） */
    lengthByKind: {},
    /** 預覽視窗目前顯示的 kind；null 表示關閉中 */
    previewKind: null,
    initialized: false,
};

const drEl = (id) => document.getElementById(id);
const drApiBase = () => resolveStockInsightApiBase();


// ============================================================
// 視圖切換（與 index.js 的 showChatView / showExploreView 互斥）
// ============================================================

function showDeepResearchView() {
    if (typeof maybeParkViewportForLeavingChat === 'function') {
        maybeParkViewportForLeavingChat(
            typeof state !== 'undefined' ? state.currentChatId : null
        );
    }
    if (typeof hideExploreView === 'function') hideExploreView();

    drEl('chat-messages').style.display = 'none';
    drEl('project-view').style.display = 'none';
    const main = document.querySelector('.main-content');
    if (main) main.classList.remove('project-view-mode');
    document.querySelector('.chat-input-area').style.display = 'none';
    drEl('deep-research-view').style.display = 'flex';

    const btn = drEl('deep-research-btn');
    if (btn) btn.classList.add('active');

    if (typeof setMainChatTitle === 'function') setMainChatTitle('深度研究');
    ensureDeepResearchInit();
}

function hideDeepResearchView() {
    const view = drEl('deep-research-view');
    if (view) view.style.display = 'none';
    const btn = drEl('deep-research-btn');
    if (btn) btn.classList.remove('active');
}


// ============================================================
// 初始化
// ============================================================

function ensureDeepResearchInit() {
    if (drState.initialized) return;
    drState.initialized = true;

    initDrDropzone();
    initDrQuery();
    initDrModelMenu();
    initDrActions();
    loadDrConfig();
}

async function loadDrConfig() {
    try {
        const res = await authFetch(`${drApiBase()}/deep-research/config`);
        if (!res || !res.ok) throw new Error(`HTTP ${res ? res.status : 'no-response'}`);

        const json = await res.json();
        drState.config = json.data;
        drState.model = json.data.default_model;
        Object.keys(DR_SKILL_META).forEach((kind) => {
            drState.themeByKind[kind] = json.data.default_theme;
            const spec = drLengthSpec(kind);
            if (spec) drState.lengthByKind[kind] = spec.default;
        });

        renderDrModelMenu();
        applyDrModelLabel();
        renderDrAcceptHint();
        updateDrSubmitState();
    } catch (err) {
        const label = drEl('dr-model-label');
        if (label) label.textContent = '模型載入失敗';
        showToast('深度研究設定載入失敗，請重新整理頁面再試。', 'error');
        console.error('[DeepResearch] config 載入失敗：', err);
    }
}

function renderDrAcceptHint() {
    const hint = drEl('dr-accept-hint');
    const cfg = drState.config;
    if (!hint || !cfg) return;
    hint.textContent =
        `支援 ${cfg.accepted_extensions.join('、')}　·　` +
        `最多 ${cfg.max_files} 個檔案，單檔 ${cfg.max_file_mb} MB`;
}


// ============================================================
// 檔案挑選
// ============================================================

function initDrDropzone() {
    const zone = drEl('dr-dropzone');
    const input = drEl('dr-file-input');
    if (!zone || !input) return;

    const openPicker = () => input.click();
    zone.addEventListener('click', openPicker);
    zone.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            openPicker();
        }
    });

    input.addEventListener('change', () => {
        addDrFiles(Array.from(input.files || []));
        input.value = '';      // 同一個檔案再選一次也要能觸發 change
    });

    ['dragenter', 'dragover'].forEach((evt) => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.add('dragover');
        });
    });
    ['dragleave', 'drop'].forEach((evt) => {
        zone.addEventListener(evt, (e) => {
            e.preventDefault();
            zone.classList.remove('dragover');
        });
    });
    zone.addEventListener('drop', (e) => {
        addDrFiles(Array.from((e.dataTransfer && e.dataTransfer.files) || []));
    });
}

function drExtOf(name) {
    const dot = (name || '').lastIndexOf('.');
    return dot < 0 ? '' : name.slice(dot).toLowerCase();
}

function addDrFiles(incoming) {
    const cfg = drState.config;
    if (!cfg || !incoming.length) return;

    const rejected = [];
    for (const file of incoming) {
        if (drState.files.length >= cfg.max_files) {
            rejected.push(`最多只能上傳 ${cfg.max_files} 個檔案`);
            break;
        }
        if (!cfg.accepted_extensions.includes(drExtOf(file.name))) {
            rejected.push(`${file.name}：不支援的格式`);
            continue;
        }
        if (file.size > cfg.max_file_mb * 1024 * 1024) {
            rejected.push(`${file.name}：超過 ${cfg.max_file_mb} MB`);
            continue;
        }
        const duplicated = drState.files.some(
            (f) => f.name === file.name && f.size === file.size
        );
        if (duplicated) continue;

        drState.files.push(file);
    }

    if (rejected.length) showToast(rejected[0], 'warning');
    renderDrFileList();
    updateDrSubmitState();
}

function drFormatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function drIconForFile(name) {
    const ext = drExtOf(name);
    if (['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(ext)) return 'image';
    if (['.xlsx', '.xlsm', '.csv'].includes(ext)) return 'table';
    if (ext === '.pdf') return 'file-type-2';
    return 'file-text';
}

function renderDrFileList() {
    const list = drEl('dr-file-list');
    if (!list) return;

    list.textContent = '';
    drState.files.forEach((file, index) => {
        const li = document.createElement('li');
        li.className = 'dr-file-chip';

        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', drIconForFile(file.name));

        const name = document.createElement('span');
        name.className = 'dr-chip-name';
        name.textContent = file.name;
        name.title = file.name;

        const size = document.createElement('span');
        size.className = 'dr-chip-size';
        size.textContent = drFormatBytes(file.size);

        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'dr-chip-remove';
        remove.setAttribute('aria-label', `移除 ${file.name}`);
        const x = document.createElement('i');
        x.setAttribute('data-lucide', 'x');
        remove.appendChild(x);
        remove.addEventListener('click', (e) => {
            e.stopPropagation();
            drState.files.splice(index, 1);
            renderDrFileList();
            updateDrSubmitState();
        });

        li.append(icon, name, size, remove);
        list.appendChild(li);
    });

    if (window.lucide) lucide.createIcons({ el: list });
}


// ============================================================
// 題目輸入與送出狀態
// ============================================================

function initDrQuery() {
    const textarea = drEl('dr-query');
    if (!textarea) return;

    textarea.addEventListener('input', updateDrSubmitState);
    textarea.addEventListener('keydown', (e) => {
        // Cmd/Ctrl + Enter 直接送出（Enter 保留給換行，研究題目通常是多行）
        if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
            e.preventDefault();
            startDeepResearch();
        }
    });
}

function updateDrSubmitState() {
    const textarea = drEl('dr-query');
    const btn = drEl('dr-submit-btn');
    const counter = drEl('dr-query-count');
    if (!textarea || !btn) return;

    const length = textarea.value.trim().length;
    const max = drState.config ? drState.config.query_max_chars : 4000;

    if (counter) {
        counter.textContent = length ? `${length} / ${max}` : '';
        counter.classList.toggle('over', length > max);
    }

    btn.disabled =
        !drState.config || drState.running || length === 0 || length > max;
}


// ============================================================
// 模型選單
// ============================================================

function initDrModelMenu() {
    const btn = drEl('dr-model-btn');
    const menu = drEl('dr-model-menu');
    if (!btn || !menu) return;

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const open = menu.classList.toggle('hidden') === false;
        btn.setAttribute('aria-expanded', String(open));
    });

    menu.addEventListener('click', (e) => {
        const option = e.target.closest('.dr-model-option');
        if (!option) return;
        drState.model = option.dataset.value;
        applyDrModelLabel();
        syncDrModelSelection();
        menu.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
    });

    document.addEventListener('click', () => {
        menu.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
    });
}

function renderDrModelMenu() {
    const menu = drEl('dr-model-menu');
    if (!menu || !drState.config) return;

    menu.textContent = '';
    drState.config.models.forEach((model) => {
        const li = document.createElement('li');
        li.className = 'dr-model-option';
        li.setAttribute('role', 'option');
        li.setAttribute('tabindex', '0');
        li.dataset.value = model.id;

        const title = document.createElement('span');
        title.className = 'dr-model-option-title';
        title.textContent = model.label;

        const desc = document.createElement('span');
        desc.className = 'dr-model-option-desc';
        desc.textContent = model.description;

        li.append(title, desc);
        menu.appendChild(li);
    });

    syncDrModelSelection();
}

function syncDrModelSelection() {
    document.querySelectorAll('#dr-model-menu .dr-model-option').forEach((el) => {
        el.classList.toggle('selected', el.dataset.value === drState.model);
    });
}

function applyDrModelLabel() {
    const label = drEl('dr-model-label');
    if (!label || !drState.config) return;
    const found = drState.config.models.find((m) => m.id === drState.model);
    label.textContent = found ? found.label : drState.model;
}


// ============================================================
// 進度列
// ============================================================

function drClearProgress() {
    const list = drEl('dr-progress');
    if (list) list.textContent = '';
}

/** 新增一列進度；回傳該列元素，之後可用 drFinishStep() 標記完成。 */
function drAddStep(text, { spinning = true } = {}) {
    const list = drEl('dr-progress');
    if (!list) return null;

    const li = document.createElement('li');
    li.className = 'dr-step';

    const icon = document.createElement('span');
    icon.className = 'dr-step-icon';
    if (spinning) {
        const spinner = makeSvgSpinner(14);
        spinner.classList.add('dr-spinner');
        icon.appendChild(spinner);
    } else {
        icon.appendChild(makeSvgCheck(14));
    }

    const label = document.createElement('span');
    label.textContent = text;

    li.append(icon, label);
    list.appendChild(li);
    return li;
}

function drFinishStep(li) {
    if (!li || li.classList.contains('done')) return;
    li.classList.add('done');
    const icon = li.querySelector('.dr-step-icon');
    if (icon) {
        icon.textContent = '';
        icon.appendChild(makeSvgCheck(14));
    }
}

function drFinishAllSteps() {
    document.querySelectorAll('#dr-progress .dr-step:not(.done)').forEach(drFinishStep);
}


// ============================================================
// 執行研究
// ============================================================

async function startDeepResearch() {
    if (drState.running) return;

    const textarea = drEl('dr-query');
    const query = (textarea.value || '').trim();
    if (!query || !drState.config) return;

    // 進入執行畫面
    drState.running = true;
    drState.sessionId = null;
    drState.resultMarkdown = '';
    drState.citations = [];
    drReleaseArtifacts();
    drState.artifacts = {};
    drState.generating = {};

    drEl('dr-composer').classList.add('hidden');
    drEl('dr-run').classList.remove('hidden');
    drEl('dr-run-title').textContent = query;
    drEl('dr-run-meta').textContent =
        `${drState.model}　·　${drState.files.length ? `${drState.files.length} 個附件` : '未附檔案'}`;
    drEl('dr-error').classList.add('hidden');
    drEl('dr-error').textContent = '';
    drEl('dr-result').textContent = '';
    drEl('dr-citations').classList.add('hidden');
    drEl('dr-skills').classList.add('hidden');
    drEl('dr-copy-btn').classList.add('hidden');
    drClearProgress();
    updateDrSubmitState();

    const form = new FormData();
    form.append('query', query);
    form.append('model', drState.model);
    drState.files.forEach((file) => form.append('files', file, file.name));

    const controller = new AbortController();
    drState.abort = controller;

    const uploadStep = drAddStep(
        drState.files.length ? '上傳檔案…' : '建立研究任務…'
    );

    /** 後端 tool 事件的 id → 進度列（同一種工具可能並行多次） */
    const toolSteps = {};
    let statusStep = null;
    let writingStep = null;
    let renderScheduled = false;

    const scheduleResultRender = () => {
        if (renderScheduled) return;
        renderScheduled = true;
        requestAnimationFrame(() => {
            renderScheduled = false;
            applyMarkdown(drEl('dr-result'), drState.resultMarkdown);
        });
    };

    try {
        const res = await authFetch(`${drApiBase()}/deep-research/runs`, {
            method: 'POST',
            body: form,
            signal: controller.signal,
        });

        if (!res) return;                       // authFetch 已導向 /login
        if (!res.ok) {
            throw new Error(await drParseError(res));
        }
        if (!res.body) throw new Error('瀏覽器不支援 Streaming');

        drFinishStep(uploadStep);

        await drConsumeSse(res, (event, payload) => {
            switch (event) {
                case 'session':
                    drState.sessionId = payload.session_id;
                    break;

                case 'status':
                    if (statusStep) drFinishStep(statusStep);
                    statusStep = drAddStep(payload.text);
                    break;

                case 'warning':
                    (payload.messages || []).forEach((m) => showToast(m, 'warning', 6000));
                    break;

                case 'sources_ready':
                    drRenderSourceManifest(payload.sources || []);
                    break;

                case 'tool_start': {
                    const step = drAddStep(`${payload.tool}…`);
                    toolSteps[payload.id || payload.tool] = step;
                    break;
                }

                case 'tool_done': {
                    const key = payload.id || payload.tool;
                    drFinishStep(toolSteps[key]);
                    delete toolSteps[key];
                    break;
                }

                case 'thinking':
                    // 推理事件很密集，只在沒有其他進行中步驟時才補一列
                    if (!document.querySelector('#dr-progress .dr-step:not(.done)')) {
                        statusStep = drAddStep(payload.text || '推理中…');
                    }
                    break;

                case 'writing':
                    drFinishAllSteps();
                    writingStep = drAddStep(payload.text || '整理研究結果中…');
                    break;

                case 'delta':
                    drState.resultMarkdown += payload.text || '';
                    scheduleResultRender();
                    break;

                case 'done':
                    drFinishAllSteps();
                    if (writingStep) drFinishStep(writingStep);
                    drState.resultMarkdown = payload.markdown || drState.resultMarkdown;
                    drState.citations = payload.citations || [];
                    applyMarkdown(drEl('dr-result'), drState.resultMarkdown);
                    drRenderCitations();
                    drRenderSkillCards();
                    drEl('dr-copy-btn').classList.remove('hidden');
                    drEl('dr-run-meta').textContent =
                        `${drState.model}　·　` +
                        `耗時 ${Math.round((payload.elapsed_ms || 0) / 1000)} 秒　·　` +
                        `${(payload.tools_used || []).join('、') || '未使用工具'}`;
                    break;

                case 'error':
                    throw new Error(payload.message || '研究執行失敗');

                default:
                    break;
            }
        });
    } catch (err) {
        if (err && err.name === 'AbortError') return;
        drFinishAllSteps();
        drShowError(err && err.message ? err.message : String(err));
    } finally {
        drState.running = false;
        drState.abort = null;
        updateDrSubmitState();
    }
}

/** 讀完 SSE 串流，逐個事件回呼；解析方式與 index.js 的聊天串流一致。 */
async function drConsumeSse(response, onEvent) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });

            const parts = buffer.split('\n\n');
            buffer = parts.pop();

            for (const part of parts) {
                if (part.startsWith(':')) continue;      // 心跳
                const eventMatch = part.match(/^event:\s*(\w+)/m);
                const dataMatch = part.match(/^data:\s*(.+)/ms);
                if (!eventMatch || !dataMatch) continue;

                let payload;
                try {
                    payload = JSON.parse(dataMatch[1]);
                } catch {
                    continue;
                }
                // onEvent 收到 error 事件會 throw，讓呼叫端統一處理
                onEvent(eventMatch[1], payload);
            }
        }
    } finally {
        // onEvent 中途拋錯時也要放掉底層連線
        reader.cancel().catch(() => {});
    }
}

async function drParseError(res) {
    try {
        const body = await res.json();
        if (typeof body.detail === 'string') return body.detail;
        if (body.detail) return JSON.stringify(body.detail);
    } catch {
        /* 非 JSON 錯誤內容 */
    }
    return `HTTP ${res.status}`;
}

function drShowError(message) {
    const box = drEl('dr-error');
    if (!box) return;
    box.textContent = message;
    box.classList.remove('hidden');
}

const DR_CHANNEL_LABEL = {
    file_search: '文件檢索',
    spreadsheet: '表格轉文字',
    image: '圖片辨識',
    skipped: '已略過',
};

function drRenderSourceManifest(sources) {
    if (!sources.length) return;
    const summary = sources
        .map((s) => `${s.name}（${DR_CHANNEL_LABEL[s.channel] || s.channel}）`)
        .join('、');
    const step = drAddStep(`已讀取：${summary}`, { spinning: false });
    if (step) step.classList.add('done');
}


// ============================================================
// 引用來源
// ============================================================

function drRenderCitations() {
    const box = drEl('dr-citations');
    if (!box) return;

    box.textContent = '';
    if (!drState.citations.length) {
        box.classList.add('hidden');
        return;
    }

    const heading = document.createElement('h4');
    heading.textContent = `引用來源（${drState.citations.length}）`;

    const list = document.createElement('ul');
    list.className = 'dr-citation-list';

    drState.citations.forEach((cite, index) => {
        const li = document.createElement('li');
        const link = document.createElement('a');
        link.href = cite.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';

        const num = document.createElement('span');
        num.className = 'dr-cite-num';
        num.textContent = String(index + 1).padStart(2, '0');

        const title = document.createElement('span');
        title.className = 'dr-cite-title';
        title.textContent = cite.title || cite.url;
        title.title = cite.url;

        link.append(num, title);
        li.appendChild(link);
        list.appendChild(li);
    });

    box.append(heading, list);
    box.classList.remove('hidden');
}


// ============================================================
// 產出 skill：報告 / 簡報
// ============================================================

function drRenderSkillCards() {
    const wrap = drEl('dr-skills');
    const container = drEl('dr-skill-cards');
    if (!wrap || !container) return;

    container.textContent = '';
    Object.entries(DR_SKILL_META).forEach(([kind, meta]) => {
        container.appendChild(drBuildSkillCard(kind, meta));
    });

    wrap.classList.remove('hidden');
    if (window.lucide) lucide.createIcons({ el: wrap });
}

function drBuildSkillCard(kind, meta) {
    const card = document.createElement('div');
    card.className = 'dr-skill-card';
    card.dataset.kind = kind;

    const head = document.createElement('div');
    head.className = 'dr-skill-head';
    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', meta.icon);
    const title = document.createElement('span');
    title.className = 'dr-skill-title';
    title.textContent = meta.label;
    head.append(icon, title);

    const desc = document.createElement('p');
    desc.className = 'dr-skill-desc';
    desc.textContent = meta.desc;

    const actions = document.createElement('div');
    actions.className = 'dr-skill-actions';

    card.append(head, desc, drBuildThemePicker(kind), drBuildLengthPicker(kind), actions);
    drPaintSkillActions(kind, card);   // 按鈕內容由狀態決定，統一在這裡畫
    return card;
}

/**
 * 視覺風格選擇列：色票 + 名稱，選了之後才按產生。
 * 報告與簡報各自獨立記憶，因為兩者適合的風格未必相同
 * （例如簡報常用暗色投影，報告多半要印出來）。
 */
function drBuildThemePicker(kind) {
    const wrap = document.createElement('div');
    wrap.className = 'dr-theme-picker';
    wrap.dataset.kind = kind;

    const label = document.createElement('span');
    label.className = 'dr-theme-label';
    label.textContent = '風格';
    wrap.appendChild(label);

    const themes = (drState.config && drState.config.themes) || [];
    themes.forEach((theme) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'dr-theme-chip';
        chip.dataset.theme = theme.id;
        chip.title = theme.description;
        chip.setAttribute('aria-pressed', String(drState.themeByKind[kind] === theme.id));
        if (drState.themeByKind[kind] === theme.id) chip.classList.add('selected');

        const swatch = document.createElement('span');
        swatch.className = 'dr-theme-swatch';
        // 直接用主題自己的底色與強調色當預覽，不另外維護一份縮圖
        swatch.style.background = theme.surface;
        swatch.style.borderColor = theme.ink;
        const dot = document.createElement('span');
        dot.className = 'dr-theme-dot';
        dot.style.background = theme.swatch;
        swatch.appendChild(dot);

        const name = document.createElement('span');
        name.textContent = theme.label;

        chip.append(swatch, name);
        chip.addEventListener('click', () => {
            drState.themeByKind[kind] = theme.id;
            wrap.querySelectorAll('.dr-theme-chip').forEach((el) => {
                const on = el.dataset.theme === theme.id;
                el.classList.toggle('selected', on);
                el.setAttribute('aria-pressed', String(on));
            });
        });
        wrap.appendChild(chip);
    });

    return wrap;
}

function drLengthSpec(kind) {
    const specs = (drState.config && drState.config.length_specs) || {};
    return specs[kind] || null;
}

/**
 * 篇幅輸入：簡報填頁數、報告填小節數。
 * 數字只會寫進 skill 的 prompt，後端不裁切產出 —— 模型偶爾會差一頁，
 * 那比硬砍掉一頁有內容的投影片好，因此產生中的狀態文字寫的是「約 N 頁」。
 */
function drBuildLengthPicker(kind) {
    const spec = drLengthSpec(kind);
    const wrap = document.createElement('div');
    wrap.className = 'dr-length-picker';
    wrap.dataset.kind = kind;
    if (!spec) return wrap;   // 後端沒給這個 skill 的範圍就整列不顯示

    const label = document.createElement('label');
    label.className = 'dr-theme-label';
    label.textContent = spec.label;
    label.htmlFor = `dr-length-${kind}`;

    const input = document.createElement('input');
    input.type = 'number';
    input.id = `dr-length-${kind}`;
    input.className = 'dr-length-input';
    input.min = String(spec.min);
    input.max = String(spec.max);
    input.step = '1';
    input.value = String(drState.lengthByKind[kind] ?? spec.default);

    const hint = document.createElement('span');
    hint.className = 'dr-length-hint';
    hint.textContent = `${spec.min}–${spec.max}${spec.unit}${spec.hint ? `，${spec.hint}` : ''}`;

    // clamp 在 change（失焦／按上下鍵）而非 input，否則打「1」要接著打「2」
    // 湊成 12 時，第一個字元就被彈成下限了。
    const commit = () => {
        const raw = parseInt(input.value, 10);
        const value = Number.isNaN(raw)
            ? spec.default
            : Math.max(spec.min, Math.min(spec.max, raw));
        drState.lengthByKind[kind] = value;
        input.value = String(value);
    };
    input.addEventListener('change', commit);
    input.addEventListener('blur', commit);

    label.appendChild(input);
    wrap.append(label, hint);
    return wrap;
}

/**
 * 依 drState 重繪某張 skill 卡片的按鈕區（idle / 產生中 / 已完成）。
 * 建立卡片時尚未掛進 DOM，因此允許把卡片元素直接傳進來。
 */
function drPaintSkillActions(kind, cardEl) {
    const card =
        cardEl || document.querySelector(`.dr-skill-card[data-kind="${kind}"]`);
    if (!card) return;
    const actions = card.querySelector('.dr-skill-actions');
    if (!actions) return;

    const meta = DR_SKILL_META[kind];
    actions.textContent = '';

    if (drState.generating[kind]) {
        const status = document.createElement('span');
        status.className = 'dr-skill-status';
        const spinner = makeSvgSpinner(13);
        spinner.classList.add('dr-spinner');
        const themes = (drState.config && drState.config.themes) || [];
        const picked = themes.find((t) => t.id === drState.themeByKind[kind]);
        const spec = drLengthSpec(kind);
        const size = spec
            ? `、約 ${drState.lengthByKind[kind] ?? spec.default}${spec.unit}`
            : '';
        const text = document.createElement('span');
        text.textContent =
            `${meta.label}產生中（${picked ? picked.label : '預設'}風格${size}），約需 30–90 秒…`;
        status.append(spinner, text);
        actions.appendChild(status);
        return;
    }

    const artifact = drState.artifacts[kind];
    if (artifact) {
        actions.appendChild(
            drMakeButton('download', `下載${meta.label}`, 'dr-submit-btn', () =>
                drDownloadArtifact(kind)
            )
        );
        actions.appendChild(
            drMakeButton('external-link', '預覽', 'dr-ghost-btn', () =>
                drPreviewArtifact(kind)
            )
        );
        actions.appendChild(
            drMakeButton('refresh-cw', '換設定重產', 'dr-ghost-btn', () =>
                drGenerateArtifact(kind)
            )
        );
    } else {
        actions.appendChild(
            drMakeButton('sparkles', meta.cta, 'dr-submit-btn', () =>
                drGenerateArtifact(kind)
            )
        );
    }

    if (window.lucide) lucide.createIcons({ el: actions });
}

function drMakeButton(iconName, text, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className;
    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', iconName);
    const span = document.createElement('span');
    span.textContent = text;
    btn.append(icon, span);
    btn.addEventListener('click', onClick);
    return btn;
}

async function drGenerateArtifact(kind) {
    if (!drState.sessionId || drState.generating[kind]) return;

    drState.generating[kind] = true;
    drPaintSkillActions(kind);

    try {
        const res = await authFetch(
            `${drApiBase()}/deep-research/runs/${drState.sessionId}/artifacts`,
            {
                method: 'POST',
                body: JSON.stringify({
                    kind,
                    theme: drState.themeByKind[kind],
                    length: drState.lengthByKind[kind],
                }),
            }
        );

        if (!res) return;
        if (!res.ok) throw new Error(await drParseError(res));
        if (!res.body) throw new Error('瀏覽器不支援 Streaming');

        let done = false;
        await drConsumeSse(res, (event, payload) => {
            if (event === 'done') {
                done = true;
                drReleaseArtifact(kind);
                drState.artifacts[kind] = {
                    filename: payload.filename,
                    downloadPath: payload.download_path,
                    blobUrl: null,
                };
                showToast(`${DR_SKILL_META[kind].label}已產生：${payload.filename}`, 'success');
            } else if (event === 'error') {
                throw new Error(payload.message || '產生失敗');
            }
        });

        if (!done && !drState.artifacts[kind]) {
            throw new Error('產生過程中連線中斷，請再試一次。');
        }
    } catch (err) {
        showToast(err && err.message ? err.message : String(err), 'error', 7000);
    } finally {
        drState.generating[kind] = false;
        drPaintSkillActions(kind);
    }
}

/** 下載需帶 JWT，因此先 authFetch 取回 Blob，再用 object URL 觸發瀏覽器下載。 */
async function drFetchArtifactBlobUrl(kind) {
    const artifact = drState.artifacts[kind];
    if (!artifact) return null;
    if (artifact.blobUrl) return artifact.blobUrl;

    const origin = drApiBase().replace(/\/api$/, '');
    const res = await authFetch(`${origin}${artifact.downloadPath}`);
    if (!res) return null;
    if (!res.ok) throw new Error(await drParseError(res));

    artifact.blobUrl = URL.createObjectURL(await res.blob());
    return artifact.blobUrl;
}

async function drDownloadArtifact(kind) {
    try {
        const url = await drFetchArtifactBlobUrl(kind);
        if (!url) return;

        const link = document.createElement('a');
        link.href = url;
        link.download = drState.artifacts[kind].filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
    } catch (err) {
        showToast(`下載失敗：${err && err.message ? err.message : err}`, 'error');
    }
}

// ============================================================
// 產出預覽（頁面最上層的 iframe 視窗）
// ============================================================

/**
 * 預覽視窗只綁一次事件。
 *
 * iframe 不加 sandbox 是刻意的：簡報的翻頁、講稿切換都靠產出檔自己的
 * inline script，加了 sandbox 就全部失效，預覽會退化成一張靜態封面。
 * 安全性靠後端把關 —— `templates/common.scrub_html()` 已經把 LLM 產出裡的
 * script / iframe / on* / javascript: 全部拔掉，其餘欄位一律 esc()。
 * 這與「用新分頁開啟同一個檔案」的信任模型完全相同。
 */
function initDrPreviewModal() {
    const modal = drEl('dr-preview-modal');
    if (!modal || modal.dataset.bound) return;
    modal.dataset.bound = '1';

    const close = drEl('dr-preview-close');
    if (close) close.addEventListener('click', drClosePreview);

    // 點背景關閉；點到卡片內部不關
    modal.addEventListener('click', (e) => {
        if (e.target === modal) drClosePreview();
    });

    const download = drEl('dr-preview-download');
    if (download) {
        download.addEventListener('click', () => {
            if (drState.previewKind) drDownloadArtifact(drState.previewKind);
        });
    }

    const newtab = drEl('dr-preview-newtab');
    if (newtab) {
        newtab.addEventListener('click', () => {
            const artifact = drState.artifacts[drState.previewKind];
            if (artifact && artifact.blobUrl) {
                window.open(artifact.blobUrl, '_blank', 'noopener');
            }
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && drState.previewKind) drClosePreview();
    });
}

async function drPreviewArtifact(kind) {
    const modal = drEl('dr-preview-modal');
    const frame = drEl('dr-preview-frame');
    if (!modal || !frame) return;

    try {
        const url = await drFetchArtifactBlobUrl(kind);
        if (!url) return;

        const artifact = drState.artifacts[kind];
        drState.previewKind = kind;
        drEl('dr-preview-title').textContent =
            (artifact && artifact.filename) || DR_SKILL_META[kind].label;

        frame.src = url;
        modal.classList.add('show');
        document.body.style.overflow = 'hidden';

        // 焦點交給 iframe，簡報才收得到方向鍵；順便讓 Esc 在裡面也能關。
        // blob: 繼承本頁 origin，所以拿得到 contentDocument。
        frame.addEventListener(
            'load',
            () => {
                try {
                    frame.contentWindow.focus();
                    frame.contentDocument.addEventListener('keydown', (e) => {
                        if (e.key === 'Escape') drClosePreview();
                    });
                } catch {
                    /* 取不到就算了，右上角還有關閉鈕 */
                }
            },
            { once: true }
        );

        if (window.lucide) lucide.createIcons({ el: modal });
    } catch (err) {
        showToast(`預覽失敗：${err && err.message ? err.message : err}`, 'error');
    }
}

function drClosePreview() {
    const modal = drEl('dr-preview-modal');
    const frame = drEl('dr-preview-frame');
    if (!modal) return;

    modal.classList.remove('show');
    document.body.style.overflow = '';
    drState.previewKind = null;
    // 清掉 src，否則簡報的 rAF／事件監聽會在關閉後繼續跑
    if (frame) frame.removeAttribute('src');
}

function drReleaseArtifact(kind) {
    // 正在預覽這份就先關窗：blobUrl 一 revoke，iframe 只會剩下一片空白
    if (drState.previewKind === kind) drClosePreview();

    const artifact = drState.artifacts[kind];
    if (artifact && artifact.blobUrl) {
        URL.revokeObjectURL(artifact.blobUrl);
        artifact.blobUrl = null;
    }
}

function drReleaseArtifacts() {
    Object.keys(drState.artifacts).forEach(drReleaseArtifact);
}


// ============================================================
// 重設
// ============================================================

function initDrActions() {
    const submit = drEl('dr-submit-btn');
    if (submit) submit.addEventListener('click', startDeepResearch);

    const reset = drEl('dr-reset-btn');
    if (reset) reset.addEventListener('click', resetDeepResearch);

    const copy = drEl('dr-copy-btn');
    if (copy && !copy.dataset.bound) {
        copy.dataset.bound = '1';       // 每次進入深度研究頁都會呼叫，別重複綁
        copy.addEventListener('click', drCopyResult);
    }

    initDrPreviewModal();
}


// ============================================================
// 複製全文
// ============================================================

/**
 * 複製整份研究結果。
 *
 * 複製的是 **Markdown 原文**而非畫面上的純文字：研究結果的價值有一半在
 * 那些行內引用連結，轉成純文字會把 [標題](網址) 的網址整個丟掉。
 * 貼到 Notion／HackMD／Word 也都吃 Markdown。
 */
function drCopyResult() {
    const text = (drState.resultMarkdown || '').trim();
    if (!text) return;

    const btn = drEl('dr-copy-btn');
    copyTextToClipboard(text)
        .then(() => {
            drFlashCopyButton(btn);
            showToast('已複製整份研究結果（Markdown）', 'success');
        })
        .catch((err) => {
            console.error('[DeepResearch] 複製失敗：', err);
            showToast('複製失敗，請改用手動選取。', 'error');
        });
}

/** 按鈕短暫切成「已複製」，兩秒後復原（與聊天訊息的複製鈕一致） */
function drFlashCopyButton(btn) {
    if (!btn) return;
    const label = btn.querySelector('span');
    const icon = btn.querySelector('i');
    if (!label || !icon) return;

    btn.classList.add('copied');
    label.textContent = '已複製';
    icon.setAttribute('data-lucide', 'check');
    if (window.lucide) lucide.createIcons({ el: btn });

    clearTimeout(btn._copyTimer);
    btn._copyTimer = setTimeout(() => {
        btn.classList.remove('copied');
        const back = btn.querySelector('span');
        const backIcon = btn.querySelector('i');
        if (back) back.textContent = '複製全文';
        if (backIcon) backIcon.setAttribute('data-lucide', 'copy');
        if (window.lucide) lucide.createIcons({ el: btn });
    }, 2000);
}

function resetDeepResearch() {
    if (drState.running && drState.abort) drState.abort.abort();

    drReleaseArtifacts();
    if (drState.sessionId) {
        // 主動釋放後端的記憶體 session；失敗也無妨，後端有 TTL
        authFetch(`${drApiBase()}/deep-research/runs/${drState.sessionId}`, {
            method: 'DELETE',
        }).catch(() => {});
    }

    drState.sessionId = null;
    drState.running = false;
    drState.abort = null;
    drState.resultMarkdown = '';
    drState.citations = [];
    drState.artifacts = {};
    drState.generating = {};
    drState.files = [];

    const textarea = drEl('dr-query');
    if (textarea) textarea.value = '';
    renderDrFileList();
    drClearProgress();
    drEl('dr-result').textContent = '';
    drEl('dr-error').classList.add('hidden');
    drEl('dr-citations').classList.add('hidden');
    drEl('dr-skills').classList.add('hidden');
    drEl('dr-copy-btn').classList.add('hidden');
    drEl('dr-run').classList.add('hidden');
    drEl('dr-composer').classList.remove('hidden');
    updateDrSubmitState();
}


// ============================================================
// 掛載
// ============================================================

document.addEventListener('DOMContentLoaded', () => {
    const btn = drEl('deep-research-btn');
    if (!btn) return;

    btn.addEventListener('click', () => {
        showDeepResearchView();
        if (typeof closeSidebarDrawer === 'function') closeSidebarDrawer();
    });
});
