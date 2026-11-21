/**
 * 公開分享頁（/share?t=<token>）：免登入讀取對話快照，排成「批註式研究筆記」。
 * 每一組 = 一個提問（左側批註欄，捲動時固定）+ 其後的回答。
 */
(() => {
  const $ = (id) => document.getElementById(id);

  const dateFmt = new Intl.DateTimeFormat('zh-TW', {
    year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const numFmt = new Intl.NumberFormat('zh-TW');

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function renderMarkdown(text) {
    if (!window.marked || !window.DOMPurify) {
      const p = el('p');
      p.textContent = text;
      return p.outerHTML;
    }
    const html = window.marked.parse(text, { gfm: true, breaks: true });
    return window.DOMPurify.sanitize(html);
  }

  // 外部連結一律新分頁開啟、不帶 referrer（網址含分享 token）
  function hardenLinks(root) {
    root.querySelectorAll('a[href]').forEach((a) => {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    });
    // 寬表格包一層，手機上可橫向捲動而不撐破版面
    root.querySelectorAll('table').forEach((table) => {
      if (table.parentElement.classList.contains('share-table')) return;
      const wrap = el('div', 'share-table');
      wrap.tabIndex = 0;
      wrap.setAttribute('role', 'region');
      wrap.setAttribute('aria-label', '表格（可橫向捲動）');
      table.replaceWith(wrap);
      wrap.append(table);
    });
  }

  /** 把訊息串依「提問 → 回答」分組；開頭若是回答，該組沒有提問 */
  function groupExchanges(messages) {
    const groups = [];
    messages.forEach((msg) => {
      if (msg.role === 'user' || !groups.length) {
        groups.push({ question: msg.role === 'user' ? msg : null, answers: [] });
      }
      if (msg.role === 'assistant') groups[groups.length - 1].answers.push(msg);
    });
    return groups;
  }

  function renderThread(messages) {
    const thread = $('conversation');
    thread.textContent = '';
    groupExchanges(messages).forEach((group) => {
      const row = el('section', 'exchange');
      const rail = el('div', 'exchange-q');
      if (group.question) {
        rail.append(el('span', 'exchange-q-label', '提問'), el('p', 'exchange-q-text', group.question.content));
      }
      const body = el('div', 'exchange-a');
      if (!group.answers.length) {
        body.append(el('p', 'exchange-pending', '這個提問在分享時還沒有回答。'));
      }
      group.answers.forEach((answer) => {
        const article = el('div', 'answer');
        article.innerHTML = renderMarkdown(answer.content);
        hardenLinks(article);
        body.append(article);
      });
      row.append(rail, body);
      thread.append(row);
    });
  }

  function showMissing(message) {
    document.title = '找不到分享的對話 | Insight';
    $('share-title').textContent = '找不到這段對話';
    $('share-sub').textContent = message;
    $('conversation').textContent = '';
    const foot = $('share-foot');
    foot.hidden = false;
    foot.querySelector('.share-disclaimer').hidden = true;
  }

  async function load() {
    const token = new URLSearchParams(window.location.search).get('t') || '';
    if (!/^[A-Za-z0-9_-]{32,64}$/.test(token)) {
      showMissing('連結不完整。請確認複製時有包含網址最後的整串代碼。');
      return;
    }
    let res;
    try {
      res = await fetch(`${resolveStockInsightApiBase()}/share/${encodeURIComponent(token)}`, {
        credentials: 'omit',
        referrerPolicy: 'no-referrer',
      });
    } catch (_) {
      showMissing('目前無法連線。請檢查網路後重新整理這個頁面。');
      return;
    }
    if (res.status === 404) {
      showMissing('連結可能已被分享者停止分享。如果需要，請向分享者索取新的連結。');
      return;
    }
    if (!res.ok) {
      showMissing(`載入失敗（${res.status}）。請稍後重新整理這個頁面。`);
      return;
    }
    const { data } = await res.json();
    const title = data.title || '分享的對話';
    document.title = `${title} | Insight`;
    $('share-title').textContent = title;

    const sub = $('share-sub');
    sub.textContent = '';
    const updated = new Date(data.updated_at);
    sub.append(
      el('span', null, `分享時的對話快照，共 ${numFmt.format(data.message_count)} 則訊息`),
      el('span', null, Number.isNaN(updated.getTime()) ? '' : `更新於 ${dateFmt.format(updated)}`),
    );
    if (data.truncated) sub.append(el('span', null, '對話較長，這裡只收錄最新的部分。'));

    renderThread(Array.isArray(data.messages) ? data.messages : []);
    $('share-foot').hidden = false;
  }

  function initThemeToggle() {
    $('share-theme').addEventListener('click', () => {
      const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem('insightUiTheme', next); } catch (_) {}
    });
  }

  initThemeToggle();
  load();
})();
