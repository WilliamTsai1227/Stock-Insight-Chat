/**
 * 新手功能導覽（Step 1 / 2 / 3 …）
 *
 * - 第一次登入進到主頁時自動播放一次；看完或略過都記在 localStorage，之後不再自動出現。
 *   這是「每台裝置／瀏覽器」的紀錄，換裝置會再看到一次，對新手導覽來說可以接受。
 * - 右上角頭像選單的「功能導覽」可隨時重看（window.InsightTour.start()）。
 * - 純前端：每一步用 CSS selector 指向頁面上既有的按鈕，挖洞聚光並在旁邊顯示說明卡。
 * - 手機版側欄是抽屜，指向側欄按鈕的步驟會先打開抽屜，離開時再關上。
 *
 * 新增步驟：在 STEPS 加一筆 { target, title, body, drawer? }。
 * target 找不到（或元素被隱藏）時，那一步改成畫面置中顯示，不會卡住。
 * 改了步驟內容想讓所有人再看一次：把 STORAGE_KEY 的版本號加一。
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'insightTourDone.v1';
  const PAD = 6;          // 聚光框比目標元素多出的留白（px）
  const GAP = 12;         // 說明卡與聚光框的距離（px）
  const DRAWER_WAIT = 320; // 側欄抽屜滑出動畫時間（index.css 為 .28s）

  const STEPS = [
    {
      target: null,
      title: '歡迎使用 Insight',
      body: '花 30 秒認識主要功能。之後想再看一次，可以從右上角頭像選單裡的「功能導覽」打開。',
    },
    {
      target: '.input-container',
      title: '在這裡提問',
      body: '直接輸入問題，例如「台積電最近法說會的重點是什麼？」，按 Enter 送出。',
    },
    {
      target: '#chat-mode-btn',
      title: '選擇對話模式',
      body: '<b>一般對話</b>：日常問答。<br><b>股市 Agent</b>：自動搜尋新聞與市場資料再回答，輸入 @ 可指定分析方法。<br><b>深度研究</b>：多輪查證、可附檔案，約 1–5 分鐘產出完整報告。',
    },
    {
      target: '#explore-group',
      drawer: true,
      title: '探索市場',
      body: '<b>K 線圖</b>看技術面、<b>新聞</b>看最新消息、<b>AI 分析</b>看每日市場報告。',
    },
    {
      target: '#portfolio-btn',
      drawer: true,
      title: '投資組合',
      body: '記錄庫存與觀察清單，可以設定排程，定期幫你分析持股並給出調整建議。',
    },
    {
      target: '#calendar-btn',
      drawer: true,
      title: '行事曆',
      body: '總經數據、FOMC 會議、大型財報日，以及你持股與觀察清單的重要日子。',
    },
    {
      target: '#notes-btn',
      drawer: true,
      title: '筆記',
      body: '在對話中選取幾則訊息就能整理成筆記，分頁管理，之後回來複習。',
    },
    {
      target: '.ai-assistant-fab',
      title: 'AI 小助手',
      body: '每一頁都能叫出來。在新聞頁選一則新聞、或在投資組合頁打開，它會直接讀你正在看的內容回答。',
    },
    {
      target: '#user-avatar',
      title: '帳號與用量',
      body: '會員資料、用量統計都在這裡；想再看一次導覽，也從這裡的「功能導覽」打開。',
    },
  ];

  let index = 0;
  let active = false;
  let els = null;
  let drawerOpenedByTour = false;
  let lastFocus = null;

  // ── localStorage（私密模式可能丟例外）────────────────────────
  function isDone() {
    try { return localStorage.getItem(STORAGE_KEY) === '1'; } catch { return false; }
  }
  function markDone() {
    try { localStorage.setItem(STORAGE_KEY, '1'); } catch { /* 無法保存就下次再看一次 */ }
  }

  // ── 手機側欄抽屜 ────────────────────────────────────────────
  function drawerMode() {
    return window.matchMedia && window.matchMedia('(max-width: 1024px)').matches;
  }
  function setDrawer(open) {
    const app = document.querySelector('.app-container');
    if (!app || !drawerMode()) return false;
    const isOpen = app.classList.contains('sidebar-open');
    if (open === isOpen) return false;
    // 沿用 index.js 的開關函式，aria-expanded 與遮罩狀態才會一致
    if (open && typeof window.toggleSidebarDrawer === 'function') window.toggleSidebarDrawer();
    else if (!open && typeof window.closeSidebarDrawer === 'function') window.closeSidebarDrawer();
    return true;
  }

  // ── DOM ────────────────────────────────────────────────────
  function build() {
    const root = document.createElement('div');
    root.className = 'tour-root';
    root.innerHTML = `
      <div class="tour-blocker"></div>
      <div class="tour-spotlight" aria-hidden="true"></div>
      <div class="tour-card" role="dialog" aria-modal="true" aria-labelledby="tour-title" aria-describedby="tour-body">
        <div class="tour-step" aria-live="polite"></div>
        <h3 class="tour-title" id="tour-title"></h3>
        <p class="tour-body" id="tour-body"></p>
        <div class="tour-dots" aria-hidden="true"></div>
        <div class="tour-actions">
          <button type="button" class="tour-skip">略過導覽</button>
          <span class="tour-spacer"></span>
          <button type="button" class="tour-prev">上一步</button>
          <button type="button" class="tour-next">下一步</button>
        </div>
      </div>`;
    document.body.append(root);

    const q = (s) => root.querySelector(s);
    els = {
      root,
      spot: q('.tour-spotlight'),
      card: q('.tour-card'),
      step: q('.tour-step'),
      title: q('.tour-title'),
      body: q('.tour-body'),
      dots: q('.tour-dots'),
      prev: q('.tour-prev'),
      next: q('.tour-next'),
      skip: q('.tour-skip'),
    };
    els.dots.innerHTML = STEPS.map(() => '<span></span>').join('');

    els.next.addEventListener('click', next);
    els.prev.addEventListener('click', prev);
    els.skip.addEventListener('click', () => finish());
    // 點遮罩不關閉（避免誤觸），只有按鈕或 Esc 才結束
  }

  function targetOf(step) {
    if (!step.target) return null;
    const el = document.querySelector(step.target);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return null;
    return el;
  }

  async function show(i) {
    index = i;
    const step = STEPS[i];

    // 進入側欄步驟先開抽屜；離開側欄步驟把導覽自己開的抽屜關回去
    if (step.drawer) {
      if (setDrawer(true)) {
        drawerOpenedByTour = true;
        els.root.classList.add('tour-waiting');
        await new Promise((r) => setTimeout(r, DRAWER_WAIT));
        if (!active || index !== i) return;
        els.root.classList.remove('tour-waiting');
      }
    } else if (drawerOpenedByTour) {
      setDrawer(false);
      drawerOpenedByTour = false;
    }

    els.step.textContent = `Step ${i + 1} / ${STEPS.length}`;
    els.title.textContent = step.title;
    els.body.innerHTML = step.body; // 內容為本檔寫死的字串，非使用者輸入
    [...els.dots.children].forEach((d, n) => d.classList.toggle('on', n === i));
    els.prev.hidden = i === 0;
    els.next.textContent = i === STEPS.length - 1 ? '開始使用' : (i === 0 ? '開始導覽' : '下一步');
    els.skip.hidden = i === STEPS.length - 1;

    const el = targetOf(step);
    if (el) el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    position();
    els.next.focus({ preventScroll: true });
  }

  function position() {
    if (!active) return;
    const step = STEPS[index];
    const el = targetOf(step);
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const card = els.card;

    if (!el) {
      els.root.classList.add('tour-centered');
      els.spot.style.cssText = '';
      card.style.left = '';
      card.style.top = '';
      return;
    }
    els.root.classList.remove('tour-centered');

    const r = el.getBoundingClientRect();
    const spot = {
      left: Math.max(4, r.left - PAD),
      top: Math.max(4, r.top - PAD),
      right: Math.min(vw - 4, r.right + PAD),
      bottom: Math.min(vh - 4, r.bottom + PAD),
    };
    const radius = parseFloat(getComputedStyle(el).borderRadius) || 10;
    Object.assign(els.spot.style, {
      left: `${spot.left}px`,
      top: `${spot.top}px`,
      width: `${spot.right - spot.left}px`,
      height: `${spot.bottom - spot.top}px`,
      borderRadius: `${Math.min(radius + PAD, 999)}px`,
    });

    // 說明卡：優先放下方，放不下改上方，再不行放右側（側欄按鈕）；最後夾在畫面內
    const cw = card.offsetWidth;
    const ch = card.offsetHeight;
    let top;
    let left = spot.left + (spot.right - spot.left) / 2 - cw / 2;
    if (spot.bottom + GAP + ch <= vh - 8) {
      top = spot.bottom + GAP;
    } else if (spot.top - GAP - ch >= 8) {
      top = spot.top - GAP - ch;
    } else if (spot.right + GAP + cw <= vw - 8) {
      left = spot.right + GAP;
      top = spot.top + (spot.bottom - spot.top) / 2 - ch / 2;
    } else {
      top = vh - ch - 16;
    }
    left = Math.min(Math.max(left, 12), vw - cw - 12);
    top = Math.min(Math.max(top, 12), vh - ch - 12);
    card.style.left = `${left}px`;
    card.style.top = `${top}px`;
  }

  function next() {
    if (index >= STEPS.length - 1) finish();
    else show(index + 1);
  }
  function prev() {
    if (index > 0) show(index - 1);
  }

  function onKey(e) {
    if (!active) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); next(); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); prev(); }
    else if (e.key === 'Tab') {
      // 焦點留在說明卡內
      const f = [...els.card.querySelectorAll('button:not([hidden])')];
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }

  let raf = 0;
  function onViewportChange() {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(position);
  }

  function start() {
    if (active) return;
    if (!els) build();
    active = true;
    lastFocus = document.activeElement;
    els.root.classList.add('open');
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', onViewportChange);
    window.addEventListener('scroll', onViewportChange, true);
    show(0);
  }

  function finish() {
    if (!active) return;
    active = false;
    markDone();
    els.root.classList.remove('open', 'tour-centered', 'tour-waiting');
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onViewportChange);
    window.removeEventListener('scroll', onViewportChange, true);
    if (drawerOpenedByTour) { setDrawer(false); drawerOpenedByTour = false; }
    if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus({ preventScroll: true });
  }

  // ── 啟動 ───────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', async () => {
    const menuItem = document.getElementById('menu-tour');
    if (menuItem) {
      menuItem.addEventListener('click', (e) => {
        e.preventDefault();
        document.getElementById('user-dropdown')?.classList.remove('show');
        start();
      });
    }

    if (isDone()) return;
    const ok = await (window.authReady ?? Promise.resolve(true));
    if (!ok) return; // 未登入，auth.js 會導向 /login
    // 等側欄、小助手按鈕等畫面元素就位，也避開剛登入時的其他彈窗
    setTimeout(() => {
      if (isDone() || document.querySelector('.modal-overlay.show, .modal-overlay.active')) return;
      start();
    }, 900);
  });

  window.InsightTour = { start, reset: () => { try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ } } };
})();
