/**
 * 從對話訊息產生報告／簡報
 * =========================
 * 使用者在對話標題列按「產生報告／簡報」→ 勾選最多 5 則訊息（選取 UI 在 notes.js，
 * 與「整理成筆記」共用）→ 這裡的視窗選類型、風格、篇幅 → 產生 → 預覽／下載。
 *
 * 報告／簡報的 skill、風格、篇幅範圍與深度研究頁完全相同（GET /api/deep-research/config），
 * 預覽視窗也共用深度研究頁那一個（deep-research.js 的 openArtifactPreview）。
 *
 * 產出的檔案只在後端記憶體保留一段時間（預設 2 小時），不寫進對話紀錄。
 * 所有文字一律以 textContent 寫入。
 */

const caState = {
    chatId: null,
    /** @type {string[]} */
    messageIds: [],
    kind: 'report',
    /** kind → theme id */
    themeByKind: {},
    /** kind → 篇幅 */
    lengthByKind: {},
    generating: false,
    /** @type {AbortController|null} */
    abort: null,
};

const caEl = (id) => document.getElementById(id);

function caConfig() {
    return typeof drChatConfig !== 'undefined' ? drChatConfig : null;
}

async function caEnsureConfig() {
    if (caConfig()) return caConfig();
    if (typeof loadDeepResearchChatConfig === 'function') await loadDeepResearchChatConfig();
    return caConfig();
}

function caSkillMeta(kind) {
    return (typeof DR_SKILL_META !== 'undefined' && DR_SKILL_META[kind]) || { label: kind, icon: 'file', desc: '' };
}

/** 給 notes.js 的選取列呼叫：開啟設定視窗 */
async function openChatArtifactDialog(chatId, messageIds) {
    const config = await caEnsureConfig();
    if (!config) {
        showToast('報告／簡報設定載入失敗，請重新整理後再試', 'error');
        return;
    }
    caState.chatId = chatId;
    caState.messageIds = messageIds.slice(0, 5);
    Object.keys(config.length_specs || {}).forEach((kind) => {
        if (!caState.themeByKind[kind]) caState.themeByKind[kind] = config.default_theme;
    });

    caEl('chat-artifact-source').textContent =
        `以你選取的 ${caState.messageIds.length} 則訊息為素材。檔案在伺服器保留約 2 小時，請記得下載。`;
    caEl('chat-artifact-status').classList.add('hidden');
    caEl('chat-artifact-result').classList.add('hidden');
    caEl('chat-artifact-result').textContent = '';
    caRenderKinds();
    caRenderOptions();
    caSetGenerating(false);
    caEl('chat-artifact-modal').classList.add('show');
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

function caCloseDialog() {
    caEl('chat-artifact-modal').classList.remove('show');
}

function caRenderKinds() {
    const wrap = caEl('chat-artifact-kinds');
    wrap.textContent = '';
    ['report', 'deck'].forEach((kind) => {
        const meta = caSkillMeta(kind);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chat-artifact-kind';
        btn.setAttribute('role', 'radio');
        btn.setAttribute('aria-checked', String(caState.kind === kind));
        btn.dataset.kind = kind;

        const head = document.createElement('span');
        head.className = 'chat-artifact-kind-head';
        const icon = document.createElement('i');
        icon.setAttribute('data-lucide', meta.icon);
        const title = document.createElement('strong');
        title.textContent = meta.label;
        head.append(icon, title);

        const desc = document.createElement('span');
        desc.className = 'chat-artifact-kind-desc';
        desc.textContent = meta.desc;

        btn.append(head, desc);
        btn.addEventListener('click', () => {
            if (caState.generating) return;
            caState.kind = kind;
            wrap.querySelectorAll('.chat-artifact-kind').forEach((el) => {
                el.setAttribute('aria-checked', String(el.dataset.kind === kind));
            });
            caRenderOptions();
        });
        wrap.appendChild(btn);
    });
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/** 風格與篇幅：外觀與深度研究頁一致（共用 dr-theme-* / dr-length-* 樣式） */
function caRenderOptions() {
    const config = caConfig();
    const wrap = caEl('chat-artifact-options');
    wrap.textContent = '';
    if (!config) return;
    const kind = caState.kind;

    const themes = document.createElement('div');
    themes.className = 'dr-theme-picker';
    const tLabel = document.createElement('span');
    tLabel.className = 'dr-theme-label';
    tLabel.textContent = '風格';
    themes.appendChild(tLabel);
    (config.themes || []).forEach((theme) => {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'dr-theme-chip';
        chip.title = theme.description;
        const on = caState.themeByKind[kind] === theme.id;
        chip.classList.toggle('selected', on);
        chip.setAttribute('aria-pressed', String(on));
        const swatch = document.createElement('span');
        swatch.className = 'dr-theme-swatch';
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
            if (caState.generating) return;
            caState.themeByKind[kind] = theme.id;
            caRenderOptions();
        });
        themes.appendChild(chip);
    });
    wrap.appendChild(themes);

    const spec = (config.length_specs || {})[kind];
    if (!spec) return;
    const lengthWrap = document.createElement('div');
    lengthWrap.className = 'dr-length-picker';
    const label = document.createElement('label');
    label.className = 'dr-theme-label';
    label.textContent = spec.label;
    label.htmlFor = 'chat-artifact-length';
    const input = document.createElement('input');
    input.type = 'number';
    input.id = 'chat-artifact-length';
    input.className = 'dr-length-input';
    input.min = String(spec.min);
    input.max = String(spec.max);
    input.step = '1';
    input.value = String(caState.lengthByKind[kind] ?? spec.default);
    const commit = () => {
        const raw = parseInt(input.value, 10);
        const value = Number.isNaN(raw) ? spec.default : Math.max(spec.min, Math.min(spec.max, raw));
        caState.lengthByKind[kind] = value;
        input.value = String(value);
    };
    input.addEventListener('change', commit);
    input.addEventListener('blur', commit);
    const hint = document.createElement('span');
    hint.className = 'dr-length-hint';
    hint.textContent = `${spec.min}–${spec.max}${spec.unit}${spec.hint ? `，${spec.hint}` : ''}`;
    label.appendChild(input);
    lengthWrap.append(label, hint);
    wrap.appendChild(lengthWrap);
}

function caSetGenerating(on, text) {
    caState.generating = on;
    const submit = caEl('chat-artifact-submit');
    submit.disabled = on;
    submit.textContent = on ? '產生中…' : '產生';
    const status = caEl('chat-artifact-status');
    status.textContent = '';
    if (on) {
        status.classList.remove('hidden');
        if (typeof makeSvgSpinner === 'function') {
            const sp = makeSvgSpinner(14);
            sp.classList.add('dr-spinner');
            status.appendChild(sp);
        }
        const span = document.createElement('span');
        span.textContent = text || '準備中…';
        status.appendChild(span);
    } else {
        status.classList.add('hidden');
    }
}

function caApiOrigin() {
    return String(state.apiBase || '').replace(/\/api$/, '');
}

async function caSubmit() {
    if (caState.generating || !caState.chatId || !caState.messageIds.length) return;
    const kind = caState.kind;
    const meta = caSkillMeta(kind);
    const config = caConfig();
    const spec = config && config.length_specs ? config.length_specs[kind] : null;
    const length = caState.lengthByKind[kind] ?? (spec ? spec.default : undefined);
    caEl('chat-artifact-result').classList.add('hidden');
    caSetGenerating(true, `${meta.label}產生中，約需 30–90 秒…`);

    const chatId = caState.chatId;
    try {
        const res = await authFetch(`${state.apiBase}/chat/artifacts`, {
            method: 'POST',
            body: JSON.stringify({
                chat_id: chatId,
                message_ids: caState.messageIds,
                kind,
                theme: caState.themeByKind[kind],
                length,
            }),
        });
        if (!res) return;
        if (!res.ok) {
            if (res.status === 429 && typeof refreshUserQuotaFromServer === 'function') refreshUserQuotaFromServer();
            const detail = typeof parseApiErrorBody === 'function' ? await parseApiErrorBody(res) : null;
            const mapped = typeof mapChatMessagesErrorForDisplay === 'function'
                ? mapChatMessagesErrorForDisplay(res.status, detail)
                : { text: `HTTP ${res.status}` };
            throw new Error(mapped.text);
        }

        let done = null;
        await caConsumeSse(res, (event, payload) => {
            if (event === 'status') caSetGenerating(true, payload.text);
            else if (event === 'notice') showToast((payload.messages || []).join('\n'), 'info');
            else if (event === 'error') throw new Error(payload.message || '產生失敗');
            else if (event === 'done') done = payload;
        });
        if (!done) throw new Error('產生過程中連線中斷，請再試一次。');

        const result = {
            kind,
            label: done.label || meta.label,
            title: done.title || '',
            formats: (done.formats || []).map((f) => ({ ...f, blobUrl: null })),
        };
        caSetGenerating(false);
        caRenderResult(caEl('chat-artifact-result'), result);
        caEl('chat-artifact-result').classList.remove('hidden');
        caAppendChatCard(chatId, result);
        showToast(`${result.label}已產生`, 'success');
        if (typeof refreshUserQuotaFromServer === 'function') refreshUserQuotaFromServer();
    } catch (err) {
        caSetGenerating(false);
        showToast(err && err.message ? err.message : String(err), 'error', 7000);
    }
}

async function caConsumeSse(response, onEvent) {
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
            const ev = part.match(/^event:\s*(\w+)/m);
            const data = part.match(/^data:\s*(.+)/ms);
            if (!ev || !data) continue;
            let payload;
            try { payload = JSON.parse(data[1]); } catch { continue; }
            onEvent(ev[1], payload);
        }
    }
}

async function caBlobUrl(format) {
    if (format.blobUrl) return format.blobUrl;
    const res = await authFetch(`${caApiOrigin()}${format.download_path}`);
    if (!res) return null;
    if (!res.ok) {
        const detail = typeof parseApiErrorBody === 'function' ? await parseApiErrorBody(res) : null;
        throw new Error(typeof detail === 'string' ? detail : `HTTP ${res.status}`);
    }
    format.blobUrl = URL.createObjectURL(await res.blob());
    return format.blobUrl;
}

async function caDownload(format) {
    try {
        const url = await caBlobUrl(format);
        if (!url) return;
        const link = document.createElement('a');
        link.href = url;
        link.download = format.filename;
        document.body.appendChild(link);
        link.click();
        link.remove();
    } catch (err) {
        showToast(`下載失敗：${err.message || err}（檔案約保留 2 小時，逾時請重新產生）`, 'error');
    }
}

async function caPreview(result) {
    const html = result.formats.find((f) => f.fmt === 'html');
    if (!html) return;
    try {
        const url = await caBlobUrl(html);
        if (!url) return;
        const primary = result.formats[0];
        caCloseDialog();
        openArtifactPreview({
            title: html.filename || result.label,
            htmlUrl: url,
            primaryLabel: primary ? primary.label : '',
            onDownload: () => caDownload(primary),
        });
    } catch (err) {
        showToast(`預覽失敗：${err.message || err}`, 'error');
    }
}

function caButton(icon, text, cls, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = cls;
    const i = document.createElement('i');
    i.setAttribute('data-lucide', icon);
    const span = document.createElement('span');
    span.textContent = text;
    btn.append(i, span);
    btn.addEventListener('click', onClick);
    return btn;
}

/** 產出的操作列：預覽、各格式下載（主要格式用主要按鈕） */
function caRenderResult(container, result) {
    container.textContent = '';
    const head = document.createElement('div');
    head.className = 'chat-artifact-result-head';
    const icon = document.createElement('i');
    icon.setAttribute('data-lucide', caSkillMeta(result.kind).icon);
    const title = document.createElement('strong');
    title.textContent = `${result.label}：${result.title}`;
    head.append(icon, title);

    const actions = document.createElement('div');
    actions.className = 'chat-artifact-actions';
    if (result.formats.some((f) => f.fmt === 'html')) {
        actions.appendChild(caButton('eye', '預覽', 'dr-ghost-btn', () => caPreview(result)));
    }
    result.formats.forEach((f, i) => {
        actions.appendChild(caButton('download', `下載 ${f.label}`, i === 0 ? 'dr-submit-btn' : 'dr-ghost-btn', () => caDownload(f)));
    });
    container.append(head, actions);
    if (typeof lucide !== 'undefined') lucide.createIcons();
}

/** 在對話裡留一張卡片，關掉視窗後還拿得到檔案（不寫進對話紀錄，重新整理即消失） */
function caAppendChatCard(chatId, result) {
    if (String(state.currentChatId) !== String(chatId)) return;
    const container = document.getElementById('chat-messages');
    if (!container) return;
    const card = document.createElement('div');
    card.className = 'chat-artifact-msg';
    const body = document.createElement('div');
    caRenderResult(body, result);
    const note = document.createElement('p');
    note.className = 'chat-artifact-note';
    note.textContent = '檔案在伺服器保留約 2 小時；這張卡片不會存進對話紀錄，重新整理後就不見了。';
    card.append(body, note);
    container.appendChild(card);
    if (typeof scrollToBottom === 'function') scrollToBottom();
}

document.addEventListener('DOMContentLoaded', () => {
    const modal = caEl('chat-artifact-modal');
    if (!modal) return;
    caEl('chat-artifact-close').addEventListener('click', caCloseDialog);
    caEl('chat-artifact-cancel').addEventListener('click', caCloseDialog);
    caEl('chat-artifact-submit').addEventListener('click', caSubmit);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && modal.classList.contains('show')) caCloseDialog();
    });
});
