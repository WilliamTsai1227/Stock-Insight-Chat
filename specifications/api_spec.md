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
| **深度研究** | 取得可選模型與上限 | `/api/deep-research/config` | `GET` | 是 |
| | 執行研究（multipart → SSE） | `/api/deep-research/runs` | `POST` | 是 |
| | 產生報告／簡報（SSE） | `/api/deep-research/runs/{sid}/artifacts` | `POST` | 是 |
| | 下載產出（主要格式） | `/api/deep-research/runs/{sid}/artifacts/{kind}` | `GET` | 是 |
| | 下載產出（指定格式） | `/api/deep-research/runs/{sid}/artifacts/{kind}/{fmt}` | `GET` | 是 |
| | 釋放 session | `/api/deep-research/runs/{sid}` | `DELETE` | 是 |

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
  "model": "gpt-5.6-luna（僅 chat_mode=general 生效）",
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
| `model` | string | 否 | `chat_mode=general` 時生效：使用者選的模型 id。只接受 `GET /api/chat/models` 的白名單，其餘退回預設模型（最長 64 字元） |
| `agent_config` | object | 否 | 含 `enabled_tools`；空則由 Agent 自行判斷 |

### `GET /api/chat/models`

一般對話可選的模型清單（前端輸入框旁的下拉選單用）。

```json
{ "status": "success",
  "data": { "default_model": "gpt-5.6-luna",
            "models": [
              {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "description": "…"},
              {"id": "gpt-5.4-mini", "label": "GPT-5.4 mini", "description": "…"}
            ] } }
```

清單是後端白名單（`general_chat.GENERAL_CHAT_MODEL_CATALOG`）：model id 直接決定單價，
而 `token_usage.TOKEN_COST_TABLE` 以 id 對照費率，因此不接受清單外的值。
`default_model` 來自 `GENERAL_CHAT_MODEL`，同樣只能指到白名單內。
股市 Agent 的 router / analyst 模型寫死在後端，不在此清單也不開放切換。

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

## 8. 深度研究模組（Deep Research）

[`deep_research.py`](../app/backend/api/deep_research.py)：以 **OpenAI Agents SDK** 的 hosted tools
（`WebSearchTool` / `FileSearchTool`）執行一次性研究，再交給報告／簡報 skill 產出可下載的 HTML。

**MVP 不落地任何資料**：session 只放在 backend 記憶體（`SessionStore`，TTL 由
`DEEP_SEARCH_SESSION_TTL_MINUTES` 控制，預設 120 分鐘），前端重新整理即失去 `session_id`；
上傳的文件只在研究期間存在於 OpenAI 的臨時 vector store，研究結束（含失敗）立刻刪除。
完整設計見 [`deep_search.md`](./deep_search.md) §21。

**唯一的例外是 token 用量**：深度研究與聊天吃同一份月配額。`POST /runs` 與
`POST /runs/{sid}/artifacts` 都會先做配額 pre-flight（已達上限回 **429**，body 與聊天相同：
`{"detail": {"code": "quota_exceeded", "used_tokens", "monthly_token_limit", "quota_resets_at"}}`），
結束後把用量寫進 `user_usage_quotas` 與 `token_usage_logs`（`chat_id` 為 NULL，
`caller` = `deep_research` / `deep_research_report` / `deep_research_deck`）。
中途失敗或前端斷線時，已經花掉的 token 一樣入帳 —— 否則中斷就成了免費研究的後門。

### `GET /api/deep-research/config`

回傳前端初始化所需的設定。

```json
{ "status": "success",
  "data": { "default_model": "gpt-5.6-luna",
            "models": [{"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna", "description": "…"}],
            "themes": [{"id": "consulting", "label": "顧問簡報", "description": "…",
                        "swatch": "#1b4fd8", "surface": "#ffffff", "ink": "#0d1b33"}],
            "default_theme": "consulting",
            "max_files": 10, "max_file_mb": 20, "max_images": 4,
            "query_max_chars": 4000, "accepted_extensions": [".csv", ".docx", "…"],
            "length_specs": {
              "report": {"label": "小節數", "unit": "節", "hint": "…",
                         "min": 3, "max": 10, "default": 6},
              "deck":   {"label": "頁數", "unit": "頁", "hint": "…",
                         "min": 5, "max": 20, "default": 12} } } }
```

`models` 是後端白名單（`config.MODEL_CATALOG`），目前只有 `gpt-5.6-luna`。
`DEEP_SEARCH_MODEL` / `DEEP_SEARCH_MODELS` 只能在白名單內挑選，設白名單外的值會被忽略
並在 log 提醒一次 —— 換模型等於換費率，而費率表與配額扣點都以 model id 對照。
前端在只有一個可選模型時會把模型按鈕鎖成純標示（不再是下拉選單）。

### `POST /api/deep-research/runs`

`multipart/form-data`：`query`（必填）、`model`（選填，不在清單內一律退回預設）、`files`（可多個）。
回應為 `text/event-stream`。

| 事件 | payload | 說明 |
|------|---------|------|
| `session` | `{session_id, model, sources}` | 第一個事件，前端據此記住 `session_id` |
| `status` | `{stage, text}` | 階段變更（`ingest` / `research`） |
| `warning` | `{messages[]}` | 個別檔案解析失敗，不中斷研究 |
| `sources_ready` | `{sources: [{name, channel}]}` | 每個檔案實際走哪條路（見下表） |
| `tool_start` / `tool_done` | `{tool}` | hosted tool 呼叫，`tool` 已是中文標籤 |
| `thinking` | `{text}` | 模型推理中 |
| `writing` | `{text}` | 開始輸出正文 |
| `delta` | `{text}` | 正文 token |
| `done` | `{session_id, markdown, citations, tools_used, elapsed_ms, usage, skills}` | 完成；`usage` = `{prompt_tokens, completion_tokens, total_tokens, requests}` |
| `error` | `{message}` | 失敗 |

沉默超過 15 秒會送出 `: keepalive` 註解行，避免 ALB / nginx 判定連線閒置而中斷。

**檔案依副檔名分三條路**（OpenAI File Search 不支援試算表，圖片也不該進向量庫）：

| channel | 副檔名 | 處理方式 |
|---------|--------|----------|
| `file_search` | `.pdf` `.docx` `.pptx` `.txt` `.md` `.json` `.html` | 上傳臨時 vector store，交給 `FileSearchTool` |
| `spreadsheet` | `.xlsx` `.xlsm` `.csv` | 本地轉 Markdown 表格，直接放進 prompt |
| `image` | `.png` `.jpg` `.jpeg` `.webp` `.gif` | 轉 base64 data URL，以 `input_image` 傳給模型 |

`.doc` / `.xls` / `.ppt` 會回 400 並提示改存新格式。

**錯誤**：`400` 題目為空／超長／檔案格式或大小不符；`409` 同一使用者已有研究在跑；
`429` 月配額已用盡（pre-flight，尚未讀檔也未打 OpenAI）；`502` vector store 建立失敗；
`503` 未設定 `OPENAI_API_KEY`。

### `POST /api/deep-research/runs/{session_id}/artifacts`

Body `{"kind": "report" | "deck", "theme": "consulting", "length": 12}`
（`theme` 與 `length`皆選填；不認得的 `theme` 退回預設，`length` 會被 clamp 進範圍）。
同樣回 SSE（`status` → `done` / `error`），`done` 帶
`{kind, label, filename, size, theme, length, usage, download_path, formats}`。
產檔是研究之外的另一次模型呼叫，因此有自己的配額 pre-flight（超額回 **429**）與
獨立的 `token_usage_logs` 一列。

`formats` 是這次產出的每一種格式（陣列，第一個是主要格式）：
`{fmt, label, filename, size, download_path}`。報告是 `docx` + `html`、
簡報是 `pptx` + `html`；不帶格式的 `filename` / `size` / `download_path`
一律指主要格式（Office 檔）。

**篇幅**（簡報頁數／報告小節數）由 `length` 指定，範圍與預設值見 `GET /config` 的
`length_specs`（簡報 5–20 頁預設 12、報告 3–10 節預設 6，可用 `DEEP_SEARCH_DECK_SLIDES`
／`DEEP_SEARCH_REPORT_SECTIONS` 改預設）。範圍不是排版限制（樣板本身不限頁數），
而是成本與品質的煞車 —— 這個值直接來自瀏覽器且會變成模型的輸出量。

**上限有三道，職責不同：**

| 層 | 做什麼 | 越界時 |
|----|--------|--------|
| 前端 `<input>` 的 `min`/`max` + change 時 clamp | 體感層，直打 API 可繞過 | 靜靜修正 |
| API `exceeds_hard_max()`，絕對上限 `LENGTH_HARD_MAX = 20` | 擋掉只可能來自直打的值（`500`、`-1`、非整數） | `400 篇幅請介於 1 到 20 之間。` |
| `resolve_length()` clamp 進各 skill 區間 | 區間內的偏差（如簡報填 25） | 靜靜 clamp，`done` 事件回報實際採用值 |

`LENGTH_SPECS` 的 `max` 與環境變數給的 `default` 在程式啟動時都會被壓進
`LENGTH_HARD_MAX` 與區間內 —— `default` 是唯一能繞過 `resolve_length()` 的路徑
（`length` 沒送時直接回傳），沒有這層校正，`DEEP_SEARCH_DECK_SLIDES=999`
會讓每一次產出都跑 999 頁而且從 API 看不出異常。

數字只寫進 skill 的 instructions，後端**不裁切**產出：截斷會砍掉有內容的頁，
而結構化輸出對「剛好 N 個」的命中率夠好，偶爾偏一頁的成本低於硬砍一頁。

**視覺主題**由 [`templates/themes.py`](../app/backend/deep_research/templates/themes.py) 定義，
一個主題 = 色票 + 字體配對 + 幾個排版取向（`kicker` 樣式、分隔線樣式、標題字重）。
目前五組：`editorial`（編輯部）、`consulting`（顧問簡報，預設）、`midnight`（暗夜）、
`minimal`（極簡）、`warm`（暖刊）。報告與簡報可各自選不同主題。
字體只用系統堆疊 —— 產出的檔案會被下載到本機離線開啟，不能依賴 webfont。

模型只產生**結構化 JSON**（`ReportDoc` / `DeckDoc`），HTML 由
[`templates/`](../app/backend/deep_research/templates/) 的樣板決定性地組出來 ——
模型漏一個結束標籤就會毀掉整份檔案，改成填 JSON 之後最差只是內容平庸而非版面破碎。

### `GET /api/deep-research/runs/{session_id}/artifacts/{kind}[/{fmt}]`

下載已產生的檔案。`Content-Disposition: attachment`，中文檔名以 RFC 5987 的
`filename*` 編碼。需要 Bearer AT，因此前端以 `authFetch` 取 Blob 後再觸發下載，
而非直接開連結。

| `kind` | `fmt` | 回傳 |
|--------|-------|------|
| `report` | `docx`（主要） | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` |
| `report` | `html` | `text/html; charset=utf-8` |
| `deck` | `pptx`（主要） | `application/vnd.openxmlformats-officedocument.presentationml.presentation` |
| `deck` | `html` | `text/html; charset=utf-8` |

省略 `fmt` 會拿到主要格式 —— 這條舊網址保留著，是為了讓 CDN 上還沒換掉的舊版
前端仍拿得到檔案。要求不存在的格式回 `404`，訊息會列出這份產出實際有哪些格式。

### `DELETE /api/deep-research/runs/{session_id}`

主動釋放記憶體 session；不呼叫也會由 TTL 清掉。

---

## 9. 環境變數參考

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
