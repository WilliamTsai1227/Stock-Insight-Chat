-- 1. 開啟 UUID 擴充功能 (gen_random_uuid 必備)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 2. 建立 projects 表 (專案頂層容器)
CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 3. 建立 chats 表 (隸屬於專案下的對話視窗)
CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 4. 建立 messages 表 (存放每一筆對話記錄)
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 5. 建立 files 表 (專案共用的知識庫文件)
CREATE TABLE IF NOT EXISTS files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chat_id UUID REFERENCES chats(id) ON DELETE SET NULL,
    file_name VARCHAR(255) NOT NULL,
    s3_url TEXT NOT NULL,
    file_type VARCHAR(50) NOT NULL, -- image, pdf, etc.
    status VARCHAR(50) NOT NULL,    -- uploading, ready, failed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
