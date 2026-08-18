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
| | 取得當月用量統計 | `/api/user/usage` | `GET` | 是 |
| | 查今日回饋剩餘次數 | `/api/user/feedback/eligibility` | `GET` | 是 |
| | 提交建議回饋 | `/api/user/feedback` | `POST` | 是 |
| | 回饋表單公開設定 | `/api/public/feedback-config` | `GET` | **否** |
| **對話** | 建立對話 | `/api/chat` | `POST` | 是 |
| | 取得所有對話 | `/api/chat/all` | `GET` | 是 |
| | 取得單一對話（訊息） | `/api/chat` | `GET` | 是 |
| | 發送訊息（SSE） | `/api/chat/messages` | `POST` | 是 |
| | 修改對話標題 | `/api/chat` | `PATCH` | 是 |
| | 刪除對話 | `/api/chat` | `DELETE` | 是 |
| | 指派對話至專案 | `/api/chat/project` | `POST` | 是 |
| | 將對話移出專案 | `/api/chat/project` | `DELETE` | 是 |
| **專案** | 取得所有專案 | `/api/project/all` | `GET` | 是 |
| | 取得單一專案（含 chats / files） | `/api/project` | `GET` | 是 |
| | 建立專案 | `/api/project` | `POST` | 是 |
| | 刪除專案 | `/api/project` | `DELETE` | 是 |
| **檔案** | 上傳檔案 | `/api/files/upload` | `POST` | 是 |
| | 刪除檔案 | `/api/files/{file_id}` | `DELETE` | 是 |
| **探索** | Kinetic Charts 反向代理 | `/explore/{path}` | `GET`/`POST`/`DELETE` | 是（RT Cookie） |

> **沒有 `PATCH /api/project`。** 專案目前不支援改名；[`project.py`](../app/backend/api/project.py) 只實作 POST / GET all / GET / DELETE。
>
> 檔案模組（`/api/files/*`）為 **stub**，S3 上傳與檢索尚未完成，見 [`feature_mapping.md`](./feature_mapping.md)。
>
> 探索代理不出現在 Swagger（`include_in_schema=False`）；`KINETIC_UPSTREAM` 未設定時整組回 404。

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

### `GET /api/user/usage`

取得當月 Token 用量統計。`tier_id` 為 NULL 時以 free 額度 fallback。

**Header**：`Authorization: Bearer <access_token>`

**回應** `200 OK`：
```json
{
  "tier_name": "free",
  "used_tokens": 48200,
  "monthly_token_limit": 200000,
  "remaining_tokens": 151800,
  "usage_percent": 24,
  "quota_exhausted": false,
  "current_period_start": "2026-08-01T00:00:00+00:00",
  "quota_resets_at": "2026-09-01T00:00:00+00:00"
}
```

---

### `GET /api/public/feedback-config`

建議回饋表單的公開設定，**無需登入**（前端在渲染表單前呼叫）。回傳 Turnstile site key（未啟用時為 `null`）、防刷參數與 Token 獎勵規則。

---

### `GET /api/user/feedback/eligibility`

查詢目前登入使用者**今日**還能提交幾次建議回饋（上限見 `FEEDBACK_DAILY_MAX`，時區見 `FEEDBACK_DAILY_TIMEZONE`）。

---

### `POST /api/user/feedback`

提交建議或問題回饋（需登入）。成功後寫入 `user_feedback.tokens_granted`，並從 `user_usage_quotas.used_tokens` **扣除**獎勵 Token（等同回饋額度）。

防護機制：rate limit、重複內容檢查、`context` / `page_url` 驗證、honeypot、可選 Cloudflare Turnstile CAPTCHA（見 [`env.md`](./env.md) 的 `TURNSTILE_*`）。

| HTTP | 意義 |
|------|------|
| `201` | 已收到 |
| `401` | 未登入 |
| `409` | 短時間內重複相同內容 |
| `422` | 欄位驗證失敗 |
| `429` | 提交過於頻繁，或今日次數已達上限 |
| `500` | 伺服器錯誤（不含 DB 細節） |

欄位定義與 `category` / `status` 列舉見 [`database_spec.md`](./database_spec.md) §2.13。

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
    "enabled_tools": [
      "search_stock_news",
      "search_market_ai_analysis",
      "get_market_recommendations",
      "tavily_global_search"
    ]
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

### `GET /api/project`

讀取指定專案詳細資訊，含其底下的 chats 與 files 列表。Query：`?project_id=uuid`

**回應** `200 OK`：
```json
{
  "project": { "id": "uuid", "name": "Q2 財報分析", "created_at": "..." },
  "chats": [ { "id": "uuid", "title": "台積電分析" } ],
  "files": [ { "id": "uuid", "file_name": "...", "s3_url": "...", "file_type": "pdf", "status": "ready", "created_at": "..." } ]
}
```

### `POST /api/project`

建立新專案。Request Body：`{ "name": "專案名稱" }`

### `DELETE /api/project`

刪除專案（CASCADE 刪除所有子對話）。Query：`?project_id=uuid`

---

## 6. 檔案模組（Files）— stub

> ⚠️ 端點已存在但**尚未完整實作**（S3 上傳與後續檢索未完成），見 [`feature_mapping.md`](./feature_mapping.md)。

### `POST /api/files/upload`

`multipart/form-data`：`project_id`（必填）、`chat_id`（選填）、`file`。

允許的 content type：`image/jpeg`、`image/png`、`image/webp`、`application/pdf`、`text/csv`、`text/plain`、`application/vnd.ms-excel` 等。

### `DELETE /api/files/{file_id}`

刪除指定檔案紀錄。

---

## 7. 探索模組（Explore / Kinetic Charts）

backend 內建的反向代理（[`explore.py`](../app/backend/api/explore.py)），把 `/explore/*` 轉發給 docker network 內的 `kinetic` 容器。

| 路徑 | 方法 | 說明 |
|------|------|------|
| `/explore` | `GET` | **301** 重導至 `/explore/`（少了結尾斜線會讓 kinetic 頁面的相對資源路徑解析錯層級） |
| `/explore/{path}` | `GET` / `POST` / `DELETE` | 代理至 `KINETIC_UPSTREAM` |

- **認證走 RT Cookie**，不是 Bearer AT：代理會驗 `refresh_token` Cookie 為有效的 refresh 型 JWT，否則 401。kinetic 本身無認證，登入閘門完全由此把關。
- `KINETIC_UPSTREAM` 未設定時整組回 **404**（等同關閉探索功能）。
- Cookie **不會**轉發給上游。
- 兩個端點都 `include_in_schema=False`，不出現在 Swagger。

---

## 8. 環境變數參考

完整清單見 [`env.md`](./env.md)，以下僅列與本文件端點直接相關者：

| 變數 | 說明 | 範例 |
|------|------|------|
| `GOOGLE_CLIENT_ID` | Google OAuth Client ID | `xxx.apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | Google OAuth Client Secret | `GOCSPX-...` |
| `GOOGLE_OAUTH_REDIRECT_URI` | Callback URI（需與 Console 一致） | `http://localhost:8000/api/user/auth/google/callback` |
| `FRONTEND_URL` | 前端根網址（登入後重導目標） | `http://localhost` |
| `COOKIE_SECURE` | Cookie 是否只在 HTTPS 送出 | `false`（開發）/ `true`（正式） |
| `SECRET_KEY` | JWT 簽名密鑰 | 隨機長字串 |
| `KINETIC_UPSTREAM` | 探索代理上游；留空＝關閉 `/explore/*` | `http://kinetic:8000` |
| `QUERY_MAX_CHARS` | `POST /api/chat/messages` 的 `query` 長度上限 | `2000` |

> **AT / RT 效期不是環境變數。** `ACCESS_TOKEN_EXPIRE_MINUTES = 15` 與 `REFRESH_TOKEN_EXPIRE_DAYS = 7` 是 [`module/jwt.py`](../app/backend/module/jwt.py) 內的硬編碼常數，寫進 `.env` **不會生效**；要改請改程式。
