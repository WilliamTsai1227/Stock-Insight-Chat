// ============================================
// Login / Register Page Logic
// Handles: 登入、註冊、表單切換、密碼強度
// ============================================

const API_BASE = resolveStockInsightApiBase();

// --- DOM Elements ---
const loginForm = document.getElementById('login-form');
const registerForm = document.getElementById('register-form');

// --- Form Switching ---
document.getElementById('show-register').addEventListener('click', (e) => {
    e.preventDefault();
    loginForm.classList.remove('active');
    registerForm.classList.add('active');
    clearErrors();
});

document.getElementById('show-login').addEventListener('click', (e) => {
    e.preventDefault();
    registerForm.classList.remove('active');
    loginForm.classList.add('active');
    clearErrors();
});

// --- Password Visibility Toggle ---
function setupPasswordToggle(toggleId, inputId) {
    const toggle = document.getElementById(toggleId);
    const input = document.getElementById(inputId);
    if (!toggle || !input) return;
    
    toggle.addEventListener('click', () => {
        const isPassword = input.type === 'password';
        input.type = isPassword ? 'text' : 'password';
        // 更新圖標
        const icon = toggle.querySelector('i');
        icon.setAttribute('data-lucide', isPassword ? 'eye-off' : 'eye');
        lucide.createIcons();
    });
}

setupPasswordToggle('toggle-login-pw', 'login-password');
setupPasswordToggle('toggle-reg-pw', 'reg-password');

// --- Password Strength Indicator ---
const regPassword = document.getElementById('reg-password');
if (regPassword) {
    regPassword.addEventListener('input', () => {
        const val = regPassword.value;
        const fill = document.getElementById('strength-fill');
        const text = document.getElementById('strength-text');
        
        let score = 0;
        if (val.length >= 8) score++;
        if (val.length >= 12) score++;
        if (/[A-Z]/.test(val)) score++;
        if (/[0-9]/.test(val)) score++;
        if (/[^A-Za-z0-9]/.test(val)) score++;

        const levels = [
            { width: '0%', color: 'transparent', label: '' },
            { width: '20%', color: '#ff6b6b', label: '很弱' },
            { width: '40%', color: '#ffa94d', label: '弱' },
            { width: '60%', color: '#ffd43b', label: '普通' },
            { width: '80%', color: '#69db7c', label: '強' },
            { width: '100%', color: '#00d68f', label: '很強' },
        ];

        const level = val.length === 0 ? levels[0] : levels[Math.min(score, 5)];
        fill.style.width = level.width;
        fill.style.background = level.color;
        text.textContent = level.label;
        text.style.color = level.color;
    });
}

// --- Login ---
document.getElementById('login-btn').addEventListener('click', async () => {
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;
    const btn = document.getElementById('login-btn');
    const errorEl = document.getElementById('login-error');

    if (!email || !password) {
        showError(errorEl, '請填寫所有欄位');
        return;
    }

    setLoading(btn, true);
    try {
        const res = await fetch(`${API_BASE}/user/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            credentials: 'include', // 接收 HttpOnly Cookie
            body: JSON.stringify({ email, password })
        });

        const data = await res.json();

        if (!res.ok) {
            showError(errorEl, data.detail || '登入失敗，請確認帳號與密碼');
            return;
        }

        // AT 不寫入 localStorage（防 XSS）
        // index.html 載入時 auth.js 會用 RT Cookie 靜默換取 AT 存入記憶體
        localStorage.setItem('user', JSON.stringify(data.user));

        // 導向主頁
        window.location.href = 'index.html';
    } catch (err) {
        showError(errorEl, '無法連線至伺服器，請確認後端是否已啟動');
    } finally {
        setLoading(btn, false);
    }
});

// --- Register ---
document.getElementById('register-btn').addEventListener('click', async () => {
    const username = document.getElementById('reg-username').value.trim();
    const email = document.getElementById('reg-email').value.trim();
    const password = document.getElementById('reg-password').value;
    const confirm = document.getElementById('reg-password-confirm').value;
    const btn = document.getElementById('register-btn');
    const errorEl = document.getElementById('register-error');
    const successEl = document.getElementById('register-success');

    // 前端驗證
    if (!username || !email || !password || !confirm) {
        showError(errorEl, '請填寫所有欄位');
        return;
    }
    if (password.length < 8) {
        showError(errorEl, '密碼長度至少需要 8 個字元');
        return;
    }
    if (password !== confirm) {
        showError(errorEl, '兩次輸入的密碼不一致');
        return;
    }

    setLoading(btn, true);
    try {
        const res = await fetch(`${API_BASE}/user/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email, username, password })
        });

        const data = await res.json();

        if (!res.ok) {
            showError(errorEl, data.detail || '註冊失敗');
            return;
        }

        // 註冊成功：顯示成功訊息，並自動切換到登入表單
        hideError(errorEl);
        successEl.textContent = '帳號建立成功！正在跳轉至登入頁面...';
        successEl.classList.remove('hidden');

        setTimeout(() => {
            successEl.classList.add('hidden');
            registerForm.classList.remove('active');
            loginForm.classList.add('active');
            // 自動填入 email
            document.getElementById('login-email').value = email;
        }, 1500);
    } catch (err) {
        showError(errorEl, '無法連線至伺服器');
    } finally {
        setLoading(btn, false);
    }
});

// --- Enter Key Support ---
document.getElementById('login-password').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('login-btn').click();
});
document.getElementById('reg-password-confirm').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('register-btn').click();
});

// --- Utility Functions ---
function showError(el, msg) {
    el.textContent = msg;
    el.classList.remove('hidden');
}

function hideError(el) {
    el.textContent = '';
    el.classList.add('hidden');
}

function clearErrors() {
    document.querySelectorAll('.error-msg, .success-msg').forEach(el => {
        el.classList.add('hidden');
        el.textContent = '';
    });
}

function setLoading(btn, isLoading) {
    const span = btn.querySelector('span');
    const loader = btn.querySelector('.btn-loader');
    if (isLoading) {
        span.classList.add('hidden');
        loader.classList.remove('hidden');
        btn.disabled = true;
    } else {
        span.classList.remove('hidden');
        loader.classList.add('hidden');
        btn.disabled = false;
    }
}

// --- Auto-redirect if already logged in ---
// AT 已改為記憶體儲存，無法在 login 頁直接讀取
// 改用 localStorage.user 做 UX 判斷（若有 user 資料，嘗試導向主頁）
// 真正的認證由 index.html 載入時 auth.js 的 tryRefreshToken() 完成
window.addEventListener('DOMContentLoaded', () => {
    const user = localStorage.getItem('user');
    if (user) {
        window.location.href = 'index.html';
    }
});
