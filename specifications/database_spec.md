# 資料庫規格說明書 (Database Specification)

本專案使用 **PostgreSQL** 作為關聯式資料庫，用於管理會員、訂閱、專案、對話、訊息以及上傳的文件。向量數據則存儲於 **Qdrant**。

## 1. 實體關係圖 (ERD)

```mermaid
erDiagram
    subscription_tiers ||--o{ users : "定義等級"
    users ||--o{ projects : "擁有"
    users ||--o| user_usage_quotas : "目前用量"
    users ||--o{ token_usage_logs : "使用日誌"
    users ||--o{ user_roles : "具備角色"
    roles ||--o{ user_roles : "定義角色"
    users ||--o| user_settings : "個人偏好"
    projects ||--o{ chats : "包含"
    projects ||--o{ files : "相關文件"
    chats ||--o{ messages : "對話記錄"
    messages ||--o{ messages : "Q&A 對齊 (parent_id)"

    subscription_tiers {
        uuid id PK
        string name "free / pro / ultra"
        bigint monthly_token_limit
        int max_projects
        jsonb features
    }

    users {
        uuid id PK
        string email
        string username
        string password_hash
        string status "active / disabled"
        uuid tier_id FK
    }

    user_usage_quotas {
        uuid user_id PK, FK
        timestamp current_period_start
        bigint used_tokens
        timestamp updated_at
    }

    token_usage_logs {
        uuid id PK
        uuid user_id FK
        uuid message_id FK
        string model_name
        int prompt_tokens
        int completion_tokens
        int total_tokens
        numeric cost_usd
        timestamp created_at
    }

    user_settings {
        uuid user_id PK, FK
        string theme "dark / light"
        string language "zh-TW / en"
        boolean notifications_enabled
        jsonb settings
    }

    projects {
        uuid id PK
        string name
        uuid user_id FK
        timestamp created_at
    }

    files {
        uuid id PK
        uuid project_id FK
        uuid chat_id FK
        string file_name
        string s3_url
        string file_type
        string status "uploading / ready / failed"
    }

    chats {
        uuid id PK
        uuid project_id FK
        string title
        timestamp created_at
    }

    messages {
        uuid id PK
        uuid chat_id FK
        uuid parent_id FK
        string role "user / assistant"
        text content
        jsonb tokens "prompt/completion/total"
        jsonb context_refs "引用來源"
        jsonb metadata "系統元數據"
        timestamp created_at
    }
```

---

## 2. 資料表詳細定義

### 2.1 subscription_tiers (訂閱等級)
定義不同會員等級的權利與配額。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 等級唯一識別碼 |
| name | VARCHAR(50) | UNIQUE, NOT NULL | 等級名稱 (free, pro, ultra) |
| monthly_token_limit | BIGINT | NOT NULL | 每月 Token 額度 |
| max_projects | INTEGER | DEFAULT 3 | 最大專案數 |
| features | JSONB | NULLABLE | 功能開關設定 |

### 2.2 users (使用者)
會員系統核心表。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 使用者唯一識別碼 |
| email | VARCHAR(255) | UNIQUE, NOT NULL | 電子郵件 (登入帳號) |
| username | VARCHAR(100) | UNIQUE, NOT NULL | 顯示名稱 |
| password_hash | TEXT | NOT NULL | 加密後的密碼 |
| status | VARCHAR(20) | DEFAULT 'active' | 帳號狀態 (active, disabled) |
| tier_id | UUID | FK -> subscription_tiers.id | 目前等級 |

### 2.3 user_usage_quotas (當前用量)
紀錄使用者在當前計費週期內的即時累計用量。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| user_id | UUID | PRIMARY KEY, FK -> users.id | 使用者 ID |
| current_period_start | TIMESTAMP | NOT NULL | 當前週期開始時間 |
| used_tokens | BIGINT | DEFAULT 0 | 已消耗總 Token 數 |
| updated_at | TIMESTAMP | DEFAULT NOW() | 最後更新時間 |

### 2.4 projects (專案)
存放頂層容器資訊。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 專案唯一識別碼 |
| name | VARCHAR(255) | NOT NULL | 專案名稱 |
| user_id | UUID | FK -> users.id, NOT NULL | 建立者 ID |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 建立時間 |

### 2.5 files (檔案管理)
管理專案相關的附件與上傳文件。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 檔案唯一識別碼 |
| project_id | UUID | FK -> projects.id | 所屬專案 |
| s3_url | TEXT | NOT NULL | 儲存路徑 |
| status | VARCHAR(20) | NOT NULL | 狀態 (ready, failed, etc.) |

### 2.6 messages (訊息)
存放每一筆對話記錄與 Token 消耗。

| 欄位名稱 | 資料型別 | 限制 | 說明 |
| :--- | :--- | :--- | :--- |
| id | UUID | PRIMARY KEY | 訊息唯一識別碼 |
| chat_id | UUID | FK -> chats.id | 所屬對話 ID |
| parent_id | UUID | FK -> messages.id | 父訊息 ID (用於追溯) |
| tokens | JSONB | NOT NULL | 消耗詳情 `{ "prompt": 100, "completion": 50 }` |
| context_refs | JSONB | NULLABLE | 檢索來源片段 |
| created_at | TIMESTAMP | DEFAULT CURRENT_TIMESTAMP | 發送時間 |
