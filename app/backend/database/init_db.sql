-- 1. 開啟 UUID 擴充功能 (gen_random_uuid 必備)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 2. 建立 subscription_tiers 表 (訂閱等級定義)
CREATE TABLE IF NOT EXISTS subscription_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL, -- free, pro, ultra
    monthly_token_limit BIGINT NOT NULL,
    max_projects INTEGER DEFAULT 10,
    features JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 種子：訂閱等級（與 README／usage_quota.DEFAULT_FALLBACK_MONTHLY_LIMIT 對齊）
INSERT INTO subscription_tiers (name, monthly_token_limit, max_projects)
VALUES
    ('free', 200000, 10),
    ('pro', 1000000, 20),
    ('ultra', 5000000, 999999)
ON CONFLICT (name) DO UPDATE SET
    monthly_token_limit = EXCLUDED.monthly_token_limit,
    max_projects = EXCLUDED.max_projects;

-- 3. 建立 users 表 (會員系統核心)
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    status VARCHAR(20) DEFAULT 'active', -- active, disabled, pending
    tier_id UUID REFERENCES subscription_tiers(id) ON DELETE SET NULL, -- 關聯訂閱等級
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 4. 建立 user_usage_quotas 表 (使用者當前週期的 Token 用量累計)
-- 採用此表進行高頻更新，與 logs 分開以優化效能
CREATE TABLE IF NOT EXISTS user_usage_quotas (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_period_start TIMESTAMPTZ NOT NULL, -- 當前計費/計量週期的開始時間
    used_tokens BIGINT DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 5. 建立 token_usage_logs 表 (Token 使用詳細流水帳)
-- 寫入時機：可為「每次 LLM 結束一筆」（同一 chat_id 多列），或舊版「每次對話結束一筆」；
-- 不在串流途中寫入。與 user_usage_quotas 分開：
--   quotas = 即時計數器；logs = append-only 對帳／報表／依模型統計
CREATE TABLE IF NOT EXISTS token_usage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_id UUID REFERENCES chats(id) ON DELETE SET NULL, -- 關聯到 chats 表，方便按對話查詢費用
    message_id UUID,                                      -- 關聯到最後一則 assistant message (若有)
    caller VARCHAR(50),                                   -- LLM 輪次來源：router、analyst 等（可 NULL）
    model_name VARCHAR(100),
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC(10, 6),
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 6. 建立 roles 表 (權限角色)
CREATE TABLE IF NOT EXISTS roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL, -- admin, user, guest
    description TEXT
);

-- 7. 建立 user_roles (使用者與角色的關聯)
CREATE TABLE IF NOT EXISTS user_roles (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

-- 8. 建立 user_settings (使用者偏好設定)
CREATE TABLE IF NOT EXISTS user_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    theme VARCHAR(20) DEFAULT 'dark',
    language VARCHAR(10) DEFAULT 'zh-TW',
    notifications_enabled BOOLEAN DEFAULT TRUE,
    settings JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 9. 建立 refresh_tokens 表 (JWT 刷新權杖)
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 10. 建立 projects 表 (專案頂層容器)
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 11. 建立 chats 表 (隸屬於專案下的對話視窗)
-- project_id 為 nullable：chat 可獨立存在於 project 之外（對應前端 sidebar 的「最近」區塊）
-- user_id 必填：直接記錄 owner，避免「無 project_id」的 chat 無法做 ownership 驗證
-- title_generated：FALSE = 仍為 placeholder（截斷 query），TRUE = 已由 LLM 產出正式 title
CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    title_generated BOOLEAN NOT NULL DEFAULT FALSE,
    summary TEXT, -- 存儲 LLM 產生的對話摘要，優化下次載入 Context 的速度
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 12. 建立 messages 表 (存放每一筆對話記錄)
-- 透過 parent_id 實現訊息與回覆的精確對齊與溯源
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES messages(id) ON DELETE SET NULL, -- 父訊息：assistant 指向對應 user；user 建議指向前一則訊息（如上一則 assistant）以串成主線，供遞迴上下文載入
    role VARCHAR(50) NOT NULL, -- user / assistant
    content TEXT NOT NULL,
    tokens JSONB NOT NULL DEFAULT '{"prompt":0, "completion":0, "total":0, "is_cached": false}',
    context_refs JSONB,        -- 存儲檢索到的來源與片段 (方案 B)
    metadata JSONB,            -- 存儲系統元數據如 System Prompt
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 13. 建立 files 表 (專案共用的知識庫文件)
CREATE TABLE IF NOT EXISTS files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chat_id UUID REFERENCES chats(id) ON DELETE SET NULL,
    file_name VARCHAR(255) NOT NULL,
    s3_url TEXT NOT NULL,
    file_type VARCHAR(50) NOT NULL, -- image, pdf, etc.
    status VARCHAR(50) NOT NULL,    -- uploading, ready, failed
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- =============================================
-- 14. 建立索引優化 (Performance Indexing)
-- =============================================

-- 使用者與權限相關
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
-- token_usage_logs：同一 chat 可多列（每輪 LLM）；複合索引對齊熱點查詢
--   (user_id, chat_id)        → 某使用者某一對話 SUM／GROUP BY model_name
--   (user_id, created_at DESC)→ 某使用者時間區間報表、ORDER BY 時間
--   (chat_id, created_at DESC)→ 某對話時間序明細（chat_id 可為 NULL 時略過索引匹配）
--   (created_at DESC)         → 全站／管理端依時間掃描（搭配 LIMIT）
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_chat
    ON token_usage_logs(user_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_created_at
    ON token_usage_logs(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_chat_created_at
    ON token_usage_logs(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_created_at ON token_usage_logs(created_at DESC);

-- 專案與對話 (最核心的查詢路徑)
CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_chats_project_id ON chats(project_id);
CREATE INDEX IF NOT EXISTS idx_chats_user_id ON chats(user_id);
CREATE INDEX IF NOT EXISTS idx_chats_created_at ON chats(created_at);

-- 訊息表 (支援 Parent ID 遞迴查詢與對話流讀取)
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent_id ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
-- Cursor-based 歷史訊息分頁的主要熱點查詢：
--   WHERE chat_id = $1 AND created_at < $2 ORDER BY created_at DESC LIMIT N
-- (chat_id, created_at DESC) 複合索引可一次走完 filter + sort，避免 in-memory sort
CREATE INDEX IF NOT EXISTS idx_messages_chat_id_created_at_desc
    ON messages(chat_id, created_at DESC);

-- 文件管理
CREATE INDEX IF NOT EXISTS idx_files_project_id ON files(project_id);
CREATE INDEX IF NOT EXISTS idx_files_chat_id ON files(chat_id);

