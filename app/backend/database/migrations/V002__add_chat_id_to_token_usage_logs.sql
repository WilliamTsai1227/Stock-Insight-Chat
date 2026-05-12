-- ============================================================
-- Migration V002: add chat_id to token_usage_logs
-- 建立時間: 2026-05-13
-- 說明:
--   1. 在 token_usage_logs 新增 chat_id 欄位，關聯 chats 表，
--      方便按「對話」維度查詢費用明細。
--   2. 新增對應索引 idx_token_usage_logs_chat_id。
--
-- ⚠️  安全性確認：
--   - 使用 ADD COLUMN IF NOT EXISTS，可重複執行不會報錯。
--   - 欄位允許 NULL，不影響現有資料列（舊資料 chat_id 保持 NULL）。
--   - 使用 CREATE INDEX IF NOT EXISTS，可重複執行不會報錯。
-- ============================================================

BEGIN;

-- 1. 新增 chat_id 欄位（允許 NULL，舊資料不受影響）
ALTER TABLE token_usage_logs
    ADD COLUMN IF NOT EXISTS chat_id UUID REFERENCES chats(id) ON DELETE SET NULL;

-- 2. 新增索引（方便按 chat_id 查詢費用）
CREATE INDEX IF NOT EXISTS idx_token_usage_logs_chat_id ON token_usage_logs(chat_id);

COMMIT;
