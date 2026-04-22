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
CREATE TABLE IF NOT EXISTS token_usage_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id UUID, -- 關聯到 messages 表 (若有)
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
CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    summary TEXT, -- 存儲 LLM 產生的對話摘要，優化下次載入 Context 的速度
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 12. 建立 messages 表 (存放每一筆對話記錄)
-- 透過 parent_id 實現訊息與回覆的精確對齊與溯源
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    parent_id UUID REFERENCES messages(id) ON DELETE SET NULL, -- 父訊息 ID，用於 Q&A 對齊
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
