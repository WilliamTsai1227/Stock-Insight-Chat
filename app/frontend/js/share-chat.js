/**
 * 分享對話：建立／更新／停止公開唯讀連結（快照）。
 *
 * 後端：GET/POST/DELETE /api/chat/{chat_id}/share；公開頁 /share?t=<token>。
 * 快照只包含建立或更新當下的訊息，之後的新訊息要按「更新為最新內容」才會出現在連結裡。
 */
(() => {
  const modalId = 'share-chat-modal';
  let current = null;      // 目前分享狀態（後端 data）
  let chatId = null;
  let busy = false;
  let revokeArmedUntil = 0;
  let lastFocus = null;

  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function icon(name) {
    const i = document.createElement('i');
    i.setAttribute('data-lucide', name);
    i.setAttribute('aria-hidden', 'true');
    return i;
  }

  function button(label, cls, action, iconName) {
    const btn = el('button', `modal-btn ${cls}`);
    btn.type = 'button';
    btn.dataset.shareAction = action;
    if (iconName) btn.append(icon(iconName));
    btn.append(el('span', null, label));
    btn.disabled = busy;
    return btn;
  }

  function toast(message, type) {
    if (typeof window.showToast === 'function') window.showToast(message, type);
  }

  function shareUrl() {
    return current && current.path ? `${window.location.origin}${current.path}` : '';
  }

  const dateFmt = new Intl.DateTimeFormat(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
  const numFmt = new Intl.NumberFormat();

  function formatDate(iso) {
    const d = iso ? new Date(iso) : null;
    return d && !Number.isNaN(d.getTime()) ? dateFmt.format(d) : '';
  }

  async function errorText(res, fallback) {
    if (!res) return '無法連線，請檢查網路後再試一次。';
    try {
      const json = await res.json();
      if (typeof json.detail === 'string') return json.detail;
    } catch (_) {}
    return `${fallback}（${res.status}）`;
  }

  // ── 渲染 ───────────────────────────────────────────────────

  function render(state, message) {
    const body = $('share-chat-body');
    const footer = $('share-chat-footer');
    if (!body || !footer) return;
    body.textContent = '';
    footer.textContent = '';

    if (state === 'loading') {
      body.append(el('p', 'share-chat-muted', '讀取分享狀態中…'));
    } else if (state === 'error') {
      body.append(el('p', 'modal-msg error share-chat-error', message || '讀取失敗，請稍後再試。'));
      footer.append(button('關閉', 'secondary', 'close'), button('重試', 'primary', 'reload', 'rotate-ccw'));
    } else if (state === 'not-shared') {
      const intro = el('div', 'share-chat-intro');
      intro.append(icon('globe'), el('p', null, '建立一個公開的唯讀連結。拿到連結的人不需要登入就能看到這則對話目前的內容。'));
      const list = el('ul', 'share-chat-points');
      [
        '分享的是「現在」的快照：之後的新訊息不會自動出現。',
        '不包含上傳的附件、使用的模型與工具細節。',
        '隨時可以停止分享，連結會立即失效。',
      ].forEach((t) => list.append(el('li', null, t)));
      body.append(intro, list);
      footer.append(button('取消', 'secondary', 'close'), button('建立公開連結', 'primary', 'create', 'link'));
    } else if (state === 'shared') {
      const field = el('div', 'share-chat-link');
      const input = el('input', 'share-chat-url');
      input.type = 'text';
      input.readOnly = true;
      input.value = shareUrl();
      input.setAttribute('aria-label', '公開分享連結');
      input.spellcheck = false;
      input.addEventListener('focus', () => input.select());
      const copy = el('button', 'share-chat-copy');
      copy.type = 'button';
      copy.dataset.shareAction = 'copy';
      copy.append(icon('copy'), el('span', null, '複製'));
      field.append(input, copy);

      const meta = el('p', 'share-chat-meta');
      meta.append(
        el('span', null, `快照時間 ${formatDate(current.updated_at)}`),
        el('span', null, `${numFmt.format(current.message_count)} 則訊息`),
        el('span', null, `${numFmt.format(current.view_count)} 次瀏覽`),
      );
      body.append(field, meta);
      if (current.truncated) {
        body.append(el('p', 'share-chat-muted', '對話太長，連結只包含最新的部分訊息。'));
      }

      const left = el('div', 'share-chat-footer-left');
      const armed = Date.now() < revokeArmedUntil;
      left.append(button(armed ? '確定停止分享？' : '停止分享', `danger share-chat-revoke${armed ? ' armed' : ''}`, 'revoke', 'link-2-off'));
      const right = el('div', 'share-chat-footer-right');
      right.append(button('更新為最新內容', 'secondary', 'refresh', 'refresh-cw'));
      if (navigator.share) right.append(button('分享…', 'primary', 'native', 'send'));
      else right.append(button('開啟連結', 'primary', 'open', 'external-link'));
      footer.append(left, right);
    }
    if (message && state !== 'error') body.append(el('p', 'modal-msg share-chat-note', message));
    if (window.lucide) window.lucide.createIcons();
  }

  // ── API ────────────────────────────────────────────────────

  function endpoint() {
    return `${window.state.apiBase}/chat/${encodeURIComponent(chatId)}/share`;
  }

  async function load() {
    render('loading');
    try {
      const res = await window.authFetch(endpoint());
      if (!res || !res.ok) return render('error', await errorText(res, '讀取分享狀態失敗'));
      current = (await res.json()).data;
      render(current.shared ? 'shared' : 'not-shared');
    } catch (err) {
      render('error', '無法連線，請檢查網路後再試一次。');
    }
  }

  async function mutate(method, successMessage) {
    if (busy) return;
    busy = true;
    render(current && current.shared ? 'shared' : 'not-shared');
    try {
      const res = await window.authFetch(endpoint(), { method });
      if (!res || !res.ok) {
        busy = false;
        return render(current && current.shared ? 'shared' : 'not-shared', await errorText(res, '操作失敗'));
      }
      current = (await res.json()).data;
      busy = false;
      render(current.shared ? 'shared' : 'not-shared');
      if (successMessage) toast(successMessage, 'success');
      if (method === 'POST') $('share-chat-modal')?.querySelector('.share-chat-copy')?.focus();
    } catch (_) {
      busy = false;
      render(current && current.shared ? 'shared' : 'not-shared', '無法連線，請檢查網路後再試一次。');
    }
  }

  async function copyLink() {
    const url = shareUrl();
    try {
      await navigator.clipboard.writeText(url);
    } catch (_) {
      const input = $('share-chat-modal')?.querySelector('.share-chat-url');
      if (input) { input.focus(); input.select(); }
      document.execCommand && document.execCommand('copy');
    }
    const label = $('share-chat-modal')?.querySelector('.share-chat-copy span');
    if (label) {
      label.textContent = '已複製';
      setTimeout(() => { label.textContent = '複製'; }, 1800);
    }
  }

  // ── 開關 ───────────────────────────────────────────────────

  function open() {
    chatId = window.state && window.state.currentChatId;
    if (!chatId) {
      toast('先開始或開啟一段對話，才能分享。', 'info');
      return;
    }
    lastFocus = document.activeElement;
    current = null;
    revokeArmedUntil = 0;
    $(modalId).classList.add('show');
    load().then(() => {
      const target = $(modalId).querySelector('.share-chat-copy, .modal-btn.primary');
      if (target) target.focus();
    });
  }

  function close() {
    $(modalId)?.classList.remove('show');
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus();
  }

  function onAction(action) {
    switch (action) {
      case 'close': return close();
      case 'reload': return load();
      case 'create': return mutate('POST', '已建立公開連結');
      case 'refresh': return mutate('POST', '已更新為最新內容');
      case 'copy': return copyLink();
      case 'open': return window.open(shareUrl(), '_blank', 'noopener');
      case 'native':
        return navigator.share({ title: document.getElementById('current-chat-title')?.textContent || '分享對話', url: shareUrl() })
          .catch(() => {});
      case 'revoke':
        if (Date.now() < revokeArmedUntil) {
          revokeArmedUntil = 0;
          return mutate('DELETE', '已停止分享，連結已失效');
        }
        // 破壞性操作：第一下只是「上膛」，4 秒內再按一次才真的停止
        revokeArmedUntil = Date.now() + 4000;
        render('shared');
        $(modalId).querySelector('.share-chat-revoke')?.focus();
        setTimeout(() => { if (Date.now() >= revokeArmedUntil && current?.shared) render('shared'); }, 4100);
        return undefined;
      default: return undefined;
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('share-chat-btn')?.addEventListener('click', open);
    const modal = $(modalId);
    if (!modal) return;
    modal.addEventListener('click', (event) => {
      if (event.target === modal) return close();   // 點背景關閉
      const target = event.target.closest('[data-share-action]');
      if (target && !target.disabled) onAction(target.dataset.shareAction);
      return undefined;
    });
    modal.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') close();
    });
  });

  window.SHARE_CHAT = { open, close };
})();
