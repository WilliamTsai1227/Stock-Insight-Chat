// ============================================
// Auth Module v2（共用認證管理）
//
// 三重 Token 刷新機制：
//   機制 A：主動 Timer  → 提前 60 秒靜默背景換 AT（零等待，主要路徑）
//   機制 B：Request Interceptor → 事前檢查 90s + 401 Fallback 重試
//   機制 C：並發鎖 → 防止多個 401 同時觸發 /refresh → RT Reuse Attack
// ============================================

const AUTH_API = resolveStockInsightApiBase();

// ─── 機制 C：並發鎖 ──────────────────────────────────────────────
// 若多個請求同時收到 401，確保只有一個 /refresh 被執行
// 其餘等待同一個 Promise resolve，共用同一次換 Token 的結果
// 若不加鎖：兩個並發 401 → 觸發兩次 /refresh → 第二次用已旋轉的舊 RT
//          → 後端判定 Reuse Attack → 所有 Session 被撤銷 → 用戶被踢下線
let _isRefreshing = false;
let _refreshPromise = null;

// ─── AT 存入 JS 記憶體（防 XSS）─────────────────────────────────
// AT 不寫入 localStorage / sessionStorage（XSS 腳本無法讀取 JS 變數）
// RT 由後端設定為 HttpOnly Cookie，JS 完全無法讀取
// 代價：頁面刷新後記憶體清空，需靠 RT Cookie 自動補 AT（見 DOMContentLoaded）
let _accessToken = null;

// ─── 機制 A：主動 Timer 控制代碼 ────────────────────────────────
let _refreshTimer = null;


// ─── Token 管理 API ──────────────────────────────────────────────

function setAccessToken(token) {
    _accessToken = token;
    _scheduleProactiveRefresh(token);   // 同時重設 Timer
}

function getAccessToken() {
    return _accessToken;
}

function clearAccessToken() {
    _accessToken = null;
    if (_refreshTimer) {
        clearTimeout(_refreshTimer);
        _refreshTimer = null;
    }
}

function getUser() {
    try {
        return JSON.parse(localStorage.getItem('user'));
    } catch {
        return null;
    }
}

function isLoggedIn() {
    return !!_accessToken;
}

// 解碼 JWT Payload（Base64Url → JSON，不驗簽）
function decodeJwtPayload(token) {
    try {
        const base64Url = token.split('.')[1];
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
        const jsonPayload = decodeURIComponent(
            atob(base64).split('').map(c =>
                '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2)
            ).join('')
        );
        return JSON.parse(jsonPayload);
    } catch {
        return null;
    }
}


// ─── 機制 A：主動 Timer ──────────────────────────────────────────
// 每次取得新 AT 後，計算 exp-60s 的時間點，設定 setTimeout
// Timer 到期時在背景靜默呼叫 /refresh，用戶完全感知不到
// 若分頁在背景被瀏覽器節流，Timer 可能延誤 → 靠機制 B 補位

function _scheduleProactiveRefresh(token) {
    // 清除舊 Timer（避免多個 Timer 同時跑）
    if (_refreshTimer) {
        clearTimeout(_refreshTimer);
        _refreshTimer = null;
    }
    if (!token) return;

    const payload = decodeJwtPayload(token);
    if (!payload || !payload.exp) return;

    const nowMs = Date.now();
    const expMs = payload.exp * 1000;
    const refreshAtMs = expMs - 60 * 1000;  // 到期前 60 秒觸發
    const delayMs = refreshAtMs - nowMs;

    if (delayMs <= 0) {
        // 已在 60 秒緩衝內或已過期，立刻觸發靜默換 AT
        _silentRefresh();
        return;
    }

    _refreshTimer = setTimeout(() => {
        _refreshTimer = null;
        _silentRefresh();
    }, delayMs);
}

async function _silentRefresh() {
    const ok = await tryRefreshToken();
    if (!ok) {
        // RT 也失效（過期或 Reuse Attack 被撤銷），強制登出
        logout();
    }
}


// ─── 機制 C：tryRefreshToken（帶並發鎖）──────────────────────────
// 若已有一個 /refresh 進行中，後續呼叫直接等待同一個 Promise
// 保證每個 RT 只被後端消費一次（配合後端 DELETE...RETURNING 原子操作）

async function tryRefreshToken() {
    if (_isRefreshing) {
        return _refreshPromise;     // 等待進行中的那次，不重複發請求
    }

    _isRefreshing = true;
    _refreshPromise = (async () => {
        try {
            const res = await fetch(`${AUTH_API}/user/refresh`, {
                method: 'POST',
                credentials: 'include'  // 瀏覽器自動帶上 HttpOnly RT Cookie
            });

            if (!res.ok) return false;

            const data = await res.json();
            setAccessToken(data.access_token);  // 存入記憶體 + 重設 Timer A
            return true;
        } catch {
            return false;
        } finally {
            _isRefreshing = false;
            _refreshPromise = null;
        }
    })();

    return _refreshPromise;
}


// ─── 機制 B：authFetch（事前檢查 + 401 Fallback）────────────────
// 所有需要認證的 API 請求都透過此函式發送

async function authFetch(url, options = {}) {
    let token = getAccessToken();
    if (!token) {
        window.location.href = 'login.html';
        return;
    }

    // 事前檢查：AT 在 90 秒內到期 → 先換 AT 再發請求（比 Timer 更保守的防線）
    const payload = decodeJwtPayload(token);
    if (payload && payload.exp) {
        const currentTime = Math.floor(Date.now() / 1000);
        if (payload.exp - currentTime <= 90) {
            const refreshed = await tryRefreshToken();
            if (refreshed) {
                token = getAccessToken();
            } else {
                logout();
                return;
            }
        }
    }

    const headers = {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
        ...(options.headers || {})
    };

    let res = await fetch(url, { ...options, headers, credentials: 'include' });

    // 401 Fallback：AT 已過期（Timer/事前檢查未及時），嘗試換 AT 後重試一次
    if (res.status === 401) {
        const refreshed = await tryRefreshToken();
        if (refreshed) {
            headers['Authorization'] = `Bearer ${getAccessToken()}`;
            res = await fetch(url, { ...options, headers, credentials: 'include' });
        } else {
            logout();
            return;
        }
    }

    return res;
}


// ─── 登出 ────────────────────────────────────────────────────────

async function logout() {
    clearAccessToken();     // 清記憶體 AT + 取消 Timer
    try {
        await fetch(`${AUTH_API}/user/logout`, {
            method: 'POST',
            credentials: 'include'  // 後端會清除 RT Cookie
        });
    } catch {
        // 靜默失敗（離線登出也要能清本地狀態）
    }
    localStorage.removeItem('user');
    window.location.href = 'login.html';
}


// ─── 使用者選單初始化 ─────────────────────────────────────────────

function initUserMenu() {
    const user = getUser();
    if (!user) return;

    const avatarEl = document.getElementById('user-avatar');
    const nameEl = document.getElementById('user-display-name');
    if (avatarEl) {
        avatarEl.textContent = (user.username || user.email || 'U').charAt(0).toUpperCase();
    }
    if (nameEl) {
        nameEl.textContent = user.username || user.email;
    }

    const trigger = document.getElementById('user-menu-trigger');
    const dropdown = document.getElementById('user-dropdown');
    if (trigger && dropdown) {
        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            dropdown.classList.toggle('show');
        });
        document.addEventListener('click', () => {
            dropdown.classList.remove('show');
        });
        dropdown.addEventListener('click', (e) => e.stopPropagation());
    }

    const logoutBtn = document.getElementById('menu-logout');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            logout();
        });
    }

    const profileBtn = document.getElementById('menu-profile');
    if (profileBtn) {
        profileBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openProfileModal();
        });
    }

    const passwordBtn = document.getElementById('menu-password');
    if (passwordBtn) {
        passwordBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openPasswordModal();
        });
    }

    const deleteBtn = document.getElementById('menu-delete');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openDeleteModal();
        });
    }
}


// ─── 個人資料 Modal ──────────────────────────────────────────────

async function openProfileModal() {
    closeAllModals();
    const modal = document.getElementById('profile-modal');
    modal.classList.add('show');

    try {
        const res = await authFetch(`${AUTH_API}/user`);
        if (!res || !res.ok) return;
        const user = await res.json();

        document.getElementById('profile-email').value = user.email;
        document.getElementById('profile-username').value = user.username;
        document.getElementById('profile-status').textContent = user.status || 'active';
        document.getElementById('profile-tier').textContent = user.tier_id || 'Free';

        localStorage.setItem('user', JSON.stringify(user));
    } catch (err) {
        console.error('Failed to fetch profile:', err);
    }
}

async function saveProfile() {
    const username = document.getElementById('profile-username').value.trim();
    const msgEl = document.getElementById('profile-msg');

    if (!username) {
        msgEl.textContent = '使用者名稱不可為空';
        msgEl.className = 'modal-msg error';
        return;
    }

    try {
        const res = await authFetch(`${AUTH_API}/user`, {
            method: 'PATCH',
            body: JSON.stringify({ username })
        });

        if (!res || !res.ok) {
            const err = await res.json();
            msgEl.textContent = err.detail || '更新失敗';
            msgEl.className = 'modal-msg error';
            return;
        }

        const updated = await res.json();
        localStorage.setItem('user', JSON.stringify(updated));

        const avatarEl = document.getElementById('user-avatar');
        const nameEl = document.getElementById('user-display-name');
        if (avatarEl) avatarEl.textContent = updated.username.charAt(0).toUpperCase();
        if (nameEl) nameEl.textContent = updated.username;

        const sidebarName = document.querySelector('.sidebar-footer .user-profile span');
        const sidebarAvatar = document.querySelector('.sidebar-footer .avatar');
        if (sidebarName) sidebarName.textContent = updated.username;
        if (sidebarAvatar) sidebarAvatar.textContent = updated.username.charAt(0).toUpperCase();

        msgEl.textContent = '資料更新成功！';
        msgEl.className = 'modal-msg success';
    } catch {
        msgEl.textContent = '無法連線至伺服器';
        msgEl.className = 'modal-msg error';
    }
}


// ─── 修改密碼 Modal ──────────────────────────────────────────────

function openPasswordModal() {
    closeAllModals();
    document.getElementById('password-modal').classList.add('show');
}

async function savePassword() {
    const oldPw = document.getElementById('pw-old').value;
    const newPw = document.getElementById('pw-new').value;
    const confirmPw = document.getElementById('pw-confirm').value;
    const msgEl = document.getElementById('password-msg');

    if (!oldPw || !newPw || !confirmPw) {
        msgEl.textContent = '請填寫所有欄位';
        msgEl.className = 'modal-msg error';
        return;
    }
    if (newPw.length < 8) {
        msgEl.textContent = '新密碼至少需要 8 個字元';
        msgEl.className = 'modal-msg error';
        return;
    }
    if (newPw !== confirmPw) {
        msgEl.textContent = '兩次輸入的新密碼不一致';
        msgEl.className = 'modal-msg error';
        return;
    }

    try {
        const res = await authFetch(`${AUTH_API}/user/password`, {
            method: 'PATCH',
            body: JSON.stringify({ old_password: oldPw, new_password: newPw })
        });

        if (!res || !res.ok) {
            const err = await res.json();
            msgEl.textContent = err.detail || '密碼更新失敗';
            msgEl.className = 'modal-msg error';
            return;
        }

        msgEl.textContent = '密碼已更新，請重新登入';
        msgEl.className = 'modal-msg success';
        setTimeout(() => logout(), 2000);
    } catch {
        msgEl.textContent = '無法連線至伺服器';
        msgEl.className = 'modal-msg error';
    }
}


// ─── 刪除帳號 Modal ──────────────────────────────────────────────

function openDeleteModal() {
    closeAllModals();
    document.getElementById('delete-modal').classList.add('show');
    document.getElementById('delete-confirm-input').value = '';
    document.getElementById('delete-confirm-btn').disabled = true;
}

function checkDeleteConfirm() {
    const input = document.getElementById('delete-confirm-input').value;
    document.getElementById('delete-confirm-btn').disabled = (input !== 'DELETE');
}

async function confirmDeleteAccount() {
    const msgEl = document.getElementById('delete-msg');
    try {
        const res = await authFetch(`${AUTH_API}/user`, {
            method: 'DELETE'
        });

        if (!res || !res.ok) {
            const err = await res.json();
            msgEl.textContent = err.detail || '帳號刪除失敗';
            msgEl.className = 'modal-msg error';
            return;
        }

        msgEl.textContent = '帳號已永久刪除，即將跳轉...';
        msgEl.className = 'modal-msg success';

        setTimeout(() => {
            clearAccessToken();
            localStorage.removeItem('user');
            window.location.href = 'login.html';
        }, 2000);
    } catch {
        msgEl.textContent = '無法連線至伺服器';
        msgEl.className = 'modal-msg error';
    }
}


// ─── Modal 管理 ──────────────────────────────────────────────────

function closeAllModals() {
    document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('show'));
    document.querySelectorAll('.modal-msg').forEach(m => {
        m.textContent = '';
        m.className = 'modal-msg';
    });
}

document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        closeAllModals();
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllModals();
});


// ─── 頁面初始化 ──────────────────────────────────────────────────
// 頁面刷新後記憶體 AT 消失，優先用 RT Cookie 靜默換取新 AT
// 若 RT 也失效（過期/被撤銷），導向登入頁

window.addEventListener('DOMContentLoaded', async () => {
    const ok = await tryRefreshToken();
    if (!ok) {
        // 清除 user，防止 login.html 看到 localStorage.user 又自動 redirect 回來（無限迴圈）
        localStorage.removeItem('user');
        window.location.href = 'login.html';
        return;
    }

    initUserMenu();

    const user = getUser();
    if (user) {
        const sidebarName = document.querySelector('.sidebar-footer .user-profile span');
        const sidebarAvatar = document.querySelector('.sidebar-footer .avatar');
        if (sidebarName) sidebarName.textContent = user.username || user.email;
        if (sidebarAvatar) sidebarAvatar.textContent = (user.username || 'U').charAt(0).toUpperCase();
    }
});
