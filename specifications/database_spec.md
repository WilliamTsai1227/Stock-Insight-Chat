# 資料庫規格說明書 (Database Specification)

本專案使用 **PostgreSQL** 作為關聯式資料庫，管理會員（Google SSO）、訂閱配額、專案、對話、訊息、Token 計量與使用者回饋。向量數據存儲於 **Qdrant**，新聞／分析原文存儲於 **MongoDB**。

> - **權威來源**：實際 schema 以 [`app/backend/database/init_db.sql`](../app/backend/database/init_db.sql) 與 [`migrations/`](../app/backend/database/migrations/)（V002～V008）為準，本文件與其對齊。
> - **開發時如何進 Docker 改 SQL、套用 migration、重置本機 DB**：請見 [`sql_dev_handbook.md`](./sql_dev_handbook.md)。
> - 所有時間欄位皆為 **`TIMESTAMPTZ`**（UTC 儲存）。

## 1. 實體關係圖 (ERD)

```mermaid
erDiagram
    subscription_tiers ||--o{ users : "定義等級"
    users ||--o| user_usage_quotas : "當期用量"
    users ||--o{ token_usage_logs : "LLM 流水帳"
    users ||--o{ quota_reset_logs : "配額重置紀錄"
    users ||--o{ refresh_tokens : "多裝置 Session"
    users ||--o{ user_roles : "具備角色"
    roles ||--o{ user_roles : "定義角色"
    users ||--o| user_settings : "個人偏好"
    users ||--o{ user_feedback : "提交回饋"
    users ||--o{ projects : "擁有"
    users ||--o{ chats : "擁有 (chat 可不屬於任何 project)"
    projects |o--o{ chats : "可選歸屬"
    projects ||--o{ files : "相關文件"
    chats |o--o{ files : "可選關聯"
    chats ||--o{ messages : "對話記錄"
    chats |o--o{ token_usage_logs : "按對話統計"
    messages ||--o{ messages : "Q&A 對齊 (parent_id)"
    users ||--o{ note_pages : "筆記頁"
    note_pages ||--o{ notes : "筆記"
    chats |o--o{ note_pages : "來源對話 (一則對話最多一頁)"
    users ||--o{ portfolio_items : "庫存／觀察清單"
    users ||--o| insight_schedules : "排程設定"
    users ||--o{ insight_jobs : "分析紀錄"
    insight_jobs ||--o{ insight_results : "每檔 × 每個 tool"

    subscription_tiers {
        uuid id PK
        string name "free / pro / ultra"
        bigint monthly_token_limit
        int max_projects "DEFAULT 10"
        jsonb features
        timestamptz created_at
    }

    users {
        uuid id PK
        string email UK
        string username UK
        text google_sub UK "Google OIDC subject，身分主鍵"
        string last_login_provider "固定 'google'"
        string status "active / disabled / pending"
        uuid tier_id FK "ON DELETE SET NULL"
        timestamptz last_login_at
        timestamptz created_at
        timestamptz updated_at
    }

    user_usage_quotas {
        uuid user_id PK, FK
        timestamptz current_period_start
        bigint used_tokens
        timestamptz updated_at
    }

    refresh_tokens {
        uuid id PK
        uuid user_id FK
        text token UK "RT 字串本身為 key，DELETE...RETURNING 原子消費"
        timestamptz expires_at
        timestamptz created_at
    }

    token_usage_logs {
        uuid id PK
        uuid user_id FK
        uuid chat_id FK "nullable，按對話統計費用"
        uuid message_id "無 FK 約束，關聯最後一則 assistant message"
        string caller "router / analyst 等輪次來源"
        string model_name
        int prompt_tokens
        int completion_tokens
        int total_tokens
        numeric cost_usd
        timestamptz created_at
    }

    quota_reset_logs {
        uuid id PK
        uuid user_id FK
        timestamptz reset_at
        timestamptz previous_period_start
        bigint previous_used_tokens
        bigint period_total_tokens
        numeric period_total_cost_usd
        text note
        string reset_by "DEFAULT 'monitor'"
    }

    roles {
        uuid id PK
        string name UK "admin / user / guest"
        text description
    }

    user_roles {
        uuid user_id PK, FK
        uuid role_id PK, FK
    }

    user_settings {
        uuid user_id PK, FK
        string theme "DEFAULT 'dark'"
        string language "DEFAULT 'zh-TW'"
        boolean notifications_enabled
        jsonb settings
        timestamptz updated_at
    }

    projects {
        uuid id PK
        string name
        uuid user_id FK
        timestamptz created_at
        timestamptz updated_at
    }

    chats {
        uuid id PK
        uuid project_id FK "nullable：可獨立存在（前端「最近」區塊）"
        uuid user_id FK "NOT NULL：直接記錄 owner"
        string title
        boolean title_generated "FALSE=placeholder，TRUE=LLM 已產出"
        text summary "LLM 對話摘要，優化 Context 載入"
        timestamptz created_at
        timestamptz updated_at
    }

    messages {
        uuid id PK
        uuid chat_id FK
        uuid parent_id FK "ON DELETE SET NULL"
        string role "user / assistant"
        text content
        jsonb tokens "prompt/completion/total/is_cached"
        jsonb context_refs "檢索來源片段"
        jsonb metadata "系統元數據"
        timestamptz created_at
    }

    files {
        uuid id PK
        uuid project_id FK "NOT NULL"
        uuid chat_id FK "nullable, ON DELETE SET NULL"
        string file_name
        text s3_url
        string file_type "image / pdf ..."
        string status "uploading / ready / failed"
        timestamptz created_at
    }

    user_feedback {
        uuid id PK
        uuid user_id FK
        string category "feature / bug / ux / billing / other"
        text message "使用者手動輸入的回饋內容"
        string page_url "送出當下的頁面路徑"
        text user_agent "瀏覽器與裝置識別字串"
        jsonb context "當下頁面情境快照"
        string status "new / reviewed / resolved ..."
        bigint tokens_granted "本次發放的 Token 獎勵"
        timestamptz created_at
        timestamptz updated_at
    }
```

---

## 2. 資料表詳細定義

### 2.1 subscription_tiers (訂閱等級)

定義會員等級的權利與配額。`init_db.sql` 內建種子資料（migration `V005` 可對齊既有庫）：

| name | monthly_token_limit | max_projects |
| :--- | ---: | ---: |
| free | 200,000 | 10 |
| pro | 1,000,000 | 20 |
| ultra | 5,000,000 | 999,999（實務上不限） |

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 等級唯一識別碼 |
| name | VARCHAR(50) | UNIQUE, NOT NULL | 等級名稱 (free, pro, ultra) |
| monthly_token_limit | BIGINT | NOT NULL | 每月 Token 額度 |
| max_projects | INTEGER | DEFAULT 10 | 最大專案數 |
| features | JSONB | DEFAULT '{}' | 功能開關設定 |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 建立時間 |

### 2.2 users (使用者)

會員系統核心表。**僅支援 Google SSO 登入**（無密碼欄位）；`google_sub`（Google OIDC subject）為身分主鍵，登入時以 `WHERE google_sub = $1` 查找。詳見 [`auth_system_spec.md`](./auth_system_spec.md) §6。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 使用者唯一識別碼 |
| email | VARCHAR(255) | UNIQUE, NOT NULL | 電子郵件（取自 Google UserInfo） |
| username | VARCHAR(100) | UNIQUE, NOT NULL | 顯示名稱 |
| google_sub | TEXT | UNIQUE, NOT NULL | Google OIDC subject，身分主鍵 |
| last_login_provider | VARCHAR(32) | DEFAULT 'google' | 登入供應商（目前固定 google） |
| status | VARCHAR(20) | DEFAULT 'active' | 帳號狀態 (active, disabled, pending) |
| tier_id | UUID | FK → subscription_tiers.id, ON DELETE SET NULL | 目前等級；NULL 時後端以 free 等級額度 fallback |
| last_login_at | TIMESTAMPTZ | NULLABLE | 最後登入時間 |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 建立時間 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 更新時間 |

### 2.3 user_usage_quotas (當前用量)

使用者在當前計量週期內的即時累計用量。**高頻更新的計數器**，與 `token_usage_logs`（append-only 流水）分表以兼顧效能與對帳。配額檢查邏輯見 [`usage_quota.py`](../app/backend/module/usage_quota.py)：Pre-flight 429（入口攔截）+ 原子遞增 `UPDATE ... SET used_tokens = used_tokens + delta`（`increment_used_tokens`，允許單輪略超、由下一輪 pre-flight 攔截的「最終一致」策略）。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| user_id | UUID | PRIMARY KEY, FK → users.id, ON DELETE CASCADE | 使用者 ID（一人一列） |
| current_period_start | TIMESTAMPTZ | NOT NULL | 當前週期開始時間（月初，UTC） |
| used_tokens | BIGINT | DEFAULT 0 | 已消耗總 Token 數 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 最後更新時間 |

### 2.4 refresh_tokens (JWT 刷新權杖)

RT Rotation 的核心表。每次登入 INSERT 一筆獨立 RT（**多裝置支援**）；refresh 時以 `DELETE ... WHERE token = $1 AND expires_at > NOW() RETURNING user_id` **原子消費**，併發下僅一個請求成功——DELETE 到 0 列但簽名仍有效即判定 Token Reuse Attack，撤銷該用戶所有 session。完整流程見 [`auth_system_spec.md`](./auth_system_spec.md)。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 所屬使用者 |
| token | TEXT | UNIQUE, NOT NULL | RT 字串本身（內含 jti 供稽核） |
| expires_at | TIMESTAMPTZ | NOT NULL | 過期時間（7 天） |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 簽發時間 |

### 2.5 token_usage_logs (Token 使用流水帳)

**Append-only** 對帳／報表用流水；每次 LLM 輪次結束寫一列（同一 `chat_id` 可多列），不在串流途中寫入。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 使用者 |
| chat_id | UUID | FK → chats.id, ON DELETE SET NULL | 所屬對話（V002 加入，按對話統計費用） |
| message_id | UUID | NULLABLE，**無 FK 約束** | 關聯最後一則 assistant message（若有） |
| caller | VARCHAR(50) | NULLABLE | LLM 輪次來源：router、analyst 等（V004 加入） |
| model_name | VARCHAR(100) | NULLABLE | 模型名稱 |
| prompt_tokens | INTEGER | DEFAULT 0 | 輸入 Token |
| completion_tokens | INTEGER | DEFAULT 0 | 輸出 Token |
| total_tokens | INTEGER | DEFAULT 0 | 總 Token |
| cost_usd | NUMERIC(10, 6) | NULLABLE | 粗估成本（美元） |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 寫入時間 |

### 2.6 quota_reset_logs (配額重置紀錄)

由姊妹專案 **Insight-Monitor** 或維運 SQL 寫入；**本專案主程式不讀寫此表**（V006 加入）。記錄每次手動重置 `user_usage_quotas` 前的區間摘要，供對帳；重置時只歸零 `used_tokens`，`token_usage_logs` 永久保留。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 使用者 |
| reset_at | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | 重置時間 |
| previous_period_start | TIMESTAMPTZ | NULLABLE | 重置前的週期起點 |
| previous_used_tokens | BIGINT | NOT NULL, DEFAULT 0 | 重置前累計用量 |
| period_total_tokens | BIGINT | NULLABLE | 該區間流水加總 Token |
| period_total_cost_usd | NUMERIC(10, 6) | NULLABLE | 該區間流水加總成本 |
| note | TEXT | NULLABLE | 備註 |
| reset_by | VARCHAR(100) | DEFAULT 'monitor' | 操作來源 |

### 2.7 roles / user_roles (權限角色)

RBAC 基礎表（目前保留供後台擴充）。

| 表 | 欄位 | 說明 |
| :--- | :--- | :--- |
| roles | id (UUID PK)、name (VARCHAR(50) UNIQUE)、description (TEXT) | 角色定義：admin / user / guest |
| user_roles | user_id + role_id（複合 PK，皆 FK ON DELETE CASCADE） | 使用者↔角色多對多關聯 |

### 2.8 user_settings (使用者偏好)

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| user_id | UUID | PRIMARY KEY, FK → users.id, ON DELETE CASCADE | 使用者 |
| theme | VARCHAR(20) | DEFAULT 'dark' | 主題 (dark / light) |
| language | VARCHAR(10) | DEFAULT 'zh-TW' | 語言 (zh-TW / en) |
| notifications_enabled | BOOLEAN | DEFAULT TRUE | 通知開關 |
| settings | JSONB | DEFAULT '{}' | 其他偏好 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 更新時間 |

### 2.9 projects (專案)

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 專案唯一識別碼 |
| name | VARCHAR(255) | NOT NULL | 專案名稱 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 建立者 |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 建立時間 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 更新時間 |

### 2.10 chats (對話)

`project_id` 為 **nullable**：chat 可獨立存在於 project 之外（對應前端 sidebar「最近」區塊）；因此另設 `user_id` **NOT NULL** 直接記錄 owner，確保無 project 的 chat 仍可做 ownership 驗證。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 對話唯一識別碼 |
| project_id | UUID | FK → projects.id, NULLABLE, ON DELETE CASCADE | 所屬專案（可為空） |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者 |
| title | VARCHAR(255) | NOT NULL | 對話標題 |
| title_generated | BOOLEAN | NOT NULL, DEFAULT FALSE | FALSE=placeholder（截斷 query），TRUE=LLM 已產出正式標題 |
| summary | TEXT | NULLABLE | LLM 產生的對話摘要，優化 Context 載入 |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 建立時間 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 更新時間 |

### 2.11 messages (訊息)

透過 `parent_id` 自參照實現 Parent DAG 結構：支援重新生成（多個 assistant 共用同一 parent）、精確溯源與 Recursive CTE 上下文載入（見 [`chat_context.py`](../app/backend/module/chat_context.py)）。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 訊息唯一識別碼 |
| chat_id | UUID | FK → chats.id, NOT NULL, ON DELETE CASCADE | 所屬對話 |
| parent_id | UUID | FK → messages.id, ON DELETE SET NULL | 父訊息：assistant 指向對應 user；user 指向前一則訊息以串成主線 |
| role | VARCHAR(50) | NOT NULL | user / assistant |
| content | TEXT | NOT NULL | 訊息內容 |
| tokens | JSONB | NOT NULL, DEFAULT `{"prompt":0, "completion":0, "total":0, "is_cached": false}` | Token 消耗詳情 |
| context_refs | JSONB | NULLABLE | 檢索來源片段（溯源） |
| metadata | JSONB | NULLABLE | 系統元數據（如 System Prompt） |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 發送時間 |

### 2.12 files (檔案管理)

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 檔案唯一識別碼 |
| project_id | UUID | FK → projects.id, NOT NULL, ON DELETE CASCADE | 所屬專案 |
| chat_id | UUID | FK → chats.id, NULLABLE, ON DELETE SET NULL | 關聯對話（可為空） |
| file_name | VARCHAR(255) | NOT NULL | 檔案名稱 |
| s3_url | TEXT | NOT NULL | 儲存路徑 |
| file_type | VARCHAR(50) | NOT NULL | 類型 (image, pdf, etc.) |
| status | VARCHAR(50) | NOT NULL | 狀態 (uploading, ready, failed) |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 上傳時間 |

### 2.13 user_feedback (使用者建議回饋)

存放登入使用者透過 `POST /api/user/feedback` 提交的建議、問題回報與使用體驗回饋。使用者只需填「類型」與「內容」；其餘欄位由前端自動附帶，方便日後排查。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 回饋唯一識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 提交者；刪除帳號時一併清除 |
| category | VARCHAR(32) | NOT NULL | 回饋類型（見下方列舉） |
| message | TEXT | NOT NULL | 使用者手動輸入的回饋正文（10～2000 字） |
| page_url | VARCHAR(500) | NULLABLE | 送出當下的頁面路徑（前端自動帶入） |
| user_agent | TEXT | NULLABLE | 瀏覽器 User Agent 字串（前端自動帶入，最多 512 字元） |
| context | JSONB | DEFAULT `'{}'` | 送出當下的頁面情境快照（前端自動帶入，最多 20 個 key） |
| status | VARCHAR(20) | NOT NULL, DEFAULT `'new'` | 回饋處理狀態（見下方列舉） |
| tokens_granted | BIGINT | NOT NULL, DEFAULT `0` | 本次回饋發放的 Token 獎勵（正常提交為 2500；供每日次數稽核；V008 加入） |
| created_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 使用者第一次送出的時間 |
| updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | 這筆回饋最後一次被修改的時間 |

#### category 列舉值

| 值 | 前端顯示 | 說明 |
| :--- | :--- | :--- |
| `feature` | 功能建議 | 希望新增或改進的功能 |
| `bug` | 問題回報 | 錯誤、異常、功能失效 |
| `ux` | 使用體驗 | 介面、流程、易用性 |
| `billing` | 方案與計費 | 訂閱、配額、付款相關 |
| `other` | 其他 | 不屬於以上類別 |

#### status 列舉值與工單流程設計

`status` 欄位**刻意設計成可支援多種工單狀態**，而非只有「已送出／未送出」二元值。回饋在本質上是一張**內部工單**：使用者提交後，團隊需要追蹤「是否已讀、是否處理中、是否已回覆、是否結案」。預先定義完整狀態流，可避免日後做管理後台時再改 schema 或做資料遷移。

**目前實際行為**

- `POST /api/user/feedback` 建立回饋時，**固定寫入 `'new'`**（資料表 `DEFAULT 'new'` 與 API 邏輯一致）
- 使用者端**不會**也**不應**修改 `status`
- 其餘狀態保留給**日後管理後台**（例如 Insight-Monitor）以 `UPDATE` 推進工單

**狀態一覽**

| 狀態 | 目前是否使用 | 說明 |
| :--- | :---: | :--- |
| `new` | ✅ 是 | 剛收到，尚未處理（**預設值**；API 建立時固定寫入） |
| `reviewed` | ❌ 日後 | 已閱讀／已分派給負責人 |
| `in_progress` | ❌ 日後 | 處理中（調查、修復、回覆草稿等） |
| `resolved` | ❌ 日後 | 已解決或已回覆使用者 |
| `closed` | ❌ 日後 | 關閉，不再追蹤（例如重複回報、無法重現、使用者撤回） |

**典型狀態流（日後後台）**

```mermaid
stateDiagram-v2
    [*] --> new : 使用者送出回饋
    new --> reviewed : 管理員已讀／分派
    reviewed --> in_progress : 開始處理
    in_progress --> resolved : 已回覆或已修復
    resolved --> closed : 結案歸檔
    new --> closed : 直接關閉（重複／無效）
    in_progress --> closed : 無法處理而關閉
```

狀態不必嚴格線性；上圖表示常見路徑，實際後台可允許合理跳轉（例如 `new` → `closed`）。

**型別選擇：為何用 `VARCHAR(20)` 而非 PostgreSQL `ENUM`？**

- 日後新增狀態（例如 `spam`、`duplicate`）只需改應用層約束，**不必** `ALTER TYPE` 或重建 enum
- 與專案其他狀態欄位（如 `users.status`、`files.status`）風格一致
- 20 字元已足夠容納上述狀態名稱

#### message 與 context 的差異

兩者分工不同：**message 是使用者說的話；context 是系統記的環境**。

| | message | context |
| :--- | :--- | :--- |
| **誰填** | 使用者手動輸入 | 前端自動帶入 |
| **型別** | 純文字 `TEXT` | 結構化 JSON `JSONB` |
| **用途** | 回饋的**主要內容** | 回饋發生時的**環境快照**，方便排查 |

**context 範例**（前端 `window.getFeedbackPageContext()` 自動附帶）：

```json
{
  "chat_id": "abc-123",
  "project_id": "def-456",
  "chat_mode": "general",
  "response_mode": "thinking"
}
```

聯絡方式不需另存：提交者身分與 `users.email` 已可關聯查詢。

### 2.14 note_pages（筆記頁）

筆記功能（V009，V010 加 `source_chat_id`）。一個使用者可以有多頁，每頁是一塊獨立空間。
從對話整理出的筆記，**一則對話對應一頁**：同一則對話再整理會回到同一頁累加。手動建立的頁沒有來源對話。
API 見 [`api/notes.py`](../app/backend/api/notes.py)。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者 |
| title | VARCHAR(100) | NOT NULL | 頁面名稱 |
| position | INTEGER | NOT NULL, DEFAULT 0 | 側欄排序 |
| source_chat_id | UUID | FK → chats.id, ON DELETE SET NULL | 來源對話（V010）；手動建立的頁為 NULL。對話刪除後頁面保留 |
| created_at / updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | |

約束：`ux_note_pages_user_chat UNIQUE (user_id, source_chat_id) WHERE source_chat_id IS NOT NULL`（一則對話最多對應一頁；手動建立的頁不受限制）。
上限：每位使用者 50 頁（API 層）。

### 2.15 notes（筆記）

單則筆記。`canvas_*` 是之後白板視圖的座標，目前不寫入（NULL = 尚未擺放），先留欄位，避免以後再做一次 migration。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| page_id | UUID | FK → note_pages.id, NOT NULL, ON DELETE CASCADE | 所屬筆記頁 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者（冗餘存放，查詢時直接用來做授權條件） |
| title | VARCHAR(200) | NOT NULL | 標題 |
| content | TEXT | NOT NULL | 內容（LLM 產生或手動輸入的 Markdown，前端以純文字渲染；上限 20,000 字） |
| source_chat_id | UUID | FK → chats.id, ON DELETE SET NULL | 來源對話；對話刪除後筆記保留 |
| source_message_ids | JSONB | NULLABLE | 整理時選取的訊息 id 清單 |
| canvas_x / canvas_y / canvas_w / canvas_h | INTEGER | NULLABLE | 白板座標與尺寸（目前全為 NULL） |
| created_at / updated_at | TIMESTAMPTZ | DEFAULT CURRENT_TIMESTAMP | |

上限：每頁 500 則（API 層）。

### 2.16 portfolio_items（投資組合：庫存／觀察清單）

使用者在「投資組合」頁維護的股票（V011）。排程分析與庫存總覽都從這張表讀。流程見 [`portfolio_insight_spec.md`](./portfolio_insight_spec.md)。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者 |
| list_type | VARCHAR(10) | NOT NULL, CHECK IN (`HOLDING`, `WATCHLIST`) | `HOLDING` = 已買庫存；`WATCHLIST` = 觀察清單 |
| market | VARCHAR(2) | NOT NULL, CHECK IN (`TW`, `US`) | 台股／美股 |
| symbol | VARCHAR(15) | NOT NULL | 代碼。台股存數字代碼（不含 `.TW`），美股大寫 ticker |
| name | VARCHAR(60) | NOT NULL | 股票名稱 |
| cost | NUMERIC(18,4) | NULLABLE | 每股成本價（原幣） |
| shares | NUMERIC(20,4) | NULLABLE | 持有股數 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT CURRENT_TIMESTAMP | 加入時間（圓餅圖依此決定每檔的顏色） |
| updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT CURRENT_TIMESTAMP | 最後修改時間 |

約束：
- `uq_portfolio_items_symbol UNIQUE (user_id, market, symbol)`：**同一檔只能在一個清單**，買進時從觀察清單「移到」庫存。
- `ck_portfolio_items_holding`：`HOLDING` 必須有 `cost >= 0` 與 `shares > 0`；`WATCHLIST` 可以沒有。
- 每個清單最多 30 檔（API 層限制，等同 Zentra `portfolio-advisor` 的上限）。

### 2.17 insight_schedules（投資組合：排程設定）

每位使用者一列。排程器以 `next_run_at` 認領到期的排程（`FOR UPDATE SKIP LOCKED`）。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| user_id | UUID | PRIMARY KEY, FK → users.id, ON DELETE CASCADE | 一人一列 |
| enabled | BOOLEAN | NOT NULL, DEFAULT TRUE | 是否啟用 |
| frequency | VARCHAR(10) | NOT NULL, CHECK IN (`DAILY`, `WEEKDAYS`, `WEEKLY`, `MONTHLY`) | 每天／週一至週五／每週／每月 |
| run_time | TIME | NOT NULL | 執行時間，**台北時間**（UTC+8） |
| weekday | SMALLINT | CHECK 1–7 | ISO 星期（1=週一）；`WEEKLY` 必填 |
| month_day | SMALLINT | CHECK 1–28 | 每月幾號；`MONTHLY` 必填（只到 28，避免二月沒有那天） |
| risk_tolerance | VARCHAR(12) | CHECK IN (`CONSERVATIVE`, `BALANCED`, `AGGRESSIVE`) | 風險偏好，傳給整體建議 |
| cash_twd | NUMERIC(18,2) | CHECK >= 0 | 可動用現金（台幣），傳給整體建議 |
| next_run_at | TIMESTAMPTZ | NULLABLE | 下一次執行時間（UTC）；NULL = 停用 |
| last_run_at | TIMESTAMPTZ | NULLABLE | 上一次被認領的排程時間點 |
| created_at / updated_at | TIMESTAMPTZ | NOT NULL, DEFAULT CURRENT_TIMESTAMP | |

約束：`ck_insight_schedules_weekly`（`WEEKLY` 必有 `weekday`）、`ck_insight_schedules_monthly`（`MONTHLY` 必有 `month_day`）。

### 2.18 insight_jobs（投資組合：每次分析）

每次執行（排程觸發或手動「立即分析」）一列，也保存 `portfolio-advisor` 的整體建議。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者 |
| trigger | VARCHAR(10) | NOT NULL, CHECK IN (`SCHEDULED`, `MANUAL`) | 觸發來源 |
| scheduled_for | TIMESTAMPTZ | NOT NULL | 排程的目標時間；手動 = 按下的時間 |
| status | VARCHAR(10) | NOT NULL, DEFAULT `QUEUED`, CHECK IN (`QUEUED`, `RUNNING`, `SUCCEEDED`, `PARTIAL`, `FAILED`) | 狀態（語意見 portfolio_insight_spec §1） |
| started_at | TIMESTAMPTZ | NULLABLE | 開始執行 |
| heartbeat_at | TIMESTAMPTZ | NULLABLE | 每完成一個 tool 更新；超過 30 分鐘沒動視為中斷 |
| finished_at | TIMESTAMPTZ | NULLABLE | 結束 |
| error | TEXT | NULLABLE | 失敗原因（契約不符、設定錯誤、清單為空、中斷…） |
| counts | JSONB | NULLABLE | `{"total", "succeeded", "failed"}`（個股 tool 呼叫數） |
| portfolio_status | VARCHAR(10) | CHECK IN (`SUCCEEDED`, `FAILED`, `SKIPPED`) | 整體建議的結果；`SKIPPED` = 沒有庫存 |
| portfolio_run_id | TEXT | NULLABLE | Zentra 的 run_id（追查用） |
| portfolio_output | JSONB | NULLABLE | `portfolio-advisor` 的完整輸出 |
| portfolio_error | TEXT | NULLABLE | 整體建議失敗原因 |
| created_at | TIMESTAMPTZ | NOT NULL, DEFAULT CURRENT_TIMESTAMP | 建立時間 |

約束（部分唯一索引）：
- `uq_insight_jobs_scheduled (user_id, scheduled_for) WHERE trigger = 'SCHEDULED'`：同一個排程時間點只會有一個 job。
- `uq_insight_jobs_active (user_id) WHERE status IN ('QUEUED','RUNNING')`：同一位使用者同時最多一個進行中的 job。

保留期：超過 `INSIGHT_RETENTION_DAYS`（預設 180 天）的已結束 job 會連同結果一起刪除。

### 2.19 insight_results（投資組合：每檔 × 每個 tool 的結果）

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 識別碼 |
| job_id | UUID | FK → insight_jobs.id, NOT NULL, ON DELETE CASCADE | 所屬分析 |
| user_id | UUID | FK → users.id, NOT NULL, ON DELETE CASCADE | 擁有者（冗餘，方便授權查詢） |
| list_type | VARCHAR(10) | NOT NULL, CHECK IN (`HOLDING`, `WATCHLIST`) | 執行當下在哪個清單 |
| market / symbol / name | VARCHAR | NOT NULL | 執行當下的快照 |
| cost / shares | NUMERIC | NULLABLE | 執行當下的快照（之後改清單不影響舊報告） |
| tool | VARCHAR(40) | NOT NULL, CHECK IN (`stock-news`, `stock-chart`, `stock-technical`, `stock-fundamental`) | 哪個 Zentra tool |
| status | VARCHAR(10) | NOT NULL, CHECK IN (`SUCCEEDED`, `FAILED`) | 結果 |
| run_id | TEXT | NULLABLE | Zentra 的 run_id |
| output | JSONB | NULLABLE | tool 的完整輸出（K 線圖的 base64 已移到 `chart_png`） |
| chart_png | BYTEA | NULLABLE | 只有 stock-chart 有（約 100 KB）；超過 `INSIGHT_CHART_RETENTION_DAYS`（預設 30 天）清成 NULL |
| error | TEXT | NULLABLE | 失敗原因 |
| started_at / finished_at | TIMESTAMPTZ | NULLABLE | 執行起訖 |

約束：`uq_insight_results_cell UNIQUE (job_id, market, symbol, tool)`。

---

## 3. 索引設計 (Performance Indexing)

索引皆定義於 `init_db.sql` §15（token_usage_logs 複合索引另見 V003；筆記見 V009／V010；投資組合見 V011）。

| 索引 | 欄位 | 對應熱點查詢 |
| :--- | :--- | :--- |
| idx_users_email | users(email) | 登入／帳號查找 |
| idx_token_usage_logs_user_chat | (user_id, chat_id) | 某使用者某對話 SUM／GROUP BY model_name |
| idx_token_usage_logs_user_created_at | (user_id, created_at DESC) | 使用者時間區間報表 |
| idx_token_usage_logs_chat_created_at | (chat_id, created_at DESC) | 某對話時間序明細 |
| idx_token_usage_logs_created_at | (created_at DESC) | 全站／管理端時間掃描（搭配 LIMIT） |
| idx_quota_reset_logs_user_reset_at | (user_id, reset_at DESC) | 查使用者最近重置紀錄 |
| idx_projects_user_id / idx_projects_updated_at | user_id · updated_at DESC | sidebar 專案列表 |
| idx_chats_project_id / idx_chats_user_id / idx_chats_created_at / idx_chats_updated_at | — | sidebar 對話列表（最近排序） |
| idx_messages_chat_id / idx_messages_parent_id / idx_messages_created_at | — | 對話流讀取、parent_id 遞迴回溯 |
| **idx_messages_chat_id_created_at_desc** | (chat_id, created_at DESC) | **Cursor-based 歷史分頁**：`WHERE chat_id=$1 AND created_at<$2 ORDER BY created_at DESC LIMIT N`，複合索引一次走完 filter + sort，避免 in-memory sort |
| idx_files_project_id / idx_files_chat_id | — | 檔案列表 |
| idx_user_feedback_user_created_at | (user_id, created_at DESC) | 使用者回饋歷史 |
| idx_user_feedback_status_created_at | (status, created_at DESC) | 後台依狀態篩選待辦（`WHERE status='new' ORDER BY created_at DESC`） |
| idx_note_pages_user | (user_id, position, created_at) | 筆記頁側欄排序 |
| ux_note_pages_user_chat | (user_id, source_chat_id) WHERE source_chat_id IS NOT NULL | 一則對話對應一頁（唯一）；整理時找回同一頁 |
| idx_notes_page / idx_notes_user | (page_id, created_at DESC) · (user_id, created_at DESC) | 某頁的筆記、我的所有筆記 |
| idx_portfolio_items_user | (user_id, list_type, created_at) | 投資組合清單 |
| uq_portfolio_items_symbol | UNIQUE (user_id, market, symbol) | 同一檔只能在一個清單；新增時撞到即回 409 |
| uq_insight_jobs_scheduled | UNIQUE (user_id, scheduled_for) WHERE trigger = 'SCHEDULED' | 同一個排程時間點只產生一個 job（排程器重複認領也不會跑兩次） |
| uq_insight_jobs_active | UNIQUE (user_id) WHERE status IN ('QUEUED','RUNNING') | 同一位使用者同時最多一個進行中的 job |
| uq_insight_results_cell | UNIQUE (job_id, market, symbol, tool) | 一次分析內每檔 × 每個 tool 只寫一列 |
| idx_insight_schedules_due | (next_run_at) WHERE enabled AND next_run_at IS NOT NULL | 排程器每輪找到期的排程 |
| idx_insight_jobs_user | (user_id, created_at DESC) | 分析紀錄列表 |
| idx_insight_jobs_queued | (created_at) WHERE status = 'QUEUED' | 排程器認領排隊中的 job |
| idx_insight_results_job | (job_id) | 讀單次分析的所有結果 |

---

## 4. Migration 歷史

| 版本 | 內容 |
| :--- | :--- |
| V002 | `token_usage_logs` 加入 `chat_id`（FK → chats） |
| V003 | `token_usage_logs` 複合索引 |
| V004 | `token_usage_logs` 加入 `caller`（router / analyst 輪次來源） |
| V005 | `subscription_tiers` 種子資料（free / pro / ultra） |
| V006 | 新增 `quota_reset_logs` 表 |
| V007 | 新增 `user_feedback` 表 |
| V008 | `user_feedback` 加入 `tokens_granted` |
| V009 | 新增 `note_pages`、`notes`（筆記功能） |
| V010 | `note_pages` 加入 `source_chat_id` 與唯一索引（一則對話對應一頁） |
| V011 | 投資組合排程分析：`portfolio_items`、`insight_schedules`、`insight_jobs`、`insight_results`（見 [`portfolio_insight_spec.md`](./portfolio_insight_spec.md)） |

新環境直接執行 `init_db.sql`（已含所有變更、冪等）；既有環境依序套用 `migrations/`。
