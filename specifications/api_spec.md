# Stock Insight Chat API 規格書

> 認證方式：**Google SSO Only**（無本地密碼註冊/登入）。  
> 所有需要身分驗證的端點均需在 Header 帶 `Authorization: Bearer <access_token>`。

---

## 1. 端點清單

| 模組 | 功能 | 路徑 | 方法 | 需要認證 |
|------|------|------|:----:|:--------:|
| **認證** | 啟動 Google 登入 | `/api/user/auth/google/start` | `GET` | 否 |
| | Google OAuth Callback | `/api/user/auth/google/callback` | `GET` | 否 |
| | 登出 | `/api/user/logout` | `POST` | 否（帶 Cookie） |
| | 刷新 Access Token | `/api/user/refresh` | `POST` | 否（帶 Cookie） |
| **使用者** | 取得個人資料 | `/api/user` | `GET` | 是 |
| | 修改個人資料 | `/api/user` | `PATCH` | 是 |
| | 刪除帳號 | `/api/user` | `DELETE` | 是 |
| | 提交建議回饋 | `/api/user/feedback` | `POST` | 是 |
| **對話** | 建立對話 | `/api/chat` | `POST` | 是 |
| | 取得所有對話 | `/api/chat/all` | `GET` | 是 |
| | 取得單一對話（訊息） | `/api/chat` | `GET` | 是 |
| | 發送訊息（SSE） | `/api/chat/messages` | `POST` | 是 |
| | 修改對話標題 | `/api/chat` | `PATCH` | 是 |
| | 刪除對話 | `/api/chat` | `DELETE` | 是 |
| | 指派對話至專案 | `/api/chat/project` | `POST` | 是 |
| | 將對話移出專案 | `/api/chat/project` | `DELETE` | 是 |
| **專案** | 取得所有專案 | `/api/project/all` | `GET` | 是 |
| | 建立專案 | `/api/project` | `POST` | 是 |
| | 修改專案 | `/api/project` | `PATCH` | 是 |
| | 刪除專案 | `/api/project` | `DELETE` | 是 |

---

## 2. 認證模組（Authentication）

### Google OAuth 登入流程

```
使用者點擊「Login with Google」
    ↓
GET /api/user/auth/google/start
    ↓ 302
Google 授權頁面（使用者選擇帳號、同意授權）
    ↓ 302 with code & state
GET /api/user/auth/google/callback
    ↓ 驗 state → 換 token → upsert user → 簽發 RT Cookie
    ↓ 302
前端首頁（自動 POST /api/user/refresh → 取 AT）
```

---

### `GET /api/user/auth/google/start`

啟動 Google OAuth，重新導向到 Google 授權頁面。

**請求**：無需 body，直接導向此 URL（瀏覽器跳轉或 `window.location.href`）。

**回應**：`302 Found`，`Location` 指向 Google 授權 URL。

同時設定 `oauth_state` HttpOnly Cookie（10 分鐘有效），用於 CSRF 防護。

**環境變數需求**：`GOOGLE_CLIENT_ID`、`GOOGLE_OAUTH_REDIRECT_URI`。

---

### `GET /api/user/auth/google/callback`

Google 完成授權後自動呼叫此端點（由 Google 重導）。

**Query Parameters**（由 Google 帶入）：

| 參數 | 型別 | 說明 |
|------|------|------|
| `code` | string | Google 授權碼 |
| `state` | string | CSRF 防護用 state |
| `error` | string | 使用者取消時由 Google 帶入 |

**成功回應**：`302 Found`，重導到 `FRONTEND_URL`，並設定：
- `refresh_token`：HttpOnly Cookie，有效期 `REFRESH_TOKEN_EXPIRE_DAYS` 天

**失敗回應**：`302 Found`，重導到 `FRONTEND_URL/?error=<error_code>`

| error_code | 原因 |
|---|---|
| `oauth_cancelled` | 使用者在 Google 頁面取消 |
| `invalid_state` | CSRF 驗證失敗 |
| `token_exchange_failed` | Google token exchange 失敗 |
| `userinfo_failed` | 無法取得 Google UserInfo |
| `missing_user_info` | Google 未回傳 sub 或 email |
| `db_error` | 資料庫操作失敗 |
| `session_error` | 無法建立 Session |

**User Upsert 邏輯**：
1. 以 `google_sub` 查找已存在帳號 → 更新 `last_login_at`
2. 找不到但 email 相符 → 補綁 `google_sub`（舊帳號升級）
3. 找不到 → 建立新帳號（`username` 由 `display_name` 衍生，自動確保唯一）

---

### `POST /api/user/logout`

撤銷當前 Refresh Token，清除 Cookie。

**請求**：無需 body，Cookie 中需含 `refresh_token`。

**回應**：
```json
{ "status": "success", "message": "Logged out successfully" }
```

---

### `POST /api/user/refresh`

RT Rotation：換取新的 Access Token + 新的 Refresh Token。

**請求**：無需 body，Cookie 中需含 `refresh_token`。

**回應** `200 OK`：
```json
{
  "status": "success",
  "access_token": "<jwt>",
  "token_type": "bearer"
}
```

同時更新 `refresh_token` Cookie（舊 RT 被原子消費）。

**安全機制**：若 RT 已被消費（Token Reuse 攻擊），立即撤銷該 user 所有 Session，回傳 `401`。

**錯誤回應**：

| HTTP | detail |
|------|--------|
| `401` | `Refresh token missing` |
| `401` | `Refresh token invalid or expired. Please login again.` |
| `401` | `Security alert: Token reuse detected. All sessions have been revoked.` |
| `401` | `User not found` |
| `500` | `Failed to rotate refresh token.` |

---

## 3. 使用者模組（User Management）

### `GET /api/user`

取得當前登入使用者的個人資料。

**Header**：`Authorization: Bearer <access_token>`

**回應** `200 OK`：
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "username": "john_doe",
  "status": "active",
  "tier_id": "uuid or null"
}
```

---

### `PATCH /api/user`

修改個人資料（目前僅支援 `username`）。

**Header**：`Authorization: Bearer <access_token>`

**Request Body**：
```json
{ "username": "new_username" }
```

**回應** `200 OK`：同 `GET /api/user`。

---

### `DELETE /api/user`

永久刪除帳號（CASCADE 刪除所有相關對話、訊息、Token）。

**Header**：`Authorization: Bearer <access_token>`

**回應** `200 OK`：
```json
{ "status": "success", "message": "Account has been permanently deleted." }
```

---

## 4. 對話模組（Chat）

### `GET /api/chat/all`

取得當前使用者的所有對話，按 `updated_at` 降冪排序。

**Header**：`Authorization: Bearer <access_token>`

**回應** `200 OK`：
```json
[
  {
    "id": "uuid",
    "title": "台積電分析",
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T10:05:00Z",
    "project_id": "uuid or null"
  }
]
```

---

### `POST /api/chat/messages`

發送訊息，以 **Server-Sent Events（SSE）** 串流回傳 AI 回應。

**Header**：`Authorization: Bearer <access_token>`

**Request Body**：
```json
{
  "query": "台積電最新財報分析",
  "chat_id": "必填，需先呼叫 POST /api/chat 取得",
  "chat_mode": "general | stock_agent",
  "response_mode": "thinking | flash",
  "agent_config": {
    "enabled_tools": ["search_stock_news", "search_market_ai_analysis", "get_market_recommendations"]
  }
}
```

| 欄位 | 型別 | 必填 | 說明 |
|------|------|:----:|------|
| `query` | string | 是 | 使用者問題（最長 2000 字元） |
| `chat_id` | UUID | 是 | 由 `POST /api/chat` 取得 |
| `chat_mode` | string | 否 | `general`（一般對話）或 `stock_agent`（股市 Agent，預設） |
| `response_mode` | string | 否 | `chat_mode=stock_agent` 時生效：`thinking`（預設）或 `flash` |
| `agent_config` | object | 否 | 含 `enabled_tools`；空則由 Agent 自行判斷 |

**SSE Event 格式**：

| event type | data | 說明 |
|---|---|---|
| `thinking` | `{ "text": "..." }` | Router 思考片段（股市 Agent） |
| `token` | `{ "text": "..." }` | 串流 token |
| `tool_start` | `{ "tool": "tavily_global_search" }` | 工具開始執行 |
| `tool_done` | `{ "tool": "tavily_global_search" }` | 工具執行完成 |
| `title_update` | `{ "title": "..." }` | 首則訊息時產出的正式標題 |
| `done` | `{ "final_content": "...", "steps": [...], "retrieval_sources": [...], "total_execution_time": ... }` | 完成，含執行軌跡與耗時 |
| `error` | `{ "message": "..." }` | 錯誤 |

---

## 5. 專案模組（Project）

### `GET /api/project/all`

取得當前使用者的所有專案，按 `updated_at` 降冪排序。

**回應** `200 OK`：
```json
[
  {
    "id": "uuid",
    "name": "Q2 財報分析",
    "created_at": "2026-06-01T10:00:00Z",
    "updated_at": "2026-06-01T10:05:00Z"
  }
]
```

### `POST /api/project`

建立新專案。Request Body：`{ "name": "專案名稱" }`

### `PATCH /api/project`

修改專案名稱。Request Body：`{ "project_id": "uuid", "name": "新名稱" }`

### `DELETE /api/project`

刪除專案（CASCADE 刪除所有子對話）。Query：`?project_id=uuid`

---

## 6. 環境變數參考

| 變數 | 說明 | 範例 |
|------|------|------|
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID | `xxx.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client Secret | `GOCSPX-...` |
| `GOOGLE_OAUTH_REDIRECT_URI` | Callback URI（需與 Console 一致） | `http://localhost:8000/api/user/auth/google/callback` |
| `FRONTEND_URL` | 前端根網址（登入後重導目標） | `http://localhost` |
| `COOKIE_SECURE` | Cookie 是否只在 HTTPS 送出 | `false`（開發）/ `true`（正式） |
| `SECRET_KEY` | JWT 簽名密鑰 | 隨機長字串 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | RT 有效天數（預設 7） | `7` |
