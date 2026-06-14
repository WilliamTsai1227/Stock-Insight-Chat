-- Migration V008: 建議回饋 Token 獎勵紀錄
-- 每次成功提交且發放獎勵時寫入 tokens_granted；供每日次數統計與稽核

ALTER TABLE user_feedback
    ADD COLUMN IF NOT EXISTS tokens_granted BIGINT NOT NULL DEFAULT 0;

COMMENT ON COLUMN user_feedback.tokens_granted IS
    '本次回饋發放的 Token 獎勵（0 表示未發放；正常提交為 FEEDBACK_TOKEN_REWARD）';
