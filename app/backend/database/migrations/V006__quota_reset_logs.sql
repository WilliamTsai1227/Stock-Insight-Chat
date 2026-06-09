-- Migration V006: quota_reset_logs（配額重置區間紀錄）
-- 與 init_db.sql §12-A 等價；既有資料庫請手動執行本檔。
-- 由 Insight-Monitor 寫入；Stock-Insight-Chat 主程式不讀寫此表。

BEGIN;

CREATE TABLE IF NOT EXISTS quota_reset_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reset_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    previous_period_start TIMESTAMPTZ,
    previous_used_tokens BIGINT NOT NULL DEFAULT 0,
    period_total_tokens BIGINT,
    period_total_cost_usd NUMERIC(10, 6),
    note TEXT,
    reset_by VARCHAR(100) DEFAULT 'monitor'
);

CREATE INDEX IF NOT EXISTS idx_quota_reset_logs_user_reset_at
    ON quota_reset_logs(user_id, reset_at DESC);

COMMIT;
