# 🔐 身份驗證與授權系統規格 (Auth System Specification)

## 1. 概述
本系統負責管理 Stock Insight Chat 的使用者帳戶安全性。採用基於 JWT (JSON Web Token) 的非狀態化驗證機制，並設計為可擴充以支援 OAuth2 (如 Google SSO)。

## 2. 核心技術選型
- **密碼加密**: Argon2 或 Bcrypt (強烈建議 Argon2)
- **驗證機制**: JWT (Header: `Authorization: Bearer <token>`)
- **權杖策略**: 
    - `access_token`: 效期 30 分鐘，用於 API 認證。
    - `refresh_token`: 效期 7 天，存於 `HttpOnly` Cookie，用於無感刷新。
- **資料庫實體**: `users`, `refresh_tokens`, `subscription_tiers`

## 3. 流程詳細設計

### A. 註冊流程 (Register)
1. 前端發送 `email`, `username`, `password`。
2. 後端檢查 Email/Username 是否重複。
3. 使用 Argon2 對密碼進行 Salted Hashing。
4. 寫入 `users` 表，並預設分配 `Free` 等級的 `tier_id`。
5. 初始化 `user_usage_quotas` (Token 配額)。

### B. 登入流程 (Login - 本地)
1. 前端發送 `email` 與 `password`。
2. 後端從 DB 撈取 `password_hash` 進行比對。
3. 比對成功後，產生一組 `access_token` 與 `refresh_token`。
4. 將 `refresh_token` 存入 DB 並寫入客戶端 `HttpOnly` Cookie。
5. 回傳 `access_token` 與使用者基本資訊。

### C. 登出流程 (Logout)
1. 清除 DB 中該使用者的 `refresh_tokens`。
2. 清除客戶端的 `refresh_token` Cookie。

### D. Google SSO 整合策略 (未來)
1. 前端啟動 Google Login，取得 Google 核發的 `id_token`。
2. 前端將 `id_token` 發送給後端 `/auth/google`。
3. 後端驗證 Google Token 的有效性。
4. 若該 Email 尚未存在於系統，自動建立新帳號。
5. 後端核發本系統的 `access_token` 與 `refresh_token` 給前端，後續流程與本地登入一致。

## 4. 安全性考量
- **HttpOnly & Secure**: 防止 XSS 攻擊讀取 Refresh Token。
- **CSRF Protection**: Access Token 透過 Header 傳遞，天然具備基本的 CSRF 防禦能力。
- **Token Rotation**: 每次刷新 Access Token 時，可選擇性地更新 Refresh Token 以增加安全性。
