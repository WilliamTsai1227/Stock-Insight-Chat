-- ============================================================
-- Migration V004: token_usage_logs 新增 caller（router / analyst 等）
-- ============================================================

BEGIN;

ALTER TABLE token_usage_logs
    ADD COLUMN IF NOT EXISTS caller VARCHAR(50);

COMMENT ON COLUMN token_usage_logs.caller IS 'LLM 輪次來源：router、analyst 等（可 NULL）';

COMMIT;
