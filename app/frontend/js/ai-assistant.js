(() => {
  const state = {
    open: false,
    context: null,
    // 每個頁面上下文一條獨立對話（general / portfolio / news:<id>）：
    // key → { chatId, box }。換頁只切換顯示，不會把 A 新聞的歷史帶進投資組合的提問。
    threads: new Map(),
    activeKey: 'general',
    streaming: false,
    files: [],
    uploadConfig: null,
  };

  // 後端拿不到設定時的保守預設，與 deep_research/config.py 預設值一致
  const FALLBACK_UPLOAD_CONFIG = {
    max_files: 10,
    max_images: 4,
    max_file_mb: 20,
    max_image_mb: 10,
    max_total_mb: 50,
    accepted_extensions: ['.csv', '.docx', '.gif', '.html', '.jpeg', '.jpg', '.json', '.md', '.pdf', '.png', '.pptx', '.txt', '.webp', '.xlsm', '.xlsx'],
  };
  const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
  const LEGACY_HINTS = {
    '.doc': '舊版 Word（.doc）無法解析，請另存為 .docx 後再上傳。',
    '.xls': '舊版 Excel（.xls）無法解析，請另存為 .xlsx 後再上傳。',
    '.ppt': '舊版 PowerPoint（.ppt）無法解析，請另存為 .pptx 後再上傳。',
  };

  function extOf(name) {
    const dot = (name || '').lastIndexOf('.');
    return dot >= 0 ? name.slice(dot).toLowerCase() : '';
  }

  /** 與深度研究共用同一份上傳限制（GET /deep-research/config），只抓一次 */
  async function loadUploadConfig() {
    if (state.uploadConfig) return state.uploadConfig;
    try {
      const res = await window.authFetch(`${window.state.apiBase}/deep-research/config`);
      if (res && res.ok) {
        const json = await res.json();
        state.uploadConfig = { ...FALLBACK_UPLOAD_CONFIG, ...(json.data || {}) };
      }
    } catch (_) {}
    if (!state.uploadConfig) return FALLBACK_UPLOAD_CONFIG;
    const fileInput = document.querySelector('.ai-assistant-window [data-role="file-input"]');
    if (fileInput) fileInput.accept = state.uploadConfig.accepted_extensions.join(',');
    return state.uploadConfig;
  }

  /** 前端先擋一輪（後端 read_uploads 仍會完整驗證）；回傳被拒的說明 */
  async function addFiles(fileList) {
    const cfg = await loadUploadConfig();
    const accepted = new Set(cfg.accepted_extensions);
    const errors = [];
    const next = [...state.files];
    let totalBytes = next.reduce((sum, f) => sum + f.size, 0);
    let images = next.filter((f) => IMAGE_EXTS.has(extOf(f.name))).length;

    for (const file of Array.from(fileList || [])) {
      const ext = extOf(file.name);
      const isImage = IMAGE_EXTS.has(ext);
      const limitMb = isImage ? cfg.max_image_mb : cfg.max_file_mb;
      if (LEGACY_HINTS[ext]) { errors.push(LEGACY_HINTS[ext]); continue; }
      if (!accepted.has(ext)) { errors.push(`不支援的格式：${file.name}`); continue; }
      if (!file.size) { errors.push(`${file.name} 是空檔案`); continue; }
      if (file.size > limitMb * 1024 * 1024) { errors.push(`${file.name} 超過 ${limitMb} MB`); continue; }
      if (next.length >= cfg.max_files) { errors.push(`一次最多 ${cfg.max_files} 個檔案`); break; }
      if (isImage && images >= cfg.max_images) { errors.push(`一次最多 ${cfg.max_images} 張圖片`); continue; }
      if (totalBytes + file.size > cfg.max_total_mb * 1024 * 1024) {
        errors.push(`檔案加總超過 ${cfg.max_total_mb} MB`);
        continue;
      }
      if (next.some((f) => f.name === file.name && f.size === file.size)) continue;
      next.push(file);
      totalBytes += file.size;
      if (isImage) images += 1;
    }
    state.files = next;
    renderFiles(errors);
  }

  const EMPTY_STATES = {
    general: {
      title: '開始詢問市場洞察',
      body: '可分析新聞事件、基本面、技術面或個人資產配置，也能附上財報、截圖或試算表。',
      prompts: ['近期台股盤勢的主要風險是什麼？', '幫我比較台積電與聯發科的基本面'],
    },
    selected_news: {
      title: '針對這篇新聞提問',
      body: '小助手會讀取資料庫中的完整原文，再查證基本面與技術面。',
      prompts: ['這則新聞會影響哪些個股與產業鏈？', '從基本面和技術面看，現在該怎麼操作？'],
    },
    portfolio: {
      title: '分析你的資產配置',
      body: '小助手會讀取你的持股、觀察清單、現金與風險偏好，以及近 90 天相關新聞。',
      prompts: ['目前時事對我的持股有哪些風險？', '幫我規劃短期、中期、長期的配置調整'],
    },
  };

  function modeOf(context) {
    return context && EMPTY_STATES[context.mode] ? context.mode : 'general';
  }

  function threadKey(context) {
    const mode = modeOf(context);
    if (mode === 'selected_news') return `news:${context.news_id}`;
    return mode;
  }

  function buildEmptyState(mode) {
    const spec = EMPTY_STATES[mode];
    const box = el('div', 'ai-assistant-empty');
    box.append(el('strong', null, spec.title), el('span', null, spec.body));
    const list = el('div', 'ai-assistant-suggestions');
    spec.prompts.forEach((text) => {
      const btn = el('button', 'ai-assistant-suggestion', text);
      btn.type = 'button';
      btn.addEventListener('click', () => {
        const input = document.querySelector('.ai-assistant-window [data-role="input"]');
        if (!input || state.streaming) return;
        input.value = text;
        send();
      });
      list.append(btn);
    });
    box.append(list);
    return box;
  }

  function createThread(mode) {
    const box = el('div', 'ai-assistant-thread');
    box.append(buildEmptyState(mode));
    return { chatId: null, box, mode };
  }

  /** 切到目前上下文對應的對話（沒有就建一條），其他對話隱藏 */
  function switchThread() {
    const key = threadKey(state.context);
    let thread = state.threads.get(key);
    if (!thread) {
      thread = createThread(modeOf(state.context));
      state.threads.set(key, thread);
    }
    state.activeKey = key;
    const container = document.querySelector('.ai-assistant-messages');
    if (!container) return thread;
    if (thread.box.parentNode !== container) container.append(thread.box);
    state.threads.forEach((t, k) => { t.box.hidden = k !== key; });
    container.scrollTop = container.scrollHeight;
    return thread;
  }

  function activeThread() {
    return state.threads.get(state.activeKey) || switchThread();
  }

  /** 「新對話」：丟掉目前上下文的對話紀錄，下次提問會開新的 chat */
  function resetActiveThread() {
    if (state.streaming) return;
    const old = state.threads.get(state.activeKey);
    if (old) old.box.remove();
    state.threads.delete(state.activeKey);
    switchThread();
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function build() {
    const fab = el('button', 'ai-assistant-fab');
    fab.type = 'button';
    fab.title = '開啟金融 AI 小助手';
    fab.setAttribute('aria-label', '開啟金融 AI 小助手');
    fab.innerHTML = '<i data-lucide="sparkles"></i>'; 

    const win = el('section', 'ai-assistant-window');
    win.setAttribute('role', 'dialog');
    win.setAttribute('aria-label', '金融 AI 小助手');

    win.innerHTML = `
      <header class="ai-assistant-header">
        <div class="ai-assistant-icon"><i data-lucide="badge-dollar-sign"></i></div>
        <div class="ai-assistant-title"><strong>金融洞察小助手</strong><span data-role="page-label">全站分析模式</span></div>
        <div class="ai-assistant-actions">
          <button type="button" class="ai-assistant-reset" title="新對話" aria-label="開始新對話" data-role="reset"><i data-lucide="rotate-ccw"></i></button>
          <button type="button" class="ai-assistant-minimize" title="最小化" aria-label="最小化小助手"><i data-lucide="minus"></i></button>
          <button type="button" class="ai-assistant-close" title="關閉" aria-label="關閉小助手"><i data-lucide="x"></i></button>
        </div>
      </header>
      <div class="ai-assistant-context" data-role="context"></div>
      <div class="ai-assistant-messages" data-role="messages" aria-live="polite"></div>
      <footer class="ai-assistant-composer">
        <div class="ai-assistant-files" data-role="files"></div>
        <div class="ai-assistant-file-errors" data-role="file-errors" role="alert"></div>
        <div class="ai-assistant-input-row">
          <button type="button" class="ai-assistant-tool" title="選擇圖片或文件" aria-label="附加圖片或文件" data-role="attach"><i data-lucide="paperclip"></i></button>
          <textarea rows="1" maxlength="2000" aria-label="輸入問題" autocomplete="off" placeholder="問目前新聞、持倉或技術面…" data-role="input"></textarea>
          <button type="button" class="ai-assistant-send" title="送出" aria-label="送出問題" data-role="send"><i data-lucide="send"></i></button>
        </div>
        <input type="file" multiple accept="${FALLBACK_UPLOAD_CONFIG.accepted_extensions.join(',')}" hidden data-role="file-input">
      </footer>`;

    document.body.append(fab, win);

    const close = win.querySelector('.ai-assistant-close');
    const minimize = win.querySelector('.ai-assistant-minimize');
    fab.addEventListener('click', () => toggle(true));
    close.addEventListener('click', () => toggle(false));
    minimize.addEventListener('click', () => toggle(false));
    win.querySelector('[data-role="reset"]').addEventListener('click', resetActiveThread);

    win.querySelector('[data-role="attach"]').addEventListener('click', () => win.querySelector('[data-role="file-input"]').click());
    win.querySelector('[data-role="file-input"]').addEventListener('change', async (event) => {
      const picked = Array.from(event.target.files || []);
      event.target.value = '';
      await addFiles(picked);
    });

    // 拖曳檔案到小視窗也能附加
    win.addEventListener('dragover', (event) => {
      if (!event.dataTransfer || !Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault();
      win.classList.add('drag-over');
    });
    win.addEventListener('dragleave', (event) => {
      if (!win.contains(event.relatedTarget)) win.classList.remove('drag-over');
    });
    win.addEventListener('drop', (event) => {
      win.classList.remove('drag-over');
      if (!event.dataTransfer || !event.dataTransfer.files.length) return;
      event.preventDefault();
      addFiles(event.dataTransfer.files);
    });

    // 貼上截圖（例如 K 線圖）直接當附件
    win.querySelector('[data-role="input"]').addEventListener('paste', (event) => {
      const pasted = Array.from(event.clipboardData?.files || []);
      if (!pasted.length) return;
      event.preventDefault();
      addFiles(pasted);
    });

    const input = win.querySelector('[data-role="input"]');
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        send();
      }
    });
    win.querySelector('[data-role="send"]').addEventListener('click', send);

    // market-feed / portfolio 可能在 DOMContentLoaded 前就設好上下文
    renderContext();
    switchThread();
    if (window.lucide) window.lucide.createIcons();
  }

  function toggle(open) {
    const win = document.querySelector('.ai-assistant-window');
    if (!win) return;
    state.open = open;
    win.classList.toggle('open', open);
    // 手機版為全螢幕面板：開啟時隱藏浮動按鈕並鎖住背後頁面捲動（見 ai-assistant.css）
    document.body.classList.toggle('ai-assistant-open', open);
  }

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.open) toggle(false);
  });

  function setContext(context) {
    state.context = context || null;
    renderContext();
    switchThread();
  }

  function clearContext() {
    state.context = null;
    renderContext();
    switchThread();
  }

  function renderContext() {
    const win = document.querySelector('.ai-assistant-window');
    if (!win) return;
    const box = win.querySelector('[data-role="context"]');
    const label = win.querySelector('[data-role="page-label"]');
    box.textContent = '';

    if (!state.context) {
      label.textContent = '全站分析模式';
      return;
    }
    if (state.context.mode === 'selected_news') {
      label.textContent = '新聞原文分析';
      const title = state.context.title ? `已選新聞：${state.context.title}` : '已選新聞';
      const chip = makeChip(title, 'remove');
      chip.title = state.context.title || '';
      box.append(chip);
    } else if (state.context.mode === 'portfolio') {
      label.textContent = '個人資產配置＋近 90 天新聞';
      box.append(makeChip('持倉與觀察清單', 'remove'), makeChip('近 90 天新聞', null));
    }
  }

  function makeChip(text, action) {
    const chip = el('div', 'ai-context-chip');
    chip.append(el('span', 'ai-context-chip-text', text));
    if (action) {
      const btn = el('button', null, '×');
      btn.type = 'button';
      btn.title = '移除上下文';
      btn.setAttribute('aria-label', '移除上下文，回到全站模式');
      btn.addEventListener('click', clearContext);
      chip.append(btn);
    }
    return chip;
  }

  function fileIcon(name) {
    const ext = extOf(name);
    if (IMAGE_EXTS.has(ext)) return 'image';
    if (['.csv', '.xlsx', '.xlsm'].includes(ext)) return 'sheet';
    return 'file-text';
  }

  function renderFiles(errors) {
    const win = document.querySelector('.ai-assistant-window');
    if (!win) return;
    const box = win.querySelector('[data-role="files"]');
    box.textContent = '';
    state.files.forEach((file, index) => {
      const item = el('div', 'ai-assistant-file');
      item.title = file.name;
      const icon = el('i');
      icon.setAttribute('data-lucide', fileIcon(file.name));
      item.append(icon, el('span', null, file.name));
      const remove = el('button', null, '×');
      remove.type = 'button';
      remove.title = '移除檔案';
      remove.setAttribute('aria-label', `移除 ${file.name}`);
      remove.disabled = state.streaming;
      remove.addEventListener('click', () => { state.files.splice(index, 1); renderFiles(); });
      item.append(remove);
      box.append(item);
    });
    const errBox = win.querySelector('[data-role="file-errors"]');
    errBox.textContent = errors && errors.length ? errors.join('；') : '';
    if (window.lucide) window.lucide.createIcons();
  }

  function addUserMessage(thread, text, files) {
    const node = addMessage(thread, 'user', text);
    if (node && files.length) {
      const list = el('div', 'ai-assistant-msg-files');
      files.forEach((f) => list.append(el('span', null, `📎 ${f.name}`)));
      node.append(list);
    }
    return node;
  }

  /** 4xx/5xx 回應轉成可讀訊息（FastAPI 的 detail 可能是字串或驗證錯誤陣列） */
  async function responseError(res, fallback) {
    if (!res) return '無法連線';
    try {
      const json = await res.json();
      if (typeof json.detail === 'string') return json.detail;
      if (Array.isArray(json.detail) && json.detail[0]?.msg) return json.detail[0].msg;
    } catch (_) {}
    return `${fallback}（${res.status}）`;
  }

  function addMessage(thread, role, text) {
    const box = thread.box;
    box.querySelector('.ai-assistant-empty')?.remove();
    const node = el('div', `ai-assistant-message ${role}`);
    if (role === 'assistant' && window.marked && window.DOMPurify) {
      node.innerHTML = window.DOMPurify.sanitize(window.marked.parse(text || ''));
    } else {
      node.textContent = text;
    }
    box.append(node);
    scrollIfActive(thread);
    return node;
  }

  function scrollIfActive(thread) {
    if (state.threads.get(state.activeKey) !== thread) return;
    const container = document.querySelector('.ai-assistant-messages');
    if (container) container.scrollTop = container.scrollHeight;
  }

  /** 每條對話各自建一個 chat；source=assistant 讓它不出現在主側欄 */
  async function ensureChat(thread, query) {
    if (thread.chatId) return thread.chatId;
    const res = await window.authFetch(`${window.state.apiBase}/chat`, {
      method: 'POST',
      body: JSON.stringify({ query, source: 'assistant' }),
      headers: { 'Content-Type': 'application/json' },
    });
    if (!res || !res.ok) throw new Error(await responseError(res, '建立對話失敗'));
    const json = await res.json();
    thread.chatId = json.data.id;
    return thread.chatId;
  }

  async function send() {
    if (state.streaming) return;
    const win = document.querySelector('.ai-assistant-window');
    const input = win?.querySelector('[data-role="input"]');
    const query = input?.value.trim();
    if (!query) return;
    // 送出當下就鎖定對話與上下文：串流途中換頁，回覆仍寫回原本那條對話
    const thread = activeThread();
    const context = state.context;
    const files = state.files;
    addUserMessage(thread, query, files);
    input.value = '';
    state.files = [];

    state.streaming = true;
    renderFiles();
    const sendButton = win.querySelector('[data-role="send"]');
    const resetButton = win.querySelector('[data-role="reset"]');
    sendButton.disabled = true;
    resetButton.disabled = true;
    const assistantNode = addMessage(thread, 'assistant', files.length ? '讀取與索引附件中…' : '正在整合資料與風險評估…');
    assistantNode.classList.add('pending');

    try {
      const chatId = await ensureChat(thread, query);
      // 沿用深度研究框架（網路搜尋＋附件檢索＋看圖），後端依 assistant_context 換成小助手 prompt
      const form = new FormData();
      form.append('chat_id', chatId);
      form.append('query', query);
      form.append('assistant_context', JSON.stringify(
        context && context.mode === 'selected_news'
          ? { mode: 'selected_news', news_id: context.news_id }
          : { mode: modeOf(context) },
      ));
      files.forEach((file) => form.append('files', file, file.name));
      // 不帶 Content-Type：authFetch 偵測到 FormData 會讓瀏覽器自己補 multipart boundary
      const response = await window.authFetch(`${window.state.apiBase}/chat/deep-research`, {
        method: 'POST',
        body: form,
      });
      if (!response || !response.ok) throw new Error(await responseError(response, '分析失敗'));

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let result = '';
      let streamError = '';
      const warnings = [];
      while (true) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const parts = buffer.split('\n\n');
        buffer = parts.pop() || '';
        for (const part of parts) {
          const line = part.split('\n').find(v => v.startsWith('data: '));
          if (!line) continue;
          try {
            const eventTypeLine = part.split('\n').find(v => v.startsWith('event: '));
            const eventType = eventTypeLine ? eventTypeLine.slice(7).trim() : '';
            const payload = JSON.parse(line.slice(6));
            if (eventType === 'thinking' && !result) {
              if (typeof payload.text === 'string' && payload.text) assistantNode.textContent = payload.text;
            } else if (eventType === 'notice') {
              const notes = Array.isArray(payload.messages) ? payload.messages : [];
              notes.forEach((m) => warnings.push(String(m)));
            } else if (eventType === 'error') {
              streamError = typeof payload.message === 'string' && payload.message ? payload.message : '分析過程發生錯誤';
            } else if (eventType === 'token') {
              const text = typeof payload.text === 'string' ? payload.text : '';
              if (text) {
                result += text;
                assistantNode.classList.remove('pending');
                if (window.marked && window.DOMPurify) {
                  assistantNode.innerHTML = window.DOMPurify.sanitize(window.marked.parse(result));
                } else assistantNode.textContent = result;
                scrollIfActive(thread);
              }
            }
          } catch (_) {}
        }
        if (done) break;
      }
      assistantNode.classList.remove('pending');
      if (!result) {
        assistantNode.textContent = streamError
          ? `⚠️ ${streamError}`
          : '本輪沒有產生可引用的分析文字，請確認資料來源或稍後再試。';
      } else if (streamError) {
        assistantNode.append(el('div', 'ai-assistant-error', `⚠️ ${streamError}`));
      }
      if (warnings.length) {
        assistantNode.append(el('div', 'ai-assistant-error', `⚠️ ${warnings.join('；')}`));
      }
      scrollIfActive(thread);
    } catch (err) {
      assistantNode.classList.remove('pending');
      assistantNode.textContent = `分析暫時失敗：${err.message || err}`;
      // 請求沒送成功（例如檔案驗證 4xx）時把附件還回去，使用者不用重選
      if (!state.files.length) {
        state.files = files;
      }
    } finally {
      state.streaming = false;
      sendButton.disabled = false;
      resetButton.disabled = false;
      renderFiles();
    }
  }

  document.addEventListener('DOMContentLoaded', build);
  window.AI_ASSISTANT = {
    setContext,
    clearContext,
    open: () => toggle(true),
    close: () => toggle(false),
  };
})();
