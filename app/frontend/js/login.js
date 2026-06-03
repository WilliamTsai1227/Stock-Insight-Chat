// ============================================
// Login Page Logic（Google SSO Only）
// ============================================

const API_BASE = resolveStockInsightApiBase();

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

    // 直接導向後端的 OAuth start 端點
    // 後端會產生 state、設 Cookie、302 到 Google 授權頁面
    window.location.href = `${API_BASE}/user/auth/google/start`;
});
