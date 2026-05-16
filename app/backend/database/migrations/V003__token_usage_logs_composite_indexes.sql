-- ============================================================
-- Migration V003: token_usage_logs 複合索引（每輪 LLM 多列情境）
-- 建立時間: 2026-05-15
-- 說明:
--   1. 新增 (user_id, chat_id)、(user_id, created_at DESC)、(chat_id, created_at DESC)
--      以利對話內 SUM、使用者時間區間報表、對話時間序明細。
--   2. 移除舊單欄 idx_token_usage_logs_user_id、idx_token_usage_logs_chat_id，
--      避免與複合索引前綴重複、加重 INSERT 負擔（保留 idx_token_usage_logs_created_at）。
-- ============================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_chat
    ON token_usage_logs(user_id, chat_id);

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_user_created_at
    ON token_usage_logs(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_token_usage_logs_chat_created_at
    ON token_usage_logs(chat_id, created_at DESC);

DROP INDEX IF EXISTS idx_token_usage_logs_user_id;
DROP INDEX IF EXISTS idx_token_usage_logs_chat_id;

COMMIT;
