-- Migration V007: 使用者建議回饋
-- 使用者透過 POST /api/user/feedback 提交；後台日後可擴充審阅流程

CREATE TABLE IF NOT EXISTS user_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    page_url VARCHAR(500),
    user_agent TEXT,
    context JSONB DEFAULT '{}',
    status VARCHAR(20) NOT NULL DEFAULT 'new',
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_user_feedback_user_created_at
    ON user_feedback(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_user_feedback_status_created_at
    ON user_feedback(status, created_at DESC);
