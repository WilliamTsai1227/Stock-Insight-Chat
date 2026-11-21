-- V012：區分對話來源（主聊天室 / 浮動 AI 小助手）
--
-- 浮動 AI 小助手的對話也存在 chats（沿用訊息、用量、標題等既有機制），
-- 但不該出現在主側欄「最近對話」。用 source 欄位區分，GET /api/chat/all 只列 main。
-- 既有資料一律視為 main。須在部署新後端「之前」套用（新程式碼會查這個欄位）。

ALTER TABLE chats
    ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'main';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chk_chats_source'
    ) THEN
        ALTER TABLE chats
            ADD CONSTRAINT chk_chats_source CHECK (source IN ('main', 'assistant'));
    END IF;
END $$;
