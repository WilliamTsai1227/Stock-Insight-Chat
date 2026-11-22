// ============================================
// Login Page Logic（Google SSO Only）
// ============================================

const API_BASE = resolveStockInsightApiBase();
/** 與 index.js 共用，登入頁與主頁主題同步 */
const THEME_STORAGE_KEY = 'insightUiTheme';

function applyUiTheme(theme) {
    const isLight = theme === 'light';
    document.body.classList.add('theme-switching');
    document.body.classList.toggle('dark-theme', !isLight);
    document.body.classList.toggle('light-theme', isLight);
    document.documentElement.dataset.uiTheme = theme;
    try {
        localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (_) {}

    // iOS 獨立視窗的狀態列底色跟著主題走（pwa.js 提供）
    if (typeof window.syncPwaThemeColor === 'function') window.syncPwaThemeColor(theme);

    requestAnimationFrame(() => {
        document.body.classList.remove('theme-switching');
    });
}

function initUiTheme() {
    let theme = 'dark';
    try {
        const saved = localStorage.getItem(THEME_STORAGE_KEY);
        if (saved === 'light' || saved === 'dark') theme = saved;
    } catch (_) {}
    applyUiTheme(theme);
}

function initThemeToggle() {
    const btn = document.getElementById('theme-toggle-btn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const next = document.body.classList.contains('light-theme') ? 'dark' : 'light';
        applyUiTheme(next);
    });
}

// ── OAuth 錯誤代碼對應訊息 ────────────────────────────────────────
// 後端 auth.py 的 _fe_error() 會帶這些代碼回來（Google 端的 access_denied 等一律轉成 oauth_cancelled）。
// 每則都說明發生什麼事、下一步怎麼做；不出現 CSRF、Session 這類技術名詞。
const OAUTH_ERROR_MESSAGES = {
    oauth_cancelled:       '你在 Google 畫面取消了登入。準備好時再按一次「以 Google 帳號登入」。',
    invalid_state:         '登入連結已失效，可能是停留太久或開了多個分頁。請再按一次「以 Google 帳號登入」。',
    token_exchange_failed: 'Google 暫時沒有回應，請過一分鐘再試一次。',
    no_access_token:       'Google 沒有完成授權，請再試一次。',
    userinfo_failed:       '暫時讀不到你的 Google 帳號資料，請過一分鐘再試一次。',
    missing_user_info:     'Google 沒有提供電子郵件。請在 Google 的授權畫面允許 Insight 讀取電子郵件後再登入。',
    db_error:              'Insight 伺服器暫時無法完成登入，請稍後再試。',
    session_error:         'Insight 伺服器暫時無法完成登入，請稍後再試。',
};

/** 對照表外的代碼：先給下一步，代碼只當回報用的參考 */
function oauthErrorMessage(code) {
    return OAUTH_ERROR_MESSAGES[code]
        || `登入沒有完成，請再試一次。如果一直失敗，回報問題時請附上代碼：${code}`;
}

// ── 服務條款 / 隱私條款 Modal ───────────────────────────────────

function appendFormattedText(parent, text) {
    const parts = text.split(/(\*\*[^*]+\*\*)/g);
    parts.forEach((part) => {
        if (!part) return;
        if (part.startsWith('**') && part.endsWith('**')) {
            const strong = document.createElement('strong');
            strong.textContent = part.slice(2, -2);
            parent.appendChild(strong);
        } else {
            parent.appendChild(document.createTextNode(part));
        }
    });
}

function renderLegalBody(container, sections) {
    while (container.firstChild) container.removeChild(container.firstChild);

    sections.forEach((section) => {
        const sec = document.createElement('section');
        const h4 = document.createElement('h4');
        h4.textContent = section.heading;
        sec.appendChild(h4);

        section.paragraphs.forEach((para) => {
            const p = document.createElement('p');
            appendFormattedText(p, para);
            sec.appendChild(p);
        });

        container.appendChild(sec);
    });
}

function closeLegalModal() {
    const modal = document.getElementById('legal-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    document.body.style.overflow = '';
}

function openLegalModal(type) {
    if (typeof LEGAL_CONTENT === 'undefined' || !LEGAL_CONTENT[type]) return;

    const data = LEGAL_CONTENT[type];
    const modal = document.getElementById('legal-modal');
    const titleEl = document.getElementById('legal-modal-title');
    const updatedEl = document.getElementById('legal-modal-updated');
    const bodyEl = document.getElementById('legal-modal-body');

    if (!modal || !titleEl || !updatedEl || !bodyEl) return;

    titleEl.textContent = data.title;
    updatedEl.textContent = `最後更新：${data.updated}`;
    renderLegalBody(bodyEl, data.sections);

    modal.classList.remove('hidden');
    document.body.style.overflow = 'hidden';

    if (typeof lucide !== 'undefined') lucide.createIcons();

    const closeBtn = document.getElementById('legal-modal-close');
    if (closeBtn) closeBtn.focus();
}

function initLegalModals() {
    document.querySelectorAll('.legal-link[data-legal]').forEach((btn) => {
        btn.addEventListener('click', () => {
            openLegalModal(btn.getAttribute('data-legal'));
        });
    });

    const closeBtn = document.getElementById('legal-modal-close');
    const okBtn = document.getElementById('legal-modal-ok');
    const modal = document.getElementById('legal-modal');

    if (closeBtn) closeBtn.addEventListener('click', closeLegalModal);
    if (okBtn) okBtn.addEventListener('click', closeLegalModal);

    if (modal) {
        modal.addEventListener('click', (e) => {
            if (e.target === modal) closeLegalModal();
        });
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && modal && !modal.classList.contains('hidden')) {
            closeLegalModal();
        }
    });
}

// ── 偵測 OAuth callback 錯誤 ─────────────────────────────────────
// 若 Google SSO 失敗，後端會把錯誤代碼帶在 ?error= 重導回登入頁
window.addEventListener('DOMContentLoaded', () => {
    initUiTheme();
    initThemeToggle();
    initLegalModals();
    if (typeof lucide !== 'undefined') lucide.createIcons();

    // 已登入則直接跳主頁
    if (localStorage.getItem('user')) {
        window.location.href = '/';
        return;
    }

    const params = new URLSearchParams(window.location.search);
    const errorCode = params.get('error');
    if (errorCode) {
        const msg = oauthErrorMessage(errorCode.slice(0, 64));
        const errorEl = document.getElementById('oauth-error');
        errorEl.textContent = msg;
        errorEl.classList.remove('hidden');

        // 清掉 URL 上的 ?error= 參數（不影響頁面功能，美觀）
        window.history.replaceState({}, '', window.location.pathname);
    }
});

// ── Google SSO 按鈕 ───────────────────────────────────────────────
document.getElementById('google-login-btn').addEventListener('click', async () => {
    const btn = document.getElementById('google-login-btn');
    btn.disabled = true;
    btn.querySelector('span').textContent = '跳轉至 Google…';

    if (window.stockInsightDesktop) {
        btn.querySelector('span').textContent = '請在瀏覽器完成登入…';
        try {
            const result = await window.stockInsightDesktop.login();
            if (result.ok) {
                window.location.href = '/?sso=1';
                return;
            }
            throw new Error(result.message);
        } catch (error) {
            const errorEl = document.getElementById('oauth-error');
            errorEl.textContent = error.message || '登入失敗，請稍後再試。';
            errorEl.classList.remove('hidden');
            btn.disabled = false;
            btn.querySelector('span').textContent = '以 Google 帳號登入';
        }
        return;
    }

    // 帶上目前前端 origin，OAuth 完成後回到同一網址（區網 IP 不會被導到 localhost）
    const returnUrl = encodeURIComponent(window.location.origin);
    window.location.href = `${API_BASE}/user/auth/google/start?return_url=${returnUrl}`;
});
