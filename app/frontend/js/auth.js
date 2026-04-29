// ============================================
// Auth Module (共用認證管理)
// 用途: 管理登入狀態、Token 刷新、使用者選單
// 在 index.html 中引入使用
// ============================================

const AUTH_API = 'http://localhost:8000/api';

// --- Token 管理 ---
function getAccessToken() {
    return localStorage.getItem('access_token');
}

function getUser() {
    try {
        return JSON.parse(localStorage.getItem('user'));
    } catch {
        return null;
    }
}

function isLoggedIn() {
    return !!getAccessToken();
}

// 解碼 JWT payload (Base64Url 解碼)
function decodeJwtPayload(token) {
    try {
        const base64Url = token.split('.')[1];
        const base64 = base64Url.replace(/-/g, '+').replace(/_/g, '/');
        const jsonPayload = decodeURIComponent(atob(base64).split('').map(function (c) {
            return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
        }).join(''));
        return JSON.parse(jsonPayload);
    } catch (e) {
        return null;
    }
}

// 帶有認證的 fetch 封裝
async function authFetch(url, options = {}) {
    let token = getAccessToken();
    if (!token) {
        window.location.href = 'login.html';
        return;
    }

    // 無縫更新 (事前檢查 AT 剩餘期限)
    const payload = decodeJwtPayload(token);
    if (payload && payload.exp) {
        const currentTime = Math.floor(Date.now() / 1000);
        // 若過期時間小於等於 90 秒，則先發送 refresh 拿新 AT
        if (payload.exp - currentTime <= 90) {
            const refreshed = await tryRefreshToken();
            if (refreshed) {
                token = getAccessToken(); // 更新為新的 token
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

    // AT 過期 → 嘗試用 RT 刷新
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

// 嘗試刷新 Access Token
async function tryRefreshToken() {
    try {
        const res = await fetch(`${AUTH_API}/user/refresh`, {
            method: 'POST',
            credentials: 'include' // 帶上 HttpOnly Cookie
        });

        if (!res.ok) return false;

        const data = await res.json();
        localStorage.setItem('access_token', data.access_token);
        return true;
    } catch {
        return false;
    }
}

// 登出
async function logout() {
    try {
        await fetch(`${AUTH_API}/user/logout`, {
            method: 'POST',
            credentials: 'include'
        });
    } catch {
        // 靜默失敗
    }
    localStorage.removeItem('access_token');
    localStorage.removeItem('user');
    window.location.href = 'login.html';
}

// --- 使用者選單初始化 ---
function initUserMenu() {
    const user = getUser();
    if (!user) return;

    // 更新頭像文字
    const avatarEl = document.getElementById('user-avatar');
    const nameEl = document.getElementById('user-display-name');
    if (avatarEl) {
        avatarEl.textContent = (user.username || user.email || 'U').charAt(0).toUpperCase();
    }
    if (nameEl) {
        nameEl.textContent = user.username || user.email;
    }

    // 下拉選單開關
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

    // 登出按鈕
    const logoutBtn = document.getElementById('menu-logout');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', (e) => {
            e.preventDefault();
            logout();
        });
    }

    // 會員資料按鈕
    const profileBtn = document.getElementById('menu-profile');
    if (profileBtn) {
        profileBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openProfileModal();
        });
    }

    // 修改密碼按鈕
    const passwordBtn = document.getElementById('menu-password');
    if (passwordBtn) {
        passwordBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openPasswordModal();
        });
    }

    // 刪除帳號按鈕
    const deleteBtn = document.getElementById('menu-delete');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', (e) => {
            e.preventDefault();
            openDeleteModal();
        });
    }
}

// --- 個人資料 Modal ---
async function openProfileModal() {
    closeAllModals();
    const modal = document.getElementById('profile-modal');
    modal.classList.add('show');

    // 撈取最新資料
    try {
        const res = await authFetch(`${AUTH_API}/user`);
        if (!res || !res.ok) return;
        const user = await res.json();

        document.getElementById('profile-email').value = user.email;
        document.getElementById('profile-username').value = user.username;
        document.getElementById('profile-status').textContent = user.status || 'active';
        document.getElementById('profile-tier').textContent = user.tier_id || 'Free';

        // 更新 localStorage
        localStorage.setItem('user', JSON.stringify(user));
    } catch (err) {
        console.error('Failed to fetch profile:', err);
    }
}

// 儲存個人資料
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

        // 更新頂部頭像
        const avatarEl = document.getElementById('user-avatar');
        const nameEl = document.getElementById('user-display-name');
        if (avatarEl) avatarEl.textContent = updated.username.charAt(0).toUpperCase();
        if (nameEl) nameEl.textContent = updated.username;

        // 更新側邊欄
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

// --- 修改密碼 Modal ---
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

        // 2 秒後自動登出
        setTimeout(() => logout(), 2000);
    } catch {
        msgEl.textContent = '無法連線至伺服器';
        msgEl.className = 'modal-msg error';
    }
}

// --- 刪除帳號 Modal ---
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
            localStorage.removeItem('access_token');
            localStorage.removeItem('user');
            window.location.href = 'login.html';
        }, 2000);
    } catch {
        msgEl.textContent = '無法連線至伺服器';
        msgEl.className = 'modal-msg error';
    }
}

// --- Modal 管理 ---
function closeAllModals() {
    document.querySelectorAll('.modal-overlay').forEach(m => m.classList.remove('show'));
    // 清除訊息
    document.querySelectorAll('.modal-msg').forEach(m => {
        m.textContent = '';
        m.className = 'modal-msg';
    });
}

// 點擊遮罩關閉
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) {
        closeAllModals();
    }
});

// ESC 關閉 Modal
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllModals();
});

// --- 頁面初始化 ---
window.addEventListener('DOMContentLoaded', () => {
    // 檢查登入狀態
    if (!isLoggedIn()) {
        window.location.href = 'login.html';
        return;
    }

    initUserMenu();

    // 更新側邊欄使用者資訊
    const user = getUser();
    if (user) {
        const sidebarName = document.querySelector('.sidebar-footer .user-profile span');
        const sidebarAvatar = document.querySelector('.sidebar-footer .avatar');
        if (sidebarName) sidebarName.textContent = user.username || user.email;
        if (sidebarAvatar) sidebarAvatar.textContent = (user.username || 'U').charAt(0).toUpperCase();
    }
});
