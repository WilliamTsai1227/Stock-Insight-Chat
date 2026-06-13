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
const OAUTH_ERROR_MESSAGES = {
    oauth_cancelled:      '您已取消 Google 授權，請重新嘗試。',
    invalid_state:        '登入驗證失敗（CSRF），請重新嘗試。',
    token_exchange_failed:'與 Google 建立連線失敗，請稍後再試。',
    userinfo_failed:      '無法取得 Google 帳號資訊，請稍後再試。',
    missing_user_info:    'Google 未提供必要帳號資訊，請確認授權範圍。',
    db_error:             '伺服器發生錯誤，請稍後再試。',
    session_error:        '無法建立登入 Session，請稍後再試。',
};

// ── 偵測 OAuth callback 錯誤 ─────────────────────────────────────
// 若 Google SSO 失敗，後端會把錯誤代碼帶在 ?error= 重導回登入頁
window.addEventListener('DOMContentLoaded', () => {
    initUiTheme();
    initThemeToggle();
    if (typeof lucide !== 'undefined') lucide.createIcons();

    // 已登入則直接跳主頁
    if (localStorage.getItem('user')) {
        window.location.href = 'index.html';
        return;
    }

    const params = new URLSearchParams(window.location.search);
    const errorCode = params.get('error');
    if (errorCode) {
        const msg = OAUTH_ERROR_MESSAGES[errorCode] || `登入失敗（${errorCode}），請重新嘗試。`;
        const errorEl = document.getElementById('oauth-error');
        errorEl.textContent = msg;
        errorEl.classList.remove('hidden');

        // 清掉 URL 上的 ?error= 參數（不影響頁面功能，美觀）
        window.history.replaceState({}, '', window.location.pathname);
    }
});

// ── Google SSO 按鈕 ───────────────────────────────────────────────
document.getElementById('google-login-btn').addEventListener('click', () => {
    const btn = document.getElementById('google-login-btn');
    btn.disabled = true;
    btn.querySelector('span').textContent = '跳轉至 Google...';

    // 帶上目前前端 origin，OAuth 完成後回到同一網址（區網 IP 不會被導到 localhost）
    const returnUrl = encodeURIComponent(window.location.origin);
    window.location.href = `${API_BASE}/user/auth/google/start?return_url=${returnUrl}`;
});
