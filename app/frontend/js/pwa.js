/**
 * PWA 支援：註冊 Service Worker、同步狀態列顏色、iOS「加入主畫面」提示。
 *
 * 這支檔案在 index.html 與 login.html 都會載入，不依賴其他前端模組，
 * 也不依賴 lucide（提示列的圖示直接內嵌 SVG，載入順序無關）。
 */
(function () {
    'use strict';

    /** 與 index.css / login.css 的 --bg-dark 對齊；iOS 用它決定狀態列底色。 */
    var THEME_COLORS = { dark: '#0d0d0d', light: '#f4f4f5' };
    var THEME_STORAGE_KEY = 'insightUiTheme';
    var HINT_DISMISSED_KEY = 'insightA2hsHintDismissed';

    // ── 狀態列 / 瀏覽器 UI 顏色 ──────────────────────────────────
    /**
     * 更新 <meta name="theme-color">。深／淺色切換時由 index.js、login.js 呼叫，
     * 讓 iOS 獨立視窗的狀態列跟著換色（否則淺色主題會頂著一條黑）。
     * @param {'dark'|'light'} [theme] 省略時自動從 body class / localStorage 判斷
     */
    function syncPwaThemeColor(theme) {
        var resolved = theme;
        if (resolved !== 'dark' && resolved !== 'light') {
            if (document.body && document.body.classList.contains('light-theme')) {
                resolved = 'light';
            } else if (document.body && document.body.classList.contains('dark-theme')) {
                resolved = 'dark';
            } else {
                try {
                    resolved = localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark';
                } catch (_) {
                    resolved = 'dark';
                }
            }
        }

        var meta = document.querySelector('meta[name="theme-color"]');
        if (!meta) {
            meta = document.createElement('meta');
            meta.name = 'theme-color';
            document.head.appendChild(meta);
        }
        meta.content = THEME_COLORS[resolved] || THEME_COLORS.dark;
    }
    window.syncPwaThemeColor = syncPwaThemeColor;

    // ── 執行環境判斷 ────────────────────────────────────────────
    /** 已經從主畫面圖示開啟（iOS 用 navigator.standalone，其餘看 display-mode）。 */
    function isStandalone() {
        return window.navigator.standalone === true ||
            (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
    }

    function isIOS() {
        // iPadOS 13+ 的 UA 會偽裝成 Mac，靠觸控點數補判
        return /iPad|iPhone|iPod/.test(navigator.userAgent) ||
            (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    }

    /** 只有 Safari 的分享選單長成提示裡描述的樣子，其他 iOS 瀏覽器步驟不同。 */
    function isIOSSafari() {
        return isIOS() &&
            /Safari/.test(navigator.userAgent) &&
            !/CriOS|FxiOS|EdgiOS|OPiOS|Chrome/.test(navigator.userAgent);
    }

    // ── Service Worker ─────────────────────────────────────────
    function registerServiceWorker() {
        if (!('serviceWorker' in navigator)) return;
        // file:// 或 http://（非 localhost）不允許註冊，直接跳過免得 console 噴錯
        if (location.protocol !== 'https:' &&
            location.hostname !== 'localhost' &&
            location.hostname !== '127.0.0.1') {
            return;
        }
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function (err) {
                console.warn('[PWA] Service Worker 註冊失敗：', err);
            });
        });
    }

    // ── iOS「加入主畫面」提示（僅 login 頁；body 需帶 data-pwa-install-hint）──
    var HINT_CSS = [
        '.pwa-a2hs-hint{',
        '  position:fixed;left:50%;transform:translateX(-50%) translateY(0);',
        '  bottom:calc(1rem + env(safe-area-inset-bottom,0px));',
        '  z-index:2000;width:min(420px,calc(100vw - 2rem));',
        '  display:flex;align-items:flex-start;gap:.75rem;',
        '  padding:.85rem 1rem;border-radius:16px;',
        '  background:var(--glass,rgba(20,20,20,.7));',
        '  border:1px solid var(--border,#2a2a2a);',
        '  box-shadow:0 12px 40px rgba(0,0,0,.35);',
        '  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);',
        '  color:var(--text-main,#e0e0e0);font-size:.82rem;line-height:1.55;',
        '  opacity:0;transition:opacity .28s ease,transform .28s ease;',
        '}',
        '.pwa-a2hs-hint.is-visible{opacity:1;}',
        '.pwa-a2hs-hint svg{flex:none;width:16px;height:16px;vertical-align:-3px;}',
        '.pwa-a2hs-hint .pwa-a2hs-icon{margin-top:.15rem;width:18px;height:18px;color:var(--accent,#b0b0b0);}',
        '.pwa-a2hs-hint strong{display:block;font-weight:500;margin-bottom:.15rem;}',
        '.pwa-a2hs-hint p{margin:0;color:var(--text-dim,#888);}',
        '.pwa-a2hs-close{flex:none;margin:-.2rem -.3rem 0 0;padding:.3rem;',
        '  background:none;border:0;border-radius:8px;cursor:pointer;',
        '  color:var(--text-dim,#888);line-height:0;}',
        '.pwa-a2hs-close:hover{background:var(--surface-hover,rgba(255,255,255,.06));}',
    ].join('');

    var SHARE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>' +
        '<polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>';

    var PLUS_SQUARE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M8 12h8"/><path d="M12 8v8"/></svg>';

    var CLOSE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="width:16px;height:16px">' +
        '<path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';

    function showAddToHomeScreenHint() {
        if (document.querySelector('.pwa-a2hs-hint')) return;

        var style = document.createElement('style');
        style.textContent = HINT_CSS;
        document.head.appendChild(style);

        var box = document.createElement('div');
        box.className = 'pwa-a2hs-hint';
        box.setAttribute('role', 'status');
        box.innerHTML =
            '<span class="pwa-a2hs-icon">' + PLUS_SQUARE_SVG + '</span>' +
            '<div><strong>把 Insight 加到主畫面</strong>' +
            '<p>點下方的分享 ' + SHARE_SVG + ' → 選「加入主畫面」，' +
            '之後就能像 App 一樣全螢幕開啟。</p></div>' +
            '<button type="button" class="pwa-a2hs-close" aria-label="關閉提示">' + CLOSE_SVG + '</button>';

        box.querySelector('.pwa-a2hs-close').addEventListener('click', function () {
            box.classList.remove('is-visible');
            try { localStorage.setItem(HINT_DISMISSED_KEY, '1'); } catch (_) {}
            setTimeout(function () { box.remove(); }, 300);
        });

        document.body.appendChild(box);
        requestAnimationFrame(function () { box.classList.add('is-visible'); });
    }

    function maybeShowHint() {
        if (!document.body || document.body.dataset.pwaInstallHint !== 'on') return;
        if (isStandalone() || !isIOSSafari()) return;
        try {
            if (localStorage.getItem(HINT_DISMISSED_KEY) === '1') return;
        } catch (_) {}
        showAddToHomeScreenHint();
    }

    // ── 啟動 ────────────────────────────────────────────────────
    function init() {
        // 獨立視窗時掛個 class，CSS 可以針對「像 App」的情境微調（例如安全區留白）
        if (isStandalone()) document.documentElement.classList.add('pwa-standalone');
        syncPwaThemeColor();
        maybeShowHint();
    }

    registerServiceWorker();

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
