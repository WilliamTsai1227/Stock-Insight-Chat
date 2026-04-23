# 📋 專案開發待辦清單 (Project TODO List)

## 🔐 身份驗證系統 (Authentication)
- [x] **資料庫優化**: 為 `init_db.sql` 加入關鍵欄位索引 (Index)。
- [x] **系統架構設計**: 定義 JWT (AT/RT) 雙權杖認證流程。
- [x] **安全性工具**: 實作 `security.py` (Argon2 雜湊、JWT 簽發與驗證)。
- [x] **ORM 模型**: 建立 `User` 與 `RefreshToken` 的 SQLAlchemy 模型。
- [x] **API 實作**:
    - [x] 使用者註冊 (`/api/auth/register`) - 含配額初始化。
    - [x] 使用者登入 (`/api/auth/login`) - 含 Cookie 寫入。
    - [x] 使用者登出 (`/api/auth/logout`) - 含資料庫 Token 撤銷。
- [x] **自動化測試**: 建立 `test_auth_api.py` 整合測試腳本。
- [ ] **環境修復**: 解決主機 Port 5432 衝突問題 (Local Postgres vs Docker)。
- [ ] **權杖刷新**: 實作 `/api/auth/refresh` 接口，使用 RT 換取新 AT。
- [ ] **權限保護**: 將現有的 `getAIResponse` 等 API 加上 JWT 驗證裝飾器。

## 📈 核心功能擴充
- [ ] **會員資料**: 實作 `/api/user/profile` 與 `/api/user/usage` 接口。
- [ ] **歷史紀錄**: 實作對話分頁查詢功能。
- [ ] **Google SSO**: 串接 Google OAuth2 登入。

## 🎨 前端開發 (Frontend)
- [ ] **登入/註冊頁面**: 實作玻璃擬態設計的 UI。
- [ ] **權杖管理**: 實作前端自動刷新 Token 的攔截器 (Interceptor)。
- [ ] **配額顯示**: 在介面上顯示剩餘 Token 用量。

---
*最後更新日期: 2026-04-24*
